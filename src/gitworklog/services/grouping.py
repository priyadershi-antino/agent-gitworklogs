"""Deterministic grouping of commits into logical tasks, with evidence retained."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

from gitworklog.models import Commit, Task

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<desc>.+)$"
)
_TYPE_ALIASES = {
    "feat": "feat",
    "feature": "feat",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "refactor": "refactor",
    "perf": "perf",
    "test": "test",
    "tests": "test",
    "docs": "docs",
    "doc": "docs",
    "style": "style",
    "build": "build",
    "ci": "ci",
    "chore": "chore",
    "revert": "revert",
}
_VERB_TYPES = {
    "add": "feat",
    "adds": "feat",
    "added": "feat",
    "implement": "feat",
    "implemented": "feat",
    "create": "feat",
    "created": "feat",
    "introduce": "feat",
    "support": "feat",
    "fix": "fix",
    "fixed": "fix",
    "fixes": "fix",
    "resolve": "fix",
    "resolved": "fix",
    "correct": "fix",
    "patch": "fix",
    "refactor": "refactor",
    "refactored": "refactor",
    "simplify": "refactor",
    "cleanup": "refactor",
    "clean": "refactor",
    "rename": "refactor",
    "restructure": "refactor",
    "extract": "refactor",
    "test": "test",
    "tests": "test",
    "document": "docs",
    "docs": "docs",
    "readme": "docs",
    "bump": "chore",
    "upgrade": "chore",
    "revert": "revert",
}
KIND_VERBS = {
    "feat": "Implemented",
    "fix": "Fixed",
    "refactor": "Refactored",
    "perf": "Improved performance of",
    "test": "Added tests for",
    "docs": "Updated documentation for",
    "style": "Cleaned up",
    "build": "Updated build configuration for",
    "ci": "Updated CI for",
    "chore": "Maintenance of",
    "revert": "Reverted changes in",
    "other": "Worked on",
}
STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "to",
    "for",
    "of",
    "in",
    "on",
    "with",
    "from",
    "by",
    "at",
    "into",
    "as",
    "is",
    "be",
    "it",
    "this",
    "that",
    "add",
    "adds",
    "added",
    "adding",
    "fix",
    "fixed",
    "fixes",
    "fixing",
    "update",
    "updated",
    "updates",
    "updating",
    "implement",
    "implemented",
    "remove",
    "removed",
    "change",
    "changed",
    "changes",
    "refactor",
    "improve",
    "improved",
    "new",
    "use",
    "make",
    "handle",
    "support",
    "some",
    "more",
    "minor",
    "small",
    "wip",
    "misc",
    "stuff",
    "code",
    "file",
    "files",
    "test",
    "tests",
    "spec",
    "api",
    "service",
    "services",
    "controller",
    "controllers",
    "component",
    "components",
    "util",
    "utils",
    "helper",
    "helpers",
    "index",
    "main",
    "src",
    "app",
    "lib",
    "py",
    "js",
    "ts",
    "jsx",
    "tsx",
    "model",
    "models",
    "view",
    "views",
    "route",
    "routes",
    "page",
    "pages",
    "init",
    "feat",
    "chore",
    "docs",
    "style",
    "config",
    "module",
    "class",
    "function",
    "method",
    "when",
    "not",
}
_GENERIC_FILES = {
    "readme.md",
    "changelog.md",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    ".gitignore",
    "cargo.lock",
    "go.sum",
    "go.mod",
    "__init__.py",
    "index.js",
    "index.ts",
    "requirements.txt",
    "uv.lock",
    "claude.md",
}
_ROOT_DIRS = {
    "src",
    "app",
    "lib",
    "libs",
    "packages",
    "source",
    "main",
    "java",
    "python",
    "tests",
    "test",
    "__tests__",
    "spec",
    "internal",
    "pkg",
    "cmd",
    "frontend",
    "backend",
    "server",
    "client",
    "web",
    "api",
    "controllers",
    "services",
    "models",
    "routes",
    "components",
    "pages",
    "views",
    "handlers",
    "utils",
    "hooks",
    "store",
    "schemas",
    "unit",
    "integration",
    "e2e",
}
VAGUE_SUBJECTS = {
    "wip",
    "fix",
    "fixes",
    "update",
    "updates",
    "changes",
    "misc",
    "stuff",
    "temp",
    "tmp",
    "test",
    "commit",
    "save",
    "work",
    "minor",
    "minor changes",
    "small fix",
    "cleanup",
    ".",
    "done",
    "final",
    "asdf",
    "changes made",
    "updated",
    "progress",
}

_TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$"
)
_FRONTEND_EXT = {".jsx", ".tsx", ".vue", ".svelte", ".css", ".scss", ".sass", ".less", ".html"}
_FRONTEND_DIRS = {"frontend", "client", "web", "ui", "components", "pages", "views", "public"}
_BACKEND_EXT = {
    ".py",
    ".go",
    ".java",
    ".rb",
    ".php",
    ".cs",
    ".rs",
    ".kt",
    ".scala",
    ".sql",
    ".ex",
    ".exs",
    ".c",
    ".cpp",
    ".h",
}
_CONFIG_EXT = {".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".lock", ".env.example"}
_DOC_EXT = {".md", ".rst", ".txt", ".adoc"}


def split_words(text: str) -> list[str]:
    """Split identifiers and prose into lowercase words (camelCase, snake_case, kebab-case)."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w]


