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
  origin TEXT NOT NULL DEFAULT 'web',
  actor_role TEXT NOT NULL DEFAULT 'owner',
  actor_id TEXT,
  actor_name TEXT,
  history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate() -> None:
    with get_conn() as c:
        c.executescript(MIGRATION)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(conversations)").fetchall()}
        if "origin" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN origin TEXT NOT NULL DEFAULT 'web'")
        if "actor_role" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN actor_role TEXT NOT NULL DEFAULT 'owner'")
        if "actor_id" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN actor_id TEXT")
        if "actor_name" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN actor_name TEXT")
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


def _owned_by_user_where(user: dict) -> tuple[str, tuple]:
    """SQL predicate for conversations owned by an authenticated Boardy user.

    `actor_id` is not enough because Telegram owner chats use the Telegram
    user_id, while web chats use the Boardy DB user id. `actor_name` bridges
    both worlds when the same human has a Boardy username.
    """
    uid = str(user.get("id")) if user.get("id") is not None else ""
    username = (user.get("username") or "").lower()
    return (
        """actor_role='owner' AND (
             actor_id=?
             OR lower(COALESCE(actor_name, ''))=?
           )""",
        (uid, username),
    )


def list_conversations(*, user: dict | None = None, scope: str = "mine") -> list[dict]:
    with get_conn() as c:
        base = """SELECT id, title, created_at, updated_at,
                         origin, actor_role, actor_id, actor_name
                  FROM conversations"""
        params: tuple = ()
        if scope != "audit":
            if user is None:
                return []
            where, params = _owned_by_user_where(user)
            base += f" WHERE {where}"
        rows = c.execute(base + " ORDER BY updated_at DESC LIMIT 100", params).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute(
            """SELECT id, title, created_at, updated_at,
                      origin, actor_role, actor_id, actor_name, history_json
               FROM conversations WHERE id=?""",
            (conv_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["history"] = json.loads(d.pop("history_json") or "[]")
    return d


def is_owned_by_user(conversation: dict, user: dict) -> bool:
    if conversation.get("actor_role") != "owner":
        return False
    uid = str(user.get("id")) if user.get("id") is not None else ""
    username = (user.get("username") or "").lower()
    actor_id = str(conversation.get("actor_id") or "")
    actor_name = str(conversation.get("actor_name") or "").lower()
    if (uid and actor_id == uid) or (username and actor_name == username):
        return True
    # Telegram owner chats are stored with the *Telegram* identity (user_id +
    # display name) for audit, but the bot always authenticates as the shared
    # service account BOARDY_BOT_USERNAME — so neither id nor name ever matches
    # the cookie user, and a plain ownership check 403s every continuation.
    # The bot already gates which conversation a chat_id may continue via its own
    # per-chat state file, and only owner/admin roles reach this branch, so we
    # trust any owner/admin to continue a telegram-origin owner conversation.
    if (conversation.get("origin") == "telegram"
            and user.get("role") in {"owner", "admin"}):
        return True
    return False


def create_conversation(
    *,
    origin: str = "web",
    actor_role: str = "owner",
    actor_id: str | None = None,
    actor_name: str | None = None,
) -> int:
    now = _now()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO conversations(
                   created_at, updated_at, origin, actor_role, actor_id, actor_name, history_json
               ) VALUES(?, ?, ?, ?, ?, ?, '[]')""",
            (now, now, origin, actor_role, actor_id, actor_name),
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
