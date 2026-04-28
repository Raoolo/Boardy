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
- [x] System prompt teaches `add_to_inventory` vs `update_inventory` and `recent_changes` for history Qs.

## 🔴 High priority
- [ ] **Local LLM via Ollama: in progress, two open issues** (resume here next session). Provider abstraction is done in `app/llm.py` (`AnthropicProvider` + `OllamaProvider`), `LLM_PROVIDER=ollama` works end-to-end, slim prompt + Modelfile (`boardy-qwen.Modelfile` with `num_ctx=8192`, `temperature=0.3`) confirmed live (`ollama ps` shows CONTEXT 8192). BUT:
  - **Inference too slow** — 100% CPU on Ryzen AI 7 PRO 350 + Radeon 860M. The iGPU (RDNA3.5) should be usable via Vulkan/ROCm but Ollama Windows + AMD APU support is patchy. Investigate: does `ollama` log mention GPU detection? Try setting `OLLAMA_NUM_GPU=999` or test with the Vulkan backend. Fallback if no GPU path: live with CPU latency or downsize to 3B (quality drop), or upsize hardware (NPU not yet supported by Ollama as of 2026-04).
  - **Output quality below threshold** — Qwen2.5 7B drifts: scarce summaries when tools return rich JSON, tool-call regressions emitted as chat text (`["$sleeve_summary", {}]`) even within 8192 ctx. Two paths: (a) bump to `qwen2.5:14b-instruct` via a parallel Modelfile (~9 GB, fits 32 GB), (b) add few-shot examples of "good answers" to the slim prompt teaching it to verbalize the JSON returned by `sleeve_summary` etc. Try (b) first — cheaper.
- [ ] **AI-ready: embeddings on `games.description`** for semantic search ("ho voglia di un gioco di esplorazione spaziale"). Reuse the rulebooks embedding pipeline; one pass when description is set/updated. (memo §8)
- [ ] **Frontend citation polish** — the `[↗](url)` suffix is ugly; convert text-block citations into superscript footnotes with a sources panel at the bottom of each bot bubble. Backend already passes citations in the `assistant.content[].citations` blocks; wire them in `web/index.html`.

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
