import pytest

from gitworklog.safety import OperationDenied, ScriptedApprover
from gitworklog.services.commits import (
    NothingToCommit,
    execute_commit,
    is_valid_message,
    plan_commit,
)
from gitworklog.tools.git import GitRunner
from helpers import FakeLLM


@pytest.fixture
def dirty(repo):
    repo.commit("init", {"src/rider/service.py": "a = 1\n", ".gitignore": ""})
    repo.write("src/rider/service.py", "a = 2\n")
    repo.write("src/rider/new_helper.py", "b = 1\n")
    repo.write(".env", "SECRET_KEY=abcdefghijk\n")
    return repo


def test_plan_without_llm(dirty):
    plan = plan_commit(GitRunner(dirty.path))
    assert plan.to_stage == ["src/rider/service.py"]
    assert plan.sensitive_skipped == [".env"]
    assert plan.untracked_skipped == ["src/rider/new_helper.py"]
    assert plan.source == "heuristic" and is_valid_message(plan.message)
    assert "abcdefghijk" not in plan.context.patch


def test_plan_with_llm_message(dirty):
    llm = FakeLLM([{"message": "fix(rider): correct service default\n\n- set a to 2"}])
    plan = plan_commit(GitRunner(dirty.path), llm)
    assert plan.source == "llm" and plan.message.startswith("fix(rider):")
    diff_sent = llm.calls[0]["messages"][1]["content"]
    assert "service.py" in diff_sent and "abcdefghijk" not in diff_sent


def test_plan_rejects_invalid_llm_message(dirty):
    plan = plan_commit(GitRunner(dirty.path), FakeLLM(default={"message": "Updated stuff"}))
    assert plan.source == "heuristic"
    assert any("Conventional" in n for n in plan.notes)


def test_commit_approved_is_verified(dirty):
    git = GitRunner(dirty.path)
    before = dirty.head()
    plan = plan_commit(git, include_untracked=True)
    approver = ScriptedApprover([True])
    [result] = execute_commit(git, approver, plan, ["feat(rider): add helper"])
    assert len(approver.asked) == 1  # one explicit approval for stage + commit
    assert result["verified"] and dirty.head() != before
    assert set(result["files"]) == {"src/rider/service.py", "src/rider/new_helper.py"}
    assert ".env" not in dirty.git("show", "--name-only", "--format=", "HEAD")


def test_commit_rejected_changes_nothing(dirty):
    git = GitRunner(dirty.path)
    before = dirty.head()
    plan = plan_commit(git)
    with pytest.raises(OperationDenied):
        execute_commit(git, ScriptedApprover([False]), plan)
    assert dirty.head() == before
    assert dirty.git("diff", "--cached", "--name-only").strip() == ""  # nothing was staged


def test_commit_uses_existing_staged_files(dirty):
    dirty.git("add", "src/rider/new_helper.py")
    git = GitRunner(dirty.path)
    plan = plan_commit(git)
    assert plan.already_staged == ["src/rider/new_helper.py"] and plan.to_stage == []
    execute_commit(git, ScriptedApprover([True]), plan, ["feat(rider): add helper"])
    assert dirty.git("status", "--porcelain").count("service.py") == 1  # still unstaged


def test_nothing_to_commit(repo):
    repo.commit("init", {"a.py": "a\n"})
    with pytest.raises(NothingToCommit):
        plan_commit(GitRunner(repo.path))
    repo.write(".env", "X=1\n")
    with pytest.raises(NothingToCommit):
        plan_commit(GitRunner(repo.path))


@pytest.mark.parametrize(
    ("message", "valid"),
    [
        ("feat(rider): add x", True),
        ("fix: y", True),
        ("refactor(api)!: drop z", True),
        ("feat(riderService): add helper", True),
        ("feat(bad scope): x", False),
        ("Added stuff", False),
        ("feat: " + "x" * 80, False),
        ("", False),
    ],
)
def test_message_validation(message, valid):
    assert is_valid_message(message) is valid


