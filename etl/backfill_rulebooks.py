"""Backfill rulebooks for owned games from 1jour-1jeu.com and/or BGG Files.

For each owned game without an indexed rulebook, find the best rulebook candidate
and (with --apply) download + ingest it into the DB (rulebooks.pdf_blob).

Two sources (`--source`):
  1j1ju  — resolve the ENGLISH name (via BGG when a bgg_id is present — 1j1ju
           titles are English while our DB often holds Italian names), search
           1j1ju, score by title match. Candidate carries a direct `url`.
  bgg    — BGG Files for the game's bgg_id (the file list is open JSON). Files
           are game-specific by construction, so any rulebook there is the right
           game; score ranks rulebook-ness + language. Candidate carries a
           `filepageid`, downloaded via a headless browser + BGG login (one shared
           session for the whole batch).
  both   — (default) take the better of the two per game; ties go to 1j1ju
           (no browser needed).

Match levels: strong (exact 1j1ju title / strong+EN BGG rulebook) → safe to
auto-ingest; likely → usually right, glance worthwhile; weak/none → review.

Usage:
  uv run python etl/backfill_rulebooks.py                          # dry-run, both
  uv run python etl/backfill_rulebooks.py --source bgg             # dry-run, BGG only
  uv run python etl/backfill_rulebooks.py --apply --level likely   # ingest strong+likely
  uv run python etl/backfill_rulebooks.py --apply --only Azul

Needs BGG_USERNAME / BGG_PASSWORD in .env for the bgg source (download is
login-gated). Run `uv run playwright install chromium` once.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # Windows console = cp1252

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.db import get_conn                       # noqa: E402
from app import rulebooks                          # noqa: E402
from app.tools import _rulebook_core, _fetch_pdf_bytes  # noqa: E402
from etl import bgg_api, bgg_files_api, onejour_api  # noqa: E402

LEVEL_ORDER = {"strong": 3, "likely": 2, "weak": 1, "none": 0}


def _bgg_level(score: float) -> str:
    # BGG files are game-specific by construction, so even 'likely' is the right
    # game — the score only reflects rulebook-ness + language.
    return "strong" if score >= 2.0 else "likely" if score >= 1.0 else "weak"


def english_name(bgg_id: int | None) -> str | None:
    """The game's primary English name from BGG, or None."""
    if not bgg_id:
        return None
    try:
        thing = bgg_api.fetch_thing(bgg_id)
        return (thing or {}).get("_bgg_name")
    except Exception:
        return None


def classify(target: str, candidate_title: str) -> tuple[str, float]:
    """Score a candidate title against a normalized target name."""
    core = _rulebook_core(candidate_title)
    if not core or not target:
        return ("weak", 0.0)
    if core == target:
        return ("strong", 1.0)
    tt, ct = set(target.split()), set(core.split())
    overlap = len(tt & ct)
    # subset either way → likely (subtitle/edition noise)
    if overlap and (tt <= ct or ct <= tt):
        return ("likely", overlap / max(len(tt), len(ct)))
    if overlap:
        return ("weak", overlap / max(len(tt), len(ct)))
    return ("weak", 0.0)


def best_candidate(game_name: str, bgg_id: int | None) -> dict:
    """Search 1j1ju (English name first, DB name fallback) and pick the best PDF."""
    en = english_name(bgg_id)
    queries = [q for q in (en, game_name) if q]
    # de-dup while preserving order
    seen, qlist = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower()); qlist.append(q)

    best = {"level": "none", "score": 0.0, "title": None, "url": None,
            "lang": None, "filepageid": None, "src": "1j1ju", "en_name": en}
    for q in qlist:
        target = rulebooks._norm_name(q)
        try:
            hits = onejour_api.search_rulebooks(q, limit=8)
        except Exception:
            hits = []
        for h in hits:
            level, score = classify(target, h["title"])
            key = (LEVEL_ORDER[level], h["lang"] == "EN", score)
            cur = (LEVEL_ORDER[best["level"]], best["lang"] == "EN", best["score"])
            if key > cur:
                best = {"level": level, "score": round(score, 2), "title": h["title"],
                        "url": h["url"], "lang": h["lang"], "fileid": None,
                        "src": "1j1ju", "en_name": en}
    return best


