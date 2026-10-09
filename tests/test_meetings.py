"""Tests für ratsinfo_mcp.meetings – /tops/-Parsing, /vorgang/-Parsing, ICS-Feed.

Offline: echte Captured-HTMLs/ICS aus tests/fixtures, FakeClient als HTTP-Schicht.
"""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup

from ratsinfo_mcp.config import Config
from ratsinfo_mcp.http_client import RISHTTPError
from ratsinfo_mcp.meetings import (
    MeetingsError,
    _clean_gremium,
    _doc_type,
    _parse_ics_date,
    _parse_tops,
    _parse_vorgang,
    _unfold_ics,
    find_meetings,
    meeting_documents,
)
from tests.conftest import FakeClient, FakeResponse

BASE = "https://ris.teststadt.de"
TOPS_URL = "https://ris.teststadt.de/tops/?__=UGhVM0hpd2NXNFdFcExjZRF95vT"
VORGANG_URL = "https://ris.teststadt.de/vorgang/?__=UGhVM0hpd2NXNFdFcExjZfQ5ARw4u"


# --- ICS-Helfer ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("A:1\nB:2", ["A:1", "B:2"]),
        # RFC5545: Fortsetzungszeilen BEGINNEN mit einem Leerzeichen, das
        # entfernt wird; ohne Leerzeichen ist es eine NEUE Zeile.
        ("DESC:zeile eins\nline zwei\nEND", ["DESC:zeile eins", "line zwei", "END"]),
        ("DESC:zeile eins\n line zwei\nEND", ["DESC:zeile einsline zwei", "END"]),
        ("CRLF\r\ntest", ["CRLF", "test"]),
    ],
)
def test_unfold_ics(text, expected):
    assert _unfold_ics(text) == expected


def test_parse_ics_date():
    assert _parse_ics_date("20261008T152933Z") == "2026-10-08"
    assert _parse_ics_date("20241009T170000") == "2024-10-09"
    assert _parse_ics_date("20241009T170000Z") == "2024-10-09"
    assert _parse_ics_date("1730000") is None  # nur 7 Ziffern → kein Datum
    assert _parse_ics_date("gar keins") is None
    assert _parse_ics_date("") is None
    assert _parse_ics_date(None) is None


# --- Dokumenttyp-Klassifikation --------------------------------------------------------
@pytest.mark.parametrize(
    "aria,expected",
    [
        ("Öffentliche Tagesordnung Rat 09.10.2024 im PDF-Format öffnen", "tagesordnung"),
        ("Öffentliche Niederschrift Rat 09.10.2024 im PDF-Format öffnen", "niederschrift"),
        ("Gesamtes Sitzungspaket im PDF-Format herunterladen", "sitzungspaket"),
        ("Anlage Produkt 0801 zum Vorgang 926 /XI. exportiert am 29.08.2024", "anlage"),
        ("Anlage 20241009_KWP.pdf zum TOP 1. exportiert", "anlage"),
        ("Beschlusstext 954 /XI. (öffentlich) Rat 09.10.2024 im PDF-Format öffnen", "beschlusstext"),
        ("Drucksache XI. 954 /XI. im PDF-Format öffnen", "drucksache"),
        ("Antrag GRÜNE XII. A 1 /XII.-GRÜNE im PDF-Format öffnen", "antrag"),
        ("irgendwas.pdf im PDF-Format öffnen", "pdf"),
        ("", "pdf"),
    ],
)
def test_doc_type(aria, expected):
    assert _doc_type(aria) == expected


def test_clean_gremium():
    assert _clean_gremium("Rat , 28. XI. Ratsperiode Sitzung") == "Rat"
    assert _clean_gremium("Sportausschuss, 5. XII. Ratsperiode Sitzung") == "Sportausschuss"
    assert _clean_gremium(None) is None
    assert _clean_gremium("") is None


# --- /tops/-Parsing ----------------------------------------------------------------------
def test_parse_tops_metadata(tops_html: str):
    d = _parse_tops(BeautifulSoup(tops_html, "html.parser"), Config())
    assert d["typ"] == "sitzung"
    assert d["gremium"] == "Rat"
    assert "09.10.2024" in d["termin"]
    assert "Rathaus" in d["ort"]


