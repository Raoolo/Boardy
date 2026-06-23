"""Star-schema migration for Boardy.

Run `migrate()` at boot. Idempotent:
- Creates new dimension/bridge tables if missing.
- Detects old flat `games` schema (has `producer`/`publisher` columns)
  and rewrites it into the new wide-dimension shape, splitting comma-
  separated designers/publishers into bridge rows.

The fact tables `sleeve_requirements`, `sleeve_inventory`, and the
operational `conversations` table are unchanged.
"""
from __future__ import annotations

import sqlite3

from .db import get_conn


NEW_GAMES_DDL = """
CREATE TABLE games (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  bgg_id INTEGER UNIQUE,
  year_published INTEGER,
  players_min INTEGER,
  players_max INTEGER,
  players_best TEXT,
  duration_min INTEGER,
  duration_min_min INTEGER,
  duration_max_min INTEGER,
  age_min INTEGER,
  complexity_label TEXT,
  complexity_weight REAL,
  bgg_rating REAL,
  description TEXT,
  thumbnail_url TEXT,
  image_url TEXT,
  language TEXT,
  condition TEXT,
  notes TEXT,
  sleeve_status TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

DIMENSIONS = ("designers", "publishers", "categories", "mechanics")
BRIDGES = {
    "game_designers":  "designer_id",
    "game_publishers": "publisher_id",
    "game_categories": "category_id",
    "game_mechanics":  "mechanic_id",
}


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _ensure_dim_and_bridge(conn: sqlite3.Connection, dim: str, bridge: str, fk: str) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {dim} (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {bridge} (
              game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
              {fk} INTEGER NOT NULL REFERENCES {dim}(id) ON DELETE CASCADE,
              PRIMARY KEY (game_id, {fk})
            )"""
    )


