"""Respektvoller HTTP-Client auf httpx-Basis.

Garantiert für jeden Request:
  * klar erkennbarer, respektvoller User-Agent,
  * Rate-Limiting (mind. ``rate_limit`` Sekunden Pause zwischen Requests),
  * Timeouts (connect/read),
  * Retry mit exponentiellem Backoff bei 429/5xx (max ``max_retries`` Versuche),
  * saubere Fehlerobjekte bei HTTP-/Netzwerkfehlern (Status + gekürzte Meldung),
  * persistente Cookies über eine ``httpx.Client``-Instanz (notwendig, damit
    der von der Recherche-Seite gesetzte Session-Cookie beim POST wieder gesendet
    wird – ohne ihn lehnt der Server die Suche als ungültiges Formular ab).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger("ratsinfo_mcp.http")


class RISHTTPError(Exception):
    """Saubere Fehlermeldung für HTTP-/Netzwerkprobleme (kein Traceback nötig)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"error": "http_error", "message": str(self), "status": self.status}


class RateLimiter:
    """Stellt sicher, dass zwischen zwei Starts mindestens ``min_interval`` liegen."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        sleep_for = 0.0
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            if elapsed < self.min_interval:
                sleep_for = self.min_interval - elapsed
                self._last = now + sleep_for
            else:
                self._last = now
        if sleep_for > 0:
            time.sleep(sleep_for)


class HttpClient:
    """Gedrosselter, respektvoller httpx-Client mit Retry."""

    def __init__(self, cfg: Config, follow_redirects: bool = False) -> None:
        self.cfg = cfg
        self.limiter = RateLimiter(cfg.rate_limit)
        self.follow_redirects_default = follow_redirects
        self._client = httpx.Client(
            base_url=cfg.base_url,
            headers={"User-Agent": cfg.user_agent},
            timeout=httpx.Timeout(cfg.timeout, connect=10.0),
            follow_redirects=follow_redirects,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        """Request mit Rate-Limit, Retry (429/5xx) und sauberen Fehlern.

        ``data`` wird als ``application/x-www-form-urlencoded`` gesendet (Formular).
        """
        attempts = max(1, self.cfg.max_retries)
        redirect = (
            self.follow_redirects_default if follow_redirects is None else follow_redirects
        )
        last_error: RISHTTPError | None = None
        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            try:
                resp = self._client.request(
                    method,
                    url,
                    data=data,
                    params=params,
                    headers=headers,
                    follow_redirects=redirect,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = RISHTTPError(
                    f"Netzwerkfehler bei {url}: {type(exc).__name__} ({exc})"
                )
                logger.warning("Netzwerkfehler (Versuch %d/%d): %s", attempt, attempts, exc)
                if attempt < attempts:
                    self._backoff(attempt)
                    continue
                raise last_error from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = RISHTTPError(
                    f"Serverfehler {resp.status_code} bei {url}", status=resp.status_code
                )
                logger.warning(
                    "HTTP %s (Versuch %d/%d) bei %s", resp.status_code, attempt, attempts, url
                )
                if attempt < attempts:
                    self._backoff(attempt, resp.headers.get("Retry-After"))
                    continue
                raise last_error

            return resp

        raise last_error or RISHTTPError(f"Keine Antwort von {url}")

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(float(retry_after))
                return
            except ValueError:
                pass
        time.sleep(min(2.0 ** attempt, 8.0) + random.uniform(0.0, 0.5))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
