"""Code review: budgeted diff context, deterministic checks and validated LLM findings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from gitworklog.llm import ChatBackend, LLMError, complete_json
from gitworklog.models import SEVERITY_ORDER, Evidence, ReviewFinding, Severity
from gitworklog.prompts import REVIEW_SYSTEM
from gitworklog.safety import is_sensitive_path, withheld_notice
from gitworklog.services.grouping import file_area
from gitworklog.tools.git import (
    GitRunner,
    budget_patch,
    git_status,
    parse_numstat,
    resolve_base,
    split_patch,
)

_SKIP_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "cargo.lock",
    "go.sum",
    "uv.lock",
    "composer.lock",
    "gemfile.lock",
}
_SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".snap")
_DEBUG_RE = re.compile(r"console\.log\(|\bdebugger;|pdb\.set_trace\(\)|\bbreakpoint\(\)")
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class NothingToReview(ValueError):
    pass


@dataclass
class ReviewContext:
    scope: str
    files: list[str]
    patch: str
    additions: int = 0
    deletions: int = 0
    skipped_generated: list[str] = field(default_factory=list)
    truncated_files: list[str] = field(default_factory=list)
    omitted_files: list[str] = field(default_factory=list)

    @property
    def reviewed_files(self) -> list[str]:
        return [p for p, _ in split_patch(self.patch)]


def _skip(path: str) -> bool:
    name = PurePosixPath(path.lower()).name
    return name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES)


def _untracked_pseudo_diff(git: GitRunner, paths: list[str]) -> str:
    """Render untracked text files as new-file diffs so they can be reviewed too."""
    out = []
    for path in paths:
        if is_sensitive_path(path):
            out.append(f"diff --git a/{path} b/{path}\n{withheld_notice(path)}\n")
            continue
        full = git.root / path
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192] or len(data) > git.limits.max_diff_chars_per_file * 4:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        body = "\n".join("+" + line for line in lines)
        out.append(
            f"diff --git a/{path} b/{path}\nnew file (untracked)\n--- /dev/null\n"
            f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"
        )
    return "".join(out)


def build_review_context(
    git: GitRunner, staged: bool = False, base: str | None = None, include_untracked: bool = True
) -> ReviewContext:
    """Collect the diff to review: working tree (default), staged only, or branch vs base."""
    if base:
        resolved = resolve_base(git, base)
        rev, scope = [f"{resolved}...HEAD"], f"branch {git.current_branch()} vs {resolved}"
    elif staged:
        rev, scope = ["--cached"], "staged changes"
    else:
        rev = ["HEAD"] if git.head() else ["--cached"]
        scope = "uncommitted changes (staged + unstaged + untracked)"
    numstat = parse_numstat(git.run(["diff", "--numstat", *rev]).stdout)
    patch = git.run(["diff", "--no-ext-diff", "-U3", *rev]).stdout
    added = sum(f.additions for f in numstat)
    if not base and not staged and include_untracked:
        untracked = _untracked_pseudo_diff(git, [e.path for e in git_status(git).untracked])
        added += sum(len(list(iter_added_lines(s))) for _, s in split_patch(untracked))
        patch += untracked
    sections, skipped, files = [], [], []
    for path, section in split_patch(patch):
        files.append(path)
        if _skip(path):
            skipped.append(path)
        else:
            sections.append(section)
    if not files:
        raise NothingToReview(f"No changes to review ({scope})")
    budget = budget_patch("".join(sections), git.limits)
    return ReviewContext(
        scope=scope,
        files=files,
        patch=budget["patch"],
        additions=added,
        deletions=sum(f.deletions for f in numstat),
        skipped_generated=skipped,
        truncated_files=budget["truncated_files"],
        omitted_files=budget["omitted_files"],
    )


def iter_added_lines(section: str):
    """Yield (new_line_number, text) for each added line in one file's diff section."""
    line_no = 0
    for line in section.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            line_no = int(match.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            yield line_no, line[1:]
            line_no += 1
        elif line.startswith(" "):
            line_no += 1


def static_findings(ctx: ReviewContext) -> list[ReviewFinding]:
    """High-certainty checks that need no LLM."""
    findings: list[ReviewFinding] = []
    for path, section in split_patch(ctx.patch):
        if "The content has been withheld" in section:
            findings.append(
                ReviewFinding(
                    Severity.HIGH,
                    path,
                    "Potential secret file is part of the change",
                    "Committing credential files exposes secrets to anyone with repo access.",
                    "Remove it from the change, add it to .gitignore and rotate exposed values.",
                )
            )
            continue
        for line_no, text in iter_added_lines(section):
            if "[REDACTED:" in text:
                findings.append(
                    ReviewFinding(
                        Severity.CRITICAL,
                        path,
                        "Possible hardcoded secret (value withheld)",
                        "Committed secrets leak to everyone with repo access and stay in history.",
                        "Load it from an environment variable or secret manager and rotate it.",
                        str(line_no),
                    )
                )
            if text.startswith(("<<<<<<< ", ">>>>>>> ")) or text == "=======":
                findings.append(
                    ReviewFinding(
                        Severity.CRITICAL,
                        path,
                        "Unresolved merge conflict marker",
                        "The file will not parse or will behave incorrectly.",
                        "Resolve the conflict and remove the markers.",
                        str(line_no),
                    )
                )
            if _DEBUG_RE.search(text):
                findings.append(
                    ReviewFinding(
                        Severity.LOW,
                        path,
                        f"Debug statement left in code: {text.strip()[:60]}",
                        "Debug output/breakpoints should not ship.",
                        "Remove it or use a logger.",
                        str(line_no),
                    )
                )
            if _TODO_RE.search(text):
                findings.append(
                    ReviewFinding(
                        Severity.SUGGESTION,
                        path,
                        f"New TODO/FIXME: {text.strip()[:80]}",
                        "Unfinished work can be forgotten.",
                        "Track it in an issue or resolve it.",
                        str(line_no),
                    )
                )
    code = [f for f in ctx.files if file_area(f) in ("Backend", "Frontend")]
    tests = [f for f in ctx.files if file_area(f) == "Tests"]
    if code and not tests and ctx.additions > 30:
        findings.append(
            ReviewFinding(
                Severity.SUGGESTION,
                code[0],
                f"{ctx.additions} lines added in {len(code)} code file(s) with no test changes",
                "Untested changes are more likely to regress.",
                "Add or update tests covering the new behaviour.",
                evidence=Evidence.INFERENCE,
            )
        )
    return findings


def _match_file(name: str, files: list[str]) -> str | None:
    name = name.strip().lstrip("./").removeprefix("a/").removeprefix("b/")
    if name in files:
        return name
    matches = [f for f in files if f.endswith("/" + name) or name.endswith("/" + f)]
    return matches[0] if len(matches) == 1 else None


@dataclass
class ReviewResult:
    context: ReviewContext
    findings: list[ReviewFinding]
    overall: str = ""
    used_llm: bool = False
    notes: list[str] = field(default_factory=list)


def llm_findings(llm: ChatBackend, ctx: ReviewContext) -> tuple[list[ReviewFinding], str, int]:
    """Ask the model for findings; drop anything not grounded in the reviewed diff."""
    user = f"Scope: {ctx.scope}\nFiles: {', '.join(ctx.files[:100])}\n\n```diff\n{ctx.patch}\n```"
    data = complete_json(llm, REVIEW_SYSTEM, user)
    reviewed = ctx.reviewed_files
    findings, dropped = [], 0
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).upper().strip()
        path = _match_file(str(item.get("file", "")), reviewed)
        problem = str(item.get("problem", "")).strip()
        if severity not in Severity.__members__ or path is None or not problem:
            dropped += 1
            continue
        evidence = (
            Evidence.INFERENCE
            if str(item.get("evidence", "")).upper() == "INFERENCE"
            else Evidence.FACT
        )
        findings.append(
            ReviewFinding(
                Severity(severity),
                path,
                problem,
                str(item.get("why", "")).strip(),
                str(item.get("fix", "")).strip(),
                str(item.get("lines", "")).strip(),
                evidence,
            )
        )
    overall = data.get("overall") if isinstance(data.get("overall"), str) else ""
    return findings, overall, dropped


