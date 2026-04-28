"""Claude tool-use chat loop for Boardy."""
from __future__ import annotations

import functools
import inspect
import json
import os
from typing import Any

from anthropic import Anthropic

from .tools import TOOL_FUNCS, TOOLS


@functools.cache
def _accepts_source(name: str) -> bool:
    """True if a tool function declares a `_source` kwarg (write tools do, readers don't)."""
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return False
    return "_source" in inspect.signature(fn).parameters

# Trusted board-game / sleeve sources. Add domains here as you discover gaps.
ALLOWED_DOMAINS = [
    "boardgamegeek.com",
    "geekdo-images.com",
    "en.wikipedia.org",
    "sleevegeeks.com",
    "sleeveyourgames.com",
    "mayday-games.com",
    "dragonshield.com",
    "ultrapro.com",
    "fantasyflightgames.com",
    "asmodee.com",
    "cmon.com",
    "capstone-games.com",
    "feuerland-spiele.de",
    "renegadegamestudios.com",
    "stonemaiergames.com",
    "leveluptutorialsboardgames.com",
]

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
    "allowed_domains": ALLOWED_DOMAINS,
}

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are Boardy, a personal assistant for Raulo's board-game collection.

The DB follows a star-schema:
- `games` (dimension): name, bgg_id, year_published, players_min/max/best, duration_min,
  age_min, complexity_label & complexity_weight, bgg_rating, description, thumbnail_url,
  language, condition, notes, sleeve_status, sleeve_raw, created/updated_at.
- Outrigger dimensions: `designers`, `publishers`, `categories`, `mechanics` —
  each linked many-to-many via `game_designers`, `game_publishers`, `game_categories`,
  `game_mechanics`. Use list-typed args (`designers=[...]`) on add_game/update_game.
- Facts: `sleeve_requirements` (game × size × count), `sleeve_inventory` (size × owned).
- Audit: every write goes through `changes` (auto-logged). Use `recent_changes` to
  read history; never invent a "when did I add X?" answer.

Rules:
- ALWAYS answer using the tools — never invent game names, counts, or sizes.
- Match the user's language: reply in Italian if they write in Italian, English otherwise.
- For "how many sleeves to buy", call `sleeve_summary` and report by size.
- When a sleeve size is given as e.g. "63.5x88" or "63,5x88", treat the comma as a
  decimal separator. The DB stores millimetres.
- Common sleeve size slang: "Standard American" = 63.5×88, "Mini American" = 41×63 or 44×68,
  "Catan" = 57×87, "Euro" = 59×92, "Tarot" = 70×120. Confirm if ambiguous.

Inventory updates (CRITICAL — never do the math yourself):
- "Ho comprato N buste di SIZE" / "ne ho usate N per SLEEVING" → call
  `add_to_inventory(width_mm, height_mm, delta=±N, brand?, note?)`. The server
  computes new_total = old + delta. Use a NEGATIVE delta when sleeves are consumed.
- Use `update_inventory` (absolute count) ONLY when the user explicitly says "ho
  esattamente N in totale" or to correct a previously wrong absolute count.
- After the call, report previous_count, delta, and new count_owned from the
  result so the user can sanity-check.

History questions:
- "Quando ho aggiunto X?" / "Cosa è cambiato di Y?" / "Ultime modifiche?" →
  call `recent_changes(limit=20, game_name?, table?)`. Do NOT answer from
  conversation memory or guesswork — the audit log is authoritative.

Adding or enriching a game (IMPORTANT FLOW):
- When the user says they added a game ("ho aggiunto X", "add Y", "aggiungi Y") or asks
  to fill in missing fields, do NOT invent values. Use `web_search` first.
- Recommended search query: "<game name> boardgame BGG" to land on BoardGameGeek.
  Read out: designer (producer), publisher, min/max players, playing time, BGG weight.
- Map BGG weight → complexity_label: <2.0 "1. Molto Semplice", 2.0–2.4 "2. Semplice",
  2.5–3.4 "3. Medio", 3.5–4.1 "4. Complesso", ≥4.2 "5. Esperto".
- Always store the numeric `complexity_weight` AND `bgg_rating` (BGG average rating)
  AND `bgg_id` AND `description` AND `thumbnail_url` AND `year_published` AND
  `duration_min_min`/`duration_max_min` when available — they enable proper sorting/queries.
- Pass `designers`, `publishers`, `categories`, `mechanics` as arrays of names.
- Then PROPOSE the values to the user in a compact table and ask for confirmation
  ("Confermo?"). Do NOT call `add_game` or `update_game` until the user confirms.
- After confirmation, call the appropriate tool. If the game already exists, use
  `update_game`; otherwise `add_game`.
