# Boardy TODO

Actionable backlog. When the user asks "what's next?", read top-down and propose
the highest-priority unticked item with the trade-off in 2–3 sentences.
For long-form rationale see `secondbrain/memo-boardy-future.md`.

## ✅ Done
- [x] ETL: parse `1) ElencoGiochi.xlsx` → SQLite, regex-split the messy SLEEVE column.
- [x] FastAPI + Anthropic tool-use chat (Sonnet 4.6 default).
- [x] Server-side conversation persistence + dropdown switcher in UI.
- [x] Star-schema refactor (games dim + designers/publishers/categories/mechanics outrigger dims via bridges; sleeve facts).
- [x] Web search: client-side `web_search` tool (Tavily) with trusted-domain allowlist — provider-agnostic.
- [x] `add_game` / `update_game` / `delete_game` / `set_sleeve_requirements` tools with user-confirmation flow.
- [x] Rulebook RAG: pypdf parsing, local sentence-transformers embeddings, brute-force cosine search, page-cited answers.
- [x] Drag-and-drop PDF upload with autocomplete game picker.
- [x] BGG backfill script (`etl/backfill_bgg.py`) using Haiku 4.5.
- [x] BGG backfill v2 via official XML API2 (`etl/bgg_api.py` + `etl/backfill_v2.py`). Awaits BGG token to run.
- [x] Audit log `changes(...)` + integration in all write tools (memo §8). New tool `recent_changes` for the model.
- [x] Delta-based inventory tool `add_to_inventory(width, height, delta, brand?)`.
- [x] System prompt teaches `add_to_inventory` vs `update_inventory` and `recent_changes` for history Qs.
- [x] Pluggable LLM providers (`app/llm.py`): Anthropic + OpenAI-compatible (Ollama/DeepSeek). DeepSeek-chat is the new default — ~10× cheaper than Sonnet.
- [x] Local LLM via Ollama — **archived 2026-04-29**. Provider lives on but disabled in practice: hardware (AMD APU, shared RAM, no NPU support in Ollama) + 7B tool-use quality both insufficient. Re-open only with dGPU or a stronger small model. See `LEARNINGS.md` 2026-04-29 (PM) + `secondbrain/memo-boardy-future.md` §1.
- [x] Sleeve schema v3 (2026-04-29 PM): drop `sleeve_raw`, collapse `'no'`→`'na'`, `sleeve_requirements` reinterpreted as TODO list (rows only for non-sleeved games), cascade-clear in `update_game`, guard rule in `set_sleeve_requirements`. Idempotent migration in `app/schema.py`.
- [x] Import bug fix (2026-04-29 PM): `classify_sleeve` defaulted numeric-only Excel cells to `sleeved` — fixed to `unknown`. 5 games restored from audit log via `etl/fix_misclassified_sleeve.py`.
- [x] Tagged stdout logging of every tool-use round (`[boardy] conv=… round=… …`) for live debugging from the uvicorn terminal.
- [x] Web search reads FULL page (Tavily `raw_content` + `search_depth=advanced` defaults). Snippet was wrong on BGG/sleeveyourgames; full page text fixes it. (2026-05-01)
- [x] Counting bug: list-returning tools now wrap as `{count, items}` so the model transcribes the integer instead of estimating list length. Header/list mismatch ("28 giochi" with list of 29) eliminated. (2026-05-01)
- [x] **`/sleeves` dashboard** (2026-05-01): KPI cards, "Da comprare" table, inventory with inline +/- preset buttons (`-50/-10/+10/+50/+100`), quick-add form, mini-chat with separate `conversation_id`. New endpoints `/sleeves/data`, `/sleeves/inventory/delta`, `/sleeves/inventory/upsert` (audit-source `web:sleeves`). Library got a Buste status pill column + filter; nav `Chat / Libreria / Buste` shared across pages.
- [x] **Frontend rerender bug** (2026-05-01): `web/index.html` only rendered Anthropic-shape histories (`content` as array). DeepSeek/OpenAI shape (`content` string + separate `tool_calls`) was silently skipped → reloaded conversations showed only user bubbles. Now accepts both shapes per turn.
- [x] **Citation suffix cleanup** (2026-05-01): killed the `[↗](url)` pattern. The prompt was teaching the model to write arrow-icon link suffixes; replaced the example with normal `[Value](url)` syntax. Also dropped the dead Anthropic-citation injection in `chat.py` — Tavily-backed `web_search` makes citations the model's own prose now.
- [x] **Skip-reason column + tool surfaces excluded games** (2026-05-03 PM): schema v5 adds `games.description_skip_reason TEXT` (idempotent migration); backfill script writes it on skip/error and clears on success, so re-runs are naturally idempotent. New `--retry-skipped` CLI flag. `search_games_semantic` now returns `{count, items, excluded_count, excluded}`; tool description tells the model to MUST mention the excluded list when non-zero (anti-silent-subset). Also tightened the DeepSeek json_object prompt (single-line strings, ASCII apostrophes, escape rules) and added `_try_repair_json` fallback (curly→ASCII normalize, newline collapse) — kills the deterministic apostrophe bug observed on Memoir/War Chest.
- [x] **Semantic search on `games.description`** (2026-05-03): hybrid SQL-filter + cosine over e5-base embeddings of the BGG description. Schema v4 adds `description_embedding BLOB` + `description_hash TEXT` to `games` (idempotent). New module `app/games_semantic.py` reuses the rulebooks model. New tool `search_games_semantic(query, players?, max_complexity_weight?, max_duration_min?, sleeve_status?, category_contains?, mechanic_contains?, k=10)`. Auto-embed hook in `add_game`/`update_game` (best-effort, never breaks the write). Backfill via `etl/embed_descriptions.py` — 32/56 games have descriptions and are now indexed; the other 24 will be picked up automatically once `backfill_v2` enriches them. System prompt teaches when to pick semantic vs `list_games`.
- [x] **CLAUDE.md refactor** (2026-05-04): trimmed from ~206 → ~50 righe seguendo i principi Claude Code per CLAUDE.md. Struttura: Setup & commands → Where to look (mappa file con 1 riga ciascuno) → Companion docs (incluso `secondbrain/` con regola "non scrivere senza permesso") → Conventions → Environment. Tagliata la prosa architetturale (auto-documentata dal codice); tenute solo regole non deducibili e gotcha. Era TODO medium "Refactor CLAUDE.md", ora chiuso.

