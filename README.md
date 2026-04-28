# Boardy

> A personal board-game inventory chatbot. Ask natural-language questions about your collection — sleeve math, player counts, rules lookups — and get answers grounded in your own data.

Boardy turns a messy Excel inventory into a queryable SQLite database, layers a Claude-powered chat on top, and adds a local rulebook RAG so you can ask things like *"in Dune Imperium, can I buy two cards from Imperium Row?"* and get a page-cited answer.

Single-user, runs on `localhost`, no cloud DB, no build step.

---

## What it can do

- 🎲 **Query your collection** — *"what 4-player engine builders do I own?"*, *"which games take more than 2 hours?"*
- 🛡️ **Sleeve math** — *"how many 63.5×88 sleeves do I need to buy?"* (computes need vs. inventory across all games)
- 📖 **Rulebook Q&A** — drag-and-drop a PDF, get answers cited by page number via local embeddings
- ✏️ **Manage inventory by chat** — add/update/delete games, update sleeve stock with delta-based purchases (*"ho comprato 200 buste 63.5×88"*), confirmation prompts on destructive ops
- 🕒 **Full audit trail** — every write logged with old/new values, source, and timestamp; ask *"quando ho aggiunto Concordia?"* and get the truth from the log
- 🔎 **Trusted web search** — when the model needs external info, it's restricted to BGG, publisher sites, and a sleeve-database allowlist
- 💬 **Persistent conversations** — full chat history server-side, switchable via a dropdown

## Tech stack

| Layer        | Choice                                                                 |
|--------------|------------------------------------------------------------------------|
| Backend      | Python 3.13, FastAPI, uvicorn                                          |
| Database     | SQLite (star schema, no ORM)                                           |
| LLM          | Anthropic Claude Sonnet 4.6 (chat) + Haiku 4.5 (BGG backfill)          |
| Embeddings   | `intfloat/multilingual-e5-base` via `sentence-transformers` (local)    |
| RAG          | `pypdf` + brute-force NumPy cosine over float32 BLOBs                  |
| Frontend     | Vanilla HTML/JS + `marked.js` from CDN (no build, no framework)        |
| Package mgmt | `uv`                                                                   |

## Architecture at a glance

```
                ┌──────────────┐
   Excel ──ETL─▶│              │
                │   SQLite     │◀── star schema:
   PDFs  ──RAG─▶│  boardy.db   │    games + sleeve facts +
                │              │    designer/publisher/
                └──────┬───────┘    category/mechanic dims
                       │
                       ▼
              ┌─────────────────┐
              │  FastAPI tools  │── list_games, sleeve_summary,
              │   (read+write)  │   ask_rules, add_game, ...
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  Claude Sonnet  │── tool-use loop, up to 8 rounds
              │   + web_search  │   trusted-domain allowlist
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  web/index.html │── single-file UI, drag-drop PDFs
              └─────────────────┘
```

## Quick start

```bash
# 1. Install deps
uv sync

# 2. Set your API key in a .env file at repo root
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# 3. Import your Excel inventory (destructive — wipes games + sleeves)
uv run python etl/import_excel.py

# 4. Run the server
uv run uvicorn app.main:app --port 8765

# 5. Open http://localhost:8765
```

> **First run note:** the embedding model (~280MB, ~1GB cache) downloads once to `~/.cache/huggingface/`. Subsequent loads take ~3s.

## Common tasks

```bash
# Backfill BGG metadata via the official XML API2 (free, deterministic).
# Requires BGG_API_TOKEN — register at https://boardgamegeek.com/using_the_xml_api
uv run python etl/backfill_v2.py phase1                # fill known-id games
uv run python etl/backfill_v2.py phase2                # disambiguate unknowns
uv run python etl/backfill_v2.py apply --gid 27 --bgg 699   # manual id pick

# Smoke-test the BGG client without going through the orchestrator
uv run python etl/bgg_api.py thing 316554              # fetch Dune Imperium
uv run python etl/bgg_api.py search HeroQuest          # list candidates

# Smoke-test a tool function without going through chat
uv run python -c "from app.tools import sleeve_summary; print(sleeve_summary())"

# Browse the audit log
uv run python -c "from app.tools import recent_changes; \
                  [print(r) for r in recent_changes(limit=10)]"
```

## Project layout

```
app/
  main.py         FastAPI routes (/chat, /conversations, /rulebooks/upload, ...)
  chat.py         Tool-use loop, system prompt, web_search config, _source injection
  tools.py        Tool definitions (JSON schema) + Python implementations
  schema.py       DDL + idempotent migrations (star schema, rulebooks, changes)
  rulebooks.py    PDF parsing, chunking, embedding, cosine search
  audit.py        Audit-log helpers (log_change / log_diff / log_full / recent)
etl/
  import_excel.py     Excel → SQLite (regex-splits sleeve column)
  bgg_api.py          BGG XML API2 client (parser + cache + bearer auth)
  backfill_v2.py      Deterministic 3-phase BGG backfill orchestrator
  backfill_bgg.py     [DEPRECATED] Haiku + web_search backfill — kept for reference
web/
  index.html      Single-file UI (HTML + CSS + JS inline)
rulebooks/        Uploaded PDFs land here
boardy.db         SQLite database (gitignored)
```

## Design notes

- **Star schema** — games is the dim, sleeve_requirements/sleeve_inventory are facts. Designers, publishers, categories, mechanics live in outrigger dims joined via bridge tables.
- **Idempotent migration** — `app/schema.py` runs on every server boot; if it sees the v1 flat schema it rewrites in place.
- **No vector DB** — at <10k chunks, NumPy brute-force cosine over L2-normalized float32 BLOBs is plenty fast and removes a dependency.
- **Server-side web search** — `web_search_20250305` is executed by Anthropic, not by us; we just supply the `allowed_domains`.
- **Citations survive JSON round-trips** — text blocks with citations get an inline `[↗](url)` suffix so they render in Markdown without losing the source.
- **No tests** — validation is by smoke-testing tool functions and curling `/chat`. This is a single-user weekend project, not production.

## What's next

See [`TODO.md`](TODO.md) for the prioritized backlog. Highlights:

- Semantic search over game descriptions (*"ho voglia di un gioco di esplorazione spaziale"*)
- Inline footnote-style citations instead of `[↗]`
- Inventory bulk-edit UI

## License

Personal project, no license declared. If you find it useful, fork freely; don't expect support.
