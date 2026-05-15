"""Server-side conversation persistence.

Single SQLite table; auto-created on import. History is stored as JSON blob
(simple, since 99% of access is whole-conversation read/write).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .db import get_conn

MIGRATION = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT,
  history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate() -> None:
    with get_conn() as c:
        c.executescript(MIGRATION)
        c.commit()


def _extract_text(content) -> str:
    """Pull free-text out of an Anthropic-list or plain-string content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _first_exchange(history: list[dict]) -> tuple[str | None, str | None]:
    """Return (first_user_text, first_assistant_text_with_content).

    Skips tool-only assistant turns: an assistant turn whose blocks are all
    `tool_use` (Anthropic) or whose `content` is empty with `tool_calls`
    (OpenAI shape) has no prose yet — the real reply comes after the tool
    round. Walks until it finds an assistant turn that actually carries text.
    """
    user_text: str | None = None
    assistant_text: str | None = None
    for turn in history:
        role = turn.get("role")
        text = _extract_text(turn.get("content")).strip()
        if role == "user" and user_text is None and text:
            user_text = text
        elif role == "assistant" and assistant_text is None and text:
            assistant_text = text
        if user_text and assistant_text:
            break
    return user_text, assistant_text


# Hardcoded to DeepSeek (same precedent as /library/filter) — cheapest viable
# model, decoupled from per-request LLM_PROVIDER so titles cost ~$0.0001 each
# even when the chat runs on Sonnet. If DEEPSEEK_API_KEY is missing the helper
# returns None and we silently fall back to truncation.
_TITLE_SYSTEM_PROMPT = (
    "You are a titler. Given the first exchange of a conversation, return ONLY "
    "a very short title (3-5 words, max 50 chars) that describes the user's "
    "INTENT, not the numbers in the answer. CRITICAL: write the title in the "
    "same language the USER wrote in — Italian if the user wrote Italian, "
    "English if English. No quotes, no trailing period, no prefixes like "
    "'Title:' or 'Titolo:'."
)


def _generate_title_llm(user_text: str, assistant_text: str) -> str | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
        # 400-char cap on each side keeps the prompt tiny and predictable.
        prompt = (
            f"Utente: {user_text[:400]}\n\n"
            f"Assistente: {assistant_text[:400]}"
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            temperature=0,
            max_tokens=20,
            messages=[
                {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        title = (resp.choices[0].message.content or "").strip()
        # Sanitize quotes + common prefixes + trailing punctuation.
        title = title.strip('"').strip("'").strip()
        for prefix in ("titolo:", "title:"):
            if title.lower().startswith(prefix):
                title = title[len(prefix):].strip()
        title = title.rstrip(".").strip()
        if not title:
            return None
        if len(title) > 60:
            title = title[:60].rstrip() + "…"
        return title
    except Exception:
        return None


def _title_from_history(history: list[dict]) -> str | None:
    user_text, assistant_text = _first_exchange(history)
    if not user_text:
        return None
    if assistant_text:
        llm_title = _generate_title_llm(user_text, assistant_text)
        if llm_title:
            return llm_title
    # Fallback: truncate first user message (legacy behavior).
    text = user_text.replace("\n", " ")
    return (text[:60] + "…") if len(text) > 60 else text


def list_conversations() -> list[dict]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT id, title, created_at, updated_at, history_json FROM conversations WHERE id=?",
            (conv_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["history"] = json.loads(d.pop("history_json") or "[]")
    return d


def create_conversation() -> int:
    now = _now()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO conversations(created_at, updated_at, history_json) VALUES(?, ?, '[]')",
            (now, now),
        )
        c.commit()
    return cur.lastrowid


def save_conversation(conv_id: int, history: list[dict]) -> None:
    with get_conn() as c:
        # Skip title computation entirely once one is stored — avoids paying
        # for an LLM call on every subsequent save. COALESCE still guards
        # against races / external resets.
        row = c.execute("SELECT title FROM conversations WHERE id=?", (conv_id,)).fetchone()
        existing = (row["title"] if row else None) or ""
        title_candidate = _title_from_history(history) if not existing.strip() else None
        c.execute(
            "UPDATE conversations SET history_json=?, updated_at=?, title=COALESCE(NULLIF(title,''), ?) WHERE id=?",
            (json.dumps(history, ensure_ascii=False), _now(), title_candidate, conv_id),
        )
        c.commit()


def delete_conversation(conv_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        c.commit()
