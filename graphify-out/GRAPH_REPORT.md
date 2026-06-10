# Graph Report - .  (2026-06-10)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 597 nodes · 1076 edges · 32 communities (28 shown, 4 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 9 edges (avg confidence: 0.76)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6ce96bcd`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Authentication Middleware|Authentication Middleware]]
- [[_COMMUNITY_Chat Provider Interface|Chat Provider Interface]]
- [[_COMMUNITY_Semantic Search|Semantic Search]]
- [[_COMMUNITY_Application Core|Application Core]]
- [[_COMMUNITY_Rulebook Download|Rulebook Download]]
- [[_COMMUNITY_Telegram Bot|Telegram Bot]]
- [[_COMMUNITY_BGG Metadata Backfill|BGG Metadata Backfill]]
- [[_COMMUNITY_BGG Tools|BGG Tools]]
- [[_COMMUNITY_Game Update & Search|Game Update & Search]]
- [[_COMMUNITY_Audit Logging|Audit Logging]]
- [[_COMMUNITY_Conversation Persistence|Conversation Persistence]]
- [[_COMMUNITY_Database Migrations|Database Migrations]]
- [[_COMMUNITY_User Authentication|User Authentication]]
- [[_COMMUNITY_Friendly Tags|Friendly Tags]]
- [[_COMMUNITY_Oracle Cloud Provisioner|Oracle Cloud Provisioner]]
- [[_COMMUNITY_SYG API Client|SYG API Client]]
- [[_COMMUNITY_BGG Files Discovery|BGG Files Discovery]]
- [[_COMMUNITY_Game & Wishlist CRUD|Game & Wishlist CRUD]]
- [[_COMMUNITY_OneJour Rulebook Search|OneJour Rulebook Search]]
- [[_COMMUNITY_UI Modules|UI Modules]]
- [[_COMMUNITY_Excel Import ETL|Excel Import ETL]]
- [[_COMMUNITY_Game & Wishlist Queries|Game & Wishlist Queries]]
- [[_COMMUNITY_Frontend Auth|Frontend Auth]]
- [[_COMMUNITY_Delete Operations|Delete Operations]]
- [[_COMMUNITY_Rulebook Discovery|Rulebook Discovery]]
- [[_COMMUNITY_Language Model Testing|Language Model Testing]]
- [[_COMMUNITY_Hooks Configuration|Hooks Configuration]]
- [[_COMMUNITY_Sleeve Readiness|Sleeve Readiness]]
- [[_COMMUNITY_Sleeve Requirements|Sleeve Requirements]]
- [[_COMMUNITY_Inventory Management|Inventory Management]]

## God Nodes (most connected - your core abstractions)
1. `get_conn()` - 82 edges
2. `update_game()` - 15 edges
3. `migrate()` - 14 edges
4. `BGGSession` - 14 edges
5. `require_owner()` - 13 edges
6. `chat()` - 12 edges
7. `Connection` - 10 edges
8. `generate_for_game()` - 9 edges
9. `OllamaProvider` - 9 edges
10. `ingest_bytes()` - 9 edges

## Surprising Connections (you probably didn't know these)
- `web/index.html` --references--> `app/main.py`  [INFERRED]
  web/index.html → app/main.py
- `web/library.html` --references--> `app/main.py`  [INFERRED]
  web/library.html → app/main.py
- `web/login.html` --references--> `app/auth.py`  [INFERRED]
  web/login.html → app/auth.py
- `docs/LEARNINGS.md` --cites--> `etl/onejour_api.py`  [EXTRACTED]
  docs/LEARNINGS.md → etl/onejour_api.py
- `secondbrain/memo-telegram-bot.md` --references--> `bot/telegram_bot.py`  [EXTRACTED]
  secondbrain/memo-telegram-bot.md → bot/telegram_bot.py

## Import Cycles
- 1-file cycle: `etl/backfill_bgg.py -> etl/backfill_bgg.py`

## Communities (32 total, 4 thin omitted)

### Community 0 - "Authentication Middleware"
Cohesion: 0.06
Nodes (54): Helper: alza 401 se user è None. Da chiamare negli endpoint di scrittura., require_owner(), auth_login(), auth_logout(), auth_me(), _build_filter_system_prompt(), chat_endpoint(), ChatRequest (+46 more)

### Community 1 - "Chat Provider Interface"
Cohesion: 0.08
Nodes (40): ABC, Anthropic, _accepts_source(), _build_system_prompt(), chat(), _filter_tools_for_user(), _log(), Any (+32 more)

### Community 2 - "Semantic Search"
Cohesion: 0.07
Nodes (43): embed_one(), _embed_passages(), _embed_query(), excluded_from_search(), _hash_text(), ndarray, Semantic search over `games.description`.  Hybrid search pattern: 1. SQL filters, Hybrid search: SQL filters + cosine on description embedding.      Returns rows (+35 more)

