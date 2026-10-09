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

## 8 Tools

| Tool | Zweck |
|------|-------|
| `recherche` | Volltext-Recherche **inkl. indizierter PDF-Volltexte** (Fundstelle mit `<mark>`); optional mit OParl-Metadaten-Anreicherung (Papers) bzw. OParl-Personen-Anreicherung bei `dokumenttyp=personen` |
| `pdf_as_markdown` | Lädt ein RIS-PDF und liefert den Volltext als Markdown (Seiten-/Begriffs-Filter, Zeichen-Cap) |
| `meeting_documents` | Metadaten einer Sitzung (`/tops/`) oder eines Vorgangs (`/vorgang/`) inkl. aller PDF-URLs, pro TOP gruppiert |
| `find_meetings` | Sitzungen aus dem ICS-Termin-Feed (Gremium, Datum, Ort, `tops_url`) |
| `find_person` | Strukturierte Personensuche: OParl-Personen-Index der Instanz (Name, Fraktion, Gremien, Mandatszeit, `oparl_id`) + HTML-Personenindex als Komplement; ohne Adresse (Datenminimierung) |
| `person_steckbrief` | Vorgefertigte Personen-Steckbrief-Karte (Markdown/HTML), deterministisch gerendert, ohne zweites LLM; mehrdeutige Namen liefern Kandidaten statt einer Karte |
| `committee_members` | Mitglieder eines Gremiums (`/committee/{id}/person`), Gremiennamen normalisiert aufgelöst |
| `terms_of_use` | Nutzungsbedingungen/Impressum/Datenschutz der Instanz: Discovery (ToS-Links finden) + Extraktion (Haupttext + Keyword-Flags) — damit das aufrufende LLM selbst prüfen kann, unter welchen Bedingungen Dokumente zitiert/aufbereitet werden dürfen |

Typischer Agent-Fluss: `recherche` → (Treffer mit `detail_url`/TOP) →
`meeting_documents` → `pdf_as_markdown` auf das eine PDF. Bei
Wiederverwendungsfragen: `terms_of_use`. Personen-Fluss:
`find_person` → `person_steckbrief` (einmalig aufgelöst) bzw.
`committee_members` (Gremien) und `recherche(volltext=<name>)` für
Dokumente, in denen die Person vorkommt.

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
| `OPARL_ENRICH` | `0` | `1` = OParl-Metadaten-Anreicherung der Suchtreffer (Papers) + OParl-Personen-Anreicherung bei `dokumenttyp=personen` (verlängert Laufzeit) |
| `OPARL_MAX_PAGES` | `8` | OParl: max. Paper-Index-Seiten für die Anreicherung |
| `OPARL_PERSON_MAX_PAGES` | `25` | OParl: max. Person-Index-Seiten (≈ 500 Personen) für `find_person`/`person_steckbrief` |
| `OPARL_PERSON_TTL` | `86400` (24 h) | TTL des OParl-Personen-/Gremien-Index (Personen ändern sich seltener als Vorlagen) |

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
* **Nur RIM-Daten, keine externen Quellen**: Alle Requests gehen ausschließlich
  an den konfigurierten Instanz-Host — OParl ist das Webdienst-Subsystem der
  *selben* Instanz (gleicher Host, eigener Datenbestand). Keine
  Stadt-Websites, keine Portale, keine Cross-Instanz-Verknüpfungen.
* **Datenminimierung (Personen)**: `find_person`/`person_steckbrief` liefern
  nur, was das RIM selbst öffentlich abbildet (Name, Personenkreis/Fraktion,
  Gremien, Mandatszeit); **Adressen werden bewusst nicht mitgeliefert** — sie
  bleiben auf der Personenseite (`personen_url`), die verlinkt wird.
* **Personen-Index-Caches**: OParl-Personen- und Gremien-Index werden
  seitenbegrenzt geladen und mit TTL gecacht (`OPARL_PERSON_MAX_PAGES`,
  `OPARL_PERSON_TTL`) — deutlich geduldiger mit der Ziel-Infrastruktur als
  Vorlagen, die sich häufiger ändern.

## Grenzen

* **Nur SD.NET RIM** (Sternberg). Ältere SD.NET-Versionen oder andere
  Hersteller (z. B. OParl-native Portale) weichen im DOM ab
  (`table0..3`, `row-*`, `top-oeff-data`, `table-details`) und müssten neu
  gemappt werden — der Fail-early-Check erkennt die Abweichung und bricht mit
  klarer Diagnose ab, statt leere Ergebnisse zu liefern.
* `terms_of_use` ruft **kein** zweites LLM auf — es liefert deterministisch
  Text + Keyword-Flags; die semantische Bewertung der Wiederverwendungs-
  Bedingungen liegt beim aufrufenden Agenten. Gleiches gilt für
  `person_steckbrief`: deterministische Vorlage, keine semantische Bewertung.
* Die Recherche liefert pro Suchlauf je Dokumenttyp die **erste Trefferseite**
  (max. 25 Zeilen; RIM-limits). `limit` begrenzt zusätzlich.
* `find_person`/`person_steckbrief` sind **best effort** über die OParl-
  Personen-Endpoints: Liegt `/body/1/person` nicht vor (ältere Instanz) oder
  liefert es nichts, fällt das Tool auf den HTML-Personenindex zurück
  (Name + Personenkreis + `personen_url`, ohne OParl-Metadaten). Die
  HTML-Spalten der Personen-Tabelle (Name/Personenkreis) sind DOM-abhängig;
  das Anschriftenfeld wird bewusst nicht gelesen — es bleibt auf der
  verlinkten Personenseite.

## Tests

`uv sync && uv run pytest` — vollständige **Offline**-Testsuite (FakeClient
statt echtem HTTP; Fixturen sind anonymisierte Captured-Antworten einer
Referenz-Instanz, fiktive „Teststadt"). `RIS_DETECT=0` über eine autouse-
Fixture. 186 Tests.
