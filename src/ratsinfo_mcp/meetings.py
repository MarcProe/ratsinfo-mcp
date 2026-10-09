"""Sitzungs-/Vorgangs-Detailseiten + Termin-Feed (Tools ``meeting_documents``,
``find_meetings``).

Zwei read-only, sessionfreie Datenquellen (verifiziert, Stand 2026-10):

  1. **Detailseiten** (``/tops/?__=…`` = Sitzung, ``/vorgang/?__=…`` = Dossier):
     reines HTML-GET (200, kein reqid/csrftoken). ``meeting_documents`` liefert
     **nur Metadaten** (Gremium, Datum, Ort, Dokumente mit PDF-URLs, pro TOP
     gruppiert) – **keinen** Volltext. Der Volltext kommt gezielt über
     ``pdf_as_markdown``. Damit bleibt die Antwort klein und die Token-Kosten
     kontrolliert (eine Ratssitzung hat z. B. 76 PDFs; wir listen sie nur auf).

  2. **Termin-Feed** (``/termine/ics/SD.NET_RIM.ics``): serverseitig erzeugter
     iCal-Feed mit ~250 Sitzungen (Gremium, Datum, Ort) + direktem ``/tops/``-
     Link in jedem Event. ``find_meetings`` parst ihn (1 Request). Die
     sichtbare Seite ``/termine`` selbst ist JS-generiert und wird daher
     **nicht** genutzt.

Layout-Evidenz (getestete Samples):
  * ``table.table-details`` → Metadaten-Zeilen (Sitzung/Termin/Ort/Tages-
    ordnung/Niederschrift/Sitzungspaket bzw. Drucksache/Betreff/Federführung).
  * ``/tops/``: ``table.table-top`` → eine Zeile pro TOP (Klasse
    ``top-oeff-data``), Spalten: Nummer | Übersicht | Vorlagennummer |
    Dokumente. Die Dokumente-Zelle enthält die PDF-Links + Vorgang-Link.
  * PDF-Links tragen ein aria-label, das den Dokumenttyp verrät:
    „… im PDF-Format öffnen" (Hauptdok/Beschluss), „Anlage <name> zum Vorgang
    <ref> …" (Anlage), „Öffentliche Tagesordnung …" (tagesordnung),
    „Öffentliche Niederschrift …" (niederschrift), „Gesamtes Sitzungspaket …"
    (sitzungspaket).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .config import Config
from .http_client import HttpClient, RISHTTPError

logger = logging.getLogger("ratsinfo_mcp.meetings")


class MeetingsError(Exception):
    """Saubere Fehlermeldung für Detail-/Feed-Probleme (kein Traceback)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "detail_url": None, "tops": []}


