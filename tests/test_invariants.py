"""Regression guards for Boardy's most fragile, easy-to-silently-break invariants.

These are NOT a full test suite — they pin the handful of cross-cutting rules
that have bitten us before (guest executing a write tool, audit `_source`
spoofing, non-atomic requirement writes). Run with `uv run pytest`.
"""
from __future__ import annotations

import json

from app import tools
from app.chat import _accepts_source, _filter_tools_for_user, _tool_name


def _visible_names(toollist) -> set[str]:
    return {_tool_name(t) for t in toollist}


# ── Tool registry coherence ──────────────────────────────────────────────────

def test_tool_names_unique_and_match_funcs():
    names = [_tool_name(t) for t in tools.TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names in TOOLS"
    assert set(names) == set(tools.TOOL_FUNCS), (
        "TOOLS schema names and TOOL_FUNCS keys diverged"
    )


def test_write_tools_have_functions():
    missing = tools.WRITE_TOOLS - set(tools.TOOL_FUNCS)
    assert not missing, f"WRITE_TOOLS names with no function: {missing}"


# The registries are now built from the @tool decorators + a boot-time assert
# (app/tools.py). That assert guards schema↔function drift in both directions,
# but it CANNOT see a wrong write-flag (a read tool decorated @tool(write=True)
# or a writer as @tool()). That's the real guest-gating risk, so pin the counts
# and the exact write set here.
_EXPECTED_WRITE_TOOLS = {
    "add_game", "update_game", "delete_game", "set_sleeve_requirements",
    "update_inventory", "add_to_inventory",
    "add_to_wishlist", "update_wishlist", "mark_as_owned", "remove_from_wishlist",
    "ingest_rulebook", "download_rulebook",
}


def test_registry_counts_and_write_set_are_pinned():
    assert len(tools.TOOL_FUNCS) == 29, "registered tool count changed — update this pin deliberately"
    assert tools.WRITE_TOOLS == _EXPECTED_WRITE_TOOLS, (
        "WRITE_TOOLS drifted from the expected set — a tool's @tool(write=...) "
        "flag changed; confirm it's intentional before updating this pin"
    )


# ── Guest gating (the Barrage download regression) ───────────────────────────

def test_guest_sees_no_write_tools():
    visible = _filter_tools_for_user(tools.TOOLS, user=None)
    leaked = _visible_names(visible) & tools.WRITE_TOOLS
    assert not leaked, f"guest registry leaks write tools: {leaked}"


def test_owner_sees_every_tool():
    visible = _filter_tools_for_user(tools.TOOLS, user={"id": 1, "username": "o", "role": "owner"})
    assert _visible_names(visible) == set(tools.TOOL_FUNCS)


def test_source_writers_are_gated():
    """Any tool that accepts `_source` mutates state, so it MUST be in
    WRITE_TOOLS — otherwise it would be exposed to (and runnable by) guests."""
    for name in tools.TOOL_FUNCS:
        if _accepts_source(name):
            assert name in tools.WRITE_TOOLS, (
                f"{name} accepts _source (writer) but is not in WRITE_TOOLS"
            )


def test_source_never_in_tool_schema():
    """`_source` is injected by chat.py; it must never appear in a tool's JSON
    schema, or the model could spoof the audit origin."""
    for t in tools.TOOLS:
        assert "_source" not in json.dumps(t), f"{_tool_name(t)} exposes _source in its schema"


# ── State-mutation guards (the bugs Codex surfaced) ──────────────────────────

def test_update_inventory_rejects_negative():
    out = tools.update_inventory(63.5, 88, count_owned=-5, _source="test")
    assert "error" in out


def test_set_sleeve_requirements_is_atomic_on_bad_input(owned_game):
    # Seed a valid requirement.
    ok = tools.set_sleeve_requirements(
        owned_game, [{"count": 10, "width_mm": 63.5, "height_mm": 88}], _source="test"
    )
    assert ok.get("ok")

    # A batch with one malformed row must be rejected WHOLE — the prior
    # requirement must survive (no partial DELETE committed).
    bad = tools.set_sleeve_requirements(
        owned_game,
        [
            {"count": 5, "width_mm": 44, "height_mm": 68},
            {"count": "oops", "width_mm": 41, "height_mm": 63},  # invalid
        ],
        _source="test",
    )
    assert "error" in bad

    detail = tools.sleeve_games_detail("owned")
    rows = detail.get((63.5, 88), [])
    assert any(g["name"] == owned_game for g in rows), (
        "original requirement was wiped by a rejected batch — not atomic"
    )