### Community 3 - "Application Core"
Cohesion: 0.10
Nodes (39): app/audit.py, app/auth.py, app/chat.py, app/games_semantic.py, app/llm.py, app/main.py, app/rulebooks.py, app/schema.py (+31 more)

### Community 4 - "Rulebook Download"
Cohesion: 0.07
Nodes (31): _backfill_rulebook(), download_rulebook(), _fetch_pdf_bytes(), Normalized title with trailing rulebook-words removed, for match scoring., Post-commit hook: auto-fetch a rulebook for a freshly added game IFF a     confi, Download `url` and verify it's actually a PDF (magic bytes).      Mirrors the br, Download a rulebook and index it under `game_name`. Provide EITHER `url`     (a, _rulebook_core() (+23 more)

### Community 5 - "Telegram Bot"
Cohesion: 0.11
Nodes (28): Application, BoardyClient, build_app(), cmd_help(), cmd_new(), cmd_start(), cmd_whoami(), _ensure_state() (+20 more)

### Community 6 - "BGG Metadata Backfill"
Cohesion: 0.11
Nodes (31): Element, apply_cli(), _apply_one(), main(), _missing_fields(), phase1(), phase2(), Backfill BGG metadata using the official XML API (v2).  Three phases — see CLAUD (+23 more)

### Community 7 - "BGG Tools"
Cohesion: 0.07
Nodes (27): add_to_inventory(), ask_rules(), bgg_lookup(), bgg_search(), ingest_rulebook(), list_dimension(), list_rulebooks(), mark_as_owned() (+19 more)

### Community 8 - "Game Update & Search"
Cohesion: 0.10
Nodes (27): _clear_requirements_if_done(), Tavily-backed web search. Returns top results with FULL-PAGE content.      By de, Patch fields on an existing game. Only non-null args are updated. Lists REPLACE, If new_status is a 'done' status, drop pending requirements for the game.      R, update_game(), web_search(), build_update_kwargs(), deepseek_extract() (+19 more)

