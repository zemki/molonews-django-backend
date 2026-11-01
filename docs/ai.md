# AI features

All AI features of molo run on **open-weight models on your own hardware**. The
backend talks to an [Ollama](https://ollama.com) server through a single client,
[`molo/llm.py`](../molo/llm.py). No request leaves your infrastructure unless you
point `LLM_BASE_URL` to an external server yourself.

The default model is [gpt-oss](https://ollama.com/library/gpt-oss) (Apache 2.0):

| Model | Memory | Recommended for |
|---|---|---|
| `gpt-oss:20b` | ~16 GB RAM/VRAM | Default; search, tagging, date recognition |
| `gpt-oss:120b` | ~80 GB RAM/VRAM | Scraper generation, highest quality everywhere |

Every feature is optional. Without a reachable Ollama server the backend works
normally; AI-dependent functions fall back or report an error.

## Setup

```bash
# on the AI machine
ollama pull gpt-oss:20b
ollama serve            # listens on http://localhost:11434
```

`.env` of the backend:

```
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=gpt-oss:20b
LLM_CODEGEN_MODEL=gpt-oss:120b   # optional, only for scraper generation
LLM_THINK=low                    # reasoning effort: low, medium, high
LLM_TIMEOUT=120
LLM_NUM_CTX=8192
```

Do not expose the Ollama port to the internet. If Ollama runs on another machine,
restrict access to the backend server (firewall, VPN or reverse proxy with
authentication).

## Features

| Feature | Where | Model call |
|---|---|---|
| Natural-language event search | `rag/`, `POST /api/v4/api/event/ai-search/`, search in the event list | Parses the query into filters (structured output), optional relevance filter |
| Voice input for the search | `POST /api/v4/api/event/transcribe/` | Local [faster-whisper](https://github.com/SYSTRAN/faster-whisper), no LLM |
| Scraper generation | `import_html_articles`, `import_html_events`, admin action on sources | Writes an import script from the page HTML; runs only after a superuser approved it ([importing.md](importing.md#approval)) |
| Date recognition | newadmin event form (date generator) | Turns free text like "every Tuesday 6–8 pm in November" into dates |
| Tag suggestions | newadmin (`autotag.js`), `/api/v4/AI/generate-*-tags/` | Chooses from the existing tags |
| Event tagging | `python manage.py tag_events_local` | Assigns existing tags to untagged events |
| Occurrence correction | `python manage.py convert_broad_occurrences` | Splits implausibly long date ranges |

All calls use Ollama's structured outputs (JSON schema) where the result is
data, so responses are always valid JSON.

### Event search

Enable with `USE_RAG_EVENT_SEARCH=true`. The search is not a vector search: the
model translates a query such as "techno concert in Lübeck at the weekend" into
areas, time window, keywords and tags, which are then matched in PostgreSQL. Only
when the query asks for a general topic or a context ("with my grandma") does a
second call filter the candidates. See [rag/README.md](../rag/README.md) for the
full flow and the evaluation set (`rag/eval_golden.py`).

| Variable | Default | Description |
|---|---|---|
| `USE_RAG_EVENT_SEARCH` | `false` | Use the AI search in the event list |
| `RAG_CACHE_TIMEOUT` | `120` | Seconds a parsed query is cached |
| `RAG_SEARCH_DEBUG` | `false` | Verbose logging |

If the model is unavailable, the search falls back to a plain text search.

### Voice input

```bash
pip install faster-whisper
```

```
SPEECH_TO_TEXT_ENABLED=true
WHISPER_MODEL=small        # tiny, base, small, medium, large-v3
WHISPER_DEVICE=auto        # cpu, cuda or auto
```

The model is downloaded on first use and kept in memory. Without these settings
the endpoint answers with HTTP 503.

### External tagging workers

Instead of tagging on the backend server, a separate worker (e.g. a GPU machine)
can fetch untagged content and submit tags via the API, authenticated with
`AI_TAGGING_API_KEY`. See [api.md](api.md#tagging-worker-interface).

## Choosing another model

Any Ollama model with structured output support can be used by setting
`LLM_MODEL`. Prompts and tests were tuned for gpt-oss; smaller models may produce
weaker search parsing and scrapers. Reasoning models accept `LLM_THINK`; for
other models set `LLM_THINK=` (empty) to disable it.
