# Boardy — Learnings & Decisions Log

A running notepad for Claude (and humans) working on this project across sessions.
Append, don't rewrite. Newest entries on top.

---

## 2026-04-28 — Audit log + add_to_inventory tool

### What's new
- Table `changes(id, ts, table_name, row_id, row_label, action, field, old_value,
  new_value, source)` created via `app/schema.py` migrate (idempotent).
- Helper module `app/audit.py` (`log_change` / `log_diff` / `log_full` / `recent`).
  All helpers run inside the caller's existing connection so audit rows share
  the transaction with the mutation — failed UPDATE rolls back its log row too.
- Write tools (`add_game`, `update_game`, `delete_game`, `set_sleeve_requirements`,
  `update_inventory`) now accept a kwarg `_source: str | None`. The kwarg is
  **not** declared in the JSON schema seen by Claude — `app/chat.py` injects it
  automatically using `inspect.signature` (cached per tool) so the model can't
  spoof it.
- `etl/backfill_v2.py` passes `_source="backfill_v2"` for its updates.
- New write tool `add_to_inventory(width_mm, height_mm, delta, brand?, note?)`:
  delta-based, server-side arithmetic, refuses negative results. Use this for
  "ho comprato N buste" — `update_inventory` (absolute count) stays available
  but the prompt should prefer the delta variant to avoid model arithmetic
  errors.
- New read tool `recent_changes(limit, table?, game_name?)`: lets the model
  answer "quando ho aggiunto X?" / "cosa è cambiato di Y?" from the audit log.

### Source convention
- `chat:{conversation_id}` for chat-driven writes
- `chat:?` if conversation_id wasn't passed (legacy callers)
- `backfill_v2` for the BGG-API backfill
- `etl` for `import_excel.py` (not yet wired — direct SQL writes there don't
  hit the audit log; deliberate, since ETL is a destructive bulk reset)
- `manual` / `unknown` for ad-hoc scripts

### Fields excluded from diffs
`updated_at` and `created_at` are filtered out in `audit._IGNORED_FIELDS` —
otherwise every UPDATE would log a noisy timestamp diff.

### Things left for later
- ETL → audit wiring (low priority; ETL is destructive by design).
- A simple "/changes" page in the UI to browse history without going through
  the chat. SQL ad-hoc query works for now.

---

## 2026-04-28 — Backfill v1 post-mortem & switch to BGG XML API2

### What happened with the Haiku + web_search backfill
- **Cost: ~€3 / ~80 tool rounds** for 56 games. Result: 20/56 still missing
  `bgg_id` (16 of those marked "ambiguous BGG match — manual review"), 27/56
  with `bgg_id` but missing `complexity_weight`/`bgg_rating`/categories/mechanics.
- Two structural failures:
  1. **BGG pages are JS-rendered** — weight/rating/category widgets are loaded
     client-side via the internal API, so `web_search_20250305` reads the
     static HTML and gets nothing for those fields. We paid for a search that
     literally cannot return what we need.
  2. **No a-priori disambiguation** — when BGG has multiple editions
     (HeroQuest 1989/1990/2021, base vs expansion for Sagrada/Splendor/Catan,
     etc.), Haiku correctly bailed with "manual review" — but each bail still
     cost ~$0.03. Should have given the model a candidate list up front.

### BGG XML API status (re-tested 2026-04-28)
- **Both v1 (`xmlapi/...`) and v2 (`xmlapi2/...`) are now Bearer-gated** by
  Cloudflare. All anonymous requests → HTTP 401, varying User-Agent doesn't
  bypass it. Confirmed the policy at `boardgamegeek.com/using_the_xml_api`:
  > "Registration and authorization is required for use of the XML API."
- Path forward: register an app, get a bearer token, put it in `.env` as
  `BGG_API_TOKEN`. With token, deterministic backfill is free + fast.
- The earlier note ("BGG XML API is paywalled (401)") referred to the same
  Cloudflare gate — it applies to xmlapi2 too, not just v1.

### New backfill architecture (in `etl/bgg_api.py` + `etl/backfill_v2.py`)
- **No LLM in the loop** for backfill. Pure XML parsing.
- Phase 1: for every game with known `bgg_id`, GET `thing?id=X&stats=1` and
  patch only fields that are currently NULL (preserve manual edits).
- Phase 2: for games without `bgg_id`, GET `search?query=NAME` → list of
  candidates with id+year+type. Human picks via `apply --gid N --bgg X`
  (or `--auto` for single-result/non-expansion hits).
- On-disk cache at `etl/.bgg_cache/` keeps dev cheap; rate-limited at ~0.6s
  between requests (BGG asks ≤2 req/s).
- The complexity_weight → label mapping is now a function (`_label_from_weight`)
  instead of being inlined in a system prompt.

### DB cleanup done today
- Cleared `notes` for the 30 games that still had stale `backfill: ...`
  messages from the v1 run. Two legitimate descriptive notes (7 Wonders Babel,
  Mysterium Refresh) preserved.
- The candidate BGG ids that were mentioned in those notes (e.g. "111661 vs
  316378 for 7 Wonders Cities") will be re-discovered deterministically by
  `xmlapi2/search` in Phase 2, so wiping was safe.

### Convention: don't reuse `web_search_20250305` for structured DB enrichment
- It's good for: looking up a sleeve size on sleevegeeks, fetching a quick fact
  for a chat answer, finding the publisher of a niche game.
- It's bad for: anything that needs structured fields from a JS-rendered
  page (BGG, Asmodee shop, BGG GeekLists, dragonshield product specs).
- For structured BGG data, always go through `etl/bgg_api.py`.

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
