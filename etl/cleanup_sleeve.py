"""Schema cleanup v3: drop `games.sleeve_raw` and collapse 'no' → 'na'.

WHY:
- `sleeve_raw` was an Excel-import artifact storing the messy raw cell text.
  After verification (etl/inspect on 2026-04-29), every value was already
  faithfully represented in `sleeve_status` (status echoes) or in
  `sleeve_requirements` (the structured per-size counts). Keeping it around
  produced redundant info that confused the LLM during chat reasoning.
- `sleeve_status='no'` ("ho deciso di non sleevare") and `'na'` ("non
  applicabile") collapse into one bucket: "doesn't need sleeving."

Both changes are audit-logged so values are recoverable from the `changes`
table if ever needed:
    SELECT row_label, field, old_value FROM changes
    WHERE source LIKE 'cleanup_sleeve_v3%'

Idempotent — safe to re-run. The schema check uses PRAGMA table_info before
attempting the ALTER.

Usage:
    uv run python etl/cleanup_sleeve.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit  # noqa: E402
from app.db import get_conn  # noqa: E402

SOURCE_DROP = "cleanup_sleeve_v3_drop_raw"
SOURCE_COLLAPSE = "cleanup_sleeve_v3_collapse_no_to_na"


def _has_column(conn, table: str, col: str) -> bool:
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def drop_sleeve_raw(conn) -> int:
    """Audit-log + ALTER TABLE DROP COLUMN. Returns # of rows audit-logged."""
    if not _has_column(conn, "games", "sleeve_raw"):
        print("  [skip] sleeve_raw column already absent.")
        return 0

    rows = conn.execute(
        "SELECT id, name, sleeve_raw FROM games WHERE sleeve_raw IS NOT NULL"
    ).fetchall()
    for r in rows:
        audit.log_change(
            conn, table="games", row_id=r["id"], row_label=r["name"],
            action="update", field="sleeve_raw",
            old=r["sleeve_raw"], new=None,
            source=SOURCE_DROP,
        )
    # SQLite >= 3.35 supports DROP COLUMN natively. Python 3.13 ships newer.
    conn.execute("ALTER TABLE games DROP COLUMN sleeve_raw")
    print(f"  [done] dropped sleeve_raw column. Audited {len(rows)} prior values.")
    return len(rows)


def collapse_no_to_na(conn) -> int:
    """UPDATE sleeve_status='no' → 'na'. One audit row per game touched."""
    rows = conn.execute(
        "SELECT id, name, sleeve_status FROM games WHERE sleeve_status='no'"
    ).fetchall()
    for r in rows:
        audit.log_change(
            conn, table="games", row_id=r["id"], row_label=r["name"],
            action="update", field="sleeve_status",
            old="no", new="na",
            source=SOURCE_COLLAPSE,
        )
    if rows:
        conn.execute("UPDATE games SET sleeve_status='na' WHERE sleeve_status='no'")
    print(f"  [done] collapsed {len(rows)} 'no' rows into 'na'.")
    return len(rows)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("Cleanup sleeve schema v3:")
    with get_conn() as conn:
        n_drop = drop_sleeve_raw(conn)
        n_collapse = collapse_no_to_na(conn)
        conn.commit()
    print(f"\nSummary: dropped raw on {n_drop} games, collapsed {n_collapse} 'no'→'na'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
