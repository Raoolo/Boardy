"""Massive backfill of BGG metadata for the 56 games imported from Excel.

For each game without a `bgg_id`, run a fresh Haiku-powered conversation:
1. Web-search BoardGameGeek for the title
2. Auto-apply the structured metadata via `update_game`

Idempotent: skips games that already have `bgg_id`. Re-running after a partial
run costs $0 for the already-done games.

Usage:
    uv run python etl/backfill_bgg.py            # interactive: confirm each game
    uv run python etl/backfill_bgg.py --auto     # batch mode: no prompts
    uv run python etl/backfill_bgg.py --only "Wingspan"   # one specific game
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# Force UTF-8 output on Windows consoles (default cp1252 chokes on Unicode).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make `app` importable when running from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.chat import WEB_SEARCH_TOOL  # reuse trusted-domain allowlist
from app.db import get_conn
from app.tools import TOOL_FUNCS, TOOLS

load_dotenv(ROOT / ".env")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_ROUNDS = 8

SYSTEM = """You are running an AUTOMATED metadata backfill. No user is reading.

For the named game:
1. Use `web_search` to find the BoardGameGeek entry. Search exactly: "<game name> boardgame BGG".
2. From the BGG result, extract: bgg_id, year_published, designers (list), publishers (list of primary publishers, max 3), players_min, players_max, players_best (e.g. "3-4"), duration_min (representative), duration_min_min, duration_max_min, age_min, complexity_weight (BGG average weight 1-5), bgg_rating, description (1–2 sentences max, English or Italian), thumbnail_url, categories (list), mechanics (list).
3. Map complexity_weight → complexity_label: <2.0 "1. Molto Semplice", 2.0–2.4 "2. Semplice", 2.5–3.4 "3. Medio", 3.5–4.1 "4. Complesso", ≥4.2 "5. Esperto".
4. Call `update_game` with EVERY field you found. Pass arrays for designers/publishers/categories/mechanics.
5. After `update_game` returns ok, REPLY with one short sentence "Done." and STOP.

If the BGG match is ambiguous (e.g. game has many editions/expansions and you can't tell which one the user means), call `update_game` with only `name` and `notes="backfill: ambiguous BGG match — manual review"` and stop. Do NOT guess.

NEVER call `add_game` (the game already exists). NEVER ask for confirmation.
"""


def backfill_one(client: Anthropic, name: str) -> dict:
    """Run one Haiku conversation to enrich one game. Returns result dict."""
    history = [{"role": "user", "content": f"Enrich game: '{name}'"}]
    used_tokens_in = 0
    used_tokens_out = 0

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[WEB_SEARCH_TOOL, *TOOLS],
            messages=history,
        )
        used_tokens_in  += resp.usage.input_tokens
        used_tokens_out += resp.usage.output_tokens

        history.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

        if resp.stop_reason != "tool_use":
            return {"ok": True, "name": name, "tok_in": used_tokens_in, "tok_out": used_tokens_out}

        tool_results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            fn = TOOL_FUNCS.get(b.name)
            if fn is None:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": b.id,
                    "content": json.dumps({"error": f"unknown tool {b.name}"}),
                })
                continue
            try:
                result = fn(**(b.input or {}))
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            tool_results.append({
                "type": "tool_result", "tool_use_id": b.id,
                "content": json.dumps(result, default=str, ensure_ascii=False),
            })
        history.append({"role": "user", "content": tool_results})

    return {"ok": False, "name": name, "error": "tool round limit"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Skip per-game confirmation prompts.")
    parser.add_argument("--only", help="Backfill a single game by name (substring match).")
    parser.add_argument("--reset", action="store_true",
                        help="Re-run on games that already have a bgg_id (otherwise skipped).")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr); sys.exit(1)

    client = Anthropic()

    # Pick targets
    with get_conn() as conn:
        if args.only:
            rows = conn.execute(
                "SELECT id, name, bgg_id FROM games WHERE LOWER(name) LIKE ? ORDER BY name",
                (f"%{args.only.lower()}%",),
            ).fetchall()
        elif args.reset:
            rows = conn.execute("SELECT id, name, bgg_id FROM games ORDER BY name").fetchall()
        else:
            rows = conn.execute("SELECT id, name, bgg_id FROM games WHERE bgg_id IS NULL ORDER BY name").fetchall()

    if not rows:
        print("Nothing to backfill.")
        return

    print(f"Backfilling {len(rows)} games with {MODEL}")
    print(f"Estimated cost: ~${len(rows) * 0.018:.2f}–${len(rows) * 0.030:.2f} (web_search + Haiku tokens)\n")

    if not args.auto and not args.only:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            return

    total_in = total_out = 0
    ok = fail = 0
    for i, r in enumerate(rows, start=1):
        prefix = f"[{i}/{len(rows)}]"
        if r["bgg_id"] and not args.reset:
            print(f"{prefix} {r['name']!r} — already has bgg_id={r['bgg_id']}, skip.")
            continue

        if not args.auto:
            ans = input(f"{prefix} Backfill {r['name']!r}? [Y/n/q] ").strip().lower()
            if ans == "q":
                break
            if ans == "n":
                continue

        print(f"{prefix} → {r['name']!r}", end=" ", flush=True)
        t0 = time.time()
        result = backfill_one(client, r["name"])
        dt = time.time() - t0
        if result.get("ok"):
            ok += 1
            total_in += result["tok_in"]; total_out += result["tok_out"]
            print(f"✓  ({dt:.1f}s, in={result['tok_in']} out={result['tok_out']})")
        else:
            fail += 1
            print(f"✗ {result.get('error')}")
        time.sleep(0.5)  # gentle pacing on web_search

    print(f"\nDone. ok={ok} fail={fail}  total_tokens in={total_in} out={total_out}")
    # Rough Haiku 4.5 pricing: $1/Mtok in, $5/Mtok out. Web search billed separately.
    cost = (total_in / 1e6) * 1.0 + (total_out / 1e6) * 5.0
    print(f"Approx Claude token cost: ${cost:.3f} (web_search billed separately on console)")


if __name__ == "__main__":
    main()
