"""Client für die HTML-Recherche eines SD.NET RIM (``/recherche``).

Primäre Datenquelle: einzige Möglichkeit, nach **Volltext im PDF-Inhalt** zu
suchen. Fundstellen werden als ``<span class="search_match">`` markiert; der
PDF-Link trägt den Treffer als Fragment ``#search=<Begriff>``.

Mechanik (verifiziert, Stand 2026-10):
  1. ``GET /recherche`` (mit persistentem Cookie-Jar) → ``reqid`` + ``csrftoken``
     (hidden) + Gremium-Select ``idgremium`` auslesen.
  2. ``POST /recherche`` (``application/x-www-form-urlencoded``) mit exakt den
     Formularfeldern, **ohne** die Honeypots ``keywords``/``searchall`` und
     ``follow_redirects=False`` → ``302`` mit ``Location: /recherche/?__=<token>``
     (PRG-Muster).
  3. ``GET`` auf die ``Location``-URL → Ergebnis-HTML.

Root-Cause des „keine Ergebnisse"-Problems (früherer Researcher): Schritt 1 und
2 müssen im **gleichen Session-Kontext** laufen (gleicher ``PHPSESSID``-Cookie).
Wurden die Tokens mit einem anderen Client/Cookie-Jar geholt als der POST,
antwortet der Server „Das Formular ist nicht mehr gültig."

Server-Spezifika (verifiziert, wichtig für die Nutzung):
  * Das Feld ``count`` akzeptiert den Server **nur mit Wert 50** (JS-Default).
    Jeder andere Wert liefert „keine Ergebnisse gefunden", selbst für existierende
    Begriffe. Deshalb wird die Server-Seitenhöhe immer 50 gesendet und ``limit``
    des Tools als *Client-Filter* auf die zurückgegebenen Treffer angewendet.
  * Pro Suchlauf liefert der Server je Tabelle (Dokumente/Anlagen/Personen/News)
    maximal 25 Zeilen der ersten Seite.
  * Paginierung steht **vor** jeder Tabelle als „Seite 1 von Y" (Y = Seitenzahl).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from .config import Config
from .http_client import HttpClient, RISHTTPError

logger = logging.getLogger("ratsinfo_mcp.recherche")

# Mapping der MCP-Tool-Enums auf die Werte des Formulars.
TYP_VALUES = {"alle": "-1", "dokumente": "0", "anlagen": "1", "personen": "2", "anfragen": "3"}
DOKTYP_VALUES = {"vorlagen": "T", "einladungen": "E", "beschluesse": "B", "niederschriften": "N"}
ALL_DOKTYP = ["T", "E", "B", "N"]

_FORM_INVALID = "nicht mehr gültig"
_NO_RESULTS = "keine Ergebnisse gefunden"

# Server akzeptiert nur count=50 (siehe Modul-Doku).
_SERVER_COUNT = "50"


@dataclass
class SearchParams:
    terms: str = ""
    typ: str = "-1"           # -1/0/1/2/3
    gremium: str = "-1"       # -1 = alle, sonst ID aus dem Select
    gremium_name: str = ""
    datefrom: str = ""        # JJJJ-MM-TT oder ""
    dateto: str = ""
    doktyp: list[str] = field(default_factory=lambda: list(ALL_DOKTYP))
    exakt: bool = False
    count: int = 50           # Client-Cap, wird NICHT an den Server geschickt


@dataclass
class RechercheHit:
    typ: str
    titel: str
    vorlage_kennung: str | None = None   # Vorlagenname inkl. Nummer (aus PDF-Link)
    gremium: str | None = None
    datum: str | None = None
    sitzungstermin: str | None = None
    fundstelle: str | None = None
    pdf_url: str | None = None
    detail_url: str | None = None
    anhaenge: list[dict[str, str]] = field(default_factory=list)
    personenkreis: str | None = None  # nur bei typ == "Personen"

    def to_dict(self) -> dict[str, Any]:
        return {
            "typ": self.typ,
            "titel": self.titel,
            "vorlage_kennung": self.vorlage_kennung,
            "gremium": self.gremium,
            "datum": self.datum,
            "sitzungstermin": self.sitzungstermin,
            "fundstelle": self.fundstelle,
            "pdf_url": self.pdf_url,
            "detail_url": self.detail_url,
            "anhaenge": self.anhaenge,
            "personenkreis": self.personenkreis,
        }


@dataclass
class RechercheResult:
    query: str
    total_seiten_pro_typ: dict[str, int | None]
    total_ergebnisse: int | None
    current_page: int
    hits: list[RechercheHit]
    keine_ergebnisse: bool = False
    source: str = "HTML-Recherche (RIS-Instanz /recherche, inkl. PDF-Volltext)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query": self.query,
            "total_seiten_pro_typ": self.total_seiten_pro_typ,
            "total_ergebnisse": self.total_ergebnisse,
            "current_page": self.current_page,
            "keine_ergebnisse": self.keine_ergebnisse,
            "treffer": [h.to_dict() for h in self.hits],
        }


class GremiumMapping:
    """ID → Name des Gremium-Selects (aus dem Formular, gecacht pro Server-Lauf)."""

    def __init__(self) -> None:
        self._by_id: dict[str, str] = {}
        self._by_name: dict[str, str] = {}

    def load(self, id_to_name: dict[str, str]) -> None:
        self._by_id = id_to_name
        self._by_name = {_norm(n): i for i, n in id_to_name.items()}

    def name_to_id(self, name: str) -> str | None:
        return self._by_name.get(_norm(name))

    def id_to_name(self, gid: str) -> str | None:
        return self._by_id.get(gid)

    def all_names(self) -> list[str]:
        return list(self._by_id.values())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class RechercheClient:
    def __init__(self, cfg: Config, client: HttpClient) -> None:
        self.cfg = cfg
        self.client = client
        self.gremien = GremiumMapping()
        self._gremien_loaded = False

    # -- Formular / Tokens ---------------------------------------------
    def _seed_form(self) -> tuple[str, str]:
        resp = self.client.request("GET", "/recherche")
        if resp.status_code != 200:
            raise RISHTTPError(
                f"Recherche-Seite nicht erreichbar (HTTP {resp.status_code})",
                status=resp.status_code,
            )
        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", id="rechercheForm")
        reqid = _form_value(form, "reqid")
        csrftoken = _form_value(form, "csrftoken")
        if not reqid or not csrftoken:
            raise RISHTTPError(
                "reqid/csrftoken konnten nicht aus dem Recherche-Formular gelesen "
                "werden. Die Formularstruktur hat sich vermutlich geändert."
            )
        if not self._gremien_loaded:
            sel = form.find("select", {"name": "idgremium"})
            if sel:
                mapping: dict[str, str] = {}
                for opt in sel.find_all("option"):
                    v = (opt.get("value") or "").strip()
                    n = opt.get_text(" ", strip=True)
                    if v and v != "-1" and n:
                        mapping[v] = n
                self.gremien.load(mapping)
            self._gremien_loaded = True
        return reqid, csrftoken

    def ensure_gremien(self) -> None:
        """Gremium-Mapping sicherstellen (ohne Suchlauf, falls noch leer)."""
        if not self._gremien_loaded:
            self._seed_form()

    # -- Suche ----------------------------------------------------------
    def search(self, p: SearchParams) -> RechercheResult:
        reqid, csrftoken = self._seed_form()

        data: dict[str, Any] = {
            "reqid": reqid,
            "terms": p.terms,
            "typ": p.typ,
            "idgremium": p.gremium,
            "legislaturperiode": "-1",
            "datefrom": p.datefrom,
            "dateto": p.dateto,
            "doktyp[]": list(p.doktyp),
            "count": _SERVER_COUNT,   # Server akzeptiert nur 50
            "csrftoken": csrftoken,
        }
        if p.exakt:
            data["exakt"] = "1"

        post = self.client.request(
            "POST", "/recherche", data=data, follow_redirects=False,
            headers={"Referer": self.cfg.recherche_url},
        )
        if post.status_code not in (301, 302, 303):
            raise RISHTTPError(
                f"POST /recherche lieferte keinen 302-Redirect (HTTP {post.status_code})."
                f"{_first_error(post.text)}",
                status=post.status_code,
            )
        loc = post.headers.get("Location", "")
        if not loc:
            raise RISHTTPError("302-Redirect ohne Location-Header erhalten.")
        result_url = loc if loc.startswith("http") else self.cfg.base_url + loc

        resp = self.client.request(
            "GET", result_url, headers={"Referer": self.cfg.recherche_url}
        )
        if resp.status_code != 200:
            raise RISHTTPError(
                f"Ergebnisseite nicht erreichbar (HTTP {resp.status_code})",
                status=resp.status_code,
            )
        return self._parse(resp.text, p)

    # -- Parsing --------------------------------------------------------
    def _parse(self, html: str, p: SearchParams) -> RechercheResult:
        if _FORM_INVALID in html:
            raise RISHTTPError(
                "Das Formular ist nicht mehr gültig. - Token/Session ungültig. "
                "Bitte erneut versuchen (Tokens werden pro Suche neu geholt)."
            )
        soup = BeautifulSoup(html, "html.parser")
        pages = self._table_page_counts(html)
        typ_total = {
            "Dokumente": pages.get("table0"),
            "Anlagen": pages.get("table1"),
            "Personen": pages.get("table2"),
            "Anfragen und News": pages.get("table3"),
        }
        table_to_typ = {
            "table0": "Dokumente",
            "table1": "Anlagen",
            "table2": "Personen",
            "table3": "Anfragen und News",
        }
        hits: list[RechercheHit] = []
        for tid, label in table_to_typ.items():
            table = soup.find("table", id=tid)
            if not table:
                continue
            for tr in table.select("tr[class^='row-']"):
                hit = self._parse_row(tr, label)
                if hit is not None:
                    hits.append(hit)

        # Client-Filters: typ (falls nicht alle) + count-Cap.
        typ_filter = {
            "0": {"Dokumente"}, "1": {"Anlagen"}, "2": {"Personen"},
            "3": {"Anfragen und News"},
        }.get(p.typ)
        if typ_filter is not None:
            hits = [h for h in hits if h.typ in typ_filter]
        hits = hits[: max(1, min(p.count, 100))]

        keine = _NO_RESULTS in html and not hits
        return RechercheResult(
            query=p.terms,
            total_seiten_pro_typ=typ_total,
            total_ergebnisse=None,  # exakte Gesamtzahl nicht isoliert verfügbar
            current_page=1,
            hits=hits,
            keine_ergebnisse=keine,
            source=(
                f"HTML-Recherche ({self.cfg.host}/recherche, inkl. PDF-Volltext)"
                + (f" – {self.cfg.name}" if getattr(self.cfg, "name", "") else "")
            ),
        )

    def _table_page_counts(self, html: str) -> dict[str, int | None]:
        """Liest je Tabelle die Paginierung „Seite 1 von Y" (Y=Seitenzahl).

        Die Paginierungs-Blöcke stehen im DOM **vor** ihrer Tabelle. Bei
        einteitigen Ergebnissen (1 Seite) emittiert der Server keinen Pager → None.
        """
        result: dict[str, int | None] = {}
        vons = [(m.start(), int(m.group(1))) for m in re.finditer(r"Seite 1 von (\d+)", html)]
        if not vons:
            return {"table0": None, "table1": None, "table2": None, "table3": None}
        for tid in ("table0", "table1", "table2", "table3"):
            pos = html.find(f'id="{tid}"')
            if pos == -1:
                continue
            before = [y for p2, y in vons if p2 < pos]
            result[tid] = before[-1] if before else None
        return result

    def _parse_row(self, tr, typ: str) -> RechercheHit | None:
        links = tr.find_all("a", href=True)

        pdf_url = None
        vorlage_kennung = None
        anhaenge: list[dict[str, str]] = []
        detail_url = None
        sitzung = None
        gremium = None
        seen_main = False

        for a in links:
            href = a["href"]
            aria = a.get("aria-label", "")
            abs_href = _abs(self.cfg.base_url, href)

            # PDF-Links (sdnetrim … .pdf): erster = Hauptdokument, Rest = Anlagen.
            if ".pdf" in href or "sdnetrim" in href:
                if not seen_main:
                    seen_main = True
                    pdf_url = abs_href
                    # Vorlagename aus aria: "<name> im PDF-Format öffnen"
                    name = aria.replace("im PDF-Format öffnen", "").strip()
                    if name:
                        vorlage_kennung = name
                else:
                    # Anlage (hat oft kein aria-label) – Name aus dem hide-text/aria.
                    an_name = _attachment_name(a, aria)
                    if an_name or abs_href not in [x["url"] for x in anhaenge]:
                        anhaenge.append({"name": an_name or abs_href.rsplit("/", 1)[-1], "url": abs_href})
                continue

            # Vorgang-Link: sauberster Name inkl. Vorlagennummer.
            if "/vorgang/" in href:
                if detail_url is None:
                    detail_url = abs_href
                # "<Name> - Vorgang. Vorgang öffnen" → Vorlagename + Nummer
                vm = re.search(r"^(.*)\s*-\s*Vorgang\.\s*Vorgang öffnen\s*$", aria)
                if vm:
                    vorlage_kennung = vm.group(1).strip() or vorlage_kennung
                continue

            # Sitzungs-/TOP-Link: aria "Sitzung am <tag> des Gremiums <G> anzeigen"
            if "Sitzung am" in aria:
                sm = re.search(
                    r"Sitzung am\s+(.+?)\s+des Gremiums\s+(.+?)\s+anzeigen\s*$", aria
                )
                if sm:
                    sitzung = sm.group(1).strip()
                    gremium = sm.group(2).strip()
                continue

            # Personen-Link (Personen-Tabelle): /personen/?__=… – Personenseite
            # als detail_url (Verlinkung für find_person/person_steckbrief).
            if "/personen/" in href and detail_url is None:
                detail_url = abs_href
                continue

        subject = tr.select_one("span.search_result_subject")
        titel = subject.get_text(" ", strip=True) if subject else ""
        if not titel and typ == "Personen":
            # Personen-Tabelle: Name steht in der ersten Spalte (Name-Link),
            # nicht im Suchbegriff-Subject.
            name_cell = tr.select_one("td[class*='gesamteBezeichnung'] a")
            if name_cell:
                titel = name_cell.get_text(" ", strip=True)
        if not titel:
            titel = vorlage_kennung or ""

        matches = tr.select("span.search_match")
        fundstelle = self._build_fundstelle(tr, matches) if matches else None

        datum = self._extract_datum(tr)

        # Personen-Tabelle (table2): zweite Spalte = Personenkreis (z.B. Fraktion,
        # "Sachkundiger Bürger", „Mitglieder (AC)“). Wir extrahieren bewusst NUR
        # diesen strukturierten Kreis – die Adresse (dritte Spalte) bleibt auf der
        # Personenseite (detail_url) und wird hier NICHT mitgeliefert (Datenminimierung).
        personenkreis: str | None = None
        if typ == "Personen":
            pk_cell = tr.find("td", class_=re.compile("personenkreis", re.I))
            if pk_cell:
                pk_text = pk_cell.get_text(" ", strip=True)
                if pk_text and pk_text.lower() != "keine angabe":
                    personenkreis = pk_text

        if not (titel or pdf_url or detail_url):
            return None

        return RechercheHit(
            typ=typ,
            titel=titel,
            vorlage_kennung=vorlage_kennung,
            gremium=gremium,
            datum=datum,
            sitzungstermin=sitzung,
            fundstelle=fundstelle,
            pdf_url=pdf_url,
            detail_url=detail_url,
            anhaenge=anhaenge,
            personenkreis=personenkreis,
        )

    def _build_fundstelle(self, tr, matches) -> str | None:
        """Textausschnitt um den ersten Treffer, Begriff als ``<mark>`` markiert."""
        first = matches[0]
        term = first.get_text(strip=True)
        if not term:
            return None
        raw = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
        idx = raw.find(term)
        if idx == -1:
            return None
        start = max(0, idx - 90)
        end = min(len(raw), idx + len(term) + 90)
        prefix = ("…" if start > 0 else "") + raw[start:idx]
        core = f"<mark>{term}</mark>"
        suffix = raw[idx + len(term):end] + ("…" if end < len(raw) else "")
        return prefix + core + suffix

    def _extract_datum(self, tr) -> str | None:
        raw = tr.get_text(" ", strip=True)
        # "…, den 24.03.2026 …" (Datum des Dokuments)
        m = re.search(r"den\s+(\d{2}\.\d{2}\.\d{4})", raw)
        return m.group(1) if m else None


def _form_value(form, name: str) -> str | None:
    if not form:
        return None
    el = form.find("input", {"name": name})
    if el is not None and el.get("value") is not None:
        return el["value"]
    return None


def _abs(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def _attachment_name(a, aria: str) -> str:
    """Name einer Anlage-Datei (Anlagen-Links haben meist kein aria-label).

    Fallback-Reihenfolge: aria ("Anlage <name> zum Vorgang …"), span.hide-text
    ("<name>.pdf (exportiert: …)"), sonst leer.
    """
    if aria:
        m = re.search(r"Anlage\s+(.*?)(?:\s+zum Vorgang|\s+exportiert)", aria)
        if m:
            return m.group(1).strip()
    hide = a.find("span", class_="hide-text")
    if hide:
        txt = hide.get_text(" ", strip=True)
        base = re.sub(r"\(exportiert:.*$", "", txt).strip()
        return base
    return ""


def _first_error(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    err = soup.select_one(".errorlist")
    if err:
        return " | " + err.get_text(" ", strip=True)[:160]
    return ""
