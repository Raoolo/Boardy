# AGENTS.md

Personal board-game inventory chatbot. Single-user, runs on Windows. Natural-language Q&A over a local SQLite DB + rulebook RAG index.

## Setup & commands

```bash
uv sync                                            # install deps
uv run uvicorn app.main:app --port 8765            # run web app
uv run python etl/import_excel.py                  # upsert-by-name re-import (see Conventions)
uv run python -m bot.telegram_bot                  # Telegram bot (optional, vedi sezione Deploy)
```

Backfills (run in order on a fresh DB):
```bash
uv run python etl/backfill_v2.py phase1 | phase2 [--auto] | apply --gid N --bgg X
uv run python etl/backfill_descriptions_tavily.py [--only NAME] [--dry-run]
uv run python etl/backfill_descriptions_websearch.py [--only NAME] [--manual "text"] [--dry-run]
uv run python etl/embed_descriptions.py [--force]
uv run python etl/generate_friendly_tags.py [--force] [--only NAME] [--dry-run]
uv run python etl/backfill_rulebooks.py [--apply] [--level strong|likely|weak] [--only NAME] [--skip "csv"]
```

`backfill_rulebooks.py`: per ogni gioco posseduto senza regolamento risolve il nome **inglese** via BGG (i titoli 1j1ju sono EN), cerca su 1j1ju, classifica il match (`strong`=titolo esatto, `likely`=subset token, `weak`/`none`) e con `--apply` scarica+indicizza. Default dry-run = tabella valutabile. `--skip` esclude falsi positivi (es. bgg_id errati). Match `weak` = quasi sempre sbagliati (gioco non su 1j1ju).

No test suite — validate by smoke-testing a tool (`uv run python -c "from app.tools import sleeve_summary; print(sleeve_summary())"`) or hitting `POST /chat`. Server has no auto-reload; restart manually after Python changes. `web/` is served live.

## Directory layout

```
app/         FastAPI app + chat loop + tools (read code first)
etl/         One-shot scripts: Excel import, BGG backfill, embeddings
web/         Static HTML pages (index/library/sleeves/wishlist), no build step
deploy/      Dockerfile + docker-compose.yml (compose pins `name: boardy` so volumes stay stable)
bot/         Telegram bot (opzionale): thin client su POST /chat
docs/        LEARNINGS.md (tribal knowledge) + TODO.md (prioritized backlog)
rulebooks/   PDF rulebooks (gitignored — copyright + bulky)
data/        Source data + runtime DB + backups (Excel, boardy.db, *.db.bak)
archive/     Legacy code from abandoned approaches (e.g. Ollama exploration). Read-only history.
secondbrain/ Owner's Obsidian vault; memos about Boardy live in `memo-*.md`. DO NOT write without being asked.
.Codex/     Codex's own state (auto-managed)
```

## Where to look

