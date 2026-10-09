# ratsinfo-mcp · generischer MCP-Server für SD.NET RIM (Sternberg)

> **⚠️ Hinweis: Inoffizielles Drittanbieter-Projekt**
>
> Diese Software ist ein **unabhängiges, inoffizielles Open-Source-Projekt**.
> Sie steht in **keiner Verbindung** zu den Städten/Kommunen, die SD.NET RIM
> einsetzen, und wird von diesen weder betrieben, unterstützt noch autorisiert.
> Ebenso besteht **kein Verhältnis** zum Hersteller **SD.NET RIM / Sternberg
> Software** — keine Partnerschaft, keine Kooperation, keine Freigabe.
> „SD.NET RIM" und „Sternberg" sind Marken des jeweiligen Herstellers und
> werden hier ausschließlich zur Produktidentifikation verwendet.
> Nutzung auf eigene Verantwortung; die Betreiber der angebundenen
> Ratsinformationssysteme tragen keine Haftung für dieses Projekt.

MCP-Server für **SD.NET RIM** (Sternberg Software) Ratsinformationssysteme —
generisch über alle Instanzen. Eine Instanz = eine URL, konfiguriert über
`RIS_BASE_URL`. Der Server ist bewusst so gebaut, dass er auf einer
beliebigen RIM-Instanz unverändert läuft; instanzspezifische Merkmale
(Träger-Name, Vorlagen-Periodisierung, count-Verhalten) werden
**automatisch erkannt** und nicht manuell gepflegt.

## 5 Tools

| Tool | Zweck |
|------|-------|
| `recherche` | Volltext-Recherche **inkl. indizierter PDF-Volltexte** (Fundstelle mit `<mark>`); optional mit OParl-Metadaten-Anreicherung |
| `pdf_as_markdown` | Lädt ein RIS-PDF und liefert den Volltext als Markdown (Seiten-/Begriffs-Filter, Zeichen-Cap) |
| `meeting_documents` | Metadaten einer Sitzung (`/tops/`) oder eines Vorgangs (`/vorgang/`) inkl. aller PDF-URLs, pro TOP gruppiert |
| `find_meetings` | Sitzungen aus dem ICS-Termin-Feed (Gremium, Datum, Ort, `tops_url`) |
| `terms_of_use` | Nutzungsbedingungen/Impressum/Datenschutz der Instanz: Discovery (ToS-Links finden) + Extraktion (Haupttext + Keyword-Flags) — damit das aufrufende LLM selbst prüfen kann, unter welchen Bedingungen Dokumente zitiert/aufbereitet werden dürfen |

Typischer Agent-Fluss: `recherche` → (Treffer mit `detail_url`/TOP) →
`meeting_documents` → `pdf_as_markdown` auf das eine PDF. Bei
Wiederverwendungsfragen: `terms_of_use`.

## Konfiguration (Env)

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `RIS_BASE_URL` | `https://ris.example.org` | **Die** Instanz (eine RIS-URL). Pflicht für den Produktivbetrieb setzen. |
| `RIS_NAME` | *auto* | Träger-Name; leer = automatisch erkennen (OParl `/body/1 → name`, Fallback `<title>`) |
| `RIS_STRICT` | `1` | `1` = **Fail-early**: Der Server startet nicht, wenn die konfigurierte URL keine erkannte SD.NET RIM-Instanz ist (klarer Fehler-Report statt späterer Leerergebnisse). `0` = nur Warnung. |
| `RIS_DETECT` | `1` | `0` = Kompatibilitäts- und Name-Erkennung überspringen (Offline-Tests, bewusste Deaktivierung) |
| `RIS_RATE_LIMIT` | `1.0` | Mindest-Pause zwischen zwei HTTP-Requests in Sekunden (Respekt vor der Ziel-Infrastruktur) |
| `RIS_TIMEOUT` | `15.0` | Connect/Read-Timeout in Sekunden |
| `RIS_MAX_RETRIES` | `3` | Request-Versuche inkl. Backoff bei 429/5xx |
| `RIS_USER_AGENT` | `ratsinfo-mcp/<ver>` | User-Agent (klar erkennbar als Read-only-Client) |
| `RIS_PDF_MAX_BYTES` | `20971520` (20 MB) | PDF-Größen-Limit (Guard) |
| `RIS_PDF_MAX_SEITEN` | `500` | PDF-Seiten-Limit (Guard) |
| `OPARL_ENRICH` | `0` | `1` = OParl-Metadaten-Anreicherung der Suchtreffer (verlängert Laufzeit) |
| `OPARL_MAX_PAGES` | `8` | OParl: max. Paper-Index-Seiten für die Anreicherung |

