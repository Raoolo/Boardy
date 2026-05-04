# CLAUDE.md

Personal board-game inventory chatbot. Single-user, runs on Windows. Natural-language Q&A over a local SQLite DB + rulebook RAG index.

## Setup & commands

```bash
uv sync                                            # install deps
uv run uvicorn app.main:app --port 8765            # run web app
uv run python etl/import_excel.py                  # destructive re-import (see Conventions)
```

Backfills (run in order on a fresh DB):
```bash
uv run python etl/backfill_v2.py phase1 | phase2 [--auto] | apply --gid N --bgg X
uv run python etl/backfill_descriptions_tavily.py [--only NAME] [--dry-run]
uv run python etl/backfill_descriptions_websearch.py [--only NAME] [--manual "text"] [--dry-run]
uv run python etl/embed_descriptions.py [--force]
```

No test suite — validate by smoke-testing a tool (`uv run python -c "from app.tools import sleeve_summary; print(sleeve_summary())"`) or hitting `POST /chat`. Server has no auto-reload; restart manually after Python changes. `web/` is served live.

## Where to look

- `app/chat.py` — provider-agnostic tool-use loop, up to 8 rounds. Auto-injects `_source="chat:{conv_id}"` into write tools via `inspect.signature`.
- `app/tools.py` — all tools. Adding one = function + JSON schema in `TOOLS` + entry in `TOOL_FUNCS`. Write tools must declare `_source: str | None = None`.
- `app/llm.py` — `Provider` ABC. `AnthropicProvider` (default, `claude-sonnet-4-6` + server-side `web_search_20250305`) and `OllamaProvider`. Selection per-request via `LLM_PROVIDER`.
- `app/schema.py` — star schema DDL + idempotent v1→v2 migration on every boot.
- `app/audit.py` — every write to `games`/`sleeve_requirements`/`sleeve_inventory` logs to `changes`.
- `app/games_semantic.py` — hybrid SQL+cosine over `games.description_embedding`. Reuses `_model_lazy()` from `rulebooks.py` (single 280MB load).
- `app/rulebooks.py` — pypdf chunking + e5 embeddings + brute-force cosine.
- `web/index.html` — single-file UI, vanilla JS + `marked.js`. No build step.

## Companion docs (read before non-trivial work)

- `LEARNINGS.md` — **read first**. Tribal knowledge: gotchas, decisions, user preferences accumulated across sessions.
- `TODO.md` — actionable backlog with priorities. Consult when the user asks "what's next?".
- `secondbrain/memo-boardy-future.md` — long-form rationale behind TODO items. Open when a TODO needs context.
- `secondbrain/` (broader) — the user's Obsidian vault. Notes about Boardy live here; cross-references to other personal projects may exist. Don't write to it without being asked.

## Conventions

- **Reply in the user's language.** Italian for Italian prompts; the user mixes IT/EN freely.
- **Confirm before destructive ops.** `delete_game` and BGG-enriched `add_game`/`update_game` must propose a table and wait for "sì/confermo".
- **`etl/import_excel.py` wipes** `games` / `sleeve_requirements` / bridges. Inventory, conversations, dim tables survive. Chat-added games (e.g. Concordia) disappear on re-import — by design until upsert lands.
- **No "Fonti:" prose sections** after web_search — system prompt forbids it (post-processor mangles them). Inline `[label](url)` only.
- **`add_to_inventory(width, height, delta, ...)` is preferred** over `update_inventory` for purchases/consumption: server-side arithmetic, refuses negative results.
- **`_source` is internal.** Never put it in a tool's JSON schema — chat.py injects it. Otherwise the model can spoof audit origins.
- **Windows console = cp1252.** Scripts printing `→`/`✓`/`↗` must `sys.stdout.reconfigure(encoding="utf-8")` early or run with `PYTHONIOENCODING=utf-8`.
- **E5 multilingual thresholds**: ≥0.78 strong, 0.72–0.77 borderline, <0.72 noise. Lower than English-only — IT/EN trade-off.

## Environment

- `ANTHROPIC_API_KEY` — Anthropic Console key (separate from claude.ai Pro; Pro does NOT include API).
- `LLM_PROVIDER` — `anthropic` (default) or `ollama`. Per-request, no restart.
- `LLM_MODEL`, `OLLAMA_BASE_URL` — optional overrides.
- `BGG_API_TOKEN` — required since 2026-04 (BGG XML API is Cloudflare-gated, both v1 and v2). Public-page scraping via web_search was tried and failed (JS-rendered widgets — see LEARNINGS).
- First run downloads ~1GB to `~/.cache/huggingface/` for the e5 model. Subsequent loads ~3s.
