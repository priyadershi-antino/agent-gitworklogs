"""End-to-end CLI tests on temporary repositories (deterministic, --no-llm)."""

import csv
import io

import pytest
from typer.testing import CliRunner

from gitworklog.cli import app
from helpers import build_sample_repo

runner = CliRunner()


def invoke(repo, *args, input=None, env=None):
    return runner.invoke(app, ["--repo", str(repo.path), "--no-llm", *args], input=input, env=env)


@pytest.fixture(autouse=True)
def no_piped_approval(monkeypatch):
    monkeypatch.delenv("GITWORKLOG_ALLOW_PIPED_APPROVAL", raising=False)


def test_status(sample):
    sample.write("README.md", "changed\n")
    result = invoke(sample, "status")
    assert result.exit_code == 0, result.output
    assert "Branch: main" in result.output and "Unstaged (1):" in result.output


def test_worklog_csv(sample):
    result = invoke(
        sample, "worklog", "--from", "2026-09-25", "--to", "2026-09-26", "--format", "csv"
    )
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.output)))
    assert rows and {r["date"] for r in rows} == {"2026-09-25", "2026-09-26"}


def test_worklog_normal_with_hours(sample):
    result = invoke(sample, "worklog", "--from", "2026-09-25", "--to", "2026-09-25", "--hours", "7")
    assert result.exit_code == 0, result.output
    assert "Hours: 7 [USER-PROVIDED]" in result.output
    assert "cannot reliably determine" in result.output


def test_worklog_bad_dates(sample):
    result = invoke(sample, "worklog", "--from", "2026-09-30", "--to", "2026-09-01")
    assert result.exit_code == 1 and "must not be after" in result.output


def test_worklog_output_file(sample, tmp_path):
    out = tmp_path / "log.txt"
    result = invoke(sample, "worklog", "--from", "2026-09-25", "--to", "2026-09-25", "-o", str(out))
    assert result.exit_code == 0 and "rider" in out.read_text(encoding="utf-8").lower()


def test_summary_and_standup(sample):
    assert invoke(sample, "summary", "--from", "2026-09-25").exit_code == 0
    result = invoke(sample, "standup", "--plan", "Pair on payouts")
    assert result.exit_code == 0 and "[User-provided]" in result.output


def test_health_exit_codes(sample, repo):
    assert invoke(sample, "health").exit_code == 0
    repo.commit("init", {"a.py": "a\n"})
    repo.write(".env", "X=1\n")  # not ignored -> fail
    result = invoke(repo, "health")
    assert result.exit_code == 2 and "NOT git-ignored" in result.output


def test_compare_and_pr(tmp_path):
    repo = build_sample_repo(tmp_path / "c")
    repo.git("checkout", "-q", "-b", "feature/x")
    repo.commit("feat(x): add thing", {"x.py": "x\n"})
    result = invoke(repo, "compare", "main")
    assert result.exit_code == 0 and "Commits ahead: 1" in result.output
    result = invoke(repo, "pr", "main")
    assert result.exit_code == 0 and "## Testing" in result.output
    assert "Tests were not executed" in result.output
    assert invoke(repo, "compare", "nope").exit_code == 1


def test_review_static(sample):
    sample.write("backend/auth/tokens.py", "def check(t):\n    breakpoint()\n    return t\n")
    result = invoke(sample, "review")
    assert result.exit_code == 0 and "[LOW]" in result.output


def test_commit_non_interactive_is_never_approved(sample):
    sample.write("README.md", "changed\n")
    before = sample.head()
    result = invoke(sample, "commit", input="y\n")
    assert result.exit_code == 0, result.output
    assert "NOT approved" in result.output and "cancelled" in result.output
    assert sample.head() == before


def test_commit_dry_run(sample):
    sample.write("README.md", "changed\n")
    result = invoke(sample, "commit", "--dry-run")
    assert result.exit_code == 0 and "Commit plan" in result.output
    assert "Approval required" not in result.output


def test_commit_with_explicit_piped_approval(sample):
    sample.write("README.md", "changed\n")
    before = sample.head()
    env = {"GITWORKLOG_ALLOW_PIPED_APPROVAL": "1"}
    rejected = invoke(sample, "commit", "-m", "docs: update readme", input="n\n", env=env)
    assert "cancelled" in rejected.output and sample.head() == before
    result = invoke(sample, "commit", "-m", "docs: update readme", input="y\n", env=env)
    assert result.exit_code == 0, result.output
    assert "Committed and verified" in result.output
    assert sample.git("log", "-1", "--format=%s").strip() == "docs: update readme"


def test_ask_requires_llm(sample):
    result = invoke(sample, "ask", "hello")
    assert result.exit_code == 1 and "needs the LLM" in result.output


def test_not_a_repository(tmp_path):
    result = runner.invoke(app, ["--repo", str(tmp_path), "--no-llm", "status"])
    assert result.exit_code == 1 and "Not a git repository" in result.output


def test_no_command_starts_chat(sample):
    result = invoke(sample)  # --no-llm makes chat stop immediately, proving it was started
    assert result.exit_code == 1 and "needs the LLM" in result.output


def test_help_still_available(sample):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0 and "worklog" in result.output


def test_commit_rejects_too_long_message(sample):
    sample.write("README.md", "changed\n")
    before = sample.head()
    result = invoke(sample, "commit", "-m", "docs: " + "x" * 80)
    assert result.exit_code == 1 and "the limit is 72" in result.output
    assert sample.head() == before


def test_commit_splits_unrelated_changes(tmp_path):
    repo = build_sample_repo(tmp_path / "s")
    repo.write("backend/auth/tokens.py", "def check(t):\n    return t is not None\n")
    repo.write("frontend/src/pages/Settings.jsx", "export default 2;\n")
    before = repo.head()
    env = {"GITWORKLOG_ALLOW_PIPED_APPROVAL": "1"}
    result = invoke(repo, "commit", input="n\ny\n", env=env)
    assert result.exit_code == 0, result.output
    assert "[2/2]" in result.output and result.output.count("Committed and verified") == 2
    assert repo.git("rev-list", "--count", f"{before}..HEAD").strip() == "2"
    single = invoke(repo, "commit", "--single", "--dry-run")
    assert "Nothing" in single.output or "No committable" in single.output
