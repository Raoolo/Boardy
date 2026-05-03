"""Backfill BGG metadata for games missing a `description`, using Tavily +
DeepSeek (the active chat provider).

Pipeline per game:
  1. Tavily search "<game name> boardgame BGG" against the BGG domain.
  2. DeepSeek extracts a structured JSON object from the raw_content.
     Returns {"skip": true, "reason": "..."} if nothing relevant.
  3. update_game(...) — auto-embed hook indexes the description.

Why DeepSeek and not Sonnet: it's the active chat provider (LLM_PROVIDER=
deepseek in .env), ~10× cheaper than Sonnet, and JSON-mode extraction is
well within its capabilities for this task.

Usage:
    uv run python etl/backfill_descriptions_tavily.py
    uv run python etl/backfill_descriptions_tavily.py --only "Catan"
    uv run python etl/backfill_descriptions_tavily.py --dry-run
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

EXTRACT_PROMPT = """You are extracting BoardGameGeek metadata for ONE specific game.

The user owns a game called: {name}

Below is raw web-search content from BoardGameGeek-related pages. Extract a
single JSON object with the schema described. If the content clearly does
NOT describe the same game (wrong title, wrong edition, expansion of a
different game), return {{"skip": true, "reason": "<why>"}}.

OUTPUT SCHEMA (omit any field you can't confidently fill — DO NOT guess):
{{
  "bgg_id": int,
  "year_published": int,
  "players_min": int,
  "players_max": int,
  "players_best": "string like '3-4'",
  "duration_min": int,
  "duration_min_min": int,
  "duration_max_min": int,
  "age_min": int,
  "complexity_weight": float (BGG average weight, 1.0-5.0),
  "bgg_rating": float,
  "description": "string, 2-4 sentences from the BGG description",
  "thumbnail_url": "string URL",
  "designers": ["string", ...],
  "publishers": ["string", ...],
  "categories": ["string", ...],
  "mechanics": ["string", ...]
}}

CONTENT RULES:
- description must be in English, 2-4 sentences, ≤80 words, paraphrased.
- If the user-owned game is an expansion (e.g. "7 Wonders II Cities"), match
  the expansion specifically — don't return data for the base game.
- If the page is for a different edition or unrelated product, return
  {{"skip": true, ...}} rather than guessing.

STRICT JSON FORMATTING (this is critical — past failures came from this):
- Output VALID JSON only. The whole response must parse with json.loads.
- ALL string values MUST be on a single line (no literal newlines inside a string).
- Inside string values, escape every double quote as \\" and every backslash as \\\\.
- Replace fancy/curly apostrophes (’) and quotes (“ ”) with ASCII (' " ).
- Do NOT use single-quoted strings (JSON requires double quotes).
- Do NOT include trailing commas, comments, or any text before/after the JSON object.
- For game titles containing apostrophes (e.g. Memoir '44), prefer rewording in
  the description to avoid the apostrophe entirely (e.g. "Memoir 44") OR
  ensure the apostrophe is plain ASCII inside a properly-closed string.

RAW CONTENT:
{raw}
"""


def _try_repair_json(text: str) -> dict | None:
    """Best-effort fix for the most common DeepSeek JSON-mode bugs.

    Observed failure modes:
    - Unterminated string in `description` due to a literal newline inside the
      value (e.g. "Memoir '44 - …\\n…").
    - Curly apostrophes (’) sometimes confuse the parser when paired with
      backslash artifacts.

    We try, in order: a curly→ASCII normalization; replacing newlines inside
    string runs; finally giving up. Never raises — returns None on failure.
    """
    candidate = text.replace("’", "'").replace("‘", "'") \
                    .replace("“", '"').replace("”", '"')
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Replace bare newlines that appear inside (likely) string values. Crude
    # but works for the unterminated-string-in-description case.
    repaired = candidate.replace("\r", "").replace("\n", " ")
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def deepseek_extract(name: str, raw: str) -> dict:
    """Call DeepSeek with JSON mode to extract structured fields.

    On a json.loads failure we attempt a small repair pass (curly-quote
    normalization + newline collapse). If that still fails we return
    `{"skip": true, "reason": "json parse error: ..."}` so the caller can
    persist the reason.
    """
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    )
    # Cap raw at 8000 chars to keep prompt manageable.
    raw = raw[:8000]
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(name=name, raw=raw)}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    text = resp.choices[0].message.content or "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        repaired = _try_repair_json(text)
        if repaired is not None:
            return repaired
        return {"skip": True, "reason": f"json parse error: {e}"}


def fetch_bgg_raw(name: str) -> str:
    """Search BGG-only via Tavily, concatenate the top results' raw_content."""
    res = web_search(
        query=f"{name} boardgame BGG",
        include_domains=["boardgamegeek.com"],
        max_results=3,
        search_depth="advanced",
        include_raw_content=True,
        raw_content_chars=4000,
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


def build_update_kwargs(extracted: dict) -> dict:
    """Whitelist + type-coerce the fields we'll pass to update_game."""
    allowed = {
        "bgg_id": int, "year_published": int,
        "players_min": int, "players_max": int, "players_best": str,
        "duration_min": int, "duration_min_min": int, "duration_max_min": int,
        "age_min": int,
        "complexity_weight": float, "bgg_rating": float,
        "description": str, "thumbnail_url": str,
    }
    out: dict[str, Any] = {}
    for k, caster in allowed.items():
        v = extracted.get(k)
        if v in (None, "", []):
            continue
        try:
            out[k] = caster(v)
        except (ValueError, TypeError):
            continue
    for k in ("designers", "publishers", "categories", "mechanics"):
        v = extracted.get(k)
        if isinstance(v, list) and v:
            out[k] = [str(x).strip() for x in v if str(x).strip()]
    return out


def derive_complexity_label(weight: float | None) -> str | None:
    if weight is None:
        return None
    if weight < 2.0:  return "1. Molto Semplice"
    if weight < 2.5:  return "2. Semplice"
    if weight < 3.5:  return "3. Medio"
    if weight < 4.2:  return "4. Complesso"
    return "5. Esperto"


def _persist_skip_reason(game_id: int, reason: str | None) -> None:
    """Write/clear the skip_reason on `games` (no audit log — meta-field)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE games SET description_skip_reason=? WHERE id=?",
            (reason, game_id),
        )
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Substring filter on game name.")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB.")
    parser.add_argument("--limit", type=int, default=0, help="Max games to process (0 = all).")
    parser.add_argument("--retry-skipped", action="store_true",
                        help="Process games whose description is missing AND a previous "
                             "skip_reason is recorded — useful to retry after prompt tweaks.")
    args = parser.parse_args()

    with get_conn() as conn:
        sql = "SELECT id, name FROM games WHERE description IS NULL OR description=''"
        params: list[Any] = []
        if args.only:
            sql += " AND LOWER(name) LIKE ?"
            params.append(f"%{args.only.lower()}%")
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    print(f"[backfill] {len(rows)} game(s) to process. dry_run={args.dry_run}")
    ok, skipped, errors = 0, 0, 0
    for i, r in enumerate(rows, 1):
        gid, name = r["id"], r["name"]
        print(f"\n[{i}/{len(rows)}] {name}")
        skip_reason: str | None = None
        try:
            raw = fetch_bgg_raw(name)
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
            kwargs = build_update_kwargs(extracted)
            if not kwargs.get("description"):
                skip_reason = "no description in extracted payload"
                print(f"  [SKIP] {skip_reason}")
                skipped += 1
                continue
            # Add derived complexity_label if we got a weight.
            if "complexity_weight" in kwargs:
                lbl = derive_complexity_label(kwargs["complexity_weight"])
                if lbl:
                    kwargs["complexity_label"] = lbl
            preview_keys = sorted(kwargs.keys())
            print(f"  [OK] fields={preview_keys}")
            print(f"       description={kwargs['description'][:120]!r}")
            if args.dry_run:
                continue
            res = update_game(name=name, _source="backfill_tavily", **kwargs)
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
            # Persist the outcome on the row so we can revisit later.
            # On success: clear any prior reason. On skip/error: write it.
            if not args.dry_run:
                _persist_skip_reason(gid, skip_reason)
        # Be nice to APIs.
        time.sleep(0.5)

    print(f"\n[backfill] DONE — ok={ok} skipped={skipped} errors={errors}")


if __name__ == "__main__":
    main()
