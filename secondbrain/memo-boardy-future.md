# Boardy — Future Improvements Memo

Last updated: 2026-05-01

This file captures ideas you've shared that are out of scope for the current build but worth coming back to. When you ask "how can I improve Boardy?", start here.

---

## Status snapshot (2026-05-01)

- ✅ §4 "Auto-fill missing fields" — shipped via `etl/backfill_bgg.py` (Haiku) and
  then rebuilt as `etl/bgg_api.py` + `etl/backfill_v2.py` (deterministic XML
  API2). Awaiting BGG bearer token (paywalled since 2026-04). See §6 below for
  the post-mortem on why v1 was replaced.
- ✅ §5 "Rulebook RAG" — shipped (`app/rulebooks.py`, `ingest_rulebook` /
  `ask_rules` tools, drag-and-drop in the UI).
- ✅ §6 "Massive backfill" — shipped twice; v1 cost ~€3 with partial results
  (JS-rendered BGG widgets + ambiguous-edition bailouts), v2 is the proper fix.
- ✅ §8 "Audit log" — shipped (`app/audit.py`, `changes` table, all writes
  auto-logged with `source` propagated from `chat:{conversation_id}` /
  `backfill_v2` / etc.). New tools `add_to_inventory` (delta-based) and
  `recent_changes` (read the log) are wired into the chat.
- ✅ §1 "Self-host LLM" — **archived 2026-04-29 PM**. Provider abstraction lives on
  and now hosts a third backend (DeepSeek), but local Ollama is shelved until
  hardware or models change. See §1 below for the post-mortem.
- ✅ Provider switch: **DeepSeek-chat** is the new default (~10× cheaper than
  Sonnet 4.6) via the OpenAI-compatible endpoint, reusing OllamaProvider's
  translation layer. Web search is now a **client-side Tavily tool**
  (`app/tools.py:web_search`) — provider-agnostic, replaces Anthropic's
  server-side `web_search_20250305`.
- ✅ Sleeve schema v3 (2026-04-29 PM): `sleeve_raw` dropped, `'no'`→`'na'`,
  `sleeve_requirements` is now a TODO list (rows only for non-sleeved games),
  with cascade + guard enforcing the invariant.
- 🟡 §8 "Embeddings on description" — still open, top of the High-priority TODO
  after the prompt-verification task.
- 🟡 **Anti-hallucination prompt verification** — new in TODO. The 2026-04-29
  rules ("header count = len(list_below)", "always `list_games()` with no
  filters for full-collection queries") are untested in practice.

---

## 1. Replace Claude API with a self-hosted free LLM  ✅ *archived (2026-04-29 PM)*

**Decision:** local Ollama path is **shelved** until hardware or models change.
Anthropic Sonnet was replaced with **DeepSeek-chat** (hosted, OpenAI-compatible,
~10× cheaper than Sonnet 4.6) — the cost pressure is gone, so the local path
is no longer urgent. Code preserved: `app/llm.py` still exposes
`OllamaProvider` and the `boardy-qwen.Modelfile` + `test_local.py` stay in the
repo. Re-enable with `LLM_PROVIDER=ollama` in `.env` if conditions change.

**Reality check on hardware (still true):**
- A classic Arduino (AVR/ATmega328, 32 KB flash, 2 KB RAM) **cannot host an LLM**. Not even a tiny one. The smallest practical LLMs need at least ~1 GB of RAM and tens of GB/s of memory bandwidth.
- "Arduino-like" boards that *can* run small LLMs reasonably:
  - **Raspberry Pi 5** (8 GB) — runs 3B–7B Q4 models at 1–5 tok/s via `llama.cpp` / Ollama.
  - **NVIDIA Jetson Orin Nano** — GPU-accelerated, 7B at usable speed.
  - **Orange Pi 5 / Rock 5B** (RK3588, 16 GB) — best Pi-class option, has NPU.
  - Mac Mini M4 — overkill but trivially fast for 8B–14B models, low idle power.

### Why we shelved it (numbers measured on HP ZBook G1a)

Hardware: Ryzen AI 7 PRO 350 + Radeon 860M (iGPU, RDNA 3.5) + 32 GB RAM.

| Config | Eval rate | Note |
|---|---|---|
| Vulkan iGPU + flash_attn | 5.54 tok/s | GPU at 100% but slower than CPU |
| **CPU + flash_attn** | **5.74 tok/s** | Marginally faster — chosen baseline |
| End-to-end "quante buste mi mancano?" | **254 seconds** | tool-loop + prefill |

**Structural insight:** AMD iGPU shares RAM with the CPU — no dedicated VRAM,
no bandwidth advantage on memory-bound 7B Q4 inference. So CPU vs iGPU is a
wash on this hardware. The 50 TOPS XDNA NPU is **not used by Ollama**; would
require AMD Ryzen AI Software / Lemonade SDK as a separate project.

**Quality side:** Qwen2.5 7B Q4 at `num_ctx=8192` regresses on tool-use:
- Emits tool calls as literal text (`[tool_call sleeve_summary()]`).
- Few-shot examples written with `[tool_call X → {…}]` pseudocode markers
  **made it worse** — the model imitated the markers. Lesson kept in
  LEARNINGS: never use bracket-pseudocode in few-shots for small models.
- Jump to 14B (~9 GB Q4) not attempted — fits in 32 GB but ~3 tok/s is
  unusable. Would need a 32B+ on a stronger machine for serious tool use.

### What lives on (the parts that paid off)

- **Provider abstraction in `app/llm.py`** — survived and absorbed a third
  provider (DeepSeek) with zero changes to `Provider` ABC. `OllamaProvider`
  is now the parent class for all OpenAI-compatible backends.
- **Backwards-compat history translation.** A conversation started under
  Anthropic continues correctly when switched to DeepSeek/Ollama and back —
  `_history_to_openai` handles both content shapes.
- **Modelfile lesson.** Ollama OpenAI-compat ignores `extra_body.options`
  (`num_ctx`, `keep_alive`, etc.); bake them into a Modelfile instead.

### When to re-open

- Hardware change: dGPU NVIDIA with dedicated VRAM, or laptop with a working
  Ryzen AI stack in Ollama.
- New small model with tool-use that doesn't regress (Llama 4 small, Qwen3,
  …). The bar is "no tool-call-as-text regressions, even at 8K ctx".
