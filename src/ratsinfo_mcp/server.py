"""MCP-Server (stdio) für SD.NET RIM (Sternberg) Ratsinformationssysteme.

Bietet acht Tools:

* ``recherche`` – Volltext-Recherche (inkl. indizierter PDF-Volltexte), optional
  mit OParl-Metadaten-Anreicherung (Papers) bzw. OParl-Personen-Anreicherung
  bei ``dokumenttyp=personen``.
* ``pdf_as_markdown`` – lädt ein RIS-PDF und liefert den Volltext als Markdown
  (mit Seitenauswahl, Suchbegriff-Filter, Zeichen-Cap).
* ``meeting_documents`` – Metadaten einer Sitzung (``/tops/``) oder eines
  Vorgangs (``/vorgang/``) inkl. aller PDF-URLs, pro TOP gruppiert (kein Volltext).
* ``find_meetings`` – Sitzungen aus dem ICS-Termin-Feed (Gremium, Datum, Ort,
  ``tops_url``) als Einstieg, um Detail-/PDF-URLs zu finden.
* ``find_person`` – strukturierte Personensuche (OParl-Personen-Index der
  Instanz + HTML-Personenindex als Komplement; nur RIM-Daten, keine Adresse).
* ``person_steckbrief`` – vorgefertigte Personen-Steckbrief-Karte (Markdown/
  HTML), deterministisch gerendert, ohne zweites LLM.
* ``committee_members`` – Mitglieder eines Gremiums (OParl
  ``/committee/{id}/person``), Gremiennamen normalisiert aufgelöst.
* ``terms_of_use`` – Nutzungsbedingungen/Impressum/Datenschutz der Instanz.

Typischer Agent-Fluss: ``recherche`` → (Treffer mit ``detail_url``/TOP) →
``meeting_documents`` → ``pdf_as_markdown`` auf das eine PDF. Personen-Fluss:
``find_person`` → ``person_steckbrief`` (einmalig aufgelöst) bzw.
``committee_members`` (Gremien) und ``recherche(volltext=<name>)`` für
Dokumente, in denen die Person vorkommt.
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
from .oparl import OParlClient, _enrich_enabled, norm_name, extract_references
from .pdf_doc import PDFDocumentError, fetch_pdf_markdown
from .persons import render_steckbrief
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

            # Best-effort OParl-Personen-Anreicherung (nur bei
            # dokumenttyp=personen UND OPARL_ENRICH=1 – wie die Paper-
            # Anreicherung standardmäßig AUS, weil sie den Person-Index
            # (seitenbegrenzt, rate-limited) lädt und die Antwort verlängern
            # kann). Name → OParl-Person-Index (normalisiert, eindeutig nur
            # bei exakt einem Treffer); liefert oparl_id/fraktion/gremien/
            # mandatszeit; best effort.
            if typ_key == "personen" and _enrich_enabled():
                for idx, hit in enumerate(result.hits):
                    if idx >= len(payload["treffer"]):
                        break
                    if not hit.titel:
                        continue
                    try:
                        pmatches = oparl.lookup_person(hit.titel, limit=1)
                        if len(pmatches) == 1:
                            p = pmatches[0]
                            payload["treffer"][idx]["oparl"] = {
                                "oparl_id": p.get("oparl_id"),
                                "fraktion": p.get("fraktion"),
                                "mandatszeit": p.get("mandatszeit"),
                                "gremien": p.get("gremien"),
                            }
                    except RISHTTPError as e:
                        logger.warning("OParl-Personen-Anreicherung übersprungen: %s", e)
                    except Exception as e:  # noqa: BLE001 – best effort
                        logger.warning("OParl-Personen-Anreicherung fehlgeschlagen: %s", e)

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
def find_person(
    name: str,
    fraktion: str | None = None,
    gremium: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Strukturierte Personensuche im RIM (OParl-Index + HTML-Personenindex).

    Findet Mandatsträger:innen / Personen, die das RIM selbst abbildet –
    **nur Daten der konfigurierten RIS-Instanz** (OParl-Webdienst + HTML-
    Personenindex derselben Instanz; keine externen Quellen).

    Primärquelle: OParl ``/body/1/person`` (lazy, TTL-gecacht, seitenbegrenzt)
    – liefert Name, Fraktion, Gremien mit Funktion, Mandatszeit, ``oparl_id``.
    Komplementär (falls OParl leer oder nicht verfügbar): HTML-Recherche
    ``dokumenttyp=personen`` – deckt auch Personen ab, die OParl nicht
    abbildet; HTML-only-Treffer tragen Name, Personenkreis und ``personen_url``
    (Personenseite) – **ohne Adresse** (Datenminimierung; die Anschrift bleibt
    auf der Personenseite).

    Parameter:
      * name (Pflicht): Suchbegriff (z. B. Nachname oder Vollname); normalisiert
        (case-/Umlaut-insensitiv), Teilstrings matchen.
      * fraktion (optional): Filter, z. B. „CDU“, „GRÜNE“ (Teilstring).
      * gremium (optional): Filter nach Gremiennamen, z. B. „Rat“,
        „Schulausschuss“ (Teilstring).
      * limit (Default 10, Max 50): Anzahl der zurückgegebenen Treffer.

    Rückgabe: ``source``, ``query``, ``anzahl_gesamt``, ``treffer[]`` mit
    ``name``, ``fraktion``, ``mandatszeit``, ``gremien[]`` (gremium, funktion,
    beginn, ende), ``oparl_id``, ``personen_url`` (RIM-Personenseite),
    ``oparl_url``, ``quelle`` (``oparl`` | ``html``) – exakte
    Normalisierungs-Treffer zuerst (Disambiguierung durch den Agenten über
    Fraktion/Gremium/``oparl_id``).

    Typischer Fluss: ``find_person`` → ``person_steckbrief`` oder
    ``recherche(volltext=<name>)`` (Dokumente, in denen die Person vorkommt).
    """
    logger.info(
        "find_person: name=%r fraktion=%r gremium=%r limit=%s",
        name, fraktion, gremium, limit,
    )
    if not (name or "").strip():
        return _tool_error("invalid_parameter", "name darf nicht leer sein.")
    limit = max(1, min(int(limit), 50))
    try:
        cfg = _get_config()
    except Exception as e:  # noqa: BLE001
        logger.exception("Config-Initialisierung fehlgeschlagen")
        return _tool_error("internal_error", str(e))

    oparl_hits: list[dict[str, Any]] = []
    html_hits: list[dict[str, Any]] = []
    oparl_available = True

    with HttpClient(cfg) as hc:
        oparl = OParlClient(cfg, hc)
        try:
            for p in oparl.lookup_person(name, fraktion=fraktion,
                                         gremium=gremium, limit=limit):
                p = dict(p)
                p["quelle"] = "oparl"
                p["personen_url"] = None
                p["oparl_url"] = f"{cfg.oparl_base}{p.get('oparl_id') or ''}"
                oparl_hits.append(p)
            # Lade-Status: False = der Index-Request war fehlgeschlagen
            # (best effort, leerer Index) → OParl gilt als nicht verfügbar.
            if oparl._person_load_ok is False:
                oparl_available = False
        except RISHTTPError as e:
            logger.warning("OParl-Personen-Index nicht erreichbar: %s", e)
            oparl_available = False
        except Exception as e:  # noqa: BLE001 – best effort, nie abbrechen
            logger.warning("OParl-Personensuche fehlgeschlagen: %s", e)
            oparl_available = False

        # HTML-Komplement: nur wenn OParl leer oder nicht verfügbar.
        if not oparl_hits or not oparl_available:
            try:
                rec = RechercheClient(cfg, hc)
                params = SearchParams(
                    terms=name.strip(),
                    typ=TYP_VALUES["personen"],
                    gremium="-1",
                    datefrom="",
                    dateto="",
                    doktyp=["T", "E", "B", "N"],
                    exakt=False,
                    count=limit,
                )
                result = rec.search(params)
                gremium_n = norm_name(gremium or "")
                for hit in result.hits:
                    if gremium_n and gremium_n not in norm_name(hit.gremium or ""):
                        continue
                    html_hits.append({
                        "name": hit.titel,
                        "fraktion": hit.personenkreis,
                        "mandatszeit": None,
                        "gremien": [],
                        "oparl_id": None,
                        "personen_url": hit.detail_url,
                        "oparl_url": None,
                        "quelle": "html",
                    })
            except RISHTTPError as e:
                logger.warning("HTML-Personensuche fehlgeschlagen: %s", e)
            except Exception as e:  # noqa: BLE001 – best effort
                logger.warning("HTML-Personensuche unerwarteter Fehler: %s", e)

    # Verschmelzen: OParl-Treffer haben Vorrang; HTML-only-Treffer (Name noch
    # nicht vorhanden) folgen – Duplikate werden per norm_name entfernt.
    seen_norms = {norm_name(h["name"] or "") for h in oparl_hits}
    merged = list(oparl_hits)
    for h in html_hits:
        key = norm_name(h["name"] or "")
        if key in seen_norms:
            continue
        seen_norms.add(key)
        merged.append(h)
    merged = merged[:limit]

    return {
        "source": (
            f"Personensuche ({cfg.host})"
            + (f" – {cfg.name}" if getattr(cfg, "name", "") else "")
        ),
        "query": {"name": name, "fraktion": fraktion, "gremium": gremium},
        "anzahl_gesamt": len(merged),
        "treffer": merged,
        "oparl_verfügbar": oparl_available,
        "noten": (
            "quelle=oparl: vollständige OParl-Metadaten (Fraktion, Gremien, "
            "Mandatszeit). quelle=html: nur Name/Personenkreis/Personenseite "
            "(OParl deckt diese Person nicht ab). Adresse wird bewusst NICHT "
            "mitgeliefert (Datenminimierung) – siehe personen_url."
        ),
    }


