"""Pytest setup: point the app at a throwaway SQLite DB.

`app/db.py` resolves `DB_PATH` at import time from `BOARDY_DB`, so we MUST set
the env var before any app module is imported — hence doing it here (conftest is
imported first by pytest) rather than in a fixture.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="boardy-tests-")
os.environ["BOARDY_DB"] = str(Path(_TMP_DIR) / "test_boardy.db")
os.environ.setdefault("BOARDY_SESSION_SECRET", "test-secret-not-for-prod")

from app import schema  # noqa: E402
from app import conversations as conv  # noqa: E402
from app.db import get_conn  # noqa: E402

# Fact tables live in the ETL bootstrap, not in schema.migrate(). Mirror their
# DDL here so the sleeve tools have somewhere to write during tests.
_SLEEVE_FACT_DDL = """
CREATE TABLE IF NOT EXISTS sleeve_requirements (
  id INTEGER PRIMARY KEY,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  count INTEGER NOT NULL,
  width_mm REAL NOT NULL,
  height_mm REAL NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS sleeve_inventory (
  id INTEGER PRIMARY KEY,
  width_mm REAL NOT NULL,
  height_mm REAL NOT NULL,
  count_owned INTEGER NOT NULL DEFAULT 0,
  brand TEXT,
  UNIQUE(width_mm, height_mm, brand)
);
"""


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    # migrate() early-returns when `games` doesn't exist (it expects the ETL to
    # bootstrap the base table). Create it from the real DDL first, then let the
    # migrations layer on status/wishlist/friendly_tags/rulebook columns.
    with get_conn() as c:
        if not schema._table_exists(c, "games"):
            c.executescript(schema.NEW_GAMES_DDL)
        # Fact tables are ETL-owned (migrate() leaves them alone), so create them
        # here too — the sleeve tools need them.
        c.executescript(_SLEEVE_FACT_DDL)
        c.commit()
    schema.migrate()
    conv.migrate()
    yield


@pytest.fixture
def owned_game():
    """A throwaway owned game with sleeve_status='to_sleeve' (accepts requirements).

    Inserted via raw SQL — going through add_game would fire the BGG/rulebook
    backfill hooks (network), which we don't want in a unit test.
    """
    name = "ZZ Test Game"
    with get_conn() as c:
        c.execute("DELETE FROM games WHERE name=?", (name,))
        c.execute(
            "INSERT INTO games(name, status, sleeve_status) VALUES(?, 'owned', 'to_sleeve')",
            (name,),
        )
        c.commit()
    yield name
    with get_conn() as c:
        c.execute("DELETE FROM games WHERE name=?", (name,))
        c.commit()
