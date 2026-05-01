"""One-shot fix for game names corrupted during the original Excel import.

Three cases observed in 2026-04-29 audit:
  1. "Here To Slay, Gioco" — adjacent cell glued onto the name.
  2. "Il Singore dei Tortelli -Le Due Torri-" — typo (Singore→Signore).
  3. "Sherlock Holmes Consulente Investigativo:\\nI Delitti..." — literal newline.

This script uses direct SQL UPDATE (not the chat `update_game` tool, which
uses `name` as lookup key and so cannot rename) and writes one audit row per
fix via app.audit so the change is traceable in `changes`.

Idempotent: re-running after a successful fix is a no-op (rows simply don't
match the old name anymore).

Usage:
    uv run python etl/fix_encoding.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `app.*` importable when run as a script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit  # noqa: E402
from app.db import get_conn  # noqa: E402

# (old_name, new_name) — exact-match lookup, case-sensitive.
FIXES: list[tuple[str, str]] = [
    ("Here To Slay, Gioco",
     "Here To Slay"),
    ("Il Singore dei Tortelli -Le Due Torri-",
     "Il Signore dei Tortelli -Le Due Torri-"),
    ("Sherlock Holmes Consulente Investigativo:\nI Delitti del Tamigi e Altri Casi",
     "Sherlock Holmes Consulente Investigativo: I Delitti del Tamigi e Altri Casi"),
]

SOURCE = "manual_encoding_fix_2026-04-29"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console fix
    fixed = 0
    skipped = 0
    with get_conn() as conn:
        for old, new in FIXES:
            row = conn.execute(
                "SELECT id FROM games WHERE name=?", (old,)
            ).fetchone()
            if row is None:
                # Already fixed (or never existed) — idempotent path.
                already = conn.execute(
                    "SELECT 1 FROM games WHERE name=?", (new,)
                ).fetchone()
                tag = "OK (already fixed)" if already else "NOT FOUND"
                print(f"  [skip] {old!r}\n         → {tag}")
                skipped += 1
                continue
            gid = row["id"]
            conn.execute(
                "UPDATE games SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new, gid),
            )
            audit.log_change(
                conn, table="games", row_id=gid, row_label=new,
                action="update", field="name",
                old=old, new=new, source=SOURCE,
            )
            print(f"  [fix]  id={gid}\n         old: {old!r}\n         new: {new!r}")
            fixed += 1
        conn.commit()

    print(f"\nDone. Fixed {fixed}, skipped {skipped}.")
    print("Audit rows logged with source=", SOURCE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