def bgg_best(bgg_id: int | None) -> dict:
    """Top BGG-Files rulebook candidate (carries a `filepageid`, no URL)."""
    base = {"level": "none", "score": 0.0, "title": None, "url": None,
            "lang": None, "filepageid": None, "src": "bgg", "en_name": None}
    if not bgg_id:
        return base
    try:
        cands = bgg_files_api.find_rulebooks(int(bgg_id))
    except Exception:
        return base
    if not cands:
        return base
    c = cands[0]
    return {"level": _bgg_level(c["score"]), "score": round(c["score"], 2),
            "title": c["title"] or c["filename"], "url": None, "lang": c["language"] or "?",
            "filepageid": c["filepageid"], "src": "bgg", "en_name": None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually download + ingest")
    ap.add_argument("--level", choices=["strong", "likely", "weak"], default="strong",
                    help="minimum match level to ingest with --apply (default strong)")
    ap.add_argument("--only", help="restrict to one game (substring, case-insensitive)")
    ap.add_argument("--skip", default="", help="CSV of exact game names to NOT ingest "
                    "(false positives from wrong bgg_id, etc.)")
    ap.add_argument("--source", choices=["1j1ju", "bgg", "both"], default="both",
                    help="rulebook source(s) to consider (default both)")
    args = ap.parse_args()
    min_level = LEVEL_ORDER[args.level]
    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}

    def pick(g) -> dict:
        opts = []
        if args.source in ("1j1ju", "both"):
            opts.append(best_candidate(g["name"], g["bgg_id"]))
        if args.source in ("bgg", "both"):
            opts.append(bgg_best(g["bgg_id"]))
        # best level, then score; 1j1ju wins ties (no browser needed)
        opts.sort(key=lambda b: (LEVEL_ORDER[b["level"]], b["score"], b["src"] == "1j1ju"),
                  reverse=True)
        return opts[0]

    with get_conn() as conn:
        sql = """SELECT g.id, g.name, g.bgg_id FROM games g
                 WHERE g.status='owned'
                   AND NOT EXISTS (SELECT 1 FROM rulebooks rb WHERE rb.game_id=g.id)"""
        params: tuple = ()
        if args.only:
            sql += " AND LOWER(g.name) LIKE ?"
            params = (f"%{args.only.lower()}%",)
        games = conn.execute(sql + " ORDER BY g.name", params).fetchall()

    print(f"{len(games)} owned game(s) without a rulebook (source={args.source})"
          f"{' (mode: APPLY ≥' + args.level + ')' if args.apply else ' (dry-run)'}\n")
    print(f"{'GAME':<34} {'SRC':<6} {'LVL':<7} {'SC':<5} {'LANG':<6} CANDIDATE")
    print("-" * 110)

    counts: dict[str, int] = {}
    ingested = 0
    bgg_session = None  # lazily opened (one browser for the whole batch)
    try:
        for g in games:
            b = pick(g)
            counts[b["level"]] = counts.get(b["level"], 0) + 1
            en_note = f"  (EN: {b['en_name']})" if b.get("en_name") and b["en_name"].lower() != g["name"].lower() else ""
            print(f"{g['name'][:33]:<34} {b['src']:<6} {b['level']:<7} {str(b['score']):<5} "
                  f"{str(b['lang'] or '-'):<6} {b['title'] or '—'}{en_note}")

            if not args.apply or LEVEL_ORDER[b["level"]] < min_level:
                continue
            if g["name"].lower() in skip:
                print("    – skipped (excluded)")
                continue

            # obtain PDF bytes from whichever source won
            if b["src"] == "1j1ju" and b["url"]:
                dl = _fetch_pdf_bytes(b["url"])
                if "error" in dl:
                    print(f"    ! download failed: {dl['error']}"); continue
                data, source = dl["data"], b["url"]
            elif b["src"] == "bgg" and b["filepageid"]:
                if bgg_session is None:
                    from etl.bgg_browser import BGGSession
                    bgg_session = BGGSession().open()
                    print(f"    [bgg browser opened, logged_in={bgg_session.logged_in}]")
                try:
                    data = bgg_session.download(b["filepageid"])
                except Exception as e:
                    print(f"    ! bgg download failed: {e}"); continue
                source = f"bgg:filepage/{b['filepageid']}"
            else:
                continue

            res = rulebooks.ingest_bytes(g["name"], data, source=source)
            if res.get("ok"):
                ingested += 1
                print(f"    ✓ ingested ({b['src']}): {res['pages']}p, {res['chunks']} chunks, {res['bytes']//1024} KB")
            else:
                print(f"    ! ingest failed: {res.get('error')}")
    finally:
        if bgg_session is not None:
            bgg_session.close()

    print("\nSummary by level:", ", ".join(f"{k}={v}" for k, v in sorted(
        counts.items(), key=lambda kv: -LEVEL_ORDER[kv[0]])))
    if args.apply:
        print(f"Ingested: {ingested}")
    else:
        print("Dry-run — re-run with --apply (--level likely to include 'likely').")


if __name__ == "__main__":
    main()
