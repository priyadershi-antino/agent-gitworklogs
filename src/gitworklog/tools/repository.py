"""High-level repository facts."""

from __future__ import annotations

import re
from typing import Any

from gitworklog.tools.git import GitRunner, current_user_email, git_status, operation_in_progress


def _strip_credentials(url: str) -> str:
    return re.sub(r"(?<=://)[^/@\s]+@", "", url)


def repository_info(git: GitRunner) -> dict[str, Any]:
    status = git_status(git)
    remotes: dict[str, str] = {}
    for line in git.run(["remote", "-v"]).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], _strip_credentials(parts[1]))
    head = git.head()
    info: dict[str, Any] = {
        "root": str(git.root),
        "name": git.root.name,
        "branch": status.branch,
        "detached": status.branch is None and head is not None,
        "head": head[:8] if head else None,
        "has_commits": head is not None,
        "upstream": status.upstream,
        "ahead": status.ahead,
        "behind": status.behind,
        "remotes": remotes,
        "user_email": current_user_email(git),
        "in_progress": operation_in_progress(git),
        "changed_files": len(status.entries),
    }
    if head:
        info["commit_count"] = int(git.run(["rev-list", "--count", "HEAD"]).stdout.strip())
        dates = git.run(["log", "--format=%aI", "--reverse", "--max-parents=0", "HEAD"]).stdout
        info["first_commit_date"] = (dates.split() or [None])[0]
        info["last_commit_date"] = git.run(["log", "-1", "--format=%aI"]).stdout.strip()
    return info
