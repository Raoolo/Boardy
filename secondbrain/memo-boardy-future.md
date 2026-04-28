# Boardy — Future Improvements Memo

Last updated: 2026-04-29

This file captures ideas you've shared that are out of scope for the current build but worth coming back to. When you ask "how can I improve Boardy?", start here.

---

## Status snapshot (2026-04-29)

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
- 🟡 §1 "Self-host LLM" — **half-shipped 2026-04-29**: provider abstraction
  in `app/llm.py`, working OllamaProvider with `boardy-qwen` Modelfile
  (Qwen2.5 7B, num_ctx 8192). Two open issues block production switch —
  CPU-only inference is slow, 7B output quality drifts. See §1 below.
- 🟡 §8 "Embeddings on description" — still open, second on the High-priority TODO.

---

## 1. Replace Claude API with a self-hosted free LLM  🟡 *in progress (2026-04-29)*

**Goal you stated:** stop paying per-token, eventually run "on an Arduino" so Boardy is a fully offline, self-contained appliance.

**Reality check on hardware:**
- A classic Arduino (AVR/ATmega328, 32 KB flash, 2 KB RAM) **cannot host an LLM**. Not even a tiny one. The smallest practical LLMs need at least ~1 GB of RAM and tens of GB/s of memory bandwidth.
- "Arduino-like" boards that *can* run small LLMs reasonably:
  - **Raspberry Pi 5** (8 GB) — runs 3B–7B Q4 models at 1–5 tok/s via `llama.cpp` / Ollama.
  - **NVIDIA Jetson Orin Nano** — GPU-accelerated, 7B at usable speed.
  - **Orange Pi 5 / Rock 5B** (RK3588, 16 GB) — best Pi-class option, has NPU.
  - Mac Mini M4 — overkill but trivially fast for 8B–14B models, low idle power.
- For now: run on the laptop you already have. Arduino-class hardware is a separate project.

**Status (2026-04-29):**
The infra side is **done**. `app/llm.py` exposes a clean `Provider` interface
with `AnthropicProvider` (default) and `OllamaProvider` (Ollama at
`http://localhost:11434/v1`). Schema translation between Anthropic's
`input_schema` shape and OpenAI's `function.parameters` is a one-liner
inside the provider. Conversations recorded under one provider keep working
when you switch to the other — `_history_to_openai` translates Anthropic
content blocks on the fly. Switch with `LLM_PROVIDER=ollama` in `.env`.

**Two issues block making Ollama the default:**

1. **CPU-only inference.** Hardware: Ryzen AI 7 PRO 350 + Radeon 860M iGPU,
   32 GB RAM. Ollama loads the model at "100% CPU" — the iGPU (RDNA 3.5)
   isn't being engaged automatically. AMD APU support on Windows is patchy
   in Ollama 2026; Vulkan backend or `OLLAMA_NUM_GPU` may help. NPU on the
   350 is not yet supported by Ollama. Worst-case acceptable: live with the
   CPU latency for personal use; first turn ~60s, subsequent ~15-30s.

2. **Quality drift on 7B.** Tested with `boardy-qwen` (derived from
   `qwen2.5:7b-instruct` with `num_ctx=8192`, `temperature=0.3` via Modelfile).
   In a focused tool-routing benchmark (`test_local.py`) it scored 6/6 — the
   model picks the right tool with the right arguments. But in real chat
   it drifts:
   - **Output verbosity collapse** — `sleeve_summary` returns rich JSON,
     model summarizes to one sentence missing per-size detail.
   - **Tool-call-as-text regression** — even within ctx, occasionally
     prints `["$sleeve_summary", {}]` as chat text instead of using the
     structured channel.
   Try in order: (a) **few-shot examples** in the slim system prompt to
   teach the verbose-summary pattern (cheap, ~200 extra tokens, might fix
   both symptoms); (b) **upgrade to `qwen2.5:14b-instruct`** via a parallel
   Modelfile (~9 GB Q4, fits 32 GB with headroom). The 7B → 14B jump for
   tool-use quality is meaningful; speed drops moderately because the
   bottleneck is memory bandwidth not compute.

**What we learned this session (worth remembering):**
- **Ollama OpenAI-compat ignores `extra_body.options`** — `num_ctx`, `keep_alive`,
  etc. passed via the OpenAI SDK do not reach Ollama. Bake them into a
  Modelfile (`boardy-qwen.Modelfile` in repo root) instead.
- **Context overflow ⇒ silent regression.** With Boardy's full prompt + 16
  tool schemas you exceed the 4096 default; Ollama doesn't error, the model
  just starts emitting garbage and tool-calls-as-text. Always check `ollama ps`
  for the actual loaded `CONTEXT`.
- **Slim prompt for local mode.** CPU prefill at ~30 tok/s makes every
  prompt token cost ~30ms. Cut from ~3000 to ~470 tokens by keeping only
  the routing-critical rules (sleeve slang, add vs update inventory,
  ask_rules for rules questions, refusal to invent metadata).
- **Backwards-compat history is the trick that keeps both providers usable.**
  Don't normalize stored history — translate at the boundary inside the
  provider that needs OpenAI shape.

**If neither path works on this laptop:** fall back to a **prompt-only**
mode: stuff the DB (~56 games) directly into the system prompt as a JSON
blob, drop all tools. Loses `update_inventory` and history queries; gains
reliability since there's no tool-call format to regress to.

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

## 9. Quality-of-life

- Citations: have Boardy include the games it pulled from in each answer, with totals — already partly there via `sleeve_summary().games`, but make it a hard rule in the system prompt.
- Conversation export: button to copy the chat as Markdown.
- Multi-user: not needed (this is yours), but if it ever is, swap SQLite for Postgres and add auth.
