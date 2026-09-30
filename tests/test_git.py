import pytest

from gitworklog.config import Limits
from gitworklog.safety import DenyAllApprover, OperationDenied, ScriptedApprover
from gitworklog.tools.git import (
    GitRunner,
    NotARepository,
    UnsafeGitCommand,
    build_protected_operation,
    git_branches,
    git_compare,
    git_diff,
    git_log,
    git_show,
    git_status,
    normalize_numstat_path,
    parse_numstat,
    parse_status_v2,
    run_protected_operation,
    validate_range,
    validate_read_only,
)
from helpers import build_sample_repo


@pytest.mark.parametrize(
    "args",
    [
        ["reset", "--hard"],
        ["push"],
        ["push", "--force"],
        ["clean", "-f"],
        ["clean", "-fd"],
        ["branch", "-D", "x"],
        ["branch", "newbranch"],
        ["branch", "-m", "a", "b"],
        ["checkout", "--", "a.py"],
        ["rebase", "main"],
        ["merge", "x"],
        ["remote", "add", "o", "u"],
        ["config", "user.name", "x"],
        ["stash", "drop"],
        ["commit", "-m", "x"],
        ["-c", "x", "log"],
        ["diff", "--output=file.txt"],
        ["diff", "--ext-diff"],
        [],
    ],
)
def test_read_only_allowlist_rejects_mutations(args):
    with pytest.raises(UnsafeGitCommand):
        validate_read_only(args)


@pytest.mark.parametrize(
    "args",
    [
        ["status"],
        ["log", "-1"],
        ["branch", "--list"],
        ["branch", "--show-current"],
        ["branch", "--no-merged", "main", "--format=%(refname:short)"],
        ["clean", "-n", "-d"],
        ["remote", "-v"],
        ["config", "--get", "user.email"],
        ["stash", "list"],
    ],
)
def test_read_only_allowlist_accepts_reads(args):
    validate_read_only(args)


def test_runner_refuses_destructive_command(repo):
    git = GitRunner(repo.path)
    with pytest.raises(UnsafeGitCommand):
        git.run(["reset", "--hard"])


def test_not_a_repository(tmp_path):
    with pytest.raises(NotARepository):
        GitRunner(tmp_path)


def test_validate_range():
    assert validate_range("main..HEAD") == "main..HEAD"
    assert validate_range("origin/main...HEAD") == "origin/main...HEAD"
    with pytest.raises(UnsafeGitCommand):
        validate_range("--all..HEAD")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/{old => new}/f.py", "src/new/f.py"),
        ("a.py => b.py", "b.py"),
        ("src/{ => sub}/f.py", "src/sub/f.py"),
        ("plain.py", "plain.py"),
    ],
)
def test_normalize_numstat_path(raw, expected):
    assert normalize_numstat_path(raw) == expected


def test_parse_numstat_handles_binary():
    changes = parse_numstat("3\t1\ta.py\n-\t-\timg.png\n")
    assert (changes[0].additions, changes[0].deletions) == (3, 1)
    assert changes[1].binary and changes[1].additions == 0


def test_parse_status_v2():
    text = "\0".join(
        [
            "# branch.oid abc123",
            "# branch.head feature/x",
            "# branch.upstream origin/feature/x",
            "# branch.ab +2 -1",
            "1 M. N... 100644 100644 100644 h1 h2 staged.py",
            "1 .M N... 100644 100644 100644 h1 h2 unstaged.py",
            "2 R. N... 100644 100644 100644 h1 h2 R100 new.py",
            "old.py",
            "u UU N... 100644 100644 100644 100644 h1 h2 h3 conflict.py",
            "? new file.txt",
            "",
        ]
    )
    status = parse_status_v2(text)
    assert status.branch == "feature/x" and status.upstream == "origin/feature/x"
    assert (status.ahead, status.behind) == (2, 1)
    assert [e.path for e in status.staged] == ["staged.py", "new.py", "conflict.py"]
    assert [e.path for e in status.unstaged] == ["unstaged.py", "conflict.py"]
    assert [e.path for e in status.untracked] == ["new file.txt"]
    assert [e.path for e in status.conflicted] == ["conflict.py"]
    assert status.entries[2].orig_path == "old.py"


def test_status_on_real_repo(repo):
    repo.commit("init", {"a.py": "a = 1\n"})
    repo.write("a.py", "a = 2\n")
    repo.write("b.py", "b = 1\n")
    repo.write("c.py", "c = 1\n")
    repo.git("add", "c.py")
    status = git_status(GitRunner(repo.path))
    assert status.branch == "main"
    assert [e.path for e in status.unstaged] == ["a.py"]
    assert [e.path for e in status.staged] == ["c.py"]
    assert [e.path for e in status.untracked] == ["b.py"]


def test_status_empty_repo(repo):
    status = git_status(GitRunner(repo.path))
    assert status.head is None and status.is_clean