- `app/main.py` — FastAPI entry point. All REST endpoints (`/chat`, `/conversations/*`, `/sleeves/*`, `/library/*`, `/wishlist/*`, `/games/*`, `/auth/*`) + StaticFiles mount. First file to read when adding a new route.
- `app/auth.py` — username/password locale + cookie firmato (bcrypt diretto + itsdangerous). Espone `get_current_user` (FastAPI dependency, ritorna `dict | None`), `require_owner(user)` per gate dei write endpoint e `can_audit_conversations(user)` per la vista Audit delle chat. Hash/verify password truncano a 72 bytes (limite bcrypt).
- `app/chat.py` — provider-agnostic tool-use loop, up to 8 rounds. Auto-injects `_source="chat:{conv_id}/user:{username}"` (or `/guest`) into write tools via `inspect.signature`. Filtra `TOOLS` per ruolo: guest vede solo i read tools (vedi `tools.WRITE_TOOLS`).
- `app/tools.py` — all tools. Adding one = funzione decorata `@tool()` (read) o `@tool(write=True)` (write) + JSON schema in `TOOLS`. I registri `TOOL_FUNCS`/`WRITE_TOOLS` si costruiscono **da soli** dai decorator (niente più liste a mano) e un **assert al boot** crasha se uno schema in `TOOLS` non ha funzione o viceversa. Il flag `write=True` è la source-of-truth del gating guest/owner (rimpiazza il vecchio set esplicito — serviva perché `ingest_rulebook` scrive ma non dichiara `_source`, quindi l'euristica `_source` non bastava). `ingest_rulebook_photos` NON è un tool LLM registrato (niente `@tool`): gira solo via endpoint `/rulebooks/upload-photos`. Write tools devono comunque dichiarare `_source: str | None = None` (iniettato da chat.py per l'audit). BGG metadata viene dai tool **`bgg_search`/`bgg_lookup`** (wrapper read-only su `etl/bgg_api`, official XML API) — NON da `web_search` (lo scraping della pagina pubblica BGG è cookie-walled, vedi LEARNINGS). Le **buste** (`sleeve_lookup`, read-only su `etl/syg_api`) vengono dall'API JSON privata di sleeveyourgames.com (deterministica, CORS-aperta): `requirements` già nella forma di `set_sleeve_requirements`. L'API BGG NON espone misure carte. `web_search` resta come fallback per le sleeve (quando `sleeve_lookup` dà `found:false`, es. giochi troppo nuovi) + errata.
- `app/llm.py` — `Provider` ABC with three impls: `AnthropicProvider` (`Codex-sonnet-4-6`), `DeepSeekProvider` (`deepseek-chat`, OpenAI-compatible — **current production default per `.env`**, ~10× cheaper than Sonnet), `OllamaProvider` (local, archived — see memory). Selection per-request via `LLM_PROVIDER`. Web search is client-side (Tavily tool in `app/tools.py`) — no provider-specific search anymore. `/library/filter` is hardcoded to `deepseek-chat` (override via `LIBRARY_FILTER_MODEL`).
- `app/schema.py` — star schema DDL + idempotent v1→v9 migration on every boot (latest: `pdf_blob` BLOB column on `rulebooks` — the raw PDF lives in the DB, see below).
- `app/friendly_tags.py` — LLM-generated user-friendly tags (DeepSeek `deepseek-chat`, T=0, vocabolario fisso di 19 voci). Genera 3-5 tag/gioco da nome+description+BGG cats/mechs+weight+duration. Called post-commit da `add_game`/`update_game`/`add_to_wishlist`/`update_wishlist` (best-effort) + batch via `etl/generate_friendly_tags.py`. Vocab decoupled: cambiarlo richiede regenerare tutto il catalogo (`--force`).
- `app/audit.py` — every write to `games`/`sleeve_requirements`/`sleeve_inventory` logs to `changes`.
- `app/conversations.py` — server-side conversation persistence + ownership metadata (`origin`, `actor_role`, `actor_id`, `actor_name`) + `_title_from_history` (DeepSeek `deepseek-chat`, T=0, ~$0.0001/conv; first save only, then COALESCE-sticky). `/conversations` default = `scope=mine`; `scope=audit` è solo owner/admin.
- `app/db.py` — SQLite connection. Reads env `BOARDY_DB` (Docker volume path); falls back to `<repo>/data/boardy.db`.
- `app/games_semantic.py` — hybrid SQL+cosine over `games.description_embedding`. Reuses `_model_lazy()` from `rulebooks.py` (single 280MB load).
- `app/rulebooks.py` — pypdf chunking + e5 embeddings + brute-force cosine. Rulebook RAG flow: `find_rulebook` (read, cerca PDF online) → `download_rulebook` (write, scarica+indicizza in un colpo) → `ask_rules` (read, Q&A con citazione pagina). `ingest_rulebook` resta per PDF già su disco. **Storage: il PDF grezzo vive in `rulebooks.pdf_blob` (DB), NON su disco** — `ingest_bytes(game_name, data, source=...)` è il core (lo usano download/upload/auto-hook); `ingest(path)` legge il file e delega; `get_pdf(name)` ri-estrae i bytes. Risoluzione nome tollerante via `_resolve_game` (exact LOWER → normalizzato non-ambiguo) usata da ingest+search, così nomi con punteggiatura diversa non falliscono. Fonte primaria dei PDF: **`etl/onejour_api`** (1j1ju.com, vedi sotto).
- `etl/onejour_api.py` — wrapper read-only su 1jour-1jeu.com (`/rules/search?q=`, HTML scraping deterministico). Restituisce link PDF diretti `cdn.1j1ju.com` con lingua dedotta dal filename. Usato da `find_rulebook`. Tavily resta come fallback quando 1j1ju non trova nulla (indicizza male cdn.1j1ju.com). BGG Files NON usato (API non li espone + cookie-wall, vedi LEARNINGS).
- `web/index.html` — chat UI (single-file vanilla JS + `marked.js`). Sidebar conversazioni con toggle `Mie/Audit`: `Mie` mostra solo le chat dell'utente, `Audit` è read-only e solo per owner/admin. No build step.
- `web/library.html` — library page: grid/table toggle, multi-select **friendly_tags** filter (raw BGG categories/mechanics still in DB but not surfaced in UI), smart-filter chatbot (`/library/filter`) — chatbot estrae anch'esso `friendly_tags` invece di cats/mechs.
- `web/sleeves.html` — sleeve dashboard: KPI cards, Da comprare, Buste future (wishlist preview), Pronti da sleevare, mini-chat dock.
- `web/wishlist.html` — wishlist page: grid+table, priority chips, Promise-based confirm modal for buy/remove, chat dock.
- `web/login.html` — standalone login form (POST `/auth/login` → set cookie → redirect `?next=...`). No nav, no sidebar; matches the dark theme.
- `web/static/auth.js` — shared client helper (`BoardyAuth.state()`, `mountBadge(headerEl)`, `isOwner()`, `logout()`). Caricato da tutte le 4 pagine via `<script src="/static/auth.js"></script>` per il chip auth in topbar.
- `bot/telegram_bot.py` — client Telegram opzionale (PTB v21, async). Thin wrapper su `POST /chat`: niente duplicazione del chat loop. Auth a 2 ruoli specchiata sul web: allow-list `TELEGRAM_OWNER_IDS` → cookie-auth con `BOARDY_BOT_USERNAME`/`PASSWORD` → conv Boardy persistita; chi non è in lista parla in guest mode read-only ma la chat viene persistita lato Boardy con `origin=telegram`, `actor_role=guest`, `actor_id=<telegram user_id>`. Mapping `chat_id → {role, conversation_id}` salvato in `<BOARDY_DB dir>/telegram_chats.json`. Comandi: `/start`, `/new`, `/whoami`, `/help`.

