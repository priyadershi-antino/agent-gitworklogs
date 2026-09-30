"""The only module that talks to the OpenAI-compatible API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from gitworklog.config import Settings


class LLMError(Exception):
    """The model could not be reached or returned an unusable answer."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class AssistantMessage:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    def to_message(self) -> dict[str, Any]:
        """Rebuild a clean assistant message (drops provider-specific reasoning fields)."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return message


class ChatBackend(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AssistantMessage: ...


class LLMClient:
    """OpenAI-compatible chat client (NVIDIA-hosted by default)."""

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                base_url=settings.base_url,
                api_key=settings.require_api_key(),
                timeout=settings.request_timeout,
                max_retries=settings.max_retries,  # SDK retries connection errors, 429 and 5xx
            )
        self._client = client

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AssistantMessage:
        import openai

        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if reasoning_effort:  # "low" | "medium" | "high" (reasoning models such as gpt-oss)
            kwargs["reasoning_effort"] = reasoning_effort
        try:
            response = self._client.chat.completions.create(**kwargs)
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMError("LLM authentication failed: check NVIDIA_API_KEY") from exc
        except openai.APIError as exc:
            raise LLMError(f"LLM request failed: {exc.__class__.__name__}: {exc}") from exc
        if not response.choices:
            raise LLMError("LLM returned no choices")
        choice = response.choices[0]
        msg = choice.message
        calls = [
            ToolCall(
                id=c.id,
                name=_clean_tool_name(c.function.name),
                arguments=c.function.arguments or "{}",
            )
            for c in (msg.tool_calls or [])
            if getattr(c, "function", None) is not None
        ]
        return AssistantMessage(
            content=msg.content, tool_calls=calls, finish_reason=choice.finish_reason
        )


def _clean_tool_name(name: str | None) -> str:
    """Some models leak chat-template tokens into names, e.g. `git_status<|channel|>commentary`."""
    return (name or "").split("<|", 1)[0].strip()


def extract_json(text: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (handles ``` fences and prose)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start != -1:
        depth, in_str, escape = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                escape = ch == "\\" and not escape
                if ch == '"' and not escape:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def complete_json(
    llm: ChatBackend,
    system: str,
    user: str,
    retries: int = 1,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Ask for a JSON object; retry once with a reminder if the reply is not valid JSON."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    effort = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    for _ in range(retries + 1):
        reply = llm.chat(messages, **effort)
        data = extract_json(reply.content)
        if data is not None:
            return data
        messages += [
            reply.to_message(),
            {"role": "user", "content": "Reply with only a valid JSON object."},
        ]
    raise LLMError("LLM did not return valid JSON")
