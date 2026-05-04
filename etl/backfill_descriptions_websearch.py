"""Backfill ONLY the `description` field via broad-web Tavily + DeepSeek.

Complementary to `backfill_descriptions_tavily.py`:
- That script does BGG-only search + DeepSeek extraction of ALL fields
  (bgg_id, weight, etc). Fails on wrong-edition matches, parody/IT editions,
  and JSON-mode bugs in long payloads.
- THIS script searches the broader web (Wikipedia, publishers, BGG) and asks
  for description ONLY. Smaller surface area = fewer ways to fail. Use as
  the second pass for the residual games.

Pipeline per game:
  1. Tavily search "<name> board game" against a broad allowlist (BGG +
     Wikipedia + publishers).
  2. DeepSeek extracts {description: "..."} or {skip: true, reason: "..."}.
  3. update_game(description=...) — auto-embed indexes the new description.

Manual override:
  --only NAME --manual "user-written description here"
  Skips the LLM call entirely and writes the provided text directly. For
  parody/non-BGG games (e.g. "Il Signore dei Tortelli").

Usage:
    uv run python etl/backfill_descriptions_websearch.py
    uv run python etl/backfill_descriptions_websearch.py --only "Catan"
    uv run python etl/backfill_descriptions_websearch.py --only "Tortelli" --manual "Parodia italiana del Signore degli Anelli, ..."
    uv run python etl/backfill_descriptions_websearch.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# Force UTF-8 stdout on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Make `app` importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from app.db import get_conn  # noqa: E402
from app.tools import web_search, update_game  # noqa: E402


# Broader than the default trusted list: add Wikipedia explicitly + drop
# the sleeve-specific domains that just dilute results.
SEARCH_DOMAINS = [
    "boardgamegeek.com",
    "en.wikipedia.org",
    "it.wikipedia.org",
    "asmodee.com",
    "fantasyflightgames.com",
    "stonemaiergames.com",
    "cmon.com",
    "renegadegamestudios.com",
    "feuerland-spiele.de",
    "capstone-games.com",
]

EXTRACT_PROMPT = """Sei un assistente che estrae UNA descrizione concisa di un gioco da tavolo.

Gioco target: {name}

Sotto trovi contenuto raw da pagine web. Estrai un singolo oggetto JSON con
una di queste due forme:

  {{"description": "2-4 frasi che spiegano cosa è e come si gioca, in inglese, max ~80 parole, parafrasato"}}

OPPURE, se il contenuto NON descrive chiaramente lo stesso gioco (titolo
sbagliato, edizione diversa, espansione di un gioco diverso, parodia
non documentata):

  {{"skip": true, "reason": "<motivo breve>"}}

REGOLE:
- description in inglese, 2-4 frasi, ≤80 parole, parafrasata (non copia letterale).
- Apostrofi ASCII (' non ’), virgolette ASCII (" non “”).
- JSON valido: doppi apici, niente virgole finali, niente testo extra prima/dopo.
- Output SOLO l'oggetto JSON, niente markdown, niente prefissi tipo ```json.

CONTENUTO:
{raw}
"""


def deepseek_extract(name: str, raw: str) -> dict:
    """Single DeepSeek call with json_object mode, description-only schema.

    Smaller payload than the all-fields backfill → far fewer JSON-mode bugs.
    Caps raw at 8000 chars to keep the prompt small.
    """
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    )
    raw = raw[:8000]
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(name=name, raw=raw)}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    text = (resp.choices[0].message.content or "{}").strip()
    # Curly→ASCII normalization in case json_object still slips a smart-quote in.
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # One repair pass: collapse newlines that may sit inside an unterminated string.
        repaired = text.replace("\r", "").replace("\n", " ")
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return {"skip": True, "reason": f"json parse error: {e}; raw={text[:200]}"}


def fetch_raw(name: str) -> str:
    """Broad-web Tavily search, concatenate top results' raw_content."""
    res = web_search(
        query=f"{name} board game",
        include_domains=SEARCH_DOMAINS,
        max_results=5,
        search_depth="advanced",
        include_raw_content=True,
        raw_content_chars=3500,
    )
    if "error" in res:
        return ""
    parts = []
    for r in res.get("results", []):
        url = r.get("url", "")
        raw = r.get("raw_content") or r.get("content") or ""
        if raw:
            parts.append(f"## {url}\n{raw}")
    return "\n\n".join(parts)


def _persist_skip_reason(game_id: int, reason: str | None) -> None:
    """Write/clear `description_skip_reason` (meta-field, no audit log)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE games SET description_skip_reason=? WHERE id=?",
            (reason, game_id),
        )
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Substring filter on game name.")
    parser.add_argument("--manual", help="Set description directly (requires --only). "
                                          "Skips Tavily + LLM.")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB.")
    parser.add_argument("--limit", type=int, default=0, help="Max games (0 = all).")
    args = parser.parse_args()

    if args.manual and not args.only:
        print("[ERR] --manual requires --only to target a single game.")
        sys.exit(2)

    with get_conn() as conn:
        # Parens are critical: without them, AND binds tighter than OR and
        # the name filter only applies to the empty-string branch.
        sql = "SELECT id, name FROM games WHERE (description IS NULL OR description='')"
        params: list[Any] = []
        if args.only:
            sql += " AND LOWER(name) LIKE ?"
            params.append(f"%{args.only.lower()}%")
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print("[backfill] nothing to do — no matching games without description.")
        return

    print(f"[backfill] {len(rows)} game(s) to process. dry_run={args.dry_run}")
    ok, skipped, errors = 0, 0, 0
    for i, r in enumerate(rows, 1):
        gid, name = r["id"], r["name"]
        print(f"\n[{i}/{len(rows)}] {name}")
        skip_reason: str | None = None

        # Manual override path: bypass Tavily + LLM entirely.
        if args.manual:
            description = args.manual
            print(f"  [MANUAL] description={description[:120]!r}")
            if args.dry_run:
                continue
            res = update_game(name=name, description=description, _source="backfill_websearch")
            if "error" in res:
                print(f"  [ERR] update_game: {res['error']}")
                errors += 1
                _persist_skip_reason(gid, f"update_game error: {res['error']}")
            else:
                ok += 1
                _persist_skip_reason(gid, None)
            continue

        try:
            raw = fetch_raw(name)
            if not raw:
                skip_reason = "no Tavily results"
                print(f"  [SKIP] {skip_reason}")
                skipped += 1
                continue
            extracted = deepseek_extract(name, raw)
            if extracted.get("skip"):
                skip_reason = extracted.get("reason", "no reason")
                print(f"  [SKIP] {skip_reason}")
                skipped += 1
                continue
            description = (extracted.get("description") or "").strip()
            if not description:
                skip_reason = "no description in extracted payload"
                print(f"  [SKIP] {skip_reason}")
                skipped += 1
                continue
            print(f"  [OK] description={description[:120]!r}")
            if args.dry_run:
                continue
            res = update_game(name=name, description=description, _source="backfill_websearch")
            if "error" in res:
                skip_reason = f"update_game error: {res['error']}"
                print(f"  [ERR] update_game: {res['error']}")
                errors += 1
            else:
                ok += 1
        except Exception as e:
            skip_reason = f"{type(e).__name__}: {e}"
            print(f"  [ERR] {skip_reason}")
            errors += 1
        finally:
            if not args.dry_run:
                _persist_skip_reason(gid, skip_reason)
        time.sleep(0.4)

    print(f"\n[backfill] DONE — ok={ok} skipped={skipped} errors={errors}")


if __name__ == "__main__":
    main()