def _migrate_v1_games(conn: sqlite3.Connection) -> None:
    """Rewrite the v1 flat `games` table into the new wide-dimension shape.

    v1 columns: id, name, producer, publisher, players, players_min, players_max,
                duration_min, complexity, condition, sleeve_status, sleeve_raw.
    """
    # 1) Snapshot v1 rows.
    v1_rows = conn.execute(
        "SELECT id, name, producer, publisher, players, players_min, players_max, "
        "duration_min, complexity, condition, sleeve_status, sleeve_raw FROM games"
    ).fetchall()

    # 2) Rename old table out of the way.
    conn.execute("ALTER TABLE games RENAME TO games_v1_backup")

    # 3) Create new shape.
    conn.executescript(NEW_GAMES_DDL)

    # 4) Build dim/bridges (now that `games` exists with new shape).
    for dim, fk in BRIDGES.items():
        # dim is bridge name; map to dim singular root via DIMENSIONS list
        pass
    # Properly: iterate DIMENSIONS+BRIDGES
    for dim_name, (bridge_name, fk_col) in zip(DIMENSIONS, BRIDGES.items()):
        _ensure_dim_and_bridge(conn, dim_name, bridge_name, fk_col)

    # 5) Copy core columns; map old field names → new.
    for r in v1_rows:
        conn.execute(
            """INSERT INTO games(id, name, players_min, players_max, players_best,
                                 duration_min, complexity_label, condition,
                                 sleeve_status, sleeve_raw)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                r["id"], r["name"], r["players_min"], r["players_max"], r["players"],
                r["duration_min"], r["complexity"], r["condition"],
                r["sleeve_status"], r["sleeve_raw"],
            ),
        )

        # Split CSV into designers/publishers bridges.
        for d in _split_csv(r["producer"]):
            conn.execute("INSERT OR IGNORE INTO designers(name) VALUES(?)", (d,))
            did = conn.execute("SELECT id FROM designers WHERE name=?", (d,)).fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO game_designers(game_id, designer_id) VALUES(?,?)", (r["id"], did))

        for p in _split_csv(r["publisher"]):
            conn.execute("INSERT OR IGNORE INTO publishers(name) VALUES(?)", (p,))
            pid = conn.execute("SELECT id FROM publishers WHERE name=?", (p,)).fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO game_publishers(game_id, publisher_id) VALUES(?,?)", (r["id"], pid))

    # 6) Drop the v1 table.
    conn.execute("DROP TABLE games_v1_backup")


RULEBOOKS_DDL = """
CREATE TABLE IF NOT EXISTS rulebooks (
  id INTEGER PRIMARY KEY,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  source_path TEXT,                      -- original PDF path or source URL
  language TEXT,
  page_count INTEGER,
  ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  embedding_model TEXT NOT NULL,
  pdf_blob BLOB,                         -- the raw PDF bytes (v9): single backup artifact
  UNIQUE(game_id, source_path)
);

CREATE TABLE IF NOT EXISTS rulebook_chunks (
  id INTEGER PRIMARY KEY,
  rulebook_id INTEGER NOT NULL REFERENCES rulebooks(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  page_start INTEGER,
  page_end INTEGER,
  text TEXT NOT NULL,
  embedding BLOB NOT NULL                -- float32 numpy array, raw bytes
);

CREATE INDEX IF NOT EXISTS idx_chunks_rulebook ON rulebook_chunks(rulebook_id);
"""

CHANGES_DDL = """
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  table_name TEXT NOT NULL,        -- 'games' | 'sleeve_requirements' | 'sleeve_inventory'
  row_id INTEGER,                   -- PK of the affected row (NULL allowed for cascades)
  row_label TEXT,                   -- human-readable handle (game name, "70x110/Mayday", ...)
  action TEXT NOT NULL,             -- 'insert' | 'update' | 'delete'
  field TEXT,                       -- column name; NULL for insert/delete (whole-row events)
  old_value TEXT,                   -- JSON-encoded
  new_value TEXT,                   -- JSON-encoded
  source TEXT NOT NULL DEFAULT 'unknown'  -- 'chat:{conv_id}' | 'etl' | 'backfill_v2' | 'manual'
);
CREATE INDEX IF NOT EXISTS idx_changes_table_row ON changes(table_name, row_id);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON changes(ts DESC);
"""


def _migrate_v3_drop_sleeve_raw(conn: sqlite3.Connection) -> None:
    """v3: drop `games.sleeve_raw` (Excel-import artifact, redundant).

    Idempotent — only runs if the column still exists. Audit-logs each
    non-null value before the ALTER so the data is recoverable from
    `changes` if ever needed.

    Also collapses any leftover `sleeve_status='no'` into `'na'` since
    the two values now mean the same thing ("doesn't need sleeving").
    """
    if _has_column(conn, "games", "sleeve_raw"):
        # Lazy import to avoid circular: audit imports nothing app-level
        # but stays low-coupling here.
        from . import audit
        for r in conn.execute(
            "SELECT id, name, sleeve_raw FROM games WHERE sleeve_raw IS NOT NULL"
        ).fetchall():
            audit.log_change(
                conn, table="games", row_id=r["id"], row_label=r["name"],
                action="update", field="sleeve_raw",
                old=r["sleeve_raw"], new=None,
                source="schema_v3_drop_raw",
            )
        conn.execute("ALTER TABLE games DROP COLUMN sleeve_raw")

    # Defensive: collapse any 'no' rows. After cleanup_sleeve.py this is
    # usually a no-op, but covers fresh imports that still emit 'no'.
    rows = conn.execute(
        "SELECT id, name FROM games WHERE sleeve_status='no'"
    ).fetchall()
    if rows:
        from . import audit
        for r in rows:
            audit.log_change(
                conn, table="games", row_id=r["id"], row_label=r["name"],
                action="update", field="sleeve_status",
                old="no", new="na", source="schema_v3_collapse_no_to_na",
            )
        conn.execute("UPDATE games SET sleeve_status='na' WHERE sleeve_status='no'")


def _migrate_v6_wishlist(conn: sqlite3.Connection) -> None:
    """v6: extend `games` with a `status` column + wishlist-only fields.

    Why one table instead of a separate `wishlist`: BGG enrichment, the
    description embedding, audit logging, and dimension bridges already work
    on `games`. Promoting a wishlist item to owned becomes a one-column
    UPDATE — no row migration, BGG data preserved. The cost is adding
    `WHERE status='owned'` to a handful of read queries (library_data,
    list_games, sleeve_summary, search_games_semantic), bounded and explicit.

    Columns:
    - `status`: 'owned' | 'wishlist'. NOT NULL default 'owned' — every
      pre-existing row is, by definition, a game the user owns.
    - `priority`: 'high' | 'medium' | 'low' | NULL. Only meaningful for
      wishlist rows; NULL on owned (and preserved if a wishlist row is
      promoted, so we keep the historical context).
    - `notes_wishlist`: free-text. Separate from `notes` so the existing
      ETL-driven `notes` (BGG backfill prose, etc.) stays untouched.
    - `target_price`: optional EUR target. NULL means no target set.
    """
    if not _has_column(conn, "games", "status"):
        # ALTER TABLE ADD COLUMN with NOT NULL + literal DEFAULT works in SQLite.
        conn.execute("ALTER TABLE games ADD COLUMN status TEXT NOT NULL DEFAULT 'owned'")
    if not _has_column(conn, "games", "priority"):
        conn.execute("ALTER TABLE games ADD COLUMN priority TEXT")
    if not _has_column(conn, "games", "notes_wishlist"):
        conn.execute("ALTER TABLE games ADD COLUMN notes_wishlist TEXT")
    if not _has_column(conn, "games", "target_price"):
        conn.execute("ALTER TABLE games ADD COLUMN target_price REAL")
    # Cheap index — used by every read query that filters by status.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_games_status ON games(status)")


def _migrate_v7_users(conn: sqlite3.Connection) -> None:
    """v7: add `users` table for auth (owner login, guest = unauthenticated).

    Why a separate table instead of just a hardcoded list in .env:
    - Hashed passwords belong in the DB, not in a config file checked into git.
    - `created_at` lets us audit who joined when.
    - `role` lets us add 'admin' / read-only roles later without another migration.

    Why no per-user columns on `games` / `conversations`:
    - Collezione condivisa: tutti gli owner vedono e modificano lo stesso
      inventario (decisione 2026-05-14). Stessa cosa per le chat.
    - Solo l'audit log (`changes.source`) traccia CHI ha fatto cosa
      tramite `_source = "chat:{conv_id}/user:{username}"`.
    """
    if not _table_exists(conn, "users"):
        conn.execute(
            """CREATE TABLE users (
                 id INTEGER PRIMARY KEY,
                 username TEXT NOT NULL UNIQUE,
                 password_hash TEXT NOT NULL,
                 role TEXT NOT NULL DEFAULT 'owner',
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )


def _migrate_v4_description_embedding(conn: sqlite3.Connection) -> None:
    """v4: add description embedding columns to games (idempotent).

    `description_embedding`: float32 raw bytes (e5-base = 768 dims = 3072 B).
    `description_hash`: SHA1 of the description text used to embed; lets us
    skip re-embedding when nothing changed.
    `description_skip_reason`: free-text reason why automatic backfill
    skipped this game. Cleared once the description is filled. Lets us
    revisit the laggards later instead of re-running the whole pipeline.
    """
    if not _has_column(conn, "games", "description_embedding"):
        conn.execute("ALTER TABLE games ADD COLUMN description_embedding BLOB")
    if not _has_column(conn, "games", "description_hash"):
        conn.execute("ALTER TABLE games ADD COLUMN description_hash TEXT")
    if not _has_column(conn, "games", "description_skip_reason"):
        conn.execute("ALTER TABLE games ADD COLUMN description_skip_reason TEXT")


def _migrate_v8_friendly_tags(conn: sqlite3.Connection) -> None:
    """v8: add `games.friendly_tags` for user-friendly LLM-generated tags.

    Stored as a JSON-encoded array of strings (e.g. `["rilassante","cooperativo"]`).
    Vocabolario fisso in `app/friendly_tags.py` — il modello deve scegliere
    SOLO da quella lista, niente vocabolario aperto (rompe la ricercabilita').
    NULL = non ancora generato. `[]` = generato ma il modello non ha trovato
    match validi (raro). No index: il filtraggio e' client-side (multi-select)
    perche' il numero di righe e' piccolo (<200) e qualunque indice su una
    LIKE su JSON non aiuta.
    """
    if not _has_column(conn, "games", "friendly_tags"):
        conn.execute("ALTER TABLE games ADD COLUMN friendly_tags TEXT")


def _migrate_v9_rulebook_pdf_blob(conn: sqlite3.Connection) -> None:
    """v9: store the raw PDF bytes inside the DB (`rulebooks.pdf_blob`).

    Decision (2026-06-09): the rulebook PDF lives in SQLite, not on disk, so a
    single boardy.db backup carries everything (PDF + chunks + embeddings) and
    is fully portable. The searchable content (chunks + embeddings) was already
    in the DB; this adds the source artifact so the file is re-exportable.
    Idempotent — only adds the column when missing. Pre-existing rows keep
    pdf_blob NULL (their on-disk source is gone after this change; re-ingest to
    backfill the blob).
    """
    if not _has_column(conn, "rulebooks", "pdf_blob"):
        conn.execute("ALTER TABLE rulebooks ADD COLUMN pdf_blob BLOB")


def _migrate_v10_rulebook_ocr_report(conn: sqlite3.Connection) -> None:
    """v10: store the OCR ingest report (`rulebooks.ocr_report`, JSON text).

    Decision (2026-06-23): rulebooks can now be ingested from PHOTOS via a vision
    model (see `app/ocr.py`). Unlike a clean PDF, a photo scan carries quality
    caveats — illegible pages, missing page numbers, detected gaps. We persist
    that report next to the rulebook so the UI/bot can re-show "what was read and
    what to recheck" later. NULL for PDF-sourced rulebooks. Idempotent.
    """
    if not _has_column(conn, "rulebooks", "ocr_report"):
        conn.execute("ALTER TABLE rulebooks ADD COLUMN ocr_report TEXT")


def migrate() -> None:
    with get_conn() as conn:
        # If `games` doesn't exist at all, nothing to do — ETL will create it.
        if not _table_exists(conn, "games"):
            return

        if _has_column(conn, "games", "producer"):
            # v1 flat schema → migrate
            _migrate_v1_games(conn)

        # Ensure all dim+bridge tables exist (in case the DB was recreated fresh by ETL).
        for dim_name, (bridge_name, fk_col) in zip(DIMENSIONS, BRIDGES.items()):
            _ensure_dim_and_bridge(conn, dim_name, bridge_name, fk_col)

        # Rulebook tables (Step 2: RAG)
        conn.executescript(RULEBOOKS_DDL)

        # Audit log (Step 3: history of writes)
        conn.executescript(CHANGES_DDL)

        # v3: drop sleeve_raw + collapse 'no'→'na' (idempotent).
        # Must run AFTER changes table exists since it audit-logs the drop.
        _migrate_v3_drop_sleeve_raw(conn)

        # v4: semantic-search columns on games.
        _migrate_v4_description_embedding(conn)

        # v6: wishlist columns + status fence.
        _migrate_v6_wishlist(conn)

        # v7: users table for owner login.
        _migrate_v7_users(conn)

        # v8: friendly_tags column on games.
        _migrate_v8_friendly_tags(conn)

        # v9: store raw PDF bytes inside rulebooks (DB-as-single-source).
        _migrate_v9_rulebook_pdf_blob(conn)

        # v10: store the OCR ingest report for photo-sourced rulebooks.
        _migrate_v10_rulebook_ocr_report(conn)

        conn.commit()
