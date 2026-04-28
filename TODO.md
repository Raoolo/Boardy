# Boardy TODO

Actionable backlog. When the user asks "what's next?", read top-down and propose
the highest-priority unticked item with the trade-off in 2–3 sentences.
For long-form rationale see `secondbrain/memo-boardy-future.md`.

## ✅ Done
- [x] ETL: parse `1) ElencoGiochi.xlsx` → SQLite, regex-split the messy SLEEVE column.
- [x] FastAPI + Anthropic tool-use chat (Sonnet 4.6 default).
- [x] Server-side conversation persistence + dropdown switcher in UI.
- [x] Star-schema refactor (games dim + designers/publishers/categories/mechanics outrigger dims via bridges; sleeve facts).
- [x] Web search restricted to trusted board-game domains; citations as inline links.
- [x] `add_game` / `update_game` / `delete_game` / `set_sleeve_requirements` tools with user-confirmation flow.
- [x] Rulebook RAG: pypdf parsing, local sentence-transformers embeddings, brute-force cosine search, page-cited answers.
- [x] Drag-and-drop PDF upload with autocomplete game picker.
- [x] BGG backfill script (`etl/backfill_bgg.py`) using Haiku 4.5.
- [x] BGG backfill v2 via official XML API2 (`etl/bgg_api.py` + `etl/backfill_v2.py`). Awaits BGG token to run.
- [x] Audit log `changes(...)` + integration in all write tools (memo §8). New tool `recent_changes` for the model.
- [x] Delta-based inventory tool `add_to_inventory(width, height, delta, brand?)`.

## 🔴 High priority
- [ ] **AI-ready: embeddings on `games.description`** for semantic search ("ho voglia di un gioco di esplorazione spaziale"). Reuse the rulebooks embedding pipeline; one pass when description is set/updated. (memo §8)
- [ ] **Frontend citation polish** — the `[↗](url)` suffix is ugly; convert text-block citations into superscript footnotes with a sources panel at the bottom of each bot bubble. Backend already passes citations in the `assistant.content[].citations` blocks; wire them in `web/index.html`.
- [ ] **System prompt** — teach Claude to prefer `add_to_inventory` (delta) over `update_inventory` (absolute) for purchases, and to call `recent_changes` for "quando/cosa è cambiato" questions.

## 🟡 Medium priority
- [ ] **Inventory editing UI** (forms, not chat). Bulk-update sleeve stock after a shopping run. (memo §7)
- [ ] **Voice input** via Web Speech API (browser-native, free). Mic button next to "Invia"; Italian recognition is decent in Chrome. (memo §2)
- [ ] **OCR fallback** for scanned-image rulebooks (`pytesseract`); detect zero-text pages and run OCR on those only.
- [ ] **Chunking for tabular rulebooks** — current line-based chunker breaks HeroQuest/TI reference tables. Try a heuristic that keeps consecutive table-like lines together.
- [ ] **Re-import without losing chat-added games** — make ETL upsert by `name` rather than DROP+CREATE, preserving chat-added rows and inventory.

## 🟢 Low priority
- [ ] **Local LLM swap** (Ollama + Qwen2.5/Llama3) once usage is high enough that API cost matters. Tool-use quality drops on 7–8B; full-DB-in-prompt fallback documented in memo §1.
- [ ] **Telegram bot** sharing the same `/chat` endpoint (memo §3). Skip WhatsApp.
- [ ] **BGG sleeve-count discovery** — try BGG forums/files for community sleeve guides; current backfill leaves the 22 "sleeved-no-detail" games unfixed.
- [ ] **UI redesign attempt #2** — minimal/Linear-inspired, not skeumorphic-2005. User vetoed the wood+parchment attempt.
- [ ] **Vendor `marked.js` locally** to enable offline-first.

## 📚 Reference
- Detailed rationale: `secondbrain/memo-boardy-future.md`
- Architectural decisions & gotchas: `LEARNINGS.md`
- Code structure: `CLAUDE.md`
