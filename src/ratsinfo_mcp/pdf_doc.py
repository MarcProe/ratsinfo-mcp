"""PDF-Volltext als Markdown (Tool ``pdf_as_markdown``).

Lädt ein öffentliches RIS-PDF (``/sdnetrim/… .pdf``) direkt – **ohne
Session** (verifiziert: PDF-Endpunkte antworten mit 200 + ``application/pdf``
auf einen einfachen GET, kein reqid/csrftoken nötig) – und liefert den
Volltext als Markdown mit je einer ``## Seite N``-Überschrift pro Seite.

WICHTIG / Guards:
  * Nur PDF-URLs desselben Hosts wie ``RIS_BASE_URL`` (SSRF-Schutz).
  * ``#search=…``-Fragment wird entfernt (es ist nur ein Browser-Bookmark).
  * Max. Größe (``RIS_PDF_MAX_BYTES``, Default 20 MB) und Max. Seitenzahl
    (``RIS_PDF_MAX_SEITEN``, Default 500) begrenzen den Arbeitsspeicher.
  * Extraktion mit **pypdf** (pure Python – keine nativen Wheel-Probleme im
    mcphub/uvx-Umfeld; Textqualität nachweislich gleichwertig zu PyMuPDF auf
    den getesteten RIS-Dokumenten).
  * In-Memory-LRU-Cache (TTL 1 h, max. 8 Dokumente) pro Server-Lauf, damit
    wiederholte Aufrufe desselben Dokuments das 1-s-Rate-Limit nicht erneut
    belasten. Kein Disk-State.

``suchbegriff`` und ``seiten`` schränken die gelieferten Seiten ein;
``max_chars`` kappt den finalen Markdown-Text (Client-Cap, wie ``limit`` bei
``recherche``).
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import pypdf

from .config import Config
from .http_client import HttpClient, RISHTTPError

logger = logging.getLogger("ratsinfo_mcp.pdf_doc")


class PDFDocumentError(Exception):
    """Saubere Fehlermeldung für PDF-Guards / Parse-Probleme (kein Traceback)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "markdown": ""}


