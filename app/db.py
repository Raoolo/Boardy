"""SQLite connection helper. One read/write connection per request."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Default: <repo>/data/boardy.db (local-dev location).
# Override via BOARDY_DB env var to point at a persistent volume in Docker
# (`/data/boardy.db`) — set in docker-compose.yml.
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "boardy.db"
DB_PATH = Path(os.environ.get("BOARDY_DB") or _DEFAULT_DB)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
