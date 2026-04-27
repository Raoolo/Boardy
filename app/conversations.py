"""Server-side conversation persistence.

Single SQLite table; auto-created on import. History is stored as JSON blob
(simple, since 99% of access is whole-conversation read/write).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import get_conn

MIGRATION = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT,
  history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate() -> None:
    with get_conn() as c:
        c.executescript(MIGRATION)
        c.commit()


def _title_from_history(history: list[dict]) -> str | None:
    for turn in history:
        if turn.get("role") == "user" and isinstance(turn.get("content"), str):
            text = turn["content"].strip().replace("\n", " ")
            return (text[:60] + "…") if len(text) > 60 else text
    return None


def list_conversations() -> list[dict]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT id, title, created_at, updated_at, history_json FROM conversations WHERE id=?",
            (conv_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["history"] = json.loads(d.pop("history_json") or "[]")
    return d


def create_conversation() -> int:
    now = _now()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO conversations(created_at, updated_at, history_json) VALUES(?, ?, '[]')",
            (now, now),
        )
        c.commit()
    return cur.lastrowid


def save_conversation(conv_id: int, history: list[dict]) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE conversations SET history_json=?, updated_at=?, title=COALESCE(NULLIF(title,''), ?) WHERE id=?",
            (json.dumps(history, ensure_ascii=False), _now(), _title_from_history(history), conv_id),
        )
        c.commit()


def delete_conversation(conv_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        c.commit()
