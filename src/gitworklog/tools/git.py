"""Git access. `GitRunner` is the only code in the project that spawns `git`.

Read-only commands go through `GitRunner.run`, which enforces an allowlist. Anything that
changes the repository is described as a `ProposedOperation` and executed only via
`GitRunner.run_approved`, which requires human approval first.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gitworklog.config import Limits
from gitworklog.models import BranchComparison, Commit, FileChange, RepoStatus, StatusEntry
from gitworklog.safety import (
    Approver,
    ProposedOperation,
    is_sensitive_path,
    mask_secrets,
    require_approval,
    withheld_notice,
)


class GitError(Exception):
    """A git command failed or returned something unexpected."""


class NotARepository(GitError):
    pass


class UnsafeGitCommand(GitError):
    """A command was refused by the read-only allowlist or input validation."""


READ_ONLY_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "rev-list",
    "merge-base",
    "ls-files",
    "grep",
    "for-each-ref",
    "symbolic-ref",
    "check-ignore",
    "cat-file",
    "branch",
    "remote",
    "config",
    "stash",
    "clean",
    "describe",
    "name-rev",
}
MUTATING_SUBCOMMANDS = {
    "add",
    "commit",
    "push",
    "checkout",
    "branch",
    "merge",
    "rebase",
    "reset",
    "clean",
}
_FORBIDDEN_ARGS = ("--output", "--ext-diff", "-O", "--open-files-in-pager", "--exec")
_BRANCH_READ_FLAGS = {
    "--list",
    "-a",
    "--all",
    "-r",
    "--remotes",
    "-v",
    "-vv",
    "--verbose",
    "--show-current",
    "--merged",
    "--no-merged",
    "--contains",
    "--no-color",
    "--no-column",
}
_BRANCH_VALUE_FLAGS = {"--merged", "--no-merged", "--contains", "--list"}
_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9._/@{}~^+\-]+$")
_REMOTE_RE = re.compile(r"^(?!-)[A-Za-z0-9._\-]+$")
_RECORD, _FIELD, _END = "\x1e", "\x1f", "\x1d"
_LOG_FORMAT = (
    f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%ae{_FIELD}%aI{_FIELD}%D{_FIELD}%s{_FIELD}%b{_END}"
)


@dataclass
class GitResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def validate_read_only(args: list[str]) -> None:
    """Raise `UnsafeGitCommand` unless `args` is a read-only git invocation."""
    if not args or args[0].startswith("-"):
        raise UnsafeGitCommand("A git subcommand is required")
    sub, rest = args[0], args[1:]
    if sub not in READ_ONLY_SUBCOMMANDS:
        raise UnsafeGitCommand(f"git {sub} is not an allowed read-only command")
    for arg in rest:
        if arg == "--":
            break
        if any(arg == bad or arg.startswith(bad + "=") for bad in _FORBIDDEN_ARGS):
            raise UnsafeGitCommand(f"Argument {arg} is not allowed")
    if sub == "branch":
        prev = ""
        for arg in rest:
            if arg.startswith("-"):
                if arg not in _BRANCH_READ_FLAGS and not arg.startswith(("--format=", "--sort=")):
                    raise UnsafeGitCommand(f"git branch {arg} is not read-only")
            elif prev not in _BRANCH_VALUE_FLAGS:
                raise UnsafeGitCommand("git branch with a name would create a branch")
            prev = arg
    elif sub == "remote":
        if rest not in ([], ["-v"]) and not (len(rest) == 2 and rest[0] == "get-url"):
            raise UnsafeGitCommand("Only `git remote`, `-v` and `get-url` are allowed")
    elif sub == "config":
        if len(rest) != 2 or rest[0] != "--get":
            raise UnsafeGitCommand("Only `git config --get <key>` is allowed")
    elif sub == "stash":
        if rest[:1] != ["list"]:
            raise UnsafeGitCommand("Only `git stash list` is allowed")
    elif sub == "clean":
        allowed = {"-n", "--dry-run", "-d", "-x", "-X"}
        if not set(rest) <= allowed or not ({"-n", "--dry-run"} & set(rest)):
            raise UnsafeGitCommand("Only dry-run `git clean -n` is allowed")


def validate_ref(ref: str) -> str:
    if not ref or len(ref) > 200 or not _REF_RE.match(ref) or ".." in ref:
        raise UnsafeGitCommand(f"Invalid git reference: {ref!r}")
    return ref


def validate_range(rev_range: str) -> str:
    """Validate `a..b`, `a...b` or a single ref."""
    for sep in ("...", ".."):
        if sep in rev_range:
            left, right = rev_range.split(sep, 1)
            validate_ref(left)
            validate_ref(right)
            return rev_range
    return validate_ref(rev_range)


class GitRunner:
    """Runs git inside one repository."""

    def __init__(self, path: str | Path = ".", limits: Limits | None = None, timeout: float = 60):
        self.limits = limits or Limits()
        self.timeout = timeout
        start = Path(path).resolve()
        if not start.exists():
            raise NotARepository(f"Path does not exist: {start}")
        result = self._exec(["rev-parse", "--show-toplevel"], cwd=start)
        if not result.ok:
            raise NotARepository(f"Not a git repository: {start}")
        self.root = Path(result.stdout.strip()).resolve()

    # -- execution -----------------------------------------------------------------------

    def _exec(
        self, args: list[str], cwd: Path | None = None, stdin: str | None = None
    ) -> GitResult:
        cmd = [
            "git",
            "-C",
            str(cwd or self.root),
            "-c",
            "core.quotepath=false",
            "-c",
            "color.ui=never",
            *args,
        ]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"}
        try:
            proc = subprocess.run(
                cmd,
                input=stdin.encode("utf-8") if stdin is not None else None,
                capture_output=True,
                timeout=self.timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError("git executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out after {self.timeout}s") from exc
        return GitResult(
            args=args,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )

    def run(self, args: list[str], check: bool = True) -> GitResult:
        """Run a read-only git command (allowlisted)."""
        validate_read_only(args)
        result = self._exec(args)
        if check and not result.ok:
            raise GitError(
                f"git {' '.join(args[:3])} failed: {mask_secrets(result.stderr.strip())}"
            )
        return result

    def run_approved(self, op: ProposedOperation, approver: Approver) -> GitResult:
        """Run a mutating git command, but only after explicit human approval."""
        if not op.command or op.command[0] not in MUTATING_SUBCOMMANDS:
            raise UnsafeGitCommand(f"Unsupported mutating command: {op.command_text}")
        require_approval(approver, op)
        result = self._exec(op.command)
        if not result.ok:
            raise GitError(f"{op.command_text} failed: {mask_secrets(result.stderr.strip())}")
        return result

    # -- small helpers -------------------------------------------------------------------

    def rev_parse(self, ref: str) -> str | None:
        result = self.run(
            ["rev-parse", "--verify", "--quiet", f"{validate_ref(ref)}^{{commit}}"], check=False
        )
        return result.stdout.strip() or None if result.ok else None

    def head(self) -> str | None:
        return self.rev_parse("HEAD")

    def current_branch(self) -> str | None:
        result = self.run(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        return result.stdout.strip() or None if result.ok else None

    def git_dir(self) -> Path:
        path = Path(self.run(["rev-parse", "--git-dir"]).stdout.strip())
        return path if path.is_absolute() else (self.root / path).resolve()

    def resolve_path(self, path: str) -> Path:
        """Resolve a repo-relative path, refusing anything outside the repository."""
        if not path or path.startswith("-"):
            raise UnsafeGitCommand(f"Invalid path: {path!r}")
        resolved = (self.root / path).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise UnsafeGitCommand(f"Path is outside the repository: {path}")
        return resolved


# ------------------------------------------------------------------------------ utilities


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]", True


def normalize_numstat_path(raw: str) -> str:
    """Turn git's rename notation (`a => b`, `dir/{a => b}/f`) into the new path."""
    path = raw.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if "{" in path and " => " in path:
        path = re.sub(r"\{([^{}]*) => ([^{}]*)\}", lambda m: m.group(2), path)
        path = re.sub(r"/{2,}", "/", path).strip("/")
    elif " => " in path:
        path = path.split(" => ", 1)[1]
    return path


