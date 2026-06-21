"""1jour-1jeu.com (1j1ju) rulebook search client (deterministic, no LLM).

1j1ju hosts 4700+ board-game rulebooks as direct PDFs on `cdn.1j1ju.com`,
multilingual. The on-site search box is JS/AJAX so plain GET on `/rules?search=`
is ignored — BUT the autocomplete it drives, `GET /rules/search?q=Q`, returns
plain HTML listing the matching rule entries with their direct `.pdf` links.
We hit that endpoint and scrape the `<a class="dark-link" href="...pdf">` anchors.

Why this beats Tavily for rulebooks: deterministic, free (no API credits), and
Tavily barely indexes cdn.1j1ju.com (returns near-nothing). Why not BGG's Files
section: the XML API doesn't expose it and the web page is Cloudflare/cookie-
walled — see docs/LEARNINGS.md.

CAVEAT: this scrapes an undocumented HTML endpoint; markup can change without
notice. The HTTP + parsing layers are isolated here (mirroring etl/bgg_api.py &
etl/syg_api.py) so a breakage is a one-file fix. Callers must handle OneJourError
gracefully (fall back to web_search, then manual PDF URL / local file).
"""
from __future__ import annotations

import html as _html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL  = "https://en.1jour-1jeu.com"
# Cache root overridable via BOARDY_CACHE_DIR (read-only code mount in Docker).
_CACHE_ROOT = os.environ.get("BOARDY_CACHE_DIR")
CACHE_DIR = (Path(_CACHE_ROOT) / "onejour" if _CACHE_ROOT
             else Path(__file__).resolve().parent / ".onejour_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": BASE_URL + "/rules",
}

RATE_DELAY_S = 0.5
_last_call_ts = 0.0

# Primary rule anchors look like:
#   <a class="dark-link" href="https://cdn.1j1ju.com/.../x-rulebook.pdf"
#      title="Dune: Imperium Rulebook">Dune: Imperium Rulebook</a>
_ANCHOR_RE = re.compile(
    r'<a[^>]*class="dark-link"[^>]*href="(https://cdn\.1j1ju\.com/[^"]+\.pdf)"[^>]*'
    r'title="([^"]*)"',
    re.IGNORECASE,
)

# filename-suffix → language (1j1ju names files predictably).
_LANG_SUFFIXES = [
    (("-rulebook", "-rules", "-rulesheet"), "EN"),
    (("-regle", "-regles"), "FR"),
    (("-regole", "-regolamento", "-istruzioni"), "IT"),
    (("-anleitung", "-regeln", "-spielregeln"), "DE"),
    (("-reglas", "-instrucciones"), "ES"),
    (("-regras",), "PT"),
]


class OneJourError(RuntimeError):
    """Raised on any non-200 / unreadable response from 1j1ju."""


def guess_lang(url: str) -> str:
    """Best-effort language tag from a rulebook PDF filename. '?' if unknown."""
    low = url.lower()
    for suffixes, lang in _LANG_SUFFIXES:
        if any(s in low for s in suffixes):
            return lang
    return "?"


def _http_get(path: str, params: dict | None = None, *,
              cache_key: str | None = None, use_cache: bool = True) -> str:
    """Low-level GET → HTML text, with browser headers, pacing, on-disk cache."""
    global _last_call_ts

    cache_path = CACHE_DIR / f"{cache_key}.html" if cache_key else None
    if use_cache and cache_path and cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    elapsed = time.time() - _last_call_ts
    if elapsed < RATE_DELAY_S:
        time.sleep(RATE_DELAY_S - elapsed)

    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{BASE_URL}/{path}{qs}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise OneJourError(f"1j1ju HTTP {e.code} on {url}") from None
    except urllib.error.URLError as e:
        raise OneJourError(f"1j1ju network error on {url}: {e.reason}") from None
    finally:
        _last_call_ts = time.time()

    if cache_path:
        cache_path.write_text(body, encoding="utf-8")
    return body


def search_rulebooks(query: str, *, limit: int = 8, use_cache: bool = True) -> list[dict]:
    """Search 1j1ju for rulebook PDFs matching `query` (a game name).

    Returns `[{title, url, lang}]` — direct cdn.1j1ju.com .pdf links, de-duped
    in result (relevance) order, capped at `limit`. Empty list if nothing found.
    """
    safe_key = "".join(c if c.isalnum() else "_" for c in query.lower())[:60]
    html = _http_get("rules/search", {"q": query},
                     cache_key=f"rules_{safe_key}", use_cache=use_cache)

    out: list[dict] = []
    seen: set[str] = set()
    for url, title in _ANCHOR_RE.findall(html):
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": _html.unescape(title.strip()) or url.rsplit("/", 1)[-1],
                    "url": url, "lang": guess_lang(url)})
        if len(out) >= limit:
            break
    return out
