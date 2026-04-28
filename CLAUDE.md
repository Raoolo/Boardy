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

# Backfill BGG metadata via official XML API2 (deterministic, free, requires BGG_API_TOKEN)
uv run python etl/backfill_v2.py phase1                # fill missing fields for games with bgg_id
uv run python etl/backfill_v2.py phase2 [--auto]       # search BGG for games without bgg_id
uv run python etl/backfill_v2.py apply --gid N --bgg X # manual id pick after phase2

# Legacy (deprecated, expensive): Haiku + web_search backfill — kept for reference only.
# uv run python etl/backfill_bgg.py --auto

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
- **LLM**: pluggable via `app/llm.py` (`Provider` ABC + factory). Two implementations:
  - **AnthropicProvider** (default) — `claude-sonnet-4-6` for chat, plus the server-side `web_search_20250305` tool with a trusted-domain allowlist. `claude-haiku-4-5-20251001` is used by the legacy `etl/backfill_bgg.py`.
  - **OllamaProvider** — local Ollama via OpenAI-compatible endpoint (`http://localhost:11434/v1`). Tested with `qwen2.5:7b-instruct`. No web_search; relies on `etl/backfill_v2.py` for BGG metadata.
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
changes              — audit log of every write (table, row_id, field, old/new, source, ts)
```

`app/schema.py` runs an idempotent **migration on every server boot**: detects the v1 flat schema (column `producer` exists in `games`) and rewrites it to the v2 star schema, splitting CSV producer/publisher into bridge rows. Subsequent runs are no-ops.

### Chat loop (the heart of the app)

`app/chat.py:chat()` runs a provider-agnostic tool-use loop with up to 8 rounds:

1. POST `/chat` with `{message, conversation_id?}` → if no `conversation_id`, creates a fresh row in `conversations`.
2. Loads history from DB, appends the user message, calls `provider.run_turn(history, system_prompt, TOOLS)`. The provider is selected by `LLM_PROVIDER` env var (default `anthropic`).
3. While `stop_reason == "tool_use"`: invoke each tool from `TOOL_FUNCS`, append results via `provider.tool_result_history_entries(...)`, loop.
4. When the model finishes, extract text blocks; for each `TextBlock` with citations, append `[↗](url)` so the source survives the JSON round-trip (the frontend renders Markdown). Citations are produced only by Anthropic's `web_search`; Ollama returns plain text.
5. Save updated history to DB.

The system prompt is built dynamically in `_build_system_prompt(supports_web_search)`: a base block that's identical across providers, plus one of two addenda — `WEBSEARCH` (Anthropic) or `NO_WEBSEARCH` (Ollama, telling the model not to invent BGG metadata and to suggest `backfill_v2.py` instead).

### LLM provider layer (`app/llm.py`)

Single source of truth for "how do we call the model". Three pieces:
- **Vendor-neutral content blocks** — `TextBlock`, `ToolUseBlock`, and `ProviderResponse(stop_reason, blocks, assistant_history_entry)`. Modeled after Anthropic's shape because that was already the storage format; OllamaProvider translates on input/output to keep history backwards-compatible.
- **`Provider` ABC** with two methods: `run_turn(...)` (one model call) and `tool_result_history_entries(...)` (how to feed tool results back; Anthropic packs into one user message, OpenAI wants one role=tool message per result).
- **`AnthropicProvider`** owns the `WEB_SEARCH_TOOL` config (`allowed_domains` allowlist) and `cache_control`. **`OllamaProvider`** translates Anthropic-shaped `TOOLS` (input_schema) → OpenAI's function-calling envelope (parameters), and on history reads handles BOTH shapes (`_history_to_openai`) so a conversation started under Anthropic continues correctly under Ollama.

Switching providers is **a per-request decision**: `get_provider()` is called inside `chat()`, so flipping `LLM_PROVIDER` and POSTing again uses the new provider without restart. Server still needs restart for code changes elsewhere.

`web_search_20250305` is a **server-side tool** — Anthropic executes it transparently; we don't dispatch it. Its `allowed_domains` list (in `AnthropicProvider`) restricts to BGG, sleevegeeks, sleeveyourgames, dragonshield, mayday-games, asmodee, cmon, stonemaier, en.wikipedia.org, and a few publishers.

### Tools (`app/tools.py`)

Read-only: `list_games` (filterable by name/players/complexity/sleeve_status/designer/publisher/category/mechanic), `get_game`, `sleeve_summary`, `list_inventory`, `list_dimension`, `list_rulebooks`, `recent_changes`.

Write: `add_game`, `update_game`, `delete_game`, `set_sleeve_requirements`, `update_inventory`, `add_to_inventory` (delta), `ingest_rulebook`.

RAG: `ask_rules` returns the top-k chunks; the calling Claude synthesizes the final answer and cites pages.

`add_game` / `update_game` accept arrays for `designers`, `publishers`, `categories`, `mechanics` — `_set_bridges()` upserts the dim row and replaces the bridge rows in one go.

`add_to_inventory(width, height, delta, brand?, note?)` is the **preferred** way to record purchases / consumption — server-side arithmetic so the model can't get `new = old + bought` wrong, and refuses negative results. `update_inventory` (absolute count) stays for explicit recounts.

### Audit log (`app/audit.py`, `changes` table)

Every write to `games`, `sleeve_requirements`, `sleeve_inventory` produces audit rows via `audit.log_change` / `log_diff` / `log_full`. Helpers run inside the caller's existing `sqlite3.Connection` so audit rows share the transaction with the mutation — failed UPDATE rolls back its own log row.

`source` field convention: `chat:{conversation_id}` (auto-injected by `app/chat.py` via `inspect.signature` — kwarg `_source` is **not** declared in the JSON tool schema so the model can't spoof it), `backfill_v2`, `manual`, `unknown`. `import_excel.py` intentionally bypasses the log because it's a destructive bulk reset.

`updated_at` and `created_at` are explicitly excluded from diffs (`audit._IGNORED_FIELDS`) to avoid noisy timestamp-only rows.

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
- **Provider selection** (`.env`): `LLM_PROVIDER=anthropic` (default) or `ollama`. Optional: `LLM_MODEL=...` to override the per-provider default (Anthropic: `claude-sonnet-4-6`; Ollama: `qwen2.5:7b-instruct`), `OLLAMA_BASE_URL=...` if Ollama runs elsewhere. Switching is instant (no restart) — the factory runs per request.
- **BGG XML API is paywalled** since 2026-04 — both v1 (`xmlapi/...`) and v2 (`xmlapi2/...`) require a bearer token (Cloudflare-gated). Register at `boardgamegeek.com/using_the_xml_api`, set `BGG_API_TOKEN=...` in `.env`. Without it, `etl/backfill_v2.py` errors with an explicit message; the public webpage scrape via `web_search` was tried in v1 and failed (JS-rendered widgets — see LEARNINGS).
- **Audit kwarg `_source` is internal.** Write tools accept it but it must not appear in the JSON schemas inside `TOOLS` — `app/chat.py` injects it via introspection so the model never sees or sets it. If you add a new write tool, declare `_source: str | None = None` and let chat.py handle propagation.

## Files you'll touch most

- `app/tools.py` — adding a tool means: function + JSON schema in `TOOLS` + entry in `TOOL_FUNCS`. The model picks them up on next chat call (no server restart needed if just editing function bodies; restart for new TOOLS schemas). Write tools should accept `_source: str | None = None` so the audit log learns the origin automatically. Schemas use Anthropic's `input_schema` shape; `OllamaProvider._tool_anthropic_to_openai` wraps it for OpenAI on the fly — no duplication.
- `app/chat.py` — `SYSTEM_PROMPT_BASE` + addenda (`WEBSEARCH` / `NO_WEBSEARCH`). Keep base provider-agnostic; only mention web_search inside the addendum, otherwise Ollama mode advertises a non-existent capability.
- `app/llm.py` — provider abstraction. Add a new provider (e.g. Groq, OpenRouter) by subclassing `Provider`, implementing `run_turn` + `tool_result_history_entries`, and registering it in `get_provider()`.
- `app/schema.py` — DDL for the star schema. Add new tables here, with `CREATE TABLE IF NOT EXISTS` so `migrate()` stays idempotent.
- `app/audit.py` — audit-log helpers. Touch when changing what we log (e.g. adding a new ignored field) or when wiring a new write path.
- `etl/bgg_api.py` — BGG XML API2 client. Touch when BGG adds new fields we want to capture or when changing the cache strategy.
- `web/index.html` — single-file UI. CSS + JS inline. No build step.
