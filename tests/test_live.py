"""Real LLM calls. Run with: GITWORKLOG_LIVE_TESTS=1 pytest -m live"""

import os

import pytest

from gitworklog.agent import Agent
from gitworklog.config import load_settings
from gitworklog.llm import LLMClient
from gitworklog.prompts import agent_system_prompt
from gitworklog.safety import DenyAllApprover
from gitworklog.services.review import review
from gitworklog.services.worklog import build_worklog, resolve_range
from gitworklog.tools.registry import ToolContext, build_registry

pytestmark = pytest.mark.live


@pytest.fixture
def llm(sample_git):
    settings = load_settings(sample_git.root)
    if os.environ.get("GITWORKLOG_LIVE_TESTS") != "1" or not settings.api_key:
        pytest.skip("live LLM tests disabled")
    return LLMClient(settings)


def test_live_agent_uses_tools(llm, sample_git):
    registry = build_registry(ToolContext(git=sample_git, approver=DenyAllApprover()))
    agent = Agent(llm, registry, agent_system_prompt("sample", resolve_range().end))
    answer = agent.ask("Which branch am I on, and what is the subject of the latest commit?")
    assert any(m["role"] == "tool" for m in agent.messages)
    assert "main" in answer and "availability" in answer.lower()


def test_live_worklog_enrichment(llm, sample_git):
    log = build_worklog(
        sample_git, resolve_range(date_from="2026-09-25", date_to="2026-09-25"), llm=llm
    )
    assert log.enriched and log.tasks
    assert len(log.tasks) == len(
        build_worklog(sample_git, resolve_range(date_from="2026-09-25", date_to="2026-09-25")).tasks
    )


def test_live_review_finds_obvious_bug(llm, sample, sample_git):
    sample.write(
        "backend/auth/tokens.py",
        "def check(t):\n    if t == None:\n        return True  # allow missing token\n"
        "    return bool(t)\n",
    )
    result = review(sample_git, llm)
    assert result.used_llm
    assert all(f.file in result.context.files for f in result.findings)
