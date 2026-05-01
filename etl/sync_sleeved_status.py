"""One-shot fixer: drop pending sleeve_requirements rows for already-sleeved games.

WHY:
The semantic for `sleeve_requirements` is "pending work" — what still needs to
be bought / sleeved. If a game is already `sleeved` (or `na` = doesn't need
any), it must NOT contribute to `sleeve_summary.to_buy`. The 2026-04-29 audit
found 1807 phantom sleeves inflating the buy list because 11 sleeved games
still had requirement rows from the original Excel parse.

This script removes those leftovers and audit-logs each deletion so they're
recoverable from `changes` if ever needed.

Idempotent: re-running after a successful run is a no-op.

Usage:
    uv run python etl/sync_sleeved_status.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit  # noqa: E402
from app.db import get_conn  # noqa: E402

SOURCE = "sync_sleeved_status_2026-04-29"
# Statuses that should NEVER have pending requirements rows.
DONE_STATUSES = ("sleeved", "na")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    placeholders = ",".join("?" * len(DONE_STATUSES))
    with get_conn() as conn:
        # Group by game so we log one audit row per game (matches the
        # existing pattern from set_sleeve_requirements).
        games = conn.execute(
            f"""
            SELECT g.id, g.name, g.sleeve_status
            FROM games g
            WHERE g.sleeve_status IN ({placeholders})
              AND EXISTS (SELECT 1 FROM sleeve_requirements sr WHERE sr.game_id=g.id)
            ORDER BY g.name
            """,
            DONE_STATUSES,
        ).fetchall()

        total_rows = 0
        for g in games:
            old_rows = [
                dict(r) for r in conn.execute(
                    "SELECT count, width_mm, height_mm, note "
                    "FROM sleeve_requirements WHERE game_id=? "
                    "ORDER BY width_mm, height_mm",
                    (g["id"],),
                ).fetchall()
            ]
            conn.execute("DELETE FROM sleeve_requirements WHERE game_id=?", (g["id"],))
            audit.log_change(
                conn, table="sleeve_requirements", row_id=g["id"], row_label=g["name"],
                action="update", field="requirements",
                old=old_rows, new=[], source=SOURCE,
            )
            n = sum(r["count"] for r in old_rows)
            print(f"  [clean] {g['name'][:45]:<45} status={g['sleeve_status']:>8} "
                  f"removed {len(old_rows)} sizes ({n} sleeves)")
            total_rows += n
        conn.commit()

    print(f"\nDone. Removed {len(games)} games' phantom requirements ({total_rows} sleeves total).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
