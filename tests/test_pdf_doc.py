"""Tests für ratsinfo_mcp.pdf_doc – Guards, URL-Whitelist, Markdown, Seiten/Suche/Cache.

Offline: Mini-PDFs aus tests/fixtures, FakeClient als Download-Schicht.
"""

from __future__ import annotations

import pytest

from ratsinfo_mcp.config import Config
from ratsinfo_mcp.http_client import RISHTTPError
from ratsinfo_mcp.pdf_doc import (
    PDFDocumentError,
    _parse_seiten,
    _render_markdown,
    fetch_pdf_markdown,
)
from tests.conftest import PDF_URL, PDF_URL_FRAG, FakeClient, FakeResponse, pdf_response


# --- _parse_seiten ------------------------------------------------------------------
@pytest.mark.parametrize(
    "spec,expected",
    [
        (None, None),
        ("", None),
        ([], None),
        ("5", [5]),
        ("12-18", [12, 13, 14, 15, 16, 17, 18]),
        ("3, 7, 42", [3, 7, 42]),
        ("2-4, 8", [2, 3, 4, 8]),
        ([3, 1, 2], [1, 2, 3]),
    ],
)
def test_parse_seiten_valid(spec, expected):
    assert _parse_seiten(spec) == expected


def test_parse_seiten_list_of_strings_invalid():
    # Nur int-Liste oder String; String-Elemente in einer Liste sind kein Spec.
    with pytest.raises(PDFDocumentError):
        _parse_seiten(["1-2"])


@pytest.mark.parametrize("spec", ["abc", "0", "5-2", [0], ["x"], [-1]])
def test_parse_seiten_invalid(spec):
    with pytest.raises(PDFDocumentError) as ei:
        _parse_seiten(spec)
    assert ei.value.code == "invalid_parameter"


