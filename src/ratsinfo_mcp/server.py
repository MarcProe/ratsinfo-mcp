"""MCP-Server (stdio) für SD.NET RIM (Sternberg) Ratsinformationssysteme.

Bietet vier Tools:

* ``recherche`` – Volltext-Recherche (inkl. indizierter PDF-Volltexte), optional
  mit OParl-Metadaten-Anreicherung.
* ``pdf_as_markdown`` – lädt ein RIS-PDF und liefert den Volltext als Markdown
  (mit Seitenauswahl, Suchbegriff-Filter, Zeichen-Cap).
* ``meeting_documents`` – Metadaten einer Sitzung (``/tops/``) oder eines
  Vorgangs (``/vorgang/``) inkl. aller PDF-URLs, pro TOP gruppiert (kein Volltext).
* ``find_meetings`` – Sitzungen aus dem ICS-Termin-Feed (Gremium, Datum, Ort,
  ``tops_url``) als Einstieg, um Detail-/PDF-URLs zu finden.

Typischer Agent-Fluss: ``recherche`` → (Treffer mit ``detail_url``/TOP) →
``meeting_documents`` → ``pdf_as_markdown`` auf das eine PDF.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import Config
from .detect import CompatibilityError, ensure_compatible
from .http_client import HttpClient, RISHTTPError
from .meetings import MeetingsError, find_meetings as _find_meetings, meeting_documents as _meeting_documents
from .oparl import OParlClient, extract_references
from .pdf_doc import PDFDocumentError, fetch_pdf_markdown
from .tos import TermsError, terms_of_use as _terms_of_use
from .recherche import (
    ALL_DOKTYP,
    DOKTYP_VALUES,
    RechercheClient,
    SearchParams,
    TYP_VALUES,
)

# Logging strikt nach stderr – stdout ist der MCP-stdio-Kanal.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ratsinfo_mcp.mcp")

mcp = FastMCP("ratsinfo-mcp")

# Prozessweites Memo: eine einzige, geprüfte Config-Instanz. Die Erkennung
# (Name + Periodisierung) wird beim ersten Aufruf einmalig durchgeführt
# (≈5–8 gedrosselte Requests); danach ist der Zugriff gratis.
_CFG: Config | None = None


def _get_config() -> Config:
    """Liefert die (memoisierte) Config; führt beim ersten Mal die
    fail-early-Kompatibilitätsprüfung durch.

    * Strict + nicht SD.NET RIM  → :class:`CompatibilityError` (klare Meldung).
    * Erkannt → Config trägt automatisch ``name`` (RIS_NAME) und
      ``period_scheme`` aus der Erkennung.

    Dieselbe Config-Instanz wird für alle Tool-Aufrufe wiederverwendet, damit
    die erkannten Attribute (name, period_scheme) in jedem Aufruf sichtbar sind.
    """
    global _CFG
    if _CFG is None:
        cfg = Config()
        ensure_compatible(cfg, profile=True, verify_count=False)
        _CFG = cfg
    return _CFG


def _reset_config_for_tests() -> None:
    """Test-Helfer: geleerte Config-Memo (damit ein Test nicht das Ergebnis
    eines anderen erbt, wenn die Env-Variablen sich ändern)."""
    global _CFG
    _CFG = None

# Akzeptierte Enum-Werte (Spezifikation + großzügige Varianten).
_DOKUMENTTYP_ALIASES = {
    "alle": "alle",
    "alledokumenteanlagenpersonenanfragen": "alle",
    "dokumente": "dokumente",
    "anlagen": "anlagen",
    "personen": "personen",
    "anfragen": "anfragen",
    "anfragennews": "anfragen",
}
_ART_ALIASES = {
    "vorlagen": "vorlagen",
    "vorlageneinladungenbeschluesse": "vorlagen",  # (Fangschuß, s.u.)
    "einladungen": "einladungen",
    "beschluesse": "beschluesse",
    "beschlüsse": "beschluesse",
    "niederschriften": "niederschriften",
}


def _norm_enum(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).strip().lower()


def _resolve_dokumenttyp(value: str | None) -> str:
    if not value:
        return "alle"
    key = _norm_enum(value)
    if key in _DOKUMENTTYP_ALIASES:
        return _DOKUMENTTYP_ALIASES[key]
    # Fallback: Substring-Match für Tippfehler-Toleranz.
    for k, v in _DOKUMENTTYP_ALIASES.items():
        if k in key:
            return v
    raise ValueError(
        f"Unbekanntes dokumenttyp {value!r}. Erlaubt: alle, dokumente, anlagen, "
        f"personen, anfragen."
    )


def _resolve_arten(value: list[str] | None) -> list[str]:
    if not value:
        return ["vorlagen", "einladungen", "beschluesse", "niederschriften"]
    resolved: list[str] = []
    for item in value:
        key = _norm_enum(item)
        if key not in _ART_ALIASES:
            # Sonderfall: der Spez-Eintrag "vorlageneinladungenbeschluesse"
            if key in ("vorlageneinladungenbeschluesse",):
                resolved.extend(["vorlagen", "einladungen", "beschluesse"])
                continue
            raise ValueError(
                f"Unbekannte Art {item!r}. Erlaubt: vorlagen, einladungen, "
                f"beschluesse, niederschriften."
            )
        v = _ART_ALIASES[key]
        if v not in resolved:
            resolved.append(v)
    if not resolved:
        return ["vorlagen", "einladungen", "beschluesse", "niederschriften"]
    return resolved


def _validate_date(value: str | None, field: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} muss das Format JJJJ-MM-TT haben (z.B. 2026-03-01).")
    return value


def _doktyp_codes(arten: list[str]) -> list[str]:
    return [DOKTYP_VALUES[a] for a in arten]


@mcp.tool()
def recherche(
    volltext: str | None = None,
    dokumenttyp: str | None = None,
    gremium: str | None = None,
    datum_von: str | None = None,
    datum_bis: str | None = None,
    arten: list[str] | None = None,
    genaue_suche: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Durchsucht das konfigurierte Ratsinformationssystem (RIS_BASE_URL).

    Abbildung der Webseite-Recherche: Volltextsuche über Dokumente, Anlagen,
    Personen und Anfragen/News – **inkl. indizierter PDF-Volltexte** (Fundstelle
    wird als Textausschnitt mit ``<mark>`` umrungenem Suchbegriff zurückgegeben;
    der PDF-Link öffnet das Dokument direkt am Treffer).

    Parameter:
      * volltext (optional): Freitextsuchbegriff(e), z.B. ein Wort aus dem PDF-
        Text oder eine Vorlagennummer wie "603/IX.".
      * dokumenttyp (optional): alle | dokumente | anlagen | personen | anfragen.
      * gremium (optional): Gremiumsname, z.B. "Rat", "Schulausschuss".
      * datum_von / datum_bis (optional): Zeitraum JJJJ-MM-TT.
      * arten (optional): Subset von vorlagen | einladungen | beschluesse |
        niederschriften (Default: alle vier).
      * genaue_suche (optional): exakte Wiedergabe (Default: false).
      * limit (Default 20, Max 100): Anzahl der zurückgegebenen Treffer.

    Rückgabe: strukturierte Trefferliste (typ, titel, vorlagennummer, gremium,
    datum, sitzungstermin, fundstelle, pdf_url, detail_url, anhaenge) plus
    Paginierungsinformation pro Dokumenttyp.

    Hinweis: Der Server liefert pro Suchlauf je Dokumenttyp die erste Seite
    (max. 25 Zeilen). ``limit`` begrenzt die Rückgabe zusätzlich.
    """
    cfg = _get_config()
    logger.info(
        "recherche: volltext=%r dokumenttyp=%r gremium=%r datum=%s..%s arten=%r genaue=%s limit=%s",
        volltext, dokumenttyp, gremium, datum_von, datum_bis, arten, genaue_suche, limit,
    )
    try:
        # Parameter normalisieren / validieren (ValueError → saubere Fehlermeldung).
        try:
            typ_key = _resolve_dokumenttyp(dokumenttyp)
            arten_norm = _resolve_arten(arten)
            datefrom = _validate_date(datum_von, "datum_von")
            dateto = _validate_date(datum_bis, "datum_bis")
            limit = max(1, min(int(limit), 100))
        except ValueError as e:
            return _tool_error("invalid_parameter", str(e))

        typ_id = TYP_VALUES[typ_key]

        with HttpClient(cfg, follow_redirects=False) as hc:
            rec = RechercheClient(cfg, hc)
            oparl = OParlClient(cfg, hc)

            # Gremium auflösen (ID aus dem Formular-Select).
            gremium_id = "-1"
            gremium_name = ""
            if gremium:
                rec.ensure_gremien()
                gid = rec.gremien.name_to_id(gremium)
                if gid is None:
                    # Fuzzy: Substring.
                    for cand in rec.gremien.all_names():
                        if gremium.strip().lower() in cand.lower():
                            gid = rec.gremien.name_to_id(cand)
                            gremium_name = cand
                            break
                if gid is None:
                    known = ", ".join(sorted(rec.gremien.all_names()))
                    return _tool_error(
                        "unknown_gremium",
                        f"Gremium {gremium!r} nicht gefunden. Bekannte Gremien: {known}",
                    )
                gremium_id = gid
                gremium_name = rec.gremien.id_to_name(gid) or gremium

            params = SearchParams(
                terms=(volltext or "").strip(),
                typ=typ_id,
                gremium=gremium_id,
                gremium_name=gremium_name,
                datefrom=datefrom,
                dateto=dateto,
                doktyp=_doktyp_codes(arten_norm),
                exakt=genaue_suche,
                count=limit,
            )
            result = rec.search(params)
            payload = result.to_dict()

            # Best-effort OParl-Anreicherung (nur bei Treffern mit Vorlagennummer).
            for idx, hit in enumerate(result.hits):
                if idx >= len(payload["treffer"]):
                    break
                try:
                    meta = oparl.enrich_hit(hit.vorlage_kennung, hit.titel)
                    if meta:
                        payload["treffer"][idx]["oparl"] = meta
                except RISHTTPError as e:
                    logger.warning("OParl-Anreicherung übersprungen: %s", e)
                except Exception as e:  # noqa: BLE001 – best effort, nie abbrechen
                    logger.warning("OParl-Anreicherung fehlgeschlagen: %s", e)

            payload["gremium_gefragt"] = gremium_name or gremium or None
            payload["noten"] = (
                "total_seiten_pro_typ = Seitenzahl der Ergebnisliste je Dokumenttyp "
                "(nicht exakte Trefferzahl). Der Server liefert pro Typ die erste "
                "Seite (max. 25); fundstelle enthält <mark>-markierten Suchbegriff; "
                "pdf_url öffnet das PDF direkt am Fundstellen-Treffer. OParl-Metadaten "
                "sind best effort (nur wenn Vorlagennummer im geladenen Zeitfenster)."
            )
            return payload
    except RISHTTPError as e:
        logger.warning("HTTP-Fehler: %s", e)
        return e.to_dict()
    except ValueError as e:
        return _tool_error("invalid_parameter", str(e))
    except Exception as e:  # noqa: BLE001 – Tool soll nie Traceback ausgeben
        logger.exception("Unerwarteter Fehler im Tool 'recherche'")
        return _tool_error("internal_error", f"Unerwarteter Fehler: {e}")


