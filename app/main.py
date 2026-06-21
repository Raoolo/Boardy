"""FastAPI app: serves the chat UI, /chat endpoint, and conversation CRUD."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
import re
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth
from . import conversations as conv
from . import games_semantic as gs
from . import schema
from . import rulebooks as rb
from . import tools as tools_mod
from .auth import get_current_user, require_owner
from .chat import chat
from .db import get_conn

load_dotenv()
schema.migrate()
conv.migrate()

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"
LIBRARY = ROOT / "web" / "library.html"
SLEEVES = ROOT / "web" / "sleeves.html"
WISHLIST = ROOT / "web" / "wishlist.html"
LOGIN = ROOT / "web" / "login.html"
RULEBOOKS_DIR = ROOT / "rulebooks"
RULEBOOKS_DIR.mkdir(exist_ok=True)
STATIC_DIR = ROOT / "web" / "static"
STATIC_DIR.mkdir(exist_ok=True)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file"

app = FastAPI(title="Boardy")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    # Guest path: client invia l'intero history ogni turno (vive in sessionStorage).
    # Ignorato se conversation_id e' impostato (owner: history caricato dal DB).
    history: list[dict] | None = None


class ChatResponse(BaseModel):
    reply: str
    history: list[dict]
    # 0 per chat guest ephemera (non persistita); >0 per chat owner.
    conversation_id: int


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX)


# ── Auth ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(LOGIN)


@app.post("/auth/login")
def auth_login(req: LoginRequest, response: Response) -> dict:
    user = auth.authenticate(req.username, req.password)
    if user is None:
        # Stesso messaggio per user inesistente e password sbagliata
        # (evita user enumeration).
        raise HTTPException(401, "credenziali non valide")
    auth.set_session_cookie(response, user)
    return {"username": user["username"], "role": user["role"]}


@app.post("/auth/logout")
def auth_logout(response: Response) -> dict:
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
def auth_me(user: dict | None = Depends(get_current_user)) -> dict:
    """Discovery endpoint: il frontend lo chiama al boot per decidere se
    mostrare 'Guest · Accedi' o 'username · Esci'."""
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": user["username"], "role": user["role"]}


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(
    req: ChatRequest,
    user: dict | None = Depends(get_current_user),
) -> ChatResponse:
    if user is None:
        # Guest: chat ephemera lato client. NON creiamo conversation_id,
        # NON salviamo nulla. Il client invia l'intero history a ogni turno
        # (vive in sessionStorage del browser, sparisce al refresh).
        # Tool gating: chat() filtra fuori i write tools quando user=None.
        history = req.history or []
        reply, history = chat(req.message, history, conversation_id=None, user=None)
        return ChatResponse(reply=reply, history=history, conversation_id=0)

    # Owner: chat condivisa tra owner, persistita in `conversations`.
    conv_id = req.conversation_id
    if conv_id is None:
        conv_id = conv.create_conversation()
        history = []
    else:
        loaded = conv.get_conversation(conv_id)
        if loaded is None:
            raise HTTPException(404, f"conversation {conv_id} not found")
        history = loaded["history"]

    reply, history = chat(req.message, history, conversation_id=conv_id, user=user)
    conv.save_conversation(conv_id, history)
    return ChatResponse(reply=reply, history=history, conversation_id=conv_id)


# Conversations sidebar: solo owner. Guest non ha sidebar (chat ephemera client-side).
@app.get("/conversations")
def list_conversations(user: dict | None = Depends(get_current_user)) -> list[dict]:
    require_owner(user)
    return conv.list_conversations()


@app.get("/conversations/{conv_id}")
def get_conversation(conv_id: int, user: dict | None = Depends(get_current_user)) -> dict:
    require_owner(user)
    c = conv.get_conversation(conv_id)
    if c is None:
        raise HTTPException(404, f"conversation {conv_id} not found")
    return c


@app.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: dict | None = Depends(get_current_user)) -> dict:
    require_owner(user)
    conv.delete_conversation(conv_id)
    return {"ok": True}


@app.get("/games/names")
def games_names() -> list[str]:
    """Names only — for autocomplete dropdowns in the UI.

    Owned-only: rulebook upload is the main consumer, and wishlist items
    don't have rulebooks indexed (you don't own them yet).
    """
    with get_conn() as c:
        return [r["name"] for r in c.execute(
            "SELECT name FROM games WHERE status='owned' ORDER BY name"
        )]


@app.get("/library")
def library_page() -> FileResponse:
    return FileResponse(LIBRARY)


@app.get("/library/data")
def library_data() -> dict:
    """All OWNED games + the friendly_tags vocabulary for the filter dropdown.

    Wishlist items are intentionally excluded — they live on /wishlist.

    `friendly_tags` is the user-facing tag set (LLM-generated, fixed vocab in
    `app/friendly_tags.py`). Raw BGG `categories`/`mechanics` are still
    available per-game for the semantic-search SQL pre-filter on the server
    side, but the library UI now filters/displays on friendly_tags only.
    """
    import json
    from .friendly_tags import VOCABULARY
    with get_conn() as c:
        games = []
        rows = c.execute("""
            SELECT id, name, bgg_id, year_published,
                   players_min, players_max, players_best,
                   duration_min, duration_min_min, duration_max_min,
                   complexity_label, complexity_weight, bgg_rating,
                   thumbnail_url, sleeve_status, friendly_tags
            FROM games WHERE status='owned' ORDER BY name
        """).fetchall()
        for r in rows:
            d = dict(r)
            try:
                d["friendly_tags"] = json.loads(d["friendly_tags"]) if d["friendly_tags"] else []
            except (TypeError, json.JSONDecodeError):
                d["friendly_tags"] = []
            games.append(d)
    return {"games": games, "friendly_vocabulary": list(VOCABULARY)}


# ── Smart filter (natural-language → filter spec) ────────────────────────────
# Tier→weight conversions mirror weightTier() in web/library.html. Used to
# translate user-facing tiers (1–5) into SQL-friendly weight bounds when
# we forward the structured filters to the semantic search.
_TIER_TO_MAX_WEIGHT = {1: 2.0, 2: 2.5, 3: 3.5, 4: 4.2, 5: 5.0}
_TIER_TO_MIN_WEIGHT = {1: 1.0, 2: 2.0, 3: 2.5, 4: 3.5, 5: 4.2}

_FILTER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "apply_filter",
        "description": (
            "Apply a filter to the user's board game library. "
            "Call EXACTLY ONCE per query. "
            "Use semantic_query for similarity/vibe queries; otherwise leave empty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "semantic_query": {
                    "type": "string",
                    "description": (
                        "Natural-language query for semantic search over game descriptions. "
                        "Use ONLY when the user asks for similarity ('come Catan', 'simile a X', "
                        "'tipo Wingspan') or a vibe not captured by structured filters "
                        "('rilassante', 'epico', 'leggero ma profondo'). Leave empty for "
                        "purely structured queries like 'da 2 giocatori' or 'facili'."
                    ),
                },
                "players": {
                    "type": "integer",
                    "description": "Number of players. 1–5 = exact; 6 = '6 or more'.",
                },
                "complexity_max_tier": {
                    "type": "integer",
                    "description": (
                        "Max complexity tier the user wants. "
                        "1=Molto Semplice, 2=Semplice, 3=Medio, 4=Complesso, 5=Esperto."
                    ),
                },
                "complexity_min_tier": {"type": "integer"},
                "max_duration_min": {
                    "type": "integer",
                    "description": "Maximum game duration in minutes.",
                },
                "sleeve_status": {
                    "type": "string",
                    "enum": ["sleeved", "to_sleeve", "na", "unknown"],
                },
                "friendly_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User-friendly tags to filter by (OR semantics). MUST be exact strings from the fixed vocabulary given in the system prompt.",
                },
                "name_contains": {
                    "type": "string",
                    "description": "Substring to search in game names.",
                },
                "message": {
                    "type": "string",
                    "description": "Friendly Italian reply (1–2 short sentences) explaining what was filtered. Required.",
                },
                "reset": {
                    "type": "boolean",
                    "description": "Set to true if the user asked to clear all filters. All other fields will be ignored.",
                },
            },
            "required": ["message"],
        },
    },
}


def _build_filter_system_prompt(friendly_vocab: list[str]) -> str:
    """System prompt for the smart-filter LLM. Embeds the friendly_tags
    vocabulary so the model can only return tags in the fixed set."""
    return (
        "Sei l'assistente di filtro della libreria giochi di Boardy.\n"
        "L'utente ti chiede di filtrare la sua collezione e tu DEVI chiamare il tool `apply_filter` "
        "ESATTAMENTE UNA VOLTA con i parametri appropriati.\n\n"
        "REGOLE:\n"
        "- Se l'utente menziona somiglianza ('come Catan', 'simile a X', 'tipo Wingspan') o una vibe "
        "complessa non catturabile da un singolo tag ('epico', 'leggero ma profondo', 'tipo wargame ma rapido'), "
        "USA `semantic_query` con la frase originale dell'utente.\n"
        "- Per filtri strutturati ('da 2', 'facili', 'sotto i 30 minuti', 'da imbustare', "
        "'rilassanti', 'party', 'cooperativi'), riempi i campi strutturati e NON usare `semantic_query`.\n"
        "- Mappa la complessità: facile/leggero=tier 1-2, medio=3, complesso/pesante/esperto=4-5.\n"
        "- `friendly_tags` DEVE contenere solo voci esatte dal vocabolario qui sotto. "
        "Esempi di mapping: 'rilassanti'→`rilassante`, 'in coop'→`cooperativo`, 'da famiglia'→`per-famiglie`, "
        "'puzzle'→`puzzle`, 'tipo party'→`party`. "
        "Se la richiesta dell'utente non corrisponde ESATTAMENTE a un tag, lascia `friendly_tags` vuoto "
        "e (se sensato) usa `semantic_query`.\n"
        "- Se l'utente dice 'azzera', 'togli i filtri', 'pulisci', metti `reset: true`.\n"
        "- Risposta `message` SEMPRE in italiano, 1-2 frasi brevi, descrive cosa hai filtrato.\n"
        "- Se l'utente chiede qualcosa che non puoi tradurre in filtri (es. 'quanti giochi ho?'), "
        "rispondi nel `message` senza impostare alcun filtro.\n\n"
        f"Vocabolario `friendly_tags` (usa SOLO questi): {', '.join(friendly_vocab)}\n"
    )


class FilterRequest(BaseModel):
    query: str


@app.post("/library/filter")
def library_filter(req: FilterRequest) -> dict:
    """Natural-language → filter spec. Uses DeepSeek with forced tool use.

    Returns:
      filters: dict applied directly to the library UI dropdowns
      semantic_ids: list[int] | None — when set, the table shows ONLY these IDs
                    (in addition to the structured filters).
      message: friendly Italian reply for the chat bubble.
    """
    import json
    import os
    from openai import OpenAI
    from .friendly_tags import VOCABULARY as FRIENDLY_VOCAB

    if not req.query.strip():
        raise HTTPException(400, "empty query")

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise HTTPException(500, "DEEPSEEK_API_KEY not configured")

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    )

    resp = client.chat.completions.create(
        model=os.environ.get("LIBRARY_FILTER_MODEL", "deepseek-chat"),
        messages=[
            {"role": "system", "content": _build_filter_system_prompt(list(FRIENDLY_VOCAB))},
            {"role": "user", "content": req.query},
        ],
        tools=[_FILTER_TOOL_SCHEMA],
        # tool_choice forced — we ALWAYS want a structured response, never free text.
        tool_choice={"type": "function", "function": {"name": "apply_filter"}},
        temperature=0.0,
    )

    tcs = resp.choices[0].message.tool_calls or []
    if not tcs:
        raise HTTPException(502, "model did not call apply_filter")

    try:
        args = json.loads(tcs[0].function.arguments or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"malformed tool args: {e}")

    message = (args.get("message") or "").strip() or "Filtro applicato."

    # Reset path: short-circuit, return empty filters with explicit reset flag
    # so the frontend can distinguish "user asked to clear" from "no filter
    # extracted" (the latter shouldn't wipe existing filters).
    if args.get("reset"):
        return {"filters": {}, "semantic_ids": None, "message": message, "reset": True}

    # Sanitize friendly_tags against the fixed vocabulary — defense in depth
    # in case the model invents a tag despite the schema constraint.
    vocab_set = set(FRIENDLY_VOCAB)
    valid_tags = [t for t in (args.get("friendly_tags") or []) if t in vocab_set]

    filters = {
        "players": args.get("players"),
        "complexity_max_tier": args.get("complexity_max_tier"),
        "complexity_min_tier": args.get("complexity_min_tier"),
        "max_duration_min": args.get("max_duration_min"),
        "sleeve_status": args.get("sleeve_status"),
        "friendly_tags": valid_tags,
        "name_contains": args.get("name_contains"),
    }

    # Semantic path: the structured filters become SQL pre-filters on the
    # candidate set, then cosine ranks the survivors. We drop category/mechanic
    # pre-filtering (the UI no longer exposes them) — the description
    # embedding already captures genre, so this loses little.
    semantic_ids: list[int] | None = None
    sq = (args.get("semantic_query") or "").strip()
    if sq:
        max_w = (_TIER_TO_MAX_WEIGHT.get(filters["complexity_max_tier"])
                 if filters["complexity_max_tier"] else None)
        min_w = (_TIER_TO_MIN_WEIGHT.get(filters["complexity_min_tier"])
                 if filters["complexity_min_tier"] else None)
        # 'players=6' in the UI means "6+"; semantic search wants exact, so
        # only pass it through for 1–5.
        p = filters["players"] if filters["players"] in (1, 2, 3, 4, 5) else None
        results = gs.search_semantic(
            sq,
            players=p,
            max_complexity_weight=max_w,
            min_complexity_weight=min_w,
            max_duration_min=filters["max_duration_min"],
            sleeve_status=filters["sleeve_status"],
            k=20,
        )
        semantic_ids = [r["id"] for r in results]

    # Did the model actually pick anything up? If not (e.g. "quanti giochi ho?"),
    # the frontend should NOT wipe the user's existing manual filters.
    has_any = any(v not in (None, [], "") for v in filters.values()) or bool(semantic_ids)
    return {
        "filters": filters,
        "semantic_ids": semantic_ids,
        "message": message,
        "reset": False,
        "applied": has_any,
    }


@app.get("/sleeves")
def sleeves_page() -> FileResponse:
    return FileResponse(SLEEVES)


@app.get("/sleeves/data")
def sleeves_data() -> dict:
    """One-shot payload for the /sleeves dashboard.

    Returns:
      kpis        — top cards (totale possedute, da comprare, misure coperte).
      to_buy      — sleeve_summary rows where to_buy > 0, ordered by to_buy desc.
                    Each row has the canonical {needed, owned, to_buy, games}
                    plus a `size` label for display.
      summary_all — full sleeve_summary (including rows where to_buy=0), for
                    the "complete view" toggle on the page.
      inventory   — every sleeve_inventory row with width/height/brand/count.
    """
    summary = tools_mod.sleeve_summary()["items"]
    inventory = tools_mod.list_inventory()["items"]
    wishlist_preview = tools_mod.sleeve_summary_wishlist()["items"]
    ready_payload = tools_mod.games_ready_to_sleeve()
    owned_detail = tools_mod.sleeve_games_detail("owned")
    wish_detail = tools_mod.sleeve_games_detail("wishlist")

    def _label(w: float, h: float) -> str:
        # 63.5 → "63.5", 88 → "88" (drop trailing .0). Keeps display compact.
        def fmt(x: float) -> str:
            return str(int(x)) if x == int(x) else f"{x:g}"
        return f"{fmt(w)}×{fmt(h)}"

    summary_decorated = [
        {**r, "size": _label(r["width_mm"], r["height_mm"])}
        for r in summary
    ]

    inv_with_id = [
        {**r, "size": _label(r["width_mm"], r["height_mm"])}
        for r in inventory
    ]
    # Inventory owned-count per size — used both for the "mai comprata" tag
    # (owned == 0) and to fill the `owned` column of wishlist-only rows.
    inv_owned: dict[tuple, int] = {}
    for r in inventory:
        key = (r["width_mm"], r["height_mm"])
        inv_owned[key] = inv_owned.get(key, 0) + (r["count_owned"] or 0)

    def _games_detail(key: tuple) -> list[dict]:
        # Merge owned + wishlist contributors for the popup. Owned games first,
        # wishlist games tagged owned=False so the UI can mark "non lo possiedi".
        return (
            [{**g, "owned": True} for g in owned_detail.get(key, [])]
            + [{**g, "owned": False} for g in wish_detail.get(key, [])]
        )

    # Unified "Da comprare" table: keyed by size, includes a row whenever there
    # is owned shortfall (to_buy>0) OR future (wishlist) demand for that size.
    # This folds the old separate "Buste future" section in as a column.
    to_buy: list[dict] = []
    seen: set = set()
    for r in summary_decorated:
        key = (r["width_mm"], r["height_mm"])
        future = wish_detail.get(key, [])
        if r["to_buy"] > 0 or future:
            to_buy.append({
                **r,
                "future_count": len(future),
                "future_needed": sum(g["count"] for g in future),
                "games_detail": _games_detail(key),
            })
            seen.add(key)
    # Sizes wanted ONLY by wishlist games (no owned requirement at all): they
    # never appear in `summary`, so add them with to_buy=0 / needed=0.
    for key, future in wish_detail.items():
        if key in seen:
            continue
        w, h = key
        to_buy.append({
            "width_mm": w, "height_mm": h, "size": _label(w, h),
            "needed": 0, "owned": inv_owned.get(key, 0), "to_buy": 0,
            "games": "",
            "future_count": len(future),
            "future_needed": sum(g["count"] for g in future),
            "games_detail": _games_detail(key),
        })
    # Order: real shortfalls first (most to_buy on top), then future-only rows.
    to_buy.sort(key=lambda r: (r["to_buy"], r["future_needed"]), reverse=True)

    # Kept for backward-compat / the chat tool; the page no longer renders a
    # separate "Buste future" section (folded into `to_buy` above).
    inv_sizes = {k for k, v in inv_owned.items() if v > 0}
    wishlist_decorated = [
        {**r, "size": _label(r["width_mm"], r["height_mm"]),
         "already_covered": (r["width_mm"], r["height_mm"]) in inv_sizes}
        for r in wishlist_preview
    ]

    total_owned = sum(r["count_owned"] for r in inventory)
    total_to_buy = sum(r["to_buy"] for r in summary_decorated)
    sizes_covered = sum(1 for r in summary_decorated if r["to_buy"] == 0)

    return {
        "kpis": {
            "total_owned": total_owned,
            "total_to_buy": total_to_buy,
            "sizes_total": len(summary_decorated),
            "sizes_covered": sizes_covered,
            "wishlist_sleeves_total": sum(r["needed"] for r in wishlist_decorated),
            "ready_count": ready_payload["count_ready"],
        },
        "to_buy": to_buy,
        "summary_all": summary_decorated,
        "inventory": inv_with_id,
        "wishlist_preview": wishlist_decorated,
        "ready_to_sleeve": ready_payload,
    }


class InventoryDeltaRequest(BaseModel):
    width_mm: float
    height_mm: float
    delta: int
    brand: str | None = None
    note: str | None = None


@app.post("/sleeves/inventory/delta")
def sleeves_inventory_delta(
    req: InventoryDeltaRequest,
    user: dict | None = Depends(get_current_user),
) -> dict:
    """Apply a +N / -N delta to a sleeve inventory row. Audit-logged as `web:sleeves`.

    Wraps `tools.add_to_inventory` so the math runs server-side and a negative
    result raises an explicit error instead of silently going below zero.
    """
    require_owner(user)
    result = tools_mod.add_to_inventory(
        width_mm=req.width_mm, height_mm=req.height_mm, delta=req.delta,
        brand=req.brand, note=req.note, _source=f"web:sleeves/user:{user['username']}",
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


class InventoryUpsertRequest(BaseModel):
    width_mm: float
    height_mm: float
    count_owned: int
    brand: str | None = None


@app.post("/sleeves/inventory/upsert")
def sleeves_inventory_upsert(
    req: InventoryUpsertRequest,
    user: dict | None = Depends(get_current_user),
) -> dict:
    """Upsert an inventory row (absolute count). Used by the 'add new size' form."""
    require_owner(user)
    if req.count_owned < 0:
        raise HTTPException(400, "count_owned must be >= 0")
    return tools_mod.update_inventory(
        width_mm=req.width_mm, height_mm=req.height_mm,
        count_owned=req.count_owned, brand=req.brand,
        _source=f"web:sleeves/user:{user['username']}",
    )


# ── Wishlist ─────────────────────────────────────────────────────────────────

@app.get("/wishlist")
def wishlist_page() -> FileResponse:
    return FileResponse(WISHLIST)


@app.get("/wishlist/data")
def wishlist_data() -> dict:
    """All wishlist rows with dim arrays. Pre-sorted by priority."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT id, name, bgg_id, year_published,
                   players_min, players_max, players_best,
                   duration_min, duration_min_min, duration_max_min,
                   complexity_label, complexity_weight, bgg_rating,
                   thumbnail_url, description,
                   priority, notes_wishlist, target_price,
                   created_at
            FROM games WHERE status='wishlist'
            ORDER BY CASE priority
              WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
              name
        """).fetchall()
        items = []
        for r in rows:
            gid = r["id"]
            cats = [x["name"] for x in c.execute(
                "SELECT d.name FROM categories d JOIN game_categories b "
                "ON b.category_id=d.id WHERE b.game_id=? ORDER BY d.name",
                (gid,)).fetchall()]
            mechs = [x["name"] for x in c.execute(
                "SELECT d.name FROM mechanics d JOIN game_mechanics b "
                "ON b.mechanic_id=d.id WHERE b.game_id=? ORDER BY d.name",
                (gid,)).fetchall()]
            items.append({**dict(r), "categories": cats, "mechanics": mechs})
    counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for it in items:
        key = it["priority"] if it["priority"] in counts else "none"
        counts[key] += 1
    return {"items": items, "counts": counts}


class WishlistAddRequest(BaseModel):
    name: str
    priority: str | None = None
    notes_wishlist: str | None = None
    target_price: float | None = None


@app.post("/wishlist/add")
def wishlist_add(
    req: WishlistAddRequest,
    user: dict | None = Depends(get_current_user),
) -> dict:
    """Minimal add path from the page form. No BGG enrichment here — that flow
    goes through chat. The UI is for quick-capture; the chat is for the full
    confirm-then-add ritual."""
    require_owner(user)
    result = tools_mod.add_to_wishlist(
        name=req.name, priority=req.priority,
        notes_wishlist=req.notes_wishlist, target_price=req.target_price,
        _source=f"web:wishlist/user:{user['username']}",
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


class WishlistUpdateRequest(BaseModel):
    name: str
    priority: str | None = None
    notes_wishlist: str | None = None
    target_price: float | None = None


@app.post("/wishlist/update")
def wishlist_update(
    req: WishlistUpdateRequest,
    user: dict | None = Depends(get_current_user),
) -> dict:
    require_owner(user)
    result = tools_mod.update_wishlist(
        name=req.name, priority=req.priority,
        notes_wishlist=req.notes_wishlist, target_price=req.target_price,
        _source=f"web:wishlist/user:{user['username']}",
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


class WishlistBuyRequest(BaseModel):
    name: str
    sleeve_status: str = "unknown"


@app.post("/wishlist/buy")
def wishlist_buy(
    req: WishlistBuyRequest,
    user: dict | None = Depends(get_current_user),
) -> dict:
    """Promote wishlist → owned. Returns the updated row."""
    require_owner(user)
    result = tools_mod.mark_as_owned(
        name=req.name, sleeve_status=req.sleeve_status,
        _source=f"web:wishlist/user:{user['username']}",
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


class WishlistRemoveRequest(BaseModel):
    name: str


@app.post("/wishlist/remove")
def wishlist_remove(
    req: WishlistRemoveRequest,
    user: dict | None = Depends(get_current_user),
) -> dict:
    require_owner(user)
    result = tools_mod.remove_from_wishlist(
        name=req.name, _source=f"web:wishlist/user:{user['username']}",
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/rulebooks/upload")
async def upload_rulebook(
    game_name: str = Form(...),
    file: UploadFile = File(...),
    user: dict | None = Depends(get_current_user),
) -> dict:
    require_owner(user)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "expected a .pdf file")
    safe = _safe_filename(file.filename)
    contents = await file.read()
    # PDF bytes are stored in the DB (boardy.db is the single source of truth) —
    # we keep the original filename only as the dedup `source` handle.
    result = rb.ingest_bytes(game_name, contents, source=safe)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result