def test_parse_tops_uebersicht(tops_html: str):
    d = _parse_tops(BeautifulSoup(tops_html, "html.parser"), Config())
    arts = [x["art"] for x in d["uebersicht"]]
    assert "tagesordnung" in arts
    assert "niederschrift" in arts
    assert "sitzungspaket" in arts
    for x in d["uebersicht"]:
        assert x["url"].startswith(BASE)
        assert x["name"]


def test_parse_tops_pro_top(tops_html: str):
    d = _parse_tops(BeautifulSoup(tops_html, "html.parser"), Config())
    assert len(d["tops"]) == 44
    top3 = d["tops"][2]
    assert top3["nr"] == "3."
    assert top3["vorlage"] == "954 /XI."
    assert [x["art"] for x in top3["dokumente"]] == ["drucksache", "beschlusstext"]
    # Alle Dokumente haben absolute URLs
    for top in d["tops"]:
        for x in top["dokumente"]:
            assert x["url"].startswith("https://")


def test_parse_tops_pdf_count(tops_html: str):
    d = _parse_tops(BeautifulSoup(tops_html, "html.parser"), Config())
    assert d["n_pdfs"] == 76  # 3 Übersicht + 73 in TOPs (reales Capture)


def test_parse_tops_dedup_uebersicht(tops_html: str):
    d = _parse_tops(BeautifulSoup(tops_html, "html.parser"), Config())
    urls = [x["url"] for x in d["uebersicht"]]
    assert len(urls) == len(set(urls))


# --- /vorgang/-Parsing --------------------------------------------------------------------
def test_parse_vorgang(vorgang_html: str):
    d = _parse_vorgang(BeautifulSoup(vorgang_html, "html.parser"), Config())
    assert d["typ"] == "vorgang"
    assert d["vorlage"] == "Drucksache XI. 926 /XI."
    assert d["betreff"] and "Controllingbericht" in d["betreff"]
    assert d["federfuehrung"] and "Fachbereich" in d["federfuehrung"]
    arts = [x["art"] for x in d["uebersicht"]]
    assert arts == ["drucksache", "anlage", "anlage", "beschlusstext"]
    assert d["n_pdfs"] == 4
    assert d["tops"] == []


# --- meeting_documents (HTTP-Ebene) --------------------------------------------------------
def test_meeting_documents_tops(tops_client: FakeClient, cfg: Config):
    d = meeting_documents(cfg, tops_client, TOPS_URL)
    assert d["detail_url"] == TOPS_URL
    assert d["typ"] == "sitzung"
    assert d["n_pdfs"] == 76


def test_meeting_documents_vorgang(vorgang_client: FakeClient, cfg: Config):
    d = meeting_documents(cfg, vorgang_client, VORGANG_URL)
    assert d["typ"] == "vorgang"
    assert d["n_pdfs"] == 4


def test_meeting_documents_relative_url(vorgang_client: FakeClient, cfg: Config):
    d = meeting_documents(cfg, vorgang_client, "/vorgang/?__=TOKEN")
    assert d["detail_url"] == BASE + "/vorgang/?__=TOKEN"


def test_meeting_documents_not_detail_page(cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(200, text="<html></html>"))
    with pytest.raises(MeetingsError) as ei:
        meeting_documents(cfg, fc, "https://ris.teststadt.de/recherche")
    assert ei.value.code == "not_a_detail_page"


def test_meeting_documents_forbidden_url(cfg: Config):
    with pytest.raises(MeetingsError) as ei:
        meeting_documents(cfg, FakeClient(), "https://example.com/tops/?__=x")
    assert ei.value.code == "forbidden_url"


def test_meeting_documents_empty_url(cfg: Config):
    with pytest.raises(MeetingsError) as ei:
        meeting_documents(cfg, FakeClient(), "  ")
    assert ei.value.code == "invalid_parameter"