def parse_numstat(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        binary = added == "-" and deleted == "-"
        changes.append(
            FileChange(
                path=normalize_numstat_path(path),
                additions=0 if binary else int(added),
                deletions=0 if binary else int(deleted),
                binary=binary,
            )
        )
    return changes


def parse_status_v2(text: str) -> RepoStatus:
    """Parse `git status --porcelain=v2 --branch -z`."""
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead = behind = 0
    entries: list[StatusEntry] = []
    fields = text.split("\0")
    i = 0
    while i < len(fields):
        line = fields[i]
        i += 1
        if not line:
            continue
        if line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2]
            head = None if oid == "(initial)" else oid
        elif line.startswith("# branch.head "):
            name = line.split(" ", 2)[2]
            branch = None if name == "(detached)" else name
        elif line.startswith("# branch.upstream "):
            upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            _, _, a, b = line.split(" ")
            ahead, behind = int(a.lstrip("+")), int(b.lstrip("-"))
        elif line.startswith("1 "):
            parts = line.split(" ", 8)
            entries.append(StatusEntry(path=parts[8], index=parts[1][0], worktree=parts[1][1]))
        elif line.startswith("2 "):
            parts = line.split(" ", 9)
            orig = fields[i] if i < len(fields) else None
            i += 1
            entries.append(
                StatusEntry(path=parts[9], index=parts[1][0], worktree=parts[1][1], orig_path=orig)
            )
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            entries.append(StatusEntry(path=parts[10], index=parts[1][0], worktree=parts[1][1]))
        elif line.startswith("? "):
            entries.append(StatusEntry(path=line[2:], index="?", worktree="?"))
    return RepoStatus(
        branch=branch, head=head, upstream=upstream, ahead=ahead, behind=behind, entries=entries
    )