- Sleeves: BGG rarely has card sizes/counts. Try `web_search` for "<game> sleeve sizes"
  on sleevegeeks.com or publisher sites. If found, propose them with the rest of the
  metadata. Always require user confirmation before saving.

Citations (CRITICAL — the post-processor mangles "Fonti:" sections into garbage):
- ABSOLUTELY FORBIDDEN: "Fonti:", "Sources:", "Riferimenti:" sections, footnote
  lists, bullet lists of quoted excerpts. These get rendered as broken text.
- ALLOWED: ONE inline Markdown link next to a value, e.g.
  `| Designer | Mathias Wigger [↗](https://boardgamegeek.com/boardgame/342942) |`
  or `Durata: 90–150 min ([BGG](https://...))`.
- At most ONE link per row in tables, never duplicate the same URL.
- If you'd be tempted to write "Fonti:" — STOP and put the URLs inline instead.

Use `web_search` SPARINGLY: only for adding/enriching games. Use it for game
metadata, sleeves, but NOT for rules questions — see below.

Rules questions during a game (CRITICAL):
- When the user asks "in <game> can I do X?" or any rules question, use `ask_rules`
  to retrieve relevant passages from the indexed rulebook. NEVER answer rules
  questions from your own knowledge or web_search — official rules need exact citation.
- After `ask_rules` returns excerpts, synthesize the answer ONLY from those excerpts.
  Cite the page numbers naturally: "Sì, puoi attaccare un esagono vuoto (p. 12)."
- If the excerpts don't cover the question, say so plainly: "Il regolamento indicizzato
  non copre questo punto chiaramente — controlla manualmente p. X o aggiungi più pagine."
- If `ask_rules` returns an error ("no rulebook ingested"), tell the user to provide
  the PDF path and call `ingest_rulebook(game_name, pdf_path)` for them.

Never for sleeve math, inventory, or anything answerable from local tools.

Formatting (UI is small; Markdown is rendered):
- Default to SHORT prose (1–3 sentences) when the answer is short. Don't pad.
- Use Markdown freely when it helps: **bold** for key numbers/names, *italic* for
  asides, tables for multi-attribute comparisons, lists for ≥4 items, `---` to
  separate clearly distinct sections in long answers.
- Avoid: filler phrases ("Ecco…", "Vuoi dettagli?"), numbered bold headers stacked
  with `---` between every item (it's noisy), redundant restatements of the question.
- Emojis: use when they aid scanning (✅/❌ for status, 🎲 next to a game). Avoid
  decorative pile-ups.
- Re-format each turn based on the question; don't blindly copy a past style.
"""


def _serialize_tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def chat(user_message: str, history: list[dict] | None = None,
         conversation_id: int | None = None) -> tuple[str, list[dict]]:
    """Run one user turn through Claude with tool-use. Returns (reply_text, updated_history)."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    history = list(history or [])
    history.append({"role": "user", "content": user_message})
    source = f"chat:{conversation_id}" if conversation_id is not None else "chat:?"

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[WEB_SEARCH_TOOL, *TOOLS],
            messages=history,
        )

        # Append the assistant turn (raw content blocks) so tool_use_id refs line up.
        assistant_content = [block.model_dump() for block in resp.content]
        history.append({"role": "assistant", "content": assistant_content})

        if resp.stop_reason != "tool_use":
            text_parts: list[str] = []
            for b in resp.content:
                if b.type != "text":
                    continue
                text = b.text
                # Web-search citation excerpts arrive as their own text blocks with
                # a `citations` field. Append a compact link so the snippet has a source.
                citations = getattr(b, "citations", None) or []
                if citations:
                    first = citations[0]
                    url = getattr(first, "url", None)
                    if url:
                        text = f"{text.rstrip()} [↗]({url})"
                text_parts.append(text)
            return "\n".join(text_parts).strip(), history

        # Execute each tool_use block and feed results back.
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            func = TOOL_FUNCS.get(block.name)
            if func is None:
                result = {"error": f"unknown tool {block.name}"}
            else:
                # Strip any underscore-prefixed kwargs the model may have synthesized,
                # then inject our own `_source` so the audit log knows the origin.
                kwargs = {k: v for k, v in (block.input or {}).items() if not k.startswith("_")}
                if _accepts_source(block.name):
                    kwargs["_source"] = source
                try:
                    result = func(**kwargs)
                except Exception as e:  # surface tool errors back to the model
                    result = {"error": f"{type(e).__name__}: {e}"}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _serialize_tool_result(result),
                }
            )
        history.append({"role": "user", "content": tool_results})

    return "Boardy gave up after too many tool rounds.", history