def _abs_url(cfg: Config, url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise MeetingsError("invalid_parameter", "detail_url fehlt.")
    if not url.startswith("http"):
        url = cfg.base_url + (url if url.startswith("/") else "/" + url)
    host = cfg.base_url.split("://", 1)[-1].split("/", 1)[0].lower()
    if urlsplit(url).netloc.lower() != host:
        raise MeetingsError(
            "forbidden_url",
            f"Zulässig sind nur RIS-URLs des Hosts {host!r}. Gegeben: {urlsplit(url).netloc!r}.",
        )
    # Fragment entfernen.
    parts = urlsplit(url)
    return parts._replace(fragment="").geturl()


def _doc_type(aria: str) -> str:
    """Klassifiziert einen PDF-Link anhand seines aria-labels."""
    a = (aria or "").lower()
    if not a:
        return "pdf"
    if "tagesordnung" in a:
        return "tagesordnung"
    if "niederschrift" in a:
        return "niederschrift"
    if "sitzungspaket" in a:
        return "sitzungspaket"
    if a.startswith("anlage") or " anlage " in a:
        return "anlage"
    if "beschlusstext" in a:
        return "beschlusstext"
    if "drucksache" in a:
        return "drucksache"
    if "antrag" in a:
        return "antrag"
    return "pdf"


def _clean_name(aria: str) -> str:
    """Menschenlesbarer Dokumentname aus dem aria-label."""
    a = (aria or "").strip()
    a = re.sub(r"\s+im PDF-Format (öffnen|herunterladen)\s*$", "", a, flags=re.I)
    a = re.sub(r"\s*\(\s*exportiert:.*$", "", a, flags=re.I)
    return re.sub(r"\s+", " ", a).strip()


def _doc_entry(a, aria: str, base_url: str) -> dict[str, str]:
    href = a.get("href", "")
    url = _abs(base_url, href)
    return {"art": _doc_type(aria), "name": _clean_name(aria) or url.rsplit("/", 1)[-1], "url": url}


def _abs(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def _details_table(soup) -> dict[str, str]:
    """Liest ``table.table-details`` → {Label: Wert-Text} (ohne Link-URLs)."""
    out: dict[str, str] = {}
    for t in soup.find_all("table", class_="table-details"):
        for tr in t.select("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).rstrip(":")
            value = cells[1].get_text(" ", strip=True)
            if label:
                out[label] = re.sub(r"\s+", " ", value)
    return out


def _clean_gremium(raw: str | None) -> str | None:
    """Gremiumsname aus der Sitzungs-Zeile: 'Rat , 28. XI. Ratsperiode Sitzung' → 'Rat'.

    Das Gremium steht vor dem ersten ','; danach folgt Periode + Link-Text.
    """
    if not raw:
        return None
    g = re.split(r"\s*,\s*", raw, 1)[0].strip()
    return g or None


def _is_pdf_link(a) -> bool:
    h = a.get("href", "")
    return ".pdf" in h or "sdnetrim" in h


# --- /tops/ (Sitzung) --------------------------------------------------------
def _parse_tops(soup, cfg: Config) -> dict[str, Any]:
    details = _details_table(soup)

    overview: list[dict[str, str]] = []
    # Übersichts-Dokumente (Tagesordnung/Niederschrift/Sitzungspaket) liegen in
    # den Zeilen von table-details; der Link steckt im Wert-Zellen-Bruder.
    for t in soup.find_all("table", class_="table-details"):
        for tr in t.select("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).rstrip(":").lower()
            for a in cells[1].find_all("a", href=True):
                if _is_pdf_link(a):
                    overview.append(_doc_entry(a, a.get("aria-label", ""), cfg.base_url))
    # entdoppeln, Reihenfolge wahren
    seen: set[str] = set()
    overview = [d for d in overview if not (d["url"] in seen or seen.add(d["url"]))]

    # Pro-TOP-Gruppierung.
    tops: list[dict[str, Any]] = []
    table = None
    for t in soup.find_all("table"):
        if "table-top" in (t.get("class") or []):
            table = t
            break
    if table:
        for tr in table.select("tr"):
            if "top-oeff-data" not in (tr.get("class") or []):
                continue
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            nr = tds[0].get_text(" ", strip=True)
            titel = re.sub(r"\s+", " ", tds[1].get_text(" ", strip=True))
            vorlage = tds[2].get_text(" ", strip=True) or None
            doks = [_doc_entry(a, a.get("aria-label", ""), cfg.base_url)
                    for a in tds[3].find_all("a", href=True) if _is_pdf_link(a)]
            tops.append({
                "nr": nr,
                "titel": titel,
                "vorlage": vorlage,
                "dokumente": doks,
            })

    return {
        "typ": "sitzung",
        "gremium": _clean_gremium(details.get("Sitzung")),
        "termin": details.get("Termin"),
        "ort": details.get("Ort"),
        "uebersicht": overview,
        "tops": tops,
        "n_pdfs": len(overview) + sum(len(t["dokumente"]) for t in tops),
    }


# --- /vorgang/ (Dossier) -----------------------------------------------------
def _parse_vorgang(soup, cfg: Config) -> dict[str, Any]:
    details = _details_table(soup)
    # Drucksachen-Nummer steht oft gespalten: Label "Drucksache XI." Wert "926 /XI."
    vorlage = None
    for k, v in details.items():
        if k.lower().startswith("drucksache"):
            vorlage = f"{k} {v}".strip()
            break

    docs: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        if _is_pdf_link(a):
            e = _doc_entry(a, a.get("aria-label", ""), cfg.base_url)
            if e["url"] not in {d["url"] for d in docs}:
                docs.append(e)

    return {
        "typ": "vorgang",
        "vorlage": vorlage,
        "betreff": details.get("Betreff"),
        "federfuehrung": details.get("Federführung"),
        "uebersicht": docs,
        "tops": [],
        "n_pdfs": len(docs),
    }


def meeting_documents(cfg: Config, client: HttpClient, url: str) -> dict[str, Any]:
    """Lädt eine /tops/ oder /vorgang/-Seite und liefert Metadaten (kein Volltext)."""
    abs_url = _abs_url(cfg, url)
    resp = client.request("GET", abs_url)
    if resp.status_code != 200:
        raise RISHTTPError(f"Detailseite nicht erreichbar (HTTP {resp.status_code}).",
                           status=resp.status_code)
    soup = BeautifulSoup(resp.text, "html.parser")

    path = urlsplit(abs_url).path.lower()
    if "/tops/" in path:
        data = _parse_tops(soup, cfg)
    elif "/vorgang/" in path:
        data = _parse_vorgang(soup, cfg)
    else:
        raise MeetingsError(
            "not_a_detail_page",
            f"URL ist weder eine Sitzung (/tops/) noch ein Vorgang (/vorgang/): {path}",
        )

    if data["n_pdfs"] == 0:
        data["notiz"] = "Keine PDF-Dokumente auf dieser Seite gefunden."

    return {"detail_url": abs_url, **data}


# --- ICS-Feed (find_meetings) -------------------------------------------------
def _unfold_ics(text: str) -> list[str]:
    """RFC5545: Zeilen mit führendem Leerzeichen sind Fortsetzungen der Vorzeile."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith(" ") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _parse_ics_date(value: str | None) -> str | None:
    """Zieht das Datum (JJJJ-MM-TT) aus einem ICS-Datumsfeld.

    Erkennt UTC (20260101T090000Z), lokal (20260101T090000) und Werte mit
    Parameter (DTSTART;TZID=Europe/Berlin:20260101T090000). Fehlende/leere
    Werte liefern None (Event wird beim Feed-Parsing übersprungen).
    """
    m = re.search(r"(\d{8})", value or "")
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def find_meetings(
    cfg: Config,
    client: HttpClient,
    gremium: str | None = None,
    datum_von: str | None = None,
    datum_bis: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Listet Sitzungen aus dem ICS-Feed (Gremium, Datum, Ort, tops_url)."""
    limit = max(1, min(int(limit), 100))
    resp = client.request("GET", cfg.ics_feed_url)
    if resp.status_code != 200:
        raise RISHTTPError(f"Termin-Feed nicht erreichbar (HTTP {resp.status_code}).",
                           status=resp.status_code)
    lines = _unfold_ics(resp.text)

    events: list[dict[str, Any]] = []
    cur: dict[str, str] = {}
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur:
                events.append(cur)
            cur = {}
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.split(";")[0].strip().upper()
            cur[key] = val.strip()

    result: list[dict[str, Any]] = []
    for ev in events:
        summ = ev.get("SUMMARY", "")
        date = _parse_ics_date(ev.get("DTSTART", ""))
        if not date:
            continue
        if datum_von and date < datum_von:
            continue
        if datum_bis and date > datum_bis:
            continue
        if gremium:
            # Wortgrenzen-Match: "Rat" trifft "Rat", aber NICHT "Verwaltungsrat".
            if not re.search(
                r"\b" + re.escape(gremium.strip().lower()) + r"\b", summ.lower()
            ):
                continue
        tops_url = None
        m = re.search(r"https?://\S+/tops/\?__=\S+", ev.get("DESCRIPTION", ""))
        if m:
            tops_url = m.group(0)
        result.append({
            "gremium": summ,
            "datum": date,
            "ort": ev.get("LOCATION"),
            "tops_url": tops_url,
        })

    result.sort(key=lambda e: e["datum"], reverse=True)
    total = len(result)
    src = f"ICS-Feed ({cfg.host}/termine)" + (f" – {cfg.name}" if getattr(cfg, "name", "") else "")
    return {"source": src, "anzahl_gesamt": total,
            "ergebnisse": result[:limit]}