def test_plan_warns_about_secret_in_added_lines(repo):
    repo.commit("init", {"conf.py": "x = 1\n"})
    repo.write("conf.py", "TOKEN = '" + "ghp_" + "e" * 36 + "'\n")
    plan = plan_commit(GitRunner(repo.path))
    assert any("potential secret" in n and "conf.py" in n for n in plan.notes)


# ---------------------------------------------------------------- message length limits

from gitworklog.services.commits import (  # noqa: E402
    CommitMessageError,
    MessageLimits,
    fit_body,
    length_problems,
    message_problems,
)


def test_subject_limit_counts_the_whole_first_line():
    ok = "feat(rider): " + "x" * (72 - len("feat(rider): "))
    assert len(ok) == 72 and not message_problems(ok)
    assert "73 characters; the limit is 72" in message_problems(ok + "y")[0]
    assert message_problems("feat: short", MessageLimits(subject_max=10))


def test_body_limits():
    long_body = "feat: x\n\n" + "\n".join(f"- change {i}" for i in range(10))
    assert any("Body has 10 lines" in p for p in length_problems(long_body))
    assert any("exceed 100 characters" in p for p in length_problems("fix: y\n\n- " + "z" * 120))
    assert length_problems("feat: x\n\n- a", MessageLimits(body_max_lines=0))


def test_fit_body_trims_body_but_never_the_subject():
    limits = MessageLimits(body_max_lines=2, body_line_max=40)
    message = "feat(a): keep this subject\n\n- one\n- " + "word " * 20 + "\n- three"
    fitted = fit_body(message, limits)
    lines = fitted.splitlines()
    assert lines[0] == "feat(a): keep this subject"
    assert len([line for line in lines[1:] if line]) == 2
    assert all(len(line) <= 40 for line in lines) and lines[-1].endswith("…")
    assert fit_body("fix: x\n\n- a\n- b", MessageLimits(body_max_lines=0)) == "fix: x"


def test_llm_long_subject_gets_one_retry(dirty):
    too_long = "fix(rider): " + "very long summary " * 6
    llm = FakeLLM([{"message": too_long}, {"message": "fix(rider): correct default value"}])
    plan = plan_commit(GitRunner(dirty.path), llm)
    assert plan.source == "llm" and plan.message == "fix(rider): correct default value"
    assert "was rejected" in llm.calls[1]["messages"][1]["content"]


def test_llm_still_too_long_falls_back_to_heuristic(dirty):
    too_long = {"message": "fix(rider): " + "very long summary " * 6}
    plan = plan_commit(GitRunner(dirty.path), FakeLLM([too_long, too_long]))
    assert plan.source == "heuristic" and is_valid_message(plan.message)
    assert any("characters; the limit is 72" in n for n in plan.notes)


def test_llm_long_body_is_trimmed(dirty):
    body = "\n".join(f"- detail {i}" for i in range(12))
    llm = FakeLLM([{"message": f"fix(rider): correct default\n\n{body}"}])
    plan = plan_commit(GitRunner(dirty.path), llm, limits=MessageLimits(body_max_lines=3))
    assert plan.source == "llm" and len(plan.message.splitlines()) == 5  # subject, blank, 3


def test_prompt_states_the_limits(dirty):
    llm = FakeLLM([{"message": "fix: x"}])
    plan_commit(GitRunner(dirty.path), llm, limits=MessageLimits(50, 0, 80))
    system = llm.calls[0]["messages"][0]["content"]
    assert "at most 50" in system and "subject line only" in system


def test_heuristic_respects_limits(dirty):
    plan = plan_commit(
        GitRunner(dirty.path),
        include_untracked=True,
        limits=MessageLimits(subject_max=30, body_max_lines=1),
    )
    assert not length_problems(plan.message, MessageLimits(subject_max=30, body_max_lines=1))


def test_too_long_message_is_refused_before_approval(dirty):
    git = GitRunner(dirty.path)
    before = dirty.head()
    approver = ScriptedApprover([True])
    with pytest.raises(CommitMessageError):
        execute_commit(git, approver, plan_commit(git), ["feat: " + "x" * 100])
    assert approver.asked == [] and dirty.head() == before
    assert dirty.git("diff", "--cached", "--name-only").strip() == ""