@mcp.tool()
def person_steckbrief(
    name: str | None = None,
    oparl_id: str | None = None,
    format: str = "markdown",
    max_chars: int = 8000,
) -> dict[str, Any]:
    """Vorgefertigte Personen-Steckbrief-Karte (deterministisch, kein zweites LLM).

    Rendernt aus den RIM-Daten (OParl- oder HTML-Personenindex der
    konfigurierten Instanz) eine kompakte, menschen- und agentenlesbare
    Steckbrief-Karte – im Stil von ``terms_of_use``: Rohdaten + feste
    Vorlage, keine semantische Bewertung durch den Server. Das Ergebnis
    (``markdown``- oder ``html``-Feld) kann 1:1 in Antworten/Docs eingebettet
    werden.

    Auflösung:
      * ``oparl_id`` gesetzt → Person direkt aus dem OParl-Personen-Index.
      * sonst ``name`` → OParl-Matching, sonst HTML-Personenindex; bei
        mehrdeutigem Namen liefert das Tool ``mehrfache_treffer`` +
        ``kandidaten[]`` statt einer Karte (Disambiguierung bleibt
        deterministisch – erneut aufrufen mit ``oparl_id``).

    Parameter:
      * name (optional, mit oparl_id XOR): Personenname (Teilstring erlaubt).
      * oparl_id (optional, mit name XOR): OParl-ID aus ``find_person``,
        z. B. ``/body/1/person/42``.
      * format (Default ``markdown``): ``markdown`` oder ``html``.
      * max_chars (Default 8000, Max 50000): Zeichen-Cap der Karte.

    Rückgabe (Erfolg): ``format``, ``name``, ``markdown`` (bzw. ``html``),
    ``gekürzt``, ``max_chars``, ``oparl_id``, ``personen_url``, ``quelle``.
    """
    logger.info(
        "person_steckbrief: name=%r oparl_id=%r format=%s max_chars=%s",
        name, oparl_id, format, max_chars,
    )
    if not name and not oparl_id:
        return _tool_error(
            "invalid_parameter",
            "mindestens ein Parameter erforderlich: name ODER oparl_id",
        )
    try:
        limit = max(500, min(int(max_chars), 50000))
    except (TypeError, ValueError):
        return _tool_error(
            "invalid_parameter", f"max_chars muss eine Zahl sein (kamen {max_chars!r}).")
    try:
        cfg = _get_config()
    except Exception as e:  # noqa: BLE001
        logger.exception("Config-Initialisierung fehlgeschlagen")
        return _tool_error("internal_error", str(e))

    resolved: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []

    with HttpClient(cfg) as hc:
        oparl = OParlClient(cfg, hc)
        if oparl_id:
            try:
                resolved = oparl.person_by_oparl_id(oparl_id)
            except RISHTTPError as e:
                logger.warning("OParl-Person-Auflösung fehlgeschlagen: %s", e)
            if resolved is None:
                return _tool_error(
                    "unknown_person",
                    f"OParl-ID {oparl_id!r} nicht im Person-Index gefunden.",
                )
            resolved["quelle"] = "oparl"
            resolved["personen_url"] = None
            resolved["oparl_url"] = f"{cfg.oparl_base}{oparl_id}"
        else:
            # Name → OParl-Index (best effort), sonst HTML-Personenindex.
            try:
                matches = oparl.lookup_person(name or "", limit=5)
                if len(matches) == 1:
                    resolved = matches[0]
                    resolved["quelle"] = "oparl"
                    resolved["personen_url"] = None
                    resolved["oparl_url"] = f"{cfg.oparl_base}{resolved.get('oparl_id') or ''}"
                else:
                    candidates = matches
            except RISHTTPError as e:
                logger.warning("OParl-Name-Auflösung fehlgeschlagen: %s", e)
            except Exception as e:  # noqa: BLE001 – best effort
                logger.warning("OParl-Name-Auflösung unerwarteter Fehler: %s", e)
            if resolved is None:
                try:
                    rec = RechercheClient(cfg, hc)
                    params = SearchParams(
                        terms=(name or "").strip(), typ=TYP_VALUES["personen"],
                        gremium="-1", datefrom="", dateto="",
                        doktyp=["T", "E", "B", "N"], exakt=False, count=5,
                    )
                    result = rec.search(params)
                    html_matches = [
                        {
                            "name": h.titel,
                            "fraktion": h.personenkreis,
                            "mandatszeit": None,
                            "gremien": [],
                            "oparl_id": None,
                            "personen_url": h.detail_url,
                            "oparl_url": None,
                            "quelle": "html",
                        }
                        for h in result.hits if h.titel
                    ]
                    if len(html_matches) == 1:
                        resolved = html_matches[0]
                    elif not candidates:
                        candidates = html_matches
                except RISHTTPError as e:
                    logger.warning("HTML-Personenindex fehlgeschlagen: %s", e)
                except Exception as e:  # noqa: BLE001 – best effort
                    logger.warning("HTML-Personenindex unerwarteter Fehler: %s", e)

    if resolved is None:
        if candidates:
            return {
                "error": "mehrfache_treffer",
                "message": (
                    f"Mehrdeutiger Name {name!r}: {len(candidates)} Kandidaten. "
                    "Mit oparl_id erneut aufrufen."
                ),
                "kandidaten": [
                    {
                        "name": c.get("name"),
                        "oparl_id": c.get("oparl_id"),
                        "fraktion": c.get("fraktion"),
                        "gremien_anzahl": len(c.get("gremien") or []),
                    }
                    for c in candidates[:10]
                ],
            }
        return _tool_error(
            "unknown_person",
            f"Person {name!r} wurde weder im OParl- noch im HTML-Personenindex "
            "gefunden.",
        )

    try:
        card = render_steckbrief(
            resolved, getattr(cfg, "name", "") or "", format=format, max_chars=limit
        )
    except ValueError as e:
        return _tool_error("invalid_parameter", str(e))
    card["oparl_id"] = resolved.get("oparl_id")
    card["personen_url"] = resolved.get("personen_url")
    card["quelle"] = resolved.get("quelle")
    return card


