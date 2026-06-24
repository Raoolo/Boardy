"""Anti-drop coverage for the chat system prompt.

SYSTEM_PROMPT_BASE was compacted (dedup + restructure) from ~300 to ~185 lines.
Every behavioral rule had to survive that edit. This test pins the load-bearing
markers: if a future refactor silently drops a section/rule, a marker disappears
and this fails. It does NOT test model behavior (that's the manual/live smoke in
docs/prompt-smoke-checklist.md) — only that the rule is still *stated*.
"""
from __future__ import annotations

from app.chat import SYSTEM_PROMPT_BASE as BASE

# Each marker is a distinctive substring tied to one rule/section. Keep them
# specific enough that dropping the rule drops the marker.
_REQUIRED_MARKERS = [
    # Ground rules
    "count",                       # count-is-truth
    "list_games()",                # full-collection view
    "owned", "wishlist",           # owned/wishlist fence
    # Tool routing
    "search_games_semantic",
    "bgg_search", "bgg_lookup",
    "sleeve_lookup",
    "recent_changes",
    "web_search",
    # Owned write ritual
    "confermo",                    # explicit confirmation
    "set_sleeve_requirements",
    "complexity_label",
    "to_sleeve",
    # Wishlist write ritual
    "add_to_wishlist", "update_wishlist", "mark_as_owned", "remove_from_wishlist",
    "NO confirmation",             # the owned/wishlist contrast
    # Sleeves
    "sleeved", "unknown",
    "games_ready_to_sleeve", "contention",
    "Standard American", "63,5x88",
    # Inventory
    "add_to_inventory", "update_inventory",
    # Rules
    "ask_rules", "find_rulebook", "download_rulebook",
    "rulebook_autofetch",
    "warnings",
    # Semantic thresholds
    "0.78", "0.72",
    # Web search
    "raw_content",
    # Formatting / citations / wording
    "Fonti",                       # the no-Fonti rule
    "imbustati",                   # enum -> Italian translation
    "in collezione",
]


def test_base_prompt_covers_all_load_bearing_rules():
    missing = [m for m in _REQUIRED_MARKERS if m not in BASE]
    assert not missing, f"system prompt lost these rule markers: {missing}"


def test_base_prompt_stayed_compact():
    # Guard against the prompt re-bloating back toward the old ~19.5k chars.
    # Generous ceiling — this is a smoke gauge, not a hard budget.
    assert len(BASE) < 16000, f"BASE prompt grew to {len(BASE)} chars — re-check for bloat"


def test_owned_and_wishlist_rituals_both_present():
    # The historical contradiction was owned 'propose+confirm' vs wishlist
    # 'no confirm' scattered/implicit. They must both be stated.
    assert "TWO different rituals" in BASE
    assert "WAIT for explicit" in BASE          # owned
    assert "never a confirmation table" in BASE  # wishlist
