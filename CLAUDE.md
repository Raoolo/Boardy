# CLAUDE.md

Personal board-game inventory chatbot. Single-user, runs on Windows. Natural-language Q&A over a local SQLite DB + rulebook RAG index.

## Setup & commands

```bash
uv sync                                            # install deps
uv run uvicorn app.main:app --port 8765            # run web app
uv run python etl/import_excel.py                  # upsert-by-name re-import (see Conventions)
```

Backfills (run in order on a fresh DB):
```bash
uv run python etl/backfill_v2.py phase1 | phase2 [--auto] | apply --gid N --bgg X
uv run python etl/backfill_descriptions_tavily.py [--only NAME] [--dry-run]
uv run python etl/backfill_descriptions_websearch.py [--only NAME] [--manual "text"] [--dry-run]
uv run python etl/embed_descriptions.py [--force]
```

No test suite — validate by smoke-testing a tool (`uv run python -c "from app.tools import sleeve_summary; print(sleeve_summary())"`) or hitting `POST /chat`. Server has no auto-reload; restart manually after Python changes. `web/` is served live.

## Directory layout

```
app/         FastAPI app + chat loop + tools (read code first)
etl/         One-shot scripts: Excel import, BGG backfill, embeddings
web/         Static HTML pages (index/library/sleeves/wishlist), no build step
deploy/      Dockerfile + docker-compose.yml (compose pins `name: boardy` so volumes stay stable)
docs/        LEARNINGS.md (tribal knowledge) + TODO.md (prioritized backlog)
rulebooks/   PDF rulebooks (gitignored — copyright + bulky)
data/        Source data + runtime DB + backups (Excel, boardy.db, *.db.bak)
archive/     Legacy code from abandoned approaches (e.g. Ollama exploration). Read-only history.
secondbrain/ Owner's Obsidian vault; memos about Boardy live in `memo-*.md`. DO NOT write without being asked.
.claude/     Claude Code's own state (auto-managed)
```

## Where to look

