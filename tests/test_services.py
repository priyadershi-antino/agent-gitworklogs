from datetime import date

import pytest

from gitworklog.config import Limits
from gitworklog.models import Severity
from gitworklog.services.health import render_health, run_health_checks
from gitworklog.services.pr import TESTS_NOT_RUN, build_pr, compare_branches, render_comparison
from gitworklog.services.review import NothingToReview, render_review, review
from gitworklog.services.summaries import (
    build_standup,
    build_summary,
    render_standup,
    render_summary,
)
from gitworklog.services.worklog import resolve_range
from gitworklog.tools.git import GitRunner
from helpers import FakeLLM, build_sample_repo

# --------------------------------------------------------------------------- summaries


def test_summary_groups_by_area(sample_git):
    rng = resolve_range(date_from="2026-09-25", date_to="2026-09-25")
    text = render_summary(build_summary(sample_git, rng))
    assert "Backend" in text and "Frontend" in text and "Bug fixes" in text
    assert "Commits: 7" in text
    assert text.index("Bug fixes") > text.index("Backend")


def test_standup_labels_and_no_invented_plans(sample_git):
    standup = build_standup(sample_git, today=date(2026, 9, 28))  # Monday after Sat 26th
    text = render_standup(standup)
    assert standup.previous_heading.startswith("Last active day (Sat Sep 26)")
    assert all(i.label == "Observed" for i in standup.previous)
    assert "No confirmed future work from Git history" in text
    assert "No blockers observable from repository evidence" in text


def test_standup_with_user_plan_and_uncommitted_work(sample_git, sample):
    sample.write("backend/auth/tokens.py", "changed\n")
    standup = build_standup(sample_git, today=date(2026, 9, 26), plan=["Write payout docs"])
    assert standup.previous_heading == "Yesterday (Sep 25)"
    labels = {(i.label, i.text.split(":")[0]) for i in standup.today}
    assert ("Observed", "Uncommitted work in progress") in labels
    assert ("User-provided", "Write payout docs") in labels
    assert ("Observed", "Committed so far") in labels


# --------------------------------------------------------------------------- compare / PR


@pytest.fixture
def feature_git(tmp_path):
    repo = build_sample_repo(tmp_path / "f")
    repo.git("checkout", "-q", "-b", "feature/rider-profile")
    repo.commit("feat(profile): add avatar upload", {"frontend/Avatar.jsx": "x\n"})
    repo.commit("feat(profile): add bio field", {"frontend/Avatar.jsx": "y\n"})
    repo.commit("test(profile): cover avatar", {"tests/test_avatar.py": "def test(): pass\n"})
    return GitRunner(repo.path)


def test_compare_render(feature_git):
    text = render_comparison(compare_branches(feature_git, "main"))
    assert "Current branch: feature/rider-profile" in text
    assert "Commits ahead: 3" in text and "Commits behind: 0" in text
    assert "Major changes:" in text


def test_pr_without_llm(feature_git):
    pr = build_pr(feature_git, "main")
    assert pr.testing[0] == TESTS_NOT_RUN
    assert "tests/test_avatar.py" in pr.testing[1]
    assert pr.changes and not pr.generated_by_llm


def test_pr_llm_cannot_claim_tests_ran(feature_git):
    llm = FakeLLM(
        [
            {
                "title": "feat(profile): avatar upload",
                "summary": "Adds avatars.",
                "changes": ["Avatar upload"],
                "risks": ["Large images"],
                "testing": ["All tests passed"],
            }
        ]
    )
    pr = build_pr(feature_git, "main", llm)
    assert pr.title == "feat(profile): avatar upload"
    assert "All tests passed" not in " ".join(pr.testing)
    assert "Large images" in pr.risks


def test_pr_nothing_ahead(sample_git):
    with pytest.raises(ValueError, match="no commits ahead"):
        build_pr(sample_git, "main")


# --------------------------------------------------------------------------- health


def _status(checks, name):
    return next(c for c in checks if c.name == name)


def test_health_clean_repo(sample_git):
    checks = run_health_checks(sample_git)
    assert _status(checks, "Merge conflicts").status == "ok"
    assert _status(checks, "Working tree").status == "ok"
    assert not any(c.status == "fail" for c in checks)