def meaningful(words: list[str]) -> list[str]:
    return [w for w in words if len(w) > 2 and not w.isdigit() and w not in STOPWORDS]


def parse_subject(subject: str) -> tuple[str, str | None, str]:
    """Return (kind, scope, description) for a commit subject."""
    match = CONVENTIONAL_RE.match(subject.strip())
    if match and match.group("type").lower() in _TYPE_ALIASES:
        return (
            _TYPE_ALIASES[match.group("type").lower()],
            (match.group("scope") or "").strip().lower() or None,
            match.group("desc").strip(),
        )
    first = (split_words(subject) or [""])[0]
    return _VERB_TYPES.get(first, "other"), None, subject.strip()


def is_vague(subject: str) -> bool:
    _, _, desc = parse_subject(subject)
    cleaned = desc.strip().lower().rstrip(".!")
    return cleaned in VAGUE_SUBJECTS or len(cleaned) < 6 or not meaningful(split_words(cleaned))


def file_area(path: str) -> str:
    p = PurePosixPath(path.lower())
    parts = set(p.parts[:-1])
    if _TEST_RE.search(p.as_posix()):
        return "Tests"
    if p.suffix in _DOC_EXT or "docs" in parts:
        return "Docs"
    if p.as_posix().startswith((".github/", ".gitlab", ".circleci")) or p.name in {
        "dockerfile",
        "docker-compose.yml",
        "makefile",
        ".gitignore",
    }:
        return "Config/CI"
    if p.suffix in _FRONTEND_EXT or parts & _FRONTEND_DIRS:
        return "Frontend"
    if p.suffix in _BACKEND_EXT or p.suffix in {".js", ".ts", ".mjs", ".cjs"}:
        return "Backend"
    if p.suffix in _CONFIG_EXT:
        return "Config/CI"
    return "Other"


def file_module(path: str) -> str | None:
    """The most meaningful name for the part of the codebase a file belongs to."""
    parts = PurePosixPath(path.lower()).parts
    for directory in parts[:-1]:
        if directory not in _ROOT_DIRS and not directory.startswith("."):
            return directory
    words = meaningful(split_words(PurePosixPath(path).stem))
    return words[0] if words else None


def is_generic_file(path: str) -> bool:
    return PurePosixPath(path.lower()).name in _GENERIC_FILES


def commit_tokens(commit: Commit) -> set[str]:
    _, scope, desc = parse_subject(commit.subject)
    tokens = set(meaningful(split_words(desc)))
    if scope:
        tokens.update(meaningful(split_words(scope)) or [scope])
    for f in commit.files:
        if not is_generic_file(f.path):
            tokens.update(meaningful(split_words(PurePosixPath(f.path).stem)))
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(b)] = self.find(a)


def _clean_bullet(subject: str) -> str:
    _, _, desc = parse_subject(subject)
    desc = desc.strip().rstrip(".")
    return desc[:1].upper() + desc[1:] if desc else subject


def _files_bullet(commit: Commit) -> str:
    names = [PurePosixPath(f.path).name for f in commit.files[:3]]
    more = f" and {len(commit.files) - 3} more" if len(commit.files) > 3 else ""
    return f"Changes in {', '.join(names)}{more}" if names else "Commit with no file changes"