def test_diff_masks_secrets_and_withholds_env(repo):
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    repo.commit("init", {"app.py": "x = 1\n"})
    repo.write(".env", "OLD=1\n")
    repo.git("add", "-f", ".env")
    repo.git("commit", "-q", "-m", "env")
    repo.write("app.py", f'API_KEY = "{secret}"\n')
    repo.write(".env", "NEW_SECRET=supersecretvalue\n")
    result = git_diff(GitRunner(repo.path))
    assert secret not in result["patch"] and "[REDACTED:" in result["patch"]
    assert "supersecretvalue" not in result["patch"]
    assert "content has been withheld" in result["patch"]
    assert {f["path"] for f in result["files"]} == {"app.py", ".env"}


def test_diff_budget_limits(repo):
    repo.commit("init", {"a.py": "a\n", "b.py": "b\n"})
    repo.write("a.py", "".join(f"line {i}\n" for i in range(500)))
    repo.write("b.py", "".join(f"line {i}\n" for i in range(500)))
    git = GitRunner(repo.path, limits=Limits(max_diff_chars=3000, max_diff_chars_per_file=2000))
    result = git_diff(git)
    assert result["truncated_files"] == ["a.py"]
    assert result["omitted_files"] == ["b.py"]
    assert len(result["patch"]) < 3000


def test_log_parses_commits(sample):
    commits = git_log(GitRunner(sample.path))
    assert len(commits) == 11
    newest = commits[0]
    assert newest.subject == "docs: document availability API"
    assert newest.files[0].path == "docs/api.md"
    assert newest.authored_at.utcoffset().total_seconds() == 5.5 * 3600
    only_other = git_log(GitRunner(sample.path), author="other@example.com")
    assert [c.subject for c in only_other] == ["feat(payments): add payout scheduling"]


def test_log_rename(repo):
    repo.commit("init", {"src/old.py": "x = 1\n" * 20})
    repo.git("mv", "src/old.py", "src/new.py")
    repo.git("commit", "-q", "-m", "rename")
    assert git_log(GitRunner(repo.path), max_count=1)[0].files[0].path == "src/new.py"


def test_show(sample):
    git = GitRunner(sample.path)
    info = git_show(git, "HEAD~1")
    assert info["subject"] == "wip"
    assert "Settings.jsx" in info["patch"]
    with pytest.raises(UnsafeGitCommand):
        git_show(git, "--all")


def test_compare_ahead_behind(tmp_path):
    repo = build_sample_repo(tmp_path / "r")
    repo.git("checkout", "-q", "-b", "feature/rider-profile")
    repo.commit("feat(profile): add avatar", {"frontend/Avatar.jsx": "x\n"})
    repo.commit("feat(profile): add bio", {"frontend/Bio.jsx": "x\n"})
    repo.git("checkout", "-q", "main")
    repo.commit("chore: bump version", {"VERSION": "2\n"})
    repo.git("checkout", "-q", "feature/rider-profile")
    git = GitRunner(repo.path)
    cmp = git_compare(git, "main")
    assert (cmp.ahead, cmp.behind) == (2, 1)
    assert {f.path for f in cmp.files} == {"frontend/Avatar.jsx", "frontend/Bio.jsx"}
    assert cmp.current == "feature/rider-profile"
    branches = git_branches(git)
    assert branches["current"] == "feature/rider-profile"
    assert {b["name"] for b in branches["local"]} == {"main", "feature/rider-profile"}


def test_protected_reset_denied_keeps_changes(repo):
    repo.commit("init", {"a.py": "a = 1\n"})
    repo.write("a.py", "a = 2  # precious work\n")
    git = GitRunner(repo.path)
    op = build_protected_operation(git, "reset_hard")
    assert op.destructive and op.command == ["reset", "--hard", "HEAD"]
    assert op.consequences and "a.py" in op.preview
    with pytest.raises(OperationDenied):
        run_protected_operation(git, DenyAllApprover(), op)
    assert "precious work" in (repo.path / "a.py").read_text()


def test_protected_restore_file_approved(repo):
    repo.commit("init", {"a.py": "a = 1\n"})
    repo.write("a.py", "a = 2\n")
    git = GitRunner(repo.path)
    approver = ScriptedApprover([True])
    op = build_protected_operation(git, "restore_file", paths=["a.py"])
    result = run_protected_operation(git, approver, op)
    assert result["ok"] and (repo.path / "a.py").read_text() == "a = 1\n"
    assert approver.asked[0].command == ["checkout", "--", "a.py"]


def test_protected_operation_validation(repo):
    repo.commit("init", {"a.py": "a\n"})
    git = GitRunner(repo.path)
    with pytest.raises(UnsafeGitCommand):
        build_protected_operation(git, "delete_branch", target="main")
    with pytest.raises(UnsafeGitCommand):
        build_protected_operation(git, "merge", target="--abort")
    with pytest.raises(UnsafeGitCommand):
        build_protected_operation(git, "restore_file", paths=["../outside.py"])
    with pytest.raises(UnsafeGitCommand):
        build_protected_operation(git, "format_disk")