@mcp.tool()
def committee_members(gremium: str, limit: int = 100) -> dict[str, Any]:
    """Mitglieder eines Gremiums auflösen (OParl ``/committee/{id}/person``).

    Beantwortet „Wer sitzt in Gremium X?“ direkt, ohne Suchlauf. Der
    Gremiennamen wird über den OParl-Gremien-Index normalisiert aufgelöst
    (zuerst exakt, dann normalisierter Teilstring). Alle Daten stammen
    **nur aus der konfigurierten RIM-Instanz**.

    Parameter:
      * gremium (Pflicht): Gremiennamen, z. B. „Rat“, „Schulausschuss“.
      * limit (Default 100, Max 500): Anzahl der zurückgegebenen Mitglieder.

    Rückgabe: ``source``, ``gremium`` (aufgelöster Name), ``gremium_id``,
    ``anzahl_gesamt`` und ``mitglieder[]`` mit ``name``, ``funktion`` (falls
    zugeordnet), ``personenkreis`` (Fraktion), ``mandatszeit``, ``oparl_id``.
    """
    logger.info("committee_members: gremium=%r limit=%s", gremium, limit)
    if not (gremium or "").strip():
        return _tool_error("invalid_parameter", "gremium darf nicht leer sein.")
    limit = max(1, min(int(limit), 500))
    try:
        cfg = _get_config()
    except Exception as e:  # noqa: BLE001
        logger.exception("Config-Initialisierung fehlgeschlagen")
        return _tool_error("internal_error", str(e))
    with HttpClient(cfg) as hc:
        oparl = OParlClient(cfg, hc)
        cid = oparl.committee_name_to_id(gremium)
        if cid is None:
            return _tool_error(
                "unknown_gremium",
                f"Gremium {gremium!r} nicht im OParl-Gremien-Index gefunden.",
            )
        try:
            members = oparl.committee_members(cid)
        except RISHTTPError as e:
            logger.warning("OParl-Mitgliederauflösung fehlgeschlagen: %s", e)
            return e.to_dict()
        except Exception as e:  # noqa: BLE001
            logger.exception("Unerwarteter Fehler in 'committee_members'")
            return _tool_error("internal_error", f"Unerwarteter Fehler: {e}")
        return {
            "source": (
                f"OParl-Webdienst ({cfg.host})"
                + (f" – {cfg.name}" if getattr(cfg, "name", "") else "")
            ),
            "gremium": oparl.committee_name_by_id(cid) or gremium,
            "gremium_id": cid,
            "anzahl_gesamt": len(members),
            "mitglieder": members[:limit],
        }


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
