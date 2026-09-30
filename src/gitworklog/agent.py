"""Single orchestrating agent: LLM <-> typed tools, with hard iteration and context limits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gitworklog.config import Limits
from gitworklog.llm import ChatBackend
from gitworklog.tools.registry import ToolRegistry, serialize

LIMIT_MESSAGE = (
    "Tool-call limit reached. Do not call more tools. Answer now using only the evidence "
    "already gathered, and say what could not be checked."
)
_STUB_CHARS = 1_500


@dataclass
class AgentEvent:
    kind: str  # tool_call, tool_result, limit
    name: str = ""
    detail: str = ""


class Agent:
    def __init__(
        self,
        llm: ChatBackend,
        registry: ToolRegistry,
        system_prompt: str,
        max_tool_calls: int = 20,
        limits: Limits | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ):
        self.llm = llm
        self.registry = registry
        self.max_tool_calls = max_tool_calls
        self.limits = limits or Limits()
        self.on_event = on_event or (lambda event: None)
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    def ask(self, user_input: str) -> str:
        """Run one user turn to completion and return the final answer."""
        self.messages.append({"role": "user", "content": user_input})
        self._compact_history()
        tools = self.registry.definitions
        calls_made = 0
        while calls_made < self.max_tool_calls:
            reply = self.llm.chat(self.messages, tools=tools)
            self.messages.append(reply.to_message())
            if not reply.tool_calls:
                return (reply.content or "").strip() or "(The model returned an empty answer.)"
            for call in reply.tool_calls:
                calls_made += 1
                self.on_event(AgentEvent("tool_call", call.name, call.arguments))
                if calls_made > self.max_tool_calls:
                    result: dict[str, Any] = {"ok": False, "error": "Tool-call limit reached"}
                else:
                    result = self.registry.dispatch(call.name, call.arguments)
                self.on_event(
                    AgentEvent(
                        "tool_result",
                        call.name,
                        "ok" if result.get("ok") else str(result.get("error")),
                    )
                )
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": serialize(result, self.limits),
                    }
                )
        self.on_event(AgentEvent("limit", detail=f"{self.max_tool_calls} tool calls"))
        self.messages.append({"role": "user", "content": LIMIT_MESSAGE})
        reply = self.llm.chat(self.messages, tools=tools, tool_choice="none")
        self.messages.append({"role": "assistant", "content": reply.content or ""})
        answer = (reply.content or "").strip() or "(No answer produced.)"
        return f"{answer}\n\n_Note: stopped after the {self.max_tool_calls}-tool-call limit._"

    # -- context control -----------------------------------------------------------------

    def _user_indices(self) -> list[int]:
        return [i for i, m in enumerate(self.messages) if m["role"] == "user"]

    def _size(self) -> int:
        return sum(len(json.dumps(m, default=str)) for m in self.messages)

    def _compact_history(self) -> None:
        """Shrink old tool output; drop whole old turns if still over budget.

        Turns are dropped from one user message up to the next, so an assistant tool_call is
        never separated from its tool results.
        """
        users = self._user_indices()
        current = users[-1] if users else len(self.messages)
        for message in self.messages[1:current]:
            if message["role"] == "tool" and len(message["content"]) > _STUB_CHARS:
                message["content"] = message["content"][:_STUB_CHARS] + " ...[older output trimmed]"
        while self._size() > self.limits.max_history_chars:
            users = self._user_indices()
            if len(users) < 2:
                break
            del self.messages[users[0] : users[1]]

    def reset(self) -> None:
        del self.messages[1:]
