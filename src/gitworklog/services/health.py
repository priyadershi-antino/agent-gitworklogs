"""Repository health and hygiene checks (deterministic, no LLM)."""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from gitworklog.models import HealthCheck
from gitworklog.safety import find_secrets, is_sensitive_path
from gitworklog.tools.git import GitRunner, git_status, operation_in_progress, split_patch

GENERATED_PATTERNS = [
    "node_modules/*",
    "*/node_modules/*",
    "dist/*",
    "build/*",
    "*/__pycache__/*",
    "__pycache__/*",
    "*.pyc",
    ".DS_Store",
    "*/.DS_Store",
    "*.log",
    "coverage/*",
    ".pytest_cache/*",
    "*.egg-info/*",
    ".venv/*",
    "venv/*",
    "target/*",
    "*.class",
    "*.o",
    "*.so",
    "*.dll",
    "*.exe",
]
TEMP_PATTERNS = ["*.tmp", "*.bak", "*.swp", "*.swo", "*~", "*.orig", "*.rej", "Thumbs.db"]
DEBUG_RE = re.compile(
    r"console\.log\(|\bdebugger;|pdb\.set_trace\(\)|\bbreakpoint\(\)|"
    r"binding\.pry|var_dump\(|System\.out\.println\("
)
SECRET_SCAN_MAX_BYTES = 1024 * 1024
SECRET_SCAN_MAX_FILES = 3000


