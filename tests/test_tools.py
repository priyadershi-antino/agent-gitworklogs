import json

import pytest

from gitworklog.config import Limits
from gitworklog.safety import DenyAllApprover, ScriptedApprover
from gitworklog.tools.filesystem import apply_edit, read_file, search_files
from gitworklog.tools.git import GitError, GitRunner, UnsafeGitCommand
from gitworklog.tools.registry import ToolContext, build_registry, serialize
from gitworklog.tools.repository import repository_info

SECRET = "nvapi-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"


@pytest.fixture
def git(repo):
    repo.commit("init", {"app.py": f'KEY = "{SECRET}"\nprint("hi")\n', "bin.dat": "x"})
    repo.write("bin.dat", b"\x00\x01binary")
    repo.write(".env", f"NVIDIA_API_KEY={SECRET}\n")
    return GitRunner(repo.path)


def test_read_file_masks_and_confines(git):
    result = read_file(git, "app.py")
    assert SECRET not in result["content"] and "[REDACTED:" in result["content"]
    assert result["total_lines"] == 2
    with pytest.raises(UnsafeGitCommand):
        read_file(git, "../outside.txt")
    with pytest.raises(UnsafeGitCommand):
        read_file(git, str(git.root.parent / "x.py"))
    env = read_file(git, ".env")
    assert env["withheld"] and SECRET not in json.dumps(env)
    assert read_file(git, "bin.dat")["binary"]
    with pytest.raises(GitError):
        read_file(git, "missing.py")


def test_read_file_line_range(git):
    result = read_file(git, "app.py", start_line=2, end_line=2)
    assert result["content"].strip().startswith("2|")


def test_search_files_masks_results(git):
    result = search_files(git, "KEY")
    assert result["total_matches"] == 1
    assert SECRET not in json.dumps(result)
    assert search_files(git, "does-not-exist")["total_matches"] == 0
    with pytest.raises(UnsafeGitCommand):
        search_files(git, "")


def test_apply_edit_requires_approval(git):
    with pytest.raises(Exception, match="Not approved"):
        apply_edit(git, DenyAllApprover(), "app.py", 'print("hi")', 'print("bye")')
    assert 'print("hi")' in (git.root / "app.py").read_text()
    approver = ScriptedApprover([True])
    result = apply_edit(git, approver, "app.py", 'print("hi")', 'print("bye")')
    assert result["ok"] and 'print("bye")' in (git.root / "app.py").read_text()
    assert "-print" in approver.asked[0].preview
    with pytest.raises(UnsafeGitCommand):
        apply_edit(git, approver, ".env", "a", "b")


def test_repository_info(git):
    info = repository_info(git)
    assert info["branch"] == "main" and info["commit_count"] == 1
    assert info["user_email"] == "dev@example.com"


def _registry(git, approver=None):
    return build_registry(ToolContext(git=git, approver=approver or DenyAllApprover()))


def test_registry_definitions_have_no_shell_tool(git):
    names = {d["function"]["name"] for d in _registry(git).definitions}
    assert {
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_branch",
        "git_compare",
        "repository_info",
        "read_file",
        "search_files",
        "worklog_evidence",
        "health_check",
        "git_add",
        "git_commit",
        "protected_git_operation",
    } <= names
    assert not any("shell" in n or "exec" in n or "run" in n for n in names)


def test_registry_validation(git):
    reg = _registry(git)
    assert reg.dispatch("nope", "{}")["error"].startswith("Unknown tool")
    assert "Missing required" in reg.dispatch("read_file", "{}")["error"]
    assert "Unknown argument" in reg.dispatch("git_status", '{"x": 1}')["error"]
    assert "must be of type" in reg.dispatch("git_diff", '{"staged": "yes"}')["error"]
    assert "Invalid JSON" in reg.dispatch("git_status", "{bad")["error"]
    assert (
        "must be one of"
        in reg.dispatch("protected_git_operation", '{"operation": "rm_rf"}')["error"]
    )
    assert reg.dispatch("git_status", "")["ok"]
    assert reg.dispatch("git_log", '{"max_count": "2"}')["ok"]


def test_registry_mutations_are_denied_without_approval(git, repo):
    repo.write("app.py", "x = 2\n")
    reg = _registry(git)
    head = repo.head()
    result = reg.dispatch("git_add", json.dumps({"paths": ["app.py"]}))
    assert result["denied"] and not result["ok"]
    result = reg.dispatch("protected_git_operation", '{"operation": "reset_hard"}')
    assert result["denied"]
    assert (repo.path / "app.py").read_text() == "x = 2\n"
    assert repo.head() == head


def test_registry_commit_with_approval(git, repo):
    repo.write("app.py", "x = 2\n")
    reg = _registry(git, ScriptedApprover([True, True]))
    assert reg.dispatch("git_add", '{"paths": ["app.py"]}')["ok"]
    result = reg.dispatch("git_commit", '{"message": "refactor(app): simplify"}')
    assert result["ok"] and result["result"]["verified"]
    assert repo.git("log", "-1", "--format=%s").strip() == "refactor(app): simplify"


def test_registry_refuses_staging_secret_file(git):
    reg = _registry(git, ScriptedApprover([True]))
    result = reg.dispatch("git_add", '{"paths": [".env"]}')
    assert not result["ok"] and "secret" in result["error"]


def test_serialize_masks_and_truncates():
    text = serialize({"v": SECRET, "big": "x" * 500}, Limits(max_tool_output_chars=100))
    assert SECRET not in text and "truncated" in text


def test_restore_file_accepts_target_as_path(git, repo):
    repo.write("app.py", "changed\n")
    approver = ScriptedApprover([True])
    result = _registry(git, approver).dispatch(
        "protected_git_operation", '{"operation": "restore_file", "target": "app.py"}'
    )
    assert result["ok"] and approver.asked[0].command == ["checkout", "--", "app.py"]
    assert "changed" not in (repo.path / "app.py").read_text()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_apply_edit_preserves_line_endings(repo, newline):
    repo.commit("init", {"x.txt": "keep"})
    path = repo.write("m.py", f"a = 1{newline}b = 2{newline}".encode())
    git = GitRunner(repo.path)
    apply_edit(git, ScriptedApprover([True]), "m.py", "a = 1\nb = 2", "a = 1\nb = 3")
    assert path.read_bytes() == f"a = 1{newline}b = 3{newline}".encode()


def test_registry_commit_enforces_length_limit(git, repo):
    repo.write("app.py", "x = 3\n")
    repo.git("add", "app.py")
    approver = ScriptedApprover([True])
    result = _registry(git, approver).dispatch(
        "git_commit", json.dumps({"message": "fix: " + "y" * 90})
    )
    assert not result["ok"] and "the limit is 72" in result["error"]
    assert approver.asked == []
