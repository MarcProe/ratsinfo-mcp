"""Tests für ratsinfo_mcp.oparl – Reference-Extraction, Paper-Index, Enrichment.

Offline: FakeClient liefert OParl-JSON aus tests/fixtures/oparl_paper_list.json.
"""

from __future__ import annotations

import json

import pytest

from ratsinfo_mcp.config import Config
from ratsinfo_mcp.http_client import RISHTTPError
from ratsinfo_mcp.oparl import OParlClient, _enrich_enabled, _max_pages, extract_references
from tests.conftest import FIXTURES, FakeClient, FakeResponse


# --- Reference-Extraction ---------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Drucksache XI. 954 /XI.", {"954 /XI."}),
        ("Antrag XII. A 1 /XII.-GRÜNE", {"A 1 /XII.-GRÜNE"}),
        ("Keine Vorlage hier", set()),
        ("", set()),
        ("zwei: 954 /XI. und HA 4-2025/XI.-B", {"954 /XI.", "HA 4-2025/XI.-B"}),
    ],
)
def test_extract_references(text, expected):
    assert set(extract_references(text)) == expected


def test_extract_references_none():
    assert extract_references(None) == []


# --- Enrich-Schalter / Max-Pages ----------------------------------------------------
def test_enrich_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPARL_ENRICH", raising=False)
    assert _enrich_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE"])
def test_enrich_enabled_values(monkeypatch: pytest.MonkeyPatch, val):
    monkeypatch.setenv("OPARL_ENRICH", val)
    assert _enrich_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no"])
def test_enrich_disabled_values(monkeypatch: pytest.MonkeyPatch, val):
    monkeypatch.setenv("OPARL_ENRICH", val)
    assert _enrich_enabled() is False


def test_max_pages(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPARL_MAX_PAGES", raising=False)
    assert _max_pages() == 8
    monkeypatch.setenv("OPARL_MAX_PAGES", "3")
    assert _max_pages() == 3
    monkeypatch.setenv("OPARL_MAX_PAGES", "0")
    assert _max_pages() == 1  # Mindestwert
    monkeypatch.setenv("OPARL_MAX_PAGES", "xyz")
    assert _max_pages() == 8  # Fallback bei ungültigem Wert


# --- OParlClient --------------------------------------------------------------------
@pytest.fixture
def oparl_client(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> OParlClient:
    paper_list = json.loads((FIXTURES / "oparl_paper_list.json").read_text())

    def handler(m, u, **k):
        if u.endswith("/body/1"):
            return FakeResponse(200, json_data={"fullName": "Stadt Teststadt"})
        if u.endswith("/body/1/organization"):
            return FakeResponse(200, json_data={"data": []})
        if "/paper" in u:
            return FakeResponse(200, json_data=paper_list)
        return FakeResponse(404, text="")

    return OParlClient(cfg, FakeClient(handler=handler))


def test_body_info_cached(oparl_client):
    assert oparl_client.body_info()["fullName"] == "Stadt Teststadt"
    calls = oparl_client.client.calls
    body1_calls = [c for c in calls if c[1].endswith("/body/1")]
    assert len(body1_calls) == 1  # hart gecacht


def test_paper_by_reference_exact(oparl_client):
    assert oparl_client.paper_by_reference("954 /XI.") is not None


def test_paper_by_reference_normalized(oparl_client):
    # Punkte/Leerzeichen ignoriert, case-insensitiv
    assert oparl_client.paper_by_reference("954/XI") is not None
    assert oparl_client.paper_by_reference("954  /XI") is not None


def test_paper_by_reference_missing(oparl_client):
    assert oparl_client.paper_by_reference("1 /I.") is None
    assert oparl_client.paper_by_reference("") is None


def test_enrich_returns_mainfile(oparl_client):
    meta = oparl_client.enrich("954 /XI.")
    assert meta is not None
    assert meta["reference"] == "954 /XI."
    assert meta["paper_type"] == "Vorlage"
    assert meta["main_file_url"].endswith("Drucksache.pdf")
    assert meta["main_file_name"] == "Drucksache.pdf"
    assert meta["oparl_id"] == "/body/1/paper/954"


def test_enrich_hit_disabled_by_default(monkeypatch: pytest.MonkeyPatch, oparl_client):
    monkeypatch.delenv("OPARL_ENRICH", raising=False)
    assert oparl_client.enrich_hit("Antrag A 1 /XII.-GRÜNE", "Titel") is None


def test_enrich_hit_when_enabled(monkeypatch: pytest.MonkeyPatch, oparl_client):
    monkeypatch.setenv("OPARL_ENRICH", "1")
    meta = oparl_client.enrich_hit("Drucksache XI. 954 /XI.", "irgendein Titel")
    assert meta is not None
    assert meta["reference"] == "954 /XI."


def test_enrich_hit_no_candidate_returns_none(monkeypatch: pytest.MonkeyPatch, oparl_client):
    monkeypatch.setenv("OPARL_ENRICH", "1")
    assert oparl_client.enrich_hit("keine nummer", "auch keine") is None


def test_organizations_http_error_returns_empty_dict(oparl_client):
    oparl_client.client.calls.clear()

    def boom(m, u, **k):
        return FakeResponse(500)

    oparl_client.client.handler = boom
    # organizations() soll bei HTTP-Fehler leeres dict liefern (best effort)
    assert oparl_client.organizations() == {}


def test_org_name_by_id_unknown_returns_none(oparl_client):
    assert oparl_client.org_name_by_id(99999) is None
    assert oparl_client.org_name_by_id(None) is None


def test_get_raises_on_error(cfg: Config):
    client = FakeClient(handler=lambda m, u, **k: FakeResponse(503))
    o = OParlClient(cfg, client)
    with pytest.raises(RISHTTPError) as ei:
        o._get("/body/1")
    assert ei.value.status == 503
