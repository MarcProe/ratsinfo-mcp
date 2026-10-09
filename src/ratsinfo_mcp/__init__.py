"""ratsinfo-mcp: generischer MCP-Server für SD.NET RIM (Sternberg) RIS-Instanzen.

Eine Instanz = eine RIS-URL (``RIS_BASE_URL``), z.B. ``https://ris.example.org``
oder ``https://ratsinfo.example.net``. Fail-early: beim Start wird geprüft,
dass es sich um eine kompatible SD.NET RIM-Instanz handelt; Name und
Vorlagen-Periodisierung werden automatisch erkannt (best effort).

Bietet vier MCP-Tools: ``recherche`` (Volltext-Recherche inkl. PDF-Volltexte,
optional mit OParl-Metadaten), ``pdf_as_markdown`` (PDF-Volltext als
Markdown), ``meeting_documents`` (Sitzungs-/Vorgangs-Übersicht mit allen
PDF-URLs) und ``find_meetings`` (Sitzungen aus dem ICS-Termin-Feed).
"""

__version__ = "0.4.0"


def main() -> None:
    """Projekt-Einstiegspunkt (``uvx --from <dir> ratsinfo-mcp``).

    Startet den MCP-Server über den stdio-Transport.
    """
    from .server import main as _main

    _main()


__all__ = ["__version__", "main"]
