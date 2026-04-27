# Boardy — Future Improvements Memo

Last updated: 2026-04-27

This file captures ideas you've shared that are out of scope for the current build but worth coming back to. When you ask "how can I improve Boardy?", start here.

---

## 1. Replace Claude API with a self-hosted free LLM

**Goal you stated:** stop paying per-token, eventually run "on an Arduino" so Boardy is a fully offline, self-contained appliance.

**Reality check on hardware:**
- A classic Arduino (AVR/ATmega328, 32 KB flash, 2 KB RAM) **cannot host an LLM**. Not even a tiny one. The smallest practical LLMs need at least ~1 GB of RAM and tens of GB/s of memory bandwidth.
- "Arduino-like" boards that *can* run small LLMs reasonably:
  - **Raspberry Pi 5** (8 GB) — runs 3B–7B Q4 models at 1–5 tok/s via `llama.cpp` / Ollama.
  - **NVIDIA Jetson Orin Nano** — GPU-accelerated, 7B at usable speed.
  - **Orange Pi 5 / Rock 5B** (RK3588, 16 GB) — best Pi-class option, has NPU.
  - Mac Mini M4 — overkill but trivially fast for 8B–14B models, low idle power.
- For now: run on the laptop you already have. Arduino-class hardware is a separate project.

**Migration path (when ready):**
1. Install Ollama locally (`ollama pull qwen2.5:7b-instruct` or `llama3.1:8b-instruct`).
2. Replace the `Anthropic` client in `app/chat.py` with the OpenAI-compatible Ollama endpoint (`http://localhost:11434/v1`). The `anthropic` SDK isn't needed; tools still work via the OpenAI tool-calling spec — but tool-use quality on 7–8B models is **noticeably worse** than Claude. Test sleeve_summary specifically.
3. If tool-use quality drags, fall back to a **prompt-only** mode: stuff the entire DB (≤56 games, fits easily) into the system prompt as a JSON blob, no tools. Loses `update_inventory`, gains reliability.

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

## 6. Massive backfill of existing 56 games

**Goal:** the 56 games imported from Excel only have name/players/duration/complexity/sleeve. After the v2 schema refactor (2026-04-27) every game can hold `bgg_id`, `year_published`, `bgg_rating`, `complexity_weight` (numeric), `description`, `thumbnail_url`, plus categories/mechanics. To make them queryable the way Boardy can query newly-added games, we need to enrich them all.

**Approach:**
- Add a script `etl/backfill_bgg.py` that iterates over `games WHERE bgg_id IS NULL`, calls Anthropic web_search for each ("<name> boardgame BGG"), parses the proposed metadata, and applies it via `update_game` — with a confirmation prompt per game (or `--auto` flag for batch mode).
- Cost estimate: ~56 games × ~1 web_search each = ~$0.56 in search fees + ~56 × small LLM call = a few cents. Cheap.
- Risk: ambiguous names ("7 Wonders I" → which BGG entry? Probably 68448 base game). Show user the candidate before writing; auto-confirm only when the BGG result name match is exact (case-insensitive).
- Could also be triggered conversationally: user says "arricchisci tutti i giochi che non hanno BGG ID" → Boardy iterates with the existing tools.

## 7. Inventory & data improvements

- Add forms-based UI for `sleeve_inventory` + `sleeve_requirements` editing (chat is great for queries, awkward for bulk data entry).
- Re-import flow: instead of `DROP TABLE`, do an UPSERT merge so manually entered inventory survives an Excel re-import.
- Normalize `63x88` vs `63.5x88` in the data — currently treated as different sizes (round-up rule, or a "size aliases" table).
- Estimate sleeves for the 22 games marked `Sleeved` with no per-size breakdown — would let Boardy answer "how many sleeves does my collection have in total".
- Hook into BoardGameGeek API to auto-fill missing metadata by game name.

## 8. Make the DB fully AI-ready

The v2 schema is structured and tool-queryable but not "fully AI-ready" in the RAG sense. Two additions would close the gap:

- **Embeddings on `games.description`** — column `description_embedding BLOB`, indexed once when description is set/updated. Enables semantic search like "ho voglia di un gioco di esplorazione spaziale" without keyword matches. Same infra as the rulebook RAG (§5), so do them together.
- **Audit log table** `changes(id, table_name, row_id, field, old_value, new_value, changed_at, source)` — captures who/when/what for each write. Source = chat conversation_id when the write came from Boardy. Useful if multi-user or for "what did Boardy change last week?". Implement via SQL triggers or a thin write-wrapper in `app/tools.py`.

## 9. Quality-of-life

- Citations: have Boardy include the games it pulled from in each answer, with totals — already partly there via `sleeve_summary().games`, but make it a hard rule in the system prompt.
- Conversation export: button to copy the chat as Markdown.
- Multi-user: not needed (this is yours), but if it ever is, swap SQLite for Postgres and add auth.
