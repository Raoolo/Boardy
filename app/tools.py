"""Claude tool-use functions backed by the star-schema SQLite DB.

Tools return JSON-serializable dicts/lists; schemas are at the bottom for the
Anthropic `tools=` argument.
"""
from __future__ import annotations

import os
from typing import Any

from . import audit
from .db import get_conn

# Trusted-domain allowlist for web_search. Mirrors the old Anthropic
# server-side allowlist so search quality on BGG / sleeve sites is preserved.
# Pass `include_domains=[...]` explicitly to override (e.g. only sleeveyourgames).
DEFAULT_TRUSTED_DOMAINS = [
    "boardgamegeek.com", "geekdo-images.com", "en.wikipedia.org",
    "sleevegeeks.com", "sleeveyourgames.com", "mayday-games.com",
    "dragonshield.com", "ultrapro.com", "fantasyflightgames.com",
    "asmodee.com", "cmon.com", "capstone-games.com",
    "feuerland-spiele.de", "renegadegamestudios.com",
    "stonemaiergames.com",
]

DIM_TABLES = {  # bridge_table -> (dim_table, fk_col)
    "game_designers":  ("designers",  "designer_id"),
    "game_publishers": ("publishers", "publisher_id"),
    "game_categories": ("categories", "category_id"),
    "game_mechanics":  ("mechanics",  "mechanic_id"),
}

# `sleeve_requirements` is a TODO list ("pending work"). Once a game enters one
# of these statuses, any pending rows must be cleared — they no longer
# represent real demand for `sleeve_summary.to_buy`.
DONE_SLEEVE_STATUSES = ("sleeved", "na")


def _clear_requirements_if_done(conn, gid: int, name: str,
                                 new_status: str | None,
                                 source: str | None) -> int:
    """If new_status is a 'done' status, drop pending requirements for the game.

    Returns the count of deleted rows. Audit-logs the deletion as a single
    `requirements` field change (matching set_sleeve_requirements' pattern).
    Idempotent — no-op if there were no pending rows.
    """
    if new_status not in DONE_SLEEVE_STATUSES:
        return 0
    old_rows = [dict(r) for r in conn.execute(
        "SELECT count, width_mm, height_mm, note FROM sleeve_requirements "
        "WHERE game_id=? ORDER BY width_mm, height_mm", (gid,)
    ).fetchall()]
    if not old_rows:
        return 0
    conn.execute("DELETE FROM sleeve_requirements WHERE game_id=?", (gid,))
    audit.log_change(
        conn, table="sleeve_requirements", row_id=gid, row_label=name,
        action="update", field="requirements",
        old=old_rows, new=[],
        source=f"{source or 'unknown'} cascade=status->{new_status}",
    )
    return len(old_rows)


# ----- helpers -----

def _backfill_bgg_media(game_id: int) -> bool:
    """If a game has `bgg_id` set but `thumbnail_url`/`image_url` is empty,
    fetch them via the BGG XML API and patch the row.

    Why this hook exists: when the chat-driven flow uses `web_search` to
    enrich a new game, the model often captures title/designer/weight from
    the rendered page text but skips the thumbnail (it's an `<img>` URL
    buried in HTML, not in the prose the model reads). The BGG XML API
    returns these deterministically — much more reliable than asking the
    model to extract them.

    Best-effort: any failure is swallowed. The hook runs AFTER the commit of
    `add_game` / `update_game` / `add_to_wishlist` / `update_wishlist` so a
    network/auth error never blocks the actual write.
    """
    try:
        from .db import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT bgg_id, thumbnail_url, image_url FROM games WHERE id=?",
                (game_id,),
            ).fetchone()
            if not row or not row["bgg_id"]:
                return False
            need_thumb = not row["thumbnail_url"]
            need_image = not row["image_url"]
            if not need_thumb and not need_image:
                return False
        # Outside the connection: BGG fetch can be slow (rate-limited).
        # Lazy import to avoid loading the etl module at app boot.
        from etl import bgg_api
        parsed = bgg_api.fetch_thing(int(row["bgg_id"]))
        if not parsed:
            return False
        thumb = parsed.get("thumbnail_url") if need_thumb else None
        image = parsed.get("image_url") if need_image else None
        if not thumb and not image:
            return False
        sets, vals = [], []
        if thumb:
            sets.append("thumbnail_url=?"); vals.append(thumb)
        if image:
            sets.append("image_url=?"); vals.append(image)
        vals.append(game_id)
        with get_conn() as conn:
            conn.execute(
                f"UPDATE games SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                vals,
            )
            conn.commit()
        return True
    except Exception:
        # Best-effort: telemetry-only, never break the calling write tool.
        return False


def _backfill_friendly_tags(game_id: int) -> bool:
    """Generate + persist friendly_tags for a game (best-effort, post-commit).

    Pair to `_backfill_bgg_media`: same call sites (add_game, update_game,
    add_to_wishlist, update_wishlist) so any BGG-enriched write produces
    the user-friendly tags too. Swallows all errors; the write tool already
    succeeded, so any LLM hiccup just leaves `friendly_tags` NULL for the
    next batch run to pick up.
    """
    try:
        from . import friendly_tags
        return friendly_tags.backfill_one(game_id) is not None
    except Exception:
        return False