## Companion docs (read before non-trivial work)

- `docs/LEARNINGS.md` — **read first**. Tribal knowledge: gotchas, decisions, user preferences accumulated across sessions.
- `docs/TODO.md` — actionable backlog with priorities. Consult when the user asks "what's next?".
- `secondbrain/memo-boardy-future.md` — long-form rationale behind TODO items. Open when a TODO needs context.
- `secondbrain/memo-deploy-howto.md` — exact Docker/git commands for self-host (setup, update workflow, troubleshooting table).
- `secondbrain/memo-deploy-caveman.md` — mental model of the deploy (restaurant analogy + real-life examples). Read first when re-orienting after a break.
- `secondbrain/memo-auth-caveman.md` — auth in caveman mode (portiere/braccialetto). Use when explaining the login model to a friend or auditing what can/cannot leak.
- `secondbrain/memo-telegram-bot.md` — setup + troubleshooting del bot Telegram (token, allow-list, comandi, deploy Docker, modifiche frequenti).
- `secondbrain/` (broader) — the user's Obsidian vault. Notes about Boardy live here; cross-references to other personal projects may exist. Don't write to it without being asked.

## Conventions

- **Reply in the user's language.** Italian for Italian prompts; the user mixes IT/EN freely.
- **Confirm before destructive ops.** `delete_game` and BGG-enriched `add_game`/`update_game` must propose a table and wait for "sì/confermo".
- **NEW owned game = BGG + sleeves in one shot.** When the user asks to add a brand-new owned game (via chat, not ETL), Boardy must fetch BGG metadata via **`bgg_search` + `bgg_lookup`** (official XML API — deterministic) **and** sleeve sizes via **`sleeve_lookup(name, bgg_id=...)`** in the same turn, propose ONE compact table that includes a "Buste previste" row, and on confirmation call BOTH `add_game(..., sleeve_status='to_sleeve')` AND `set_sleeve_requirements(name, [...])`. If `sleeve_lookup` returns `found:false` (game too new), fall back to `web_search` sleeveyourgames, then manual entry. Skip the sleeve fetch only on explicit "solo metadati"/"niente buste" or when the game has no cards (use `sleeve_status='na'` then). Rationale: previously the sleeve step was optional and non-deterministic; unifying it removes the "ah giusto, ora cercami anche le buste" follow-up. (BGG metadata + sleeves via `web_search` was the old path — abbandonato perché cookie-walled/non-deterministico, vedi LEARNINGS.)
- **Rules questions = propose-then-confirm rulebook fetch.** Per domande di regole usa `ask_rules`. Se torna "no rulebook ingested", chiama `find_rulebook(game_name)` (titolo **inglese**), proponi il miglior candidato in tabella (file/titolo, fonte, lingua) e ASPETTA conferma; poi `download_rulebook(game_name, url)` (scarica+indicizza, valida `%PDF`), infine ri-`ask_rules` e cita la pagina. Il gioco DEVE già esistere in DB. Se `find_rulebook` non trova nulla, chiedi un URL PDF diretto o un path locale (`ingest_rulebook`). Lingua indifferente (e5 è multilingue). Mai rispondere a regole dalla tua conoscenza.
- **`etl/import_excel.py` upserts by `name`** (since 2026-05-04). Existing games get their ETL-managed columns refreshed (players/duration/complexity/condition/sleeve_status); BGG-enriched fields and chat-added games survive. Caveat: if a chat-cleaned name diverges from the Excel cell you get a duplicate — see LEARNINGS 2026-05-04.
- **No "Fonti:" prose sections** after web_search — system prompt forbids it (post-processor mangles them). Inline `[label](url)` only.
- **`add_to_inventory(width, height, delta, ...)` is preferred** over `update_inventory` for purchases/consumption: server-side arithmetic, refuses negative results.
- **`_source` is internal.** Never put it in a tool's JSON schema — chat.py injects it. Otherwise the model can spoof audit origins.
- **Windows console = cp1252.** Scripts printing `→`/`✓`/`↗` must `sys.stdout.reconfigure(encoding="utf-8")` early or run with `PYTHONIOENCODING=utf-8`.
- **E5 multilingual thresholds**: ≥0.78 strong, 0.72–0.77 borderline, <0.72 noise. Lower than English-only — IT/EN trade-off.
- **Wishlist fence on read tools.** `games.status` is `'owned' | 'wishlist'` in a single table. Read tools (`list_games`, `sleeve_summary`, `search_games_semantic`, `library_data`, `games_names`) MUST default to `status='owned'`. Opt-in via `status='wishlist'`/`'any'` where it makes sense. Forgetting this leaks wishlist into collection counts.
- **BGG media backfill hook fires post-write.** `_backfill_bgg_media(gid)` is called from `add_game`, `update_game`, `add_to_wishlist`, `update_wishlist` to patch `thumbnail_url`/`image_url` via `etl/bgg_api.fetch_thing()` when `bgg_id` is set but URLs are empty. If you add a write tool that mutates `bgg_id`, call the hook too.
- **Rulebook auto-fetch hook fires post-`add_game`.** `_backfill_rulebook(gid)` cerca su 1j1ju e scarica+indicizza il regolamento SOLO se trova un match certo (titolo del candidato, tolte le parole "rulebook/regle/...", normalizza ESATTAMENTE al nome del gioco → niente espansioni o omonimi; preferisce EN). Best-effort: salta in silenzio se ambiguo o già presente, lasciando la chat proporre con conferma. Solo `add_game` (giochi posseduti), non wishlist/update.
- **Auth model: guest = read-only, owner = full.** Guest (no cookie) vede tutto in read mode + può chattare (tool gating: write tools rimossi dal registry prima del loop). Owner (cookie firmato) può scrivere. Le conversazioni NON sono più globalmente condivise nella sidebar: `/conversations` default = solo chat proprie; `scope=audit` mostra tutte le chat solo a ruoli `owner`/`admin` ed è read-only in UI; `POST /chat` rifiuta di continuare chat altrui. Quando aggiungi un endpoint che scrive su DB/fs **devi** mettere `user: dict | None = Depends(get_current_user)` + `require_owner(user)` come prima riga; quando aggiungi un tool che muta stato **devi** aggiungerlo a `tools.WRITE_TOOLS`. Audit `_source` formato: `chat:{id}/user:{name}` (chat owner), `web:{page}/user:{name}` (REST owner), `chat:{id}/guest` o `chat:guest`. Per gestire utenti: `uv run python etl/create_user.py create|reset|list`.

