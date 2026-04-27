# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Boardy is a personal board-game inventory chatbot. The user asks questions in natural language ("how many 63.5×88 sleeves do I need to buy?", "what 4-player engine builders do I own?", "in Dune Imperium can I buy two cards from Imperium Row?") and Claude answers by calling tools that hit a local SQLite DB and a local rulebook RAG index. Single-user, runs on the user's Windows laptop.

## Companion docs to read

- `LEARNINGS.md` — gotchas, decisions, user preferences accumulated across sessions. **Read this first** when picking up the project; it's the closest thing to "tribal knowledge".
- `TODO.md` — actionable backlog with priorities. When the user asks "what's next?", consult this.
- `secondbrain/memo-boardy-future.md` — long-form rationale behind the TODO items.

## Commands

```bash
# One-time setup (uv installs from pyproject.toml + uv.lock)
uv sync

# Initial data import (destructive — wipes games/sleeves/bridges, preserves
# conversations + dim tables). Re-run after editing the Excel.
uv run python etl/import_excel.py

# Run the web app (localhost:8765)
uv run uvicorn app.main:app --port 8765

# Backfill BGG metadata for all games missing bgg_id (Haiku 4.5, ~$0.03/game)
uv run python etl/backfill_bgg.py --auto
uv run python etl/backfill_bgg.py --only "Wingspan"   # one game

# Smoke-test a tool function without going through chat
uv run python -c "from app.tools import sleeve_summary; print(sleeve_summary())"
```

There is **no test suite**. Validation is done by smoke-testing tools or hitting `POST /chat` with curl.

The server has no built-in restart-on-edit; restart manually after Python changes.
HTML/CSS/JS changes in `web/` are live (FastAPI serves the file fresh each request).

## Architecture

### Stack
- **Python 3.13** + **FastAPI** + **uvicorn** (single worker; threadpool handles concurrency)
- **SQLite** (one file: `boardy.db`) — no ORM, plain `sqlite3` with row factory
- **Anthropic SDK** — `claude-sonnet-4-6` for chat, `claude-haiku-4-5-20251001` for batch backfill, plus the server-side **`web_search_20250305`** tool with a trusted-domain allowlist
- **sentence-transformers** (`intfloat/multilingual-e5-base`) — local embeddings, free forever, ~280MB downloaded once to `~/.cache/huggingface/`
- **pypdf** for rulebook parsing
- **Vanilla HTML/JS** + `marked.js` from CDN — no build step, no framework

### Database (star schema)

```
games (DIM)              ── designers (outrigger DIM via game_designers bridge)
                         ── publishers (outrigger DIM via game_publishers)
                         ── categories (outrigger DIM via game_categories)
                         ── mechanics  (outrigger DIM via game_mechanics)

sleeve_requirements (FACT, granularity = game × sleeve size)
sleeve_inventory   (FACT, granularity = sleeve size)

rulebooks            ── rulebook_chunks (text + float32 embedding BLOB)
conversations        — chat history JSON, server-side persistence
```

`app/schema.py` runs an idempotent **migration on every server boot**: detects the v1 flat schema (column `producer` exists in `games`) and rewrites it to the v2 star schema, splitting CSV producer/publisher into bridge rows. Subsequent runs are no-ops.

### Chat loop (the heart of the app)

`app/chat.py:chat()` runs an Anthropic tool-use loop with up to 8 rounds:

1. POST `/chat` with `{message, conversation_id?}` → if no `conversation_id`, creates a fresh row in `conversations`.
2. Loads history from DB, appends the user message, calls `client.messages.create(tools=[WEB_SEARCH_TOOL, *TOOLS], system=SYSTEM_PROMPT)` with `cache_control` on the system prompt.
3. While `stop_reason == "tool_use"`: invoke each tool from `TOOL_FUNCS`, append results, loop.
4. When the model finishes, extract text blocks; for each `text` block with citations, append `[↗](url)` so the source survives the JSON round-trip (the frontend renders Markdown).
5. Save updated history to DB.

`web_search_20250305` is a **server-side tool** — Anthropic executes it transparently; we don't dispatch it. Its `allowed_domains` list (in `app/chat.py`) restricts to BGG, sleevegeeks, sleeveyourgames, dragonshield, mayday-games, asmodee, cmon, stonemaier, en.wikipedia.org, and a few publishers.

