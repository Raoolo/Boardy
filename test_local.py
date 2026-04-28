"""Smoke test: does Qwen2.5 7B handle Boardy's tools well enough?

Runs 5 realistic Italian prompts through the local Ollama model using its
OpenAI-compatible API. Each prompt has an EXPECTED tool call; we print what
the model picked and whether it matched.

Run:  uv run python test_local.py
"""
from __future__ import annotations

import json
from openai import OpenAI

# Ollama exposes an OpenAI-compatible endpoint. api_key is required by the
# SDK but ignored by Ollama — any string works.
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL = "qwen2.5:7b-instruct"

# Subset of Boardy's tools, converted from Anthropic schema (input_schema)
# to OpenAI schema (function.parameters). The shape inside is identical;
# only the wrapper differs.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_games",
            "description": "List games with optional filters (substring, AND-combined).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_contains":      {"type": "string"},
                    "players":            {"type": "integer"},
                    "mechanic_contains":  {"type": "string"},
                    "category_contains":  {"type": "string"},
                    "designer_contains":  {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game",
            "description": "Full record for one game.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sleeve_summary",
            "description": "Aggregate by sleeve size: needed across collection, owned, to_buy.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_inventory",
            "description": "Set ABSOLUTE sleeve count. Use only for explicit recounts ('ho ricontato e sono 250').",
            "parameters": {
                "type": "object",
                "properties": {
                    "width_mm":    {"type": "number"},
                    "height_mm":   {"type": "number"},
                    "count_owned": {"type": "integer"},
                },
                "required": ["width_mm", "height_mm", "count_owned"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_inventory",
            "description": (
                "Increment sleeve inventory by a delta. PREFERRED for 'ho comprato N' / "
                "'ne ho usati N'. Server does the arithmetic — never compute new = old + N."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "width_mm":  {"type": "number"},
                    "height_mm": {"type": "number"},
                    "delta":     {"type": "integer"},
                    "note":      {"type": "string"},
                },
                "required": ["width_mm", "height_mm", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_changes",
            "description": "Audit log. Use `game_name` to filter changes for one game.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":     {"type": "integer"},
                    "game_name": {"type": "string"},
                },
            },
        },
    },
]

SYSTEM = (
    "Sei l'assistente di Boardy, un'app inventario di giochi da tavolo. "
    "Quando l'utente fa una richiesta, scegli SEMPRE il tool più adatto e chiamalo "
    "usando il formato strutturato dei tool — MAI emettere JSON o XML come testo. "
    "Rispondi in italiano. "
    "Per aggiunte/sottrazioni di sleeve usa add_to_inventory; "
    "per ricontaggi assoluti ('ho ricontato e sono esattamente N') usa update_inventory. "
    "Le 'mechanics' di un gioco includono 'Engine Building', 'Drafting', 'Worker Placement' ecc. "
    "Le 'categories' sono temi tipo 'Strategy', 'Card Game'. Engine builder = mechanic, non category."
)

# (prompt italiano, tool atteso, descrizione del test)
CASES = [
    ("Ho appena comprato 100 sleeve 63.5x88, aggiungili",
     "add_to_inventory",
     "delta-add: 'ho comprato N' → add_to_inventory, NON update_inventory"),

    ("Quanti sleeve mi mancano in totale?",
     "sleeve_summary",
     "no-args: chiamata senza parametri"),

    ("Mostrami i giochi engine builder a 4 giocatori",
     "list_games",
     "filtri multipli: players=4 + mechanic_contains='engine'"),

    ("Dammi tutte le info su Wingspan",
     "get_game",
     "name lookup semplice"),

    ("Cosa è cambiato di recente per Dune Imperium?",
     "recent_changes",
     "filtro game_name su audit log"),

    ("Ho ricontato e ho esattamente 250 sleeve 63.5x88, aggiorna",
     "update_inventory",
     "ricontaggio assoluto → update_inventory (caso opposto)"),
]


def run_case(prompt: str, expected_tool: str, label: str) -> bool:
    print(f"\n{'─' * 70}")
    print(f"TEST: {label}")
    print(f"USER: {prompt}")
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=TOOLS,
        tool_choice="auto",
    )
    msg = resp.choices[0].message
    if not msg.tool_calls:
        print(f"  ✗ NESSUN TOOL CHIAMATO. Risposta testuale: {msg.content[:200]!r}")
        return False
    tc = msg.tool_calls[0]
    name = tc.function.name
    args = tc.function.arguments
    try:
        args_parsed = json.loads(args) if args else {}
    except json.JSONDecodeError:
        args_parsed = f"<JSON invalido: {args!r}>"
    ok = name == expected_tool
    mark = "✓" if ok else "✗"
    print(f"  {mark} chiamato: {name}({args_parsed})")
    print(f"    atteso: {expected_tool}")
    return ok


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1252 fix
    print(f"Testing model: {MODEL}")
    print(f"Endpoint: {client.base_url}")
    results = [run_case(p, e, l) for p, e, l in CASES]
    passed = sum(results)
    total = len(results)
    print(f"\n{'═' * 70}")
    print(f"RISULTATO: {passed}/{total} tool routing corretti")
    if passed == total:
        print("→ Modello promosso. Pronti per il refactor di chat.py.")
    elif passed >= total - 1:
        print("→ Quasi promosso. Vediamo dove ha sbagliato — magari basta tunare il prompt.")
    else:
        print("→ Bocciato. Salire a qwen2.5:14b-instruct e ritestare.")


if __name__ == "__main__":
    main()
