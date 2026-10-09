"""Tests für ratsinfo_mcp.persons – Steckbrief-Rendering (Markdown/HTML).

Vollständig offline: reine Rendering-Funktionen, keine HTTP-Komponenten.
"""

from __future__ import annotations

import pytest

from ratsinfo_mcp.persons import (
    _mandatszeit_text,
    _zeit_klammer,
    render_steckbrief,
    render_steckbrief_html,
    render_steckbrief_markdown,
)

# Fiktive Teststadt-Person (wie in tests/fixtures/oparl_person_list.json).
PERSON_OPARL = {
    "oparl_id": "/body/1/person/101",
    "name": "Dr. Max Mustermann",
    "fraktion": "CDU",
    "mandatszeit": ["2020-10-01", "2025-09-30"],
    "gremien": [
        {"gremium": "Rat", "funktion": None, "beginn": "2020-10-01", "ende": "2025-09-30"},
        {"gremium": "Finanzausschuss", "funktion": "Mitglied", "beginn": "2020-10-01", "ende": "2025-09-30"},
    ],
    "personen_url": None,
    "oparl_url": "https://ris.teststadt.de/webservice/oparl/v1.1/body/1/person/101",
    "quelle": "oparl",
}

PERSON_HTML = {
    "name": "Herr Lutz Ackermann",
    "fraktion": "Sachkundiger Bürger",
    "mandatszeit": None,
    "gremien": [],
    "oparl_id": None,
    "personen_url": "https://ris.teststadt.de/personen/?__=TOKEN123",
    "oparl_url": None,
    "quelle": "html",
}


# --- Hilfsfunktionen -----------------------------------------------------------
def test_mandatszeit_text_none():
    assert _mandatszeit_text(None) == "keine Angabe"
    assert _mandatszeit_text([]) == "keine Angabe"


def test_mandatszeit_text_string_list():
    assert _mandatszeit_text(["2020-10-01", "2025-09-30"]) == "2020-10-01 – 2025-09-30"


def test_mandatszeit_text_dict_list():
    assert (
        _mandatszeit_text([{"start": "2020-10-01", "end": None}])
        == "seit 2020-10-01"
    )


def test_mandatszeit_text_plain_string():
    assert _mandatszeit_text("2020–2025") == "2020–2025"


@pytest.mark.parametrize(
    "beginn,ende,expected",
    [
        ("2020-10-01", "2025-09-30", " (2020-10-01 – 2025-09-30)"),
        ("2020-10-01", "", " (seit 2020-10-01)"),
        ("", "2020-09-30", " (bis 2020-09-30)"),
        ("", "", ""),
    ],
)
def test_zeit_klammer(beginn, ende, expected):
    assert _zeit_klammer(beginn, ende) == expected


# --- Markdown-Rendering ----------------------------------------------------------
def test_markdown_contains_core_fields():
    md, gekürzt = render_steckbrief_markdown(PERSON_OPARL, "Stadt Teststadt")
    assert gekürzt is False
    assert "## Steckbrief · Dr. Max Mustermann" in md
    assert "| Fraktion | CDU |" in md
    assert "| Mandat | 2020-10-01 – 2025-09-30 |" in md
    assert "| Gremien (2) |" in md
    assert "- Rat (2020-10-01 – 2025-09-30)" in md
    assert "- Finanzausschuss – Mitglied (2020-10-01 – 2025-09-30)" in md


def test_markdown_source_attribution():
    md, _ = render_steckbrief_markdown(PERSON_OPARL, "Stadt Teststadt")
    assert "Quelle: RIM „Stadt Teststadt“ · OParl (RIM-Webdienst)" in md


def test_markdown_html_person_source():
    md, _ = render_steckbrief_markdown(PERSON_HTML, "Stadt Teststadt")
    assert "RIM-Personenindex (HTML)" in md
    assert "- [Personenseite im RIM öffnen](https://ris.teststadt.de/personen/?__=TOKEN123)" in md
    # OParl-URL fehlt bei HTML-Person
    assert "OParl-Personendatensatz" not in md


def test_markdown_no_gremien():
    md, _ = render_steckbrief_markdown(PERSON_HTML, "")
    assert "| Gremien | – |" in md


def test_markdown_truncation():
    many = dict(PERSON_OPARL)
    many["gremien"] = [
        {"gremium": f"Gremium {i}", "funktion": None, "beginn": "2020-01-01", "ende": None}
        for i in range(200)
    ]
    md, gekürzt = render_steckbrief_markdown(many, "Stadt Teststadt", max_chars=1000)
    assert gekürzt is True
    assert "[… gekürzt]" in md


# --- HTML-Rendering ---------------------------------------------------------------
def test_html_contains_core_fields():
    html, gekürzt = render_steckbrief_html(PERSON_OPARL, "Stadt Teststadt")
    assert gekürzt is False
    assert "<h2>Steckbrief · Dr. Max Mustermann</h2>" in html
    assert "Fraktion</th><td>CDU" in html
    assert "2020-10-01 – 2025-09-30" in html
    assert "<li>Finanzausschuss – Mitglied (2020-10-01 – 2025-09-30)</li>" in html


def test_html_escapes_names():
    evil = dict(PERSON_OPARL)
    evil["name"] = '<script>alert("x")</script> Müller'
    evil["fraktion"] = "<b>CDU</b>"
    html, _ = render_steckbrief_html(evil, "Stadt Teststadt")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;CDU&lt;/b&gt;" in html


# --- Dispatch -----------------------------------------------------------------------
def test_dispatch_markdown_default():
    r = render_steckbrief(PERSON_OPARL, "Stadt Teststadt")
    assert r["format"] == "markdown"
    assert "markdown" in r and "html" not in r
    assert r["gekürzt"] is False
    assert r["name"] == "Dr. Max Mustermann"


def test_dispatch_md_alias():
    r = render_steckbrief(PERSON_OPARL, "", format="md")
    assert r["format"] == "markdown"


def test_dispatch_html():
    r = render_steckbrief(PERSON_HTML, "Stadt Teststadt", format="HTML")
    assert r["format"] == "html"
    assert "html" in r and "markdown" not in r


def test_dispatch_invalid_format():
    with pytest.raises(ValueError, match="Unbekanntes format"):
        render_steckbrief(PERSON_OPARL, "", format="pdf")


def test_dispatch_max_chars_clamped():
    r = render_steckbrief(PERSON_OPARL, "", max_chars=10)
    assert r["max_chars"] == 500  # Mindest-Cap
