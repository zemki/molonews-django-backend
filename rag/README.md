# KI-Event-Suche

Dieses Modul übersetzt natürlichsprachliche Anfragen („Technokonzert in Lübeck am
Wochenende") in Datenbank-Abfragen gegen die Event-Tabellen und filtert die
Ergebnisse bei Bedarf per Sprachmodell nach. Es ist **kein** klassisches RAG mit
Vektor-Embeddings, um Serverlast und Komplexität gering zu halten. Stattdessen:
LLM-Parsing + gestufte Keyword-/Tag-Suche in PostgreSQL.

Alle Modellaufrufe laufen **lokal** über [Ollama](https://ollama.com) und den
zentralen Client `molo/llm.py`. Standardmodell ist `gpt-oss:20b`, auf
leistungsfähiger Hardware empfiehlt sich `gpt-oss:120b`. 

## Dateien

| Datei | Zweck |
|---|---|
| `rag_query.py` | Parser, Query-Builder, Suche, Relevanz-Filter |
| `eval_golden.py` | Golden-Set-Regressionstest für den Parser (siehe unten) |

Einstiegspunkte in der API:

- `api/views/event_ai_search.py` → `POST /api/v4/api/event/ai-search/` (JSON-API,
  wird auch von der Testseite `/api/v4/event/ai-search/` = `aievent.html` genutzt)
- `api/views/event_v4.py` (`perform_rag_search_with_cache`) → Suche in der App,
  mit Parse-Cache und Ergebnis-Cache
- `POST /api/v4/api/event/transcribe/` → optionale Spracheingabe (lokal, siehe unten)

## Ablauf einer Suche

```
Nutzeranfrage (Text)
   │
   ▼
[LLM-Call 1: Parsing]  Structured Output (JSON-Schema, Ollama "format")
   │  Query → EventQueryParams (Orte, Zeitraum, Keywords, Tags, Kontext, Intent)
   ▼
Intent-Gate: not_event_search == true?
   │  ja → Abbruch mit Hinweis („Deine Anfrage sieht nicht nach einer Event-Suche aus")
   ▼
Django-ORM-Query (PostgreSQL)
   │  Keywords haben Vorrang: Volltextsuche mit Wortgrenzen-Regex über alle
   │  keyword_variants (OR innerhalb einer Gruppe, AND zwischen Gruppen)
   ▼
Gestufte Verbreiterung: < MIN_KEYWORD_RESULTS (5) Keyword-Treffer?
   │  → zweite Query nur über die tag_request-Oberkategorie (z. B. „musik"),
   │    Duplikate raus, verbreiterte Events per Content-Filter geprüft,
   │    Keyword-Treffer bleiben vorne (_rank 0 vor 1)
   ▼
[LLM-Call 2: Relevanz-Filter]  Structured Output {"ids": [...]} — läuft NUR in zwei Fällen:
   │  A) Content-Filter: tag_request ohne content_keywords
   │  B) Context-Filter: query_intent contextual/recommendation + companion_context
   ▼
Sortierung (_rank, laufende Events, Startdatum) → JSON-Response
```

## Warum zwei LLM-Calls?

Die beiden Calls lösen unterschiedliche Probleme:

1. **Parsing (immer):** Freitext ist mehrdeutig — „am Wochenende", „mit meiner
   Oma", „was geht heute". Der erste Call macht daraus deterministische
   Filterparameter. Dank Structured Outputs (JSON-Schema als Ollama-`format`)
   kann die Antwort formal nicht kaputt sein.

2. **Relevanz-Filter (nur bei Bedarf):** Die Tag-Zuordnung in der Datenbank ist
   nicht perfekt — eine Lesung mit Hintergrundmusik trägt z. B. den Tag
   „Musik & Konzerte". Sucht jemand nur nach einer Oberkategorie („Konzerte am
   Wochenende"), würde die reine Tag-Suche solche Fehltreffer mitliefern. Der
   zweite Call bewertet Titel + Beschreibungsauszug jedes Kandidaten und wirft
   unpassende raus (Content-Filter) bzw. rankt nach Eignung für den Kontext
   wie „mit Oma" (Context-Filter).

   Bei einer Keyword-Suche („Technokonzert") entfällt der zweite Call: Die
   Wortgrenzen-Volltextsuche ist bereits präzise genug, ein LLM-Nachfiltern
   würde nur Latenz kosten.

Ein einziger großer Call („hier sind 50 Events, beantworte die Frage") wäre
langsamer und würde die DB-Filterung (Datum, Ort, Limit) nicht ersetzen
können — die Datenbank kennt zigtausend Events, ins Prompt passen nur die
vorgefilterten Kandidaten.

## Was bekommt das Sprachmodell?

**Call 1 — Parsing** (immer):
- System-Prompt: Parsing-Regeln, das aktuelle Datum, die Liste der öffentlichen
  Tag-Namen aus der DB, Beispiel-Queries (statisch im Code)
- User-Prompt: die Suchanfrage des Nutzers

**Call 2 — Content-/Context-Filter** (nur Fall A/B oben):
- die Suchanfrage des Nutzers
- pro Kandidat (max. 50): Event-ID, Titel, die ersten 200–300 Zeichen der
  Beschreibung (HTML entfernt), beim Context-Filter zusätzlich der Veranstaltungsort

Geräte-IDs, IP-Adressen oder Accounts werden nicht an das Modell übergeben.
Suchanfragen werden nicht in einer eigenen Logdatei gespeichert; der Logger
`rag_search` protokolliert nur den technischen Ablauf (Konfiguration über
`LOGGING` in den Settings).

## Spracheingabe (optional)

`POST /api/v4/api/event/transcribe/` transkribiert Audio (webm/ogg/wav/m4a)
lokal mit [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Die
Audiodatei wird nur temporär gespeichert und direkt danach gelöscht.

Aktivieren:

```bash
pip install faster-whisper
# .env
SPEECH_TO_TEXT_ENABLED=true
WHISPER_MODEL=small        # tiny | base | small | medium | large-v3
WHISPER_DEVICE=auto        # auto | cpu | cuda
```

Ist die Spracheingabe deaktiviert oder faster-whisper nicht installiert,
antwortet der Endpoint mit HTTP 503.

## Modelle & Konfiguration

Einstellungen in `.env` bzw. den Settings (siehe `molo/llm.py`):

| Variable | Standard | Bedeutung |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:11434` | URL des Ollama-Servers |
| `LLM_MODEL` | `gpt-oss:20b` | Modell für Parsing und Relevanz-Filter (`gpt-oss:120b` für höhere Qualität) |
| `LLM_THINK` | `low` | Reasoning-Stufe von gpt-oss (`low`, `medium`, `high`) |
| `LLM_TIMEOUT` | `120` | Timeout pro Anfrage in Sekunden |
| `USE_RAG_EVENT_SEARCH` | `false` | KI-Suche in der Event-Liste der App aktivieren |
| `RAG_CACHE_TIMEOUT` | `120` | Cache-Dauer für geparste Anfragen und Ergebnisse |
| `DEFAULT_AREA_IDS` | leer | Gebiete, die gelten, wenn weder Anfrage noch Profil einen Ort enthalten |

Modell vorbereiten:

```bash
ollama pull gpt-oss:20b     # oder gpt-oss:120b
```

Weitere Konstante in `rag_query.py`: `MIN_KEYWORD_RESULTS = 5` — Schwelle für
die gestufte Verbreiterung.

Hinweis: Schlägt ein Filter-Call fehl (z. B. Ollama nicht erreichbar), werden
alle Kandidaten ungefiltert zurückgegeben (Fail-safe), sichtbar im Log
`rag_search`. Schlägt das Parsing fehl, fällt die Event-Liste der App auf die
klassische Suche zurück (`RAG_FALLBACK_TO_TRADITIONAL`).

## Tests

```bash
python rag/eval_golden.py           # nur Parser (~20 LLM-Calls)
python rag/eval_golden.py --search  # zusätzlich komplette Suche inkl. DB
```

Vor jeder Änderung an Prompt, Schema oder Modell laufen lassen —
Exit-Code 0 = alle Checks bestanden. Neue Problemfälle als Golden-Case ergänzen.
