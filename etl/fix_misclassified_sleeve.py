"""Restore 5 games misclassified as 'sleeved' by the original Excel import.

The pre-fix `classify_sleeve()` defaulted to status='sleeved' whenever the
Excel cell had parseable numeric data without "DA COMPRARE" — this was wrong
because the cell just listed *card sizes*, not sleeving status. The 5 games
below were confirmed by the user as NOT actually sleeved.

What we do here:
  1. Pull each game's original sleeve_requirements from the audit log
     (logged on 2026-04-29 by sync_sleeved_status.py before the cascade-delete).
  2. Set sleeve_status='to_sleeve'.
  3. Re-insert the requirements rows.
  4. Audit-log both changes so the recovery is traceable.

Idempotent: re-running checks for status==sleeved before doing anything.

Usage:
    uv run python etl/fix_misclassified_sleeve.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit  # noqa: E402
from app.db import get_conn  # noqa: E402

# Confirmed by user 2026-04-29: these were NOT actually sleeved.
MISCLASSIFIED = [
    "Gloomhaven - Jaws of the Lion",
    "Room-25 - Ultimate",
    "HeroQuest",
    "Memoir '44 - Refresh",
    "Obscurio",
]
RESTORE_SOURCE = "fix_misclassified_sleeve_2026-04-29"
SYNC_SOURCE = "sync_sleeved_status_2026-04-29"


def _last_deleted_requirements(conn, name: str) -> list[dict] | None:
    """Recover the requirements that were nuked by the sync script."""
    row = conn.execute(
        """SELECT old_value FROM changes
           WHERE source=? AND field='requirements' AND row_label=?
           ORDER BY id DESC LIMIT 1""",
        (SYNC_SOURCE, name),
    ).fetchone()
    if not row or not row["old_value"]:
        return None
    try:
        return json.loads(row["old_value"])
    except (json.JSONDecodeError, TypeError):
        return None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    fixed = skipped = 0
    with get_conn() as conn:
        for name in MISCLASSIFIED:
            row = conn.execute(
                "SELECT id, sleeve_status FROM games WHERE name=?", (name,)
            ).fetchone()
            if not row:
                print(f"  [skip] {name}: not in DB")
                skipped += 1
                continue
            if row["sleeve_status"] != "sleeved":
                print(f"  [skip] {name}: status already {row['sleeve_status']!r} (not sleeved)")
                skipped += 1
                continue
            gid = row["id"]

            reqs = _last_deleted_requirements(conn, name)
            if not reqs:
                print(f"  [warn] {name}: no requirements found in audit log; status flipped only")

            # 1. Flip status
            conn.execute(
                "UPDATE games SET sleeve_status='to_sleeve', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?", (gid,),
            )
            audit.log_change(
                conn, table="games", row_id=gid, row_label=name,
                action="update", field="sleeve_status",
                old="sleeved", new="to_sleeve", source=RESTORE_SOURCE,
            )

            # 2. Re-insert requirements (if any to restore)
            if reqs:
                for r in reqs:
                    conn.execute(
                        """INSERT INTO sleeve_requirements
                                  (game_id, count, width_mm, height_mm, note)
                           VALUES(?,?,?,?,?)""",
                        (gid, int(r["count"]), float(r["width_mm"]),
                         float(r["height_mm"]), r.get("note")),
                    )
                audit.log_change(
                    conn, table="sleeve_requirements", row_id=gid, row_label=name,
                    action="update", field="requirements",
                    old=[], new=reqs, source=RESTORE_SOURCE,
                )
                summary = ", ".join(f"{r['count']}×{r['width_mm']}×{r['height_mm']}" for r in reqs)
                print(f"  [fix] {name[:40]:<40}: status→to_sleeve, restored [{summary}]")
            else:
                print(f"  [fix] {name[:40]:<40}: status→to_sleeve (no reqs in log)")
            fixed += 1
        conn.commit()

    print(f"\nDone. Fixed {fixed}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
