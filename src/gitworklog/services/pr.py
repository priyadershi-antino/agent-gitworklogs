"""Branch comparison and PR description generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from gitworklog.llm import ChatBackend, LLMError, complete_json
from gitworklog.models import BranchComparison, Task
from gitworklog.prompts import PR_SYSTEM
from gitworklog.services.grouping import file_area, group_commits
from gitworklog.tools.git import GitRunner, git_compare, git_diff

PR_DIFF_CHARS = 15_000
TESTS_NOT_RUN = "Tests were not executed by GitWorklog; run the test suite before merging."
_RISKY_HINTS = {
    "migration": "Database migrations changed; verify they are reversible and ordered.",
    "auth": "Authentication-related files changed; review access control carefully.",
    "config": "Configuration files changed; check environment-specific settings.",
    ".github/": "CI workflow files changed; confirm pipelines still pass.",
    "dockerfile": "Container build changed; rebuild and smoke-test the image.",
}


@dataclass
class Comparison:
    data: BranchComparison
    tasks: list[Task]


def compare_branches(git: GitRunner, base: str) -> Comparison:
    data = git_compare(git, base)
    return Comparison(data=data, tasks=group_commits(data.commits))


def render_comparison(cmp: Comparison) -> str:
    d = cmp.data
    lines = [
        f"Current branch: {d.current}",
        f"Base: {d.base}",
        f"Merge base: {d.merge_base[:8] if d.merge_base else 'none'}",
        f"Commits ahead: {d.ahead}",
        f"Commits behind: {d.behind}",
        f"Files changed: {len(d.files)} (+{d.additions} / -{d.deletions})",
    ]
    if cmp.tasks:
        lines += ["", "Major changes:"]
        lines += [f"- {t.title} ({len(t.commits)} commit(s))" for t in cmp.tasks[:15]]
    if d.ahead == 0:
        lines += ["", f"No unmerged commits: {d.current} has nothing that is not in {d.base}."]
    if d.behind:
        lines.append(f"Note: {d.base} has {d.behind} commit(s) not in {d.current}.")
    others = [b for b in d.unmerged_branches if b != d.current]
    if others:
        lines += ["", f"Other local branches not merged into {d.base}: {', '.join(others[:10])}"]
    return "\n".join(lines)


@dataclass
class PRDescription:
    title: str
    summary: str
    changes: list[str]
    testing: list[str]
    risks: list[str]
    comparison: Comparison
    generated_by_llm: bool = False
    notes: list[str] = field(default_factory=list)


def _testing_section(cmp: Comparison) -> list[str]:
    test_files = [f.path for f in cmp.data.files if file_area(f.path) == "Tests"]
    items = [TESTS_NOT_RUN]
    if test_files:
        items.append("Test files changed on this branch: " + ", ".join(test_files[:10]))
    else:
        items.append("No test files were changed on this branch.")
    return items


def _deterministic_risks(cmp: Comparison) -> list[str]:
    risks = []
    paths = " ".join(f.path.lower() for f in cmp.data.files)
    for hint, text in _RISKY_HINTS.items():
        if hint in paths:
            risks.append(text)
    if not any(file_area(f.path) == "Tests" for f in cmp.data.files):
        risks.append("No tests changed alongside the code changes.")
    if cmp.data.additions + cmp.data.deletions > 1000:
        risks.append("Large change set (>1000 lines); consider splitting for review.")
    if cmp.data.behind:
        risks.append(
            f"Branch is {cmp.data.behind} commit(s) behind {cmp.data.base}; rebase or "
            "merge before review may be needed."
        )
    return risks


def build_pr(git: GitRunner, base: str, llm: ChatBackend | None = None) -> PRDescription:
    cmp = compare_branches(git, base)
    if cmp.data.ahead == 0:
        raise ValueError(
            f"{cmp.data.current} has no commits ahead of {cmp.data.base}; nothing to describe."
        )
    main = cmp.tasks[0] if len(cmp.tasks) == 1 else None
    pr = PRDescription(
        title=main.title if main else f"{cmp.data.current}: {len(cmp.tasks)} logical change(s)",
        summary=(
            f"This branch contains {cmp.data.ahead} commit(s) changing "
            f"{len(cmp.data.files)} file(s) relative to {cmp.data.base}."
        ),
        changes=[
            f"{t.title}" + (f" — {'; '.join(t.bullets[:3])}" if t.bullets else "")
            for t in cmp.tasks
        ],
        testing=_testing_section(cmp),
        risks=_deterministic_risks(cmp),
        comparison=cmp,
    )
    if llm is None:
        return pr
    evidence = {
        "branch": cmp.data.current,
        "base": cmp.data.base,
        "commits_ahead": cmp.data.ahead,
        "files_changed": len(cmp.data.files),
        "additions": cmp.data.additions,
        "deletions": cmp.data.deletions,
        "tasks": [t.evidence_dict() for t in cmp.tasks],
        "observed_risks": pr.risks,
        "diff_excerpt": git_diff(git, base=cmp.data.base)["patch"][:PR_DIFF_CHARS],
    }
    try:
        data = complete_json(llm, PR_SYSTEM, json.dumps(evidence, indent=1))
    except LLMError as exc:
        pr.notes.append(f"LLM unavailable ({exc}); showing deterministic description.")
        return pr
    if isinstance(data.get("title"), str) and data["title"].strip():
        pr.title = data["title"].strip()[:120]
        pr.generated_by_llm = True
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        pr.summary = data["summary"].strip()
    if isinstance(data.get("changes"), list):
        changes = [c.strip() for c in data["changes"] if isinstance(c, str) and c.strip()]
        pr.changes = changes or pr.changes
    if isinstance(data.get("risks"), list):
        extra = [r.strip() for r in data["risks"] if isinstance(r, str) and r.strip()]
        pr.risks = pr.risks + [r for r in extra if r not in pr.risks]
    return pr


def render_pr(pr: PRDescription) -> str:
    lines = [f"# {pr.title}", "", "## Summary", pr.summary, "", "## Changes"]
    lines += [f"- {c}" for c in pr.changes]
    lines += ["", "## Testing", *[f"- {t}" for t in pr.testing]]
    lines += ["", "## Potential risks"]
    lines += [f"- {r}" for r in pr.risks] or ["- None identified from the diff"]
    d = pr.comparison.data
    lines += [
        "",
        f"_Evidence: {d.ahead} commit(s), {len(d.files)} file(s), +{d.additions}/"
        f"-{d.deletions} vs {d.base}. Risks are inferences to verify._",
        *pr.notes,
    ]
    return "\n".join(lines)
