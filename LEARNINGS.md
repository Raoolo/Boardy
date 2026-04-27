# Boardy — Learnings & Decisions Log

A running notepad for Claude (and humans) working on this project across sessions.
Append, don't rewrite. Newest entries on top.

---

## 2026-04-27 — Initial build session

### User preferences
- **Replies in Italian** when the user writes in Italian; conversational tone, no excessive hedging.
- **Concise prose by default**; tables only when comparing multi-attribute items; emojis sparingly when they aid scanning (✅/❌/🎲/📕).
- **NO "Fonti:" sections** — Sonnet has a strong bias toward writing them after web_search; the post-processor mangles them. Use inline `[label](url)` links and a single `[↗](url)` suffix on cited sentences. Reinforce via system prompt; model still drifts occasionally.
- **Cheapest path preferred** — user accepts local infra (sentence-transformers ~280MB) over paid alternatives (Voyage/OpenAI embeddings).
- **Same key for all models**: user has a single `ANTHROPIC_API_KEY` from console.anthropic.com (separate from claude.ai Pro). API billing is pay-per-token.
- **No emoji avalanches** — single emoji per line max, never decorative.

### Architectural decisions
- **BGG XML API is paywalled (401)** as of 2026-04-27 (Cloudflare). Don't try to integrate — use Anthropic `web_search_20250305` server-tool with a trusted-domain allowlist (`app/chat.py:ALLOWED_DOMAINS`). web_search reads full pages, not just snippets.
- **Star schema** chosen over flat `games` table after user explicitly asked for "data engineer" view: `games` (dim) + outrigger dims (designers/publishers/categories/mechanics) via bridge tables; sleeve_requirements/inventory are facts. v1 → v2 auto-migration in `app/schema.py`.
- **Local embeddings** for rulebook RAG: `intfloat/multilingual-e5-base`, brute-force cosine over float32 BLOBs (no `sqlite-vec` — overkill at our scale).
- **Sonnet 4.6 for chat, Haiku 4.5 for batch backfill** — Haiku is ~3× cheaper and fine for structured "extract from BGG" tasks.
- **Server-side conversation persistence**: `conversations(history_json)` table; browser keeps only `conversation_id` in localStorage. Cross-device-ready.

### Gotchas
- **Windows console encoding** is cp1252 by default; any script printing Unicode (→, ✓, etc.) must `sys.stdout.reconfigure(encoding="utf-8")` early or set `PYTHONIOENCODING=utf-8`. See `etl/backfill_bgg.py`.
- **ETL is destructive** on `games`, `sleeve_requirements`, and bridges — re-running `etl/import_excel.py` wipes any game added via chat (e.g. Concordia). Inventory is also wiped. Conversations and dim tables (designers/publishers/...) survive.
- **pypdf page count > visible pages** — pypdf includes blank/cover pages. The Dune Imperium PDF reports 17 visible but 20 to pypdf. Cosmetic only.
- **Server runs as Claude Code background task** on port 8765. Killing the Claude Code session kills the server. For persistent operation, user runs `uv run uvicorn app.main:app --port 8765` from their own terminal.
- **`marked.js` from CDN** is used for Markdown rendering in chat bubbles. If we ever go offline-first, vendor it locally.
- **Web search citation blocks** arrive as separate `text` blocks with a `citations` field. We append `[↗](url)` from the first citation; multi-citation handling is naive.

### Tool catalog (as of this session)
13 tools live in `app/tools.py`:
- Read: `list_games`, `get_game`, `sleeve_summary`, `list_inventory`, `list_dimension`, `list_rulebooks`
- Write: `add_game`, `update_game`, `delete_game`, `set_sleeve_requirements`, `update_inventory`, `ingest_rulebook`
- RAG: `ask_rules` (returns top-k chunks; the calling Claude synthesizes the answer)
Plus Anthropic server tools: `web_search_20250305` (allowlisted).

### Cost benchmarks (Apr 2026 pricing)
- Backfill 1 game with Haiku 4.5 + 1 web_search: ~14s, ~18k tok in / 1k tok out, ≈ $0.033.
- Rulebook ingest (17-page PDF): ~3s on cached embedding model.
- Rulebook query: ~50ms search + 1 Sonnet call (~$0.005).
- Embedding model first download: ~1GB to `~/.cache/huggingface/`, ~30s on first run.

### Things to fix next time you touch the area
- Rulebook chunker is line-based; works for prose, breaks tabular content (HeroQuest tables, Twilight Imperium reference cards). Switch to layout-aware chunking when needed.
- Sleeve sizes `63×88` vs `63.5×88` are stored as separate rows; some old Excel data has the imprecise 63 form. Could normalize but rare.
- 22 games imported as `sleeve_status='sleeved'` have no per-size breakdown — backfill from BGG won't fix this; needs measuring physical cards.
