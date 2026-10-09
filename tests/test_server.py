"""Tests für ratsinfo_mcp.server – Tool-Registry, Parameter-Validierung, Fehler-Mapping.

Die Tool-Funktionen werden direkt aufgerufen (synchronous); die HTTP-Schicht
wird per ``monkeypatch`` auf ``server.HttpClient`` gegen FakeClient getauscht.
"""

from __future__ import annotations

import asyncio

import pytest

from ratsinfo_mcp import server
from tests.conftest import FakeClient, FakeResponse

EXPECTED_TOOLS = {"recherche", "pdf_as_markdown", "meeting_documents", "find_meetings", "terms_of_use"}

TOPS_URL = "https://ris.teststadt.de/tops/?__=TOKEN"
VORGANG_URL = "https://ris.teststadt.de/vorgang/?__=TOKEN"
PDF_URL = "https://ris.teststadt.de/sdnetrim/AAA/doc.pdf"


# --- Tool-Registry ---------------------------------------------------------------------
def test_four_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS


def test_tool_schemas_have_expected_params():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = {n: set(t.inputSchema.get("properties", {})) for n, t in tools.items()}
    assert "pdf_url" in props["pdf_as_markdown"]
    assert "detail_url" in props["meeting_documents"]
    assert {"gremium", "datum_von", "datum_bis", "limit"} <= props["find_meetings"]
    assert "volltext" in props["recherche"]
    # Pflichtparameter: pdf_url / detail_url sind required
    assert "pdf_url" in tools["pdf_as_markdown"].inputSchema.get("required", [])
    assert "detail_url" in tools["meeting_documents"].inputSchema.get("required", [])


def test_tool_docstrings_not_empty():
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        assert (t.description or "").strip(), f"Tool {t.name} ohne Beschreibung"


# --- Recherche: Parameter-Validierung (ohne HTTP) -----------------------------------------
@pytest.mark.parametrize("dt", ["alle", None, "DOKUMENTE", "Anfragen News"])
def test_recherche_dokumenttyp_valid(dt):
    assert server._resolve_dokumenttyp(dt) in {"alle", "dokumente", "anlagen", "personen", "anfragen"}


def test_recherche_dokumenttyp_invalid():
    with pytest.raises(ValueError):
        server._resolve_dokumenttyp("kuchen")


@pytest.mark.parametrize("arten,expected", [
    (None, ["vorlagen", "einladungen", "beschluesse", "niederschriften"]),
    (["vorlagen"], ["vorlagen"]),
    (["Beschlüsse", "Niederschriften"], ["beschluesse", "niederschriften"]),
    ([], ["vorlagen", "einladungen", "beschluesse", "niederschriften"]),
])
def test_recherche_arten(arten, expected):
    assert server._resolve_arten(arten) == expected


def test_recherche_arten_invalid():
    with pytest.raises(ValueError):
        server._resolve_arten(["kuchen"])


@pytest.mark.parametrize("val,expected", [
    (None, ""), ("", ""), ("  2026-03-01  ", "2026-03-01"),
])
def test_validate_date_ok(val, expected):
    assert server._validate_date(val, "datum_von") == expected


@pytest.mark.parametrize("val", ["2026-3-1", "01.03.2026", "2026/03/01", "abc"])
def test_validate_date_bad(val):
    with pytest.raises(ValueError):
        server._validate_date(val, "datum_von")


# --- Recherche: End-to-End mit FakeClient ---------------------------------------------------
def test_recherche_tool_success(monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient,
                               monkeypatch_env_no_oparl: None):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: recherche_seq)
    r = server.recherche(volltext="Teststadt", limit=5)
    assert "error" not in r
    assert r["keine_ergebnisse"] is False
    assert len(r["treffer"]) >= 1
    assert r["gremium_gefragt"] is None
    assert "noten" in r


def test_recherche_tool_invalid_dokumenttyp(monkeypatch: pytest.MonkeyPatch):
    r = server.recherche(volltext="x", dokumenttyp="kuchen")
    assert r["error"] == "invalid_parameter"
    assert "dokumenttyp" in r["message"].lower() or "Unbekanntes" in r["message"]


def test_recherche_tool_invalid_date(monkeypatch: pytest.MonkeyPatch):
    r = server.recherche(volltext="x", datum_von="01.03.2026")
    assert r["error"] == "invalid_parameter"


def test_recherche_tool_limit_clamped(monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient,
                                      monkeypatch_env_no_oparl: None):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: recherche_seq)
    r = server.recherche(volltext="Teststadt", limit=9999)
    assert len(r["treffer"]) <= 100


