"""ToS / Nutzungsbedingungen / Impressum-Discovery + Extraktion.

Zweck: dem MCP-Client-LLM die Möglichkeit geben, *selbst* zu analysieren,
unter welchen Bedingungen die von der Instanz bereitgestellten Dokumente
weiterverwendet / zitiert / aufbereitet werden dürfen. Dieses Tool liefert
dazu **strukturierte Rohdaten + Keyword-Flags** — die semantische Bewertung
übernimmt das aufrufende LLM (das Tool selbst ruft kein zweites LLM auf,
sondern extrahiert deterministisch).

Zwei Funktionen:

* **Discovery** – findet auf der Root-Seite (und /recherche) Links, die auf
  Impressum, Datenschutz, Nutzungsbedingungen, AGB, Barrierefreiheit etc.
  hindeuten. Das Tool liefert die URL-Liste, das LLM entscheidet, welche es
  holen will (oder nutzt ``url=None`` → alle gefundenen, begrenzt auf max 3).

* **Extraktion** – lädt die gewünschte Seite, zieht den Haupt-Text heraus
  (``<main>`` / ``<article>`` / ``<body>``), liefert ihn als sauberen
  Markdown-Text (ohne Navigation/Scripts/Styles) + eine **Keyword-Liste** mit
  Booleans, ob relevante Begriffe vorkommen. Das spart dem LLM das
  Durchlesen ganzer Seiten bei offensichtlich irrelevanten Ergebnissen.

Rate-Limit: nutzt denselben gedrosselten :class:`HttpClient`. Die Discovery
kostet 1–2 Requests, die Extraktion 1 Request pro Seite.

**Kein LLM-Call in diesem Modul** — bewusst, damit (a) der MCP-Server
self-contained bleibt, (b) kein zweites API-Abonnement nötig ist, (c) die
Analyse deterministisch nachvollziehbar bleibt (Keyword-Flags als Beleg,
dass das aufrufende LLM sich auf echte Text-Inhalte stützt).
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from .config import Config
from .http_client import HttpClient, RISHTTPError

logger = logging.getLogger("ratsinfo_mcp.tos")


class TermsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


# --- Discovery: Kandidaten-Links finden ------------------------------------
# Keyword-Triple: (Kategorie, Match-Funktion, Bezeichnung).
# Match-Funktion prüft gegen den Normalisierten-Link (href.lower() +
# text.lower()) und liefert True, wenn der Link ein ToS-Kandidat ist.
# Wortgrenzen, damit z.B. "tos" nicht in "photos" oder "notorisch" matcht.
_CATEGORIES: list[tuple[str, str, str]] = [
    ("impressum",       r"(?<![a-z])(impressum)(?![a-z])",        "Impressum"),
    ("datenschutz",     r"(?<![a-z])(datenschutz|datenschutzerk|gdpr)(?![a-z])", "Datenschutz / DSVGO"),
    ("nutzungsbed",     r"(?<![a-z])(nutzungsbedingungen|agb|allgemeine.+bedingungen)(?![a-z])", "Nutzungsbedingungen / AGB"),
    ("rechtshinweise",  r"(?<![a-z])(rechtshinweise|rechtliche.+hinweise|legal)(?![a-z])", "Rechtliche Hinweise"),
    ("barrierefreiheit", r"(?<![a-z])(barrierefrei|barrierfreie)(?![a-z])",    "Barrierefreiheit"),
    ("oeffentlichkeit", r"(?<![a-z])(oeffentlichkeits.+|open.?data|freie.nutzung)(?![a-z])", "Offenheit / OGD"),
    ("presseinfo",      r"(?<![a-z])(presse.+freiheit|vortrag.+recht)(?![a-z])", "Presse- / Vortragsrecht"),
]
_COMPILED = [(cat, re.compile(pat, re.I | re.UNICODE), label) for cat, pat, label in _CATEGORIES]

# Prozessweites Discovery-Memo: base_url -> set() aller entdeckten First-Party-
# ToS-URLs. Ein Link, den der RIS-Betreiber selbst im HTML verlinkt, gilt als
# First-Party und darf geladen werden (unabhängig von der Host-Beziehung,
# z.B. wenn das RIS auf einem Subdomain-Host liegt und das Impressum auf
# der Hauptstadt-Domain).
_DISC_LOCK = threading.Lock()
_DISC_MEMO: dict[str, set[str]] = {}


def discover_terms_links(cfg: Config, client: HttpClient) -> dict[str, Any]:
    """Scannt Root- und /recherche-Seite auf ToS-Links. 2 Requests."""
    found: dict[str, dict[str, Any]] = {}  # cat -> {label, urls: set}
    checked_paths = ["/", "/recherche"]
    errors: list[str] = []

    for path in checked_paths:
        try:
            r = client.request("GET", path)
            if r.status_code != 200:
                errors.append(f"GET {path} -> HTTP {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(" ", strip=True)
                blob = f"{href} {text}".lower()
                # absolute URL normalisieren (bzw. rel → abs)
                abs_href = _abs(cfg.base_url, href)
                for cat, rx, label in _COMPILED:
                    if rx.search(blob):
                        entry = found.setdefault(cat, {"label": label, "urls": set()})
                        entry["urls"].add(abs_href)
        except RISHTTPError as e:
            errors.append(f"GET {path} fehlgeschlagen: {e}")

    result = {
        "categories": {
            cat: {
                "label": v["label"],
                "urls": sorted(v["urls"]),
            } for cat, v in found.items()
        },
        "errors": errors,
    }
    # First-Party-Memo füllen (alle entdeckten URLs, entdoppelt).
    all_urls: set[str] = set()
    for v in found.values():
        all_urls |= v["urls"]
    with _DISC_LOCK:
        _DISC_MEMO.setdefault(cfg.base_url, set())
        _DISC_MEMO[cfg.base_url] |= all_urls
    return result


def _is_first_party(cfg: Config, url: str) -> bool:
    """Ist die URL vom Betreiber selbst verlinkt (Discovery-Memo)?"""
    abs_url = _abs(cfg.base_url, url)
    with _DISC_LOCK:
        found = _DISC_MEMO.get(cfg.base_url, set())
    return any(urlsplit(abs_url).netloc.lower() == urlsplit(f).netloc.lower() for f in found) or abs_url in found


# --- Extraktion: Text + Keyword-Flags --------------------------------------
# Relevante Begriffe für die Wiederverwendungs-Frage. Das aufrufende LLM
# sieht nur Booleans + einen kurzen Kontext-String, nicht den Volltext.
_KEYWORDS: list[tuple[str, str]] = [
    ("wiedergabe",       r"wiedergabe|wiedervorlage|vortrag\.?recht"),
    ("vervielfältigung", r"vervielfältigung|vermehrung|kopie|download"),
    ("urheberrecht",     r"urheberrecht|copyright"),
    ("kommerziell",      r"kommerziell|gewerblich|kommerz"),
    ("nicht_kommerziell", r"nicht[ -]?kommerziell|non[ -]?commercial"),
    ("freie_nutzung",    r"freie.nutzung|frei.verwend|open.?data|offene.daten|cc.by"),
    ("quellenangabe",    r"quellenangabe|nennung|citation|quelle.+angab"),
    ("keine_gewähr",     r"ohne.gewähr|keine.gewähr|haftungsfrei|haftung.+ausgeschloss"),
    ("datenschutz",      r"personenbezogen|datenschutz|dsvo|gdpr"),
    ("stadt_zugriff",    r"stadt|kommune|gemeinde|träger|betreiber"),
]
_COMPILED_KEYWORDS = [(k, re.compile(p, re.I | re.UNICODE)) for k, p in _KEYWORDS]


def _extract_main_text(soup: BeautifulSoup) -> str:
    """Haupt-Text aus der Seite (ohne Navigation/Scripts/Styles/Footer)."""
    # Bevorzugte Container (in dieser Reihenfolge)
    for sel in ["main", "article", "[role='main']", ".content", "#content", ".main"]:
        node = soup.select_one(sel)
        if node:
            break
    else:
        node = soup.body or soup

    # Junk raus
    for junk in node.find_all(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
        junk.decompose()

    # Markdown-artige Textausgabe: Headings als ##, Listen als "- ", Absätze mit Leerzeile
    parts: list[str] = []
    def walk(el, depth=0):
        if isinstance(el, str):
            txt = re.sub(r"\s+", " ", str(el)).strip()
            if txt:
                parts.append(txt)
            return
        if not isinstance(el, Tag):
            return
        name = el.name
        if name in ("h1",):
            parts.append("\n\n# " + el.get_text(" ", strip=True) + "\n")
        elif name in ("h2", "h3", "h4"):
            parts.append("\n\n## " + el.get_text(" ", strip=True) + "\n")
        elif name in ("h5", "h6"):
            parts.append("\n\n### " + el.get_text(" ", strip=True) + "\n")
        elif name in ("p", "div", "section", "article"):
            txt = el.get_text(" ", strip=True)
            if txt:
                parts.append("\n\n" + txt + "\n")
        elif name in ("ul", "ol"):
            for li in el.find_all("li", recursive=False):
                parts.append("\n- " + li.get_text(" ", strip=True))
            parts.append("\n")
        else:
            for child in el.children:
                walk(child, depth + 1)
    walk(node)
    text = "".join(parts)
    # Aufeinanderfolgende Leerzeilen kollabieren, anfangs/endende trimmen
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def extract_terms_page(
    cfg: Config,
    client: HttpClient,
    url: str,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Lädt eine ToS-Seite und liefert sauberen Text + Keyword-Flags."""
    # URL-Whitelist: nur Hosts desselben Trägers ODER First-Party (vom
    # Betreiber selbst verlinkt, z.B. RIS-Subdomain -> Stadt-Domain).
    abs_url = _abs(cfg.base_url, url)
    host = urlsplit(abs_url).netloc.lower()
    cfg_host = cfg.host
    same_org = (
        host == cfg_host
        or host.endswith("." + cfg_host)
        or cfg_host.endswith("." + host)
        or _is_same_org(cfg_host, host)
    )
    first_party = _is_first_party(cfg, url)
    if not same_org and not first_party:
        raise TermsError(
            "forbidden_url",
            f"Zulässig sind nur Seiten desselben Trägers wie {cfg_host!r} "
            f"(oder vom Betreiber verlinkte ToS-Seiten). Gegeben: {host!r}.",
        )

    r = client.request("GET", abs_url)
    if r.status_code != 200:
        raise TermsError("http_error", f"{abs_url} lieferte HTTP {r.status_code}")
    ctype = (r.headers.get("content-type") or "").lower()
    if "html" not in ctype:
        raise TermsError("not_html", f"{abs_url} ist kein HTML ({ctype!r})")

    soup = BeautifulSoup(r.text, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    text = _extract_main_text(soup)

    # Keyword-Flags: ob relevant, plus kurzer Kontext um das erste Vorkommen
    flags: dict[str, Any] = {}
    low = text.lower()
    for key, rx in _COMPILED_KEYWORDS:
        m = rx.search(low)
        if m:
            start = max(0, m.start() - 40)
            end = min(len(low), m.end() + 60)
            flags[key] = {
                "gefunden": True,
                "kontext": re.sub(r"\s+", " ", low[start:end]).strip(),
            }
        else:
            flags[key] = {"gefunden": False, "kontext": None}

    gekürzt = len(text) > max_chars
    body = text[:max_chars] if gekürzt else text

    return {
        "url": abs_url,
        "title": title,
        "text": body,
        "text_gesamt": len(text),
        "text_gezeigt": len(body),
        "gekürzt": gekürzt,
        "keyword_flags": flags,
    }


def terms_of_use(
    cfg: Config,
    client: HttpClient,
    url: str | None = None,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Tool-Backend: Discovery + (optional) Extraktion.

    * ``url=None`` → Discovery-Modus: liefert nur die gefundenen
      ToS-Kandidaten (Kategorien + URLs), keine Extraktion.
    * ``url`` gesetzt → Discovery + Extraktion dieser einen URL (oder,
      falls die URL nicht in den Discovery-Ergebnissen vorkommt, nur
      Extraktion).
    """
    disc = discover_terms_links(cfg, client)
    out: dict[str, Any] = {"discovery": disc}

    if url is None:
        out["mod"] = "discovery"
        out["hinweis"] = (
            "Gib 'url' mit einer der gefundenen URLs an, um die Seite zu "
            "extrahieren + Keyword-Flags zu erhalten. Für 'alle relevanten' "
            "auf einmal: nimm die erste URL aus impressum + datenschutz."
        )
        return out

    try:
        ext = extract_terms_page(cfg, client, url, max_chars=max_chars)
        out["mod"] = "extraction"
        out["extracted"] = ext
    except TermsError as e:
        out["mod"] = "error"
        out["error"] = e.to_dict()
    return out


def _abs(base: str, href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    if not href:
        return base
    return base + "/" + href


def _is_same_org(host_a: str, host_b: str) -> bool:
    """Zwei Hosts desselben Trägers? (z.B. ris.example.org ↔ example.org)

    Vereinfachte Heuristik: die beiden letzten Labels (z.B. 'example.org')
    müssen übereinstimmen, ODER einer muss Subdomain des anderen sein.
    """
    def last2(h):
        parts = h.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else h
    if last2(host_a) == last2(host_b):
        return True
    return host_a.endswith("." + host_b) or host_b.endswith("." + host_a)