- Cost pressure on DeepSeek (very unlikely — Boardy budget is ~$0.10/month).

## 1b. Provider economics, post-DeepSeek

**Current default:** `LLM_PROVIDER=deepseek`, `LLM_MODEL=deepseek-chat`.
- **DeepSeek-chat:** ~$0.27/M input, ~$1.10/M output. ~10× cheaper than
  Sonnet 4.6.
- **Tavily** (web_search backend): 1000 ricerche/mese **free**;
  ~$4 / 1000 ricerche oltre la soglia. Boardy uses ≤5 per query, easy to
  stay under.
- **Net personal-use cost:** ~$0.10/month. DeepSeek is the new pragmatic
  baseline.

**If Tavily quota becomes a problem:** the `web_search` tool is
provider-agnostic. Swap the implementation in `app/tools.py:web_search` for
Brave Search or self-hosted SearXNG; tool schema unchanged.

## 2. Voice input

Easiest first step: **Web Speech API** (`SpeechRecognition` in the browser). Free, no backend, works in Chrome. Add a mic button next to Send in `web/index.html`. Output via `SpeechSynthesis`. No model changes.

If browser STT quality disappoints in Italian: Whisper.cpp local server, or OpenAI Whisper API.

## 3. Other frontends sharing the same `/chat` endpoint

- **Telegram bot** (`python-telegram-bot`) — easiest mobile path.
- **WhatsApp** — requires a paid Business API gateway. Skip unless you actually need it.
- **Discord bot** — fine if you live in Discord.

## 4. Auto-fill missing fields when adding a new game

**Goal:** when the user says "ho aggiunto Ark Nova alla collezione", Boardy should fetch the missing metadata (producer, publisher, players, duration, complexity, sleeve sizes/counts) automatically instead of making the user type it all.

**Approach:**
1. Add an `add_game(name)` tool that creates a stub `games` row with only the name set.
2. Add an `enrich_game(name)` tool that:
   - Queries the **BoardGameGeek XML API 2** (`https://boardgamegeek.com/xmlapi2/search?query=…` then `…/thing?id=&stats=1`) — free, no key, returns name/year/players/duration/weight/designer/publisher.
   - Maps BGG fields → our schema (`weight` 1-5 → "1. Molto Semplice"…"5. Esperto"; players → `players_min`/`max`).
   - Updates the row, returns a diff for the user to confirm.
