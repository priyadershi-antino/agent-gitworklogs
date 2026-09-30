"""Feature-wise commit splitting."""

import pytest

from gitworklog.safety import OperationDenied, ScriptedApprover
from gitworklog.services.commits import (
    MessageLimits,
    execute_commit,
    fit_body,
    group_files,
    length_problems,
    plan_commit,
)
from gitworklog.tools.git import GitRunner
from helpers import FakeLLM

RIDER = ["src/rider/service.py", "tests/test_rider.py"]
AUTH = ["src/auth/tokens.py", "docs/auth.md"]


@pytest.fixture
def two_features(repo):
    """Uncommitted changes for two unrelated features plus a README tweak."""
    repo.commit("init", {p: "v1\n" for p in [*RIDER, *AUTH, "README.md"]})
    for path in [*RIDER, *AUTH, "README.md"]:
        repo.write(path, "v2\n")
    return repo


def _committed_files(repo, rev):
    return set(repo.git("show", "--name-only", "--format=", rev).split())


def test_group_files_by_feature():
    groups = group_files([*RIDER, *AUTH, "README.md"])
    assert sorted(map(sorted, groups)) == sorted([sorted([*RIDER, "README.md"]), sorted(AUTH)])


def test_related_changes_stay_in_one_group():
    files = ["backend/rider/api.py", "frontend/rider/Profile.jsx", "tests/test_rider_api.py"]
    assert group_files(files) == [files]


def test_plan_without_llm_splits_unrelated_changes(two_features):
    plan = plan_commit(GitRunner(two_features.path))
    assert len(plan.groups) == 2
    rider, auth = (next(g for g in plan.groups if f in g.files) for f in (RIDER[0], AUTH[0]))
    assert rider is not auth
    assert set(RIDER) <= set(rider.files) and set(AUTH) <= set(auth.files)
    assert "README.md" in rider.files + auth.files  # generic file joins one feature
    assert all(not length_problems(g.message) for g in plan.groups)
    assert any("split into 2 commits" in n for n in plan.notes)


def test_single_keeps_everything_together(two_features):
    plan = plan_commit(GitRunner(two_features.path), split=False)
    assert len(plan.groups) == 1 and len(plan.groups[0].files) == 5


def test_execute_creates_one_verified_commit_per_feature(two_features):
    git = GitRunner(two_features.path)
    plan = plan_commit(git)
    approver = ScriptedApprover([True])
    results = execute_commit(git, approver, plan)
    assert len(approver.asked) == 1  # one approval for the whole plan
    op = approver.asked[0]
    assert len(op.all_commands) == 4  # add + commit for each feature
    assert "Commit 2/2" in op.preview
    assert len(results) == 2 and all(r["verified"] and r["files_match"] for r in results)
    assert _committed_files(two_features, "HEAD~1") == set(plan.groups[0].files)
    assert _committed_files(two_features, "HEAD") == set(plan.groups[1].files)
    assert two_features.git("status", "--porcelain").strip() == ""


def test_rejected_plan_changes_nothing(two_features):
    git = GitRunner(two_features.path)
    before = two_features.head()
    with pytest.raises(OperationDenied):
        execute_commit(git, ScriptedApprover([False]), plan_commit(git))
    assert two_features.head() == before
    assert two_features.git("diff", "--cached", "--name-only").strip() == ""


def test_llm_split_is_validated(two_features):
    split = {
        "commits": [
            {"files": RIDER, "message": "feat(rider): add availability", "reason": "rider"},
            {"files": [*AUTH, "src/invented.py"], "message": "fix(auth): refresh tokens"},
        ]
    }  # README.md is missing from the model's plan
    llm = FakeLLM([split, {"message": "docs: update readme"}])
    plan = plan_commit(GitRunner(two_features.path), llm)
    assert [g.files for g in plan.groups] == [RIDER, AUTH, ["README.md"]]
    assert [g.message for g in plan.groups] == [
        "feat(rider): add availability",
        "fix(auth): refresh tokens",
        "docs: update readme",
    ]
    assert any("Ignored 1 file" in n for n in plan.notes)
    assert "ONE" in llm.calls[0]["messages"][0]["content"]  # the split prompt was used


def test_prestaged_changes_are_split_with_pathspecs(two_features):
    two_features.git("add", "-A")
    git = GitRunner(two_features.path)
    plan = plan_commit(git)
    assert plan.already_staged and len(plan.groups) == 2
    approver = ScriptedApprover([True])
    results = execute_commit(git, approver, plan)
    assert all(r["files_match"] for r in results)
    assert all("--" in cmd for cmd in approver.asked[0].all_commands)
    assert _committed_files(two_features, "HEAD~1") == set(plan.groups[0].files)
    assert _committed_files(two_features, "HEAD") == set(plan.groups[1].files)


def test_partial_staging_commits_index_as_one(two_features):
    two_features.git("add", "src/rider/service.py", "src/auth/tokens.py")
    two_features.write("src/rider/service.py", "v3\n")  # partly staged now
    plan = plan_commit(GitRunner(two_features.path))
    assert len(plan.groups) == 1
    assert set(plan.groups[0].files) == {"src/rider/service.py", "src/auth/tokens.py"}
    assert any("partly staged" in n for n in plan.notes)


def test_word_limits():
    long = "feat: add thing\n\n" + "\n".join(f"- word {i} " + "x " * 10 for i in range(6))
    assert any("words; the limit is 80" in p for p in length_problems(long))
    fitted = fit_body(long, MessageLimits())
    assert not length_problems(fitted) and fitted.startswith("feat: add thing")
    assert length_problems("feat: " + "w " * 20, MessageLimits(max_words=10))


def test_prompt_mentions_word_target(two_features):
    llm = FakeLLM(default={"message": "fix: x"})
    plan_commit(GitRunner(two_features.path), llm, limits=MessageLimits(target_words=12))
    system = llm.calls[0]["messages"][0]["content"]
    assert "about 12 words" in system and "at most 80 words" in system


def test_llm_duplicate_file_keeps_first_commit(two_features):
    split = {
        "commits": [
            {"files": RIDER, "message": "feat(rider): add availability"},
            {"files": [RIDER[0], *AUTH, "README.md"], "message": "fix(auth): refresh tokens"},
        ]
    }
    plan = plan_commit(GitRunner(two_features.path), FakeLLM([split]))
    assert [g.files for g in plan.groups] == [RIDER, [*AUTH, "README.md"]]
    assert any("kept each file in its first commit" in n for n in plan.notes)


def test_message_calls_use_low_reasoning_effort(two_features):
    calls = []

    class Recorder(FakeLLM):
        def chat(self, messages, tools=None, tool_choice=None, reasoning_effort=None):
            calls.append(reasoning_effort)
            return super().chat(messages, tools, tool_choice, reasoning_effort)

    plan_commit(GitRunner(two_features.path), Recorder(default={"message": "fix: x"}), split=False)
    assert calls == ["low"]
