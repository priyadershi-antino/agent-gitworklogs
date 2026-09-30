import json
from types import SimpleNamespace

from gitworklog.agent import Agent
from gitworklog.config import Limits, Settings
from gitworklog.llm import AssistantMessage, LLMClient, ToolCall, complete_json, extract_json
from gitworklog.prompts import agent_system_prompt
from gitworklog.safety import DenyAllApprover
from gitworklog.tools.registry import ToolContext, build_registry
from helpers import FakeLLM


def _agent(git, llm, **kw):
    registry = build_registry(ToolContext(git=git, approver=DenyAllApprover()))
    return Agent(llm, registry, "system", **kw)


def test_agent_calls_tools_then_answers(sample_git):
    llm = FakeLLM([[("git_status", {}), ("git_log", {"max_count": 3})], "You are on main."])
    agent = _agent(sample_git, llm)
    assert agent.ask("what branch am I on?") == "You are on main."
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    status = json.loads(tool_msgs[0]["content"])
    assert status["ok"] and status["result"]["branch"] == "main"
    assert llm.calls[0]["tools"]  # tool definitions were sent


def test_agent_enforces_tool_call_limit(sample_git):
    llm = FakeLLM(default=[("git_status", {})])
    llm.replies = [[("git_status", {})]] * 3 + ["best effort answer"]
    agent = _agent(sample_git, llm, max_tool_calls=3)
    answer = agent.ask("loop forever")
    assert "best effort answer" in answer and "limit" in answer
    assert llm.calls[-1]["tool_choice"] == "none"
    assert sum(m["role"] == "tool" for m in agent.messages) == 3


def test_agent_reports_tool_errors_to_model(sample_git):
    llm = FakeLLM([[("no_such_tool", {})], [("read_file", {"path": "../../etc/passwd"})], "ok"])
    agent = _agent(sample_git, llm)
    agent.ask("x")
    results = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
    assert results[0]["ok"] is False and "Unknown tool" in results[0]["error"]
    assert results[1]["ok"] is False and "outside the repository" in results[1]["error"]


def test_agent_masks_secrets_in_tool_results(repo):
    from gitworklog.tools.git import GitRunner

    secret = "ghp_" + "b" * 36
    repo.commit("init", {"conf.py": f'TOKEN = "{secret}"\n'})
    agent = _agent(GitRunner(repo.path), FakeLLM([[("read_file", {"path": "conf.py"})], "done"]))
    agent.ask("read conf")
    assert secret not in json.dumps(agent.messages)


def test_agent_denied_mutation_is_reported(sample_git, sample):
    sample.write("README.md", "changed\n")
    llm = FakeLLM([[("git_add", {"paths": ["README.md"]})], "Not approved."])
    agent = _agent(sample_git, llm)
    agent.ask("stage readme")
    result = json.loads(next(m for m in agent.messages if m["role"] == "tool")["content"])
    assert result["denied"] is True
    assert sample.git("diff", "--cached", "--name-only").strip() == ""


def test_history_compaction_keeps_tool_pairs(sample_git):
    llm = FakeLLM(default="answer")
    agent = _agent(sample_git, llm, limits=Limits(max_history_chars=3000))
    for i in range(6):
        llm.replies = [[("git_log", {"max_count": 20})], f"answer {i}"]
        agent.ask(f"question {i}")
    assert agent.messages[0]["role"] == "system"
    assert sum(len(json.dumps(m)) for m in agent.messages) < 20000
    assert agent.messages[-1]["content"] == "answer 5"
    for i, m in enumerate(agent.messages):
        if m["role"] == "tool":  # every tool result still follows its tool_call
            prev = agent.messages[i - 1]
            assert prev["role"] in ("assistant", "tool")
    first_user = next(m for m in agent.messages if m["role"] == "user")
    assert first_user["content"] != "question 0"


def test_assistant_message_drops_extra_fields():
    msg = AssistantMessage(content=None, tool_calls=[ToolCall("id1", "git_status", "{}")])
    data = msg.to_message()
    assert set(data) == {"role", "content", "tool_calls"}
    assert data["tool_calls"][0]["function"]["name"] == "git_status"


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}
    assert extract_json("no json") is None
    assert complete_json(FakeLLM(["bad", '{"ok": true}']), "s", "u") == {"ok": True}


def test_llm_client_builds_request_and_parses_tool_calls():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="git_status", arguments=""))
        message = SimpleNamespace(content=None, tool_calls=[call], reasoning_content="hidden")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls")]
        )

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = LLMClient(Settings(api_key="x", model="m"), client=fake)
    reply = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert captured["model"] == "m" and captured["tool_choice"] == "auto"
    assert reply.tool_calls[0].name == "git_status" and reply.tool_calls[0].arguments == "{}"


def test_system_prompt_contains_rules():
    from datetime import date

    prompt = agent_system_prompt("repo", date(2026, 9, 30))
    assert "2026-09-30" in prompt and "hours" in prompt and "FACT" in prompt


def test_llm_client_strips_leaked_template_tokens_from_tool_names():
    def create(**kwargs):
        call = SimpleNamespace(
            id="c1",
            function=SimpleNamespace(name="git_status<|channel|>commentary", arguments="{}"),
        )
        message = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    reply = LLMClient(Settings(api_key="x"), client=fake).chat([])
    assert reply.tool_calls[0].name == "git_status"
