"""OParl-API-Client (Ergänzung: Metadaten-Anreicherung, best effort).

Quelle: ``/webservice/oparl/v1.1`` (anonymer, lesender JSON-Zugriff).
OParl liefert **keinen** PDF-Volltext – nur Metadaten (Titel, Vorlagennummer/
``reference``, Datum, ``paperType``, ``mainFile.accessUrl``).

WICHTIG zur Kostenkontrolle:
  * OParl erlaubt **keinen** Filter über ``reference``/``q`` (HTTP 400). Eine
    Vorlage lässt sich daher nur über die vollständige paginierte Liste
    auflösen.
  * Die Anreicherung ist **standardmäßig AUS** (``OPARL_ENRICH``), weil sie
    den Paper-Index lädt und die Antwort verlangsamt. Zum Aktivieren:
    ``OPARL_ENRICH=1`` (und optional ``OPARL_MAX_PAGES``).
  * Ist sie aktiv, wird der Paper-Index **lazy** und **seitenbegrenzt** geladen
    (Default: die ``OPARL_MAX_PAGES`` letzten Seiten ≈ die zuletzt
    modifizierten Vorlagen, standardmäßig ~8 Seiten ≈ 200 Papiere) und pro
    Server-Laufzeit gecacht. Die Anreicherung ist ausdrücklich *best effort*:
    Treffer, deren Vorlagennummer nicht im geladenen Fenster liegt, werden
    einfach nicht angereichert – die HTML-Recherche-Ergebnisse bleiben
    vollständig bestehen.
  * Basisdaten ``/system``, ``/body/1``, ``/body/1/organization`` sind hart gecacht.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .config import Config
from .http_client import HttpClient, RISHTTPError

logger = logging.getLogger("ratsinfo_mcp.oparl")

# Typische OParl-Vorlagennummern im RIS (generisch über SD.NET-RIM-Instanzen):
#
#  * Römische Ratsperioden: "603/IX.", "954 /XI." (ohne Suffix – häufigste
#    Form), "A 1 /XII.-GRÜNE" (mit Fraktions-Suffix), "HA 4-2025/XI.-B".
#    Der Periode-Teil matcht VIII–XIV als Römisch-Ziffern.
#  * Arabische Lfd.-/Jahresnummern: "1134/2013", "05 - 15 1134/2013"
#    (Präfix + Jahr).
#
# Die Periodisierung ist instanz-spezifisch und wird automatisch erkannt
# (siehe :mod:`ratsinfo_mcp.detect` → ``cfg.period_scheme``). Die Regex hier ist
# bewusst so breit, dass SIE BEIDEN Muster erfasst, damit die Extraktion auf
# jeder Instanz funktioniert, unabhängig davon, welches Schema erkannt wurde.
# (Die Erkennung dient der Korrektheit/Transparenz, nicht dem Filtern.)
#
# Bugfixes (Testsuite v0.3.0):
#  1) Alte Regex verlangte 2+ Zeichen nach "/XI." → "954 /XI." (ohne Suffix),
#     die häufigste Form, wurde NIE extrahiert.
#  2) Der Periode-Teil war nur "XI{1,3}" (XI–XIII) → die Ratsperioden VIII
#     (8), IX (9), X (10) wurden verfehlt. Matcht jetzt korrekt VIII–XIV.
# Struktur: optionaler Buchstaben-Präfix + Nummer + Periode (römisch) ODER
# arabische Jahres/Lfd.-Nummer + optionaler Fraktions-Suffix; Guards gegen
# Teilmuster ("4-2025/XI." aus "HA 4-2025/XI." bzw. "954 /XI." aus "954 /XI.-GRÜNE").
_PERIOD = r"(?:VI{1,3}|IX|XIV|XI{0,3}|X)"  # VIII IX XII XIII XI X XIV … (6–14)
_ARABIC = r"\d{2,4}"  # arabisches Jahr/Lfd.-Nummer (z.B. 2013)
_REFERENCE_RE = re.compile(
    # römisch: optionaler Buchstaben-Präfix + Nummer + /Periode. + optionaler Suffix
    r"(?<![A-Za-z])"
    r"([A-Z]{1,3}\s?\d{1,4}[\s\-/]*\d{0,4}\s*/" + _PERIOD + r"\.[-.]?[A-ZÄÖÜß-]{1,}(?![A-Za-z])"
    r"|\d{1,4}[\s\-/]*\d{0,4}\s*/" + _PERIOD + r"\.[-.][A-ZÄÖÜß-]{1,}(?![A-Za-z])"
    r"|\d{1,4}[\s\-/]*\d{0,4}\s*/" + _PERIOD + r"\.(?!\d))"
    # arabisch: (Präfix +) Nummer + /JJJJ  (z.B. 1134/2013, 05 - 15 1134/2013)
    r"|(?<![A-Za-z0-9/])"
    r"([A-Z0-9]{0,4}[\s\-]*\d{1,4}[\s\-]*\d{0,4}\s*/" + _ARABIC + r"(?![0-9]))"
)


def _max_pages() -> int:
    try:
        return max(1, int(os.environ.get("OPARL_MAX_PAGES", "8")))
    except ValueError:
        return 8


def _enrich_enabled() -> bool:
    # Default AUS: OParl-Anreicherung ist optional, weil sie den Paper-Index
    # (seitenbegrenzt, rate-limited) lädt und damit die Suchantwort um ~10-20 s
    # verlängern kann. Zum Aktivieren: OPARL_ENRICH=1
    return os.environ.get("OPARL_ENRICH", "0") not in ("0", "false", "no")


def extract_references(text: str) -> list[str]:
    """Extrahiert alle OParl-Vorlagennummer-Kandidaten aus einem Text."""
    if not text:
        return []
    out: list[str] = []
    for match in _REFERENCE_RE.findall(text):
        # 2 Top-Gruppen (römisch, arabisch); genau eine ist gefüllt.
        if isinstance(match, tuple):
            val = next((g for g in match if g and g.strip()), "")
        else:
            val = match or ""
        val = val.strip()
        if val and val not in out:
            out.append(val)
    return out


class OParlClient:
    def __init__(self, cfg: Config, client: HttpClient) -> None:
        self.cfg = cfg
        self.client = client
        self._cache: dict[str, Any] = {}
        self._papers_by_ref: dict[str, dict[str, Any]] | None = None
        self._paper_pages_loaded = 0

    # -- Basisdaten (hart gecacht) -------------------------------------
    def _get(self, path: str, cache: bool = False) -> dict[str, Any]:
        if cache and path in self._cache:
            return self._cache[path]
        resp = self.client.request("GET", f"{self.cfg.oparl_base}{path}")
        if resp.status_code != 200:
            raise RISHTTPError(f"OParl {path} nicht erreichbar (HTTP {resp.status_code})",
                               status=resp.status_code)
        data = resp.json()
        if cache:
            self._cache[path] = data
        return data

    def body_info(self) -> dict[str, Any]:
        return self._get("/body/1", cache=True)

    def organizations(self) -> dict[str, Any]:
        try:
            return self._get("/body/1/organization", cache=True)
        except RISHTTPError:
            # Best effort: Organisationen sind nur optionale Metadaten – bei
            # Fehlschlag lieber leer liefern als die Nutzung abzubrechen.
            return {}

    def org_name_by_id(self, org_id: int | str) -> str | None:
        if not org_id:
            return None
        try:
            resp = self.client.request(
                "GET", f"{self.cfg.oparl_base}/body/1/organization/{org_id}"
            )
            if resp.status_code == 200:
                return resp.json().get("fullName")
        except RISHTTPError:
            pass
        return None

    # -- Paper-Index (lazy, seitenbegrenzt, gecacht) -------------------
    def _ensure_papers(self) -> dict[str, dict[str, Any]]:
        if self._papers_by_ref is None:
            self._papers_by_ref = {}
        if self._paper_pages_loaded == 0:
            self._fill_index()
        return self._papers_by_ref

    def _fill_index(self) -> None:
        by_ref: dict[str, dict[str, Any]] = {}
        page = 1
        cap = _max_pages()
        total_pages = cap
        while page <= cap and page <= total_pages:
            resp = self.client.request("GET", f"{self.cfg.oparl_base}/body/1/paper",
                                       params={"page": page})
            if resp.status_code != 200:
                break
            data = resp.json()
            for p in data.get("data", []):
                ref = (p.get("reference") or "").strip()
                if ref:
                    by_ref[ref] = p
            total_pages = data.get("pagination", {}).get("totalPages", page)
            page += 1
            self._paper_pages_loaded = page - 1
        self._papers_by_ref = by_ref

    def paper_by_reference(self, reference: str) -> dict[str, Any] | None:
        if not reference:
            return None
        by_ref = self._ensure_papers()
        if reference in by_ref:
            return by_ref[reference]
        norm = reference.replace(".", "").replace(" ", "").upper()
        for k, v in by_ref.items():
            if k.replace(".", "").replace(" ", "").upper() == norm:
                return v
        return None

    def enrich(self, reference: str) -> dict[str, Any] | None:
        paper = self.paper_by_reference(reference)
        if not paper:
            return None
        mf = paper.get("mainFile") or {}
        return {
            "name": paper.get("name"),
            "reference": paper.get("reference"),
            "date": paper.get("date"),
            "paper_type": paper.get("paperType"),
            "main_file_url": mf.get("accessUrl") or mf.get("accessUrldownloadUrl"),
            "main_file_name": mf.get("name"),
            "oparl_id": paper.get("id"),
        }

    def enrich_hit(self, vorlage_kennung: str | None, titel: str | None) -> dict[str, Any] | None:
        """Best effort: Vorlagennummer aus der Treffer-Kennung ableiten & auflösen."""
        if not _enrich_enabled():
            return None
        candidates = extract_references(vorlage_kennung or "") + extract_references(titel or "")
        for cand in candidates:
            meta = self.enrich(cand)
            if meta:
                return meta
        return None
