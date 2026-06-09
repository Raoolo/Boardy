"""Download BoardGameGeek files via a headless browser (Playwright/Chromium).

WHY a browser: BGG serves the file list as open JSON (see etl/bgg_files_api.py)
but the actual file URL is hash-based and computed by the page's JavaScript at
runtime — no plain HTTP client (urllib/curl, even with browser TLS/headers/XHR)
can fetch it. Navigating `/file/download/<fileid>` in a real browser runs that
JS and triggers the download, which Playwright intercepts. The browser also
solves Cloudflare's challenge transparently and (optionally) logs in for
member-gated files.

Isolated here (like etl/bgg_api / syg_api) so a BGG markup change is a one-file
fix. Heavy dependency: `playwright` + `playwright install chromium`.

Identifiers: BGG's Files API exposes BOTH a `fileid` and a `filepageid` per file
and they DIFFER. The download flow navigates `/filepage/<id>/` — which keys on
`filepageid`, NOT `fileid` (passing the fileid 404s for many files). So this
module takes the **filepageid** throughout.

Public API:
  BGGSession() — context manager; one browser for a batch (login once).
  fetch_one(filepageid) -> {ok, data} | {error} — single download, thread-safe
      (runs the sync Playwright API in a worker thread so it's safe to call
       from inside an asyncio event loop, e.g. the FastAPI/chat path).
"""
from __future__ import annotations

import os

BGG = "https://boardgamegeek.com"
LOGIN_URL = f"{BGG}/login/api/v1"
MAX_BYTES = 30 * 1024 * 1024

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class BGGBrowserError(RuntimeError):
    pass


def _first_url(obj) -> str | None:
    """Recursively find the first download URL in the downloadurls JSON response
    (shape is undocumented: dict keyed by id, or list, or nested 'url' fields)."""
    if isinstance(obj, str):
        if "download_redirect" in obj or obj.startswith("http") or obj.startswith("/file"):
            return obj
        return None
    if isinstance(obj, dict):
        # prefer explicit url-ish keys
        for k in ("url", "downloadurl", "href", "link"):
            if isinstance(obj.get(k), str) and (u := _first_url(obj[k])):
                return u
        for v in obj.values():
            if (u := _first_url(v)):
                return u
    elif isinstance(obj, list):
        for v in obj:
            if (u := _first_url(v)):
                return u
    return None


class BGGSession:
    """Holds one Chromium browser + context. Logs in once; reuse `download`."""

    def __init__(self, *, headless: bool = True,
                 username: str | None = None, password: str | None = None):
        self.headless = headless
        self.username = username if username is not None else os.environ.get("BGG_USERNAME")
        self.password = password if password is not None else os.environ.get("BGG_PASSWORD")
        self._pw = None
        self._browser = None
        self._context = None
        self._logged_in = False

    # -- lifecycle ------------------------------------------------------------
    def open(self) -> "BGGSession":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=_UA, accept_downloads=True,
        )
        # warm-up: get a Cloudflare clearance cookie before anything else
        page = self._context.new_page()
        try:
            page.goto(BGG, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        finally:
            page.close()
        self._login()
        return self

    def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- auth -----------------------------------------------------------------
    def _login(self) -> None:
        if not (self.username and self.password):
            return  # public-only mode
        try:
            resp = self._context.request.post(
                LOGIN_URL,
                data={"credentials": {"username": self.username, "password": self.password}},
                headers={"Content-Type": "application/json"},
                timeout=30000,
            )
            self._logged_in = resp.ok  # 202/204 on success
        except Exception:
            self._logged_in = False

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    # -- download -------------------------------------------------------------
    def _resolve_url(self, filepageid: int) -> str | None:
        """Get the real (hashed) file URL by letting the filepage's JS call the
        login-gated `downloadurls` API (it sends an in-page `Authorization:
        GeekAuth` header we can't easily replicate) and intercepting the
        response. Returns the (relative) URL or None.

        The dummy slug in /filepage/<filepageid>/x is fine — BGG resolves by id,
        but the id MUST be the filepageid (the fileid 404s here).
        """
        page = self._context.new_page()
        try:
            with page.expect_response(
                lambda r: "downloadurls" in r.url and r.status == 200, timeout=30000
            ) as ri:
                try:
                    page.goto(f"{BGG}/filepage/{filepageid}/x", wait_until="commit", timeout=30000)
                except Exception:
                    pass
            return _first_url(ri.value.json())
        except Exception:
            return None
        finally:
            page.close()

    def download(self, filepageid: int) -> bytes:
        """Fetch a file's bytes. Raises BGGBrowserError on failure / non-PDF.

        `filepageid` is the BGG *filepageid* (not the fileid — they differ)."""
        if not self._context:
            raise BGGBrowserError("session not open()ed")
        data: bytes | None = None

        # Resolve the hashed URL (via the page's authenticated JS call), then
        # fetch it through the CF-cleared, logged-in browser context.
        url = self._resolve_url(filepageid)
        if url:
            if url.startswith("/"):
                url = BGG + url
            try:
                r = self._context.request.get(url, timeout=30000)
                if r.ok:
                    data = r.body()
            except Exception:
                data = None

        if not data:
            hint = "" if self._logged_in else " (BGG login required — set BGG_USERNAME/BGG_PASSWORD)"
            raise BGGBrowserError(f"could not download filepage {filepageid}{hint}")
        if len(data) > MAX_BYTES:
            raise BGGBrowserError(f"file exceeds {MAX_BYTES // (1024*1024)} MB cap")
        if not data[:5].startswith(b"%PDF"):
            raise BGGBrowserError("downloaded content is not a PDF (file may require "
                                  "login, or isn't a PDF)")
        return data


def _fetch_one_sync(filepageid: int) -> dict:
    try:
        with BGGSession() as s:
            return {"ok": True, "data": s.download(filepageid), "logged_in": s.logged_in}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def fetch_one(filepageid: int) -> dict:
    """Download a single BGG file (by *filepageid*) → {ok, data, logged_in} | {error}.

    Runs the sync Playwright API in a dedicated worker thread so it is safe to
    call from inside a running asyncio loop (the FastAPI/chat request path).
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_fetch_one_sync, filepageid).result()
