"""Vorgefertigte Personen-Steckbrief-UI (deterministisch, ohne zweites LLM).

Kern des Features: Aus einem (bereits aufgelösten) Personen-Datensatz –
OParl-``oparl_id``-basiert oder RIM-HTML-``personen_url``-basiert – wird eine
kompakte, menschen- und agentenlesbare **Steckbrief-Karte** gerendert
(Markdown oder HTML). Der Server selbst wertet semantisch nichts aus; er
liefert deterministisch die Rohdaten der RIM-Instanz in einer festen
Vorlage – im gleichen Stil wie ``terms_of_use`` (Rohdaten + Flags, Bewertung
beim aufrufenden Agenten).

Quell-Grundsatz: Es werden **nur Daten der konfigurierten RIM-Instanz**
verwendet – OParl (``/webservice/oparl/v1.1``) ist das Webdienst-Subsystem
derselben Instanz (gleicher Host, eigener Datenbestand); die HTML-
Personenseite/``detail_url``-Links sind die zweite, komplementäre Quelle.
Keine externen Datenquellen (keine Stadt-Websites, keine Portale).
"""

from __future__ import annotations

from html import escape
from typing import Any

#: Maximale Zeichenlänge des gerenderten Steckbriefs (Guard gegen sehr große
#: Gremien-Listen; Default 8000, Max 50000).
DEFAULT_MAX_CHARS = 8000
MAX_MAX_CHARS = 50000


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Kürzt den Text auf ``max_chars``; meldet ``gekürzt`` (wie pdf_as_markdown)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "\n\n[… gekürzt]", True


def _mandatszeit_text(mandatszeit: Any) -> str:
    """``termOfElection`` (list[„YYYY-MM-DD" | {start, end}] | str) → lesbar.

    OParl übergibt ``termOfElection`` als Liste: entweder ein Paar
    ``[start, end]`` (String-Datumsangaben, ``end`` ggf. null bei laufendem
    Mandat) oder mehrere Term-Objekte ``{"start": …, "end": …}``.
    Best-effort-Abbildung auf ein „von – bis“-Textformat;
    leere/None → „keine Angabe".
    """
    if not mandatszeit:
        return "keine Angabe"
    if isinstance(mandatszeit, str):
        return mandatszeit
    if isinstance(mandatszeit, (list, tuple)):
        # String-Listen = [start, end]-Paar (ggf. nur Start).
        strings = [e for e in mandatszeit if isinstance(e, str) and e.strip()]
        dicts = [e for e in mandatszeit if isinstance(e, dict)]
        if strings and not dicts:
            if len(strings) >= 2:
                return f"{strings[0]} – {strings[-1]}"
            return f"seit {strings[0]}"
        parts: list[str] = []
        for entry in dicts:
            start = entry.get("start") or ""
            end = entry.get("end") or ""
            if start and end:
                parts.append(f"{start} – {end}")
            elif start:
                parts.append(f"seit {start}")
            elif end:
                parts.append(f"bis {end}")
        if parts:
            return ", ".join(parts)
        return ", ".join(strings) if strings else "keine Angabe"
    return str(mandatszeit)


def _zeit_klammer(beginn: str, ende: str) -> str:
    """Mandatszeit-Präfix für Gremien-Listen („seit …“, „… – …“, „bis …“)."""
    if beginn and ende:
        return f" ({beginn} – {ende})"
    if beginn:
        return f" (seit {beginn})"
    if ende:
        return f" (bis {ende})"
    return ""


def _gremien_markdown_list(gremien: list[dict[str, Any]]) -> str:
    """Gremien-Liste als Markdown-Liste in einer Zelle (2-Spalten-Tabelle)."""
    items = []
    for g in gremien:
        name = g.get("gremium") or g.get("name") or "?"
        funktion = g.get("funktion") or g.get("function") or ""
        zeit = _zeit_klammer(g.get("beginn") or g.get("von") or "",
                             g.get("ende") or g.get("bis") or "")
        items.append(f"- {name}" + (f" – {funktion}" if funktion else "") + zeit)
    return "<br>".join(items)


