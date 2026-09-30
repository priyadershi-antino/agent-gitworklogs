"""Repository-confined file access: read, search and approved edits."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from gitworklog.safety import (
    Approver,
    ProposedOperation,
    is_sensitive_path,
    mask_secrets,
    require_approval,
    withheld_notice,
)
from gitworklog.tools.git import GitError, GitRunner, UnsafeGitCommand, truncate


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def read_file(
    git: GitRunner, path: str, start_line: int = 1, end_line: int | None = None
) -> dict[str, Any]:
    """Read a text file inside the repository (secrets masked, size capped)."""
    resolved = git.resolve_path(path)
    rel = resolved.relative_to(git.root).as_posix()
    if is_sensitive_path(rel):
        return {"path": rel, "withheld": True, "content": withheld_notice(rel)}
    if not resolved.is_file():
        raise GitError(f"File not found: {rel}")
    data = resolved.read_bytes()
    if _is_binary(data):
        return {"path": rel, "binary": True, "size_bytes": len(data), "content": ""}
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(1, start_line)
    end = min(len(lines), end_line or len(lines))
    numbered = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, end + 1))
    content, cut = truncate(mask_secrets(numbered), git.limits.max_file_chars)
    return {
        "path": rel,
        "total_lines": len(lines),
        "start_line": start,
        "end_line": end,
        "truncated": cut,
        "content": content,
    }


def search_files(git: GitRunner, query: str, path_glob: str | None = None) -> dict[str, Any]:
    """Fixed-string search across tracked text files (git grep)."""
    if not query or len(query) > 200:
        raise UnsafeGitCommand("Query must be 1-200 characters")
    args = ["grep", "-n", "-I", "--fixed-strings", "-e", query]
    if path_glob:
        if path_glob.startswith("-"):
            raise UnsafeGitCommand(f"Invalid path filter: {path_glob!r}")
        args += ["--", path_glob]
    result = git.run(args, check=False)
    if result.returncode not in (0, 1):  # 1 == no matches
        raise GitError(f"git grep failed: {result.stderr.strip()}")
    matches, skipped = [], 0
    for line in result.stdout.splitlines():
        file_path, _, rest = line.partition(":")
        if is_sensitive_path(file_path):
            skipped += 1
            continue
        line_no, _, text = rest.partition(":")
        matches.append(
            {
                "file": file_path,
                "line": int(line_no) if line_no.isdigit() else None,
                "text": mask_secrets(text.strip())[:300],
            }
        )
    limit = git.limits.max_search_results
    return {
        "query": query,
        "total_matches": len(matches),
        "matches": matches[:limit],
        "truncated": len(matches) > limit,
        "sensitive_files_skipped": skipped,
    }


def _planned_edit(git: GitRunner, path: str, old: str, new: str) -> tuple[Path, str, str, str]:
    """Validate an edit and return (file, relative path, original, updated) text.

    Bytes are read and written untranslated so the file keeps its own line endings.
    """
    resolved = git.resolve_path(path)
    rel = resolved.relative_to(git.root).as_posix()
    if is_sensitive_path(rel):
        raise UnsafeGitCommand(f"Refusing to edit potential secret file: {rel}")
    if not resolved.is_file():
        raise GitError(f"File not found: {rel}")
    text = resolved.read_bytes().decode("utf-8")
    if "\r\n" in text:  # the model writes "\n"; match the file's CRLF convention
        old, new = (s.replace("\r\n", "\n").replace("\n", "\r\n") for s in (old, new))
    count = text.count(old) if old else 0
    if count != 1:
        raise GitError(f"`old` text must match exactly once in {rel} (found {count})")
    return resolved, rel, text, text.replace(old, new, 1)


def edit_operation(git: GitRunner, path: str, old: str, new: str) -> ProposedOperation:
    _, rel, text, updated = _planned_edit(git, path, old, new)
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(True), updated.splitlines(True), f"a/{rel}", f"b/{rel}"
        )
    )
    return ProposedOperation(
        kind="edit",
        summary=f"Edit {rel}",
        command=[],
        consequences=["The working-tree file is modified (not committed)."],
        preview=mask_secrets(diff.replace("\r\n", "\n")),
    )


def apply_edit(git: GitRunner, approver: Approver, path: str, old: str, new: str) -> dict:
    """Replace one exact snippet in a file after the human approves the diff."""
    op = edit_operation(git, path, old, new)
    require_approval(approver, op)
    resolved, rel, _, updated = _planned_edit(git, path, old, new)
    resolved.write_bytes(updated.encode("utf-8"))
    return {"ok": True, "path": rel, "diff": op.preview}