def parse_log(text: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in text.split(_RECORD):
        if not record.strip():
            continue
        header, _, numstat = record.partition(_END)
        parts = header.split(_FIELD, 6)
        if len(parts) != 7:
            continue
        sha, author, email, date, refs, subject, body = parts
        commits.append(
            Commit(
                sha=sha,
                author=author,
                email=email,
                authored_at=datetime.fromisoformat(date),
                subject=subject.strip(),
                body=body.strip(),
                refs=refs.strip(),
                files=parse_numstat(numstat),
            )
        )
    return commits


def split_patch(patch: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, section) pairs."""
    sections: list[tuple[str, str]] = []
    for chunk in re.split(r"(?m)^(?=diff --git )", patch):
        if not chunk.startswith("diff --git "):
            continue
        match = re.search(r"(?m)^\+\+\+ b/(.+)$", chunk) or re.search(r"(?m)^--- a/(.+)$", chunk)
        if match:
            path = match.group(1).strip()
        else:
            header = chunk.splitlines()[0]
            path = header.rsplit(" b/", 1)[-1].strip()
        sections.append((path.strip('"'), chunk))
    return sections


def budget_patch(patch: str, limits: Limits) -> dict[str, Any]:
    """Mask secrets, withhold sensitive files and cap a patch per file and in total."""
    out: list[str] = []
    omitted: list[str] = []
    truncated_files: list[str] = []
    total = 0
    for path, section in split_patch(patch):
        cut = False
        if is_sensitive_path(path):
            section = f"diff --git a/{path} b/{path}\n{withheld_notice(path)}\n"
        else:
            section, cut = truncate(mask_secrets(section), limits.max_diff_chars_per_file)
        if total + len(section) > limits.max_diff_chars:
            omitted.append(path)
            continue
        if cut:
            truncated_files.append(path)
        out.append(section)
        total += len(section)
    return {"patch": "".join(out), "truncated_files": truncated_files, "omitted_files": omitted}


# ------------------------------------------------------------------------ read operations


def git_status(git: GitRunner) -> RepoStatus:
    result = git.run(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"])
    return parse_status_v2(result.stdout)


def _diff_args(staged: bool, base: str | None) -> list[str]:
    if base:
        return [f"{validate_ref(base)}...HEAD"]
    return ["--cached"] if staged else []


def git_diff_numstat(
    git: GitRunner, staged: bool = False, base: str | None = None, paths: list[str] | None = None
) -> list[FileChange]:
    args = ["diff", "--numstat", *_diff_args(staged, base)]
    if paths:
        args += ["--", *_repo_paths(git, paths)]
    return parse_numstat(git.run(args).stdout)


def git_diff(
    git: GitRunner,
    staged: bool = False,
    base: str | None = None,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    """Unified diff (masked and size-limited) plus per-file stats.

    staged=True -> index vs HEAD; base -> base...HEAD; default -> unstaged changes.
    """
    rev = _diff_args(staged, base)
    path_args = ["--", *_repo_paths(git, paths)] if paths else []
    files = parse_numstat(git.run(["diff", "--numstat", *rev, *path_args]).stdout)
    patch = git.run(["diff", "--no-ext-diff", "-U3", *rev, *path_args]).stdout
    budget = budget_patch(patch, git.limits)
    return {
        "files": [
            {"path": f.path, "additions": f.additions, "deletions": f.deletions, "binary": f.binary}
            for f in files
        ],
        "total_additions": sum(f.additions for f in files),
        "total_deletions": sum(f.deletions for f in files),
        **budget,
    }


def git_log(
    git: GitRunner,
    *,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    all_branches: bool = False,
    rev_range: str | None = None,
    max_count: int | None = None,
    no_merges: bool = True,
    paths: list[str] | None = None,
) -> list[Commit]:
    """Commits with numstat. `author` is a case-insensitive substring of "name <email>"."""
    if git.head() is None:
        return []
    limit = min(max_count or git.limits.max_log_commits, git.limits.max_log_commits)
    args = ["log", _LOG_FORMAT, "--numstat", f"--max-count={limit}"]
    if no_merges:
        args.append("--no-merges")
    if all_branches:
        args.append("--all")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if rev_range:
        args.append(validate_range(rev_range))
    if paths:
        args += ["--", *_repo_paths(git, paths)]
    commits = parse_log(git.run(args).stdout)
    if author:
        needle = author.lower()
        commits = [c for c in commits if needle in f"{c.author} <{c.email}>".lower()]
    return commits


def git_show(git: GitRunner, commit: str) -> dict[str, Any]:
    ref = validate_ref(commit)
    commits = parse_log(git.run(["log", _LOG_FORMAT, "--numstat", "-1", ref]).stdout)
    if not commits:
        raise GitError(f"Commit not found: {commit}")
    info = commits[0]
    patch = git.run(["show", "--no-ext-diff", "--format=", "-U3", ref]).stdout
    return {**info.to_dict(), "sha": info.sha, **budget_patch(patch, git.limits)}


def git_branches(git: GitRunner) -> dict[str, Any]:
    fmt = "%1f".join(
        [
            "%(refname:short)",
            "%(upstream:short)",
            "%(upstream:track)",
            "%(committerdate:iso-strict)",
            "%(HEAD)",
        ]
    )
    result = git.run(["for-each-ref", f"--format={fmt}", "--sort=-committerdate", "refs/heads"])
    local = []
    for line in result.stdout.splitlines():
        name, upstream, track, date, is_head = (line.split("\x1f") + [""] * 5)[:5]
        local.append(
            {
                "name": name,
                "upstream": upstream or None,
                "tracking": track or None,
                "last_commit": date or None,
                "current": is_head == "*",
            }
        )
    remotes = git.run(["for-each-ref", "--format=%(refname:short)", "refs/remotes"])
    remote_names = [r for r in remotes.stdout.splitlines() if not r.endswith("/HEAD")]
    return {
        "current": git.current_branch(),
        "detached": git.current_branch() is None,
        "local": local,
        "remote_branches": remote_names[:100],
    }


def resolve_base(git: GitRunner, base: str) -> str:
    validate_ref(base)
    for candidate in (base, f"origin/{base}"):
        if git.rev_parse(candidate):
            return candidate
    if base == "main" and git.rev_parse("master"):
        return "master"
    raise GitError(f"Base branch not found: {base}")


def git_compare(git: GitRunner, base: str) -> BranchComparison:
    resolved = resolve_base(git, base)
    current = git.current_branch() or (git.head() or "HEAD")[:8]
    merge_base = git.run(["merge-base", resolved, "HEAD"], check=False).stdout.strip() or None
    counts = git.run(["rev-list", "--left-right", "--count", f"{resolved}...HEAD"]).stdout.split()
    behind, ahead = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    commits = git_log(git, rev_range=f"{resolved}..HEAD")
    files = parse_numstat(git.run(["diff", "--numstat", f"{resolved}...HEAD"]).stdout)
    unmerged = git.run(
        ["branch", "--no-merged", resolved, "--format=%(refname:short)"], check=False
    ).stdout.split()
    return BranchComparison(
        current=current,
        base=resolved,
        merge_base=merge_base,
        ahead=ahead,
        behind=behind,
        commits=commits,
        files=files,
        unmerged_branches=unmerged[:50],
    )


def current_user_email(git: GitRunner) -> str | None:
    result = git.run(["config", "--get", "user.email"], check=False)
    return result.stdout.strip() or None


def operation_in_progress(git: GitRunner) -> list[str]:
    """Merge/rebase/cherry-pick/revert states detected from the git directory."""
    gd = git.git_dir()
    markers = {
        "MERGE_HEAD": "merge",
        "rebase-merge": "rebase",
        "rebase-apply": "rebase",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
    }
    return sorted({name for marker, name in markers.items() if (gd / marker).exists()})


# ----------------------------------------------------------------------- write operations


def _repo_paths(git: GitRunner, paths: list[str]) -> list[str]:
    if not paths:
        raise UnsafeGitCommand("At least one path is required")
    return [git.resolve_path(p).relative_to(git.root).as_posix() for p in paths]


def stage_operation(git: GitRunner, paths: list[str]) -> ProposedOperation:
    rel = _repo_paths(git, paths)
    sensitive = [p for p in rel if is_sensitive_path(p)]
    if sensitive:
        raise UnsafeGitCommand(f"Refusing to stage potential secret files: {', '.join(sensitive)}")
    return ProposedOperation(
        kind="stage",
        summary=f"Stage {len(rel)} path(s) for commit",
        command=["add", "--", *rel],
        consequences=["Changes are added to the index (reversible with `git restore --staged`)."],
        preview="\n".join(rel),
    )


def commit_operation(message: str, paths: list[str] | None = None) -> ProposedOperation:
    """`git commit -m ...`; with `paths`, only those (already staged) paths are committed."""
    message = message.strip()
    if not message:
        raise UnsafeGitCommand("Commit message must not be empty")
    subject, _, body = message.partition("\n")
    command = ["commit", "-m", subject.strip()]
    if body.strip():
        command += ["-m", body.strip()]
    if paths:
        if any(not p or p.startswith("-") for p in paths):
            raise UnsafeGitCommand(f"Invalid commit paths: {paths!r}")
        command += ["--", *paths]
    return ProposedOperation(
        kind="commit",
        summary="Create a commit from the staged changes",
        command=command,
        consequences=[
            "A new commit is added to the current branch (local only, nothing is pushed)."
        ],
        preview=message,
    )


def git_add(git: GitRunner, approver: Approver, paths: list[str]) -> dict[str, Any]:
    op = stage_operation(git, paths)
    git.run_approved(op, approver)
    status = git_status(git)
    return {"ok": True, "staged": [e.path for e in status.staged]}


def git_commit(
    git: GitRunner, approver: Approver, message: str, paths: list[str] | None = None
) -> dict[str, Any]:
    """Commit staged changes (optionally only `paths`) after approval, then verify it."""
    status = git_status(git)
    if not status.staged:
        raise GitError("Nothing is staged; stage changes before committing")
    before = git.head()
    git.run_approved(commit_operation(message, paths), approver)
    after = git.head()
    if not after or after == before:
        raise GitError("Commit verification failed: HEAD did not change")
    info = git_log(git, max_count=1, no_merges=False)[0]
    return {
        "ok": True,
        "verified": True,
        "sha": info.short_sha,
        "subject": info.subject,
        "files": [f.path for f in info.files],
    }


PROTECTED_OPERATIONS = (
    "push",
    "restore_file",
    "delete_branch",
    "merge",
    "rebase",
    "reset_hard",
    "clean",
)


def _status_preview(git: GitRunner) -> str:
    status = git_status(git)
    d = status.to_dict()
    return (
        f"Branch: {d['branch'] or 'DETACHED'} | staged: {len(d['staged'])} | "
        f"unstaged: {len(d['unstaged'])} | untracked: {len(d['untracked'])}"
    )


def build_protected_operation(
    git: GitRunner,
    operation: str,
    *,
    target: str | None = None,
    paths: list[str] | None = None,
    remote: str = "origin",
    force: bool = False,
) -> ProposedOperation:
    """Describe a destructive/outward-facing operation. Nothing is executed here."""
    status_line = _status_preview(git)
    branch = git.current_branch()
    if operation == "push":
        if not _REMOTE_RE.match(remote):
            raise UnsafeGitCommand(f"Invalid remote: {remote!r}")
        if remote not in git.run(["remote"]).stdout.split():
            raise GitError(f"Remote not found: {remote}")
        ref = validate_ref(target or branch or "")
        cmd = ["push", remote, ref] + (["--force-with-lease"] if force else [])
        consequences = [f"Publishes local commits of {ref} to {remote} (visible to others)."]
        if force:
            consequences.append("Force push rewrites remote history; others' work may be lost.")
        return ProposedOperation(
            "push", f"Push {ref} to {remote}", cmd, consequences, status_line, destructive=force
        )
    if operation == "restore_file":
        rel = _repo_paths(git, paths or [])
        diff = git_diff(git, paths=rel)
        return ProposedOperation(
            "destructive",
            f"Discard uncommitted changes in {len(rel)} file(s)",
            ["checkout", "--", *rel],
            ["Unstaged changes in these files are permanently lost (git cannot recover them)."],
            f"{status_line}\n{diff['patch'][:4000]}",
            destructive=True,
        )
    if operation == "delete_branch":
        name = validate_ref(target or "")
        if name == branch:
            raise UnsafeGitCommand("Cannot delete the current branch")
        if not git.rev_parse(name):
            raise GitError(f"Branch not found: {name}")
        unmerged = git.run(["rev-list", "--count", f"HEAD..{name}"]).stdout.strip()
        return ProposedOperation(
            "destructive",
            f"Delete local branch {name}",
            ["branch", "-D" if force else "-d", name],
            [
                f"{unmerged} commit(s) on {name} are not in the current branch and may become "
                "unreachable."
            ],
            status_line,
            destructive=True,
        )
    if operation in ("merge", "rebase"):
        ref = validate_ref(target or "")
        if not git.rev_parse(ref):
            raise GitError(f"Reference not found: {ref}")
        incoming = git.run(["log", "--oneline", "-20", f"HEAD..{ref}"]).stdout
        consequences = (
            ["Creates a merge commit or fast-forwards; conflicts may need resolving."]
            if operation == "merge"
            else ["Rewrites the current branch's commits; unsafe if already pushed."]
        )
        return ProposedOperation(
            "destructive",
            f"{operation.title()} {ref} into {branch}",
            [operation, ref],
            consequences,
            f"{status_line}\nIncoming commits:\n{incoming}",
            destructive=True,
        )
    if operation == "reset_hard":
        ref = validate_ref(target or "HEAD")
        stat = git.run(["diff", "--stat", "HEAD"], check=False).stdout
        return ProposedOperation(
            "destructive",
            f"Hard reset {branch or 'HEAD'} to {ref}",
            ["reset", "--hard", ref],
            [
                "All uncommitted changes to tracked files are permanently lost.",
                "Commits after the target are removed from this branch.",
            ],
            f"{status_line}\n{stat}",
            destructive=True,
        )
    if operation == "clean":
        dry = git.run(["clean", "-n", "-d"]).stdout
        return ProposedOperation(
            "destructive",
            "Delete untracked files and directories",
            ["clean", "-f", "-d"],
            ["Untracked files are permanently deleted (they are not in git history)."],
            f"{status_line}\nWould remove:\n{dry}",
            destructive=True,
        )
    raise UnsafeGitCommand(f"Unknown protected operation: {operation}")


def run_protected_operation(git: GitRunner, approver: Approver, op: ProposedOperation) -> dict:
    """Execute an approved protected operation and report the resulting state."""
    result = git.run_approved(op, approver)
    return {
        "ok": True,
        "executed": op.command_text,
        "output": mask_secrets(result.stdout[-2000:]),
        "status_after": _status_preview(git),
    }
