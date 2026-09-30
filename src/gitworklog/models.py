"""Structured data passed between tools, services and the CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class Evidence(StrEnum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    SUGGESTION = "SUGGESTION"
    USER_PROVIDED = "USER-PROVIDED"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    SUGGESTION = "SUGGESTION"


SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}


@dataclass
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0
    binary: bool = False


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    authored_at: datetime
    subject: str
    body: str = ""
    refs: str = ""
    files: list[FileChange] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    @property
    def day(self) -> date:
        """Calendar day in the author's own timezone."""
        return self.authored_at.date()

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.short_sha,
            "author": self.author,
            "authored_at": self.authored_at.isoformat(),
            "subject": self.subject,
            "body": self.body[:500],
            "files": [f.path for f in self.files],
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class StatusEntry:
    path: str
    index: str  # staged state code (git porcelain X)
    worktree: str  # unstaged state code (git porcelain Y)
    orig_path: str | None = None

    @property
    def staged(self) -> bool:
        return self.index not in (".", " ", "?", "!")

    @property
    def unstaged(self) -> bool:
        return self.worktree not in (".", " ", "?", "!")

    @property
    def untracked(self) -> bool:
        return self.index == "?"

    @property
    def conflicted(self) -> bool:
        return "U" in (self.index + self.worktree) or (self.index + self.worktree) in ("AA", "DD")


@dataclass
class RepoStatus:
    branch: str | None  # None when HEAD is detached
    head: str | None  # None when there are no commits
    upstream: str | None
    ahead: int
    behind: int
    entries: list[StatusEntry] = field(default_factory=list)

    @property
    def staged(self) -> list[StatusEntry]:
        return [e for e in self.entries if e.staged]

    @property
    def unstaged(self) -> list[StatusEntry]:
        return [e for e in self.entries if e.unstaged]

    @property
    def untracked(self) -> list[StatusEntry]:
        return [e for e in self.entries if e.untracked]

    @property
    def conflicted(self) -> list[StatusEntry]:
        return [e for e in self.entries if e.conflicted]

    @property
    def is_clean(self) -> bool:
        return not self.entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "detached": self.branch is None,
            "head": self.head[:8] if self.head else None,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "staged": [e.path for e in self.staged],
            "unstaged": [e.path for e in self.unstaged],
            "untracked": [e.path for e in self.untracked],
            "conflicted": [e.path for e in self.conflicted],
            "clean": self.is_clean,
        }


@dataclass
class BranchComparison:
    current: str
    base: str
    merge_base: str | None
    ahead: int
    behind: int
    commits: list[Commit]
    files: list[FileChange]
    unmerged_branches: list[str] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "base": self.base,
            "merge_base": self.merge_base[:8] if self.merge_base else None,
            "ahead": self.ahead,
            "behind": self.behind,
            "commits": [c.to_dict() for c in self.commits[:50]],
            "files_changed": len(self.files),
            "additions": self.additions,
            "deletions": self.deletions,
            "files": [asdict(f) for f in self.files[:200]],
            "unmerged_local_branches": self.unmerged_branches,
        }


@dataclass
class Task:
    """A logical unit of work grouped from one or more commits."""

    id: str
    title: str
    kind: str  # feat, fix, refactor, test, docs, chore, other
    topic: str
    commits: list[Commit]
    bullets: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: str = "Medium"  # High / Medium / Low
    area: str = "Other"
    rationale: str = ""

    @property
    def files(self) -> list[str]:
        seen: dict[str, None] = {}
        for commit in self.commits:
            for f in commit.files:
                seen.setdefault(f.path, None)
        return list(seen)

    @property
    def weight(self) -> float:
        """Relative size of the task (for *suggested* hour splits only)."""
        lines = sum(c.additions + c.deletions for c in self.commits)
        return len(self.commits) + min(lines, 2000) / 200

    def evidence_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "topic": self.topic,
            "area": self.area,
            "confidence": self.confidence,
            "grouping_reason": self.rationale,
            "commits": [
                {
                    "sha": c.short_sha,
                    "subject": c.subject,
                    "body": c.body[:300],
                    "files": [f.path for f in c.files[:15]],
                }
                for c in self.commits
            ],
            "files": self.files[:40],
            "additions": sum(c.additions for c in self.commits),
            "deletions": sum(c.deletions for c in self.commits),
        }


@dataclass
class HealthCheck:
    name: str
    status: str  # ok / warn / fail / info
    detail: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class ReviewFinding:
    severity: Severity
    file: str
    problem: str
    why: str = ""
    fix: str = ""
    lines: str = ""
    evidence: Evidence = Evidence.FACT
