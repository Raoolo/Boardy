"""BoardGameGeek card-sizes discovery (deterministic, no browser, no auth).

SECONDA fonte per le misure delle buste, dopo sleeveyourgames (`etl/syg_api`).
Serve per i giochi troppo nuovi/assenti da sleeveyourgames (es. Intarsia 2024):
le dimensioni delle carte vivono comunque su BGG, nella sezione "Sleeves" della
pagina gioco, alimentata da un endpoint JSON aperto su `api.geekdo.com` (stesso
host non-Cloudflare di `etl/bgg_files_api`):

    GET https://api.geekdo.com/api/cardsetsbygame?objectid=<bggid>

Shape (scoperta intercettando il traffico della pagina /sleeves, 2026-06):
    {"cardSets": [
        {"addon": false,            # false = base game, true = espansione
         "name": null|"<nome set>",
         "cardTypes": [
            {"width": "44", "height": "68", "quantity": "81",
             "name": "Material and Starting Hand cards"}, ...]},
        ...],
     "hasBaseOrExpansionCardSets": true}

`cardSets:[]` = gioco senza carte note (es. Azul). `null` = id inesistente.

NOTA "edizioni multiple": un gioco può avere PIÙ cardSet con addon=false (sono
edizioni diverse della stessa scatola, NON da sommare — es. Wingspan 212 vs 222).
Prendiamo il PRIMO come base (ordine BGG = più rilevante), come fa sleeveyourgames.

L'XML API ufficiale di BGG NON espone le misure carte: questo endemic, pur non
documentato, è l'unica via BGG. HTTP+parse isolati qui (come bgg_files_api/syg_api)
così una rottura del markup è un fix a un file solo. Output già nella forma di
`set_sleeve_requirements` ({count, width_mm, height_mm}).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL   = "https://api.geekdo.com/api/cardsetsbygame"
# Cache root overridable via BOARDY_CACHE_DIR (read-only code mount in Docker).
_CACHE_ROOT = os.environ.get("BOARDY_CACHE_DIR")
CACHE_DIR = (Path(_CACHE_ROOT) / "bgg_cards" if _CACHE_ROOT
             else Path(__file__).resolve().parent / ".bgg_cards_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

RATE_DELAY_S = 0.5
_last_call_ts = 0.0


class BGGCardsError(RuntimeError):
    """Raised on any non-200 / non-JSON from the geekdo cardsets API."""


def _http_get_json(params: dict, *, cache_key: str, use_cache: bool = True):
    global _last_call_ts
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    elapsed = time.time() - _last_call_ts
    if elapsed < RATE_DELAY_S:
        time.sleep(RATE_DELAY_S - elapsed)

    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise BGGCardsError(f"BGG cards HTTP {e.code} on {url}") from None
    except urllib.error.URLError as e:
        raise BGGCardsError(f"BGG cards network error on {url}: {e.reason}") from None
    finally:
        _last_call_ts = time.time()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise BGGCardsError(f"BGG cards returned non-JSON for {url}") from None

    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# -------------------- parsing --------------------

def _cardtypes_to_requirements(card_types: list | None,
                               note: str | None = None) -> list[dict]:
    """Map a cardSet's `cardTypes` to set_sleeve_requirements items, aggregating
    by (width, height) so multiple card types of the same size sum into one line.

    API item: {"width": "44", "height": "68", "quantity": "81", "name": ...}
    Boardy:   {"count": 81, "width_mm": 44.0, "height_mm": 68.0, "note": ...}
    """
    agg: dict[tuple[float, float], int] = {}
    for c in card_types or []:
        try:
            w = float(c["width"])
            h = float(c["height"])
            n = int(c.get("quantity") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or n <= 0:
            continue
        agg[(w, h)] = agg.get((w, h), 0) + n

    out = []
    for (w, h), n in sorted(agg.items(), key=lambda kv: kv[1], reverse=True):
        item = {"count": n, "width_mm": w, "height_mm": h}
        if note:
            item["note"] = note
        out.append(item)
    return out


def parse_cardsets(data: dict | None) -> dict | None:
    """Normalize a `cardsetsbygame` payload into the bits Boardy needs.

    Returns base-game requirements (first addon=false set) plus a per-expansion
    breakdown (each addon=true set). None when there are no card sets at all
    (game has no cards, or id unknown) — caller should fall back to manual entry.
    """
    if not isinstance(data, dict):
        return None
    card_sets = data.get("cardSets") or []
    if not card_sets:
        return None

    base_reqs: list[dict] = []
    expansions: list[dict] = []
    base_taken = False
    for cs in card_sets:
        reqs = _cardtypes_to_requirements(cs.get("cardTypes"))
        if not reqs:
            continue
        if not cs.get("addon"):
            if not base_taken:           # first base set only (avoid edition double-count)
                base_reqs = reqs
                base_taken = True
        else:
            name = cs.get("name") or "espansione"
            expansions.append({
                "name": name,
                "requirements": _cardtypes_to_requirements(
                    cs.get("cardTypes"), note=f"espansione: {name}"),
            })

    # A game whose only sets are expansions: surface them, base empty.
    if not base_reqs and not expansions:
        return None

    return {
        "base_requirements": base_reqs,
        "expansions": expansions,
    }


# -------------------- HTTP wrapper --------------------

def lookup(bgg_id: int, *, use_cache: bool = True) -> dict | None:
    """BGG id → sleeve requirements (base + expansions), or None if no cards."""
    data = _http_get_json({"objectid": int(bgg_id)},
                          cache_key=f"cards_{int(bgg_id)}", use_cache=use_cache)
    return parse_cardsets(data)


# -------------------- CLI smoke test --------------------

if __name__ == "__main__":
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="BGG cardsetsbygame smoke test")
    p.add_argument("bgg_id", type=int)
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()
    try:
        print(json.dumps(lookup(args.bgg_id, use_cache=not args.no_cache),
                         ensure_ascii=False, indent=2))
    except BGGCardsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
