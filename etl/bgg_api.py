"""BGG XML API2 client (deterministic, no LLM).

Two endpoints we care about:

1. `xmlapi2/thing?id=ID&stats=1` → full metadata for a known game id
2. `xmlapi2/search?query=Q&type=boardgame,boardgameexpansion` → candidate list

Both require Bearer auth as of 2026-04 (Cloudflare-gated). Set BGG_API_TOKEN
in .env after registering at https://boardgamegeek.com/using_the_xml_api.

The parsing layer is split from the HTTP layer so we can unit-test the parser
on a saved XML fixture without hitting BGG. Responses are cached on disk under
`etl/.bgg_cache/` to keep development cheap (delete the dir to force refresh).
"""
from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE_URL  = "https://boardgamegeek.com/xmlapi2"
# Cache root overridable via BOARDY_CACHE_DIR (read-only code mount in Docker).
_CACHE_ROOT = os.environ.get("BOARDY_CACHE_DIR")
CACHE_DIR = (Path(_CACHE_ROOT) / "bgg" if _CACHE_ROOT
             else Path(__file__).resolve().parent / ".bgg_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# BGG asks for ≤ ~2 req/s. We use 0.6s between calls to be polite.
RATE_DELAY_S = 0.6
_last_call_ts = 0.0


class BGGError(RuntimeError):
    """Raised on any non-200 from BGG (incl. the 401 when token is missing/invalid)."""


def _token() -> str:
    tok = os.environ.get("BGG_API_TOKEN")
    if not tok:
        raise BGGError(
            "BGG_API_TOKEN not set. Register an app at "
            "https://boardgamegeek.com/using_the_xml_api and put the bearer token "
            "in .env as BGG_API_TOKEN=..."
        )
    return tok


def _http_get(path: str, params: dict, *, cache_key: str | None = None,
              use_cache: bool = True) -> bytes:
    """Low-level GET with bearer auth, rate-limit pacing, and on-disk cache."""
    global _last_call_ts

    cache_path = CACHE_DIR / f"{cache_key}.xml" if cache_key else None
    if use_cache and cache_path and cache_path.exists():
        return cache_path.read_bytes()

    # Rate-limit pacing
    elapsed = time.time() - _last_call_ts
    if elapsed < RATE_DELAY_S:
        time.sleep(RATE_DELAY_S - elapsed)

    url = f"{BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/xml, text/xml, */*",
        "User-Agent": "Boardy/1.0 (personal inventory app)",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise BGGError(f"BGG HTTP {e.code} on {url}: {e.read()[:200]!r}") from None
    finally:
        _last_call_ts = time.time()

    # BGG returns 200 + "please wait" XML when the response isn't ready yet.
    # In practice this only happens for collection/plays endpoints, but handle just in case.
    if b"<message>" in body and b"please try again" in body:
        time.sleep(2.0)
        return _http_get(path, params, cache_key=cache_key, use_cache=False)

    if cache_path:
        cache_path.write_bytes(body)
    return body


# -------------------- parsing --------------------

def _label_from_weight(w: float | None) -> str | None:
    """Same mapping used by the old Haiku backfill prompt — kept consistent."""
    if w is None:
        return None
    if w < 2.0:  return "1. Molto Semplice"
    if w < 2.5:  return "2. Semplice"
    if w < 3.5:  return "3. Medio"
    if w < 4.2:  return "4. Complesso"
    return "5. Esperto"


def _attr(el: ET.Element | None, name: str, cast=str):
    if el is None:
        return None
    v = el.get(name)
    if v is None or v == "":
        return None
    try:
        return cast(v)
    except (ValueError, TypeError):
        return None


def _links(item: ET.Element, link_type: str) -> list[str]:
    return [l.get("value") for l in item.findall(f"link[@type='{link_type}']") if l.get("value")]


def parse_thing(xml_bytes: bytes) -> dict | None:
    """Parse a `xmlapi2/thing` XML response into kwargs ready for app.tools.update_game.

    Returns None if no `<item>` is in the XML. Returns a dict with normalized
    fields otherwise. Caller is responsible for matching `name` against the
    local DB row (we keep BGG's primary name in `_bgg_name`).
    """
    root = ET.fromstring(xml_bytes)
    item = root.find("item")
    if item is None:
        return None

    bgg_id = int(item.get("id"))
    bgg_type = item.get("type")  # "boardgame" or "boardgameexpansion"

    primary = item.find("name[@type='primary']")
    bgg_name = primary.get("value") if primary is not None else None

    year = _attr(item.find("yearpublished"), "value", int)
    pmin = _attr(item.find("minplayers"),    "value", int)
    pmax = _attr(item.find("maxplayers"),    "value", int)
    dur  = _attr(item.find("playingtime"),   "value", int)
    dmin = _attr(item.find("minplaytime"),   "value", int)
    dmax = _attr(item.find("maxplaytime"),   "value", int)
    age  = _attr(item.find("minage"),        "value", int)

    # Stats block (only present when ?stats=1)
    avg_w = _attr(item.find(".//averageweight"), "value", float)
    rating = _attr(item.find(".//average"),      "value", float)

    # Best-player-count derived from <poll-summary> (≥ BGG2 introduced poll-summary)
    best = None
    poll_sum = item.find("poll-summary[@name='suggested_numplayers']")
    if poll_sum is not None:
        best_el = poll_sum.find("result[@name='bestwith']")
        if best_el is not None:
            # value looks like "Best with 3–4 players" — strip narrative
            raw = best_el.get("value", "")
            # heuristic: extract the numeric span between "with" and "players"
            import re
            m = re.search(r"(\d+(?:[–—\-]\d+)?(?:\+)?)", raw)
            if m:
                best = m.group(1).replace("–", "-").replace("—", "-")

    desc_el = item.find("description")
    description = desc_el.text if desc_el is not None and desc_el.text else None
    if description:
        # BGG descriptions are HTML-entity-encoded plain text. ET already decodes &amp; etc.
        # Trim and cap to ~600 chars to keep the DB sane (full text isn't needed).
        description = description.strip().replace("&#10;", "\n")
        if len(description) > 800:
            description = description[:800].rsplit(" ", 1)[0] + " […]"

    thumbnail = (item.findtext("thumbnail") or "").strip() or None
    image     = (item.findtext("image")     or "").strip() or None

    return {
        "_bgg_id_returned": bgg_id,
        "_bgg_name": bgg_name,
        "_bgg_type": bgg_type,
        "bgg_id": bgg_id,
        "year_published": year,
        "players_min": pmin,
        "players_max": pmax,
        "players_best": best,
        "duration_min": dur,
        "duration_min_min": dmin,
        "duration_max_min": dmax,
        "age_min": age,
        "complexity_weight": avg_w,
        "complexity_label": _label_from_weight(avg_w),
        "bgg_rating": round(rating, 2) if rating is not None else None,
        "description": description,
        "thumbnail_url": thumbnail,
        "image_url": image,
        "designers":  _links(item, "boardgamedesigner"),
        "publishers": _links(item, "boardgamepublisher")[:3],  # cap at 3 (matches old prompt)
        "categories": _links(item, "boardgamecategory"),
        "mechanics":  _links(item, "boardgamemechanic"),
    }


def parse_search(xml_bytes: bytes) -> list[dict]:
    """Parse a `xmlapi2/search` XML response into a list of candidates."""
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.findall("item"):
        nm = it.find("name")
        yr = it.find("yearpublished")
        out.append({
            "id":   int(it.get("id")),
            "name": nm.get("value") if nm is not None else None,
            "year": int(yr.get("value")) if yr is not None and yr.get("value") else None,
            "type": it.get("type"),  # boardgame | boardgameexpansion | boardgameaccessory
        })
    # Sort by year desc (most recent first), unknown years last
    out.sort(key=lambda c: (c["year"] is None, -(c["year"] or 0)))
    return out


# -------------------- HTTP wrappers --------------------

def fetch_thing(bgg_id: int, *, use_cache: bool = True) -> dict | None:
    """Fetch + parse a single BGG item with stats."""
    body = _http_get("thing", {"id": bgg_id, "stats": 1},
                     cache_key=f"thing_{bgg_id}", use_cache=use_cache)
    return parse_thing(body)


def search(query: str, *, types: tuple[str, ...] = ("boardgame", "boardgameexpansion"),
           use_cache: bool = True) -> list[dict]:
    """Search BGG for candidates. Combines requested types into one query."""
    safe_key = "".join(c if c.isalnum() else "_" for c in query)[:60]
    body = _http_get("search", {"query": query, "type": ",".join(types)},
                     cache_key=f"search_{safe_key}", use_cache=use_cache)
    return parse_search(body)


# -------------------- CLI smoke test --------------------

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    import argparse
    p = argparse.ArgumentParser(description="BGG XML API2 smoke test")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_thing = sub.add_parser("thing", help="Fetch a game by BGG id")
    p_thing.add_argument("id", type=int)
    p_search = sub.add_parser("search", help="Search BGG by name")
    p_search.add_argument("query")
    args = p.parse_args()

    try:
        if args.cmd == "thing":
            data = fetch_thing(args.id)
            if data is None:
                print("No item.")
            else:
                for k, v in data.items():
                    if isinstance(v, list):
                        print(f"  {k:18s} [{len(v)}] {v[:5]}{'...' if len(v) > 5 else ''}")
                    else:
                        s = str(v)
                        print(f"  {k:18s} {s[:90]}{'…' if len(s) > 90 else ''}")
        elif args.cmd == "search":
            for c in search(args.query)[:15]:
                print(f"  id={c['id']:>7d}  {c['type']:22s}  {c['year']!s:>4s}  {c['name']}")
    except BGGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
