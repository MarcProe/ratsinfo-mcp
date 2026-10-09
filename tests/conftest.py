"""Shared Test-Fixtures für die ratsinfo-mcp-Testsuite.

Strategie: **vollständig offline**. Alle HTTP-Komponenten werden per
``FakeClient``-Objekt ersetzt (duck-typing gegen ``HttpClient.request``);
HTML/PDF/ICS/JSON kommen aus ``tests/fixtures`` (reale Captures vom
08.10.2026 + deterministisch erzeugte Mini-PDFs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from ratsinfo_mcp.config import Config
from ratsinfo_mcp import pdf_doc as _pdf_doc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- Offline-Modus: Detection abschalten, Memo leeren -------------------------
@pytest.fixture(autouse=True)
def _offline_mode(monkeypatch: pytest.MonkeyPatch):
    """Sichert alle Tests offline: keine Netz-Requests durch die Fail-early-
    Kompatibilitätsprüfung (RIS_DETECT=0) und leert das Detection-Memo pro Test,
    damit ein Test nicht die Erkennungsergebnisse eines anderen erbt."""
    monkeypatch.setenv("RIS_DETECT", "0")
    # Fiktive Testinstanz: alle Fixture-URLs liegen auf ris.teststadt.de; die
    # SSRF-Whitelist (PDF-/Detail-Guards) muss denselben Host als Basis anerkennen.
    # (test_config.py delenv't RIS_BASE_URL selbst, um das echte Default zu testen.)
    monkeypatch.setenv("RIS_BASE_URL", "https://ris.teststadt.de")
    import ratsinfo_mcp.detect as detect
    import ratsinfo_mcp.server as _server
    detect._MEMO.clear()
    _server._CFG = None
    yield
    detect._MEMO.clear()
    _server._CFG = None


# --- Fixtures-Dateien ---------------------------------------------------------
@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def recherche_form_html() -> str:
    """Echte /recherche-Seite (Formular mit reqid/csrftoken + Gremium-Select)."""
    return (FIXTURES / "recherche_page.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def results_html() -> str:
    """Echte Ergebnis-Seite mit Treffer-Tabellen, Fundstellen, Paginierung."""
    return (FIXTURES / "results_sample.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def results_empty_html() -> str:
    return (FIXTURES / "results_empty.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def form_invalid_html() -> str:
    return (FIXTURES / "form_invalid.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def tops_html() -> str:
    """Echte Sitzungsseite /tops/ (44 TOPs, 76 PDFs)."""
    return (FIXTURES / "tops_sample.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def vorgang_html() -> str:
    """Echte Vorgangsseite /vorgang/ (4 PDFs)."""
    return (FIXTURES / "vorgang_sample.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def ics_text() -> str:
    """Echter ICS-Feed (250 VEVENTs)."""
    return (FIXTURES / "ics_sample.ics").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def small_pdf_bytes() -> bytes:
    return (FIXTURES / "small_2pages.pdf").read_bytes()


@pytest.fixture(scope="session")
def big_pdf_bytes() -> bytes:
    return (FIXTURES / "big_30pages.pdf").read_bytes()


# --- Config (Test-Defaults) ----------------------------------------------------
@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> Config:
    """Config mit Test-Defaults: 0 Rate-Limit (Tests laufen ohne Wartezeit)."""
    monkeypatch.delenv("RIS_RATE_LIMIT", raising=False)
    monkeypatch.delenv("RIS_PDF_MAX_BYTES", raising=False)
    monkeypatch.delenv("RIS_PDF_MAX_SEITEN", raising=False)
    # Fiktive Testinstanz: alle Fixture-URLs liegen auf ris.teststadt.de, die
    # SSRF-Whitelist (PDF-/Detail-Guards) muss denselben Host als Basis anerkennen.
    monkeypatch.setenv("RIS_BASE_URL", "https://ris.teststadt.de")
    monkeypatch.setenv("RIS_RATE_LIMIT", "0")
    return Config()


# --- HTTP-Fake -----------------------------------------------------------------
class FakeResponse:
    """Duck-typing für httpx.Response (nur die genutzten Attribute)."""

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        # httpx.Headers ist case-insensitive (wie im echten httpx.Response) –
        # wichtig, weil z.B. recherche resp.headers.get("Location") liest.
        self.headers = httpx.Headers(headers or {})
        self._json = json_data

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("Response hat kein JSON")
        return self._json


def pdf_response(content: bytes, headers: dict[str, str] | None = None) -> FakeResponse:
    """Standard-PDF-Antwort (application/pdf)."""
    h = {"content-type": "application/pdf"}
    h.update(headers or {})
    return FakeResponse(200, content=content, headers=h)


class FakeClient:
    """Ersetzt HttpClient: ``handler(method, url, **kw)`` → FakeResponse.

    Protokolliert jeden Aufruf in ``.calls``; ``fail_after`` bricht nach
    N erfolgreichen Aufrufen ab (für Retry-/Cache-Tests).
    """

    def __init__(
        self,
        handler: Callable[..., FakeResponse] | None = None,
        default: FakeResponse | None = None,
    ) -> None:
        self.handler = handler
        self.default = default or FakeResponse(200, "")
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> FakeResponse:
        self.calls.append((method, url, {"data": data, "params": params, "headers": headers}))
        if self.handler is not None:
            return self.handler(method, url, data=data, params=params, headers=headers)
        return self.default

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    # -- Convenience-Property: letzter Aufruf ----------------------------------
    @property
    def last_call(self) -> tuple[str, str, dict[str, Any]]:
        return self.calls[-1]


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


# --- Recherche-Sequenz-Fixtures -------------------------------------------------
@pytest.fixture
def recherche_seq() -> FakeClient:
    """Recherche-Ablauf: GET Formular → POST 302 → GET Ergebnisse (reale HTML)."""
    form_html = (FIXTURES / "recherche_page.html").read_text(encoding="utf-8")
    results_html = (FIXTURES / "results_sample.html").read_text(encoding="utf-8")
    state = {"step": 0}

    def handler(method: str, url: str, **kw: Any) -> FakeResponse:
        if method == "GET" and url.rstrip("/").endswith("recherche") and "results" not in url:
            state["step"] = 1
            return FakeResponse(200, text=form_html)
        if method == "POST":
            state["step"] = 2
            return FakeResponse(
                302, headers={"Location": "/recherche/?__=RESULTTOKEN&search=1"}
            )
        # GET auf die Ergebnis-Location
        return FakeResponse(200, text=results_html)

    return FakeClient(handler=handler)


@pytest.fixture
def tops_client(tops_html: str) -> FakeClient:
    """Client für eine /tops/-Seite."""

    def handler(method: str, url: str, **kw: Any) -> FakeResponse:
        return FakeResponse(200, text=tops_html)

    return FakeClient(handler=handler)


@pytest.fixture
def vorgang_client(vorgang_html: str) -> FakeClient:
    def handler(method: str, url: str, **kw: Any) -> FakeResponse:
        return FakeResponse(200, text=vorgang_html)

    return FakeClient(handler=handler)


@pytest.fixture
def ics_client(ics_text: str) -> FakeClient:
    def handler(method: str, url: str, **kw: Any) -> FakeResponse:
        return FakeResponse(200, text=ics_text, headers={"content-type": "text/calendar"})

    return FakeClient(handler=handler)


# --- PDF-Cache: Reset zwischen Tests -------------------------------------------
@pytest.fixture(autouse=True)
def _reset_pdf_cache():
    """Jeder Test startet mit leeren PDF-Cache (sonst verdecken Cache-Treffer
    unbeabsichtigt Download-Pfade)."""
    _pdf_doc._CACHE._data.clear()
    yield
    _pdf_doc._CACHE._data.clear()


# --- PDF-URL-Helper ------------------------------------------------------------
PDF_URL = "https://ris.teststadt.de/sdnetrim/AAAABBBB/doc.pdf"
PDF_URL_FRAG = PDF_URL + "#search=Begriff"
