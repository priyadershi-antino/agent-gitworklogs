"""Shared test helpers: temporary Git repositories and a scripted fake LLM."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from gitworklog.llm import AssistantMessage, ToolCall

DEV = ("Dev", "dev@example.com")
OTHER = ("Other", "other@example.com")


class Repo:
    def __init__(self, path: Path):
        self.path = path

    def git(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit(
        self,
        message: str,
        files: dict[str, str],
        when: str = "2026-09-25T10:00:00+05:30",
        author: tuple[str, str] = DEV,
    ) -> str:
        for rel, content in files.items():
            self.write(rel, content)
        self.git("add", "--", *files)
        env = {
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": author[0],
            "GIT_COMMITTER_EMAIL": author[1],
        }
        self.git("commit", "-q", "-m", message, env=env)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()


def init_repo(path: Path) -> Repo:
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo(path)
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.email", DEV[1])
    repo.git("config", "user.name", DEV[0])
    repo.git("config", "commit.gpgsign", "false")
    repo.git("config", "core.hooksPath", ".no-hooks")
    repo.git("config", "core.autocrlf", "false")
    return repo


def build_sample_repo(path: Path) -> Repo:
    """A small rider-app history across three days and two authors."""
    repo = init_repo(path)
    repo.commit(
        "chore: initial project setup",
        {"README.md": "# Rider app\n", ".gitignore": ".env\n"},
        when="2026-09-20T09:00:00+05:30",
    )
    repo.commit(
        "feat(payments): add payout scheduling",
        {"backend/payments/payouts.py": "x = 1\n"},
        when="2026-09-24T11:00:00+05:30",
        author=OTHER,
    )
    d = "2026-09-25T{}:00+05:30"
    repo.commit(
        "feat(rider): add availability endpoint",
        {
            "backend/controllers/riderController.py": "def availability():\n    return {}\n",
            "backend/services/riderService.py": "def set_availability():\n    pass\n",
        },
        when=d.format("09:15"),
    )
    repo.commit(
        "feat(rider): validate availability status",
        {"backend/services/riderService.py": "def set_availability(s):\n    return s\n"},
        when=d.format("10:40"),
    )
    repo.commit(
        "test(rider): add availability tests",
        {"tests/test_rider_availability.py": "def test_ok():\n    assert True\n"},
        when=d.format("11:05"),
    )
    repo.commit(
        "fix(auth): handle expired access tokens",
        {"backend/auth/tokens.py": "def check(t):\n    return t\n"},
        when=d.format("14:20"),
    )
    repo.commit(
        "fix(auth): return 401 on invalid token",
        {
            "backend/auth/tokens.py": "def check(t):\n    return bool(t)\n",
            "backend/auth/middleware.py": "def mw():\n    pass\n",
        },
        when=d.format("15:00"),
    )
    repo.commit(
        "feat(ui): add availability toggle to rider profile",
        {"frontend/src/components/RiderProfile.jsx": "export const P = () => null;\n"},
        when=d.format("16:30"),
    )
    repo.commit(
        "feat(ui): improve profile loading state",
        {"frontend/src/components/RiderProfile.jsx": "export const P = () => 1;\n"},
        when=d.format("17:45"),
    )
    repo.commit(
        "wip",
        {"frontend/src/pages/Settings.jsx": "export default 1;\n"},
        when="2026-09-26T10:00:00+05:30",
    )
    repo.commit(
        "docs: document availability API",
        {"docs/api.md": "# API\n"},
        when="2026-09-26T12:00:00+05:30",
    )
    return repo


class FakeLLM:
    """Scripted ChatBackend. Replies may be str (content), dict (JSON content),
    list[tuple[name, args]] (tool calls) or AssistantMessage."""

    def __init__(self, replies: list[Any] | None = None, default: Any = None):
        self.replies = list(replies or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def chat(
        self, messages, tools=None, tool_choice=None, reasoning_effort=None
    ) -> AssistantMessage:
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice}
        )
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, AssistantMessage):
            return reply
        if isinstance(reply, dict):
            return AssistantMessage(content=json.dumps(reply))
        if isinstance(reply, list):
            calls = [
                ToolCall(id=f"call_{len(self.calls)}_{i}", name=name, arguments=json.dumps(args))
                for i, (name, args) in enumerate(reply)
            ]
            return AssistantMessage(content=None, tool_calls=calls)
        return AssistantMessage(content=reply)
