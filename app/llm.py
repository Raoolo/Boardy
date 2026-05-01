"""LLM provider abstraction for Boardy.

Three providers behind a common interface:
- AnthropicProvider: Claude via API.
- DeepSeekProvider:  DeepSeek via OpenAI-compatible API. Cheap (~10× less
                     than Sonnet) with solid tool use; no native search.
- OllamaProvider:    local Ollama via OpenAI-compatible endpoint.

Web search is now a CLIENT-SIDE tool (Tavily-backed, see app/tools.py),
so all providers share the same capability surface — no more
WEBSEARCH/NO_WEBSEARCH asymmetry in the system prompt.

The chat loop in app/chat.py is provider-agnostic: it passes Anthropic-style
tool schemas (TOOLS from app/tools.py) and gets back a ProviderResponse with
normalized content blocks. OpenAI-compatible providers translate schemas +
history on input, and translate tool_calls back on output.

Switch via env:
  LLM_PROVIDER=anthropic | deepseek | ollama   (default: anthropic)
  LLM_MODEL=...                                (default per provider)
  DEEPSEEK_API_KEY=...                         (required for deepseek)
  DEEPSEEK_BASE_URL=...                        (default: https://api.deepseek.com/v1)
  OLLAMA_BASE_URL=...                          (default: http://localhost:11434/v1)
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ----- Normalized content blocks (vendor-neutral, modeled after Anthropic) -----

@dataclass
class TextBlock:
    text: str
    citations: list[dict] = field(default_factory=list)
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class ProviderResponse:
    """Result of one model turn, normalized across providers.

    `blocks` is what the chat loop iterates over. `assistant_history_entry`
    is the opaque payload to append to history — its shape depends on the
    provider, but chat.py only stores/forwards it and doesn't inspect it.
    """
    stop_reason: str  # "tool_use" | "end_turn" | other
    blocks: list[TextBlock | ToolUseBlock]
    assistant_history_entry: dict


# ----- Base interface -----

class Provider(ABC):
    name: str
    # Local / small-context models prefer the slim system prompt to keep
    # CPU prefill costs low. API-served frontier models use the full prompt.
    prefer_slim_prompt: bool = False

    @abstractmethod
    def run_turn(self, history: list[dict], system_prompt: str,
                 tools: list[dict]) -> ProviderResponse:
        """One model call. `tools` are Anthropic-shaped schemas from app/tools.py."""

    @abstractmethod
    def tool_result_history_entries(self, tool_results: list[dict]) -> list[dict]:
        """Build history entries that feed tool results back to the model.

        Anthropic packs all results into a single user message; OpenAI wants
        one role=tool message per result. Returning a list keeps the caller
        simple: history.extend(...) regardless of provider.
        """


# ============================================================================
# Anthropic (Claude)
# ============================================================================

class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str | None = None, max_tokens: int = 2048) -> None:
        from anthropic import Anthropic
        self._client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = model or os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
        self.max_tokens = max_tokens

    def run_turn(self, history, system_prompt, tools):
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            # cache_control on the system prompt: Anthropic-only optimization,
            # no-ops elsewhere. Saves ~75% on input tokens for repeated turns.
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=history,
        )
        blocks: list[TextBlock | ToolUseBlock] = []
        for b in resp.content:
            if b.type == "text":
                cits = []
                for c in (getattr(b, "citations", None) or []):
                    url = getattr(c, "url", None)
                    if url:
                        cits.append({"url": url})
                blocks.append(TextBlock(text=b.text, citations=cits))
            elif b.type == "tool_use":
                blocks.append(ToolUseBlock(id=b.id, name=b.name,
                                           input=dict(b.input or {})))
            # Other blocks (web_search_tool_result etc.) ride along inside
            # assistant_history_entry but aren't surfaced to chat.py.
        assistant_entry = {
            "role": "assistant",
            "content": [block.model_dump() for block in resp.content],
        }
        return ProviderResponse(stop_reason=resp.stop_reason, blocks=blocks,
                                assistant_history_entry=assistant_entry)

    def tool_result_history_entries(self, tool_results):
        # Anthropic: a single user turn whose content is the list of tool_result blocks.
        return [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r["tool_use_id"],
                 "content": r["content"]}
                for r in tool_results
            ],
        }]


# ============================================================================
# Ollama (OpenAI-compatible local)
# ============================================================================

class OllamaProvider(Provider):
    name = "ollama"
    prefer_slim_prompt = True  # local CPU prefill is the bottleneck

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None) -> None:
        from openai import OpenAI
        self._client = OpenAI(
            base_url=base_url or os.environ.get(
                "OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            # Ollama ignores the value but the SDK requires non-empty;
            # subclasses (DeepSeek, etc.) pass a real key here.
            api_key=api_key or "ollama",
        )
        # Default to the Boardy-tuned derived model (boardy-qwen.Modelfile),
        # which bakes in num_ctx=8192 and saner sampling. Ollama's OpenAI-
        # compat endpoint silently drops `options` from extra_body, so
        # baking config into a Modelfile is the only reliable path.
        self.model = model or os.environ.get("LLM_MODEL", "boardy-qwen")

    def run_turn(self, history, system_prompt, tools):
        oa_tools = [self._tool_anthropic_to_openai(t) for t in tools]
        oa_messages = [{"role": "system", "content": system_prompt},
                       *self._history_to_openai(history)]
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=oa_messages,
            tools=oa_tools,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        blocks: list[TextBlock | ToolUseBlock] = []
        if msg.content:
            blocks.append(TextBlock(text=msg.content))
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=args))
        stop_reason = "tool_use" if msg.tool_calls else "end_turn"
        # Store the OpenAI-shaped assistant message verbatim in history so
        # the next OpenAI call gets a consistent transcript.
        assistant_entry: dict = {"role": "assistant",
                                 "content": msg.content or ""}
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return ProviderResponse(stop_reason=stop_reason, blocks=blocks,
                                assistant_history_entry=assistant_entry)

    def tool_result_history_entries(self, tool_results):
        # OpenAI: one role=tool message per result.
        return [
            {"role": "tool", "tool_call_id": r["tool_use_id"],
             "content": r["content"]}
            for r in tool_results
        ]

    @staticmethod
    def _tool_anthropic_to_openai(tool: dict) -> dict:
        """Wrap Anthropic schema in OpenAI's `function` envelope.

        The inner JSON Schema (`input_schema` → `parameters`) is identical
        between providers, so this is purely envelope translation.
        """
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema",
                                       {"type": "object", "properties": {}}),
            },
        }

    @staticmethod
    def _history_to_openai(history: list[dict]) -> list[dict]:
        """Translate a mixed history (Anthropic-shaped or OpenAI-shaped) → OpenAI.

        Why mixed: a conversation started under AnthropicProvider may continue
        under OllamaProvider. We translate Anthropic content blocks on the fly
        rather than rewriting the stored history.
        """
        out: list[dict] = []
        for turn in history:
            role = turn.get("role")
            content = turn.get("content")

            # ---- Already OpenAI-shaped ----
            if role == "tool":
                out.append(turn)
                continue
            if role == "assistant" and isinstance(content, str):
                entry = {"role": "assistant", "content": content}
                if turn.get("tool_calls"):
                    entry["tool_calls"] = turn["tool_calls"]
                out.append(entry)
                continue
            if role == "user" and isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue

            # ---- Anthropic-shaped: content is a list of blocks ----
            if not isinstance(content, list):
                continue

            if role == "assistant":
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                for b in content:
                    t = b.get("type")
                    if t == "text":
                        text_parts.append(b.get("text", ""))
                    elif t == "tool_use":
                        tool_calls.append({
                            "id": b["id"],
                            "type": "function",
                            "function": {
                                "name": b["name"],
                                "arguments": json.dumps(b.get("input") or {},
                                                        ensure_ascii=False),
                            },
                        })
                    # Skip web_search_tool_result and similar — Ollama can't use them.
                entry = {"role": "assistant", "content": "\n".join(text_parts)}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                out.append(entry)
            elif role == "user":
                # User turn carrying tool_result blocks (Anthropic shape).
                for b in content:
                    if b.get("type") == "tool_result":
                        c = b.get("content", "")
                        if not isinstance(c, str):
                            c = json.dumps(c, ensure_ascii=False)
                        out.append({"role": "tool",
                                    "tool_call_id": b["tool_use_id"],
                                    "content": c})
                # Also collect any free-text user blocks (rare).
                free_text = "\n".join(b.get("text", "") for b in content
                                      if b.get("type") == "text")
                if free_text:
                    out.append({"role": "user", "content": free_text})
        return out


# ============================================================================
# DeepSeek (OpenAI-compatible, hosted)
# ============================================================================

class DeepSeekProvider(OllamaProvider):
    """DeepSeek via the official OpenAI-compatible endpoint.

    Reuses OllamaProvider's translation layer (Anthropic↔OpenAI tool schemas
    + history) — only the client config changes (real API key, hosted URL,
    `prefer_slim_prompt=False` because we're on a frontier model, not a
    7B local one).
    """
    name = "deepseek"
    prefer_slim_prompt = False

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        super().__init__(
            model=model or os.environ.get("LLM_MODEL", "deepseek-chat"),
            base_url=base_url or os.environ.get(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=os.environ["DEEPSEEK_API_KEY"],
        )


# ============================================================================
# Factory
# ============================================================================

def get_provider() -> Provider:
    name = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "deepseek":
        return DeepSeekProvider()
    if name == "ollama":
        return OllamaProvider()
    raise ValueError(
        f"unknown LLM_PROVIDER {name!r}. Use 'anthropic', 'deepseek', or 'ollama'."
    )