- `app/main.py` — FastAPI entry point. All REST endpoints (`/chat`, `/conversations/*`, `/sleeves/*`, `/library/*`, `/wishlist/*`, `/games/*`, `/auth/*`) + StaticFiles mount. First file to read when adding a new route.
- `app/auth.py` — username/password locale + cookie firmato (bcrypt diretto + itsdangerous). Espone `get_current_user` (FastAPI dependency, ritorna `dict | None`) e `require_owner(user)` per gate dei write endpoint. Hash/verify password truncano a 72 bytes (limite bcrypt).
- `app/chat.py` — provider-agnostic tool-use loop, up to 8 rounds. Auto-injects `_source="chat:{conv_id}/user:{username}"` (or `/guest`) into write tools via `inspect.signature`. Filtra `TOOLS` per ruolo: guest vede solo i read tools (vedi `tools.WRITE_TOOLS`).
- `app/tools.py` — all tools. Adding one = function + JSON schema in `TOOLS` + entry in `TOOL_FUNCS`. Write tools must declare `_source: str | None = None` AND be added to `WRITE_TOOLS` set (source of truth per il gating guest/owner — non basta l'euristica `_source` perché `ingest_rulebook` scrive ma non ha `_source`).
- `app/llm.py` — `Provider` ABC with three impls: `AnthropicProvider` (`claude-sonnet-4-6`), `DeepSeekProvider` (`deepseek-chat`, OpenAI-compatible — **current production default per `.env`**, ~10× cheaper than Sonnet), `OllamaProvider` (local, archived — see memory). Selection per-request via `LLM_PROVIDER`. Web search is client-side (Tavily tool in `app/tools.py`) — no provider-specific search anymore. `/library/filter` is hardcoded to `deepseek-chat` (override via `LIBRARY_FILTER_MODEL`).
- `app/schema.py` — star schema DDL + idempotent v1→v7 migration on every boot (latest: `users` table for owner login).
- `app/audit.py` — every write to `games`/`sleeve_requirements`/`sleeve_inventory` logs to `changes`.
- `app/conversations.py` — server-side conversation persistence + `_title_from_history` (DeepSeek `deepseek-chat`, T=0, ~$0.0001/conv; first save only, then COALESCE-sticky; truncation fallback if no `DEEPSEEK_API_KEY` or the call fails).
- `app/db.py` — SQLite connection. Reads env `BOARDY_DB` (Docker volume path); falls back to `<repo>/data/boardy.db`.
- `app/games_semantic.py` — hybrid SQL+cosine over `games.description_embedding`. Reuses `_model_lazy()` from `rulebooks.py` (single 280MB load).
- `app/rulebooks.py` — pypdf chunking + e5 embeddings + brute-force cosine.
- `web/index.html` — chat UI (single-file vanilla JS + `marked.js`). No build step.
- `web/library.html` — library page: grid/table toggle, multi-select category/mechanic filters, smart-filter chatbot (`/library/filter`).
- `web/sleeves.html` — sleeve dashboard: KPI cards, Da comprare, Buste future (wishlist preview), Pronti da sleevare, mini-chat dock.
- `web/wishlist.html` — wishlist page: grid+table, priority chips, Promise-based confirm modal for buy/remove, chat dock.
- `web/login.html` — standalone login form (POST `/auth/login` → set cookie → redirect `?next=...`). No nav, no sidebar; matches the dark theme.
- `web/static/auth.js` — shared client helper (`BoardyAuth.state()`, `mountBadge(headerEl)`, `isOwner()`, `logout()`). Caricato da tutte le 4 pagine via `<script src="/static/auth.js"></script>` per il chip auth in topbar.

## Companion docs (read before non-trivial work)

- `docs/LEARNINGS.md` — **read first**. Tribal knowledge: gotchas, decisions, user preferences accumulated across sessions.
- `docs/TODO.md` — actionable backlog with priorities. Consult when the user asks "what's next?".
- `secondbrain/memo-boardy-future.md` — long-form rationale behind TODO items. Open when a TODO needs context.
- `secondbrain/memo-deploy-howto.md` — exact Docker/git commands for self-host (setup, update workflow, troubleshooting table).
- `secondbrain/memo-deploy-caveman.md` — mental model of the deploy (restaurant analogy + real-life examples). Read first when re-orienting after a break.
- `secondbrain/memo-auth-caveman.md` — auth in caveman mode (portiere/braccialetto). Use when explaining the login model to a friend or auditing what can/cannot leak.
- `secondbrain/` (broader) — the user's Obsidian vault. Notes about Boardy live here; cross-references to other personal projects may exist. Don't write to it without being asked.

## Conventions

- **Reply in the user's language.** Italian for Italian prompts; the user mixes IT/EN freely.
- **Confirm before destructive ops.** `delete_game` and BGG-enriched `add_game`/`update_game` must propose a table and wait for "sì/confermo".
- **`etl/import_excel.py` upserts by `name`** (since 2026-05-04). Existing games get their ETL-managed columns refreshed (players/duration/complexity/condition/sleeve_status); BGG-enriched fields and chat-added games survive. Caveat: if a chat-cleaned name diverges from the Excel cell you get a duplicate — see LEARNINGS 2026-05-04.
- **No "Fonti:" prose sections** after web_search — system prompt forbids it (post-processor mangles them). Inline `[label](url)` only.
- **`add_to_inventory(width, height, delta, ...)` is preferred** over `update_inventory` for purchases/consumption: server-side arithmetic, refuses negative results.
- **`_source` is internal.** Never put it in a tool's JSON schema — chat.py injects it. Otherwise the model can spoof audit origins.
- **Windows console = cp1252.** Scripts printing `→`/`✓`/`↗` must `sys.stdout.reconfigure(encoding="utf-8")` early or run with `PYTHONIOENCODING=utf-8`.
- **E5 multilingual thresholds**: ≥0.78 strong, 0.72–0.77 borderline, <0.72 noise. Lower than English-only — IT/EN trade-off.
- **Wishlist fence on read tools.** `games.status` is `'owned' | 'wishlist'` in a single table. Read tools (`list_games`, `sleeve_summary`, `search_games_semantic`, `library_data`, `games_names`) MUST default to `status='owned'`. Opt-in via `status='wishlist'`/`'any'` where it makes sense. Forgetting this leaks wishlist into collection counts.
- **BGG media backfill hook fires post-write.** `_backfill_bgg_media(gid)` is called from `add_game`, `update_game`, `add_to_wishlist`, `update_wishlist` to patch `thumbnail_url`/`image_url` via `etl/bgg_api.fetch_thing()` when `bgg_id` is set but URLs are empty. If you add a write tool that mutates `bgg_id`, call the hook too.
- **Auth model: guest = read-only, owner = full.** Due ruoli. Guest (no cookie) vede tutto in read mode + può chattare (tool gating: write tools rimossi dal registry prima del loop). Owner (cookie firmato) può scrivere; chat condivisa tra owner. Quando aggiungi un endpoint che scrive su DB/fs **devi** mettere `user: dict | None = Depends(get_current_user)` + `require_owner(user)` come prima riga; quando aggiungi un tool che muta stato **devi** aggiungerlo a `tools.WRITE_TOOLS`. Audit `_source` formato: `chat:{id}/user:{name}` (chat owner), `web:{page}/user:{name}` (REST owner), `chat:guest` (chat guest). Per gestire utenti: `uv run python etl/create_user.py create|reset|list`.

## Environment

- `ANTHROPIC_API_KEY` — Anthropic Console key (separate from claude.ai Pro; Pro does NOT include API).
- `LLM_PROVIDER` — `anthropic` | `deepseek` | `ollama`. Code default is `anthropic`, but deployed `.env` sets `deepseek` (the actual production provider). Per-request, no restart.
- `DEEPSEEK_API_KEY` — required when `LLM_PROVIDER=deepseek` AND for `/library/filter` (which is always DeepSeek regardless of provider).
- `LLM_MODEL`, `DEEPSEEK_BASE_URL`, `OLLAMA_BASE_URL` — optional overrides.
- `LIBRARY_FILTER_MODEL` — override the DeepSeek model used by `/library/filter` (default `deepseek-chat`).
- `BGG_API_TOKEN` — required since 2026-04 (BGG XML API is Cloudflare-gated, both v1 and v2). Public-page scraping via web_search was tried and failed (JS-rendered widgets — see LEARNINGS).
- `BOARDY_DB` — optional, overrides DB path. Used by `docker-compose.yml` to point at `/data/boardy.db` (named volume). Defaults to `<repo>/data/boardy.db`.
- `BOARDY_SESSION_SECRET` — **required** in production. Chiave per firmare il cookie di sessione owner. Genera con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Senza, l'app crasha al primo `/auth/login`. Ruotarla invalida tutti i cookie esistenti (logout globale).
- `BOARDY_COOKIE_SECURE` — `1` in produzione HTTPS (cookie marcato `Secure`, browser rifiuta su HTTP); lascia unset in dev locale (`http://localhost`).
- `CF_TUNNEL_TOKEN` — Cloudflare Tunnel token. Required ONLY for `docker compose --profile tunnel` (self-host deploy). Generated by the tunnel owner in the CF dashboard (Zero Trust → Networks → Tunnels → Create → token).
- First run downloads ~1GB to `~/.cache/huggingface/` for the e5 model. Subsequent loads ~3s. In the Docker image the model is pre-cached at build time → no first-boot download.

## Deploy / Self-host (Docker)

Docker files live in `deploy/`. The compose file pins `name: boardy` so the volume is always `boardy_boardy_db` regardless of where you invoke from. Two modes from one `deploy/docker-compose.yml`:

```bash
docker compose -f deploy/docker-compose.yml up -d --build              # local: boardy on http://127.0.0.1:8765
docker compose -f deploy/docker-compose.yml --profile tunnel up -d     # server: boardy + cloudflared, public via CF Tunnel
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
