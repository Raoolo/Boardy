"""Semantic search over `games.description`.

Hybrid search pattern:
1. SQL filters (players, complexity weight, duration, sleeve_status) shrink
   the candidate set.
2. Cosine similarity on the description embedding ranks survivors.

Re-uses the same e5-base model loaded by `app/rulebooks.py` (lazy module-global
in that file). E5 expects "passage: " for indexed text and "query: " for the
search string — same convention as the rulebook RAG.

Storage: `games.description_embedding` (float32 raw bytes, 768 dims = 3072 B
per row) + `games.description_hash` (SHA1 of the description text used to
build the embedding). The hash lets `reindex_all()` skip rows whose
description hasn't changed since last embed — important for the
`update_game` auto-embed hook so we don't re-encode 56 games on a sleeve
update.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .db import get_conn

# Reuse the rulebooks model so we don't load 280MB twice. The lazy loader
# inside rulebooks.py is the single source of truth.
from .rulebooks import _model_lazy, EMBED_MODEL_NAME  # noqa: F401  (re-exported)


def _hash_text(text: str) -> str:
    """Stable SHA1 of the description used to detect "needs re-embed"."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _embed_passages(texts: list[str]) -> np.ndarray:
    """E5 'passage: ' prefix for documents."""
    prefixed = [f"passage: {t}" for t in texts]
    arr = _model_lazy().encode(
        prefixed, normalize_embeddings=True, convert_to_numpy=True,
        show_progress_bar=False,
    )
    return arr.astype(np.float32, copy=False)


def _embed_query(text: str) -> np.ndarray:
    arr = _model_lazy().encode(
        [f"query: {text}"], normalize_embeddings=True, convert_to_numpy=True,
        show_progress_bar=False,
    )
    return arr[0].astype(np.float32, copy=False)


# --- public API ---------------------------------------------------------------

def embed_one(game_id: int) -> bool:
    """Embed the description of a single game. Returns True if (re-)embedded,
    False if skipped (no description, or hash unchanged).

    Designed to be called from `add_game`/`update_game` after the commit so
    a sleeve-only update doesn't trigger an embed pass.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT description, description_hash FROM games WHERE id=?",
            (game_id,),
        ).fetchone()
        if not row or not row["description"]:
            return False
        h = _hash_text(row["description"])
        if h == row["description_hash"]:
            return False
        emb = _embed_passages([row["description"]])[0]
        conn.execute(
            "UPDATE games SET description_embedding=?, description_hash=? WHERE id=?",
            (emb.tobytes(), h, game_id),
        )
        conn.commit()
        return True


def reindex_all(force: bool = False, batch_size: int = 32) -> dict:
    """One-shot: embed every game whose description hash mismatches.

    `force=True` re-embeds every game with a non-null description (use after
    swapping the embedding model). Batches the encode calls so the e5
    forward pass is amortized.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, description, description_hash FROM games "
            "WHERE description IS NOT NULL AND description != ''"
        ).fetchall()

    todo = []
    for r in rows:
        h = _hash_text(r["description"])
        if force or h != r["description_hash"]:
            todo.append((r["id"], r["name"], r["description"], h))

    if not todo:
        return {"ok": True, "total": len(rows), "embedded": 0, "skipped": len(rows)}

    embedded = 0
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        embs = _embed_passages([t[2] for t in batch])
        with get_conn() as conn:
            for (gid, _name, _desc, h), emb in zip(batch, embs):
                conn.execute(
                    "UPDATE games SET description_embedding=?, description_hash=? WHERE id=?",
                    (emb.tobytes(), h, gid),
                )
            conn.commit()
        embedded += len(batch)

    return {
        "ok": True,
        "total": len(rows),
        "embedded": embedded,
        "skipped": len(rows) - embedded,
    }