## Environment

- `ANTHROPIC_API_KEY` — Anthropic Console key (separate from Codex.ai Pro; Pro does NOT include API).
- `LLM_PROVIDER` — `anthropic` | `deepseek` | `ollama`. Code default is `anthropic`, but deployed `.env` sets `deepseek` (the actual production provider). Per-request, no restart.
- `DEEPSEEK_API_KEY` — required when `LLM_PROVIDER=deepseek` AND for `/library/filter` (which is always DeepSeek regardless of provider).
- `LLM_MODEL`, `DEEPSEEK_BASE_URL`, `OLLAMA_BASE_URL` — optional overrides.
- `LIBRARY_FILTER_MODEL` — override the DeepSeek model used by `/library/filter` (default `deepseek-chat`).
- `BGG_API_TOKEN` — required since 2026-04 (BGG XML API is Cloudflare-gated, both v1 and v2). Public-page scraping via web_search was tried and failed (JS-rendered widgets — see LEARNINGS).
- `BOARDY_DB` — optional, overrides DB path. Used by `docker-compose.yml` to point at `/data/boardy.db` (named volume). Defaults to `<repo>/data/boardy.db`.
- `BOARDY_SESSION_SECRET` — **required** in production. Chiave per firmare il cookie di sessione owner. Genera con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Senza, l'app crasha al primo `/auth/login`. Ruotarla invalida tutti i cookie esistenti (logout globale).
- `BOARDY_COOKIE_SECURE` — `1` in produzione HTTPS (cookie marcato `Secure`, browser rifiuta su HTTP); lascia unset in dev locale (`http://localhost`).
- `CF_TUNNEL_TOKEN` — Cloudflare Tunnel token. Required ONLY for `docker compose --profile tunnel` (self-host deploy). Generated by the tunnel owner in the CF dashboard (Zero Trust → Networks → Tunnels → Create → token).
- `TELEGRAM_BOT_TOKEN` — required ONLY per `bot/telegram_bot.py`. Crealo via @BotFather su Telegram.
- `TELEGRAM_OWNER_IDS` — allow-list (CSV di interi) degli user_id Telegram considerati owner. Non-owner = guest (read-only). `/whoami` sul bot mostra il proprio user_id.
- `BOARDY_BOT_USERNAME` / `BOARDY_BOT_PASSWORD` — account Boardy con cui il bot fa `POST /auth/login` per i messaggi degli owner. Crealo con `etl/create_user.py create`.
- `BOARDY_BASE_URL` — URL base di Boardy. Default `http://127.0.0.1:8765` (locale); il compose lo sovrascrive a `http://boardy:8765` (rete docker interna).
- First run downloads ~1GB to `~/.cache/huggingface/` for the e5 model. Subsequent loads ~3s. In the Docker image the model is pre-cached at build time → no first-boot download.