def _topic(commits: list[Commit]) -> str:
    # Count each keyword once per commit; "tokens"/"token" share a key.
    words: Counter[str] = Counter()
    order: dict[str, int] = {}
    display: dict[str, str] = {}
    for c in commits:
        if is_vague(c.subject):
            continue
        for w in dict.fromkeys(meaningful(split_words(parse_subject(c.subject)[2]))):
            key = w[:-1] if len(w) > 4 and w.endswith("s") else w
            words[key] += 1
            order.setdefault(key, len(order))
            display.setdefault(key, w)
    ranked = sorted(words, key=lambda w: (-words[w], order[w]))
    scopes = Counter(s for _, s, _ in (parse_subject(c.subject) for c in commits) if s)
    if scopes:
        scope = scopes.most_common(1)[0][0]
        recurring = [w for w in ranked if words[w] >= 2 and w not in scope.split()]
        return f"{scope} {display[recurring[0]]}" if recurring else scope
    if ranked:
        return " ".join(display[w] for w in sorted(ranked[:2], key=order.__getitem__))
    modules = Counter(m for c in commits for f in c.files if (m := file_module(f.path)))
    return modules.most_common(1)[0][0] if modules else "repository"


def _dominant(values: list[str], default: str) -> str:
    counts = Counter(v for v in values if v != "other")
    return counts.most_common(1)[0][0] if counts else default


def group_commits(commits: list[Commit], id_prefix: str = "T") -> list[Task]:
    """Group related commits into tasks. Commits are never invented or dropped."""
    ordered = sorted(commits, key=lambda c: c.authored_at)
    n = len(ordered)
    uf = _UnionFind(n)
    strong: set[int] = set()  # commits linked by scope or shared file
    weak: set[int] = set()
    info = []
    for c in ordered:
        _, scope, _ = parse_subject(c.subject)
        info.append(
            (scope, {f.path for f in c.files if not is_generic_file(f.path)}, commit_tokens(c))
        )
    for i in range(n):
        for j in range(i + 1, n):
            (si, fi, ti), (sj, fj, tj) = info[i], info[j]
            if si and sj and si != sj:
                continue  # distinct explicit scopes are the author's own task boundaries
            if (si and si == sj) or (fi & fj):
                uf.union(i, j)
                strong.update((i, j))
            elif _jaccard(ti, tj) >= 0.3:
                uf.union(i, j)
                weak.update((i, j))

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    tasks: list[Task] = []
    for number, members in enumerate(sorted(groups.values(), key=lambda m: m[0]), start=1):
        group = [ordered[i] for i in members]
        kinds = [parse_subject(c.subject)[0] for c in group]
        areas = [file_area(f.path) for c in group for f in c.files]
        non_test = [a for a in areas if a != "Tests"]
        kind = _dominant(kinds, "other")
        if areas and all(a == "Tests" for a in areas):
            kind = "test"
        elif areas and all(a == "Docs" for a in areas):
            kind = "docs"
        vague = [c for c in group if is_vague(c.subject)]
        topic = _topic(group)

        if len(group) == 1 and not vague:
            title = _clean_bullet(group[0].subject)
        else:
            title = f"{KIND_VERBS[kind]} {topic}"
        bullets: list[str] = []
        for c in group:
            bullet = _files_bullet(c) if c in vague else _clean_bullet(c.subject)
            if bullet.lower() not in (b.lower() for b in bullets) and bullet != title:
                bullets.append(bullet)

        if len(vague) == len(group):
            confidence = "Low"
        elif vague or (len(group) > 1 and not any(i in strong for i in members)):
            confidence = "Medium"
        else:
            confidence = "High"
        if len(group) == 1:
            rationale = "single commit"
        elif any(i in strong for i in members):
            rationale = "shared conventional-commit scope or shared files"
        else:
            rationale = "similar commit messages and file names"
        if any(i in weak for i in members) and any(i in strong for i in members):
            rationale += " (plus message similarity)"

        tasks.append(
            Task(
                id=f"{id_prefix}{number}",
                title=title,
                kind=kind,
                topic=topic,
                commits=group,
                bullets=bullets,
                confidence=confidence,
                area=_dominant(non_test or areas, "Other"),
                rationale=rationale,
            )
        )
    return tasks