def test_recherche_tool_http_error(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(503))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.recherche(volltext="x")
    assert r["error"] == "http_error"
    assert r["status"] == 503


def test_recherche_tool_unknown_gremium(monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient,
                                        monkeypatch_env_no_oparl: None):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: recherche_seq)
    r = server.recherche(volltext="x", gremium="Quatschausschuss")
    assert r["error"] == "unknown_gremium"
    assert "Quatschausschuss" in r["message"]


def test_recherche_tool_known_gremium(monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient,
                                      monkeypatch_env_no_oparl: None):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: recherche_seq)
    r = server.recherche(volltext="Teststadt", gremium="Rat", limit=3)
    assert "error" not in r
    assert r["gremium_gefragt"] == "Rat"


# --- pdf_as_markdown: Tool-Ebene ------------------------------------------------------------
def test_pdf_tool_success(monkeypatch: pytest.MonkeyPatch, small_pdf_bytes: bytes):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(
        200, content=small_pdf_bytes, headers={"content-type": "application/pdf"}))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.pdf_as_markdown(pdf_url=PDF_URL, seiten="1")
    assert "error" not in r
    assert r["seiten_gesamt"] == 2
    assert r["seiten_geiefert"] == [1]
    assert "## Seite 1" in r["markdown"]


def test_pdf_tool_max_chars_nonnumeric(monkeypatch: pytest.MonkeyPatch):
    r = server.pdf_as_markdown(pdf_url=PDF_URL, max_chars="viel")
    assert r["error"] == "invalid_parameter"


def test_pdf_tool_forbidden_url(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient()
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.pdf_as_markdown(pdf_url="https://evil.example.com/x.pdf")
    assert r["error"] == "forbidden_url"
    assert fc.calls == []


def test_pdf_tool_http_error(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(404))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.pdf_as_markdown(pdf_url=PDF_URL)
    assert r["error"] == "http_error"
    assert r["status"] == 404


# --- meeting_documents: Tool-Ebene ------------------------------------------------------------
def test_meeting_docs_tool_tops(monkeypatch: pytest.MonkeyPatch, tops_client: FakeClient):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: tops_client)
    r = server.meeting_documents(detail_url=TOPS_URL)
    assert "error" not in r
    assert r["typ"] == "sitzung"
    assert r["n_pdfs"] == 76
    assert len(r["tops"]) == 44


def test_meeting_docs_tool_vorgang(monkeypatch: pytest.MonkeyPatch, vorgang_client: FakeClient):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: vorgang_client)
    r = server.meeting_documents(detail_url=VORGANG_URL)
    assert r["typ"] == "vorgang"
    assert r["n_pdfs"] == 4


def test_meeting_docs_tool_not_detail_page(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(200, text="<html></html>"))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.meeting_documents(detail_url="https://ris.teststadt.de/recherche")
    assert r["error"] == "not_a_detail_page"


def test_meeting_docs_tool_http_error(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(500))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.meeting_documents(detail_url=TOPS_URL)
    assert r["error"] == "http_error"


# --- find_meetings: Tool-Ebene ------------------------------------------------------------------
def test_find_tool_success(monkeypatch: pytest.MonkeyPatch, ics_client: FakeClient):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: ics_client)
    r = server.find_meetings(gremium="Rat", limit=5)
    assert "error" not in r
    assert r["anzahl_gesamt"] == 20
    assert len(r["ergebnisse"]) == 5
    assert all(e["gremium"] == "Rat" for e in r["ergebnisse"])


def test_find_tool_invalid_date(monkeypatch: pytest.MonkeyPatch):
    r = server.find_meetings(datum_von="01.03.2026")
    assert r["error"] == "invalid_parameter"


def test_find_tool_http_error(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(502))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.find_meetings()
    assert r["error"] == "http_error"
    assert r["status"] == 502


# --- interne Fehler-Helfer ------------------------------------------------------------------------
def test_tool_error_shape():
    d = server._tool_error("invalid_parameter", "x fehlt")
    assert d == {"error": "invalid_parameter", "message": "x fehlt", "treffer": []}


# --- Fixtures --------------------------------------------------------------------------------------
@pytest.fixture
def monkeypatch_env_no_oparl(monkeypatch: pytest.MonkeyPatch) -> None:
    """OParl-Anreicherung im Test garantiert aus (sonst 8x OParl-Requests)."""
    monkeypatch.delenv("OPARL_ENRICH", raising=False)