def render_steckbrief_markdown(
    p: dict[str, Any],
    ris_name: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, bool]:
    """Rendernt die Markdown-Steckbrief-Karte (Default).

    Erwartetes ``p`` (beide Quellen vereinheitlicht):
      * ``name`` (Pflicht)
      * ``oparl_id``, ``fraktion``/``personenkreis``, ``mandatszeit`` (TermOfElection)
      * ``gremien[]``: pro Eintrag ``gremium``/``funktion``/``beginn``/``ende``
      * ``personen_url`` (RIM-HTML-Quelle) und/oder ``oparl_url``
      * ``quelle``: „oparl" | „html" (für die Source-Attribution)
    """
    name = (p.get("name") or "?").strip()
    quelle_text = {"html": "RIM-Personenindex (HTML)",
                   "oparl": "OParl (RIM-Webdienst)"}.get(p.get("quelle") or "", "")

    lines: list[str] = ["## Steckbrief · " + name]
    source_bits: list[str] = []
    if ris_name:
        source_bits.append(f"RIM „{ris_name}“")
    if quelle_text:
        source_bits.append(quelle_text)
    if source_bits:
        lines.append("_Quelle: " + " · ".join(source_bits) + "_")
        lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")

    fraktion = p.get("fraktion") or p.get("personenkreis")
    if fraktion:
        lines.append(f"| Fraktion | {fraktion} |")
    mandatszeit = _mandatszeit_text(p.get("mandatszeit"))
    lines.append(f"| Mandat | {mandatszeit} |")

    gremien = p.get("gremien") or []
    if gremien:
        lines.append(f"| Gremien ({len(gremien)}) | "
                     + _gremien_markdown_list(gremien) + " |")
    else:
        lines.append("| Gremien | – |")

    links: list[str] = []
    if p.get("personen_url"):
        links.append(f"- [Personenseite im RIM öffnen]({p['personen_url']})")
    if p.get("oparl_url"):
        links.append(f"- [OParl-Personendatensatz]({p['oparl_url']})")
    if links:
        lines.append("")
        lines.extend(links)

    rendered = "\n".join(lines).rstrip() + "\n"
    return _truncate(rendered, max_chars)


def render_steckbrief_html(
    p: dict[str, Any],
    ris_name: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, bool]:
    """Rendernt die HTML-Variante (gleicher Datenkern, andere Schale).

    Alle Inhalte werden escaped (nur die festen Link-Ziele der RIM-Instanz
    sind als ``href`` erlaubt – externe Ziele werden verworfen).
    """
    name = escape((p.get("name") or "?").strip())
    quelle_text = escape({"html": "RIM-Personenindex (HTML)",
                          "oparl": "OParl (RIM-Webdienst)"}
                         .get(p.get("quelle") or "", ""))
    parts: list[str] = ['<div class="steckbrief">', f"<h2>Steckbrief · {name}</h2>"]
    source_bits = []
    if ris_name:
        source_bits.append(f"RIM „{escape(ris_name)}“")
    if quelle_text:
        source_bits.append(quelle_text)
    if source_bits:
        parts.append(f'<p class="quelle">Quelle: {" · ".join(source_bits)}</p>')

    rows: list[tuple[str, str]] = []
    fraktion = p.get("fraktion") or p.get("personenkreis")
    if fraktion:
        rows.append(("Fraktion", escape(fraktion)))
    rows.append(("Mandat", escape(_mandatszeit_text(p.get("mandatszeit")))))

    gremien = p.get("gremien") or []
    if gremien:
        items = []
        for g in gremien:
            gname = escape(g.get("gremium") or g.get("name") or "?")
            funktion = escape(g.get("funktion") or g.get("function") or "")
            zeit = _zeit_klammer(escape(g.get("beginn") or g.get("von") or ""),
                                 escape(g.get("ende") or g.get("bis") or ""))
            items.append(f"<li>{gname}" + (f" – {funktion}" if funktion else "") + zeit + "</li>")
        rows.append((f"Gremien ({len(gremien)})", f"<ul>{''.join(items)}</ul>"))
    else:
        rows.append(("Gremien", "–"))

    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    parts.append(f'<table class="steckbrief-tabelle">{table}</table>')

    links: list[str] = []
    if p.get("personen_url"):
        links.append(
            f'<li><a href="{escape(str(p["personen_url"]), quote=True)}">'
            "Personenseite im RIM öffnen</a></li>"
        )
    if p.get("oparl_url"):
        links.append(
            f'<li><a href="{escape(str(p["oparl_url"]), quote=True)}">'
            "OParl-Personendatensatz</a></li>"
        )
    if links:
        parts.append(f"<ul>{''.join(links)}</ul>")
    parts.append("</div>")

    rendered = "\n".join(parts)
    return _truncate(rendered, max_chars)


def render_steckbrief(
    p: dict[str, Any],
    ris_name: str,
    format: str = "markdown",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Dispatch: ``format`` → gerenderte Karte + Metadaten.

    Rückgabe: ``format``, ``name``, ``markdown`` (nur bei markdown) oder
    ``html`` (nur bei html), ``gekürzt``, ``max_chars``.
    """
    limit = max(500, min(int(max_chars), MAX_MAX_CHARS))
    fmt = (format or "markdown").strip().lower()
    if fmt in ("md", "markdown"):
        text, gekürzt = render_steckbrief_markdown(p, ris_name, limit)
        return {"format": "markdown", "name": p.get("name"), "markdown": text,
                "gekürzt": gekürzt, "max_chars": limit}
    if fmt in ("html", "xhtml"):
        text, gekürzt = render_steckbrief_html(p, ris_name, limit)
        return {"format": "html", "name": p.get("name"), "html": text,
                "gekürzt": gekürzt, "max_chars": limit}
    raise ValueError(f"Unbekanntes format {format!r}. Erlaubt: markdown, html.")
