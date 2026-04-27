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
  sleeve_raw TEXT,
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
  source_path TEXT,                      -- original PDF path or URL
  language TEXT,
  page_count INTEGER,
  ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  embedding_model TEXT NOT NULL,
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

        conn.commit()