# --- In-Memory-LRU-Cache (prozessweit, stateless auf Disk) -------------------
class _Cache:
    """Kleiner TTL-LRU-Cache: Key (PDF-URL ohne Fragment) → (timestp, markdown, seiten_gesamt)."""

    def __init__(self, ttl: float = 3600.0, max_entries: int = 8) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: dict[str, tuple[float, str, int]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[str, int] | None:
        with self._lock:
            ent = self._data.get(key)
            if not ent:
                return None
            ts, md, n = ent
            if time.monotonic() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            # LRU: zuletzt genutzte ans Ende verschieben.
            self._data.pop(key)
            self._data[key] = ent
            return md, n

    def put(self, key: str, md: str, n: int) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._data[key] = (time.monotonic(), md, n)
            while len(self._data) > self._max:
                self._data.pop(next(iter(self._data)))


_CACHE = _Cache(ttl=3600.0, max_entries=8)


def _abs_url(cfg: Config, url: str) -> str:
    """Normalisiert die PDF-URL (Fragment entfernt, absolut, Whitelist-Host)."""
    url = (url or "").strip()
    if not url:
        raise PDFDocumentError("invalid_parameter", "pdf_url fehlt.")
    if not url.startswith("http"):
        url = cfg.base_url + (url if url.startswith("/") else "/" + url)
    parts = urlsplit(url)
    # Fragment (#search=…) ist irrelevant für den Download.
    url = parts._replace(fragment="").geturl()

    host = (cfg.base_url.split("://", 1)[-1].split("/", 1)[0]).lower()
    if parts.netloc.lower() != host:
        raise PDFDocumentError(
            "forbidden_url",
            f"Zulässig sind nur PDF-URLs des RIS-Hosts {host!r}. "
            f"Gegeben: {parts.netloc!r}.",
        )
    return url


def _parse_seiten(spec: str | list[int] | None) -> list[int] | None:
    """``"12-18"`` / ``[3,7,42]`` / ``"5"`` → sortierte 1-basierte Seitenliste."""
    if spec is None or spec == "" or spec == []:
        return None
    nums: set[int] = set()
    if isinstance(spec, str):
        for part in re.split(r"[,\s]+", spec.strip()):
            if not part:
                continue
            m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
            if not m:
                raise PDFDocumentError(
                    "invalid_parameter",
                    f"seiten: ungültiges Element {part!r}. Erlaubt: '12-18', '3,7,42', '5'.",
                )
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            if a < 1 or b < a:
                raise PDFDocumentError(
                    "invalid_parameter", f"seiten: Bereich {part!r} ungültig (mind. 1, von<=bis)."
                )
            nums.update(range(a, b + 1))
    else:
        for v in spec:
            if not isinstance(v, int) or v < 1:
                raise PDFDocumentError(
                    "invalid_parameter", f"seiten: Element {v!r} muss eine int >= 1 sein."
                )
            nums.add(v)
    return sorted(nums)


def _normalize_page_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def _extract_pages(markdown: str) -> tuple[int, int]:
    """Zählt Seiten-Marker und Zeichen (ohne den Header-Kommentar)."""
    seiten = len(re.findall(r"^## Seite \d+$", markdown, re.M))
    # Zeichen nur aus dem eigentlichen Text (nach dem ersten Marker), approx.
    return seiten, len(markdown)


def fetch_pdf_markdown(
    cfg: Config,
    client: HttpClient,
    url: str,
    seiten: str | list[int] | None = None,
    suchbegriff: str | None = None,
    max_chars: int = 200000,
) -> dict[str, Any]:
    """Lädt ein RIS-PDF und liefert den Volltext als Markdown (mit Seitenauswahl)."""
    abs_url = _abs_url(cfg, url)
    cache_key = abs_url
    max_chars = max(200, min(int(max_chars), 1000000))

    seiten_sel = _parse_seiten(seiten)
    suchbegriff_norm = (suchbegriff or "").strip().lower()

    # 1) Download (rate-limited, retry) — nur einmal pro Cache-Key.
    cached = _CACHE.get(cache_key)
    if cached is not None:
        md_full, _ = cached
        logger.info("PDF-Cache-Treffer fuer %s", abs_url)
    else:
        resp = client.request(
            "GET", abs_url, headers={"Referer": cfg.recherche_url}
        )
        if resp.status_code != 200:
            raise RISHTTPError(
                f"PDF nicht erreichbar (HTTP {resp.status_code}).",
                status=resp.status_code,
            )
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower():
            raise PDFDocumentError(
                "not_a_pdf",
                f"URL ist kein PDF (Content-Type {ctype!r}). "
                "Nur PDF-Dateien aus dem RIS werden unterstützt.",
            )
        content = resp.content
        cl = resp.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > cfg.pdf_max_bytes:
            raise PDFDocumentError(
                "too_large",
                f"PDF ({cl} Byte) überschreitet das Limit von {cfg.pdf_max_bytes} Byte.",
            )
        if len(content) > cfg.pdf_max_bytes:
            raise PDFDocumentError(
                "too_large",
                f"PDF ({len(content)} Byte) überschreitet das Limit von {cfg.pdf_max_bytes} Byte.",
            )

        md_full = _render_markdown(content, cfg, abs_url)
        _CACHE.put(cache_key, md_full, _extract_pages(md_full)[0])

    seiten_gesamt = _extract_pages(md_full)[0]

    # 2) Seitenauswahl + Suchbegriff-Filter.
    block_re = re.compile(r"^## Seite (\d+)$", re.M)
    matches = list(block_re.finditer(md_full))
    if not matches:
        raise PDFDocumentError("no_text", "Kein Text extrahierbar (Scanner-PDF?).")

    blocks: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_full)
        blocks.append((int(m.group(1)), md_full[start:end]))

    def _keep(pageno: int, block: str) -> bool:
        if seiten_sel is not None and pageno not in seiten_sel:
            return False
        if suchbegriff_norm and suchbegriff_norm not in block.lower():
            return False
        return True

    selected = [(n, b) for n, b in blocks if _keep(n, b)]

    if suchbegriff_norm and not selected:
        return {
            "pdf_url": abs_url,
            "seiten_gesamt": seiten_gesamt,
            "seiten_geiefert": [],
            "fundstellen_seiten": [],
            "kein_treffer": True,
            "zeichen_gesamt": len(md_full),
            "zeichen_geiefert": 0,
            "gekürzt": False,
            "markdown": "",
            "notiz": f"'{suchbegriff}' wurde in diesem PDF nicht gefunden.",
        }

    # 3) Markdown zusammensetzen (Header + gewählte Blöcke).
    header = f"<!-- PDF: {abs_url} · {seiten_gesamt} Seiten -->\n"
    delivered_pages = [n for n, _ in selected]
    body = "".join(b for _, b in selected)

    gekürzt = False
    if len(body) > max_chars:
        body = body[:max_chars]
        gekürzt = True
        delivered_pages = _pages_at_cutoff(selected, max_chars)

    markdown = header + body.rstrip()
    payload: dict[str, Any] = {
        "pdf_url": abs_url,
        "seiten_gesamt": seiten_gesamt,
        "seiten_geiefert": delivered_pages,
        "zeichen_gesamt": len(md_full),
        "zeichen_geiefert": len(body),
        "gekürzt": gekürzt,
        "markdown": markdown,
    }
    if suchbegriff_norm:
        payload["fundstellen_seiten"] = delivered_pages
        payload["kein_treffer"] = False
    if seiten_sel is not None:
        missing = sorted(set(seiten_sel) - set(delivered_pages))
        if missing:
            payload["seiten_fehlt"] = missing
    return payload


def _pages_at_cutoff(
    selected: list[tuple[int, str]],
    max_chars: int,
) -> list[int]:
    """Seiten, die (mindestens teilweise) im gekürzten Text vorkommen.

    Die Seite, in der der Cutoff liegt, zählt als (teilweise) geliefert.
    """
    limit = max_chars
    pages: list[int] = []
    for n, b in selected:
        if not b:
            continue
        if limit <= 0:
            break
        pages.append(n)
        limit -= len(b)
    return pages


def _render_markdown(content: bytes, cfg: Config, abs_url: str) -> str:
    """Extrahiert Text aus allen Seiten → Markdown mit ``## Seite N``-Markern."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise PDFDocumentError(
            "parse_error", f"PDF konnte nicht gelesen werden: {type(exc).__name__}"
        ) from exc

    n = len(reader.pages)
    if n == 0:
        raise PDFDocumentError("no_text", "PDF hat keine Seiten.")
    if n > cfg.pdf_max_seiten:
        raise PDFDocumentError(
            "too_many_pages",
            f"PDF hat {n} Seiten (Limit {cfg.pdf_max_seiten}). "
            "Nutze einen kleineren Abschnitt oder ein anderes Dokument.",
        )

    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001 – einzelne defekte Seite nicht abbrechen
            raw = ""
        parts.append(f"## Seite {i + 1}\n\n{_normalize_page_text(raw)}")
    return "\n\n".join(parts) + "\n"
