"""Tests für ratsinfo_mcp.oparl – Reference-Extraction, Paper-Index, Enrichment.

Offline: FakeClient liefert OParl-JSON aus tests/fixtures/oparl_paper_list.json.
"""

from __future__ import annotations

import json

import pytest

from ratsinfo_mcp.config import Config
from ratsinfo_mcp.http_client import RISHTTPError
from ratsinfo_mcp.oparl import (
    OParlClient,
    _enrich_enabled,
    _max_pages,
    _person_max_pages,
    _person_ttl_seconds,
    extract_references,
    norm_name,
)
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


# --- Person-/Gremium-Index ---------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Dr.  Max  Müller", "drmaxmuller"),
        ("GRÜNE", "grune"),
        ("Schul\nausschuss (AC)", "schulausschussac"),
        ("ßstraße", "ssstrasse"),
        ("", ""),
        (None, ""),
    ],
)
def test_norm_name(text, expected):
    assert norm_name(text) == expected


def test_person_max_pages_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPARL_PERSON_MAX_PAGES", raising=False)
    assert _person_max_pages() == 25
    monkeypatch.setenv("OPARL_PERSON_MAX_PAGES", "2")
    assert _person_max_pages() == 2
    monkeypatch.setenv("OPARL_PERSON_MAX_PAGES", "0")
    assert _person_max_pages() == 1
    monkeypatch.setenv("OPARL_PERSON_MAX_PAGES", "x")
    assert _person_max_pages() == 25


def test_person_ttl_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPARL_PERSON_TTL", raising=False)
    assert _person_ttl_seconds() == 86400.0
    monkeypatch.setenv("OPARL_PERSON_TTL", "10")
    assert _person_ttl_seconds() == 300.0  # Mindestwert
    monkeypatch.setenv("OPARL_PERSON_TTL", "xyz")
    assert _person_ttl_seconds() == 86400.0


@pytest.fixture
def persons_client(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> OParlClient:
    """Client mit OParl-Personen-/Gremien-Fixtures (fiktive Teststadt)."""
    person_list = json.loads((FIXTURES / "oparl_person_list.json").read_text())
    committees = {
        "data": [
            {"id": 11, "name": "Rat"},
            {"id": 12, "name": "Schulausschuss"},
            {"id": 13, "name": "Finanzausschuss"},
        ],
        "pagination": {"page": 1, "totalPages": 1},
    }
    members = {
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

    def handler(m, u, **k):
        if u.endswith("/body/1"):
            return FakeResponse(200, json_data={"fullName": "Stadt Teststadt"})
        if "/person" in u and "/committee/" not in u:
            return FakeResponse(200, json_data=person_list)
        if u.endswith("/body/1/committee"):
            return FakeResponse(200, json_data=committees)
        if "/committee/12/person" in u:
            return FakeResponse(200, json_data=members)
        return FakeResponse(404, text="")

    return OParlClient(cfg, FakeClient(handler=handler))


def test_lookup_person_exact(persons_client):
    hits = persons_client.lookup_person("Dr. Max Mustermann")
    assert len(hits) == 1
    h = hits[0]
    assert h["oparl_id"] == "/body/1/person/101"
    assert h["fraktion"] == "CDU"
    assert h["mandatszeit"] == ["2020-10-01", "2025-09-30"]
    names = {g["gremium"] for g in h["gremien"]}
    assert names == {"Rat", "Finanzausschuss"}


def test_lookup_person_umlaut_insensitive(persons_client):
    # „Anna Müller“ (Umlaut in den Index-Daten) matcht die eingetippte
    # Umkehrung „Anna Muller“ (Basislaute in der Eingabe) – beide Seiten
    # laufen durch dieselbe Normalisierung (Umlaut → Basislaut).
    hits = persons_client.lookup_person("Anna Muller")
    assert any(h["name"] == "Anna Müller" for h in hits)


def test_lookup_person_partial_and_ranking(persons_client):
    # Exakte Treffer vor Teilstring-Treffern; mehrere Teilstrings erlaubt.
    hits = persons_client.lookup_person("Max")
    assert len(hits) >= 2
    # „Dr. Max Mustermann“ und „Max Müller“ sind Teilstrings beider Richtungen.
    assert {h["name"] for h in hits} >= {"Dr. Max Mustermann", "Max Müller"}


def test_lookup_person_fraktion_filter(persons_client):
    hits = persons_client.lookup_person("Beispiel", fraktion="SPD")
    assert [h["name"] for h in hits] == ["Petra Beispiel"]
    assert persons_client.lookup_person("Beispiel", fraktion="CDU") == []


def test_lookup_person_gremium_filter(persons_client):
    hits = persons_client.lookup_person("Beispiel", gremium="Schul")
    assert [h["name"] for h in hits] == ["Petra Beispiel"]
    assert persons_client.lookup_person("Beispiel", gremium="Sport") == []


def test_lookup_person_limit(persons_client):
    hits = persons_client.lookup_person("Beispiel", limit=1)
    assert len(hits) == 1


def test_lookup_person_empty_query(persons_client):
    assert persons_client.lookup_person("") == []
    assert persons_client.lookup_person("   ") == []


def test_lookup_person_cached_within_ttl(persons_client):
    persons_client.lookup_person("Mustermann")
    persons_client.lookup_person("Mustermann")
    person_calls = [
        c for c in persons_client.client.calls if "/person" in c[1] and "/committee/" not in c[1]
    ]
    assert len(person_calls) == 1  # TTL-Cache: zweiter Call ohne Request


def test_person_by_oparl_id(persons_client):
    p = persons_client.person_by_oparl_id("/body/1/person/102")
    assert p is not None and p["name"] == "Anna Müller"
    assert persons_client.person_by_oparl_id("/body/1/person/999") is None
    assert persons_client.person_by_oparl_id("") is None


def test_committee_name_to_id(persons_client):
    assert persons_client.committee_name_to_id("Rat") == 11
    assert persons_client.committee_name_to_id("rat") == 11          # case-insensitiv
    assert persons_client.committee_name_to_id("Schul") == 12        # Teilstring
    assert persons_client.committee_name_to_id("Quatsch") is None
    assert persons_client.committee_name_to_id("") is None


def test_committee_name_by_id(persons_client):
    assert persons_client.committee_name_by_id(12) == "Schulausschuss"
    assert persons_client.committee_name_by_id(999) is None


def test_committee_members(persons_client):
    members = persons_client.committee_members(12)
    assert [m["name"] for m in members] == ["Dr. Max Mustermann", "Petra Beispiel"]
    assert members[0]["personenkreis"] == "CDU"
    assert members[0]["mandatszeit"] == ["2020-10-01", "2025-09-30"]


def test_committee_members_cached(persons_client):
    persons_client.committee_members(12)
    persons_client.committee_members(12)
    member_calls = [c for c in persons_client.client.calls if "/committee/12/person" in c[1]]
    assert len(member_calls) == 1


def test_committee_members_http_error(persons_client):
    persons_client.client.handler = lambda m, u, **k: FakeResponse(500)
    with pytest.raises(RISHTTPError) as ei:
        persons_client.committee_members(12)
    assert ei.value.status == 500


def test_ensure_persons_http_error_returns_empty(persons_client):
    persons_client.client.handler = lambda m, u, **k: FakeResponse(503)
    persons_client._persons = None
    assert persons_client._ensure_persons() == []  # best effort, kein Fehler
