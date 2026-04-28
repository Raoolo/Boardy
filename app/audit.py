"""Audit log for write operations on the Boardy DB.

Every mutation (insert/update/delete) on `games`, `sleeve_requirements`, and
`sleeve_inventory` writes one or more rows into `changes` so we keep a forever
history of who-changed-what-when. Useful for:

- "When did I add Concordia?" / "When did I tweak my Wingspan sleeve count?"
- Manual undo: read the old_value, re-apply via the relevant tool.
- Distinguishing chat edits from ETL/backfill mass-updates (`source` column).

Helpers run inside the caller's existing connection (sharing the transaction)
so a failed UPDATE rolls back its own audit row too.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

# Fields we never log a diff on — they tick on every UPDATE and would just be noise.
_IGNORED_FIELDS = {"updated_at", "created_at"}


def _to_json(v: Any) -> str | None:
    """Compact JSON for storage. None stays None."""
    if v is None:
        return None
    try:
        return json.dumps(v, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps(str(v), ensure_ascii=False)


def log_change(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_id: int | None,
    row_label: str | None,
    action: str,        # 'insert' | 'update' | 'delete'
    field: str | None,  # column name; None for insert/delete (whole-row events)
    old: Any = None,
    new: Any = None,
    source: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO changes(table_name, row_id, row_label, action, field,
                               old_value, new_value, source)
           VALUES(?,?,?,?,?,?,?,?)""",
        (table, row_id, row_label, action, field, _to_json(old), _to_json(new),
         source or "unknown"),
    )


def log_diff(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_id: int,
    row_label: str | None,
    before: dict,
    after: dict,
    source: str | None = None,
) -> int:
    """Compare two dicts (same keys) and write one `update` row per changed field.

    Returns the number of audit rows written. Both before/after must already be
    DB row dicts (e.g. from `dict(conn.execute(...).fetchone())`).
    """
    n = 0
    for k in after.keys() | before.keys():
        if k in _IGNORED_FIELDS:
            continue
        ov = before.get(k)
        nv = after.get(k)
        if ov == nv:
            continue
        log_change(conn, table=table, row_id=row_id, row_label=row_label,
                   action="update", field=k, old=ov, new=nv, source=source)
        n += 1
    return n


def log_full(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_id: int | None,
    row_label: str | None,
    action: str,        # 'insert' | 'delete'
    snapshot: dict,
    source: str | None = None,
) -> None:
    """Log a whole-row event (insert or delete).

    For insert: `snapshot` goes to `new_value`, `old_value=None`.
    For delete: `snapshot` goes to `old_value`, `new_value=None`.
    """
    if action == "insert":
        old, new = None, snapshot
    elif action == "delete":
        old, new = snapshot, None
    else:
        raise ValueError(f"log_full only accepts insert|delete, got {action!r}")
    # Filter out volatile timestamps from snapshot so the row stays compact.
    snap = {k: v for k, v in snapshot.items() if k not in _IGNORED_FIELDS}
    log_change(conn, table=table, row_id=row_id, row_label=row_label,
               action=action, field=None,
               old=snap if action == "delete" else None,
               new=snap if action == "insert" else None,
               source=source)


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    table: str | None = None,
    row_id: int | None = None,
) -> list[dict]:
    """Read the last N audit rows. Optionally filter by table and/or row_id."""
    sql = "SELECT id, ts, table_name, row_id, row_label, action, field, " \
          "old_value, new_value, source FROM changes"
    where, params = [], []
    if table:
        where.append("table_name=?"); params.append(table)
    if row_id is not None:
        where.append("row_id=?"); params.append(row_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Decode JSON for readability when consumed by the chat tool.
        for k in ("old_value", "new_value"):
            if d[k] is not None:
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        out.append(d)
    return out