def test_meeting_documents_http_error(cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(500))
    with pytest.raises(RISHTTPError) as ei:
        meeting_documents(cfg, fc, TOPS_URL)
    assert ei.value.status == 500


def test_meeting_documents_no_pdfs_note(cfg: Config):
    # Künftige Sitzung ohne veröffentlichte Dokumente → saubere notiz
    html = """<html><body><table class="table-details">
    <tr><td>Sitzung:</td><td>Rat, 29. XI. Ratsperiode <a href="#">Sitzung</a></td></tr>
    <tr><td>Termin:</td><td>Mi, 16.12.2026 15:00 Uhr</td></tr>
    </table></body></html>"""
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(200, text=html))
    d = meeting_documents(cfg, fc, TOPS_URL)
    assert d["n_pdfs"] == 0
    assert "Keine PDF-Dokumente" in d["notiz"]
    assert d["tops"] == []


# --- find_meetings (ICS) --------------------------------------------------------------------
def test_find_meetings_all(ics_client: FakeClient, cfg: Config):
    r = find_meetings(cfg, ics_client)
    assert r["source"]
    assert r["anzahl_gesamt"] > 200
    assert len(r["ergebnisse"]) == 30  # Default-Limit
    for e in r["ergebnisse"]:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["datum"])
        assert e["gremium"]
    # Neuere zuerst
    dates = [e["datum"] for e in r["ergebnisse"]]
    assert dates == sorted(dates, reverse=True)


def test_find_meetings_gremium_word_boundary(ics_client: FakeClient, cfg: Config):
    r = find_meetings(cfg, ics_client, gremium="Rat", limit=100)
    assert r["anzahl_gesamt"] == 20  # reales Capture: genau 20 „Rat“-Events
    assert all(e["gremium"] == "Rat" for e in r["ergebnisse"])


def test_find_meetings_gremium_case_insensitive(ics_client: FakeClient, cfg: Config):
    r = find_meetings(cfg, ics_client, gremium="rat", limit=5)
    assert r["anzahl_gesamt"] == 20


def test_find_meetings_date_filter(ics_client: FakeClient, cfg: Config):
    r = find_meetings(cfg, ics_client, datum_von="2024-10-09", datum_bis="2024-10-09", limit=100)
    assert r["anzahl_gesamt"] == 1
    assert r["ergebnisse"][0]["gremium"] == "Rat"


def test_find_meetings_tops_url(ics_client: FakeClient, cfg: Config):
    r = find_meetings(cfg, ics_client, gremium="Rat", limit=5)
    e = r["ergebnisse"][0]
    assert e["tops_url"] and e["tops_url"].startswith(BASE + "/tops/?__=")
    assert re.fullmatch(r".*/tops/\?__=\S+", e["tops_url"])


def test_find_meetings_limit_cap(ics_client: FakeClient, cfg: Config):
    r = find_meetings(cfg, ics_client, limit=99999)
    assert len(r["ergebnisse"]) <= 100


def test_find_meetings_http_error(cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(404))
    with pytest.raises(RISHTTPError) as ei:
        find_meetings(cfg, fc)
    assert ei.value.status == 404


def test_find_meetings_events_without_dtskipped(ics_client: FakeClient, cfg: Config):
    # Synthese: Event ohne DTSTART darf nicht abcrashgen
    ics = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Ohne Datum\nEND:VEVENT\n"
        "BEGIN:VEVENT\nSUMMARY:Mit Datum\nDTSTART:20260101T090000\n"
        "DESCRIPTION:Link: " + BASE + "/tops/?__=TOKEN\nEND:VEVENT\nEND:VCALENDAR"
    )
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(200, text=ics))
    r = find_meetings(cfg, fc)
    assert r["anzahl_gesamt"] == 1
    assert r["ergebnisse"][0]["gremium"] == "Mit Datum"
    assert r["ergebnisse"][0]["tops_url"] == BASE + "/tops/?__=TOKEN"


def test_meetings_error_to_dict():
    e = MeetingsError("not_a_detail_page", "falsche URL")
    d = e.to_dict()
    assert d["error"] == "not_a_detail_page"
    assert d["tops"] == []