@mcp.tool()
def pdf_as_markdown(
    pdf_url: str,
    seiten: str | list[int] | None = None,
    suchbegriff: str | None = None,
    max_chars: int = 200000,
) -> dict[str, Any]:
    """Lädt ein RIS-PDF und liefert den Volltext als Markdown.

    Ergänzung zu ``recherche``: Der Treffer liefert bereits eine ``pdf_url``;
    dieses Tool macht daraus lesbaren Volltext – für AI-Agenten ohne Browser.
    Lädt das PDF direkt (ohne Session) und rendert je Seite eine
    ``## Seite N``-Überschrift. Nur PDF-URLs des RIS-Hosts, max. 20 MB / 500
    Seiten. In-Memory-Cache (1 h), damit Wiederholungen das Rate-Limit nicht
    erneut belasten.

    Parameter:
      * pdf_url (Pflicht): Absolute RIS-PDF-URL (``…/sdnetrim/….pdf``); ein
        ``#search=…``-Fragment darf dabei sein (wird entfernt).
      * seiten (optional): Seitenbereich/-liste, z. B. ``"12-18"`` oder
        ``[3,7,42]`` – liefert nur diese Seiten.
      * suchbegriff (optional): Nur Seiten liefern, die den Begriff enthalten;
        legt ``fundstellen_seiten`` an und meldet ``kein_treffer`` bei null.
      * max_chars (Default 200000, Max 1000000): Zeichen-Cap des Markdown.

    Rückgabe: ``pdf_url``, ``seiten_gesamt``, ``seiten_geiefert``,
    ``zeichen_gesamt``/``zeichen_geiefert``, ``gekürzt``, ``markdown`` (und bei
    ``suchbegriff``: ``fundstellen_seiten``/``kein_treffer``).

    Hinweis: Tabellen-/Liste-Formatierung des PDFs wird nicht rekonstruiert
    (wie bei pdftotext); für Detailfragen ist das Original-PDF maßgeblich.
    """
    logger.info("pdf_as_markdown: url=%r seiten=%r suchbegriff=%r max_chars=%s", pdf_url, seiten, suchbegriff, max_chars)
    try:
        limit = max(200, min(int(max_chars), 1000000))
    except (TypeError, ValueError):
        return _tool_error("invalid_parameter", f"max_chars muss eine Zahl sein (kamen {max_chars!r}).")
    try:
        cfg = _get_config()
        with HttpClient(cfg) as hc:
            return fetch_pdf_markdown(cfg, hc, pdf_url, seiten=seiten,
                                      suchbegriff=suchbegriff, max_chars=limit)
    except PDFDocumentError as e:
        return e.to_dict()
    except RISHTTPError as e:
        logger.warning("PDF-HTTP-Fehler: %s", e)
        return e.to_dict()
    except Exception as e:  # noqa: BLE001
        logger.exception("Unerwarteter Fehler in 'pdf_as_markdown'")
        return _tool_error("internal_error", f"Unerwarteter Fehler: {e}")


