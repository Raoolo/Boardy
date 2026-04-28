"""Provider-agnostic tool-use chat loop for Boardy.

The actual LLM client (Anthropic vs local Ollama) is selected in app/llm.py
via the LLM_PROVIDER env var. This file owns:
  - the system prompt (base + optional web_search addendum)
  - the tool-use loop and round limit
  - audit-log `_source` injection for write tools
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Any

from .llm import TextBlock, ToolUseBlock, get_provider
from .tools import TOOL_FUNCS, TOOLS


@functools.cache
def _accepts_source(name: str) -> bool:
    """True if a tool function declares a `_source` kwarg (writers do, readers don't)."""
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return False
    return "_source" in inspect.signature(fn).parameters


MAX_TOOL_ROUNDS = 8


# Base prompt: applies to every provider. Avoid mentioning web_search here —
# providers without it would advertise a non-existent capability.
SYSTEM_PROMPT_BASE = """You are Boardy, a personal assistant for Raulo's board-game collection.

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

Adding or enriching a game:
- Propose values to the user in a compact table BEFORE saving; wait for
  explicit confirmation ("Confermo?" / "sì") before calling add_game or update_game.
- If the game already exists, use `update_game`; otherwise `add_game`.
- Pass `designers`, `publishers`, `categories`, `mechanics` as arrays of names.
- Map BGG weight → complexity_label: <2.0 "1. Molto Semplice", 2.0–2.4 "2. Semplice",
  2.5–3.4 "3. Medio", 3.5–4.1 "4. Complesso", ≥4.2 "5. Esperto".
- Always store numeric `complexity_weight` AND `bgg_rating` AND `bgg_id` AND
  `description` AND `thumbnail_url` AND `year_published` AND
  `duration_min_min`/`duration_max_min` when available — they enable proper sorting/queries.

Rules questions during a game (CRITICAL):
- When the user asks "in <game> can I do X?" or any rules question, use `ask_rules`
  to retrieve relevant passages from the indexed rulebook. NEVER answer rules
  questions from your own knowledge — official rules need exact citation.
- After `ask_rules` returns excerpts, synthesize the answer ONLY from those excerpts.
  Cite the page numbers naturally: "Sì, puoi attaccare un esagono vuoto (p. 12)."
- If the excerpts don't cover the question, say so plainly: "Il regolamento indicizzato
  non copre questo punto chiaramente — controlla manualmente p. X o aggiungi più pagine."
- If `ask_rules` returns an error ("no rulebook ingested"), tell the user to provide
  the PDF path and call `ingest_rulebook(game_name, pdf_path)` for them.

Citation formatting (in case you cite an external source):
- ABSOLUTELY FORBIDDEN: "Fonti:", "Sources:", "Riferimenti:" sections, footnote
  lists, bullet lists of quoted excerpts. They get rendered as broken text.
- ALLOWED: ONE inline Markdown link next to a value, e.g.
  `| Designer | Mathias Wigger [↗](https://boardgamegeek.com/boardgame/342942) |`
  or `Durata: 90–150 min ([BGG](https://...))`.
- At most ONE link per row in tables, never duplicate the same URL.

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

# Appended only when the provider supports web_search (Anthropic).
SYSTEM_PROMPT_WEBSEARCH_ADDENDUM = """\

Web search (Anthropic-only capability):
- Use `web_search` ONLY for adding/enriching a game. Search "<game name> boardgame BGG"
  to land on BoardGameGeek. Read out designer, publisher, players, duration, BGG weight,
  rating, bgg_id, description, thumbnail, year. Then propose values + ask "Confermo?".
- Sleeves: try "<game> sleeve sizes" on sleevegeeks.com or publisher sites.
- Use SPARINGLY. NEVER for rules questions (use ask_rules) or anything answerable
  from local tools (sleeve math, inventory, audit log).
"""

# Slim prompt for local providers without web_search.
# CPU/iGPU prefill is the dominant cost, so we trade some behavioral nuance
# for a much shorter prompt (~700 tokens vs ~3000). Keeps only the rules that
# directly affect tool routing: which tool to pick, when to confirm, sleeve
# slang lookup, the add_to_inventory vs update_inventory distinction.
SYSTEM_PROMPT_SLIM = """You are Boardy, a personal assistant for Raulo's board-game collection.

Reply in the user's language (Italian if they write Italian, else English).

Tool calls: when you need a tool, emit it via the STRUCTURED tool-call channel —
NEVER print JSON or XML like {"name": "...", "arguments": {...}} as chat text.
If unsure which tool, pick the most likely one and call it; don't ask for clarification
when a no-arg tool (sleeve_summary, list_inventory) would already answer.

ALWAYS use the tools — never invent game names, counts, or sizes.

Sleeve sizes: comma is decimal separator ("63,5x88" = 63.5×88). DB is in mm.
Slang: "Standard American"=63.5×88, "Mini American"=41×63 or 44×68,
"Catan"=57×87, "Euro"=59×92, "Tarot"=70×120. Confirm if ambiguous.

Inventory (CRITICAL — never compute new = old + N yourself):
- "ho comprato N" / "ne ho usate N" → call `add_to_inventory(width_mm, height_mm, delta=±N)`.
  Negative delta when consuming. Server does the math.
- "ho esattamente N in totale" → call `update_inventory(..., count_owned=N)`.
- After the call, report previous_count, delta, count_owned from the result.

For "quanti me ne mancano" → call `sleeve_summary` (no args).

History questions ("quando ho aggiunto X?", "cosa è cambiato?") → call
`recent_changes(limit=20, game_name?, table?)`. Don't guess from memory.

Rules questions ("in <gioco> posso fare X?") → call `ask_rules(game_name, question)`.
Synthesize ONLY from returned excerpts and cite page numbers. If excerpts don't
cover it, say so plainly. Never answer rules from your own knowledge.
If `ask_rules` errors with "no rulebook ingested", ask the user for the PDF path
and call `ingest_rulebook(game_name, pdf_path)`.

Adding/updating a game (LOCAL MODE — no web access):
- Use only fields the user provides. Do NOT invent designer/publisher/weight/bgg_id.
- Propose a compact table and wait for "sì/confermo" before calling add_game/update_game.
- Lists (designers, publishers, categories, mechanics) go as arrays of strings.
- For full BGG metadata, suggest running `etl/backfill_v2.py` from CLI.

Formatting: short prose by default (1–3 sentences). Markdown OK (**bold**, tables,
lists for ≥4 items). Avoid filler ("Ecco…", "Vuoi dettagli?").
"""


def _build_system_prompt(supports_web_search: bool) -> str:
    if supports_web_search:
        # Anthropic: full base + web_search addendum. Cache_control makes the
        # length cheap; behavioral nuance is worth the tokens.
        return SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_WEBSEARCH_ADDENDUM
    # Local providers (Ollama): slim prompt. CPU prefill is the bottleneck.
    return SYSTEM_PROMPT_SLIM


def _serialize_tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def chat(user_message: str, history: list[dict] | None = None,
         conversation_id: int | None = None) -> tuple[str, list[dict]]:
    """Run one user turn through the configured LLM with tool-use.

    Returns (reply_text, updated_history). History is appended to in place
    style (we return the new list).
    """
    provider = get_provider()
    history = list(history or [])
    history.append({"role": "user", "content": user_message})
    source = f"chat:{conversation_id}" if conversation_id is not None else "chat:?"
    system_prompt = _build_system_prompt(provider.supports_web_search)

    for _ in range(MAX_TOOL_ROUNDS):
        resp = provider.run_turn(history, system_prompt, TOOLS)
        history.append(resp.assistant_history_entry)

        if resp.stop_reason != "tool_use":
            text_parts: list[str] = []
            for b in resp.blocks:
                if not isinstance(b, TextBlock):
                    continue
                text = b.text
                # Anthropic citation (web_search): append a compact link so
                # the snippet has a source even after the JSON round-trip.
                if b.citations:
                    url = b.citations[0].get("url")
                    if url:
                        text = f"{text.rstrip()} [↗]({url})"
                text_parts.append(text)
            return "\n".join(text_parts).strip(), history

        # Execute each tool_use block and feed results back via the provider's
        # preferred history shape.
        tool_results: list[dict] = []
        for b in resp.blocks:
            if not isinstance(b, ToolUseBlock):
                continue
            func = TOOL_FUNCS.get(b.name)
            if func is None:
                result: Any = {"error": f"unknown tool {b.name}"}
            else:
                # Strip any underscore-prefixed kwargs the model may have
                # synthesized, then inject our `_source` so the audit log
                # knows the origin. The model can't spoof _source because
                # it's not declared in the JSON tool schemas.
                kwargs = {k: v for k, v in (b.input or {}).items()
                          if not k.startswith("_")}
                if _accepts_source(b.name):
                    kwargs["_source"] = source
                try:
                    result = func(**kwargs)
                except Exception as e:  # surface tool errors back to the model
                    result = {"error": f"{type(e).__name__}: {e}"}
            tool_results.append({"tool_use_id": b.id,
                                 "content": _serialize_tool_result(result)})
        history.extend(provider.tool_result_history_entries(tool_results))

    return "Boardy gave up after too many tool rounds.", history