### Tools (`app/tools.py`)

Read-only: `list_games` (filterable by name/players/complexity/sleeve_status/designer/publisher/category/mechanic), `get_game`, `sleeve_summary`, `list_inventory`, `list_dimension`, `list_rulebooks`.

Write: `add_game`, `update_game`, `delete_game`, `set_sleeve_requirements`, `update_inventory`, `ingest_rulebook`.

RAG: `ask_rules` returns the top-k chunks; the calling Claude synthesizes the final answer and cites pages.

`add_game` / `update_game` accept arrays for `designers`, `publishers`, `categories`, `mechanics` — `_set_bridges()` upserts the dim row and replaces the bridge rows in one go.

### Rulebook RAG (`app/rulebooks.py`)

`ingest(game_name, pdf_path)`:
1. `pypdf` extracts per-page text → list of `(page_no, text)`.
2. `_chunk_pages()` slides a ~350-token window with 60-token overlap, **preserving page boundaries** so each chunk knows its `page_start`/`page_end`.
3. Embed all chunks with the e5 model (`passage:` prefix per the e5 convention).
4. Store as raw float32 bytes in `rulebook_chunks.embedding`.

`search(game_name, query, k=5)`:
- Embed the query (`query:` prefix).
- Brute-force cosine over all chunks for that game (vectors are L2-normalized → dot == cosine).
- At our scale (≲ 10k chunks total) this is fast in pure NumPy; no need for `sqlite-vec`.

The model is **lazy-loaded** in a module-global to avoid the ~25s first-load on server boot when no one is asking rules questions.

### Frontend (`web/index.html`)

Single file. Header has a conversation dropdown + new/delete buttons. Chat area renders via `marked.parse()` for bot bubbles, `textContent` for user bubbles (XSS-safe).

Drag-and-drop: dragging a PDF anywhere on the page opens a modal with autocomplete from `/games/names`. Confirm → `POST /rulebooks/upload` (multipart) → server saves to `rulebooks/`, calls `ingest()`, returns chunk count.

`localStorage` stores only `boardy_conv_id`; full history lives server-side. Switching conversations re-fetches from `/conversations/{id}`.

## Conventions that matter

- **Reply in the user's language.** Italian for Italian prompts. The user codes in English but converses in Italian.
- **No "Fonti:" prose sections** after web_search results — Sonnet is biased toward writing them; the prompt forbids it explicitly because the post-processor mangles them. Use inline `[label](url)` instead.
- **User confirms destructive ops** — `delete_game`, BGG-enriched `add_game`/`update_game` propose values in a table and wait for "sì/confermo" before calling the tool.
- **ETL is destructive** on `games`/`sleeve_requirements`/bridges. Re-running `etl/import_excel.py` wipes any chat-added games. Inventory and `conversations` survive; dim tables persist (rows are additive-only). Don't be surprised when `Concordia` disappears after a re-import — that's by design until we add upsert (TODO.md, medium priority).
- **Windows console encoding is cp1252.** Any script printing `→`, `✓`, `↗`, etc. must `sys.stdout.reconfigure(encoding="utf-8")` early or run with `PYTHONIOENCODING=utf-8`. See `etl/backfill_bgg.py`.
- **Embedding model first run downloads ~1GB** to `~/.cache/huggingface/`. Subsequent loads are ~3s.
- **API keys**: the user's `ANTHROPIC_API_KEY` (in `.env`) works for all models — it's a single Anthropic Console key, separate from claude.ai Pro. Pro does NOT include API access.

## Files you'll touch most

- `app/tools.py` — adding a tool means: function + JSON schema in `TOOLS` + entry in `TOOL_FUNCS`. The model picks them up on next chat call (no server restart needed if just editing function bodies; restart for new TOOLS schemas).
- `app/chat.py:SYSTEM_PROMPT` — the model's behavioral spec. Keep it tight and explicit; Sonnet drifts on subtle rules.
- `app/schema.py` — DDL for the star schema. Add new tables here, with `CREATE TABLE IF NOT EXISTS` so `migrate()` stays idempotent.
- `web/index.html` — single-file UI. CSS + JS inline. No build step.