### Community 9 - "Audit Logging"
Cohesion: 0.11
Nodes (23): log_change(), log_diff(), log_full(), Any, Connection, Audit log for write operations on the Boardy DB.  Every mutation (insert/update/, Read the last N audit rows. Optionally filter by table and/or row_id., Compact JSON for storage. None stays None. (+15 more)

### Community 10 - "Conversation Persistence"
Cohesion: 0.13
Nodes (22): create_conversation(), delete_conversation(), _extract_text(), _first_exchange(), _generate_title_llm(), get_conversation(), list_conversations(), migrate() (+14 more)

### Community 11 - "Database Migrations"
Cohesion: 0.18
Nodes (23): _ensure_dim_and_bridge(), _has_column(), migrate(), _migrate_v1_games(), _migrate_v3_drop_sleeve_raw(), _migrate_v4_description_embedding(), _migrate_v6_wishlist(), _migrate_v7_users() (+15 more)

### Community 12 - "User Authentication"
Cohesion: 0.14
Nodes (20): authenticate(), clear_session_cookie(), get_current_user(), hash_password(), Response, Authentication for Boardy — username/password locale + cookie firmato.  Threat m, FastAPI dependency: ritorna user dict o None (guest).      Non lancia errori: la, Itsdangerous serializer scoped al salt 'boardy-session-v1'.      Cambiare il sal (+12 more)

### Community 13 - "Friendly Tags"
Cohesion: 0.16
Nodes (16): SQLite connection helper. One read/write connection per request., backfill_one(), _build_user_payload(), _client(), generate_for_game(), _parse_and_validate(), persist(), User-friendly tag generation via LLM.  Background: i `categories`/`mechanics` BG (+8 more)

### Community 14 - "Oracle Cloud Provisioner"
Cohesion: 0.18
Nodes (15): build_clients(), discover_ads(), _env_default(), is_capacity_error(), main(), make_launch_details(), notify_telegram(), parse_args() (+7 more)

### Community 15 - "SYG API Client"
Cohesion: 0.18
Nodes (15): autocomplete(), _cards_to_requirements(), fetch_game(), _http_get_json(), lookup(), parse_game(), sleeveyourgames.com private JSON API client (deterministic, no LLM).  Reverse-en, Normalize a `/game/{id}` payload into the bits Boardy needs.      Returns base-g (+7 more)

### Community 16 - "BGG Files Discovery"
Cohesion: 0.21
Nodes (13): _as_text(), BGGFilesError, find_rulebooks(), _http_get_json(), list_files(), _parse_file(), BoardGameGeek Files discovery (deterministic, no browser, no auth).  BGG's per-g, All files attached to a BGG game (paginated under the hood). (+5 more)

### Community 17 - "Game & Wishlist CRUD"
Cohesion: 0.21
Nodes (13): add_game(), add_to_wishlist(), _backfill_bgg_media(), _backfill_friendly_tags(), Generate + persist friendly_tags for a game (best-effort, post-commit).      Pai, Insert a wishlist item. Fails if `name` already exists (owned or wishlist)., Patch a wishlist row. Refuses if the row is already owned — use     `update_game, Replace bridge rows for a game with the given list (idempotent). None = leave un (+5 more)

### Community 18 - "OneJour Rulebook Search"
Cohesion: 0.27
Nodes (9): guess_lang(), _http_get(), OneJourError, 1jour-1jeu.com (1j1ju) rulebook search client (deterministic, no LLM).  1j1ju ho, Search 1j1ju for rulebook PDFs matching `query` (a game name).      Returns `[{t, Raised on any non-200 / unreadable response from 1j1ju., Best-effort language tag from a rulebook PDF filename. '?' if unknown., Low-level GET → HTML text, with browser headers, pacing, on-disk cache. (+1 more)

### Community 19 - "UI Modules"
Cohesion: 0.43
Nodes (8): Boardy Auth Module, BoardGameGeek, Boardy — Chat, Boardy — Libreria, marked.js Library, Powered by BGG Logo, Boardy — Buste, Boardy — Lista desideri

### Community 20 - "Excel Import ETL"
Cohesion: 0.39
Nodes (7): classify_sleeve(), main(), parse_int(), parse_players(), Excel -> SQLite ETL for Boardy.  Reads sheet 'Elenco Premium' from `data/ElencoG, Return (status, [(count, w, h, note), ...]).      Status is one of: sleeved, na,, _upsert_dim()

### Community 21 - "Game & Wishlist Queries"
Cohesion: 0.29
Nodes (7): get_game(), list_games(), list_wishlist(), List wishlist items as `{count, items}`. Optional `priority` filter.      Each i, Return matching games as `{count, items}`. Filters AND-combined; *_contains are, Get one game (case-insensitive name match) with all dimensions + sleeve requirem, _row_to_game_dict()

### Community 22 - "Frontend Auth"
Cohesion: 0.38
Nodes (3): _injectCss(), mountBadge(), state()

### Community 23 - "Delete Operations"
Cohesion: 0.40
Nodes (5): delete_game(), _game_dims(), Delete a wishlist row. Refuses if the row is owned (use `delete_game`)., Remove a game (cascade deletes dim links + sleeve requirements)., remove_from_wishlist()

### Community 24 - "Rulebook Discovery"
Cohesion: 0.50
Nodes (4): _bgg_candidates(), find_rulebook(), Rulebook candidates from BGG Files (no download — just metadata)., Find downloadable rulebooks for a game. Does NOT download.      Returns candidat

### Community 25 - "Language Model Testing"
Cohesion: 0.67
Nodes (3): main(), Smoke test: does Qwen2.5 7B handle Boardy's tools well enough?  Runs 5 realistic, run_case()

## Knowledge Gaps
- **10 isolated node(s):** `Stop`, `URLSafeSerializer`, `Connection`, `UploadFile`, `SentenceTransformer` (+5 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_conn()` connect `Conversation Persistence` to `Authentication Middleware`, `Chat Provider Interface`, `Semantic Search`, `Rulebook Download`, `BGG Metadata Backfill`, `BGG Tools`, `Game Update & Search`, `Audit Logging`, `Database Migrations`, `User Authentication`, `Friendly Tags`, `Game & Wishlist CRUD`, `Game & Wishlist Queries`, `Delete Operations`, `Sleeve Readiness`, `Sleeve Requirements`, `Inventory Management`?**
  _High betweenness centrality (0.287) - this node is a cross-community bridge._
- **Why does `BGGError` connect `BGG Metadata Backfill` to `Rulebook Download`?**
  _High betweenness centrality (0.109) - this node is a cross-community bridge._
- **Why does `SYGError` connect `SYG API Client` to `Rulebook Download`?**
  _High betweenness centrality (0.042) - this node is a cross-community bridge._
- **What connects `Stop`, `Audit log for write operations on the Boardy DB.  Every mutation (insert/update/`, `Compact JSON for storage. None stays None.` to the rest of the system?**
  _204 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Authentication Middleware` be split into smaller, more focused modules?**
  _Cohesion score 0.06127946127946128 - nodes in this community are weakly interconnected._
- **Should `Chat Provider Interface` be split into smaller, more focused modules?**
  _Cohesion score 0.07535460992907801 - nodes in this community are weakly interconnected._
- **Should `Semantic Search` be split into smaller, more focused modules?**
  _Cohesion score 0.07272727272727272 - nodes in this community are weakly interconnected._