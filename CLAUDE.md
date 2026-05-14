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
rulebooks/   PDF rulebooks (gitignored — copyright + bulky)
data/        Source data + DB backups (Excel + *.db.bak; runtime DB stays at repo root)
archive/     Legacy code from abandoned approaches (e.g. Ollama exploration). Read-only history.
secondbrain/ Owner's Obsidian vault; memos about Boardy live in `memo-*.md`. DO NOT write without being asked.
.claude/     Claude Code's own state (auto-managed)
```

## Where to look

- `app/chat.py` — provider-agnostic tool-use loop, up to 8 rounds. Auto-injects `_source="chat:{conv_id}"` into write tools via `inspect.signature`.
- `app/tools.py` — all tools. Adding one = function + JSON schema in `TOOLS` + entry in `TOOL_FUNCS`. Write tools must declare `_source: str | None = None`.
- `app/llm.py` — `Provider` ABC. `AnthropicProvider` (default, `claude-sonnet-4-6` + server-side `web_search_20250305`) and `OllamaProvider`. Selection per-request via `LLM_PROVIDER`.
- `app/schema.py` — star schema DDL + idempotent v1→v2 migration on every boot.
- `app/audit.py` — every write to `games`/`sleeve_requirements`/`sleeve_inventory` logs to `changes`.
- `app/games_semantic.py` — hybrid SQL+cosine over `games.description_embedding`. Reuses `_model_lazy()` from `rulebooks.py` (single 280MB load).
- `app/rulebooks.py` — pypdf chunking + e5 embeddings + brute-force cosine.
- `web/index.html` — single-file UI, vanilla JS + `marked.js`. No build step.

## Companion docs (read before non-trivial work)

- `LEARNINGS.md` — **read first**. Tribal knowledge: gotchas, decisions, user preferences accumulated across sessions.
- `TODO.md` — actionable backlog with priorities. Consult when the user asks "what's next?".
- `secondbrain/memo-boardy-future.md` — long-form rationale behind TODO items. Open when a TODO needs context.
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

## Environment

- `ANTHROPIC_API_KEY` — Anthropic Console key (separate from claude.ai Pro; Pro does NOT include API).
- `LLM_PROVIDER` — `anthropic` (default) or `ollama`. Per-request, no restart.
- `LLM_MODEL`, `OLLAMA_BASE_URL` — optional overrides.
- `BGG_API_TOKEN` — required since 2026-04 (BGG XML API is Cloudflare-gated, both v1 and v2). Public-page scraping via web_search was tried and failed (JS-rendered widgets — see LEARNINGS).
- `BOARDY_DB` — optional, overrides DB path. Used by `docker-compose.yml` to point at `/data/boardy.db` (named volume). Defaults to `<repo>/boardy.db`.
- `CF_TUNNEL_TOKEN` — Cloudflare Tunnel token. Required ONLY for `docker compose --profile tunnel` (self-host deploy). Generated by the tunnel owner in the CF dashboard (Zero Trust → Networks → Tunnels → Create → token).
- First run downloads ~1GB to `~/.cache/huggingface/` for the e5 model. Subsequent loads ~3s. In the Docker image the model is pre-cached at build time → no first-boot download.

## Deploy / Self-host (Docker)

Two modes from one `docker-compose.yml`:

```bash
docker compose up -d --build              # local: boardy on http://127.0.0.1:8765
docker compose --profile tunnel up -d     # server: boardy + cloudflared, public via CF Tunnel
```

**Update workflow** on the server: `git pull && docker compose restart boardy` — Python code and HTML are bind-mounted, no rebuild. Rebuild image (`up -d --build`) ONLY when `pyproject.toml` / `uv.lock` change.

**State**: `boardy.db` lives in the `boardy_db` named volume (survives image rebuilds); `rulebooks/` is bind-mounted from host; `.env` is bind-mounted read-only. The e5 model is baked into the image.

**Cloudflare Tunnel setup** (one-time, on the host that runs Docker):
1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel → copy the **token**.
2. In Public Hostname tab: add `boardy.<your-domain>.tld` → service `http://boardy:8765`. (`boardy` here is the container hostname inside the Docker network.)
3. Put `CF_TUNNEL_TOKEN=...` in `.env` on the server.
4. `docker compose --profile tunnel up -d`. The tunnel comes up, hostname resolves, TLS handled by Cloudflare. No port forwarding needed on the host.

The Docker image bakes the e5 model (~1.5GB total). First build ~3-5 min, subsequent rebuilds (deps unchanged) under 1 min thanks to layer cache.
