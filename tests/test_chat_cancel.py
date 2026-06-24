"""Cooperative cancellation of a chat turn (the ⏹ Stop button)."""
from __future__ import annotations

import app.chat as chatmod


class _BoomProvider:
    """A provider that fails loudly if its turn is ever run — proves the cancel
    check short-circuits BEFORE any LLM call."""
    name = "boom"
    prefer_slim_prompt = False

    def run_turn(self, *a, **k):
        raise AssertionError("run_turn must not be called when the turn is cancelled")

    def tool_result_history_entries(self, results):
        return []


def test_cancel_check_short_circuits_before_provider(monkeypatch):
    monkeypatch.setattr(chatmod, "get_provider", lambda: _BoomProvider())
    reply, history = chatmod.chat("ciao", history=[], cancel_check=lambda: True)
    assert "interrott" in reply.lower()
    # the user message is recorded, but no assistant round ran
    assert history[-1]["role"] == "user"


def test_no_cancel_check_runs_normally(monkeypatch):
    # Sanity: without a cancel_check the loop proceeds to call the provider.
    calls = {"n": 0}

    class _OneShot(_BoomProvider):
        def run_turn(self, history, system_prompt, tools):
            calls["n"] += 1
            from app.llm import ProviderResponse, TextBlock
            return ProviderResponse(
                stop_reason="end_turn",
                blocks=[TextBlock(text="ok")],
                assistant_history_entry={"role": "assistant", "content": "ok"},
            )

    monkeypatch.setattr(chatmod, "get_provider", lambda: _OneShot())
    reply, _ = chatmod.chat("ciao", history=[], cancel_check=None)
    assert reply == "ok"
    assert calls["n"] == 1
