"""Laufzeit-Konfiguration per Umgebungsvariablen (generisch für SD.NET RIM).

Eine Instanz = eine RIS-URL, zugeordnet über ``RIS_BASE_URL``. Alle anderen
Werte sind Defaults mit plausiblen Defaults und überschreibbar.

Instanz-spezifische Eigenschaften (Name, Periodisierung, count-Verhalten)
werden NICHT manuell vorgegeben, sondern per :mod:`ratsinfo_mcp.detect`
automatisch erkannt (fail-early, siehe ``RIS_STRICT``).

Werte sind bewusst konservativ, damit der Server die Infrastruktur des
betreffenden RIS betreibers respektiert (langsame Request-Frequenz, kurze
Timeouts).
"""

from __future__ import annotations

import os

from . import __version__

# Klar erkennbarer, respektvoller User-Agent (Projektname + Version + Hinweis,
# dass es sich um einen einzelnen, gedrosselten Read-only-Client handelt).
# Der Name des RIS-Trägers kann über RIS_USER_AGENT ergänzt werden.
DEFAULT_USER_AGENT = (
    f"ratsinfo-mcp/{__version__} "
    "(MCP Read-only-Client; single user; rate-limited >=1s between requests)"
)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{key} muss eine Zahl sein (kam {raw!r}).") from None


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{key} muss eine ganze Zahl sein (kam {raw!r}).") from None


class Config:
    """Zentrale Konfiguration; Defaults = 1 req/s, 15s Timeout.

    Pflicht: ``RIS_BASE_URL`` (oder ``RIS_NAME``-less Default). In Strict-Modus
    (``RIS_STRICT=1``, Default) bricht der Server fail-early ab, wenn die URL
    nicht als SD.NET RIM erkannt wird (siehe :mod:`ratsinfo_mcp.detect`).
    """

    def __init__(self) -> None:
        self.base_url: str = os.environ.get(
            "RIS_BASE_URL", "https://ris.example.org"
        ).rstrip("/")

        # Mind. Pause zwischen zwei HTTP-Requests in Sekunden (>= 1.0 empfohlen).
        self.rate_limit: float = _env_float("RIS_RATE_LIMIT", 1.0)
        # Connect/Read-Timeout in Sekunden.
        self.timeout: float = _env_float("RIS_TIMEOUT", 15.0)
        # Maximale Request-Versuche pro Aufruf (inkl. Erstversuch), Backoff 429/5xx.
        self.max_retries: int = _env_int("RIS_MAX_RETRIES", 3)
        # User-Agent überschreibbar (default: respektvoll + Projektversion).
        self.user_agent: str = os.environ.get("RIS_USER_AGENT", DEFAULT_USER_AGENT)
        # PDF-Download-Guards (Tool pdf_as_markdown).
        self.pdf_max_bytes: int = _env_int("RIS_PDF_MAX_BYTES", 20971520)  # 20 MB
        self.pdf_max_seiten: int = _env_int("RIS_PDF_MAX_SEITEN", 500)

        # Fail-early: True (Default) → Server startet NICHT, wenn die Instanz
        # nicht als SD.NET RIM erkannt wird. False → Warnung, Server läuft.
        self.strict: bool = os.environ.get("RIS_STRICT", "1") not in ("0", "false", "no")

        # Instanz-Name (best effort aus OParl/HTML); leere Zeichenkette, wenn
        # nicht erkannt. Wird für Attribution-Strings in den Tools genutzt.
        self.name: str = os.environ.get("RIS_NAME", "")

    # --- abgeleitete Endpunkte (abgeleitet von RIS_BASE_URL) ---
    @property
    def oparl_base(self) -> str:
        """OParl-Webdienst (v1.1)."""
        return f"{self.base_url}/webservice/oparl/v1.1"

    @property
    def recherche_url(self) -> str:
        """Recherche-Formular/PRG-Endpoint."""
        return f"{self.base_url}/recherche"

    @property
    def ics_feed_url(self) -> str:
        """Termin-ICS-Feed (Sitzungskalender, sessionfrei, serverseitig)."""
        return f"{self.base_url}/termine/ics/SD.NET_RIM.ics"

    @property
    def host(self) -> str:
        """Nur der Netloc (für Host-Whitelist in PDF-/Detail-Guards)."""
        return self.base_url.split("://", 1)[-1].split("/", 1)[0].lower()
