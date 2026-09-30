"""Commit assistant: group changes by feature, propose Conventional Commit messages, commit
only after one explicit approval, and verify every commit."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from gitworklog.config import Settings
from gitworklog.llm import ChatBackend, LLMError, complete_json
from gitworklog.models import RepoStatus
from gitworklog.prompts import commit_split_prompt, commit_system_prompt
from gitworklog.safety import (
    Approver,
    PreApproved,
    ProposedOperation,
    is_sensitive_path,
    require_approval,
)
from gitworklog.services.grouping import file_area, file_module, is_generic_file, split_words
from gitworklog.services.review import ReviewContext, build_review_context, iter_added_lines
from gitworklog.tools.git import (
    GitError,
    GitRunner,
    commit_operation,
    git_commit,
    git_status,
    split_patch,
    stage_operation,
)

CONVENTIONAL_SUBJECT_RE = re.compile(
    r"^(feat|fix|refactor|perf|test|docs|style|build|ci|chore|revert)"
    r"(\([\w./\-]+\))?!?: \S.*$"  # scopes may be camelCase, e.g. feat(riderService)
)


class NothingToCommit(ValueError):
    pass


class CommitMessageError(ValueError):
    """A commit message breaks the configured format or length limits."""


# ------------------------------------------------------------------------ message limits


@dataclass(frozen=True)
class MessageLimits:
    subject_max: int = 72  # whole first line, including "type(scope): "
    body_max_lines: int = 6  # 0 = subject line only
    body_line_max: int = 100
    max_words: int = 80  # whole message
    target_words: int = 15  # what the model should aim for

    @classmethod
    def from_settings(cls, settings: Settings) -> MessageLimits:
        return cls(
            settings.commit_subject_max,
            settings.commit_body_max_lines,
            settings.commit_body_line_max,
            settings.commit_max_words,
            settings.commit_target_words,
        )


DEFAULT_LIMITS = MessageLimits()


def word_count(message: str) -> int:
    return len(message.split())


def message_problems(message: str, limits: MessageLimits = DEFAULT_LIMITS) -> list[str]:
    """Everything wrong with a commit message (format and length); empty when acceptable."""
    subject = message.strip().splitlines()[0].strip() if message.strip() else ""
    problems = []
    if subject and not CONVENTIONAL_SUBJECT_RE.match(subject):
        problems.append("Subject is not in Conventional Commit format (type(scope): summary)")
    return problems + length_problems(message, limits)


def length_problems(message: str, limits: MessageLimits = DEFAULT_LIMITS) -> list[str]:
    """Length-limit violations only. These are enforced for every commit."""
    lines = message.strip().splitlines()
    if not lines:
        return ["Commit message is empty"]
    problems = []
    subject = lines[0].strip()
    if len(subject) > limits.subject_max:
        problems.append(f"Subject is {len(subject)} characters; the limit is {limits.subject_max}")
    words = word_count(message)
    if words > limits.max_words:
        problems.append(f"Message has {words} words; the limit is {limits.max_words}")
    body = [line for line in lines[1:] if line.strip()]
    if len(body) > limits.body_max_lines:
        problems.append(f"Body has {len(body)} lines; the limit is {limits.body_max_lines}")
    long_lines = [line for line in body if len(line) > limits.body_line_max]
    if long_lines:
        problems.append(f"{len(long_lines)} body line(s) exceed {limits.body_line_max} characters")
    return problems


def is_valid_message(message: str, limits: MessageLimits = DEFAULT_LIMITS) -> bool:
    return not message_problems(message, limits)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0] if " " in text[: limit - 1] else text[: limit - 1]
    return cut.rstrip(" ,;:-") + "…"


def fit_body(message: str, limits: MessageLimits) -> str:
    """Trim a generated body to the limits. The subject line is never altered."""
    lines = message.strip().splitlines()
    subject = lines[0].strip() if lines else ""
    body = [_shorten(line.rstrip(), limits.body_line_max) for line in lines[1:] if line.strip()]
    body = body[: limits.body_max_lines]
    while body and word_count(" ".join([subject, *body])) > limits.max_words:
        body.pop()
    return f"{subject}\n\n" + "\n".join(body) if body else subject


# ------------------------------------------------------------------------------ planning


@dataclass
class CommitGroup:
    """One planned commit: the files it contains and its message."""

    files: list[str]
    message: str
    source: str  # "llm" or "heuristic"
    reason: str = ""


@dataclass
class CommitPlan:
    status: RepoStatus
    to_stage: list[str]  # files GitWorklog will stage (nothing was staged by the user)
    already_staged: list[str]  # files the user staged
    sensitive_skipped: list[str]
    untracked_skipped: list[str]
    context: ReviewContext
    groups: list[CommitGroup]
    notes: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return self.already_staged or self.to_stage

    @property
    def message(self) -> str:
        """Message of the first planned commit."""
        return self.groups[0].message

    @property
    def source(self) -> str:
        return self.groups[0].source


def heuristic_message(
    status: RepoStatus, files: list[str], limits: MessageLimits = DEFAULT_LIMITS
) -> str:
    """Fallback message from file facts only. It is deliberately generic; edit it if needed."""
    areas = Counter(file_area(f) for f in files)
    new_files = {e.path for e in status.entries if e.untracked or e.index == "A"}
    if files and all(a == "Tests" for a in areas.elements()):
        kind = "test"
    elif files and all(a == "Docs" for a in areas.elements()):
        kind = "docs"
    elif files and all(a == "Config/CI" for a in areas.elements()):
        kind = "chore"
    elif any(f in new_files and file_area(f) != "Tests" for f in files):
        kind = "feat"
    else:
        kind = "chore"
    modules = Counter(m for f in files if (m := file_module(f)))
    scope = modules.most_common(1)[0][0] if modules else None
    scope = re.sub(r"[^a-z0-9._/\-]", "-", scope.lower()) if scope else None
    target = scope or (files[0].rsplit("/", 1)[-1] if len(files) == 1 else f"{len(files)} files")
    subject = _shorten(
        f"{kind}{f'({scope})' if scope else ''}: update {target}", limits.subject_max
    )
    body = [f"- {'add' if f in new_files else 'update'} {f}" for f in files]
    if len(body) > limits.body_max_lines and limits.body_max_lines > 0:
        extra = len(body) - limits.body_max_lines + 1
        body = [*body[: limits.body_max_lines - 1], f"- and {extra} more file(s)"]
    return fit_body("\n".join([subject, *body]), limits)


def group_files(files: list[str]) -> list[list[str]]:
    """Deterministic feature grouping (used without the LLM or when its plan is invalid).

    Files are grouped by module; tests and docs join the module their path mentions; generic
    files (README, lockfiles, package.json, ...) join the largest group.
    """
    keys: dict[str, str] = {}
    generic: list[str] = []
    for f in files:
        if is_generic_file(f):
            generic.append(f)
        else:
            keys[f] = file_module(f) or file_area(f).lower()
    code_keys = sorted({k for f, k in keys.items() if file_area(f) not in ("Tests", "Docs")})
    for f, key in list(keys.items()):
        if file_area(f) in ("Tests", "Docs") and key not in code_keys:
            words = set(split_words(f))
            keys[f] = next((c for c in code_keys if c in words), key)
    groups: dict[str, list[str]] = {}
    for f in files:
        if f in keys:
            groups.setdefault(keys[f], []).append(f)
    result = list(groups.values())
    if generic:
        if result:
            max(result, key=len).extend(generic)
        else:
            result.append(generic)
    return result


def _patch_for(patch: str, files: list[str]) -> str:
    wanted = set(files)
    return "".join(s for p, s in split_patch(patch) if p in wanted)


def _llm_message(llm: ChatBackend, files: list[str], patch: str, limits: MessageLimits) -> str:
    """Ask for one message within the limits; retry once with feedback if it is rejected."""
    system = commit_system_prompt(**asdict(limits))
    user = f"Files: {', '.join(files[:100])}\n\n```diff\n{patch}\n```"
    message = ""
    for _ in range(2):
        reply = complete_json(llm, system, user, reasoning_effort="low")  # short task: be fast
        message = fit_body(str(reply.get("message", "")), limits)
        problems = message_problems(message, limits)
        if not problems:
            return message
        user += (
            f"\n\nYour previous message was rejected: {'; '.join(problems)}.\n"
            f"Previous message:\n{message}\nWrite a shorter one that meets the limits."
        )
    return message


def _llm_groups(
    llm: ChatBackend, files: list[str], patch: str, limits: MessageLimits, notes: list[str]
) -> list[CommitGroup]:
    """Ask the model to split the files by feature, then validate the plan strictly."""
    listing = "\n".join(f"- {f}" for f in files)
    user = f"Changed files:\n{listing}\n\n```diff\n{patch}\n```"
    data = complete_json(llm, commit_split_prompt(**asdict(limits)), user)
    raw = data.get("commits")
    if not isinstance(raw, list) or not raw:
        raise LLMError("the model returned no commit plan")
    known = set(files)
    seen: set[str] = set()
    groups: list[CommitGroup] = []
    invented: list[str] = []
    duplicated: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("files"), list):
            continue
        paths: list[str] = []
        for value in item["files"]:
            path = str(value).strip().removeprefix("./")
            if path not in known:
                invented.append(path)
            elif path in seen or path in paths:
                duplicated.append(path)  # a file can only be in one commit: keep the first
            else:
                paths.append(path)
        if paths:
            seen.update(paths)
            message = fit_body(str(item.get("message", "")), limits)
            groups.append(CommitGroup(paths, message, "llm", str(item.get("reason", ""))[:200]))
    if invented:
        notes.append(f"Ignored {len(invented)} file(s) the model listed that are not changed.")
    if duplicated:
        notes.append(
            f"The model listed {', '.join(sorted(set(duplicated)))} in more than one commit; "
            "kept each file in its first commit only."
        )
    missing = [f for f in files if f not in seen]
    for leftover in group_files(missing):
        groups.append(CommitGroup(leftover, "", "heuristic", "not assigned by the model"))
    if not groups:
        raise LLMError("the model's plan contained none of the changed files")
    return groups


def plan_commit(
    git: GitRunner,
    llm: ChatBackend | None = None,
    include_untracked: bool = False,
    limits: MessageLimits = DEFAULT_LIMITS,
    split: bool = True,
) -> CommitPlan:
    """Inspect status and diff, group files into feature commits and propose messages.

    Nothing in the repository is changed here.
    """
    status = git_status(git)
    if status.conflicted:
        raise NothingToCommit("Resolve merge conflicts before committing")
    staged = [e.path for e in status.staged]
    to_stage: list[str] = []
    sensitive: list[str] = []
    untracked_skipped: list[str] = []
    if not staged:
        for entry in status.entries:
            if is_sensitive_path(entry.path):
                sensitive.append(entry.path)
            elif entry.untracked and not include_untracked:
                untracked_skipped.append(entry.path)
            else:
                to_stage.append(entry.path)
    else:
        sensitive = [p for p in staged if is_sensitive_path(p)]
    if not staged and not to_stage:
        raise NothingToCommit(
            "No committable changes (only secret files or untracked files; "
            "use --all to include untracked files)"
        )
    ctx = build_review_context(git, staged=bool(staged), include_untracked=include_untracked)
    files = staged or to_stage
    ctx.patch = _patch_for(ctx.patch, files)
    ctx.files = [f for f in ctx.files if f in set(files)]

    notes: list[str] = []
    partial = [e.path for e in status.entries if e.staged and e.unstaged]
    if split and partial:
        split = False
        notes.append(
            "Some files are only partly staged, so the index is committed as one commit "
            "(your staging is kept exactly as it is)."
        )
    plan = CommitPlan(status, to_stage, staged, sensitive, untracked_skipped, ctx, [], notes)
    if sensitive and staged:
        notes.append("WARNING: potential secret files are staged: " + ", ".join(sensitive))
    leaking = sorted(
        {
            p
            for p, s in split_patch(ctx.patch)
            if any("[REDACTED:" in t for _, t in iter_added_lines(s))
        }
    )
    if leaking:
        notes.append(
            "WARNING: added lines contain a potential secret (value withheld) in: "
            + ", ".join(leaking)
            + ". Move it to an environment variable first."
        )

    groups: list[CommitGroup] | None = None
    if split and llm is not None and len(files) > 1:
        try:
            groups = _llm_groups(llm, files, ctx.patch, limits, notes)
        except LLMError as exc:
            notes.append(f"Could not use the model's commit split ({exc}); grouping by module.")
    if groups is None:
        grouped = group_files(files) if split else [list(files)]
        groups = [CommitGroup(g, "", "heuristic") for g in grouped]

    for group in groups:
        if group.message and not message_problems(group.message, limits):
            continue
        if llm is not None:
            try:
                message = _llm_message(llm, group.files, _patch_for(ctx.patch, group.files), limits)
                problems = message_problems(message, limits)
                if not problems:
                    group.message, group.source = message, "llm"
                    continue
                notes.append(f"Model message rejected ({problems[0]}); using a heuristic one.")
            except LLMError as exc:
                notes.append(f"LLM unavailable ({exc}); using a heuristic message.")
        group.message, group.source = heuristic_message(status, group.files, limits), "heuristic"
    plan.groups = groups
    if len(groups) > 1:
        notes.append(f"Unrelated changes were split into {len(groups)} commits (--single for one).")
    return plan


# ----------------------------------------------------------------------------- execution


@dataclass
class _Step:
    group: CommitGroup
    message: str
    stage: ProposedOperation | None
    commit: ProposedOperation
    paths: list[str] | None  # pathspec for `git commit -- <paths>` (pre-staged splits)


def _steps(git: GitRunner, plan: CommitPlan, messages: list[str]) -> list[_Step]:
    renamed_from = {e.path: e.orig_path for e in plan.status.entries if e.orig_path}
    split_staged = bool(plan.already_staged) and len(plan.groups) > 1
    steps = []
    for group, message in zip(plan.groups, messages, strict=True):
        stage = stage_operation(git, group.files) if plan.to_stage else None
        paths = None
        if split_staged:
            paths = [*group.files, *(renamed_from[f] for f in group.files if f in renamed_from)]
        steps.append(_Step(group, message, stage, commit_operation(message, paths), paths))
    return steps


def commit_plan_operation(plan: CommitPlan, steps: list[_Step]) -> ProposedOperation:
    """One approval request describing every commit and every exact command."""
    commands = [op.command for s in steps for op in (s.stage, s.commit) if op is not None]
    preview: list[str] = []
    for i, step in enumerate(steps, 1):
        preview += [f"Commit {i}/{len(steps)} ({len(step.group.files)} file(s)):"]
        preview += [f"  {f}" for f in step.group.files]
        preview += ["  Message:", *[f"    {line}" for line in step.message.splitlines()], ""]
    branch = plan.status.branch or "detached HEAD"
    return ProposedOperation(
        kind="commit",
        summary=f"Create {len(steps)} commit(s) on {branch}",
        command=commands[-1],
        pre_commands=commands[:-1],
        consequences=[
            f"Creates {len(steps)} local commit(s); nothing is pushed.",
            "Each commit contains only the files listed for it.",
        ],
        preview="\n".join(preview).rstrip(),
    )


def execute_commit(
    git: GitRunner,
    approver: Approver,
    plan: CommitPlan,
    messages: list[str] | None = None,
    limits: MessageLimits = DEFAULT_LIMITS,
) -> list[dict]:
    """Ask for ONE explicit approval for the whole plan, then stage, commit and verify each."""
    messages = [m.strip() for m in (messages or [g.message for g in plan.groups])]
    if len(messages) != len(plan.groups):
        raise CommitMessageError("One message is needed per planned commit")
    for i, message in enumerate(messages, 1):
        problems = length_problems(message, limits)
        if problems:  # checked before approval, so nothing is staged for a rejected message
            raise CommitMessageError(f"Commit {i}: " + "; ".join(problems))
    if plan.sensitive_skipped and plan.already_staged:
        raise NothingToCommit(
            "Refusing to commit staged secret files: " + ", ".join(plan.sensitive_skipped)
        )
    steps = _steps(git, plan, messages)
    require_approval(approver, commit_plan_operation(plan, steps))
    granted = PreApproved([op.command for s in steps for op in (s.stage, s.commit) if op])
    results: list[dict] = []
    for i, step in enumerate(steps, 1):
        try:
            if step.stage is not None:
                git.run_approved(step.stage, granted)
            result = git_commit(git, granted, step.message, step.paths)
        except GitError as exc:
            done = ", ".join(r["sha"] for r in results) or "none"
            raise GitError(
                f"Commit {i}/{len(steps)} failed: {exc}. Already created: {done}"
            ) from exc
        result["files_match"] = set(result["files"]) == set(step.group.files)
        results.append(result)
    return results
