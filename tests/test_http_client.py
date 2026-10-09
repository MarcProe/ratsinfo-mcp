"""Tests für ratsinfo_mcp.http_client – RateLimiter, Retry/Backoff, Fehlerpfade.

``httpx.Client`` wird gepatcht (Offline); das Retry-/Backoff-Verhalten des
eigenen Codes wird real geprüft (``time.sleep`` wird protokolliert, nicht
ausgeführt).
"""

from __future__ import annotations

import httpx
import pytest

from ratsinfo_mcp import http_client as hc_mod
from ratsinfo_mcp.config import Config
from ratsinfo_mcp.http_client import HttpClient, RateLimiter, RISHTTPError


# --- RateLimiter ----------------------------------------------------------------
def test_rate_limiter_no_wait_when_zero():
    rl = RateLimiter(0)
    rl.wait()  # darf nie hängen


def test_rate_limiter_enforces_interval(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []
    monkeypatch.setattr(hc_mod.time, "sleep", lambda s: slept.append(s))
    rl = RateLimiter(1.0)
    rl.wait()  # erster Call: kein Sleep
    assert slept == []
    rl.wait()  # zweiter Call sofort danach: muss ~1.0 warten
    assert len(slept) == 1
    assert slept[0] >= 0.9


def test_rate_limiter_negative_becomes_zero():
    rl = RateLimiter(-5)
    assert rl.min_interval == 0.0


# --- Fake-Transport für HttpClient ----------------------------------------------
class FakeHttpxClient:
    """Ersetzt httpx.Client: liefert nacheinander vordefinierte Outcomes."""

    def __init__(self, outcomes: list, init_kwargs: dict | None = None):
        self.outcomes = list(outcomes)
        self.requests: list[tuple] = []
        self.init_kwargs = init_kwargs

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        if not self.outcomes:
            raise AssertionError("FakeHttpxClient: mehr Requests als Outcomes")
        return self.outcomes.pop(0)

    def close(self):
        pass


def test_http_ok_first_try(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    ok = httpx.Response(200, text="hi")
    monkeypatch.setattr(hc_mod.httpx, "Client", lambda **kw: FakeHttpxClient([ok]))
    monkeypatch.setattr(hc_mod.time, "sleep", lambda *a, **k: None)
    with HttpClient(cfg) as c:
        resp = c.request("GET", "/x")
    assert resp.status_code == 200
    assert resp.text == "hi"


def test_http_retries_on_500_then_ok(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    monkeypatch.setattr(hc_mod.httpx, "Client", lambda **kw: FakeHttpxClient([
        httpx.Response(500), httpx.Response(503), httpx.Response(200, text="done"),
    ]))
    sleeps: list[float] = []
    monkeypatch.setattr(hc_mod.time, "sleep", lambda s: sleeps.append(s))
    with HttpClient(cfg) as c:
        resp = c.request("GET", "/x")
    assert resp.text == "done"
    # 2 Backoff-Sleeps (500, 503); exponentiell: >= 2^1, >= 2^2 (plus Jitter)
    assert len(sleeps) == 2
    assert sleeps[0] >= 1.9  # min(2^1, 8) = 2, Jitter 0..0.5
    assert sleeps[1] >= 3.9


def test_http_retry_exhausted_raises(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    monkeypatch.setattr(hc_mod.httpx, "Client", lambda **kw: FakeHttpxClient([
        httpx.Response(429), httpx.Response(429), httpx.Response(429),
    ]))
    monkeypatch.setattr(hc_mod.time, "sleep", lambda *a, **k: None)
    with HttpClient(cfg) as c:
        with pytest.raises(RISHTTPError) as ei:
            c.request("GET", "/x")
    assert ei.value.status == 429


def test_http_4xx_not_retried(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    calls: list[int] = []

    class CountingClient:
        def __init__(self, **kw):
            pass

        def request(self, method, url, **kw2):
            calls.append(1)
            return httpx.Response(404, text="not found")

        def close(self):
            pass

    monkeypatch.setattr(hc_mod.httpx, "Client", CountingClient)
    with HttpClient(cfg) as c:
        resp = c.request("GET", "/x")
    assert resp.status_code == 404
    assert len(calls) == 1  # kein Retry bei 404


def test_http_network_error_retried_then_raises(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    def boom(self, method, url, **kw):
        raise httpx.ConnectError("refused")

    class FlakyClient:
        def __init__(self, **kw):
            pass

        request = boom

        def close(self):
            pass

    monkeypatch.setattr(hc_mod.httpx, "Client", FlakyClient)
    monkeypatch.setattr(hc_mod.time, "sleep", lambda *a, **k: None)
    with HttpClient(cfg) as c:
        with pytest.raises(RISHTTPError) as ei:
            c.request("GET", "/x")
    assert ei.value.status is None
    assert "ConnectError" in str(ei.value)


def test_http_retry_after_header_respected(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    sleeps: list[float] = []
    monkeypatch.setattr(hc_mod.httpx, "Client", lambda **kw: FakeHttpxClient([
        httpx.Response(503, headers={"Retry-After": "0.42"}),
        httpx.Response(200, text="ok"),
    ]))
    monkeypatch.setattr(hc_mod.time, "sleep", lambda s: sleeps.append(s))
    with HttpClient(cfg) as c:
        resp = c.request("GET", "/x")
    assert resp.text == "ok"
    # RateLimiter-Sleep (cfg.rate_limit=0 → keiner) + Retry-After 0.42
    assert any(abs(s - 0.42) < 1e-9 for s in sleeps)


def test_http_form_data_urlencoded(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    seen: dict = {}

    class CaptureClient:
        def __init__(self, **kw):
            pass

        def request(self, method, url, data=None, **kw2):
            seen["data"] = data
            seen["kw"] = kw2
            return httpx.Response(200)

        def close(self):
            pass

    monkeypatch.setattr(hc_mod.httpx, "Client", CaptureClient)
    with HttpClient(cfg) as c:
        c.request("POST", "/x", data={"a": "1", "b": "zwei"})
    assert seen["data"] == {"a": "1", "b": "zwei"}


def test_user_agent_and_timeout_configured(monkeypatch: pytest.MonkeyPatch, cfg: Config):
    captured: dict = {}

    class KWClient:
        def __init__(self, **kw):
            captured.update(kw)

        def request(self, *a, **k):
            return httpx.Response(200)

        def close(self):
            pass

    monkeypatch.setattr(hc_mod.httpx, "Client", KWClient)
    with HttpClient(cfg):
        pass
    assert captured["headers"]["User-Agent"] == cfg.user_agent
    assert captured["timeout"] == httpx.Timeout(cfg.timeout, connect=10.0)


def test_ris_http_error_to_dict():
    e = RISHTTPError("boom", status=503)
    d = e.to_dict()
    assert d == {"error": "http_error", "message": "boom", "status": 503}
    assert RISHTTPError("x").to_dict()["status"] is None
