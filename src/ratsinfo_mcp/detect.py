"""Fail-early-Kompatibilitäts-Check + Instanz-Profil für SD.NET RIM.

Zwei Aufgaben:

1. **Fail-early (Punkt 1):** Bevor der MCP-Server bedient, prüft
   :func:`ensure_compatible`, dass die konfigurierte ``RIS_BASE_URL`` eine
   kompatible **SD.NET RIM** (Sternberg)-Instanz ist. Ist sie es nicht (anderes
   Produkt, andere DOM-Klassen, kaputter Endpunkt), bricht der Server *vor*
   dem ersten Tool-Call ab mit einem klaren, strukturierten Fehler
   (``CompatibilityError``) statt später mysteriös „keine Ergebnisse" zu
   liefern. Das war historisch die schmerzhafteste Fehlerquelle (Session/
   Formular-Probleme, die sich wie „keine Treffer" verkleiden).

2. **Instanz-Profil (Punkte 2/3 + RIS_NAME):** Erkennt automatisch, ohne
   manuelle Konfiguration:

   * **Name** des Trägers (``RIS_NAME``) — aus ``OParl /body/1 → name``,
     Fallback ``<title>``.
   * **Periodisierung** der Vorlagen (römisch wie ``603/IX.`` vs. arabisch wie
     ``1134/2013``) — steuert die OParl-Referenz-Regex (Punkt 3).
   * **count-Verhalten** der Recherche (nur ``count=50``?) — verifiziert die
     dokumentierte RIM-Eigenheit (Punkt 2).

Alle Erkennungen sind **best effort** außer der RIM-Signatur: Ein
Nicht-Erkennen eines Profelfelds warnt, aber bricht (im Strict-Modus) nicht ab
— nur eine fehlende *RIM-Signatur* ist fatal.

Rate-Limit: Die Prüfung nutzt denselben gedrosselten :class:`HttpClient`.
Eine vollständige Prüfung kostet ca. 5–8 Requests (≈ 8–12 s bei 1 req/s).
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from bs4 import BeautifulSoup

from .config import Config
from .http_client import HttpClient, RISHTTPError

logger = logging.getLogger("ratsinfo_mcp.detect")


class CompatibilityError(Exception):
    """Fail-early: die Instanz ist keine kompatible SD.NET RIM.

    Trägt einen maschinenlesbaren ``code``, eine menschenlesbare ``message``
    und einen ``report`` (strukturierte Prüfungsergebnisse) für sauberes Logging.
    """

    def __init__(self, code: str, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.report = report or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "compatibility_error",
            "code": self.code,
            "message": str(self),
            "report": self.report,
        }


# --- Prozessweites Memo: einmal pro (base_url, strict) prüfen --------------
_MEMO_LOCK = threading.Lock()
_MEMO: dict[tuple[str, bool], dict[str, Any]] = {}


# --- RIM-Signatur -----------------------------------------------------------
# Zeichenketten, die (alle zusammen) eine SD.NET RIM-Instanz verlässlich
# identifizieren. Auf mehreren getesteten Instanzen im Root-HTML und via OParl
# /system gefunden. Bewusst mehrere unabhängige Marker, damit ein einzelner
# Umbenennungs-Fall nicht falsch-negativ wird.
_RIM_HTML_MARKERS = ("sd.net rim", "sdnetrim", "sternberg")
_OPARL_SYSTEM_NAME = "sd.net rim"


def _host_markers_ok(root_html: str) -> tuple[bool, list[str]]:
    low = root_html.lower()
    hits = [m for m in _RIM_HTML_MARKERS if m in low]
    return (len(hits) >= 1), hits


# --- Signatur-Check (fail-early) -------------------------------------------
def _check_signature(cfg: Config, client: HttpClient) -> dict[str, Any]:
    """Prüft, ob die Instanz eine SD.NET RIM ist. 4 kurze Requests."""
    report: dict[str, Any] = {
        "base_url": cfg.base_url,
        "rim_signature": False,
        "html_markers": [],
        "oparl_system_name": None,
        "recherche_form": False,
        "ics_feed": False,
        "oparl": False,
        "errors": [],
    }

    # 1) Root-HTML: RIM-Marker + (als Nebenprodukt) <title> für den Namen.
    root_text = ""
    try:
        r = client.request("GET", "/")
        if r.status_code != 200:
            report["errors"].append(f"GET / lieferte HTTP {r.status_code}")
        else:
            root_text = r.text
            ok, hits = _host_markers_ok(root_text)
            report["html_markers"] = hits
            if ok:
                report["rim_signature"] = True
    except RISHTTPError as e:
        report["errors"].append(f"GET / fehlgeschlagen: {e}")

    # 2) OParl /system: Name sollte 'SD.NET RIM' sein (unabhängiger 2. Beleg).
    try:
        r = client.request("GET", f"{cfg.oparl_base}/system")
        if r.status_code == 200:
            name = (r.json() or {}).get("name")
            report["oparl_system_name"] = name
            report["oparl"] = True
            if (name or "").lower() == _OPARL_SYSTEM_NAME:
                report["rim_signature"] = True
        else:
            report["errors"].append(f"OParl /system lieferte HTTP {r.status_code}")
    except RISHTTPError as e:
        report["errors"].append(f"OParl /system fehlgeschlagen: {e}")
    except ValueError as e:
        report["errors"].append(f"OParl /system kein JSON: {e}")

    # 3) /recherche: das Formular muss existieren + Tokens auslesbar sein.
    try:
        r = client.request("GET", "/recherche")
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            form = soup.find("form", id="rechercheForm")
            has_reqid = bool(form and form.find("input", {"name": "reqid"}))
            has_csrf = bool(form and form.find("input", {"name": "csrftoken"}))
            if form and has_reqid and has_csrf:
                report["recherche_form"] = True
                if report["rim_signature"]:
                    pass  # Signatur bereits bestätigt
                elif (root_text.lower().count("sd.net") >= 0 and "sdnet" in root_text.lower()):
                    # Formular + SD.NET-Text im Root → als RIM akzeptieren.
                    report["rim_signature"] = True
            else:
                report["errors"].append(
                    "/recherche: rechercheForm ohne reqid/csrftoken gefunden – "
                    "Formularstruktur weicht von SD.NET RIM ab."
                )
        else:
            report["errors"].append(f"GET /recherche lieferte HTTP {r.status_code}")
    except RISHTTPError as e:
        report["errors"].append(f"GET /recherche fehlgeschlagen: {e}")

    # 4) ICS-Feed: sollte ein text/calendar liefern (sessionfrei).
    try:
        r = client.request("GET", cfg.ics_feed_url)
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and "calendar" in ctype:
            report["ics_feed"] = True
        else:
            report["errors"].append(
                f"ICS-Feed {cfg.ics_feed_url} lieferte HTTP {r.status_code} "
                f"({ctype}) statt text/calendar."
            )
    except RISHTTPError as e:
        report["errors"].append(f"ICS-Feed fehlgeschlagen: {e}")

    return report


# --- Instanz-Profil (best effort) ------------------------------------------
def detect_name(cfg: Config, client: HttpClient) -> str:
    """Träger-Name (RIS_NAME): OParl /body/1 → name, Fallback <title>."""
    try:
        r = client.request("GET", f"{cfg.oparl_base}/body/1")
        if r.status_code == 200:
            name = (r.json() or {}).get("name")
            if name:
                return str(name).strip()
    except (RISHTTPError, ValueError):
        pass
    # Fallback: <title>, mit Produkt-/Stadt-Präfixen bereinigt.
    try:
        r = client.request("GET", "/")
        soup = BeautifulSoup(r.text, "html.parser")
        t = soup.find("title")
        if t:
            title = t.get_text(" ", strip=True)
            title = re.sub(r"[-|].*SD\.?NET.*$", "", title, flags=re.I)
            title = re.sub(r"^.*?[-|]\s*", "", title)  # "Ratsportal der Stadt X - …"
            return title.strip()
    except RISHTTPError:
        pass
    return ""


def detect_period_scheme(cfg: Config, client: HttpClient, pages: int = 1) -> str:
    """Vorlagen-Periodisierung erkennen: 'roman' | 'arabic' | 'unknown'.

    Verschiedene SD.NET-RIM-Instanzen nutzen unterschiedliche Vorlagen-
    Nummern: entweder römische Ratsperioden (z.B. ``603/IX.``) oder
    arabische Lfd.-/Jahresnummern (z.B. ``1134/2013``). Die OParl-Referenz-
    Regex ist für beide Schemata breit, die Erkennung dient Transparenz und
    Logging – sie zeigt, welches Schema die Instanz verwendet.
    """
    roman_re = re.compile(r"/(?:VI{1,3}|IX|XIV|XI{0,3}|X|II|IV|V|VIII|III)\.")
    arabic_re = re.compile(r"/\d{2,4}$")
    roman = arabic = 0
    total = 0
    try:
        for page in range(1, max(1, pages) + 1):
            r = client.request("GET", f"{cfg.oparl_base}/body/1/paper", params={"page": page})
            if r.status_code != 200:
                break
            for p in (r.json() or {}).get("data", []):
                ref = (p.get("reference") or "").strip()
                if not ref:
                    continue
                total += 1
                if roman_re.search(ref):
                    roman += 1
                elif arabic_re.search(ref):
                    arabic += 1
    except (RISHTTPError, ValueError) as e:
        logger.warning("Perioden-Erkennung: OParl-Index nicht lesbar (%s)", e)

    if total == 0:
        return "unknown"
    if roman >= max(1, total // 4) and roman >= arabic:
        return "roman"
    if arabic >= max(1, total // 4):
        return "arabic"
    return "unknown"


def verify_count(cfg: Config, client: HttpClient, term: str = "Antrag") -> dict[str, Any]:
    """Verifiziert die count-Eigenheit der Recherche (Punkt 2).

    Auf den Testinstanzen liefert der Server *nur* ``count=50`` Treffer; jede
    andere Seitehöhe → „keine Ergebnisse gefunden". Das Projekt nutzt count=50
    bereits hartkodiert. Diese Prüfung bestätigt das live und meldet eine
    Abweichung als Warnung (nicht fatal, da instanzinterne RIM-Eigenheit).
    """
    def run_count(count: str) -> int:
        soup = BeautifulSoup(
            client.request("GET", "/recherche").text, "html.parser"
        )
        form = soup.find("form", id="rechercheForm")
        if not form:
            return -1
        fv = lambda n: (form.find("input", {"name": n}) or {}).get("value")
        data = {
            "reqid": fv("reqid"), "terms": term, "typ": "-1", "idgremium": "-1",
            "legislaturperiode": "-1", "datefrom": "", "dateto": "",
            "doktyp[]": ["T", "E", "B", "N"], "count": count, "csrftoken": fv("csrftoken"),
        }
        post = client.request(
            "POST", "/recherche", data=data, follow_redirects=False,
            headers={"Referer": cfg.recherche_url},
        )
        if post.status_code not in (301, 302, 303):
            return -1
        loc = post.headers.get("Location", "")
        res = client.request(
            "GET", loc if loc.startswith("http") else cfg.base_url + loc,
            headers={"Referer": cfg.recherche_url},
        )
        soup2 = BeautifulSoup(res.text, "html.parser")
        return sum(
            len(soup2.find("table", id=f"table{i}").select("tr[class^='row-']"))
            for i in range(4) if soup2.find("table", id=f"table{i}")
        )

    c50 = run_count("50")
    c10 = run_count("10")
    result = {"count_50_rows": c50, "count_10_rows": c10}
    if c50 > 0 and c10 == 0:
        result["verdict"] = "standard (nur count=50 liefert Treffer) – wie erwartet"
        result["ok"] = True
    elif c50 > 0 and c10 > 0:
        result["verdict"] = "ABWEICHUNG: andere count-Werte liefern auch Treffer"
        result["ok"] = False
    else:
        result["verdict"] = "UNBESTIMMT: kein Treffer für Testbegriff (Begriff anpassen?)"
        result["ok"] = False
    return result


def build_profile(cfg: Config, client: HttpClient, *, check_count: bool = False) -> dict[str, Any]:
    """Komplettes Instanz-Profil (Name + Periodisierung + optional count)."""
    profile: dict[str, Any] = {
        "name": detect_name(cfg, client),
        "period_scheme": detect_period_scheme(cfg, client),
    }
    if check_count:
        profile["count"] = verify_count(cfg, client)
    return profile


def _detect_enabled() -> bool:
    """``RIS_DETECT``: „1“ (Default) = Prüfung läuft; „0“ = überspringen.

    Überspringen ist nützlich (a) in der Offline-Testsuite, (b) wenn man die
    Start-Prüfung bewusst abstellen will (z.B. hinter einem eigenen Reverse-
    Proxy, oder wenn das Ziel eine RIM-Variante ist, die den Check nicht
    besteht, aber trotzdem funktionieren soll). In Strict-Modus mit
    RIS_DETECT=0 wird KEIN fail-early-Abbruch gemacht (das wäre der Punkt,
    den man überspringt).
    """
    import os
    return os.environ.get("RIS_DETECT", "1") not in ("0", "false", "no")


# --- Fail-early-Entry --------------------------------------------------------
def ensure_compatible(
    cfg: Config,
    *,
    profile: bool = True,
    verify_count: bool = False,
) -> dict[str, Any]:
    """Prüft Kompatibilität und (best effort) Profil. Memoisiert pro base_url.

    * Strict + nicht RIM  → :class:`CompatibilityError` (fail-early).
    * Nicht-strict + nicht RIM → Warnung, liefert Report mit ``rim_signature=False``.
    * ``RIS_DETECT=0`` → Prüfung komplett überspringen (keine Requests, kein
      Abbruch) – für Offline-Tests / bewusste Deaktivierung.

    Das Ergebnis (inkl. erkanntem ``name`` und ``period_scheme``) wird auf
    ``cfg.name``/``cfg.period_scheme`` zurückgeschrieben und pro base_url
    prozessweit gemerkt (eine Prüfung ≈ 5–8 Requests).
    """
    key = (cfg.base_url, cfg.strict)
    with _MEMO_LOCK:
        if key in _MEMO:
            cached = _MEMO[key]
            _apply(cfg, cached)
            return cached

    if not _detect_enabled():
        # Kein Fail-early, kein Profil – leeres Report, Server läuft trotzdem.
        skip: dict[str, Any] = {
            "base_url": cfg.base_url, "rim_signature": None,  # None = nicht geprüft
            "skipped": True, "html_markers": [], "oparl_system_name": None,
            "recherche_form": None, "ics_feed": None, "oparl": None, "errors": [],
            "name": getattr(cfg, "name", ""), "period_scheme": "unknown",
        }
        _MEMO[key] = skip
        _apply(cfg, skip)
        logger.info("RIS_DETECT=0 – Kompatibilitätsprüfung übersprungen für %s", cfg.base_url)
        return skip

    with HttpClient(cfg) as client:
        report = _check_signature(cfg, client)
        if report["rim_signature"]:
            if profile:
                prof = build_profile(cfg, client, check_count=verify_count)
                report["name"] = prof["name"]
                report["period_scheme"] = prof["period_scheme"]
                report["count"] = prof.get("count")
            _MEMO[key] = report
            _apply(cfg, report)
            logger.info(
                "SD.NET RIM bestätigt: %s (name=%r, periode=%s)",
                cfg.base_url, report.get("name"), report.get("period_scheme"),
            )
            return report

        report["rim_signature"] = False
        _MEMO[key] = report
        if cfg.strict:
            _apply(cfg, report)
            raise CompatibilityError(
                "not_sdnet_rim",
                f"{cfg.base_url} ist keine erkannte SD.NET RIM-Instanz. "
                f"HTML-Marker={report['html_markers']}, "
                f"OParl /system={report['oparl_system_name']!r}, "
                f"rechercheForm={report['recherche_form']}, "
                f"Impressum-Endpunkte: {report['errors']}. "
                "Mit RIS_STRICT=0 trotzdem starten (auf Eigene Verantwortung).",
                report=report,
            )
        logger.warning(
            "Instanz %s NICHT als SD.NET RIM erkannt (RIS_STRICT=0, Warnung): %s",
            cfg.base_url, report["errors"],
        )
        _apply(cfg, report)
        return report


def _apply(cfg: Config, report: dict[str, Any]) -> None:
    """Schreibt erkannte Werte auf die Config (best effort)."""
    cfg.name = report.get("name") or cfg.name  # Env-Override gewinnt
    cfg.period_scheme = report.get("period_scheme", "unknown")  # type: ignore[attr-defined]
    cfg.rim_signature = report.get("rim_signature", False)      # type: ignore[attr-defined]
