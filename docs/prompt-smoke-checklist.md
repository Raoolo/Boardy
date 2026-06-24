# Prompt smoke checklist

Manual/behavioral regression checklist for the chat system prompt
(`app/chat.py:SYSTEM_PROMPT_BASE`). The static `tests/test_prompt_coverage.py`
guards that rules are still *stated*; this checklist verifies the model still
*behaves*. Run after any prompt edit, against the running bot (web UI or
`POST /chat`). Each case maps to a historical regression.

How to run one case via curl (guest, ephemeral):
```bash
curl -s -X POST http://127.0.0.1:8765/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"quanti giochi ho?"}' | python3 -m json.tool
```

| # | Prompt | Expected behavior | Guards against |
|---|--------|-------------------|----------------|
| 1 | "quanti giochi ho?" | Calls `list_games()` (no filters); the number equals the tool's `count`, not an estimate. | Counting from memory / header≠count |
| 2 | "com'è messa la mia collezione?" | `list_games()` no filters, groups by sleeve status with **Italian** labels (Imbustati / Da imbustare / …), totals add up. | Subset answers; raw enum tokens leaking |
| 3 | "quante buste mi mancano?" | Calls `sleeve_summary`, reports by size. | Doing sleeve math by hand |
| 4 | "cosa posso sleevare ora?" | Calls `games_ready_to_sleeve`; if `has_contention` surfaces the contention note. | Silently implying you can sleeve everything |
| 5 | "ho comprato 50 buste 63.5x88" | Calls `add_to_inventory(delta=+50)`, reports previous/delta/new. | Computing new total itself; using update_inventory |
| 6 | "aggiungi Wingspan" (owned, new) | bgg_search+bgg_lookup + sleeve_lookup → ONE table incl. "Buste previste" → waits for "confermo". | Writing without confirm; missing sleeve row |
| 7 | "metti Spirit Island in wishlist" | `add_to_wishlist`, replies in ONE sentence, NO confirmation table. | Applying the owned confirm ritual to wishlist |
| 8 | "in <gioco con regolamento> posso fare X?" | `ask_rules`, answer only from excerpts, cites a page. | Answering rules from own knowledge |
| 9 | "in <gioco SENZA regolamento> posso fare X?" | `ask_rules` errors → `find_rulebook` → proposes candidate, waits for "sì". | Giving up / inventing rules |
| 10 | a question needing the web (e.g. a price/availability) | `web_search` with English name; cites inline links, **no "Fonti:" section**. | "Fonti:" blocks; reading `content` not `raw_content` |
| 11 | "consigliami qualcosa di rilassante" | `search_games_semantic`, shows top 3-5 with score-aware phrasing. | Overselling weak (<0.72) matches |

Pass = behavior matches. On a fail, restore the dropped/garbled rule in
`SYSTEM_PROMPT_BASE` (git is the safety net) and re-run. Tip: tail the server
log (`docker compose -f deploy/docker-compose.yml logs -f boardy`) to see the
`[boardy] … call <tool>` lines and confirm the right tool fired.
