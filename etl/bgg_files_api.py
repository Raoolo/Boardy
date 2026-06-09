"""BoardGameGeek Files discovery (deterministic, no browser, no auth).

BGG's per-game Files section is reachable as plain JSON at
`https://api.geekdo.com/api/files?objectid=<bggid>&objecttype=thing` — this host
is NOT behind Cloudflare (only the React HTML at boardgamegeek.com/filepage is).
Each entry carries `fileid` AND `filepageid` (they DIFFER), `filename`, `title`,
`description` ("Rulebook in English"), `language`, vote/download counts — perfect
for finding + ranking a game's rulebook. The ACTUAL download needs a headless
browser (the file URL is JS-computed) and keys on `filepageid` — that lives in
`etl/bgg_browser.py`. This module only discovers.

Mirrors etl/syg_api / etl/onejour_api (browser headers, rate-limit, on-disk
cache). Undocumented API → isolated here so a breakage is a one-file fix.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL   = "https://api.geekdo.com/api/files"
CACHE_DIR = Path(__file__).resolve().parent / ".bgg_files_cache"
CACHE_DIR.mkdir(exist_ok=True)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

RATE_DELAY_S = 0.5
_last_call_ts = 0.0

# Keyword → weight: how strongly a file's text marks it as a full rulebook.
# Checked against description + title + filename (lowercased).
_STRONG = ("rulebook", "rule book", "rules", "regolamento", "regole", "règle",
           "regle", "anleitung", "spielregeln", "reglas", "livret", "manual",
           "instruction", "istruzioni", "regras")
_WEAK = ("reference", "summary", "aid", "player aid", "cheat", "quick", "faq",
         "errata", "variant", "scenario", "card list", "appendix")


class BGGFilesError(RuntimeError):
    """Raised on any non-200 / non-JSON from the geekdo files API."""


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
        raise BGGFilesError(f"BGG files HTTP {e.code} on {url}") from None
    except urllib.error.URLError as e:
        raise BGGFilesError(f"BGG files network error on {url}: {e.reason}") from None
    finally:
        _last_call_ts = time.time()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise BGGFilesError(f"BGG files returned non-JSON for {url}") from None

    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _as_text(v) -> str:
    """description can be a str or {'rendered': str} depending on endpoint."""
    if isinstance(v, dict):
        return v.get("rendered") or ""
    return v or ""


def _parse_file(f: dict) -> dict:
    return {
        "fileid": int(f.get("fileid") or 0),
        # filepageid != fileid; the /filepage/<id>/ download URL keys on THIS one.
        "filepageid": int(f.get("filepageid") or f.get("fileid") or 0),
        "filename": f.get("filename") or "",
        "title": f.get("title") or "",
        "description": _as_text(f.get("description")).strip(),
        "language": f.get("language") or "",
        "numpositive": int(f.get("numpositive") or 0),
        "downloadcount": int(f.get("downloadCount") or f.get("downloadcount") or 0),
        "href": f.get("href") or "",
    }


def list_files(bgg_id: int, *, max_items: int = 100, use_cache: bool = True) -> list[dict]:
    """All files attached to a BGG game (paginated under the hood)."""
    out: list[dict] = []
    page = 1
    while len(out) < max_items:
        data = _http_get_json(
            {"objectid": bgg_id, "objecttype": "thing", "showcount": 50,
             "pageid": page, "sort": "hot"},
            cache_key=f"files_{bgg_id}_p{page}", use_cache=use_cache,
        )
        files = data.get("files") or []
        if not files:
            break
        out.extend(_parse_file(f) for f in files)
        endpage = (data.get("config") or {}).get("endpage")
        if not endpage or page >= int(endpage):
            break
        page += 1
    return out[:max_items]


def _rulebook_score(f: dict, lang_pref: str | None) -> float | None:
    """Score a file as a rulebook candidate, or None if it doesn't look like one."""
    blob = f"{f['description']} {f['title']} {f['filename']}".lower()
    if not any(k in blob for k in _STRONG):
        return None  # not a rulebook at all
    score = 1.0
    # downgrade aids/references/summaries — useful but not the full rules
    if any(k in blob for k in _WEAK):
        score -= 0.5
    # a description literally starting with "rulebook"/"rules" is the real deal
    desc = f["description"].lower()
    if desc.startswith("rulebook") or desc.startswith("rule") or "rulebook" in f["filename"].lower():
        score += 0.5
    # language preference
    lang = (f["language"] or "").lower()
    if lang_pref and lang == lang_pref.lower():
        score += 2.0
    elif lang in ("english", "en"):
        score += 1.0
    # popularity tiebreaker (kept small so it never overrides language/type)
    score += min(f["downloadcount"], 50000) / 100000.0
    score += min(f["numpositive"], 200) / 1000.0
    return round(score, 4)


def find_rulebooks(bgg_id: int, *, lang_pref: str | None = None,
                   use_cache: bool = True) -> list[dict]:
    """Rulebook candidates for a game, best first. Adds `score` to each file dict."""
    cands = []
    for f in list_files(bgg_id, use_cache=use_cache):
        s = _rulebook_score(f, lang_pref)
        if s is not None:
            cands.append({**f, "score": s})
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands
