"""Backfill BGG metadata using the official XML API (v2).

Three phases — see CLAUDE.md / LEARNINGS.md for context:

  Phase 1 (fill):    for every game with a known bgg_id, GET thing?id=X&stats=1
                     and patch missing fields. €0, deterministic, idempotent.

  Phase 2 (search):  for every game without bgg_id, search BGG and either
                     auto-apply (single match) or print candidates for the user
                     to pick (`apply --gid N --bgg X`).

  Phase 3 (manual):  whatever Phase 2 couldn't disambiguate (e.g. homebrews)
                     stays manual. We just list them.

Usage:
    uv run python etl/backfill_v2.py phase1                # fill all known-id games
    uv run python etl/backfill_v2.py phase1 --only Wingspan
    uv run python etl/backfill_v2.py phase1 --dry-run      # don't write to DB
    uv run python etl/backfill_v2.py phase2                # interactive disambiguation
    uv run python etl/backfill_v2.py phase2 --auto         # auto-apply only single-result hits
    uv run python etl/backfill_v2.py apply --gid 27 --bgg 699   # manual pick

Requires BGG_API_TOKEN in .env (see etl/bgg_api.py for registration link).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.db import get_conn
from app.tools import update_game
from etl.bgg_api import BGGError, fetch_thing, search


# Fields that are "complete" — used to compute what's missing per game.
FILL_FIELDS = ("year_published", "complexity_weight", "bgg_rating",
               "duration_min", "age_min", "thumbnail_url")


def _missing_fields(row) -> list[str]:
    miss = [f for f in FILL_FIELDS if row[f] is None]
    # Bridges: query separately
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM game_categories WHERE game_id=? LIMIT 1", (row["id"],)).fetchone():
            miss.append("categories")
        if not c.execute("SELECT 1 FROM game_mechanics  WHERE game_id=? LIMIT 1", (row["id"],)).fetchone():
            miss.append("mechanics")
    return miss


def _strip_internal(d: dict) -> dict:
    """Drop the _bgg_* helper keys so the dict is safe to **kw into update_game."""
    return {k: v for k, v in d.items() if not k.startswith("_") and v is not None}


def phase1(only: str | None, dry_run: bool) -> None:
    """For every game with a bgg_id, fetch fresh metadata and patch missing fields."""
    with get_conn() as c:
        sql = "SELECT * FROM games WHERE bgg_id IS NOT NULL"
        params: tuple = ()
        if only:
            sql += " AND LOWER(name) LIKE ?"
            params = (f"%{only.lower()}%",)
        rows = c.execute(sql + " ORDER BY name", params).fetchall()

    if not rows:
        print("Phase 1: nothing to do (no games with bgg_id match the filter).")
        return

    print(f"Phase 1: {len(rows)} candidates.\n")
    ok = noop = fail = 0
    for i, row in enumerate(rows, 1):
        miss = _missing_fields(row)
        prefix = f"[{i}/{len(rows)}] {row['name'][:42]:42s} bgg={row['bgg_id']}"
        if not miss:
            print(f"{prefix}  ✓ already complete")
            noop += 1
            continue

        try:
            data = fetch_thing(row["bgg_id"])
        except BGGError as e:
            print(f"{prefix}  ✗ {e}")
            fail += 1
            continue

        if data is None:
            print(f"{prefix}  ✗ BGG returned no <item>")
            fail += 1
            continue

        kw = _strip_internal(data)
        # Don't overwrite scalars that are already set (preserve user edits).
        # Lists (designers/publishers/categories/mechanics) are REPLACED — that's
        # update_game's bridge semantics; for backfill that's what we want.
        with get_conn() as c2:
            cur = c2.execute("SELECT * FROM games WHERE id=?", (row["id"],)).fetchone()
        for k in list(kw.keys()):
            if k in {"designers", "publishers", "categories", "mechanics"}:
                continue
            if cur[k] is not None and k != "bgg_id":
                kw.pop(k)

        if not kw:
            print(f"{prefix}  ✓ nothing to patch (DB has manual values)")
            noop += 1
            continue

        if dry_run:
            print(f"{prefix}  → would patch: {sorted(kw.keys())}")
            ok += 1
            continue

        result = update_game(name=row["name"], **kw, _source="backfill_v2")
        if "error" in result:
            print(f"{prefix}  ✗ update_game: {result['error']}")
            fail += 1
        else:
            patched = result.get("updated_scalar", []) + [k for k in kw if k.endswith("s")]
            print(f"{prefix}  ✓ patched: {sorted(set(patched))}")
            ok += 1

    print(f"\nPhase 1 done. patched={ok}  already-ok={noop}  failed={fail}")


def phase2(auto: bool) -> None:
    """For every game without bgg_id, search BGG and print/apply candidates."""
    with get_conn() as c:
        rows = c.execute("SELECT id, name FROM games WHERE bgg_id IS NULL ORDER BY name").fetchall()

    if not rows:
        print("Phase 2: nothing to do.")
        return

    print(f"Phase 2: {len(rows)} games without bgg_id.\n")
    auto_count = manual_count = empty = 0
    for i, row in enumerate(rows, 1):
        prefix = f"[{i}/{len(rows)}] {row['name']!r}"
        try:
            cands = search(row["name"])
        except BGGError as e:
            print(f"{prefix}  ✗ search failed: {e}")
            continue

        if not cands:
            print(f"{prefix}  — no BGG hits")
            empty += 1
            continue

        if len(cands) == 1 or (auto and cands[0]["type"] == "boardgame"):
            chosen = cands[0]
            if auto:
                print(f"{prefix}  → auto-applying id={chosen['id']} ({chosen['name']}, {chosen['year']})")
                _apply_one(row["id"], row["name"], chosen["id"])
                auto_count += 1
                continue

        print(f"{prefix}  ? {len(cands)} candidates:")
        for c in cands[:8]:
            print(f"      id={c['id']:>7d}  year={c['year']!s:>4s}  type={c['type']:22s}  {c['name']}")
        print(f"    → run:  uv run python etl/backfill_v2.py apply --gid {row['id']} --bgg <id>")
        manual_count += 1

    print(f"\nPhase 2 done. auto-applied={auto_count}  needs-manual-pick={manual_count}  no-hits={empty}")


def _apply_one(game_id: int, game_name: str, bgg_id: int) -> None:
    """Helper used by both `phase2 --auto` and the `apply` subcommand."""
    try:
        data = fetch_thing(bgg_id)
    except BGGError as e:
        print(f"  ✗ fetch failed: {e}")
        return
    if data is None:
        print(f"  ✗ BGG returned no <item> for id={bgg_id}")
        return

    kw = _strip_internal(data)
    result = update_game(name=game_name, **kw, _source="backfill_v2")
    if "error" in result:
        print(f"  ✗ update_game: {result['error']}")
    else:
        print(f"  ✓ applied bgg_id={bgg_id} → {data['_bgg_name']}")


def apply_cli(gid: int, bgg: int) -> None:
    with get_conn() as c:
        row = c.execute("SELECT id, name FROM games WHERE id=?", (gid,)).fetchone()
    if not row:
        print(f"No game with id={gid}", file=sys.stderr)
        sys.exit(1)
    print(f"Applying bgg_id={bgg} to game [{gid}] {row['name']!r}")
    _apply_one(row["id"], row["name"], bgg)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("phase1", help="Fill missing fields for games that already have a bgg_id")
    p1.add_argument("--only", help="Substring match on game name")
    p1.add_argument("--dry-run", action="store_true")

    p2 = sub.add_parser("phase2", help="Search BGG for games without bgg_id and disambiguate")
    p2.add_argument("--auto", action="store_true",
                    help="Auto-apply when first hit is a boardgame (skip multi-candidate prompts)")

    pa = sub.add_parser("apply", help="Manually attach a BGG id to a game (used after phase2 prompt)")
    pa.add_argument("--gid", type=int, required=True, help="games.id")
    pa.add_argument("--bgg", type=int, required=True, help="BGG thing id")

    args = p.parse_args()

    if args.cmd == "phase1":
        phase1(args.only, args.dry_run)
    elif args.cmd == "phase2":
        phase2(args.auto)
    elif args.cmd == "apply":
        apply_cli(args.gid, args.bgg)


if __name__ == "__main__":
    main()