## Deploy / Self-host (Docker)

Docker files live in `deploy/`. The compose file pins `name: boardy` so the volume is always `boardy_boardy_db` regardless of where you invoke from. Profiles sono additivi:

```bash
docker compose -f deploy/docker-compose.yml up -d --build                              # local: boardy on http://127.0.0.1:8765
docker compose -f deploy/docker-compose.yml --profile tunnel up -d                     # server: boardy + cloudflared (public via CF Tunnel)
docker compose -f deploy/docker-compose.yml --profile telegram up -d                   # server: boardy + telegram bot
docker compose -f deploy/docker-compose.yml --profile tunnel --profile telegram up -d  # entrambi
```

Tip: export `COMPOSE_FILE=deploy/docker-compose.yml` in the server shell to drop the `-f` flag from subsequent commands.

**Update workflow** on the server: `git pull && docker compose -f deploy/docker-compose.yml restart boardy` — Python code and HTML are bind-mounted, no rebuild. Rebuild image (`up -d --build`) ONLY when `pyproject.toml` / `uv.lock` change.

**State**: `boardy.db` lives in the `boardy_db` named volume (survives image rebuilds); `rulebooks/` is bind-mounted from host; `.env` is bind-mounted read-only. The e5 model is baked into the image.

**Cloudflare Tunnel setup** (one-time, on the host that runs Docker):
1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel → copy the **token**.
2. In Public Hostname tab: add `boardy.<your-domain>.tld` → service `http://boardy:8765`. (`boardy` here is the container hostname inside the Docker network.)
3. Put `CF_TUNNEL_TOKEN=...` in `.env` on the server.
4. `docker compose -f deploy/docker-compose.yml --profile tunnel up -d`. The tunnel comes up, hostname resolves, TLS handled by Cloudflare. No port forwarding needed on the host.

