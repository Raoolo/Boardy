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
    return {"ok": True, "id": gid, "name": name}


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
                     f"Use `ingest_rulebook` first with a PDF path."
        }
    return {"game": game_name, "question": question, "chunks": chunks}


def list_rulebooks() -> dict:
    """List all ingested rulebooks (one row per game+source). Returns `{count, items}`."""
    from . import rulebooks
    items = rulebooks.list_rulebooks()
    return {"count": len(items), "items": items}


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
}
