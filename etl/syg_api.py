"""sleeveyourgames.com private JSON API client (deterministic, no LLM).

Reverse-engineered from the site's Nuxt bundle (2026-06). The public site is a
JS SPA whose data comes from a CORS-open JSON backend at
`https://api.sleeveyourgames.com` — no Cloudflare wall, no auth. Two endpoints
we care about:

1. `GET /game/autocomplete?query=Q` → `[{"text": <name>, "id": <syg_id>, ...}]`
   name → internal id resolver.
2. `GET /game/{syg_id}` → full game JSON. The bit we need is `cards`
   (`[{card_quantity, height, width}]`) — the per-game card list with mm sizes,
   plus `expansions[].cards` and a `bgg_id` we can cross-check against our own.

Why this beats web_search for sleeves: deterministic, exact counts + mm sizes,
no scraping the JS-rendered sleeveyourgames page (which Tavily can't read for
games not yet indexed). The XML API of BGG does NOT expose card sizes at all,
so this is the only structured source — see docs/LEARNINGS.md 2026-06-04.

CAVEAT: this is a PRIVATE, undocumented API. It can change without notice. The
HTTP + parsing layers are isolated here (mirroring etl/bgg_api.py) so a breakage
is a one-file fix. Callers must handle SYGError / None gracefully (fall back to
web_search, then manual entry).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL  = "https://api.sleeveyourgames.com"
SITE_URL  = "https://www.sleeveyourgames.com"
CACHE_DIR = Path(__file__).resolve().parent / ".syg_cache"
CACHE_DIR.mkdir(exist_ok=True)

# The backend serves a WAF "noindex" challenge to bare clients; mimicking the
# SPA's browser headers (UA + Origin + Referer) is enough to get clean JSON.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": SITE_URL,
    "Referer": SITE_URL + "/",
}

# Be polite — the SPA fires these one at a time on user keystrokes.
RATE_DELAY_S = 0.5
_last_call_ts = 0.0


class SYGError(RuntimeError):
    """Raised on any non-200 / non-JSON from the sleeveyourgames backend."""


def _http_get_json(path: str, params: dict | None = None, *,
                   cache_key: str | None = None, use_cache: bool = True):
    """Low-level GET → parsed JSON, with browser headers, pacing, on-disk cache."""
    global _last_call_ts

    cache_path = CACHE_DIR / f"{cache_key}.json" if cache_key else None
    if use_cache and cache_path and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    elapsed = time.time() - _last_call_ts
    if elapsed < RATE_DELAY_S:
        time.sleep(RATE_DELAY_S - elapsed)

    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{BASE_URL}/{path}{qs}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise SYGError(f"SYG HTTP {e.code} on {url}: {e.read()[:200]!r}") from None
    except urllib.error.URLError as e:
        raise SYGError(f"SYG network error on {url}: {e.reason}") from None
    finally:
        _last_call_ts = time.time()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # WAF challenge or HTML error page — surface as a clean error.
        raise SYGError(f"SYG returned non-JSON for {url} (WAF/error page?).") from None

    if cache_path:
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# -------------------- parsing --------------------

def _cards_to_requirements(cards: list | None, note: str | None = None) -> list[dict]:
    """Map the API's `cards` array to set_sleeve_requirements item shape.

    API item: {"card_quantity": 212, "height": "87", "width": "57"}
    Boardy:   {"count": 212, "width_mm": 57.0, "height_mm": 87.0, "note": ...}
    Skips entries missing a usable size; coerces stringly-typed numbers.
    """
    out = []
    for c in cards or []:
        try:
            w = float(c["width"])
            h = float(c["height"])
            n = int(c.get("card_quantity") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        item = {"count": n, "width_mm": w, "height_mm": h}
        if note:
            item["note"] = note
        out.append(item)
    return out


def parse_game(data: dict) -> dict:
    """Normalize a `/game/{id}` payload into the bits Boardy needs.

    Returns base-game requirements (ready for set_sleeve_requirements) plus a
    breakdown of expansions (each with its own requirements) and a `bgg_id`
    the caller can match against bgg_lookup for confidence.
    """
    base_reqs = _cards_to_requirements(data.get("cards"))
    expansions = []
    for exp in data.get("expansions") or []:
        exp_reqs = _cards_to_requirements(
            exp.get("cards"), note=f"espansione: {exp.get('name')}")
        if exp_reqs:
            expansions.append({
                "name": exp.get("name"),
                "year": exp.get("year"),
                "requirements": exp_reqs,
            })

    bgg_id = data.get("bgg_id")
    try:
        bgg_id = int(bgg_id) if bgg_id not in (None, "") else None
    except (TypeError, ValueError):
        bgg_id = None

    return {
        "syg_id": data.get("id"),
        "name": data.get("name"),
        "year": data.get("year"),
        "publisher": data.get("publisher"),
        "bgg_id": bgg_id,
        "is_verified": data.get("is_verified"),
        "base_requirements": base_reqs,
        "expansions": expansions,
    }


# -------------------- HTTP wrappers --------------------

def autocomplete(query: str, *, use_cache: bool = True) -> list[dict]:
    """Resolve a game name → candidates `[{text, id}]` (most relevant first)."""
    safe = "".join(c if c.isalnum() else "_" for c in query.lower())[:60]
    data = _http_get_json("game/autocomplete", {"query": query},
                          cache_key=f"ac_{safe}", use_cache=use_cache)
    if not isinstance(data, list):
        return []
    return [{"text": d.get("text"), "id": d.get("id")} for d in data if d.get("id")]


def fetch_game(syg_id: int, *, use_cache: bool = True) -> dict | None:
    """Fetch + parse a single game by sleeveyourgames internal id."""
    data = _http_get_json(f"game/{int(syg_id)}",
                          cache_key=f"game_{syg_id}", use_cache=use_cache)
    if not isinstance(data, dict) or "id" not in data:
        return None
    return parse_game(data)


def lookup(name: str, *, bgg_id: int | None = None,
           use_cache: bool = True) -> dict | None:
    """Full name → sleeve requirements flow.

    1. autocomplete(name) → candidates.
    2. pick the best: if `bgg_id` is given, prefer the candidate whose fetched
       payload has the matching bgg_id; else take the first candidate.
    3. fetch_game(id) → parsed requirements.

    Returns None when the game isn't in sleeveyourgames' DB (common for very
    new releases — the caller should fall back to web_search, then manual).
    """
    candidates = autocomplete(name, use_cache=use_cache)
    if not candidates:
        return None

    # Fast path: single candidate, or no bgg_id to disambiguate with.
    if bgg_id is None or len(candidates) == 1:
        return fetch_game(candidates[0]["id"], use_cache=use_cache)

    # Disambiguate by bgg_id across the top few candidates.
    first = None
    for cand in candidates[:5]:
        parsed = fetch_game(cand["id"], use_cache=use_cache)
        if parsed is None:
            continue
        if first is None:
            first = parsed
        if parsed.get("bgg_id") == bgg_id:
            return parsed
    return first  # no bgg match → best-effort first hit


# -------------------- CLI smoke test --------------------

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    import argparse
    p = argparse.ArgumentParser(description="sleeveyourgames API smoke test")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_ac = sub.add_parser("autocomplete", help="name → candidates")
    p_ac.add_argument("query")
    p_g = sub.add_parser("game", help="fetch by syg id")
    p_g.add_argument("id", type=int)
    p_l = sub.add_parser("lookup", help="name → requirements (full flow)")
    p_l.add_argument("name")
    p_l.add_argument("--bgg-id", type=int, default=None)
    args = p.parse_args()

    try:
        if args.cmd == "autocomplete":
            for c in autocomplete(args.query):
                print(f"  id={c['id']:>7}  {c['text']}")
        elif args.cmd == "game":
            print(json.dumps(fetch_game(args.id), ensure_ascii=False, indent=2))
        elif args.cmd == "lookup":
            print(json.dumps(lookup(args.name, bgg_id=args.bgg_id),
                             ensure_ascii=False, indent=2))
    except SYGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