def _matches(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


def _ls_files(git: GitRunner) -> list[str]:
    return [p for p in git.run(["ls-files", "-z"]).stdout.split("\0") if p]


def _added_debug_lines(git: GitRunner) -> list[str]:
    if not git.head():
        return []
    hits = []
    for path, section in split_patch(git.run(["diff", "--no-ext-diff", "-U0", "HEAD"]).stdout):
        for line in section.splitlines():
            if line.startswith("+") and not line.startswith("+++") and DEBUG_RE.search(line):
                hits.append(f"{path}: {line[1:].strip()[:80]}")
    return hits


def run_health_checks(git: GitRunner) -> list[HealthCheck]:
    checks = [HealthCheck("Repository", "ok", f"Git repository detected at {git.root}")]
    status = git_status(git)
    head = git.head()

    if head is None:
        checks.append(HealthCheck("Commits", "warn", "Repository has no commits yet"))
    if status.branch is None and head is not None:
        checks.append(HealthCheck("Branch", "warn", "HEAD is detached (not on a branch)"))
    else:
        checks.append(HealthCheck("Branch", "ok", f"On branch {status.branch}"))

    in_progress = operation_in_progress(git)
    if status.conflicted:
        checks.append(
            HealthCheck(
                "Merge conflicts",
                "fail",
                f"{len(status.conflicted)} conflicted file(s)",
                [e.path for e in status.conflicted],
            )
        )
    else:
        checks.append(HealthCheck("Merge conflicts", "ok", "No merge conflict detected"))
    if in_progress:
        checks.append(
            HealthCheck("Operation in progress", "warn", f"Unfinished: {', '.join(in_progress)}")
        )

    staged, unstaged, untracked = status.staged, status.unstaged, status.untracked
    if status.is_clean:
        checks.append(HealthCheck("Working tree", "ok", "Working tree is clean"))
    else:
        detail = (
            f"{len(staged)} staged, {len(unstaged)} unstaged, {len(untracked)} untracked file(s)"
        )
        checks.append(
            HealthCheck(
                "Working tree",
                "warn",
                f"Uncommitted changes: {detail}",
                [e.path for e in status.entries][:20],
            )
        )

    if status.upstream is None:
        checks.append(HealthCheck("Upstream", "info", "No upstream branch configured"))
    elif status.ahead and status.behind:
        checks.append(
            HealthCheck(
                "Upstream",
                "warn",
                f"Diverged from {status.upstream}: ahead {status.ahead}, behind {status.behind}",
            )
        )
    elif status.behind:
        checks.append(
            HealthCheck(
                "Upstream", "warn", f"Behind {status.upstream} by {status.behind} commit(s)"
            )
        )
    elif status.ahead:
        checks.append(
            HealthCheck(
                "Upstream",
                "info",
                f"Ahead of {status.upstream} by {status.ahead} unpushed commit(s)",
            )
        )
    else:
        checks.append(HealthCheck("Upstream", "ok", f"Up to date with {status.upstream}"))

    tracked = _ls_files(git)
    candidates = tracked + [e.path for e in untracked]
    large = []
    for path in candidates:
        full = git.root / path
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size > git.limits.large_file_bytes:
            large.append(f"{path} ({size / 1024 / 1024:.1f} MB)")
    checks.append(
        HealthCheck(
            "Large files",
            "warn" if large else "ok",
            f"{len(large)} file(s) over {git.limits.large_file_bytes // 2**20} MB"
            if large
            else "No large files detected",
            large[:20],
        )
    )

    generated = [p for p in tracked if _matches(p, GENERATED_PATTERNS)]
    checks.append(
        HealthCheck(
            "Generated files",
            "warn" if generated else "ok",
            f"{len(generated)} generated/build artifact(s) are tracked"
            if generated
            else "No tracked build artifacts",
            generated[:20],
        )
    )

    temp = [p for p in candidates if _matches(p, TEMP_PATTERNS)]
    checks.append(
        HealthCheck(
            "Temporary files",
            "warn" if temp else "ok",
            f"{len(temp)} temporary/backup file(s)" if temp else "No temporary files",
            temp[:20],
        )
    )

    checks.extend(_env_checks(git, tracked))
    checks.append(_secret_scan(git, tracked))

    debug = _added_debug_lines(git)
    checks.append(
        HealthCheck(
            "Debug artifacts",
            "warn" if debug else "ok",
            f"{len(debug)} debug statement(s) in uncommitted changes"
            if debug
            else "No debug statements in uncommitted changes",
            debug[:20],
        )
    )

    if not (git.root / ".gitignore").is_file():
        checks.append(HealthCheck(".gitignore", "warn", "No .gitignore file at repository root"))
    else:
        checks.append(HealthCheck(".gitignore", "ok", ".gitignore present"))
    stashes = git.run(["stash", "list"], check=False).stdout.splitlines()
    if stashes:
        checks.append(HealthCheck("Stashes", "info", f"{len(stashes)} stash entr(y/ies) saved"))
    return checks


def _env_checks(git: GitRunner, tracked: list[str]) -> list[HealthCheck]:
    checks = []
    tracked_sensitive = [p for p in tracked if is_sensitive_path(p)]
    if tracked_sensitive:
        checks.append(
            HealthCheck(
                "Secret files tracked",
                "fail",
                "Potential secret files are committed to Git (values withheld)",
                tracked_sensitive[:20],
            )
        )
    local = [p.name for p in git.root.iterdir() if p.is_file() and is_sensitive_path(p.name)]
    for name in sorted(set(local) - set(tracked_sensitive)):
        ignored = git.run(["check-ignore", "-q", name], check=False).returncode == 0
        if ignored:
            checks.append(
                HealthCheck(name, "warn", f"{name} file exists (git-ignored; value withheld)")
            )
        else:
            checks.append(
                HealthCheck(
                    name,
                    "fail",
                    f"{name} exists and is NOT git-ignored; it could be committed by accident",
                )
            )
    if not checks:
        checks.append(HealthCheck("Secret files", "ok", "No .env or key files found"))
    return checks


def _secret_scan(git: GitRunner, tracked: list[str]) -> HealthCheck:
    hits: list[str] = []
    scanned = 0
    for path in tracked[:SECRET_SCAN_MAX_FILES]:
        full = git.root / path
        try:
            if not full.is_file() or full.stat().st_size > SECRET_SCAN_MAX_BYTES:
                continue
            data = full.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        scanned += 1
        for hit in find_secrets(data.decode("utf-8", errors="replace")):
            hits.append(f"{path}:{hit.line} ({hit.kind})")
    if hits:
        return HealthCheck(
            "Secrets in tracked files",
            "fail",
            f"Potential secrets detected in {len({h.split(':')[0] for h in hits})} "
            "file(s). Values have been withheld.",
            hits[:30],
        )
    return HealthCheck(
        "Secrets in tracked files", "ok", f"No likely secrets in {scanned} tracked text file(s)"
    )


SYMBOLS = {"ok": "✓", "warn": "⚠", "fail": "✗", "info": "i"}


def render_health(checks: list[HealthCheck]) -> str:
    lines = ["Repository health", ""]
    for check in checks:
        lines.append(f"{SYMBOLS.get(check.status, '?')} {check.detail}")
        lines += [f"    - {item}" for item in check.items[:10]]
        if len(check.items) > 10:
            lines.append(f"    - ... {len(check.items) - 10} more")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines += ["", f"{fails} problem(s), {warns} warning(s)"]
    return "\n".join(lines)