def test_health_detects_problems(repo):
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    repo.commit("init", {"app.py": f"aws = '{secret}'\n", "debug.log": "log\n"})
    repo.write(".env", "PASSWORD=supersecret\n")  # not ignored: no .gitignore
    repo.write("big.bin", b"\0" * 2048)
    repo.write("notes.tmp", "x")
    repo.write("app.py", f"aws = '{secret}'\nconsole.log('debug')\n")
    git = GitRunner(repo.path, limits=Limits(large_file_bytes=1024))
    checks = run_health_checks(git)
    assert _status(checks, ".env").status == "fail"
    assert _status(checks, "Large files").status == "warn"
    assert _status(checks, "Generated files").items == ["debug.log"]
    assert _status(checks, "Temporary files").items == ["notes.tmp"]
    assert _status(checks, "Secrets in tracked files").status == "fail"
    assert _status(checks, "Debug artifacts").status == "warn"
    assert _status(checks, ".gitignore").status == "warn"
    text = render_health(checks)
    assert secret not in text and "supersecret" not in text
    assert "app.py:1 (aws-access-key)" in text


def test_health_detects_conflict_and_ignored_env(repo):
    repo.commit("init", {"a.txt": "base\n", ".gitignore": ".env\n"})
    repo.write(".env", "X=1\n")
    repo.git("checkout", "-q", "-b", "other")
    repo.commit("other", {"a.txt": "other\n"})
    repo.git("checkout", "-q", "main")
    repo.commit("main", {"a.txt": "main\n"})
    repo.git("merge", "other", check=False)
    checks = run_health_checks(GitRunner(repo.path))
    assert _status(checks, "Merge conflicts").status == "fail"
    assert _status(checks, "Operation in progress").detail == "Unfinished: merge"
    assert _status(checks, ".env").status == "warn"


# --------------------------------------------------------------------------- review


def test_review_static_findings(repo):
    repo.commit("init", {"app.js": "const a = 1;\n"})
    token = "ghp_" + "c" * 36
    repo.write("app.js", f"const a = 1;\nconsole.log(a);\nconst t = '{token}';\n// TODO: x\n")
    result = review(GitRunner(repo.path), None)
    severities = [f.severity for f in result.findings]
    assert severities[0] == Severity.CRITICAL  # sorted most severe first
    assert Severity.LOW in severities and Severity.SUGGESTION in severities
    text = render_review(result)
    assert token not in text and "app.js:3" in text


def test_review_llm_findings_are_grounded(repo):
    repo.commit("init", {"calc.py": "def div(a, b):\n    return a\n"})
    repo.write("calc.py", "def div(a, b):\n    return a / b\n")
    llm = FakeLLM(
        [
            {
                "findings": [
                    {
                        "severity": "HIGH",
                        "file": "calc.py",
                        "lines": "2",
                        "problem": "Division by zero",
                        "why": "b may be 0",
                        "fix": "Guard b == 0",
                        "evidence": "FACT",
                    },
                    {"severity": "HIGH", "file": "made_up.py", "problem": "Invented issue"},
                    {"severity": "BLOCKER", "file": "calc.py", "problem": "bad severity"},
                ],
                "overall": "One real issue.",
            }
        ]
    )
    result = review(GitRunner(repo.path), llm)
    assert [f.problem for f in result.findings] == ["Division by zero"]
    assert any("2 model finding(s) discarded" in n for n in result.notes)


def test_review_no_issues_and_nothing_to_review(repo):
    repo.commit("init", {"a.py": "a = 1\n"})
    git = GitRunner(repo.path)
    with pytest.raises(NothingToReview):
        review(git, None)
    repo.write("a.py", "a = 2\n")
    result = review(git, FakeLLM([{"findings": [], "overall": "Fine."}]))
    assert "No meaningful issues found" in render_review(result)


def test_review_includes_untracked_but_withholds_env(repo):
    repo.commit("init", {"a.py": "a\n"})
    repo.write("new_module.py", "def f():\n    return 1\n")
    repo.write(".env", "API_TOKEN=abcdefghijk\n")
    result = review(GitRunner(repo.path), None)
    assert "new_module.py" in result.context.files
    assert "abcdefghijk" not in result.context.patch
    assert any(f.file == ".env" and f.severity == Severity.HIGH for f in result.findings)


def test_review_branch(feature_git):
    result = review(feature_git, None, base="main")
    assert "frontend/Avatar.jsx" in result.context.files
