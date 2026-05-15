"""User-friendly tag generation via LLM.

Background: i `categories`/`mechanics` BGG sono spesso opachi per chi non e'
"geek" — "Hand Management", "Set Collection", "Variable Player Powers",
"Eurogame", "Card Drafting" non comunicano niente al casual player. Questo
modulo genera 3-5 tag in italiano leggibile presi da un VOCABOLARIO FISSO
(see VOCABULARY), partendo da: nome, description, BGG categories+mechanics,
duration_min, complexity_weight, players_min/max.

Perche' vocabolario fisso e non aperto: filtri dropdown multi-select richiedono
ricercabilita' deterministica. Con un vocabolario aperto, sinonimi
("rilassante"/"rilassato"/"chill") spezzano la query. La perdita di sfumatura
(20 tag fissi vs N aperti) e' un trade-off accettabile per un'app con ~100 giochi.

Stesso pattern di `app/conversations.py:_generate_title_llm`: hardcodato a
DeepSeek (cheap, indipendente da LLM_PROVIDER), T=0, best-effort (None on any
error). Chiamato:
- post-commit da `add_game`/`update_game`/`add_to_wishlist`/`update_wishlist`
  in `app/tools.py` (auto-regen su scrittura BGG-enriched);
- batch via `etl/generate_friendly_tags.py` (backfill iniziale + --force).
"""
from __future__ import annotations

import json
import os

from .db import get_conn


# Vocabolario fisso, lower-case con hyphen per consistenza dropdown.
# Italiano perche' single-user app + owner italiano. Espandere richiede:
# (1) aggiungere qui, (2) rigenerare tag per tutto il catalogo (`--force`).
VOCABULARY: tuple[str, ...] = (
    # Mood / atmosfera
    "rilassante",         # bassa tensione, partite "chill"
    "strategico",         # decisioni con orizzonte lungo, info-rich
    "tattico",            # decisioni reattive, orizzonte breve
    "caotico",            # alta varianza, swingy, dadi/carte dominanti
    "tematico",           # tema forte, immersione narrativa
    "astratto",           # nessun tema / tema irrilevante
    "narrativo",          # storytelling, scelte ramificate
    "party",              # gruppo grande, energia alta, no analisi
    "puzzle",             # risoluzione di problemi logici
    "bluff",              # bluff / info nascosta
    "negoziazione",       # accordi/scambi tra giocatori
    # Modalita'
    "competitivo",        # PvP standard
    "cooperativo",        # tutti contro il gioco
    "solitario",          # supporta bene 1 giocatore
    # Pubblico
    "per-famiglie",       # family-friendly trasversale
    "per-bambini",        # specificamente per bambini
    # Skill dominante
    "deduzione",          # inferenza logica (Cluedo-like)
    "memoria",            # memorizzazione
    "gestione-risorse",   # economia / engine building
)
_VOCAB_SET = frozenset(VOCABULARY)


_SYSTEM_PROMPT = (
    "Sei un classificatore di giochi da tavolo. Ricevi i dati di UN gioco e "
    "scegli 3-5 tag che descrivono l'esperienza di gioco in modo COMPRENSIBILE "
    "a un non-geek (no jargon BGG tipo 'Hand Management' o 'Variable Player Powers').\n\n"
    "REGOLE STRINGENTI:\n"
    "1. Scegli i tag SOLO da questo vocabolario fisso (separati da virgola):\n"
    f"   {', '.join(VOCABULARY)}\n"
    "2. Restituisci ESCLUSIVAMENTE un JSON nel formato: {\"tags\": [\"tag1\", \"tag2\", ...]}\n"
    "3. Min 3, max 5 tag. Niente duplicati. Niente tag fuori vocabolario.\n"
    "4. Pensa al feeling del gioco, non a meccaniche tecniche: un eurogame "
    "denso = `strategico` + `gestione-risorse`, non `tattico`. Un party game "
    "rapido = `party` + `caotico`, non `strategico`.\n"
    "5. Se il gioco e' chiaramente per bambini piccoli, usa `per-bambini` "
    "(non `per-famiglie`, che e' trasversale).\n"
    "6. Niente prosa, niente preambolo, niente markdown. Solo JSON."
)