The Docker image bakes the e5 model (~1.5GB total). First build ~3-5 min, subsequent rebuilds (deps unchanged) under 1 min thanks to layer cache.

**Oracle Cloud Always Free ARM (target deploy "spesa 0").** Il build è nativo arm64 sulla VM (`up -d --build`) — nessuna modifica `platform` serve. `torch` è pinnato all'indice CPU (`pyproject.toml`: `[tool.uv.sources] torch = pytorch-cpu`) per non tirare le wheel CUDA inesistenti su aarch64 / inutili su CPU — vedi LEARNINGS 2026-06-05. Per il provisioning della A1 (spesso `Out of host capacity`) usa `etl/oci_find_capacity.py` (auto-retry+backoff, notifica Telegram opzionale): `uv run --with oci python etl/oci_find_capacity.py --subnet ... --image ... --ssh-key ...` (OCI SDK è dipendenza opzionale, param anche via env `OCI_*`).

**Telegram bot setup** (one-time):
1. @BotFather su Telegram → `/newbot` → copia il token.
2. `TELEGRAM_BOT_TOKEN=...` in `.env`.
3. Lancia il bot una volta in locale (`uv run python -m bot.telegram_bot`), poi mandagli `/whoami` da Telegram per scoprire il tuo `user_id`. Mettilo in `TELEGRAM_OWNER_IDS=<id>` (CSV se piu' di uno).
4. Configura `BOARDY_BOT_USERNAME` / `BOARDY_BOT_PASSWORD` (account creato con `etl/create_user.py`).
5. Server: `docker compose -f deploy/docker-compose.yml --profile telegram up -d`. Stato (mapping `chat_id → {role, conversation_id}`) persistito su `data/telegram_chats.json`; testo/history in `conversations`.