3. Sleeve sizes: BGG doesn't expose card sizes/counts directly. Options:
   - Scrape the BGG forum/files section for community sleeve guides (fragile).
   - Use **BGG GeekItem cards info** if available, else ask the LLM to web-search ("Ark Nova sleeve sizes" + the user's preferred sleeve site, e.g. mayday-games.com, ultrasonic, etc.) and propose values for confirmation.
   - Fall back to "I don't know — please tell me", store in `sleeve_raw` for later parsing.
4. Make this opt-in per insert (not silent), so wrong BGG matches don't pollute data.

**Where it plugs in:** new tools in `app/tools.py`, new system-prompt rule "when a new game is added, immediately call `enrich_game` and present the proposed fill for user confirmation".

## 5. Rulebook Q&A during play

**Goal:** during a game session, ask Boardy "in War Chest can I attack a hex with no unit?" and get a cited answer from the actual rulebook PDF.

**Approach (RAG, simplest version):**
1. New folder `rulebooks/` with one PDF per game (downloaded by hand initially, or by an agent that fetches from BGG files).
2. New table `rulebook_chunks(game_id, chunk_id, page, text, embedding BLOB)`. Use `sqlite-vec` or just store embeddings as JSON blobs and brute-force cosine — 56 games × ~50 chunks each is tiny, no need for a vector DB.
3. Indexing script `etl/index_rulebooks.py`: `pypdf` to extract text, split into ~500-token overlapping chunks, embed with **Voyage AI** (cheap, Anthropic partner) or OpenAI `text-embedding-3-small`. One-shot, runs once per new PDF.
4. New tool `ask_rules(game_name, question)`:
   - Embed the question, top-k=5 chunks from that game's rulebook.
   - Pass chunks + question back to Claude with strict "answer only from these chunks; cite page numbers; if unclear say so".
5. UI: no change needed — just ask in chat, Boardy decides when to call the tool.

**Auto-download (later):** an agent that, on `add_game`, tries BGG files API for the official rulebook PDF, falls back to publisher's site, falls back to asking the user to drop the PDF into `rulebooks/`.

**Why local + cited:** during a real game, hallucinated rules are worse than no answer. Strict RAG with page citations keeps Boardy honest.

## 6. Massive backfill of existing 56 games  ✅ (rebuilt 2026-04-28)

**Goal:** the 56 games imported from Excel only have name/players/duration/complexity/sleeve. After the v2 schema refactor (2026-04-27) every game can hold `bgg_id`, `year_published`, `bgg_rating`, `complexity_weight` (numeric), `description`, `thumbnail_url`, plus categories/mechanics. To make them queryable the way Boardy can query newly-added games, we need to enrich them all.

**v1 — what we tried first (2026-04-27, abandoned):**
- `etl/backfill_bgg.py` running Haiku 4.5 + `web_search_20250305` per game.
- Cost: ~€3 for 56 games. Result: 20 games still without `bgg_id` (16 marked
  "ambiguous BGG match"), 27 games with `bgg_id` but missing
  `complexity_weight`/`bgg_rating`/categories/mechanics.
- Two structural failures: (a) BGG pages render those widgets via JavaScript,
  so `web_search` reads HTML that literally doesn't contain the values;
  (b) every "ambiguous edition" bailout still cost a search round.

**v2 — the proper fix (2026-04-28, awaits BGG token):**
- Skip the LLM entirely. Use BGG XML API2 directly:
  `xmlapi2/thing?id=X&stats=1` (full structured metadata) and
  `xmlapi2/search?query=NAME` (candidate list with id/year/type).
- BGG paywalled the API behind Cloudflare in early 2026 — anonymous → HTTP 401.
  Register an app at `boardgamegeek.com/using_the_xml_api`, put bearer token
  in `.env` as `BGG_API_TOKEN`. Then deterministic, free in perpetuity.
- Three phases (`etl/backfill_v2.py`):
  1. *Phase 1*: every game with known `bgg_id` → fetch + patch missing fields.
  2. *Phase 2*: every game without `bgg_id` → search → list candidates →
     human picks via `apply --gid N --bgg X`. Optional `--auto` for
     single-result hits.
  3. *Phase 3*: residue (homebrews, regional) stays manual.
- Code split: `etl/bgg_api.py` is pure HTTP+XML parsing (testable from saved
  fixtures), `etl/backfill_v2.py` is the orchestrator that talks to
  `app.tools.update_game`. On-disk cache at `etl/.bgg_cache/`.

**What v1 left in the DB (still useful):**
- The 27 games with a `bgg_id` populated (even if other fields are NULL) save
  Phase 2 work — Phase 1 picks them up directly.
- Ambiguous-match notes were cleaned out of `games.notes` on 2026-04-28
  because they were noisy and the candidate IDs they mentioned will be
  re-discovered deterministically by the v2 search.

## 7. Inventory & data improvements

- Add forms-based UI for `sleeve_inventory` + `sleeve_requirements` editing (chat is great for queries, awkward for bulk data entry).
- Re-import flow: instead of `DROP TABLE`, do an UPSERT merge so manually entered inventory survives an Excel re-import.
- Normalize `63x88` vs `63.5x88` in the data — currently treated as different sizes (round-up rule, or a "size aliases" table).
- Estimate sleeves for the 22 games marked `Sleeved` with no per-size breakdown — would let Boardy answer "how many sleeves does my collection have in total".
- ~~Hook into BoardGameGeek API to auto-fill missing metadata by game name.~~ ✅ via `etl/backfill_v2.py`.
- ✅ Delta-based purchase recording: tool `add_to_inventory(width, height, delta, brand?, note?)` does the arithmetic server-side so the model can't get `new = old + bought` wrong, and refuses negative results. Replaces the older "model-computed absolute count" pattern.

## 8. Make the DB fully AI-ready

The v2 schema is structured and tool-queryable but not "fully AI-ready" in the RAG sense. Two additions would close the gap:

- **Embeddings on `games.description`** 🟡 *open* — column `description_embedding BLOB`, indexed once when description is set/updated. Enables semantic search like "ho voglia di un gioco di esplorazione spaziale" without keyword matches. Same infra as the rulebook RAG (§5), so do them together. Now that v2 backfill will populate `description` consistently, this is the obvious next step.
- **Audit log table** ✅ shipped 2026-04-28 — `changes(id, ts, table_name, row_id, row_label, action, field, old_value, new_value, source)` with indices on `(table_name, row_id)` and `ts DESC`. Implementation in `app/audit.py` using a thin wrapper approach (not SQL triggers — keeps the logic in Python where the source can be injected via `app/chat.py` introspection without showing up in the JSON tool schema).
  - Source values in use: `chat:{conversation_id}` (auto), `backfill_v2`, `manual`, `unknown`. ETL writes (`import_excel.py`) intentionally bypass the log because that script is destructive bulk-reset by design.
  - Read access: tool `recent_changes(limit, table?, game_name?)` — Sonnet now consults this for "quando ho aggiunto X?" / "cosa è cambiato di Y?" instead of guessing.
  - Future polish: a `/changes` page in the UI for browsing without going through chat (low priority — SQL ad-hoc works for now).

## 8b. Anti-hallucination prompt — verification pending

The 2026-04-29 PM session added two new rules to the system prompt:

1. **"For full-collection queries, always call `list_games()` with NO
   filters first."** Prior tool results in the same conversation are
   subsets, not totals. The model must not enumerate the full collection
   from memory.
2. **"The number you write in a header MUST equal `len(list_below)`."**
   Count by enumeration, never from memory; if the header count and the
   list disagree, the output is wrong.

Both rules are written but **untested**. The bug they target was observed
on Sonnet (header said "5 to_sleeve", list had 6 items) and may need a
different fix on DeepSeek (different tokenizer, different attention
patterns). Verification plan:

- Run a small batch of full-collection queries: "dammi la situazione della
  mia collezione", "raggruppa per sleeve_status", "quanti giochi ho per
  designer?".
- Check that (a) `list_games()` is called without filters first, (b) every
  numeric header matches its associated list length, (c) no group is
  silently dropped.

If it still drifts, escalation order:
- Add a single concrete few-shot example ("User: quanti giochi ho? →
  Boardy: [calls list_games()] → 56 giochi totali. …").
- Split the count rule into a "Before answering, COUNT BY ENUMERATING"
  preamble, repeated near the top of the prompt.
- As last resort, post-process the model output server-side: regex header
  counts vs list length, flag mismatches before sending to the UI.

## 9. Quality-of-life

- Citations: have Boardy include the games it pulled from in each answer, with totals — already partly there via `sleeve_summary().games`, but make it a hard rule in the system prompt.
- Conversation export: button to copy the chat as Markdown.
- Multi-user: not needed (this is yours), but if it ever is, swap SQLite for Postgres and add auth.
