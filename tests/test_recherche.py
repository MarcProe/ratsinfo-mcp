"""Tests für ratsinfo_mcp.recherche – Formular-Seeding, POST-Mechanik, HTML-Parsing.

Alles offline: echte Captured-HTMLs aus tests/fixtures, FakeClient als HTTP-Schicht.
"""

from __future__ import annotations

import pytest

from ratsinfo_mcp.config import Config
from ratsinfo_mcp.http_client import RISHTTPError
from ratsinfo_mcp.recherche import (
    ALL_DOKTYP,
    DOKTYP_VALUES,
    GremiumMapping,
    RechercheClient,
    RechercheHit,
    RechercheResult,
    SearchParams,
    TYP_VALUES,
    _abs,
    _attachment_name,
    _form_value,
)
from tests.conftest import FakeClient, FakeResponse

BASE = "https://ris.teststadt.de"


# --- Hilfsfunktionen ------------------------------------------------------------
def test_typ_values_complete():
    assert set(TYP_VALUES) == {"alle", "dokumente", "anlagen", "personen", "anfragen"}
    assert TYP_VALUES["alle"] == "-1"


def test_doktyp_values_complete():
    assert set(DOKTYP_VALUES) == {"vorlagen", "einladungen", "beschluesse", "niederschriften"}
    assert set(ALL_DOKTYP) == {"T", "E", "B", "N"}


def test_abs_url_resolution():
    assert _abs(BASE, "http://x/y") == "http://x/y"
    assert _abs(BASE, "/recherche") == BASE + "/recherche"
    assert _abs(BASE, "sdnetrim/x.pdf") == f"{BASE}/sdnetrim/x.pdf"