@mcp.tool()
def meeting_documents(detail_url: str) -> dict[str, Any]:
    """Metadaten einer Sitzung (``/tops/``) oder eines Vorgangs (``/vorgang/``).

    Liefert eine Übersicht **aller auf der Seite verlinkten PDFs** (Dokumenttyp
    + Name + URL), bei Sitzungen **pro TOP gruppiert** – **ohne** Volltext. So
    bleibt die Antwort klein (eine Ratssitzung hat z. B. 76 PDFs); den Volltext
    eines ausgewählten Dokuments holt man gezielt mit ``pdf_as_markdown``.
    Besonders nützlich: der Eintrag ``art=sitzungspaket`` = ein einzelnes
    kombiniertes PDF der ganzen Sitzung.

    Parameter:
      * detail_url (Pflicht): Eine ``/tops/?__=…`` (Sitzung) oder
        ``/vorgang/?__=…`` (Dossier)-URL aus dem RIS, z. B. die ``detail_url``
        eines ``recherche``-Treffers oder die ``tops_url`` aus ``find_meetings``.

    Rückgabe: ``typ`` (sitzung|vorgang), Gremium/Datum/Ort, ``uebersicht``
    (Sitzungs-Dokumente inkl. tagesordnung/niederschrift/sitzungspaket) und
    ``tops[]`` mit je ``dokumente[]`` (art, name, url) bzw. beim Vorgang die
    Dokumente-Liste.
    """
    logger.info("meeting_documents: url=%r", detail_url)
    try:
        cfg = _get_config()
        with HttpClient(cfg) as hc:
            return _meeting_documents(cfg, hc, detail_url)
    except MeetingsError as e:
        return e.to_dict()
    except RISHTTPError as e:
        logger.warning("Detail-HTTP-Fehler: %s", e)
        return e.to_dict()
    except Exception as e:  # noqa: BLE001
        logger.exception("Unerwarteter Fehler in 'meeting_documents'")
        return _tool_error("internal_error", f"Unerwarteter Fehler: {e}")


