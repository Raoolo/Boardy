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
    limit: int = 100,
) -> dict:
    """Return matching games as `{count, items}`. Filters AND-combined; *_contains are case-insensitive substring.

    The `count` envelope is intentional: LLMs are bad at counting list elements
    in attention. Returning the integer pre-computed lets the model transcribe
    the literal instead of estimating, which has caused header/list mismatches
    in past sessions (LEARNINGS 2026-04-29 PM).
    """
    sql = "SELECT DISTINCT g.* FROM games g"
    params: list[Any] = []
    where: list[str] = []

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
    """Per sleeve size: needed (sum across games), owned (inventory), to_buy. Returns `{count, items}`."""
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
                        "Never re-estimate by looking at items."),
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
        "description": "Aggregate by size: needed across collection, owned in inventory, to_buy. Use for sleeve math.",
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
    "list_inventory":          list_inventory,
    "update_inventory":        update_inventory,
    "add_to_inventory":        add_to_inventory,
    "recent_changes":          recent_changes,
    "list_dimension":          list_dimension,
    "ingest_rulebook":         ingest_rulebook,
    "ask_rules":               ask_rules,
    "list_rulebooks":          list_rulebooks,
    "web_search":              web_search,
}
