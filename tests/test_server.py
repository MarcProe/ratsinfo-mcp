"""Tests für ratsinfo_mcp.server – Tool-Registry, Parameter-Validierung, Fehler-Mapping.

Die Tool-Funktionen werden direkt aufgerufen (synchronous); die HTTP-Schicht
wird per ``monkeypatch`` auf ``server.HttpClient`` gegen FakeClient getauscht.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ratsinfo_mcp import server
from tests.conftest import FIXTURES, FakeClient, FakeResponse

EXPECTED_TOOLS = {
    "recherche", "pdf_as_markdown", "meeting_documents", "find_meetings",
    "terms_of_use", "find_person", "person_steckbrief", "committee_members",
}

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
    assert {"name", "fraktion", "gremium", "limit"} <= props["find_person"]
    assert {"name", "oparl_id", "format", "max_chars"} <= props["person_steckbrief"]
    assert "gremium" in props["committee_members"]
    # Pflichtparameter: pdf_url / detail_url / find_person.name / committee_members.gremium
    assert "pdf_url" in tools["pdf_as_markdown"].inputSchema.get("required", [])
    assert "detail_url" in tools["meeting_documents"].inputSchema.get("required", [])
    assert "name" in tools["find_person"].inputSchema.get("required", [])
    assert "gremium" in tools["committee_members"].inputSchema.get("required", [])


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


# --- Personensuche: Tool-Ebene ---------------------------------------------------------------
# OParl-Personen-/Gremien-Fixtures (fiktive Teststadt) – identisch zu test_oparl.py.
OPARL_PERSON_LIST = (
    json.loads((FIXTURES / "oparl_person_list.json").read_text())
)
OPARL_COMMITTEES = {
    "data": [
        {"id": 11, "name": "Rat"},
        {"id": 12, "name": "Schulausschuss"},
        {"id": 13, "name": "Finanzausschuss"},
    ],
    "pagination": {"page": 1, "totalPages": 1},
}
OPARL_COMMITTEE_MEMBERS = {
    "data": [
        {"id": "/body/1/person/101", "name": "Dr. Max Mustermann",
         "functionName": None, "termOfElection": ["2020-10-01", "2025-09-30"],
         "organizationIds": [{"id": 5, "name": "CDU"}]},
        {"id": "/body/1/person/104", "name": "Petra Beispiel",
         "functionName": None, "termOfElection": ["2020-10-01", None],
         "organizationIds": [{"id": 7, "name": "SPD"}]},
    ],
    "pagination": {"page": 1, "totalPages": 1},
}


def _oparl_persons_handler(m, u, **k):
    """OParl-Handler: Personen-/Gremien-Endpoints der fiktiven Teststadt."""
    if u.endswith("/body/1"):
        return FakeResponse(200, json_data={"fullName": "Stadt Teststadt"})
    if "/person" in u and "/committee/" not in u:
        return FakeResponse(200, json_data=OPARL_PERSON_LIST)
    if u.endswith("/body/1/committee"):
        return FakeResponse(200, json_data=OPARL_COMMITTEES)
    if "/committee/12/person" in u:
        return FakeResponse(200, json_data=OPARL_COMMITTEE_MEMBERS)
    return FakeResponse(404, text="")


def test_find_person_oparl_hit(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.find_person(name="Mustermann")
    assert "error" not in r
    assert r["anzahl_gesamt"] == 1
    hit = r["treffer"][0]
    assert hit["name"] == "Dr. Max Mustermann"
    assert hit["fraktion"] == "CDU"
    assert hit["quelle"] == "oparl"
    assert hit["oparl_url"].endswith("/body/1/person/101")
    assert "noten" in r and "Datenminimierung" in r["noten"]
    # OParl-Treffer → keine HTML-Recherche nachgeladen (GET /recherche fehlt).
    assert all("/recherche" not in c[1] for c in fc.calls)


def test_find_person_empty_name(monkeypatch: pytest.MonkeyPatch):
    r = server.find_person(name="   ")
    assert r["error"] == "invalid_parameter"


def test_find_person_limit_clamped(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.find_person(name="Beispiel", limit=9999)
    assert r["anzahl_gesamt"] <= 50
    assert len(r["treffer"]) <= 50


def test_find_person_html_fallback_when_oparl_empty(
    monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient
):
    """OParl kennt die Person nicht → HTML-Personenindex als Komplement."""
    state = {"oparl_empty": True}

    def handler(m, u, **k):
        if "/person" in u and "/committee/" not in u:
            # OParl kennt die Person nicht → leere Liste.
            return FakeResponse(200, json_data={"data": [], "pagination": {"page": 1, "totalPages": 0}})
        # Sonst: Recherche-Sequenz (Formular → 302 → Ergebnisse).
        return recherche_seq.handler(m, u, **k)

    fc = FakeClient(handler=handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.find_person(name="Ackermann")
    assert "error" not in r
    assert r["anzahl_gesamt"] >= 1
    hit = r["treffer"][0]
    assert hit["quelle"] == "html"
    assert hit["name"] == "Herr Lutz Ackermann"
    assert hit["fraktion"] == "Sachkundiger Bürger"
    assert hit["personen_url"] and "/personen/" in hit["personen_url"]
    # Adresse wird bewusst nicht mitgeliefert.
    assert "Kuhstraße" not in json.dumps(r, ensure_ascii=False)


def test_find_person_oparl_down_uses_html(
    monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient
):
    """OParl nicht erreichbar → HTML-Quelle, oparl_verfügbar=False."""
    def handler(m, u, **k):
        if "/person" in u:
            return FakeResponse(503)
        return recherche_seq.handler(m, u, **k)

    fc = FakeClient(handler=handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.find_person(name="Ackermann")
    assert "error" not in r
    assert r["oparl_verfügbar"] is False
    assert r["treffer"] and r["treffer"][0]["quelle"] == "html"


# --- person_steckbrief: Tool-Ebene ---------------------------------------------------------
def test_steckbrief_by_oparl_id_markdown(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.person_steckbrief(oparl_id="/body/1/person/101")
    assert "error" not in r
    assert r["format"] == "markdown"
    assert "## Steckbrief · Dr. Max Mustermann" in r["markdown"]
    assert "| Fraktion | CDU |" in r["markdown"]
    assert r["oparl_id"] == "/body/1/person/101"
    assert r["quelle"] == "oparl"


def test_steckbrief_by_oparl_id_html(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.person_steckbrief(oparl_id="/body/1/person/102", format="html")
    assert "error" not in r
    assert r["format"] == "html"
    assert "Anna Müller" in r["html"]


def test_steckbrief_by_name_unique(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.person_steckbrief(name="Mustermann")
    assert "error" not in r
    assert r["name"] == "Dr. Max Mustermann"


def test_steckbrief_ambiguous_returns_candidates(monkeypatch: pytest.MonkeyPatch):
    """„Beispiel“ → 2 OParl-Treffer → mehrfache_treffer statt Karte."""
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.person_steckbrief(name="Beispiel")
    assert r["error"] == "mehrfache_treffer"
    assert len(r["kandidaten"]) == 2
    assert all("oparl_id" in k for k in r["kandidaten"])


def test_steckbrief_unknown_oparl_id(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.person_steckbrief(oparl_id="/body/1/person/999")
    assert r["error"] == "unknown_person"


def test_steckbrief_no_params(monkeypatch: pytest.MonkeyPatch):
    r = server.person_steckbrief()
    assert r["error"] == "invalid_parameter"


def test_steckbrief_invalid_format(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.person_steckbrief(oparl_id="/body/1/person/101", format="pdf")
    assert r["error"] == "invalid_parameter"


# --- committee_members: Tool-Ebene -----------------------------------------------------------
def test_committee_members_tool(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.committee_members(gremium="Schulausschuss")
    assert "error" not in r
    assert r["gremium"] == "Schulausschuss"
    assert r["gremium_id"] == 12
    assert r["anzahl_gesamt"] == 2
    assert r["mitglieder"][0]["personenkreis"] == "CDU"


def test_committee_members_substring(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.committee_members(gremium="Schul")
    assert "error" not in r
    assert r["gremium"] == "Schulausschuss"


def test_committee_members_unknown(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=_oparl_persons_handler)
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.committee_members(gremium="Quatschausschuss")
    assert r["error"] == "unknown_gremium"


def test_committee_members_empty_name(monkeypatch: pytest.MonkeyPatch):
    r = server.committee_members(gremium="  ")
    assert r["error"] == "invalid_parameter"


def test_committee_members_http_error(monkeypatch: pytest.MonkeyPatch):
    fc = FakeClient(handler=lambda m, u, **k: FakeResponse(500))
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: fc)
    r = server.committee_members(gremium="Rat")
    # Gremien-Index (best effort) leer → unknown_gremium statt Crash.
    assert r["error"] in {"unknown_gremium", "http_error"}


# --- recherche: Personenkreis je Treffer ------------------------------------------------------
def test_recherche_personenkreis_extracted(
    monkeypatch: pytest.MonkeyPatch, recherche_seq: FakeClient,
    monkeypatch_env_no_oparl: None,
):
    monkeypatch.setattr(server, "HttpClient", lambda cfg, **kw: recherche_seq)
    r = server.recherche(volltext="Teststadt", dokumenttyp="personen", limit=5)
    assert "error" not in r
    person_hits = [t for t in r["treffer"] if t["typ"] == "Personen"]
    assert person_hits, "results_sample.html hat Personen-Treffer"
    first = person_hits[0]
    assert first["personenkreis"] == "Sachkundiger Bürger"
    assert first["detail_url"] and "/personen/" in first["detail_url"]
    # Adresse wird bewusst nicht mitgeliefert (Datenminimierung).
    assert "Kuhstraße" not in json.dumps(r, ensure_ascii=False)


# --- interne Fehler-Helfer ------------------------------------------------------------------------
def test_tool_error_shape():
    d = server._tool_error("invalid_parameter", "x fehlt")
    assert d == {"error": "invalid_parameter", "message": "x fehlt", "treffer": []}


# --- Fixtures --------------------------------------------------------------------------------------
@pytest.fixture
def monkeypatch_env_no_oparl(monkeypatch: pytest.MonkeyPatch) -> None:
    """OParl-Anreicherung im Test garantiert aus (sonst 8x OParl-Requests)."""
    monkeypatch.delenv("OPARL_ENRICH", raising=False)