@mcp.tool()
def find_meetings(
    gremium: str | None = None,
    datum_von: str | None = None,
    datum_bis: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Listet Sitzungen aus dem RIS-Termin-Feed (ICS) – Einstieg für URLs.

    Löst die Frage „wo ist die Detail-URL?" ohne Suchlauf: Der serverseitige
    iCal-Feed (~250 Sitzungen) liefert pro Sitzung Gremium, Datum, Ort und
    einen direkten ``/tops/``-Link, der für ``meeting_documents`` geeignet ist.

    Parameter:
      * gremium (optional): Filter nach Gremiumsname, z. B. ``"Rat"``
        (Teilzeichenfolge, case-insensitive).
      * datum_von / datum_bis (optional): Zeitraum ``JJJJ-MM-TT``.
      * limit (Default 30, Max 100): Anzahl der zurückgegebenen Sitzungen
        (neueste zuerst).

    Rückgabe: ``source``, ``anzahl_gesamt`` (nach Filter) und ``ergebnisse[]``
    mit ``gremium``, ``datum``, ``ort``, ``tops_url``.
    """
    logger.info("find_meetings: gremium=%r datum=%s..%s limit=%s", gremium, datum_von, datum_bis, limit)
    try:
        datum_von = _validate_date(datum_von, "datum_von")
        datum_bis = _validate_date(datum_bis, "datum_bis")
        limit = max(1, min(int(limit), 100))
    except ValueError as e:
        return _tool_error("invalid_parameter", str(e))
    try:
        cfg = _get_config()
        with HttpClient(cfg) as hc:
            return _find_meetings(cfg, hc, gremium=gremium,
                                  datum_von=datum_von, datum_bis=datum_bis, limit=limit)
    except RISHTTPError as e:
        logger.warning("ICS-HTTP-Fehler: %s", e)
        return e.to_dict()
    except Exception as e:  # noqa: BLE001
        logger.exception("Unerwarteter Fehler in 'find_meetings'")
        return _tool_error("internal_error", f"Unerwarteter Fehler: {e}")


@mcp.tool()
def terms_of_use(
    url: str | None = None,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Nutzungsbedingungen / Impressum / Datenschutz der RIS-Instanz.

    Liefert dem Agenten die Rohdaten + Keyword-Flags, damit *er* (das LLM)
    selbst analysieren kann, unter welchen Bedingungen die abgerufenen
    Dokumente zitiert / aufbereitet / weiterverwendet werden dürfen.

    Zwei Modi:
      * ``url`` weggelassen → **Discovery**: listet auf der Root-/Recherche-
        Seite gefundene ToS-Links (Impressum, Datenschutz, Nutzungs-/
        AGB, Rechtshinweise, Barrierefreiheit, Offene-Daten) mit URLs.
      * ``url`` gesetzt → **Extraktion**: lädt die Seite, liefert sauberen
        Markdown-Text (Hauptbereich, ohne Navigation) + eine
        ``keyword_flags``-Karte (Wiedergabe, urheberrecht, kommerziell,
        freie_nutzung, quellenangabe, haftungsfrei … jeweils
        ``gefunden`` + kurzer ``kontext``).

    Parameter:
      * url (optional): Absolute ToS-URL (aus der Discovery). Weggelassen =
        Discovery. Nur Seiten desselben Trägers (RIS-Host oder Stadt-Domain).
      * max_chars (Default 4000, Max 20000): Zeichen-Cap des extrahierten Texts.

    Rückgabe: ``discovery`` (Kategorien + URLs) und bei Extraktion
    ``extracted`` (title, text, keyword_flags). Der Server rütt *kein*
    zweites LLM auf – die semantische Bewertung liegt beim aufrufenden Agenten;
    das Tool liefert deterministisch Belege (Text + Flags).
    """
    logger.info("terms_of_use: url=%r max_chars=%s", url, max_chars)
    try:
        limit = max(500, min(int(max_chars), 20000))
    except (TypeError, ValueError):
        return _tool_error("invalid_parameter", f"max_chars muss eine Zahl sein (kamen {max_chars!r}).")
    cfg = _get_config()
    try:
        with HttpClient(cfg) as hc:
            return _terms_of_use(cfg, hc, url=url, max_chars=limit)
    except TermsError as e:
        return e.to_dict()
    except RISHTTPError as e:
        logger.warning("ToS-HTTP-Fehler: %s", e)
        return e.to_dict()
    except Exception as e:  # noqa: BLE001
        logger.exception("Unerwarteter Fehler in 'terms_of_use'")
        return _tool_error("internal_error", f"Unerwarteter Fehler: {e}")


def _tool_error(code: str, message: str) -> dict[str, Any]:
    return {"error": code, "message": message, "treffer": []}


def main() -> None:
    """Einstiegspunkt: fail-early-Kompatibilitätsprüfung, dann stdio-Server."""
    logger.info("ratsinfo-mcp MCP-Server (v%s) startet – Fail-early-Prüfung.", __version__)
    try:
        _get_config()  # prüft die konfigurierte Instanz (Strict → Fehler)
    except CompatibilityError as e:
        logger.error("FATAL – Instanz inkompatibel: %s", e)
        print(json.dumps(e.to_dict(), ensure_ascii=False), file=sys.stderr)
        sys.exit(2)  # Server startet NICHT mit einer Nicht-RIM-Instanz
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