## 🔴 High priority
- [ ] **Coverage gap on semantic search** — residual games still without a `description` (currently surfaced via `excluded` in the tool result; check the `description_skip_reason` column for each). Two recovery paths:
  1. **PDF → description** for games whose rulebook is already ingested (HeroQuest et al.). Pull the first 1–2 RAG chunks, ask DeepSeek "riassumi in 3 frasi cosa è questo gioco", write via `update_game(description=...)` — auto-embed handles the rest. Reuses existing infra, ~30 min of work.
  2. **Inline description editor** on `/library` (textarea per row + `POST /games/{id}/description` endpoint). For expansions / Italian-edition variants where BGG has nothing. ~1h.



## 🟡 Medium priority
- [ ] **Fix destructive ETL / upsert re-import** — `etl/import_excel.py` wipes `games` / `sleeve_requirements` / bridges on every run, killing chat-added games (e.g. Concordia). Switch to upsert-by-name so re-imports preserve chat-added rows + inventory. (Merges the old "Re-import without losing chat-added games" item.)
- [ ] **Library v2: thumbnail grid view** — toggle on `/library` between the current dense table and a card grid (cover from `thumbnail_url`, name, players, duration, weight). Useful for visual browsing; the table stays the default for filtering/sorting.
- [ ] **Voice input** via Web Speech API (browser-native, free). Mic button next to "Invia"; Italian recognition is decent in Chrome. (memo §2)
- [ ] **OCR fallback** for scanned-image rulebooks (`pytesseract`); detect zero-text pages and run OCR on those only.
- [ ] **Chunking for tabular rulebooks** — current line-based chunker breaks HeroQuest/TI reference tables. Try a heuristic that keeps consecutive table-like lines together.
- [ ] **Re-import without losing chat-added games** — make ETL upsert by `name` rather than DROP+CREATE, preserving chat-added rows and inventory.

## 🟢 Low priority
- [ ] **Telegram bot** sharing the same `/chat` endpoint (memo §3). Skip WhatsApp.
- [ ] **BGG sleeve-count discovery** — try BGG forums/files for community sleeve guides; current backfill leaves the 22 "sleeved-no-detail" games unfixed.
- [ ] **UI redesign attempt #2** — minimal/Linear-inspired, not skeumorphic-2005. User vetoed the wood+parchment attempt.
- [ ] **Vendor `marked.js` locally** to enable offline-first.

## 📚 Reference
- Detailed rationale: `secondbrain/memo-boardy-future.md`
- Architectural decisions & gotchas: `LEARNINGS.md`
- Code structure: `CLAUDE.md`