def _client():
    """Lazy-import DeepSeek client. Returns None if key is missing."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    from openai import OpenAI
    return OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    )


def _build_user_payload(game: dict, categories: list[str], mechanics: list[str]) -> str:
    """Compact, structured payload — keeps prompt tiny and predictable."""
    parts = [f"Nome: {game['name']}"]
    if game.get("description"):
        # Cap at 800 chars: BGG descriptions can be 3kB+, and the first paragraph
        # almost always carries the genre signal we care about.
        parts.append(f"Descrizione: {game['description'][:800]}")
    if game.get("complexity_weight") is not None:
        parts.append(f"Complessita' BGG (1-5): {game['complexity_weight']:.2f}")
    if game.get("duration_min") is not None:
        parts.append(f"Durata media: {game['duration_min']} min")
    if game.get("players_min") is not None and game.get("players_max") is not None:
        parts.append(f"Giocatori: {game['players_min']}-{game['players_max']}")
    if categories:
        parts.append(f"Categorie BGG (grezze, riferimento): {', '.join(categories)}")
    if mechanics:
        parts.append(f"Meccaniche BGG (grezze, riferimento): {', '.join(mechanics)}")
    return "\n".join(parts)


def _parse_and_validate(raw: str) -> list[str] | None:
    """Parse JSON, keep only tags in VOCABULARY, dedupe, cap at 5."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        return None
    seen: set[str] = set()
    valid: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            continue
        norm = t.strip().lower()
        if norm in _VOCAB_SET and norm not in seen:
            seen.add(norm)
            valid.append(norm)
            if len(valid) >= 5:
                break
    # Require at least 1 valid tag — empty result = treat as failure so the
    # caller can fall back (or leave the field NULL for re-try later).
    return valid if valid else None


def generate_for_game(game_id: int) -> list[str] | None:
    """Look up the game, call the LLM, return validated tags or None.

    Returns None on any failure (missing key, network error, invalid JSON,
    no valid tags). The caller is responsible for persisting; this function
    is pure read/compute.
    """
    client = _client()
    if client is None:
        return None
    try:
        with get_conn() as c:
            row = c.execute(
                """SELECT id, name, description, complexity_weight, duration_min,
                          players_min, players_max
                   FROM games WHERE id=?""",
                (game_id,),
            ).fetchone()
            if not row:
                return None
            game = dict(row)
            categories = [r["name"] for r in c.execute(
                "SELECT d.name FROM categories d JOIN game_categories b "
                "ON b.category_id=d.id WHERE b.game_id=? ORDER BY d.name",
                (game_id,)).fetchall()]
            mechanics = [r["name"] for r in c.execute(
                "SELECT d.name FROM mechanics d JOIN game_mechanics b "
                "ON b.mechanic_id=d.id WHERE b.game_id=? ORDER BY d.name",
                (game_id,)).fetchall()]
        # Need at minimum the name; description is the strongest signal but
        # we'll still try without it (BGG cats/mechs alone can be enough).
        payload = _build_user_payload(game, categories, mechanics)
        resp = client.chat.completions.create(
            model="deepseek-chat",
            temperature=0,
            max_tokens=80,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
        return _parse_and_validate(resp.choices[0].message.content or "")
    except Exception:
        return None


def persist(game_id: int, tags: list[str]) -> None:
    """Write tags as JSON to the row. Caller decided they're valid."""
    with get_conn() as c:
        c.execute(
            "UPDATE games SET friendly_tags=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(tags, ensure_ascii=False), game_id),
        )
        c.commit()


def backfill_one(game_id: int) -> list[str] | None:
    """Generate + persist for one game. Returns the tags or None on failure."""
    tags = generate_for_game(game_id)
    if tags is None:
        return None
    persist(game_id, tags)
    return tags
