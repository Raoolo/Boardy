"""One-shot: build / refresh `games.description_embedding` for every owned game.

Re-run anytime — `games_semantic.reindex_all` skips rows whose
`description_hash` already matches the SHA1 of the current text. Pass
`--force` to re-encode everything (e.g. after swapping the embedding model).

Usage:
    uv run python etl/embed_descriptions.py
    uv run python etl/embed_descriptions.py --force

Windows note: cp1252 stdout chokes on the e5 progress bar's box-drawing
characters; we reconfigure to UTF-8 early. See LEARNINGS.md.
"""
from __future__ import annotations

import argparse
import sys
import time

# Force UTF-8 stdout on Windows so progress / status lines don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Make `app` importable when running this file directly.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import games_semantic  # noqa: E402
from app.schema import migrate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-embed every game even if hash matches.")
    parser.add_argument("--batch", type=int, default=32,
                        help="Encode batch size (default 32).")
    args = parser.parse_args()

    # Guarantee the schema columns exist before we try to write to them.
    migrate()

    print(f"[embed] starting force={args.force} batch={args.batch}")
    t0 = time.time()
    res = games_semantic.reindex_all(force=args.force, batch_size=args.batch)
    dt = time.time() - t0
    print(f"[embed] {res} elapsed={dt:.1f}s")


if __name__ == "__main__":
    main()