# --- fetch_pdf_markdown: Grundpfad ----------------------------------------------------
def test_basic_fetch(small_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(small_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert r["pdf_url"] == PDF_URL
    assert r["seiten_gesamt"] == 2
    assert r["seiten_geiefert"] == [1, 2]
    assert r["gekürzt"] is False
    assert r["zeichen_geiefert"] == r["zeichen_gesamt"]
    md = r["markdown"]
    assert md.startswith("<!-- PDF:")
    assert "## Seite 1" in md and "## Seite 2" in md
    assert "Kaeltschutzplan" in md
    assert len(fc.calls) == 1


def test_fragment_stripped(small_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(small_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL_FRAG)
    assert r["pdf_url"] == PDF_URL  # Fragment entfernt
    assert fc.calls[0][1] == PDF_URL


def test_relative_url_uses_base(small_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(small_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, "/sdnetrim/AAA/x.pdf")
    assert r["pdf_url"] == "https://ris.teststadt.de/sdnetrim/AAA/x.pdf"


def test_no_suchbegriff_no_fundstellen_keys(small_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(small_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert "fundstellen_seiten" not in r
    assert "kein_treffer" not in r


# --- Seiten-Auswahl --------------------------------------------------------------------
def test_seiten_subset(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, seiten="10-12")
    assert r["seiten_gesamt"] == 30
    assert r["seiten_geiefert"] == [10, 11, 12]
    assert "## Seite 10" in r["markdown"]
    assert "## Seite 13" not in r["markdown"]
    assert "## Seite 9" not in r["markdown"]


def test_seiten_list_and_missing(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, seiten=[2, 99])
    assert r["seiten_geiefert"] == [2]
    assert r["seiten_fehlt"] == [99]


def test_seiten_out_of_range(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, seiten="31-40")
    assert r["seiten_geiefert"] == []
    assert r["seiten_fehlt"] == [31, 32, 33, 34, 35, 36, 37, 38, 39, 40]


# --- Suchbegriff ------------------------------------------------------------------------
def test_suchbegriff_fundstellen(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, suchbegriff="KAEUTESCHUTZPLAN")
    assert r["kein_treffer"] is False
    assert r["fundstellen_seiten"] == [6]
    assert r["seiten_geiefert"] == [6]
    assert "## Seite 6" in r["markdown"]
    assert "## Seite 7" not in r["markdown"]


def test_suchbegriff_case_insensitive(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, suchbegriff="kaeuteschutzplan")
    assert r["fundstellen_seiten"] == [6]


def test_suchbegriff_kein_treffer(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, suchbegriff="GARNICHTDA")
    assert r["kein_treffer"] is True
    assert r["fundstellen_seiten"] == []
    assert r["markdown"] == ""
    assert "GARNICHTDA" in r["notiz"]


# --- max_chars-Cutoff -------------------------------------------------------------------
def test_max_chars_cuts(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    full = fetch_pdf_markdown(cfg, fc, PDF_URL)
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, max_chars=1500)
    assert r["gekürzt"] is True
    assert len(r["markdown"]) <= 1500 + len(full["markdown"].split("## Seite 1")[0]) + 5
    assert r["zeichen_geiefert"] < r["zeichen_gesamt"]
    # Die Seite, in der der Cut liegt, zählt als (teilweise) geliefert
    assert r["seiten_geiefert"] and r["seiten_geiefert"][0] == 1


def test_max_chars_floor_applies(small_pdf_bytes: bytes, cfg: Config):
    # max_chars < 200 wird auf 200 angehoben
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(small_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL, max_chars=10)
    assert r["zeichen_geiefert"] >= 100  # mindestens der Cap-Boden greift (200, abzgl. Header-Überhang)


# --- Cache ---------------------------------------------------------------------------------
def test_cache_hit_avoids_download(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert len(fc.calls) == 1
    r2 = fetch_pdf_markdown(cfg, fc, PDF_URL, seiten="5-7")
    assert len(fc.calls) == 1  # kein zweiter Download
    assert r2["seiten_geiefert"] == [5, 6, 7]  # aber Selektion greift trotzdem


def test_cache_different_url_downloads_again(big_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(big_pdf_bytes))
    fetch_pdf_markdown(cfg, fc, PDF_URL)
    fetch_pdf_markdown(cfg, fc, "https://ris.teststadt.de/sdnetrim/OTHER/x.pdf")
    assert len(fc.calls) == 2


# --- Guards ---------------------------------------------------------------------------------
def test_forbidden_host(cfg: Config):
    fc = FakeClient()
    with pytest.raises(PDFDocumentError) as ei:
        fetch_pdf_markdown(cfg, fc, "https://evil.example.com/x.pdf")
    assert ei.value.code == "forbidden_url"
    assert fc.calls == []


def test_subdomain_not_allowed(cfg: Config):
    fc = FakeClient()
    with pytest.raises(PDFDocumentError) as ei:
        fetch_pdf_markdown(cfg, fc, "https://evil.ris.teststadt.de/x.pdf")
    assert ei.value.code == "forbidden_url"


def test_empty_url(cfg: Config):
    fc = FakeClient()
    with pytest.raises(PDFDocumentError) as ei:
        fetch_pdf_markdown(cfg, fc, "")
    assert ei.value.code == "invalid_parameter"


def test_not_a_pdf(cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(200, text="<html>"))
    with pytest.raises(PDFDocumentError) as ei:
        fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert ei.value.code == "not_a_pdf"


def test_http_error_propagates(cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(404))
    with pytest.raises(RISHTTPError) as ei:
        fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert ei.value.status == 404


def test_too_large_by_content_length(cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(
        b"%PDF", headers={"content-length": str(cfg.pdf_max_bytes + 1)}))
    with pytest.raises(PDFDocumentError) as ei:
        fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert ei.value.code == "too_large"


def test_garbage_bytes_parse_error(cfg: Config):
    # b"%PDF" ohne gültige Struktur → Parse-Fehler (kein Traceback).
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(b"%PDF"))
    with pytest.raises(PDFDocumentError) as ei:
        fetch_pdf_markdown(cfg, fc, PDF_URL)
    assert ei.value.code in {"parse_error", "no_text"}


def test_render_too_many_pages(cfg: Config, monkeypatch: pytest.MonkeyPatch):
    import pypdf

    # Erzeuge PDF mit > Limit-Seiten (Limit runterschrauben statt 501 Seiten bauen)
    monkeypatch.setattr(cfg, "pdf_max_seiten", 5)
    big_pdf = __import__("pathlib").Path("tests/fixtures/big_30pages.pdf").read_bytes()
    with pytest.raises(PDFDocumentError) as ei:
        _render_markdown(big_pdf, cfg, PDF_URL)
    assert ei.value.code == "too_many_pages"


def test_render_invalid_pdf(cfg: Config):
    with pytest.raises(PDFDocumentError) as ei:
        _render_markdown(b"das ist kein pdf", cfg, PDF_URL)
    assert ei.value.code == "parse_error"


def test_error_to_dict():
    e = PDFDocumentError("too_large", "zu groß")
    d = e.to_dict()
    assert d == {"error": "too_large", "message": "zu groß", "markdown": ""}


# --- Markdown-Qualität ---------------------------------------------------------------------
def test_page_text_normalized(small_pdf_bytes: bytes, cfg: Config):
    fc = FakeClient(handler=lambda m, u, **k: pdf_response(small_pdf_bytes))
    r = fetch_pdf_markdown(cfg, fc, PDF_URL)
    # Keine Zeile mit trailing Whitespace, keine doppelten Leerzeilen
    assert "\n\n\n" not in r["markdown"]
    for line in r["markdown"].split("\n"):
        assert line == line.rstrip()