def test_form_value_extracts_hidden_field(recherche_form_html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(recherche_form_html, "html.parser")
    form = soup.find("form", id="rechercheForm")
    assert form is not None
    assert _form_value(form, "reqid")
    assert _form_value(form, "csrftoken")
    assert _form_value(form, "nicht_da") is None
    assert _form_value(None, "reqid") is None


def test_gremium_mapping_roundtrip():
    m = GremiumMapping()
    m.load({"1": "Rat", "2": "Schulausschuss", "3": "Sportausschuss"})
    assert m.name_to_id("Rat") == "1"
    assert m.name_to_id("rat") == "1"          # case-insensitive
    assert m.name_to_id("  Rat  ") == "1"      # whitespace-normalisiert
    assert m.name_to_id("Verwaltungsrat") is None
    assert m.id_to_name("2") == "Schulausschuss"
    assert m.all_names() == ["Rat", "Schulausschuss", "Sportausschuss"]


def test_attachment_name_from_aria():
    class FakeA:
        def find(self, *a, **k):
            return None

    a = FakeA()
    assert _attachment_name(a, "Anlage Produkt 0801 zum Vorgang 926 /XI. exportiert am 29.08.2024") == "Produkt 0801"
    assert _attachment_name(a, "") == ""


def test_attachment_name_from_hide_text():
    class FakeSpan:
        def get_text(self, *a, **k):
            return "Bericht_2024.pdf (exportiert: 01.03.2024)"

    class FakeA:
        def find(self, *a, **k):
            return FakeSpan()

    assert _attachment_name(FakeA(), "") == "Bericht_2024.pdf"


# --- RechercheClient: Seeding ---------------------------------------------------
class _FormOnlyClient(FakeClient):
    """Liefert nur das Formular (für Seeding-Tests)."""

    def __init__(self, form_html: str):
        super().__init__(handler=lambda m, u, **k: FakeResponse(200, text=form_html))
        self.form_html = form_html


def test_seed_form_reads_tokens(recherche_form_html):
    rec = RechercheClient(Config(), _FormOnlyClient(recherche_form_html))
    reqid, csrftoken = rec._seed_form()
    assert reqid and csrftoken
    # Gremien wurden mitgeloead (aus dem echten Formular)
    assert rec.gremien.all_names()


def test_seed_form_missing_form_raises():
    rec = RechercheClient(Config(), FakeClient(handler=lambda m, u, **k: FakeResponse(200, text="<html></html>")))
    with pytest.raises(RISHTTPError, match="reqid/csrftoken"):
        rec._seed_form()


def test_seed_form_http_error():
    rec = RechercheClient(Config(), FakeClient(handler=lambda m, u, **k: FakeResponse(503)))
    with pytest.raises(RISHTTPError) as ei:
        rec._seed_form()
    assert ei.value.status == 503


# --- RechercheClient: Parsing ----------------------------------------------------
def _client_with_results(results: str) -> RechercheClient:
    from tests.conftest import FIXTURES

    form_html = (FIXTURES / "recherche_page.html").read_text(encoding="utf-8")

    def handler(m, u, **k):
        if m == "GET":
            return FakeResponse(200, text=form_html)
        return FakeResponse(200, text=results)  # POST wird hier direkt als Ergebnis behandelt

    # Für Parsing-Tests ruft man _parse() direkt — Client nur für cfg.
    rec = RechercheClient(Config(), FakeClient(handler=handler))
    return rec


def test_parse_results_sample_html(results_html):
    rec = _client_with_results(results_html)
    p = SearchParams(terms="Teststadt", count=50)
    result = rec._parse(results_html, p)
    assert isinstance(result, RechercheResult)
    assert not result.keine_ergebnisse
    assert len(result.hits) > 0
    # Erste Zeile: alle Pflichtfelder vorhanden
    h = result.hits[0]
    assert h.typ in {"Dokumente", "Anlagen", "Personen", "Anfragen und News"}
    assert h.titel
    d = h.to_dict()
    for key in ("typ", "titel", "vorlage_kennung", "gremium", "datum",
                "sitzungstermin", "fundstelle", "pdf_url", "detail_url", "anhaenge"):
        assert key in d


def test_parse_results_has_fundstelle_with_mark(results_html):
    rec = _client_with_results(results_html)
    result = rec._parse(results_html, SearchParams(terms="Test", count=50))
    marked = [h for h in result.hits if h.fundstelle and "<mark>" in h.fundstelle]
    assert marked, "mindestens ein Treffer sollte <mark>-Fundstelle haben"


def test_parse_results_limit_capped(results_html):
    rec = _client_with_results(results_html)
    full = rec._parse(results_html, SearchParams(terms="x", count=100))
    capped = rec._parse(results_html, SearchParams(terms="x", count=3))
    assert len(capped.hits) == 3
    assert len(full.hits) >= len(capped.hits)


def test_parse_typ_filter_excludes_other_tables(results_html):
    rec = _client_with_results(results_html)
    only_dok = rec._parse(results_html, SearchParams(terms="x", typ="0", count=100))
    assert all(h.typ == "Dokumente" for h in only_dok.hits)


def test_parse_page_counts(results_html):
    rec = _client_with_results(results_html)
    result = rec._parse(results_html, SearchParams(terms="x", count=50))
    # Ergebnisseite hat "Seite 1 von Y" bei mind. einer Tabelle
    assert any(v is not None for v in result.total_seiten_pro_typ.values())


def test_parse_no_results():
    rec = _client_with_results("x")
    result = rec._parse(
        "<html><body>keine Ergebnisse gefunden<table id='table0'></table></body></html>",
        SearchParams(terms="zzzz"),
    )
    assert result.keine_ergebnisse
    assert result.hits == []


def test_parse_form_invalid_raises():
    rec = _client_with_results("x")
    with pytest.raises(RISHTTPError, match="nicht mehr gültig"):
        rec._parse("<html>Das Formular ist nicht mehr gültig.</html>", SearchParams())


def test_parse_row_ignores_empty_rows():
    rec = _client_with_results("x")
    html = "<html><table id='table0'><tr class='row-0'><td></td></tr></table></html>"
    result = rec._parse(html, SearchParams())
    assert result.hits == []


def test_datum_extraction():
    rec = _client_with_results("x")
    html = ("<html><table id='table0'><tr class='row-0'>"
            "<span class='search_result_subject'>Titel</span> Teststadt, den 24.03.2026 "
            "</tr></table></html>")
    result = rec._parse(html, SearchParams())
    assert result.hits[0].datum == "24.03.2026"


def test_pdf_url_and_vorgang_extraction():
    rec = _client_with_results("x")
    html = (
        "<html><table id='table0'><tr class='row-0'>"
        "<a href='/sdnetrim/AA/x.pdf' aria-label='Antrag GRÜNE XII. A 1 /XII.-GRÜNE im PDF-Format öffnen'>pdf</a> "
        "<a href='/vorgang/?__=BB' aria-label='Antrag GRÜNE XII. A 1 /XII.-GRÜNE - Vorgang. Vorgang öffnen'>vorgang</a> "
        "<span class='search_result_subject'>Titel</span>"
        "</tr></table></html>"
    )
    result = rec._parse(html, SearchParams())
    h = result.hits[0]
    assert h.pdf_url == f"{BASE}/sdnetrim/AA/x.pdf"
    assert h.detail_url == f"{BASE}/vorgang/?__=BB"
    assert h.vorlage_kennung == "Antrag GRÜNE XII. A 1 /XII.-GRÜNE"


def test_sitzung_aria_extraction():
    rec = _client_with_results("x")
    html = (
        "<html><table id='table0'><tr class='row-0'>"
        "<a href='/tops/?__=CC' aria-label='Sitzung am 09.10.2024 des Gremiums Rat anzeigen'>sitzung</a> "
        "<span class='search_result_subject'>Titel</span>"
        "</tr></table></html>"
    )
    result = rec._parse(html, SearchParams())
    h = result.hits[0]
    assert h.sitzungstermin == "09.10.2024"
    assert h.gremium == "Rat"


def test_multiple_pdfs_first_is_main_rest_anhaenge():
    rec = _client_with_results("x")
    html = (
        "<html><table id='table0'><tr class='row-0'>"
        "<a href='/sdnetrim/AA/main.pdf' aria-label='Drucksache XI. 954 im PDF-Format öffnen'>m</a> "
        "<a href='/sdnetrim/AA/anlage1.pdf' aria-label='Anlage Bericht zum Vorgang 954 exportiert'>a1</a> "
        "<a href='/sdnetrim/AA/anlage2.pdf'>a2</a> "
        "<span class='search_result_subject'>Titel</span>"
        "</tr></table></html>"
    )
    result = rec._parse(html, SearchParams())
    h = result.hits[0]
    assert h.pdf_url.endswith("main.pdf")
    assert len(h.anhaenge) == 2
    assert h.anhaenge[0]["url"].endswith("anlage1.pdf")
    assert h.anhaenge[0]["name"] == "Bericht"


def test_result_to_dict_shape(results_html):
    rec = _client_with_results(results_html)
    d = rec._parse(results_html, SearchParams(terms="q", count=5)).to_dict()
    assert set(d) == {
        "source", "query", "total_seiten_pro_typ", "total_ergebnisse",
        "current_page", "keine_ergebnisse", "treffer",
    }
    assert d["query"] == "q"
    assert d["total_ergebnisse"] is None  # exakte Zahl nicht isoliert verfügbar


def test_hit_to_dict_shape():
    h = RechercheHit(typ="Dokumente", titel="t")
    d = h.to_dict()
    assert d["typ"] == "Dokumente"
    assert d["anhaenge"] == []
    assert d["datum"] is None