def review(
    git: GitRunner, llm: ChatBackend | None, *, staged: bool = False, base: str | None = None
) -> ReviewResult:
    ctx = build_review_context(git, staged=staged, base=base)
    result = ReviewResult(context=ctx, findings=static_findings(ctx))
    if llm is not None:
        try:
            found, result.overall, dropped = llm_findings(llm, ctx)
            result.used_llm = True
            known = {(f.file, f.problem) for f in result.findings}
            result.findings += [f for f in found if (f.file, f.problem) not in known]
            if dropped:
                result.notes.append(
                    f"{dropped} model finding(s) discarded: they did not "
                    "reference a file in the reviewed diff."
                )
        except LLMError as exc:
            result.notes.append(f"LLM review unavailable ({exc}); showing static checks only.")
    else:
        result.notes.append("Static checks only (--no-llm).")
    if ctx.omitted_files:
        result.notes.append("Not reviewed (diff budget exceeded): " + ", ".join(ctx.omitted_files))
    if ctx.truncated_files:
        result.notes.append("Partially reviewed (truncated): " + ", ".join(ctx.truncated_files))
    if ctx.skipped_generated:
        result.notes.append("Skipped generated/lock files: " + ", ".join(ctx.skipped_generated))
    result.findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return result


def render_review(result: ReviewResult) -> str:
    ctx = result.context
    lines = [f"Review of {ctx.scope}: {len(ctx.files)} file(s), +{ctx.additions}/-{ctx.deletions}"]
    if not result.findings:
        lines += ["", "No meaningful issues found in the reviewed changes."]
    for i, f in enumerate(result.findings, 1):
        where = f"{f.file}:{f.lines}" if f.lines else f.file
        lines += ["", f"{i}. [{f.severity}] {where}  ({f.evidence})", f"   Problem: {f.problem}"]
        if f.why:
            lines.append(f"   Why it matters: {f.why}")
        if f.fix:
            lines.append(f"   Suggested fix: {f.fix}")
    if result.overall:
        lines += ["", f"Overall: {result.overall}"]
    if result.notes:
        lines += ["", *[f"Note: {n}" for n in result.notes]]
    return "\n".join(lines)