def _upsert_dim(conn, dim_table: str, name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("empty dim name")
    conn.execute(f"INSERT OR IGNORE INTO {dim_table}(name) VALUES(?)", (name,))
    row = conn.execute(f"SELECT id FROM {dim_table} WHERE name=?", (name,)).fetchone()
    return row["id"]


def _set_bridges(conn, game_id: int, bridge: str, dim_table: str, fk_col: str, names: list[str] | None) -> None:
    """Replace bridge rows for a game with the given list (idempotent). None = leave unchanged."""
    if names is None:
        return
    conn.execute(f"DELETE FROM {bridge} WHERE game_id=?", (game_id,))
    for nm in names:
        dim_id = _upsert_dim(conn, dim_table, nm)
        conn.execute(f"INSERT OR IGNORE INTO {bridge}(game_id, {fk_col}) VALUES(?,?)", (game_id, dim_id))


def _game_dims(conn, game_id: int, bridge: str, dim_table: str, fk_col: str) -> list[str]:
    rows = conn.execute(
        f"SELECT d.name FROM {dim_table} d JOIN {bridge} b ON b.{fk_col}=d.id WHERE b.game_id=? ORDER BY d.name",
        (game_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _row_to_game_dict(row, conn, *, full: bool = False) -> dict:
    d = {k: row[k] for k in row.keys()}
    g_id = d["id"]
    d["designers"]  = _game_dims(conn, g_id, "game_designers",  "designers",  "designer_id")
    d["publishers"] = _game_dims(conn, g_id, "game_publishers", "publishers", "publisher_id")
    d["categories"] = _game_dims(conn, g_id, "game_categories", "categories", "category_id")
    d["mechanics"]  = _game_dims(conn, g_id, "game_mechanics",  "mechanics",  "mechanic_id")
    if full:
        reqs = conn.execute(
            "SELECT count, width_mm, height_mm, note FROM sleeve_requirements WHERE game_id=?",
            (g_id,),
        ).fetchall()
        d["sleeve_requirements"] = [dict(r) for r in reqs]
    # Trim verbose / null fields for compact LLM payload
    if not full and d.get("description"):
        d["description"] = (d["description"][:200] + "…") if len(d["description"]) > 200 else d["description"]
    return d


# ----- core tools -----

def list_games(
    name_contains: str | None = None,
    players: int | None = None,
    complexity_contains: str | None = None,
    sleeve_status: str | None = None,
    designer_contains: str | None = None,
    publisher_contains: str | None = None,
    category_contains: str | None = None,
    mechanic_contains: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict:
    """Return matching games as `{count, items}`. Filters AND-combined; *_contains are case-insensitive substring.

    `status` defaults to 'owned' so wishlist items don't leak into "what
    games do I have?" queries. Pass `status='wishlist'` for wishlist-only
    or `status='any'` to include both. The fence is opt-out so the LLM
    can't accidentally count a wishlist item as owned.

    The `count` envelope is intentional: LLMs are bad at counting list elements
    in attention. Returning the integer pre-computed lets the model transcribe
    the literal instead of estimating, which has caused header/list mismatches
    in past sessions (LEARNINGS 2026-04-29 PM).
    """
    sql = "SELECT DISTINCT g.* FROM games g"
    params: list[Any] = []
    where: list[str] = []
    effective_status = (status or "owned").lower()
    if effective_status != "any":
        where.append("g.status = ?")
        params.append(effective_status)

    def join_dim(bridge: str, dim: str, fk: str, needle: str) -> None:
        sql_join = f" JOIN {bridge} b_{dim} ON b_{dim}.game_id=g.id JOIN {dim} t_{dim} ON t_{dim}.id=b_{dim}.{fk}"
        nonlocal sql
        sql += sql_join
        where.append(f"LOWER(t_{dim}.name) LIKE ?")
        params.append(f"%{needle.lower()}%")

    if designer_contains:  join_dim("game_designers", "designers", "designer_id", designer_contains)
    if publisher_contains: join_dim("game_publishers", "publishers", "publisher_id", publisher_contains)
    if category_contains:  join_dim("game_categories", "categories", "category_id", category_contains)
    if mechanic_contains:  join_dim("game_mechanics", "mechanics", "mechanic_id", mechanic_contains)
    if name_contains:
        where.append("LOWER(g.name) LIKE ?"); params.append(f"%{name_contains.lower()}%")
    if players is not None:
        where.append("g.players_min <= ? AND g.players_max >= ?"); params.extend([players, players])
    if complexity_contains:
        where.append("LOWER(g.complexity_label) LIKE ?"); params.append(f"%{complexity_contains.lower()}%")
    if sleeve_status:
        where.append("g.sleeve_status = ?"); params.append(sleeve_status)

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY g.name LIMIT {int(limit)}"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        items = [_row_to_game_dict(r, conn, full=False) for r in rows]
        return {"count": len(items), "items": items}


def get_game(name: str) -> dict | None:
    """Get one game (case-insensitive name match) with all dimensions + sleeve requirements."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM games WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not row:
            cands = conn.execute(
                "SELECT * FROM games WHERE LOWER(name) LIKE ?", (f"%{name.lower()}%",)
            ).fetchall()
            if len(cands) == 1:
                row = cands[0]
            elif len(cands) > 1:
                return {"error": "ambiguous", "matches": [c["name"] for c in cands]}
            else:
                return None
        return _row_to_game_dict(row, conn, full=True)


def add_game(
    name: str,
    bgg_id: int | None = None,
    year_published: int | None = None,
    players_min: int | None = None,
    players_max: int | None = None,
    players_best: str | None = None,
    duration_min: int | None = None,
    duration_min_min: int | None = None,
    duration_max_min: int | None = None,
    age_min: int | None = None,
    complexity_label: str | None = None,
    complexity_weight: float | None = None,
    bgg_rating: float | None = None,
    description: str | None = None,
    thumbnail_url: str | None = None,
    image_url: str | None = None,
    language: str | None = None,
    condition: str | None = None,
    notes: str | None = None,
    sleeve_status: str | None = None,
    designers: list[str] | None = None,
    publishers: list[str] | None = None,
    categories: list[str] | None = None,
    mechanics: list[str] | None = None,
    _source: str | None = None,
) -> dict:
    """Insert a new game. Fails if name already exists. Lists upsert into dim tables."""
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM games WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
            return {"error": f"Game {name!r} already exists. Use update_game to modify."}
        cur = conn.execute(
            """INSERT INTO games(name, bgg_id, year_published, players_min, players_max,
                                 players_best, duration_min, duration_min_min, duration_max_min,
                                 age_min, complexity_label, complexity_weight, bgg_rating,
                                 description, thumbnail_url, image_url, language, condition,
                                 notes, sleeve_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, bgg_id, year_published, players_min, players_max, players_best,
             duration_min, duration_min_min, duration_max_min, age_min,
             complexity_label, complexity_weight, bgg_rating, description,
             thumbnail_url, image_url, language, condition, notes, sleeve_status or "unknown"),
        )
        gid = cur.lastrowid
        bridge_added: dict[str, list[str]] = {}
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            vals = locals().get(arg_name)
            _set_bridges(conn, gid, bridge, dim, fk, vals)
            if vals:
                bridge_added[arg_name] = vals
        snapshot = dict(conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        snapshot.update(bridge_added)  # include lists in the audit snapshot
        audit.log_full(conn, table="games", row_id=gid, row_label=name,
                       action="insert", snapshot=snapshot, source=_source)
        conn.commit()
    # Auto-embed description if provided. Lazy import to keep server boot
    # cheap — first call loads the e5 model (~3s on warm cache).
    if description:
        try:
            from . import games_semantic
            games_semantic.embed_one(gid)
        except Exception:
            # Embedding is best-effort; never fail an add_game on it.
            pass
    # Backfill thumbnail/image via BGG XML API if we have a bgg_id but the
    # chat-driven enrichment didn't capture the image URLs.
    _backfill_bgg_media(gid)
    _backfill_friendly_tags(gid)
    # Auto-fetch the rulebook IFF we find a confident exact-title match online;
    # ambiguous cases are left for the chat propose-and-confirm flow. Best-effort.
    # Surface the outcome so the chat layer can be honest ("ho cercato, nessun
    # match affidabile") and propose low-confidence candidates instead of lying
    # ("non l'ho cercato") — see app/chat.py system prompt.
    rb = _backfill_rulebook(gid)
    return {"ok": True, "id": gid, "name": name, "rulebook_autofetch": rb}


def update_game(
    name: str,
    bgg_id: int | None = None,
    year_published: int | None = None,
    players_min: int | None = None,
    players_max: int | None = None,
    players_best: str | None = None,
    duration_min: int | None = None,
    duration_min_min: int | None = None,
    duration_max_min: int | None = None,
    age_min: int | None = None,
    complexity_label: str | None = None,
    complexity_weight: float | None = None,
    bgg_rating: float | None = None,
    description: str | None = None,
    thumbnail_url: str | None = None,
    image_url: str | None = None,
    language: str | None = None,
    condition: str | None = None,
    notes: str | None = None,
    sleeve_status: str | None = None,
    designers: list[str] | None = None,
    publishers: list[str] | None = None,
    categories: list[str] | None = None,
    mechanics: list[str] | None = None,
    _source: str | None = None,
) -> dict:
    """Patch fields on an existing game. Only non-null args are updated. Lists REPLACE bridges."""
    scalar_fields = {
        "bgg_id": bgg_id, "year_published": year_published,
        "players_min": players_min, "players_max": players_max, "players_best": players_best,
        "duration_min": duration_min, "duration_min_min": duration_min_min,
        "duration_max_min": duration_max_min, "age_min": age_min,
        "complexity_label": complexity_label, "complexity_weight": complexity_weight,
        "bgg_rating": bgg_rating, "description": description,
        "thumbnail_url": thumbnail_url, "image_url": image_url, "language": language,
        "condition": condition, "notes": notes, "sleeve_status": sleeve_status,
    }
    fields = {k: v for k, v in scalar_fields.items() if v is not None}
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM games WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not row:
            return {"error": f"Game {name!r} not found"}
        gid = row["id"]

        # Snapshot BEFORE for audit (scalars + lists).
        before = dict(conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            before[arg_name] = _game_dims(conn, gid, bridge, dim, fk)

        if fields:
            fields["updated_at"] = "CURRENT_TIMESTAMP"  # placeholder — handled below
            sets = ", ".join(f"{k}=?" for k in fields if k != "updated_at") + ", updated_at=CURRENT_TIMESTAMP"
            vals = [v for k, v in fields.items() if k != "updated_at"]
            conn.execute(f"UPDATE games SET {sets} WHERE id=?", [*vals, gid])
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            _set_bridges(conn, gid, bridge, dim, fk, locals().get(arg_name))

        # Cascade: status flipped to a 'done' state → drop pending requirements.
        # Runs INSIDE the same transaction so a failed audit rolls back the delete.
        cleared = _clear_requirements_if_done(conn, gid, name, sleeve_status, _source)

        # Snapshot AFTER and diff.
        after = dict(conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            after[arg_name] = _game_dims(conn, gid, bridge, dim, fk)
        n_logged = audit.log_diff(conn, table="games", row_id=gid, row_label=name,
                                  before=before, after=after, source=_source)
        conn.commit()
    result = {"ok": True, "name": name, "updated_scalar": list(fields.keys()),
              "audit_rows": n_logged}
    if cleared:
        result["cleared_requirements"] = cleared
        result["note"] = (f"sleeve_status set to {sleeve_status!r}; cleared "
                          f"{cleared} pending sleeve_requirements row(s).")
    # Auto-embed iff description was actually patched. The hash check inside
    # embed_one makes this a no-op if the text didn't really change.
    if description is not None:
        try:
            from . import games_semantic
            games_semantic.embed_one(gid)
        except Exception:
            pass
    # Backfill thumbnail/image via BGG XML API if missing (e.g. bgg_id was
    # just set but the chat didn't fetch the image fields).
    _backfill_bgg_media(gid)
    _backfill_friendly_tags(gid)
    return result


def delete_game(name: str, _source: str | None = None) -> dict:
    """Remove a game (cascade deletes dim links + sleeve requirements)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM games WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not row:
            return {"error": f"Game {name!r} not found"}
        gid = row["id"]
        snapshot = dict(row)
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            snapshot[arg_name] = _game_dims(conn, gid, bridge, dim, fk)
        audit.log_full(conn, table="games", row_id=gid, row_label=name,
                       action="delete", snapshot=snapshot, source=_source)
        conn.execute("DELETE FROM games WHERE id=?", (gid,))
        conn.commit()
    return {"ok": True, "deleted": name}


def set_sleeve_requirements(name: str, requirements: list[dict],
                            _source: str | None = None) -> dict:
    """Replace sleeve requirements for a game. Items: {count, width_mm, height_mm, note?}.

    Refuses if the game's `sleeve_status` is in DONE_SLEEVE_STATUSES — those
    games are considered "done" and must not carry pending TODO rows. Flip
    status first via `update_game(name, sleeve_status='to_sleeve' | 'unknown')`
    if you actually want to record new pending work.
    Empty `requirements=[]` is allowed regardless (acts as a clear).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, sleeve_status FROM games WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
        if not row:
            return {"error": f"Game {name!r} not found"}
        gid = row["id"]
        if requirements and row["sleeve_status"] in DONE_SLEEVE_STATUSES:
            return {"error": (
                f"{name!r} has sleeve_status={row['sleeve_status']!r}; pending "
                f"requirements are not allowed on 'done' games. "
                f"Call update_game(name, sleeve_status='to_sleeve') first if "
                f"this game actually needs sleeves recorded."
            )}
        # Snapshot existing requirements as a sorted list of tuples for diff readability.
        before_rows = [dict(r) for r in conn.execute(
            "SELECT count, width_mm, height_mm, note FROM sleeve_requirements WHERE game_id=? ORDER BY width_mm, height_mm",
            (gid,)).fetchall()]
        conn.execute("DELETE FROM sleeve_requirements WHERE game_id=?", (gid,))
        for r in requirements:
            try:
                conn.execute(
                    """INSERT INTO sleeve_requirements(game_id, count, width_mm, height_mm, note)
                       VALUES(?,?,?,?,?)""",
                    (gid, int(r["count"]), float(r["width_mm"]), float(r["height_mm"]), r.get("note")),
                )
            except (KeyError, ValueError, TypeError) as e:
                return {"error": f"bad requirement {r!r}: {e}"}
        # Single audit row capturing the whole replacement (the granularity here is the game).
        audit.log_change(conn, table="sleeve_requirements", row_id=gid, row_label=name,
                         action="update", field="requirements",
                         old=before_rows, new=requirements, source=_source)
        conn.commit()
    return {"ok": True, "name": name, "rows": len(requirements)}


def sleeve_summary() -> dict:
    """Per sleeve size: needed (sum across games), owned (inventory), to_buy. Returns `{count, items}`.

    Only OWNED games contribute to `needed`. Wishlist items might have
    BGG-derived sleeve sizes but they're hypothetical — counting them as
    "to buy now" would inflate the dashboard. They get included automatically
    the moment the user calls `mark_as_owned`.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.width_mm, s.height_mm, s.needed,
                   COALESCE(i.owned, 0) AS owned,
                   MAX(0, s.needed - COALESCE(i.owned, 0)) AS to_buy,
                   s.games
            FROM (
              SELECT width_mm, height_mm,
                     SUM(count) AS needed,
                     GROUP_CONCAT(g.name, ', ') AS games
              FROM sleeve_requirements sr JOIN games g ON g.id=sr.game_id
              WHERE g.status='owned'
              GROUP BY width_mm, height_mm
            ) s
            LEFT JOIN (
              SELECT width_mm, height_mm, SUM(count_owned) AS owned
              FROM sleeve_inventory GROUP BY width_mm, height_mm
            ) i USING (width_mm, height_mm)
            ORDER BY to_buy DESC, needed DESC
            """
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"count": len(items), "items": items}


def games_ready_to_sleeve() -> dict:
    """Owned games with `sleeve_status='to_sleeve'` whose ENTIRE requirement
    list is covered by current sleeve_inventory. Returns:

      {
        "count_ready": N,           # number of games doable right now
        "ready": [ {id, name, thumbnail_url, reqs, total} ... ],
        "not_ready": [ {id, name, reqs, missing: [{size, short_by}]} ... ],
        "has_contention": bool,     # ready games compete for same stock?
        "contention_note": str | None,
      }

    Two important nuances:
    - The check is PER-GAME and INDEPENDENT — each game is tested against
      the full inventory snapshot. Two games that individually fit but
      together exceed stock will BOTH appear in `ready`.
    - `has_contention=True` flags that case: we re-run a greedy alphabetical
      pass deducting stock as we go, and if it yields fewer games than the
      independent check, the difference is the contention.

    Used by `/sleeves/data` (dashboard "Pronti da sleevare" section) and as
    a chat tool for "cosa posso sleevare ora?" questions.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.thumbnail_url,
                   sr.width_mm, sr.height_mm, sr.count, sr.note
            FROM games g
            JOIN sleeve_requirements sr ON sr.game_id = g.id
            WHERE g.status='owned' AND g.sleeve_status='to_sleeve'
            ORDER BY g.name, sr.width_mm, sr.height_mm
            """
        ).fetchall()
        inv_rows = conn.execute(
            "SELECT width_mm, height_mm, SUM(count_owned) AS owned "
            "FROM sleeve_inventory GROUP BY width_mm, height_mm"
        ).fetchall()

    inv = {(r["width_mm"], r["height_mm"]): r["owned"] or 0 for r in inv_rows}

    # Group requirements by game (preserve order from SQL).
    by_game: dict[int, dict] = {}
    for r in rows:
        g = by_game.setdefault(r["id"], {
            "id": r["id"], "name": r["name"],
            "thumbnail_url": r["thumbnail_url"],
            "reqs": [], "total": 0,
        })
        g["reqs"].append({
            "width_mm": r["width_mm"], "height_mm": r["height_mm"],
            "count": r["count"], "note": r["note"],
        })
        g["total"] += r["count"]

    def _size_label(w: float, h: float) -> str:
        def fmt(x: float) -> str:
            return str(int(x)) if x == int(x) else f"{x:g}"
        return f"{fmt(w)}×{fmt(h)}"

    ready: list[dict] = []
    not_ready: list[dict] = []
    for g in by_game.values():
        missing = []
        for req in g["reqs"]:
            have = inv.get((req["width_mm"], req["height_mm"]), 0)
            if have < req["count"]:
                missing.append({
                    "size": _size_label(req["width_mm"], req["height_mm"]),
                    "needed": req["count"], "have": have,
                    "short_by": req["count"] - have,
                })
        item = {**g, "size_labels": [_size_label(r["width_mm"], r["height_mm"]) for r in g["reqs"]]}
        if missing:
            not_ready.append({**item, "missing": missing})
        else:
            ready.append(item)

    # Contention check — greedy alphabetical pass deducting stock.
    sim_inv = dict(inv)
    greedy_ok = 0
    for g in sorted(ready, key=lambda x: x["name"].lower()):
        if all(sim_inv.get((r["width_mm"], r["height_mm"]), 0) >= r["count"]
               for r in g["reqs"]):
            for r in g["reqs"]:
                sim_inv[(r["width_mm"], r["height_mm"])] -= r["count"]
            greedy_ok += 1
    has_contention = greedy_ok < len(ready)
    contention_note = None
    if has_contention:
        contention_note = (
            f"⚠️ {len(ready) - greedy_ok} gioco/i in 'pronti' competono per "
            f"la stessa misura. In sequenza ne sleevi solo {greedy_ok}."
        )

    return {
        "count_ready": len(ready),
        "ready": ready,
        "not_ready": not_ready,
        "has_contention": has_contention,
        "contention_note": contention_note,
    }


def sleeve_summary_wishlist() -> dict:
    """Per-size sleeve preview for WISHLIST games. Returns `{count, items}`.

    Mirrors `sleeve_summary` but filtered to `games.status='wishlist'`.
    No `owned` / `to_buy` columns — wishlist demand is hypothetical, you
    don't "owe" sleeves for a game you don't own yet. Use this to answer
    "if I bought everything on my wishlist, how many sleeves would I need?"
    or "are there sleeve sizes I'd need for wishlist that I don't already
    cover?".

    Each item: `{width_mm, height_mm, needed, games}` where `games` is the
    GROUP_CONCAT of wishlist game names contributing to that size.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT width_mm, height_mm,
                   SUM(count) AS needed,
                   GROUP_CONCAT(g.name, ', ') AS games
            FROM sleeve_requirements sr JOIN games g ON g.id=sr.game_id
            WHERE g.status='wishlist'
            GROUP BY width_mm, height_mm
            ORDER BY needed DESC
            """
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"count": len(items), "items": items}


def list_inventory() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT width_mm, height_mm, count_owned, brand FROM sleeve_inventory ORDER BY width_mm, height_mm"
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"count": len(items), "items": items}


def update_inventory(width_mm: float, height_mm: float, count_owned: int,
                     brand: str | None = None, _source: str | None = None) -> dict:
    """Upsert a sleeve inventory row (sets ABSOLUTE count, not delta).

    For "I just bought N more" use `add_to_inventory` instead — it does the
    arithmetic server-side so the model can't get the addition wrong.
    """
    label = f"{width_mm}x{height_mm}" + (f"/{brand}" if brand else "")
    with get_conn() as conn:
        before = conn.execute(
            "SELECT id, count_owned FROM sleeve_inventory "
            "WHERE width_mm=? AND height_mm=? AND brand IS ?",
            (width_mm, height_mm, brand),
        ).fetchone()
        old_count = before["count_owned"] if before else None
        conn.execute(
            """INSERT INTO sleeve_inventory(width_mm, height_mm, count_owned, brand)
               VALUES(?,?,?,?)
               ON CONFLICT(width_mm, height_mm, brand) DO UPDATE SET count_owned=excluded.count_owned""",
            (width_mm, height_mm, count_owned, brand),
        )
        row_id = conn.execute(
            "SELECT id FROM sleeve_inventory WHERE width_mm=? AND height_mm=? AND brand IS ?",
            (width_mm, height_mm, brand),
        ).fetchone()["id"]
        if before is None:
            audit.log_full(conn, table="sleeve_inventory", row_id=row_id, row_label=label,
                           action="insert",
                           snapshot={"width_mm": width_mm, "height_mm": height_mm,
                                     "count_owned": count_owned, "brand": brand},
                           source=_source)
        elif old_count != count_owned:
            audit.log_change(conn, table="sleeve_inventory", row_id=row_id, row_label=label,
                             action="update", field="count_owned",
                             old=old_count, new=count_owned, source=_source)
        conn.commit()
    return {"ok": True, "width_mm": width_mm, "height_mm": height_mm,
            "count_owned": count_owned, "brand": brand,
            "previous_count": old_count}


def add_to_inventory(width_mm: float, height_mm: float, delta: int,
                     brand: str | None = None, note: str | None = None,
                     _source: str | None = None) -> dict:
    """Add (or subtract, with negative delta) sleeves from inventory by DELTA.

    Server-side arithmetic so the model never has to compute new = old + bought.
    Creates the row if missing (start = max(0, delta)). Refuses to go negative.
    """
    if not isinstance(delta, int) or delta == 0:
        return {"error": "delta must be a non-zero integer"}
    label = f"{width_mm}x{height_mm}" + (f"/{brand}" if brand else "")
    with get_conn() as conn:
        before = conn.execute(
            "SELECT id, count_owned FROM sleeve_inventory "
            "WHERE width_mm=? AND height_mm=? AND brand IS ?",
            (width_mm, height_mm, brand),
        ).fetchone()
        old_count = before["count_owned"] if before else 0
        new_count = old_count + delta
        if new_count < 0:
            return {"error": f"would go negative: current={old_count}, delta={delta}"}
        if before is None:
            conn.execute(
                "INSERT INTO sleeve_inventory(width_mm, height_mm, count_owned, brand) VALUES(?,?,?,?)",
                (width_mm, height_mm, new_count, brand),
            )
            row_id = conn.execute(
                "SELECT id FROM sleeve_inventory WHERE width_mm=? AND height_mm=? AND brand IS ?",
                (width_mm, height_mm, brand),
            ).fetchone()["id"]
            audit.log_full(conn, table="sleeve_inventory", row_id=row_id, row_label=label,
                           action="insert",
                           snapshot={"width_mm": width_mm, "height_mm": height_mm,
                                     "count_owned": new_count, "brand": brand,
                                     "_via": "add_to_inventory", "_delta": delta,
                                     "_note": note},
                           source=_source)
        else:
            row_id = before["id"]
            conn.execute("UPDATE sleeve_inventory SET count_owned=? WHERE id=?",
                         (new_count, row_id))
            audit.log_change(conn, table="sleeve_inventory", row_id=row_id, row_label=label,
                             action="update", field="count_owned",
                             old=old_count, new=new_count,
                             source=(f"{_source} delta={delta:+d}"
                                     + (f" note={note!r}" if note else "")) if _source
                                    else f"delta={delta:+d}")
        conn.commit()
    return {"ok": True, "width_mm": width_mm, "height_mm": height_mm,
            "previous_count": old_count, "delta": delta, "count_owned": new_count,
            "brand": brand}


def recent_changes(limit: int = 20, table: str | None = None,
                   game_name: str | None = None) -> dict:
    """Read the audit log: last N writes, optionally filtered by table or by game.

    Returns `{count, items}`. `game_name` filters changes affecting the named
    game (resolves to its id and looks at table_name='games' rows for that id,
    plus sleeve_requirements rows keyed by the same game_id).
    """
    with get_conn() as conn:
        if game_name:
            row = conn.execute("SELECT id, name FROM games WHERE LOWER(name)=LOWER(?)",
                               (game_name,)).fetchone()
            if not row:
                return {"error": f"Game {game_name!r} not found"}
            gid = row["id"]
            rows = conn.execute(
                """SELECT id, ts, table_name, row_id, row_label, action, field,
                          old_value, new_value, source
                   FROM changes
                   WHERE (table_name='games' AND row_id=?)
                      OR (table_name='sleeve_requirements' AND row_id=?)
                   ORDER BY id DESC LIMIT ?""",
                (gid, gid, limit),
            ).fetchall()
            import json as _json
            out = []
            for r in rows:
                d = dict(r)
                for k in ("old_value", "new_value"):
                    if d[k] is not None:
                        try:
                            d[k] = _json.loads(d[k])
                        except (_json.JSONDecodeError, TypeError):
                            pass
                out.append(d)
            return {"count": len(out), "items": out}
        items = audit.recent(conn, limit=limit, table=table)
        return {"count": len(items), "items": items}


def ingest_rulebook(game_name: str, pdf_path: str) -> dict:
    """Index a rulebook PDF for semantic search. Lazy-imported to avoid loading
    the embedding model at server boot — first call costs ~5s on warm cache."""
    from . import rulebooks
    return rulebooks.ingest(game_name, pdf_path)


def ask_rules(game_name: str, question: str, k: int = 5) -> dict:
    """Return top-k rulebook excerpts most relevant to the question.

    The CALLING MODEL must synthesize the final answer from these excerpts
    and cite the page numbers. Do NOT invent rules not present in the chunks.
    """
    from . import rulebooks
    chunks = rulebooks.search(game_name, question, k=k)
    if not chunks:
        return {
            "error": f"No rulebook ingested for {game_name!r}. "
                     f"Call `find_rulebook` to locate the PDF online, propose it, "
                     f"and on confirmation `download_rulebook` — or `ingest_rulebook` "
                     f"for a local file."
        }
    return {"game": game_name, "question": question, "chunks": chunks}


def list_rulebooks() -> dict:
    """List all ingested rulebooks (one row per game+source). Returns `{count, items}`."""
    from . import rulebooks
    items = rulebooks.list_rulebooks()
    return {"count": len(items), "items": items}


# --- rulebook discovery + download -------------------------------------------
# Two sources, complementary:
#  - 1j1ju.com (etl/onejour_api): direct cdn .pdf links, deterministic, no auth.
#    Primary because a candidate carries a downloadable URL right away.
#  - BGG Files (etl/bgg_files_api + etl/bgg_browser): richer/multilingual, the
#    file LIST is open JSON but the actual download needs a logged-in headless
#    browser (the file URL is JS-computed + login-gated). A BGG candidate carries
#    a `bgg_filepageid` instead of a URL → download_rulebook(bgg_filepageid=...).
# Tavily stays as a last-resort broad .pdf search when both find nothing.


def _bgg_candidates(bgg_id: int) -> list[dict]:
    """Rulebook candidates from BGG Files (no download — just metadata)."""
    try:
        from etl import bgg_files_api
        out = []
        for f in bgg_files_api.find_rulebooks(int(bgg_id))[:6]:
            out.append({
                "title": f["title"] or f["filename"],
                # download keys on filepageid (≠ fileid) — see bgg_browser
                "bgg_filepageid": f["filepageid"],
                "source": "bgg",
                "lang": f["language"] or "?",
                "note": f["description"][:60],
            })
        return out
    except Exception:
        return []


def find_rulebook(game_name: str, bgg_id: int | None = None) -> dict:
    """Find downloadable rulebooks for a game. Does NOT download.

    Returns candidates from 1j1ju (carry `url`) + BGG Files (carry `bgg_filepageid`)
    for the model to propose to the user. Broad Tavily .pdf search only if both
    sources are empty. Each candidate has `source` + `lang`.
    """
    candidates: list[dict] = []

    # 1j1ju.com deterministic HTML search (direct PDF links)
    try:
        from etl import onejour_api
        for h in onejour_api.search_rulebooks(game_name, limit=6):
            candidates.append({"title": h["title"], "url": h["url"],
                               "source": "1j1ju.com", "lang": h["lang"]})
    except Exception:
        pass

    # BGG Files (needs bgg_id) — fileid-based, downloaded via browser
    if bgg_id:
        candidates += _bgg_candidates(bgg_id)

    if candidates:
        return {"game": game_name, "count": len(candidates), "candidates": candidates}

    # fallback: broad Tavily web search for direct .pdf links
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return {"error": "No candidates from 1j1ju / BGG and TAVILY_API_KEY not set. "
                         "Provide a direct PDF URL or local path instead."}
    try:
        from tavily import TavilyClient
    except ImportError:
        return {"error": "tavily-python not installed. Run `uv sync`."}

    try:
        resp = TavilyClient(api_key=api_key).search(
            query=f"{game_name} rulebook pdf",
            max_results=10, search_depth="basic",
            include_answer=False, include_raw_content=False,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    from etl import onejour_api  # reuse the same lang heuristic
    candidates = []
    for r in resp.get("results", []):
        url = r.get("url") or ""
        if not url.lower().endswith(".pdf"):
            continue
        candidates.append({
            "title": r.get("title"),
            "url": url,
            "source": url.split("/")[2] if "://" in url else None,
            "lang": onejour_api.guess_lang(url),
        })
    return {"game": game_name, "count": len(candidates),
            "candidates": candidates[:8], "source": "web"}


def _fetch_pdf_bytes(url: str, *, max_bytes: int = 30 * 1024 * 1024) -> dict:
    """Download `url` and verify it's actually a PDF (magic bytes).

    Mirrors the browser-header / urllib pattern used by etl/syg_api to get past
    naive WAFs. Returns {ok, data: bytes} or {error}. Does NOT touch disk —
    callers store the bytes in the DB.
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/pdf,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} fetching {url}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    if len(data) > max_bytes:
        return {"error": f"file exceeds {max_bytes // (1024*1024)} MB cap"}
    # magic-byte check beats trusting Content-Type: rejects HTML error pages
    if not data[:5].startswith(b"%PDF"):
        return {"error": "downloaded content is not a PDF (no %PDF header) — "
                         "likely an HTML page, pick a direct .pdf link"}
    return {"ok": True, "data": data}


def download_rulebook(game_name: str, url: str | None = None,
                      bgg_filepageid: int | None = None, _source: str | None = None) -> dict:
    """Download a rulebook and index it under `game_name`. Provide EITHER `url`
    (a direct .pdf, e.g. a 1j1ju candidate) OR `bgg_filepageid` (a BGG Files
    candidate — fetched via headless browser + BGG login).

    One confirmed action = download + ingest (propose first, then on the user's
    OK). The PDF bytes go into the DB (no on-disk file). The game must exist.
    Use `ingest_rulebook` for a local file.
    """
    from . import rulebooks

    if bgg_filepageid:
        from etl.bgg_browser import fetch_one
        r = fetch_one(int(bgg_filepageid))
        if "error" in r:
            return {"error": r["error"], "bgg_filepageid": bgg_filepageid}
        result = rulebooks.ingest_bytes(game_name, r["data"], source=f"bgg:filepage/{bgg_filepageid}")
        result["bgg_filepageid"] = bgg_filepageid
        return result

    if not url:
        return {"error": "provide either url or bgg_filepageid"}
    dl = _fetch_pdf_bytes(url)
    if "error" in dl:
        return dl
    # source=url → re-download replaces the row via UNIQUE(game_id, source_path)
    result = rulebooks.ingest_bytes(game_name, dl["data"], source=url)
    result["url"] = url
    return result


# Words that trail a rulebook title on 1j1ju ("Dune: Imperium Rulebook",
# "Catane Junior Règle") — stripped before comparing the title to the game name.
_RULEBOOK_WORDS = {
    "rulebook", "rules", "rule", "rulesheet", "regle", "regles", "regole",
    "regolamento", "anleitung", "regeln", "spielregeln", "reglas", "regras",
    "instructions", "istruzioni",
}


def _rulebook_core(title: str) -> str:
    """Normalized title with trailing rulebook-words removed, for match scoring."""
    import re
    toks = [t for t in re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
            if t not in _RULEBOOK_WORDS]
    return " ".join(toks)


def _backfill_rulebook(game_id: int) -> dict:
    """Post-commit hook: auto-fetch a rulebook for a freshly added game IFF a
    confident match exists; otherwise REPORT what was searched so the chat flow
    can be honest ("ho cercato in automatico, nessun match affidabile") and
    surface the low-confidence candidates instead of discarding them in silence.

    Tries 1j1ju first (fast, no browser): a candidate whose title (minus
    rulebook-words) normalizes EXACTLY to the game name. If none, and the game
    has a bgg_id and the browser is enabled, tries BGG Files (headless browser)
    taking the top rulebook only when its score is high (EN/strong keyword).

    Returns a dict describing the outcome (consumed by `add_game`, read by the
    chat system prompt):
      {"status": "already_present"}                 — game already had a rulebook
      {"status": "fetched", "source": "<url>"}      — confident match, indexed
      {"status": "not_found", "candidates": [...]}  — searched, no reliable match;
                                                      candidates carry weak hits (may be [])
      {"status": "skipped", "reason": "..."}        — could not search (error/no game)
    Best-effort: every failure is swallowed so it never blocks `add_game`.
    Skips (already_present) if the game already has a rulebook.
    """
    try:
        from . import rulebooks
        from etl import onejour_api

        with get_conn() as conn:
            row = conn.execute("SELECT name, bgg_id FROM games WHERE id=?", (game_id,)).fetchone()
            if not row:
                return {"status": "skipped", "reason": "game not found"}
            name, bgg_id = row["name"], row["bgg_id"]
            already = conn.execute(
                "SELECT 1 FROM rulebooks WHERE game_id=? LIMIT 1", (game_id,)
            ).fetchone()
        if already:
            return {"status": "already_present"}

        # 1) 1j1ju strong exact-title match → auto-ingest
        target = rulebooks._norm_name(name)
        hits = onejour_api.search_rulebooks(name, limit=8)
        strong = [h for h in hits if _rulebook_core(h["title"]) == target]
        if strong:
            strong.sort(key=lambda h: (h["lang"] != "EN", len(h["title"])))
            dl = _fetch_pdf_bytes(strong[0]["url"])
            if "error" not in dl:
                res = rulebooks.ingest_bytes(name, dl["data"], source=strong[0]["url"])
                if res.get("ok"):
                    return {"status": "fetched", "source": strong[0]["url"]}

        # 2) BGG Files fallback (headless browser) — only a high-confidence top hit
        if bgg_id and os.environ.get("BGG_BROWSER_ENABLED", "1") != "0":
            from etl import bgg_files_api
            cands = bgg_files_api.find_rulebooks(int(bgg_id))
            if cands and cands[0]["score"] >= 2.0:  # EN + strong rulebook keyword
                from etl.bgg_browser import fetch_one
                r = fetch_one(cands[0]["filepageid"])
                if "data" in r:
                    res = rulebooks.ingest_bytes(
                        name, r["data"], source=f"bgg:filepage/{cands[0]['filepageid']}")
                    if res.get("ok"):
                        return {"status": "fetched",
                                "source": f"bgg:filepage/{cands[0]['filepageid']}"}

        # No confident match. Surface the weak hits (1j1ju titles that didn't
        # normalize-match + BGG Files metadata, no browser) so the chat can
        # propose-and-confirm instead of pretending nothing was searched.
        candidates: list[dict] = [
            {"title": h["title"], "url": h["url"], "source": "1j1ju.com", "lang": h["lang"]}
            for h in hits
        ]
        if bgg_id:
            candidates += _bgg_candidates(int(bgg_id))
        return {"status": "not_found", "candidates": candidates[:8]}
    except Exception as e:
        return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}


def web_search(query: str, include_domains: list[str] | None = None,
               max_results: int = 5, search_depth: str = "advanced",
               include_raw_content: bool = True,
               raw_content_chars: int = 6000) -> dict:
    """Tavily-backed web search. Returns top results with FULL-PAGE content.

    By default uses `search_depth='advanced'` and `include_raw_content=True`,
    so each result carries `raw_content` (the actual page text, markdown-
    cleaned) — not just the SERP-style snippet. This is what you want for
    BGG metadata or sleeveyourgames mm sizes; the snippet alone often hides
    the structured data the page is showing.

    Cost: advanced search = 2 Tavily credits/query; raw_content adds ~10×
    response tokens. Boardy budget (≤5 calls/turn × ≤10 results) stays
    well under the 1000 free credits/month — fine for personal use.

    Args:
        query: search string. Use ENGLISH game names.
        include_domains: restrict to specific sites. Default: trusted BGG /
            sleeve / publisher allowlist.
        max_results: 1-10. Default 5.
        search_depth: 'basic' (snippet only, 1 credit) or 'advanced'
            (deeper extraction + better raw_content, 2 credits). Default
            'advanced'.
        include_raw_content: if True, each result carries `raw_content`
            (full page text). Default True.
        raw_content_chars: cap per-result raw_content length to avoid
            blowing up the model context. Default 6000 chars (~1.5K tokens).
            Set to 0 for no cap.

    USAGE NOTES (the model should follow these):
    - Use the ENGLISH game name. International sites (sleeveyourgames.com, BGG)
      do not index Italian titles. "Ali Spiegate" → "Wingspan".
    - For sleeve-size lookups: include_domains=['sleeveyourgames.com'],
      query '<game> sleeves'. The advanced extraction usually captures the
      mm size table directly.
    - For BGG metadata: default allowlist + query '<game> boardgame BGG'.
      Read raw_content for designer/publisher/weight/rating, not just the
      title or snippet.
    - Cite sources INLINE as Markdown links pulled from the `url` field —
      no "Fonti:" sections (the post-processor mangles them).
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return {"error": "TAVILY_API_KEY not set in .env. "
                         "Get a free key at https://tavily.com (1000 searches/month free)."}
    try:
        from tavily import TavilyClient
    except ImportError:
        return {"error": "tavily-python not installed. Run `uv sync`."}

    domains = include_domains if include_domains is not None else DEFAULT_TRUSTED_DOMAINS
    k = max(1, min(int(max_results), 10))
    if search_depth not in ("basic", "advanced"):
        search_depth = "advanced"
    cap = max(0, int(raw_content_chars))

    try:
        resp = TavilyClient(api_key=api_key).search(
            query=query,
            include_domains=domains,
            max_results=k,
            search_depth=search_depth,
            include_answer=False,
            include_raw_content=include_raw_content,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    results = []
    for r in resp.get("results", []):
        item = {
            "title": r.get("title"),
            "url": r.get("url"),
            "content": r.get("content"),  # SERP-style snippet (kept for compat)
        }
        if include_raw_content:
            raw = r.get("raw_content") or ""
            if cap and len(raw) > cap:
                raw = raw[:cap] + f"\n…[truncated, {len(r['raw_content']) - cap} more chars]"
            item["raw_content"] = raw
        results.append(item)

    return {
        "query": query,
        "domains_used": domains,
        "search_depth": search_depth,
        "raw_content_included": include_raw_content,
        "results": results,
    }


def bgg_search(query: str, types: list[str] | None = None) -> dict:
    """Search the BoardGameGeek XML API for games matching a name.

    This is the DETERMINISTIC, authoritative lookup — it hits BGG's official
    XML API (bearer-authed, no scraping), so it returns clean structured data
    instead of the cookie-walled HTML that `web_search` gets from BGG public
    pages. Use it to resolve a name → the right `bgg_id`, then call
    `bgg_lookup(bgg_id)` for full metadata.

    Args:
        query: game name in ENGLISH (BGG indexes English/primary titles).
        types: optional filter, subset of
            ["boardgame", "boardgameexpansion", "boardgameaccessory"].
            Default: base games + expansions.

    Returns a list of candidates `{id, name, year, type}`, most recent first.
    Pick the id whose name+year match, then `bgg_lookup` it.
    """
    from etl import bgg_api
    allowed = ("boardgame", "boardgameexpansion", "boardgameaccessory")
    if types:
        t = tuple(x for x in types if x in allowed) or ("boardgame", "boardgameexpansion")
    else:
        t = ("boardgame", "boardgameexpansion")
    try:
        candidates = bgg_api.search(query, types=t)
    except bgg_api.BGGError as e:
        return {"error": str(e)}
    except Exception as e:  # network etc.
        return {"error": f"{type(e).__name__}: {e}"}
    return {"query": query, "count": len(candidates), "candidates": candidates[:25]}


def bgg_lookup(bgg_id: int) -> dict:
    """Fetch full structured metadata for a single game from the BGG XML API.

    DETERMINISTIC source — preferred over `web_search` for ALL BGG metadata
    when adding/enriching a game. One call returns everything the DB stores:
    year, players (min/max/best), duration, age, complexity weight+label,
    BGG rating, description, thumbnail/image URLs, designers, publishers,
    categories, mechanics. No cookie wall, no hallucinated designers.

    The returned dict's keys already match `add_game`/`update_game`/
    `add_to_wishlist` kwargs (bgg_id, year_published, players_min, …,
    designers, publishers, categories, mechanics) — so you can propose the
    table from this and pass the same fields straight into the write tool.

    Note: BGG's XML API does NOT expose card/sleeve sizes — for "Buste
    previste" you still need `web_search` on sleeveyourgames.com.

    Args:
        bgg_id: the BoardGameGeek game id (from `bgg_search` or the URL,
            e.g. boardgamegeek.com/boardgame/422126/... → 422126).
    """
    from etl import bgg_api
    try:
        data = bgg_api.fetch_thing(int(bgg_id))
    except bgg_api.BGGError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if data is None:
        return {"error": f"No BGG item found for id {bgg_id}."}
    # Drop the internal `_bgg_*` debug keys before handing to the model.
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _compare_sleeve_reqs(syg: list[dict], bgg: list[dict],
                         tol_mm: float = 1.0) -> dict:
    """Cross-check two base_requirements lists (card sizes + counts).

    Match lines by card size with a ±tol_mm tolerance (handles 63 vs 63.5 etc.),
    then compare counts on the sizes present in BOTH sources. Sizes that exist in
    only one source (e.g. BGG also lists a 'Goal board' 150×120 that sleeveyourgames
    omits) are reported as `only_*` but do NOT, by themselves, break agreement —
    those are usually non-card components or promos, not a real conflict.

    `agree` is True iff there's at least one common size AND no count mismatch on
    any common size.
    """
    def norm(reqs):
        return [(float(r["width_mm"]), float(r["height_mm"]), int(r.get("count") or 0))
                for r in (reqs or [])]
    A, B = norm(syg), norm(bgg)
    matched_b: set[int] = set()
    mismatches: list[dict] = []
    only_syg: list[dict] = []
    for (wa, ha, ca) in A:
        hit = None
        for i, (wb, hb, cb) in enumerate(B):
            if i in matched_b:
                continue
            if abs(wa - wb) <= tol_mm and abs(ha - hb) <= tol_mm:
                hit = (i, cb)
                break
        if hit is None:
            only_syg.append({"count": ca, "width_mm": wa, "height_mm": ha})
        else:
            i, cb = hit
            matched_b.add(i)
            if ca != cb:
                mismatches.append({"size": f"{wa:g}×{ha:g}",
                                   "syg_count": ca, "bgg_count": cb})
    only_bgg = [{"count": cb, "width_mm": wb, "height_mm": hb}
                for i, (wb, hb, cb) in enumerate(B) if i not in matched_b]
    agree = bool(matched_b) and not mismatches
    return {"agree": agree, "count_mismatches": mismatches,
            "only_sleeveyourgames": only_syg, "only_bgg": only_bgg}


def sleeve_lookup(name: str, bgg_id: int | None = None) -> dict:
    """Look up card-sleeve sizes + counts for a game (deterministic, no scraping).

    TWO deterministic sources, cross-checked:
      1. sleeveyourgames.com (name → id → card list) — sleeve-oriented, primary;
      2. BGG `cardsetsbygame` (by `bgg_id`) — covers new games sleeveyourgames
         lacks (e.g. Intarsia) AND is used to VERIFY source 1.

    Behaviour by what's available (always pass `bgg_id`!):
      • both sources hit → `requirements` = sleeveyourgames (primary); a
        `cross_check` block compares them. If they AGREE, `source` =
        "sleeveyourgames+bgg (concordi)". If they DISAGREE, `source` =
        "sleeveyourgames (⚠️ diverge da bgg)", a `warning` is set, and
        `cross_check.bgg_requirements` holds BGG's version — SHOW BOTH to the
        user and ask which to use before saving.
      • only one hits → that one's `requirements`, `source` names it.
      • neither hits → `found:false`. A miss does NOT mean "no cards"; it means
        sizes weren't found automatically — fall back to `web_search`
        (include_domains=["sleeveyourgames.com"]), then ASK THE USER for mm sizes.

    `requirements` is already shaped for `set_sleeve_requirements`
    ({count, width_mm, height_mm}), plus a per-expansion breakdown.

    Args:
        name: game name (English works best — sleeveyourgames indexes English).
        bgg_id: BGG id — disambiguates sleeveyourgames, unlocks the BGG source,
            and enables the cross-check. Strongly recommended.
    """
    from etl import syg_api
    syg_parsed = None
    syg_error = None
    try:
        syg_parsed = syg_api.lookup(name, bgg_id=bgg_id)
    except syg_api.SYGError as e:
        syg_error = str(e)
    except Exception as e:
        syg_error = f"{type(e).__name__}: {e}"

    bgg_parsed = None
    if bgg_id is not None:
        try:
            from etl import bgg_cards_api
            bgg_parsed = bgg_cards_api.lookup(bgg_id)
        except Exception:
            bgg_parsed = None

    # --- both sources answered → cross-check ---
    if syg_parsed is not None and bgg_parsed is not None:
        cmp = _compare_sleeve_reqs(syg_parsed["base_requirements"],
                                   bgg_parsed["base_requirements"])
        out = {
            "found": True,
            "name": syg_parsed["name"],
            "year": syg_parsed["year"],
            "bgg_id_match": syg_parsed["bgg_id"] == bgg_id if bgg_id is not None else None,
            "requirements": syg_parsed["base_requirements"],   # primary
            "expansions": syg_parsed["expansions"],
            "cross_check": {**cmp, "bgg_requirements": bgg_parsed["base_requirements"]},
        }
        if cmp["agree"]:
            out["source"] = "sleeveyourgames+bgg (concordi)"
        else:
            out["source"] = "sleeveyourgames (⚠️ diverge da bgg)"
            out["warning"] = ("Le due fonti NON concordano sulle buste. Mostra "
                              "all'utente ENTRAMBE le versioni (sleeveyourgames in "
                              "`requirements`, BGG in `cross_check.bgg_requirements`) "
                              "e chiedi quale usare PRIMA di salvare.")
        return out

    # --- only sleeveyourgames ---
    if syg_parsed is not None:
        return {
            "found": True,
            "name": syg_parsed["name"],
            "year": syg_parsed["year"],
            "bgg_id_match": syg_parsed["bgg_id"] == bgg_id if bgg_id is not None else None,
            "requirements": syg_parsed["base_requirements"],
            "expansions": syg_parsed["expansions"],
            "source": "sleeveyourgames.com",
        }

    # --- only BGG ---
    if bgg_parsed is not None:
        return {
            "found": True,
            "name": name,
            "year": None,
            "bgg_id_match": True,   # looked up directly by bgg_id
            "requirements": bgg_parsed["base_requirements"],
            "expansions": bgg_parsed["expansions"],
            "source": "boardgamegeek.com",
        }

    # --- neither ---
    out = {"found": False, "name": name,
           "hint": ("Sizes not found automatically (NOT the same as 'no cards'). "
                    + ("Pass bgg_id to enable the BGG source. " if bgg_id is None else "")
                    + "Try web_search include_domains=['sleeveyourgames.com']; "
                    "if empty, ask the user for the mm sizes.")}
    if syg_error:
        out["error"] = syg_error
    return out


def search_games_semantic(
    query: str,
    players: int | None = None,
    max_complexity_weight: float | None = None,
    min_complexity_weight: float | None = None,
    max_duration_min: int | None = None,
    sleeve_status: str | None = None,
    category_contains: str | None = None,
    mechanic_contains: str | None = None,
    status: str = "owned",
    k: int = 10,
) -> dict:
    """Semantic search over the user's games via description embeddings.

    Hybrid: structured SQL filters first (players / complexity / duration /
    sleeve status / category / mechanic), then cosine on the e5-base embedding
    of `games.description`. Games without an embedding are skipped — run
    `etl/embed_descriptions.py` if results look thin.

    `status` defaults to 'owned'; pass 'wishlist' to search wishlist only,
    or 'any' to include both (e.g. "i don't own this but I have it in the
    wishlist"). The `excluded` list mirrors the same scope.

    Returns `{count, items}` with `score` per item (cosine similarity, 0–1).
    """
    from . import games_semantic
    items = games_semantic.search_semantic(
        query,
        players=players,
        max_complexity_weight=max_complexity_weight,
        min_complexity_weight=min_complexity_weight,
        max_duration_min=max_duration_min,
        sleeve_status=sleeve_status,
        category_contains=category_contains,
        mechanic_contains=mechanic_contains,
        status=status,
        k=k,
    )
    excluded = games_semantic.excluded_from_search(status=status)
    return {
        "count": len(items),
        "items": items,
        "excluded_count": len(excluded),
        "excluded": excluded,
    }


# ===== Wishlist tools =====
# Wishlist lives in the same `games` table with `status='wishlist'`. This keeps
# the BGG enrichment + description embedding + audit log unified — promoting
# a wishlist item to owned is one column flip, no row migration, BGG data
# preserved by definition.

_PRIORITY_VALUES = {"high", "medium", "low"}


def add_to_wishlist(
    name: str,
    priority: str | None = None,
    notes_wishlist: str | None = None,
    target_price: float | None = None,
    # BGG enrichment (same shape as add_game) — enrich now, reuse on promote.
    bgg_id: int | None = None,
    year_published: int | None = None,
    players_min: int | None = None,
    players_max: int | None = None,
    players_best: str | None = None,
    duration_min: int | None = None,
    duration_min_min: int | None = None,
    duration_max_min: int | None = None,
    age_min: int | None = None,
    complexity_label: str | None = None,
    complexity_weight: float | None = None,
    bgg_rating: float | None = None,
    description: str | None = None,
    thumbnail_url: str | None = None,
    image_url: str | None = None,
    language: str | None = None,
    designers: list[str] | None = None,
    publishers: list[str] | None = None,
    categories: list[str] | None = None,
    mechanics: list[str] | None = None,
    _source: str | None = None,
) -> dict:
    """Insert a wishlist item. Fails if `name` already exists (owned or wishlist).

    Wishlist-only fields: `priority` (high/medium/low), `notes_wishlist`
    (free-text — who suggested it, where you saw it), `target_price` (EUR).
    All BGG enrichment fields are accepted so the same web_search → propose →
    confirm → add flow works as for owned games. Bridges (designers etc.)
    upsert into the shared dimension tables.
    """
    if priority is not None and priority not in _PRIORITY_VALUES:
        return {"error": f"priority must be one of {sorted(_PRIORITY_VALUES)}, got {priority!r}"}
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM games WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
            return {"error": f"Game {name!r} already exists. "
                             f"Use `update_wishlist` (or `add_game` / `update_game` for owned)."}
        cur = conn.execute(
            """INSERT INTO games(name, bgg_id, year_published, players_min, players_max,
                                 players_best, duration_min, duration_min_min, duration_max_min,
                                 age_min, complexity_label, complexity_weight, bgg_rating,
                                 description, thumbnail_url, image_url, language,
                                 sleeve_status, status, priority, notes_wishlist, target_price)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, bgg_id, year_published, players_min, players_max, players_best,
             duration_min, duration_min_min, duration_max_min, age_min,
             complexity_label, complexity_weight, bgg_rating, description,
             thumbnail_url, image_url, language,
             "unknown", "wishlist", priority, notes_wishlist, target_price),
        )
        gid = cur.lastrowid
        bridge_added: dict[str, list[str]] = {}
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            vals = locals().get(arg_name)
            _set_bridges(conn, gid, bridge, dim, fk, vals)
            if vals:
                bridge_added[arg_name] = vals
        snapshot = dict(conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        snapshot.update(bridge_added)
        audit.log_full(conn, table="games", row_id=gid, row_label=name,
                       action="insert", snapshot=snapshot,
                       source=f"{_source or 'unknown'} wishlist")
        conn.commit()
    # Auto-embed description so the wishlist participates in semantic search
    # (with status='any' or 'wishlist').
    if description:
        try:
            from . import games_semantic
            games_semantic.embed_one(gid)
        except Exception:
            pass
    # Backfill thumbnail/image via BGG XML API — wishlist items benefit even
    # more than owned games because the /wishlist grid relies on the cover
    # image for visual recall ("which Brass was it again?").
    _backfill_bgg_media(gid)
    _backfill_friendly_tags(gid)
    return {"ok": True, "id": gid, "name": name, "status": "wishlist",
            "priority": priority}


def list_wishlist(priority: str | None = None, limit: int = 100) -> dict:
    """List wishlist items as `{count, items}`. Optional `priority` filter.

    Each item carries name + BGG enrichment + wishlist-only fields
    (priority, notes_wishlist, target_price). Items are ordered by
    priority (high → low → no-priority) then alphabetically by name —
    so the dashboard surfaces "what to buy first".
    """
    if priority is not None and priority not in _PRIORITY_VALUES:
        return {"error": f"priority must be one of {sorted(_PRIORITY_VALUES)}, got {priority!r}"}
    # Manual priority sort: high=0, medium=1, low=2, NULL=3. SQLite has no
    # FIELD()-like function so we use a CASE expression.
    sql = ("SELECT g.* FROM games g WHERE g.status='wishlist'")
    params: list[Any] = []
    if priority:
        sql += " AND g.priority=?"; params.append(priority)
    sql += (" ORDER BY CASE g.priority "
            "WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, "
            "g.name LIMIT ?")
    params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        items = [_row_to_game_dict(r, conn, full=False) for r in rows]
    return {"count": len(items), "items": items}


def update_wishlist(
    name: str,
    priority: str | None = None,
    notes_wishlist: str | None = None,
    target_price: float | None = None,
    # Allow BGG enrichment patches too (e.g. after a later web_search).
    bgg_id: int | None = None,
    year_published: int | None = None,
    players_min: int | None = None,
    players_max: int | None = None,
    players_best: str | None = None,
    duration_min: int | None = None,
    duration_min_min: int | None = None,
    duration_max_min: int | None = None,
    age_min: int | None = None,
    complexity_label: str | None = None,
    complexity_weight: float | None = None,
    bgg_rating: float | None = None,
    description: str | None = None,
    thumbnail_url: str | None = None,
    image_url: str | None = None,
    language: str | None = None,
    designers: list[str] | None = None,
    publishers: list[str] | None = None,
    categories: list[str] | None = None,
    mechanics: list[str] | None = None,
    _source: str | None = None,
) -> dict:
    """Patch a wishlist row. Refuses if the row is already owned — use
    `update_game` for owned items so the audit trail stays semantically clear.
    """
    if priority is not None and priority not in _PRIORITY_VALUES:
        return {"error": f"priority must be one of {sorted(_PRIORITY_VALUES)}, got {priority!r}"}
    scalar_fields = {
        "priority": priority, "notes_wishlist": notes_wishlist,
        "target_price": target_price,
        "bgg_id": bgg_id, "year_published": year_published,
        "players_min": players_min, "players_max": players_max, "players_best": players_best,
        "duration_min": duration_min, "duration_min_min": duration_min_min,
        "duration_max_min": duration_max_min, "age_min": age_min,
        "complexity_label": complexity_label, "complexity_weight": complexity_weight,
        "bgg_rating": bgg_rating, "description": description,
        "thumbnail_url": thumbnail_url, "image_url": image_url, "language": language,
    }
    fields = {k: v for k, v in scalar_fields.items() if v is not None}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, status FROM games WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
        if not row:
            return {"error": f"Game {name!r} not found in wishlist."}
        if row["status"] != "wishlist":
            return {"error": f"{name!r} is owned (status={row['status']!r}); "
                             f"use `update_game` instead."}
        gid = row["id"]
        before = dict(conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            before[arg_name] = _game_dims(conn, gid, bridge, dim, fk)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE games SET {sets} WHERE id=?", [*fields.values(), gid])
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            _set_bridges(conn, gid, bridge, dim, fk, locals().get(arg_name))
        after = dict(conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            after[arg_name] = _game_dims(conn, gid, bridge, dim, fk)
        n_logged = audit.log_diff(conn, table="games", row_id=gid, row_label=name,
                                  before=before, after=after,
                                  source=f"{_source or 'unknown'} wishlist")
        conn.commit()
    if description is not None:
        try:
            from . import games_semantic
            games_semantic.embed_one(gid)
        except Exception:
            pass
    _backfill_bgg_media(gid)
    _backfill_friendly_tags(gid)
    return {"ok": True, "name": name, "updated": list(fields.keys()), "audit_rows": n_logged}


def mark_as_owned(name: str, sleeve_status: str = "unknown",
                  _source: str | None = None) -> dict:
    """Promote a wishlist row to an owned game.

    Single-column flip: BGG enrichment, description embedding, dimension
    bridges, even audit history are all preserved. Optional `sleeve_status`
    seeds the sleeve workflow (default 'unknown' — user will set it later
    or it'll be classified on next ETL re-import).
    """
    if sleeve_status not in ("sleeved", "to_sleeve", "na", "unknown"):
        return {"error": f"sleeve_status must be sleeved/to_sleeve/na/unknown, got {sleeve_status!r}"}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, status, sleeve_status FROM games WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
        if not row:
            return {"error": f"Game {name!r} not found."}
        if row["status"] == "owned":
            return {"error": f"{name!r} is already owned."}
        gid = row["id"]
        old_sleeve = row["sleeve_status"]
        conn.execute(
            "UPDATE games SET status='owned', sleeve_status=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (sleeve_status, gid),
        )
        audit.log_change(conn, table="games", row_id=gid, row_label=name,
                         action="update", field="status",
                         old="wishlist", new="owned",
                         source=f"{_source or 'unknown'} mark_as_owned")
        if old_sleeve != sleeve_status:
            audit.log_change(conn, table="games", row_id=gid, row_label=name,
                             action="update", field="sleeve_status",
                             old=old_sleeve, new=sleeve_status,
                             source=f"{_source or 'unknown'} mark_as_owned")
        conn.commit()
    return {"ok": True, "name": name, "status": "owned", "sleeve_status": sleeve_status}


def remove_from_wishlist(name: str, _source: str | None = None) -> dict:
    """Delete a wishlist row. Refuses if the row is owned (use `delete_game`)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM games WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
        if not row:
            return {"error": f"Game {name!r} not found."}
        if row["status"] != "wishlist":
            return {"error": f"{name!r} is owned (status={row['status']!r}); "
                             f"use `delete_game` if you really want to remove it."}
        gid = row["id"]
        snapshot = dict(row)
        for bridge, (dim, fk) in DIM_TABLES.items():
            arg_name = bridge.replace("game_", "")
            snapshot[arg_name] = _game_dims(conn, gid, bridge, dim, fk)
        audit.log_full(conn, table="games", row_id=gid, row_label=name,
                       action="delete", snapshot=snapshot,
                       source=f"{_source or 'unknown'} wishlist")
        conn.execute("DELETE FROM games WHERE id=?", (gid,))
        conn.commit()
    return {"ok": True, "deleted": name}


def list_dimension(table: str) -> dict:
    """List unique values of a dimension table with their game count. Returns `{count, items}`."""
    if table not in {"designers", "publishers", "categories", "mechanics"}:
        return {"error": f"unknown dimension {table!r}"}
    bridge = next(b for b, (d, _) in DIM_TABLES.items() if d == table)
    fk = DIM_TABLES[bridge][1]
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT d.name, COUNT(b.game_id) AS games
                FROM {table} d LEFT JOIN {bridge} b ON b.{fk}=d.id
                GROUP BY d.id ORDER BY games DESC, d.name"""
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"count": len(items), "items": items}


# ===== Tool schemas =====

# Common scalar field schema reused for add_game / update_game.
_SCALAR_FIELDS = {
    "bgg_id":            {"type": "integer", "description": "BoardGameGeek game ID."},
    "year_published":    {"type": "integer"},
    "players_min":       {"type": "integer"},
    "players_max":       {"type": "integer"},
    "players_best":      {"type": "string", "description": "Recommended player count, e.g. '3-4'."},
    "duration_min":      {"type": "integer", "description": "Representative duration in minutes."},
    "duration_min_min":  {"type": "integer", "description": "BGG min playtime."},
    "duration_max_min":  {"type": "integer", "description": "BGG max playtime."},
    "age_min":           {"type": "integer"},
    "complexity_label":  {"type": "string", "description": "One of: '1. Molto Semplice'..'5. Esperto'."},
    "complexity_weight": {"type": "number", "description": "BGG average weight 1.0–5.0."},
    "bgg_rating":        {"type": "number", "description": "BGG average rating."},
    "description":       {"type": "string"},
    "thumbnail_url":     {"type": "string"},
    "image_url":         {"type": "string"},
    "language":          {"type": "string"},
    "condition":         {"type": "string"},
    "notes":             {"type": "string"},
    "sleeve_status":     {"type": "string", "enum": ["sleeved", "to_sleeve", "na", "unknown"]},
}
_LIST_FIELDS = {
    "designers":  {"type": "array", "items": {"type": "string"}, "description": "Designer name(s)."},
    "publishers": {"type": "array", "items": {"type": "string"}},
    "categories": {"type": "array", "items": {"type": "string"}, "description": "BGG categories e.g. ['Strategy','Card Game']."},
    "mechanics":  {"type": "array", "items": {"type": "string"}, "description": "BGG mechanics e.g. ['Hand Management','Drafting']."},
}

TOOLS = [
    {
        "name": "list_games",
        "description": ("List games with optional filters (substring, AND-combined). "
                        "Up to 100 results. Returns `{count, items}` — ALWAYS use the "
                        "`count` field for any number you write (headers, summaries). "
                        "Never re-estimate by looking at items. "
                        "DEFAULTS TO OWNED GAMES ONLY. Pass `status='wishlist'` for the "
                        "wishlist, or `status='any'` to include both. The user's "
                        "collection (\"i miei giochi\") = owned, NOT wishlist."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains":       {"type": "string"},
                "players":             {"type": "integer"},
                "complexity_contains": {"type": "string"},
                "sleeve_status":       {"type": "string", "enum": ["sleeved", "to_sleeve", "na", "unknown"]},
                "designer_contains":   {"type": "string"},
                "publisher_contains":  {"type": "string"},
                "category_contains":   {"type": "string"},
                "mechanic_contains":   {"type": "string"},
                "status":              {"type": "string", "enum": ["owned", "wishlist", "any"],
                                         "description": "Defaults to 'owned'. 'wishlist' = wanted, not yet bought."},
            },
        },
    },
    {
        "name": "get_game",
        "description": "Full record for one game (designers/publishers/categories/mechanics + sleeve requirements).",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "add_game",
        "description": (
            "Insert a new game. Use AFTER user confirmation of the metadata you "
            "gathered via web_search. Pass any subset of fields — only `name` is required. "
            "List fields (designers, publishers, categories, mechanics) auto-create "
            "missing dimension rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, **_SCALAR_FIELDS, **_LIST_FIELDS},
            "required": ["name"],
        },
    },
    {
        "name": "update_game",
        "description": (
            "Patch fields on an existing game. Only non-null scalar fields are updated; "
            "list fields (designers/publishers/categories/mechanics) REPLACE the existing "
            "set when provided (omit to leave them unchanged)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, **_SCALAR_FIELDS, **_LIST_FIELDS},
            "required": ["name"],
        },
    },
    {
        "name": "delete_game",
        "description": "Permanently delete a game (cascades to dim links + sleeve requirements). ALWAYS confirm first.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "set_sleeve_requirements",
        "description": "Replace ALL sleeve requirements for a game. Use after user confirmation. Empty list clears.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "count":     {"type": "integer"},
                            "width_mm":  {"type": "number"},
                            "height_mm": {"type": "number"},
                            "note":      {"type": "string"},
                        },
                        "required": ["count", "width_mm", "height_mm"],
                    },
                },
            },
            "required": ["name", "requirements"],
        },
    },
    {
        "name": "sleeve_summary",
        "description": "Aggregate by size: needed across collection, owned in inventory, to_buy. Use for sleeve math. OWNED games only — wishlist items don't count toward 'to buy now'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "games_ready_to_sleeve",
        "description": (
            "Owned games with sleeve_status='to_sleeve' whose ENTIRE "
            "requirements are covered by current inventory. Returns "
            "{count_ready, ready, not_ready, has_contention, contention_note}. "
            "Use for 'cosa posso sleevare ora?' / 'quali giochi sono pronti da "
            "imbustare?'. Each `ready` item has the reqs broken down by size; "
            "each `not_ready` item carries a `missing` list with size + short_by. "
            "If `has_contention=True`, surface the `contention_note` to the user "
            "— it means two or more ready games would compete for the same "
            "stock and you can't actually sleeve them all in sequence."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "sleeve_summary_wishlist",
        "description": (
            "Per-size sleeve preview for WISHLIST games only — hypothetical "
            "demand for things you don't own yet. Use for 'se compro tutto in "
            "wishlist quante buste mi servono?' or 'quali misure mi mancherebbero "
            "per la wishlist?'. No to_buy/owned columns — those are owned-only math."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_inventory",
        "description": "List sleeve inventory rows.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_inventory",
        "description": "Upsert sleeve inventory (absolute count). E.g. user just bought 200 of 63.5×88.",
        "input_schema": {
            "type": "object",
            "properties": {
                "width_mm":    {"type": "number"},
                "height_mm":   {"type": "number"},
                "count_owned": {"type": "integer"},
                "brand":       {"type": "string"},
            },
            "required": ["width_mm", "height_mm", "count_owned"],
        },
    },
    {
        "name": "add_to_inventory",
        "description": (
            "Increment (or decrement, with negative delta) sleeve inventory by an "
            "amount. PREFERRED for 'I just bought N more' / 'used N for sleeving X' "
            "because the server does the arithmetic — never compute new_total = old + N "
            "yourself. Refuses to go negative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "width_mm":  {"type": "number"},
                "height_mm": {"type": "number"},
                "delta":     {"type": "integer", "description": "Positive to add, negative to remove."},
                "brand":     {"type": "string"},
                "note":      {"type": "string", "description": "Optional reason, e.g. 'bought at Asmodee', 'sleeved Wingspan'."},
            },
            "required": ["width_mm", "height_mm", "delta"],
        },
    },
    {
        "name": "recent_changes",
        "description": (
            "Read the audit log: who changed what and when. Use for questions like "
            "'cosa è cambiato di Wingspan?', 'quando ho aggiunto Concordia?', "
            "'mostrami le ultime modifiche'. Filter by `table` (games | sleeve_requirements | "
            "sleeve_inventory) or by `game_name` to narrow down."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit":     {"type": "integer", "description": "Max rows (default 20)."},
                "table":     {"type": "string", "enum": ["games", "sleeve_requirements", "sleeve_inventory"]},
                "game_name": {"type": "string", "description": "Show only changes for this game."},
            },
        },
    },
    {
        "name": "ingest_rulebook",
        "description": (
            "Parse a rulebook PDF, chunk it, and index it for semantic search "
            "under the named game. The PDF must already exist on disk at the given path. "
            "Use BEFORE any rules questions for that game."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_name": {"type": "string"},
                "pdf_path":  {"type": "string", "description": "Absolute or user-relative path to the PDF file."},
            },
            "required": ["game_name", "pdf_path"],
        },
    },
    {
        "name": "ask_rules",
        "description": (
            "Retrieve the top-k passages of a game's rulebook most relevant to a "
            "natural-language rules question. Returns excerpts with page numbers; "
            "you MUST synthesize the answer ONLY from these excerpts and cite the "
            "page (e.g. 'p. 12'). If the answer isn't in the excerpts, say so — "
            "DO NOT invent rules. Call `ingest_rulebook` first if not yet indexed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_name": {"type": "string"},
                "question":  {"type": "string"},
                "k":         {"type": "integer", "description": "How many chunks to retrieve (default 5)."},
            },
            "required": ["game_name", "question"],
        },
    },
    {
        "name": "list_rulebooks",
        "description": "List which games have an indexed rulebook (with chunk counts).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_rulebook",
        "description": (
            "Find downloadable rulebooks for a game from 1j1ju.com (candidates "
            "carry a direct `url`) and BoardGameGeek Files (candidates carry a "
            "`bgg_filepageid`). Pass `bgg_id` to include the richer/multilingual "
            "BGG results. Does NOT download — propose the best candidate in a table "
            "(file, source, language) and wait for confirmation, then call "
            "`download_rulebook` with the candidate's `url` OR `bgg_filepageid`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_name": {"type": "string", "description": "Prefer the ENGLISH title — the sites index English names."},
                "bgg_id":    {"type": "integer", "description": "BGG id — include it to also search BGG Files."},
            },
            "required": ["game_name"],
        },
    },
    {
        "name": "download_rulebook",
        "description": (
            "Download a rulebook and index it (download + ingest in one step). "
            "Provide EITHER `url` (a 1j1ju/web direct .pdf) OR `bgg_filepageid` (a "
            "BGG Files candidate — fetched via headless browser + BGG login). The game "
            "must already exist. Call ONLY after the user confirmed the candidate "
            "from `find_rulebook`. For a local file use `ingest_rulebook`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_name":  {"type": "string"},
                "url":            {"type": "string", "description": "Direct link to a .pdf (1j1ju/web candidate)."},
                "bgg_filepageid": {"type": "integer", "description": "BGG Files filepage id (bgg candidate)."},
            },
            "required": ["game_name"],
        },
    },
    {
        "name": "search_games_semantic",
        "description": (
            "Semantic 'vibe' search over the user's owned games using "
            "embeddings of `games.description`. Use for fuzzy intent queries "
            "like 'gioco da viaggio facile per colleghi', 'qualcosa di "
            "esplorazione spaziale', 'un party leggero', 'engine builder "
            "leggero'. NOT for exact filters — for those use `list_games` "
            "(name/designer/category match). NOT for rules questions (use "
            "`ask_rules`) or sleeve/inventory/audit queries. "
            "The `query` is a free-form Italian or English description of "
            "what the user wants. Combine with structured filters when they "
            "appear in the request: 'facile da imparare' → max_complexity_weight=2.5, "
            "'in 4' → players=4, '<60 min' → max_duration_min=60. "
            "Returns `{count, items, excluded_count, excluded}`. Each item "
            "has a `score` (cosine, 0–1; ≥0.78 strong match for e5-base, "
            "0.72–0.77 borderline). `excluded` lists games NOT scanned because "
            "they lack a description embedding, with the `skip_reason` from the "
            "last backfill attempt (e.g. ambiguous edition, no BGG match). When "
            "`excluded_count > 0` you MUST mention this to the user — say "
            "something like 'ti ricordo che N giochi non sono inclusi nella "
            "ricerca semantica perché senza descrizione' and list the names "
            "briefly. Never claim a result set is exhaustive when this is non-zero."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-form description of what the user wants."},
                "players": {"type": "integer"},
                "max_complexity_weight": {"type": "number", "description": "BGG weight upper bound (≤2.5 = light, ≤3.5 = medium)."},
                "min_complexity_weight": {"type": "number"},
                "max_duration_min": {"type": "integer"},
                "sleeve_status": {"type": "string", "enum": ["sleeved", "to_sleeve", "na", "unknown"]},
                "category_contains": {"type": "string", "description": "Substring on BGG category, e.g. 'Party Game'."},
                "mechanic_contains": {"type": "string"},
                "status": {"type": "string", "enum": ["owned", "wishlist", "any"],
                            "description": "Defaults to 'owned'. Use 'wishlist' for desire-list searches, 'any' for cross-discovery (\"non lo possiedo, ma è in wishlist\")."},
                "k": {"type": "integer", "description": "Top-k results (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_to_wishlist",
        "description": (
            "Add a game to the WISHLIST (wanted, not yet bought). UNLIKE add_game "
            "(owned, requires confirmation), wishlist adds DO NOT need a propose-"
            "table-then-wait ritual: the cost of an unwanted wishlist row is one "
            "user click on '✗ Rimuovi'. When the user says 'aggiungi X alla "
            "wishlist (con priorità Y)', just: (1) web_search BGG for metadata, "
            "(2) optionally web_search sleeveyourgames for sleeve sizes, (3) call "
            "this tool with the enriched fields, (4) if sleeve sizes were found, "
            "follow up with set_sleeve_requirements (sleeve_status='unknown' on "
            "wishlist allows it). Reply with ONE concise sentence: '✓ X aggiunto "
            "in wishlist (priorità Y). Buste previste: …'. NEVER print a "
            "confirmation table. Wishlist-only fields: `priority` (high/medium/"
            "low), `notes_wishlist`, `target_price` (EUR). Use add_game (NOT this) "
            "when the user already owns the game."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "notes_wishlist": {"type": "string"},
                "target_price": {"type": "number", "description": "Target price in EUR."},
                **_SCALAR_FIELDS,
                **_LIST_FIELDS,
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_wishlist",
        "description": (
            "List wishlist items (wanted, not yet bought) as `{count, items}`. "
            "Items are pre-sorted by priority (high → low → none). Filter by "
            "`priority` to focus on what to buy first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
            },
        },
    },
    {
        "name": "update_wishlist",
        "description": (
            "Patch fields on a wishlist row. Wishlist-only fields are `priority`, "
            "`notes_wishlist`, `target_price`. BGG enrichment fields work too "
            "(re-run web_search later, fill in description, etc.). Refuses if "
            "the row is already owned — use update_game then."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "notes_wishlist": {"type": "string"},
                "target_price": {"type": "number"},
                **_SCALAR_FIELDS,
                **_LIST_FIELDS,
            },
            "required": ["name"],
        },
    },
    {
        "name": "mark_as_owned",
        "description": (
            "Promote a wishlist row to owned. Single column flip — preserves all "
            "BGG enrichment, description embedding, dimension bridges. Use when "
            "the user says 'ho comprato X', 'è arrivato Y', 'finalmente preso Z'. "
            "ALWAYS confirm with the user first ('Confermi che hai comprato X?') "
            "before calling. Optional `sleeve_status` seeds the sleeve workflow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "sleeve_status": {"type": "string", "enum": ["sleeved", "to_sleeve", "na", "unknown"],
                                   "description": "Default 'unknown' if not specified."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "remove_from_wishlist",
        "description": (
            "Delete a wishlist row (not an owned game). Use when the user says "
            "'tolgo X dalla wishlist', 'non lo voglio più', 'cambio idea su Y'. "
            "ALWAYS confirm first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_dimension",
        "description": "Browse a dimension table (with game counts). Useful: 'show me all designers I own'.",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "enum": ["designers", "publishers", "categories", "mechanics"]}},
            "required": ["table"],
        },
    },
    {
        "name": "bgg_search",
        "description": (
            "Search the OFFICIAL BoardGameGeek XML API by game name. This is the "
            "deterministic, authoritative source — it returns clean structured "
            "candidates from BGG's API (bearer-authed), NOT cookie-walled scraped "
            "HTML like web_search gets. USE THIS FIRST when adding/enriching a game: "
            "bgg_search('<english name>') → pick the candidate whose name+year match "
            "→ bgg_lookup(id). Returns {id, name, year, type}, most recent first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Game name in English."},
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["boardgame", "boardgameexpansion", "boardgameaccessory"]},
                    "description": "Optional type filter. Default: base games + expansions.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "bgg_lookup",
        "description": (
            "Fetch FULL structured metadata for one game from the official BGG XML "
            "API by its numeric id. PREFERRED over web_search for ALL BGG metadata "
            "when adding/enriching a game — one call returns year, players "
            "(min/max/best), duration, age, complexity weight+label, rating, "
            "description, thumbnail/image, designers, publishers, categories, "
            "mechanics. Keys match add_game/add_to_wishlist kwargs, so feed them "
            "straight into the write tool after confirmation. Get the id from "
            "bgg_search or a BGG URL (.../boardgame/422126/... → 422126). NOTE: the "
            "metadata API does NOT return card/sleeve sizes — use sleeve_lookup "
            "(name + bgg_id) for 'Buste previste'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"bgg_id": {"type": "integer", "description": "BoardGameGeek game id."}},
            "required": ["bgg_id"],
        },
    },
    {
        "name": "sleeve_lookup",
        "description": (
            "Card-sleeve sizes + counts for a game (deterministic). USE THIS for "
            "'Buste previste' instead of web_search. Cross-checks TWO sources: "
            "sleeveyourgames.com (primary) AND BGG card sizes (cardsetsbygame) — so "
            "ALWAYS pass `bgg_id` to enable BGG + the cross-check. Returns "
            "`requirements` shaped for set_sleeve_requirements ({count, width_mm, "
            "height_mm}) + per-expansion breakdown; `source` says what answered. "
            "When both sources answer, a `cross_check` block compares them: if "
            "`cross_check.agree` is FALSE (or a `warning` is present), the two "
            "sources DISAGREE — show the user BOTH versions (`requirements` vs "
            "`cross_check.bgg_requirements`) and ask which to use before saving. "
            "If `found:false`, sizes weren't found automatically — this does NOT "
            "mean the game has no cards; fall back to web_search on "
            "sleeveyourgames.com, then ask the user for the mm sizes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Game name (English works best)."},
                "bgg_id": {"type": "integer", "description": "BGG id — disambiguates AND unlocks the BGG fallback. Strongly recommended."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for board-game info NOT in the local DB or rulebook index. "
            "Backed by Tavily; pre-filtered to trusted domains (BGG, sleevegeeks, "
            "sleeveyourgames, publishers, Wikipedia). "
            "Each result carries `raw_content` — the FULL page text, not just a SERP "
            "snippet — so you can read structured data (BGG weight/rating/players, "
            "mm sleeve sizes) directly. ALWAYS read `raw_content` for facts; use "
            "`content` (the snippet) only as a quick relevance check. "
            "USE FOR: sleeve sizes ('<game> sleeves' on sleeveyourgames.com), BGG "
            "metadata when adding/updating a game ('<game> boardgame BGG'), publisher "
            "errata. DO NOT USE FOR: rules questions (use ask_rules), inventory math, "
            "audit log queries, anything answerable from the local DB. "
            "ALWAYS use the ENGLISH game name — international sites don't index Italian "
            "titles (e.g. 'Wingspan', not 'Ali Spiegate'). "
            "For sleeve lookups specifically, pass include_domains=['sleeveyourgames.com']. "
            "Cite sources as inline Markdown links from the returned `url` field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query in English."},
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Restrict to these domains. Omit to use the default trusted "
                        "allowlist. Use ['sleeveyourgames.com'] for sleeve-size lookups."
                    ),
                },
                "max_results": {"type": "integer", "description": "1–10 (default 5)."},
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": (
                        "'advanced' (default) does deeper extraction and better "
                        "raw_content. 'basic' is snippet-only, half the cost. Stick "
                        "with the default unless the query is throwaway."
                    ),
                },
                "include_raw_content": {
                    "type": "boolean",
                    "description": (
                        "Include full-page text in each result. Default true — leave "
                        "it on; the snippet alone is rarely enough for BGG/sleeve data."
                    ),
                },
                "raw_content_chars": {
                    "type": "integer",
                    "description": (
                        "Cap per-result raw_content length (chars) to control context "
                        "size. Default 6000 (~1.5K tokens). 0 = no cap."
                    ),
                },
            },
            "required": ["query"],
        },
    },
]


TOOL_FUNCS = {
    "list_games":              list_games,
    "get_game":                get_game,
    "add_game":                add_game,
    "update_game":             update_game,
    "delete_game":             delete_game,
    "set_sleeve_requirements": set_sleeve_requirements,
    "sleeve_summary":          sleeve_summary,
    "sleeve_summary_wishlist": sleeve_summary_wishlist,
    "games_ready_to_sleeve":   games_ready_to_sleeve,
    "list_inventory":          list_inventory,
    "update_inventory":        update_inventory,
    "add_to_inventory":        add_to_inventory,
    "recent_changes":          recent_changes,
    "list_dimension":          list_dimension,
    "search_games_semantic":   search_games_semantic,
    "add_to_wishlist":         add_to_wishlist,
    "list_wishlist":           list_wishlist,
    "update_wishlist":         update_wishlist,
    "mark_as_owned":           mark_as_owned,
    "remove_from_wishlist":    remove_from_wishlist,
    "ingest_rulebook":         ingest_rulebook,
    "ask_rules":               ask_rules,
    "list_rulebooks":          list_rulebooks,
    "find_rulebook":           find_rulebook,
    "download_rulebook":       download_rulebook,
    "bgg_search":              bgg_search,
    "bgg_lookup":              bgg_lookup,
    "sleeve_lookup":           sleeve_lookup,
    "web_search":              web_search,
}


# Source of truth per il gating guest/owner. Tutti i tool che mutano stato
# (DB o filesystem) devono essere qui dentro — chat.py li filtra fuori dal
# registry quando il chiamante e' guest (non autenticato).
#
# Non basta affidarsi all'euristica "ha kwarg _source" (usata per il
# `_source` injection): `ingest_rulebook` scrive su `rulebooks`/`chunks` ma
# non dichiara `_source`. Mantenere l'insieme esplicito = piu' lavoro al
# momento di aggiungere un tool, ma zero rischi di buchi silenziosi.
WRITE_TOOLS: set[str] = {
    "add_game",
    "update_game",
    "delete_game",
    "set_sleeve_requirements",
    "update_inventory",
    "add_to_inventory",
    "add_to_wishlist",
    "update_wishlist",
    "mark_as_owned",
    "remove_from_wishlist",
    "ingest_rulebook",
    "download_rulebook",
}
