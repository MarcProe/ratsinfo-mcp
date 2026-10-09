"""Tests für ratsinfo_mcp.config – Env-Parsing, Defaults, Properties."""

from __future__ import annotations

import pytest

from ratsinfo_mcp import __version__
from ratsinfo_mcp.config import Config, DEFAULT_USER_AGENT


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "RIS_BASE_URL", "RIS_RATE_LIMIT", "RIS_TIMEOUT",
        "RIS_MAX_RETRIES", "RIS_USER_AGENT",
        "RIS_PDF_MAX_BYTES", "RIS_PDF_MAX_SEITEN",
    ):
        monkeypatch.delenv(var, raising=False)


def test_defaults(monkeypatch: pytest.MonkeyPatch):
    c = Config()
    assert c.base_url == "https://ris.example.org"
    assert c.rate_limit == 1.0
    assert c.timeout == 15.0
    assert c.max_retries == 3
    assert c.user_agent == DEFAULT_USER_AGENT
    assert c.pdf_max_bytes == 20 * 1024 * 1024
    assert c.pdf_max_seiten == 500


def test_user_agent_contains_version(monkeypatch: pytest.MonkeyPatch):
    assert f"ratsinfo-mcp/{__version__}" in DEFAULT_USER_AGENT
    assert "rate-limited" in DEFAULT_USER_AGENT


def test_base_url_trailing_slash_stripped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RIS_BASE_URL", "https://ris.example.org/")
    assert Config().base_url == "https://ris.example.org"


def test_custom_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RIS_BASE_URL", "https://ris.example.org/")
    monkeypatch.setenv("RIS_RATE_LIMIT", "2.5")
    monkeypatch.setenv("RIS_TIMEOUT", "9")
    monkeypatch.setenv("RIS_MAX_RETRIES", "5")
    monkeypatch.setenv("RIS_USER_AGENT", "custom-agent/1.0")
    monkeypatch.setenv("RIS_PDF_MAX_BYTES", "1048576")
    monkeypatch.setenv("RIS_PDF_MAX_SEITEN", "42")
    c = Config()
    assert c.base_url == "https://ris.example.org"
    assert c.rate_limit == 2.5
    assert c.timeout == 9.0
    assert c.max_retries == 5
    assert c.user_agent == "custom-agent/1.0"
    assert c.pdf_max_bytes == 1_048_576
    assert c.pdf_max_seiten == 42


def test_properties_derive_from_base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RIS_BASE_URL", "https://ris.example.org")
    c = Config()
    assert c.oparl_base == "https://ris.example.org/webservice/oparl/v1.1"
    assert c.recherche_url == "https://ris.example.org/recherche"
    assert c.ics_feed_url == "https://ris.example.org/termine/ics/SD.NET_RIM.ics"


def test_invalid_env_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RIS_RATE_LIMIT", "nicht-zahl")
    with pytest.raises((ValueError, RuntimeError)):
        Config()