def search_semantic(
    query: str,
    *,
    players: int | None = None,
    max_complexity_weight: float | None = None,
    min_complexity_weight: float | None = None,
    max_duration_min: int | None = None,
    sleeve_status: str | None = None,
    category_contains: str | None = None,
    mechanic_contains: str | None = None,
    status: str = "owned",
    k: int = 10,
) -> list[dict]:
    """Hybrid search: SQL filters + cosine on description embedding.

    Returns rows ordered by cosine descending. Only games with a non-null
    embedding can be ranked — games missing an embedding are excluded
    (call `reindex_all()` to fill them).

    Filter SQL is intentionally narrow: just the dimensions a "vibe" query
    most often combines with — player count, complexity, duration, sleeve
    status, optional category/mechanic substring. If you need richer
    structured filtering (designer/publisher), pre-filter via `list_games`
    and pass the names you care about back through this function — or
    extend this signature.
    """
    sql = (
        "SELECT DISTINCT g.id, g.name, g.players_min, g.players_max, "
        "g.duration_min, g.complexity_label, g.complexity_weight, "
        "g.bgg_rating, g.sleeve_status, g.description, g.description_embedding "
        "FROM games g"
    )
    params: list[Any] = []
    where: list[str] = ["g.description_embedding IS NOT NULL"]
    if status and status != "any":
        where.append("g.status = ?")
        params.append(status)

    if category_contains:
        sql += (" JOIN game_categories bgc ON bgc.game_id=g.id "
                "JOIN categories tc ON tc.id=bgc.category_id")
        where.append("LOWER(tc.name) LIKE ?")
        params.append(f"%{category_contains.lower()}%")
    if mechanic_contains:
        sql += (" JOIN game_mechanics bgm ON bgm.game_id=g.id "
                "JOIN mechanics tm ON tm.id=bgm.mechanic_id")
        where.append("LOWER(tm.name) LIKE ?")
        params.append(f"%{mechanic_contains.lower()}%")
    if players is not None:
        where.append("g.players_min <= ? AND g.players_max >= ?")
        params.extend([players, players])
    if max_complexity_weight is not None:
        where.append("(g.complexity_weight IS NULL OR g.complexity_weight <= ?)")
        params.append(float(max_complexity_weight))
    if min_complexity_weight is not None:
        where.append("g.complexity_weight >= ?")
        params.append(float(min_complexity_weight))
    if max_duration_min is not None:
        where.append("(g.duration_min IS NULL OR g.duration_min <= ?)")
        params.append(int(max_duration_min))
    if sleeve_status is not None:
        where.append("g.sleeve_status = ?")
        params.append(sleeve_status)

    sql += " WHERE " + " AND ".join(where)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    qv = _embed_query(query) if rows else None
    scored: list[tuple[float, dict]] = []
    for r in rows:
        vec = np.frombuffer(r["description_embedding"], dtype=np.float32)
        # both query and docs L2-normalized → dot == cosine
        score = float(qv @ vec)
        d = {k_: r[k_] for k_ in r.keys() if k_ != "description_embedding"}
        # Trim description for the LLM payload — full text isn't needed in results.
        if d.get("description") and len(d["description"]) > 200:
            d["description"] = d["description"][:200] + "…"
        d["score"] = round(score, 4)
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:k]]


def excluded_from_search(status: str = "owned") -> list[dict]:
    """Games whose description_embedding is NULL — they are NEVER returned by
    `search_semantic`, so the model must mention them explicitly when the
    user asks about the whole collection.

    Defaults to `status='owned'` to match `search_semantic`'s default scope:
    the "ti ricordo che N giochi non sono inclusi" warning should reflect
    the same universe the search actually scanned.

    Returns one dict per excluded game with the recorded `skip_reason` (or
    None if never attempted). Used by the `search_games_semantic` tool to
    surface a coverage warning alongside the ranked results.
    """
    sql = ("SELECT id, name, description_skip_reason "
           "FROM games WHERE description_embedding IS NULL")
    params: list[Any] = []
    if status and status != "any":
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY name"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"name": r["name"], "skip_reason": r["description_skip_reason"]}
        for r in rows
    ]