### Zwei Instanzen nebeneinander (MCP-Client-Konfig)

```json
{
  "mcpServers": {
    "ris-stadt-a": {
      "command": "uv", "args": ["--directory", "/pfad/zu/ratsinfo-mcp", "run", "ratsinfo-mcp"],
      "env": { "RIS_BASE_URL": "https://ris.stadt-a.example" }
    },
    "ris-stadt-b": {
      "command": "uv", "args": ["--directory", "/pfad/zu/ratsinfo-mcp", "run", "ratsinfo-mcp"],
      "env": { "RIS_BASE_URL": "https://ris.stadt-b.example" }
    }
  }
}
```

Jede Instanz führt beim Start ihre eigene Fail-early-Prüfung und
Instanz-Erkennung durch (pro Instanz ≈ 5–8 gedrosselte Requests, memoisiert).

## Automatische Erkennung (best effort, beim Start einmalig)

* **SD.NET RIM-Signatur** (Fail-early, `RIS_STRICT`): vier unabhängige Belege —
  HTML-Marker im Root (`sd.net rim` / `sdnetrim` / `sternberg`), OParl
  `/system → name == "SD.NET RIM"`, `/recherche`-Formular mit `reqid`+`csrftoken`,
  ICS-Feed als `text/calendar`. Fehlt die Signatur → `CompatibilityError`
  mit strukturiertem Report (welche Belege fehlen) und Exit-Code 2.
* **Träger-Name** (`RIS_NAME`): OParl `/body/1 → name` (z. B. „Stadt X"),
  Fallback `<title>` — wird in den `source`-Strings der Tools eingeblendet.
* **Vorlagen-Periodisierung**: römische Ratsperioden (`603/IX.`-Stil) vs.
  arabische Lfd.-/Jahresnummern (`1134/2013`-Stil), aus dem OParl-Paper-Index.
  Die OParl-Referenz-Regex ist für beide Schemata breit; die Erkennung dient
  Transparenz/Logging.
* **count-Verhalten** (optional, `verify_count`): verifiziert die RIM-
  Eigenheit „nur `count=50` liefert Treffer" live und meldet Abweichungen.

## Sicherheit / Ressourcenschutz

* **Rate-Limit** zwischen allen Requests (Default 1 req/s) + Backoff bei 429/5xx.
* **SSRF-Whitelist**: PDF-/Detail-URLs nur vom eigenen Instanz-Host.
* **PDF-Guards**: Größen- und Seiten-Caps, In-Memory-LRU-Cache (TTL 1 h).
* **ToS-Guard**: `terms_of_use` lädt nur Seiten desselben Trägers oder solche,
  die der Betreiber selbst verlinkt (First-Party-Discovery).
* **Read-only**: Der Server führt ausschließlich GET/POST-Requests aus, die
  auch der Browser-Client führt; keine Schreiboperationen, keine Sessions
  außerhalb der Anfrage.

## Grenzen

* **Nur SD.NET RIM** (Sternberg). Ältere SD.NET-Versionen oder andere
  Hersteller (z. B. OParl-native Portale) weichen im DOM ab
  (`table0..3`, `row-*`, `top-oeff-data`, `table-details`) und müssten neu
  gemappt werden — der Fail-early-Check erkennt die Abweichung und bricht mit
  klarer Diagnose ab, statt leere Ergebnisse zu liefern.
* `terms_of_use` ruft **kein** zweites LLM auf — es liefert deterministisch
  Text + Keyword-Flags; die semantische Bewertung der Wiederverwendungs-
  Bedingungen liegt beim aufrufenden Agenten.
* Die Recherche liefert pro Suchlauf je Dokumenttyp die **erste Trefferseite**
  (max. 25 Zeilen; RIM-limits). `limit` begrenzt zusätzlich.

## Tests

`uv sync && uv run pytest` — vollständige **Offline**-Testsuite (FakeClient
statt echtem HTTP; Fixturen sind anonymisierte Captured-Antworten einer
Referenz-Instanz, fiktive „Teststadt"). `RIS_DETECT=0` über eine autouse-
Fixture. 186 Tests.
