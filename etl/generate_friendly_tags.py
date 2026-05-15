"""Batch backfill of `games.friendly_tags` via DeepSeek.

Stesso pattern di `etl/embed_descriptions.py`: scorre la tabella `games`,
per ogni riga senza tag chiama l'LLM, persiste se valido, salta altrimenti.
Idempotente: senza `--force` non tocca righe gia' taggate.

Usage:
    uv run python etl/generate_friendly_tags.py                  # backfill solo i mancanti
    uv run python etl/generate_friendly_tags.py --force          # rigenera tutti
    uv run python etl/generate_friendly_tags.py --only Catan     # un singolo gioco
    uv run python etl/generate_friendly_tags.py --dry-run        # nessuna scrittura

Costo stimato: ~$0.0002/gioco con deepseek-chat → ~1 cent per il catalogo
completo (~50-100 giochi). Niente quota concerns.

Windows note: cp1252 stdout chokes su emoji; riconfiguriamo a UTF-8 in testa.
"""
from __future__ import annotations

import argparse
import os
import sys

# Force UTF-8 on Windows console (see docs/LEARNINGS.md).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Make `app` importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

# Run schema migration so v8 column exists even on a fresh DB.
from app import schema  # noqa: E402
schema.migrate()

from app.db import get_conn  # noqa: E402
from app.friendly_tags import generate_for_game, persist  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch-generate friendly_tags for games.")
    ap.add_argument("--force", action="store_true",
                    help="Rigenera anche dove friendly_tags e' gia' presente.")
    ap.add_argument("--only", metavar="NAME",
                    help="Match LIKE %%NAME%% sul nome del gioco (case-insensitive).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Stampa cosa farebbe ma non scrive nel DB.")
    args = ap.parse_args()

    with get_conn() as c:
        params: list = []
        clauses: list[str] = []
        if args.only:
            clauses.append("name LIKE ? COLLATE NOCASE")
            params.append(f"%{args.only}%")
        if not args.force:
            clauses.append("friendly_tags IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = c.execute(
            f"SELECT id, name FROM games{where} ORDER BY name", params
        ).fetchall()

    if not rows:
        print("Niente da fare (tutti i giochi hanno gia' tag, o --only non ha match).")
        return 0

    print(f"Da processare: {len(rows)} giochi" + (" [DRY-RUN]" if args.dry_run else ""))
    ok = skipped = 0
    for r in rows:
        gid = r["id"]
        name = r["name"]
        tags = generate_for_game(gid)
        if tags is None:
            print(f"  ✗ {name}: LLM failure (no key? json invalid? no valid tags?)")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  ◦ {name}: {tags}")
        else:
            persist(gid, tags)
            print(f"  ✓ {name}: {tags}")
        ok += 1

    print(f"\nDone. {ok} ok, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
