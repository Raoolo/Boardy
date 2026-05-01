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

## 🔴 High priority
- [ ] **AI-ready: embeddings on `games.description`** for semantic search ("ho voglia di un gioco di esplorazione spaziale"). Reuse the rulebooks embedding pipeline; one pass when description is set/updated. (memo §8)
- [ ] **Frontend citation polish** — the `[↗](url)` suffix is ugly; convert text-block citations into superscript footnotes with a sources panel at the bottom of each bot bubble. Now that web_search is client-side (Tavily), the citation source is `web_search` tool results, not Anthropic's `assistant.content[].citations` — adapt accordingly.

## 🟡 Medium priority
- [ ] **Library v2: thumbnail grid view** — toggle on `/library` between the current dense table and a card grid (cover from `thumbnail_url`, name, players, duration, weight). Useful for visual browsing; the table stays the default for filtering/sorting.
- [ ] **Inventory editing UI** (forms, not chat). Bulk-update sleeve stock after a shopping run. (memo §7)
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
