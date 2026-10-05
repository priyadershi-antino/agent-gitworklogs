"""Typed tool registry exposed to the LLM. No arbitrary shell execution is available."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from gitworklog.config import Limits
from gitworklog.safety import Approver, OperationDenied, mask_secrets
from gitworklog.tools import filesystem, git, repository

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
    "number": (int, float),
}


@dataclass
class ToolContext:
    git: git.GitRunner
    approver: Approver
    today: date = field(default_factory=date.today)
    base_branch: str = "main"
    commit_limits: Any = None  # services.commits.MessageLimits; default limits when None
    # Returns an authenticated services.timesheet.TimesheetSession (asks for the token on first
    # use); None when the timesheet is not available.
    timesheet_session: Callable[[], Any] | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    handler: Callable[..., Any]
    required: list[str] = field(default_factory=list)
    mutating: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }


def serialize(result: Any, limits: Limits) -> str:
    text = json.dumps(result, default=str, ensure_ascii=False)
    text = mask_secrets(text)
    if len(text) > limits.max_tool_output_chars:
        text = text[: limits.max_tool_output_chars] + '... [tool output truncated]"'
    return text


class ToolRegistry:
    def __init__(self, ctx: ToolContext, specs: list[ToolSpec]):
        self.ctx = ctx
        self.specs = {s.name: s for s in specs}

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [s.definition() for s in self.specs.values()]

    def validate(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - set(spec.properties)
        if unknown:
            raise ValueError(f"Unknown argument(s): {', '.join(sorted(unknown))}")
        missing = [r for r in spec.required if args.get(r) in (None, "")]
        if missing:
            raise ValueError(f"Missing required argument(s): {', '.join(missing)}")
        clean = {}
        for key, value in args.items():
            if value is None:
                continue
            schema = spec.properties[key]
            expected = _JSON_TYPES.get(schema.get("type", "string"))
            if schema.get("type") == "integer" and isinstance(value, str) and value.isdigit():
                value = int(value)
            if expected and (
                not isinstance(value, expected)
                or (schema.get("type") in ("integer", "number") and isinstance(value, bool))
            ):
                raise ValueError(f"Argument {key} must be of type {schema.get('type')}")
            if "enum" in schema and value not in schema["enum"]:
                raise ValueError(f"Argument {key} must be one of {schema['enum']}")
            clean[key] = value
        return clean

    def dispatch(self, name: str, raw_args: str | dict[str, Any] | None) -> dict[str, Any]:
        spec = self.specs.get(name)
        if spec is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("Arguments must be a JSON object")
            args = self.validate(spec, args)
            return {"ok": True, "result": spec.handler(self.ctx, **args)}
        except OperationDenied:
            return {
                "ok": False,
                "denied": True,
                "error": "The user did not approve this operation. It was NOT executed. "
                "Do not retry unless the user explicitly asks.",
            }
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"Invalid JSON arguments: {exc}"}
        except Exception as exc:  # tool errors are reported to the model, never raised
            return {"ok": False, "error": mask_secrets(f"{exc.__class__.__name__}: {exc}")}


# -------------------------------------------------------------------------- handlers


def _status(ctx: ToolContext) -> dict:
    status = git.git_status(ctx.git)
    data = status.to_dict()
    for key in ("staged", "unstaged", "untracked"):
        if len(data[key]) > 100:
            data[key] = [*data[key][:100], f"... {len(data[key]) - 100} more"]
    data["in_progress"] = git.operation_in_progress(ctx.git)
    return data


def _diff(
    ctx: ToolContext,
    staged: bool = False,
    base: str | None = None,
    path: str | None = None,
    stat_only: bool = False,
) -> dict:
    result = git.git_diff(ctx.git, staged=staged, base=base, paths=[path] if path else None)
    if stat_only:
        result.pop("patch")
    return result


def _log(
    ctx: ToolContext,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    max_count: int = 30,
    all_branches: bool = False,
    rev_range: str | None = None,
    path: str | None = None,
) -> dict:
    commits = git.git_log(
        ctx.git,
        since=since,
        until=until,
        author=author,
        max_count=min(max_count, 200),
        all_branches=all_branches,
        rev_range=rev_range,
        paths=[path] if path else None,
    )
    return {"count": len(commits), "commits": [c.to_dict() for c in commits]}


def _compare(ctx: ToolContext, base: str | None = None) -> dict:
    from gitworklog.services.pr import compare_branches

    cmp = compare_branches(ctx.git, base or ctx.base_branch)
    return {**cmp.data.to_dict(), "logical_changes": [t.title for t in cmp.tasks]}


def _worklog(
    ctx: ToolContext,
    date_from: str,
    date_to: str | None = None,
    author: str | None = None,
    all_authors: bool = False,
) -> dict:
    from gitworklog.services.worklog import build_worklog, render_json, resolve_range

    log = build_worklog(
        ctx.git,
        resolve_range(date_from=date_from, date_to=date_to, today=ctx.today),
        author=author,
        all_authors=all_authors,
    )
    return json.loads(render_json(log))


def _health(ctx: ToolContext) -> list[dict]:
    from gitworklog.services.health import run_health_checks

    return [asdict(c) for c in run_health_checks(ctx.git)]


def _commit(ctx: ToolContext, message: str) -> dict:
    from gitworklog.services.commits import CommitMessageError, MessageLimits, length_problems

    limits = ctx.commit_limits or MessageLimits()
    problems = length_problems(message, limits)
    if problems:  # rejected before the approval prompt; the model is told to shorten it
        raise CommitMessageError("; ".join(problems) + ". Write a shorter message and retry.")
    return git.git_commit(ctx.git, ctx.approver, message)


def _protected(
    ctx: ToolContext,
    operation: str,
    target: str | None = None,
    paths: list[str] | None = None,
    remote: str = "origin",
    force: bool = False,
) -> dict:
    if operation == "restore_file" and not paths and target:
        paths, target = [target], None  # models often pass the file as `target`
    op = git.build_protected_operation(
        ctx.git, operation, target=target, paths=paths, remote=remote, force=force
    )
    return git.run_protected_operation(ctx.git, ctx.approver, op)


def _timesheet_plan(
    ctx: ToolContext,
    date_from: str,
    date_to: str | None,
    hours: float | None,
    update_existing: bool,
):
    from gitworklog.services import timesheet as ts
    from gitworklog.services.worklog import resolve_range
    from gitworklog.tools.nexus import NexusError

    if ctx.timesheet_session is None:
        raise NexusError("The timesheet is not available in this session.")
    session = ctx.timesheet_session()
    rng = resolve_range(date_from=date_from, date_to=date_to, today=ctx.today)
    plan = ts.collect_plan(ctx.git, session, rng, hours=hours, update_existing=update_existing)
    return session, plan


def _timesheet_entries(
    ctx: ToolContext,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    day: str | None = None,
    week: str | None = None,
    month: str | None = None,
    project: str | None = None,
    all_projects: bool = False,
) -> dict:
    """List entries already in Nexus. Read-only."""
    from gitworklog.services import timesheet as ts
    from gitworklog.tools.nexus import NexusError

    if ctx.timesheet_session is None:
        raise NexusError("The timesheet is not available in this session.")
    rng = ts.resolve_listing_range(
        period, date_from, date_to, day=day, week=week, month=month, today=ctx.today
    )
    groups = ts.list_entries(
        ctx.timesheet_session(), rng, project=project, all_projects=all_projects
    )
    return {
        "range": [rng.start.isoformat(), rng.end.isoformat()],
        "projects": [
            {
                "project": g.project.name,
                "project_id": g.project.id,
                "total_minutes": g.total_minutes,
                "entries": [
                    {
                        "date": e.date,
                        "hours": e.hours,
                        "minutes": e.minutes,
                        "description": e.description,
                    }
                    for e in sorted(g.entries, key=lambda e: e.date)
                ],
            }
            for g in groups
        ],
    }


def _timesheet_preview(
    ctx: ToolContext,
    date_from: str,
    date_to: str | None = None,
    hours: float | None = None,
    update_existing: bool = False,
) -> dict:
    """Plan only. Reads commits and existing entries; nothing is written."""
    _, plan = _timesheet_plan(ctx, date_from, date_to, hours, update_existing)
    return plan.to_dict()


def _timesheet_submit(
    ctx: ToolContext,
    date_from: str,
    hours: float,
    date_to: str | None = None,
    update_existing: bool = False,
) -> dict:
    from gitworklog.services import timesheet as ts

    session, plan = _timesheet_plan(ctx, date_from, date_to, hours, update_existing)
    results = ts.execute_plan(session, ctx.approver, plan)
    return {"results": [r.to_dict() for r in results], "plan": plan.to_dict()}


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}


def build_registry(ctx: ToolContext) -> ToolRegistry:
    specs = [
        ToolSpec(
            "git_status",
            "Current branch, upstream ahead/behind, staged/unstaged/untracked "
            "files and in-progress operations.",
            {},
            _status,
        ),
        ToolSpec(
            "git_diff",
            "Unified diff with per-file stats (secrets masked, size-limited). "
            "Default: unstaged changes. staged=true: index vs HEAD. base: base...HEAD. "
            "Use stat_only=true first for large changes.",
            {"staged": _BOOL, "base": _STR, "path": _STR, "stat_only": _BOOL},
            _diff,
        ),
        ToolSpec(
            "git_log",
            "Commits with changed files. since/until accept dates like "
            "2026-09-01 or '2 weeks ago'. author is a substring of name/email.",
            {
                "since": _STR,
                "until": _STR,
                "author": _STR,
                "max_count": _INT,
                "all_branches": _BOOL,
                "rev_range": _STR,
                "path": _STR,
            },
            _log,
        ),
        ToolSpec(
            "git_show",
            "One commit's metadata, files and (masked, truncated) patch.",
            {"commit": _STR},
            lambda c, commit: git.git_show(c.git, commit),
            ["commit"],
        ),
        ToolSpec(
            "git_branch",
            "Local branches with upstream tracking info, and remote branches.",
            {},
            lambda c: git.git_branches(c.git),
        ),
        ToolSpec(
            "git_compare",
            "Compare the current branch with a base branch: ahead/behind, "
            "commits, files, +/- and logical changes. Also lists unmerged local branches.",
            {"base": _STR},
            _compare,
        ),
        ToolSpec(
            "repository_info",
            "Repository root, branch, HEAD, remotes, commit count, dates "
            "and the configured user email.",
            {},
            lambda c: repository.repository_info(c.git),
        ),
        ToolSpec(
            "read_file",
            "Read a text file in the repository (line-numbered, masked). "
            "Secret files such as .env are withheld.",
            {"path": _STR, "start_line": _INT, "end_line": _INT},
            lambda c, **kw: filesystem.read_file(c.git, **kw),
            ["path"],
        ),
        ToolSpec(
            "search_files",
            "Fixed-string search across tracked files (git grep).",
            {"query": _STR, "path_glob": _STR},
            lambda c, **kw: filesystem.search_files(c.git, **kw),
            ["query"],
        ),
        ToolSpec(
            "worklog_evidence",
            "Commits in a date range (author's local dates, all "
            "branches) grouped into logical tasks per day, with evidence and confidence. "
            "Defaults to the configured user's commits.",
            {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "author": _STR,
                "all_authors": _BOOL,
            },
            _worklog,
            ["date_from"],
        ),
        ToolSpec(
            "health_check",
            "Repository hygiene: conflicts, divergence, large/generated/"
            "temp files, .env files, likely secrets (values withheld), debug statements.",
            {},
            _health,
        ),
        ToolSpec(
            "git_add",
            "Stage paths. REQUIRES HUMAN APPROVAL. Secret files are refused.",
            {"paths": {"type": "array", "items": _STR}},
            lambda c, paths: git.git_add(c.git, c.approver, paths),
            ["paths"],
            True,
        ),
        ToolSpec(
            "git_commit",
            "Commit staged changes with a Conventional Commit message. "
            "REQUIRES HUMAN APPROVAL. Only use when the user asked to commit.",
            {"message": _STR},
            _commit,
            ["message"],
            True,
        ),
        ToolSpec(
            "apply_edit",
            "Replace one exact, unique snippet in a file. REQUIRES HUMAN "
            "APPROVAL of the shown diff. Read the file first.",
            {"path": _STR, "old_text": _STR, "new_text": _STR},
            lambda c, path, old_text, new_text: filesystem.apply_edit(
                c.git, c.approver, path, old_text, new_text
            ),
            ["path", "old_text", "new_text"],
            True,
        ),
        ToolSpec(
            "timesheet_entries",
            "List timesheet entries already in Nexus for a time and project. Time: one of "
            "period (today, yesterday, week, last-week, month, last-month), date_from/date_to, "
            "day (YYYY-MM-DD), week (YYYY-Www or a date in that week), month (YYYY-MM); default "
            "is this week. Project: the repository's project by default, `project` (id or part "
            "of the name) or all_projects=true. Read-only.",
            {
                "period": _STR,
                "date_from": _STR,
                "date_to": _STR,
                "day": _STR,
                "week": _STR,
                "month": _STR,
                "project": _STR,
                "all_projects": _BOOL,
            },
            _timesheet_entries,
        ),
        ToolSpec(
            "timesheet_preview",
            "Plan timesheet entries from commits in a date range and compare with entries "
            "already in Nexus. Writes nothing. `hours` (per day) must be stated by the user; "
            "if the user did not give hours, omit it (never guess).",
            {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "hours": {"type": "number", "description": "hours per day, from the user"},
                "update_existing": _BOOL,
            },
            _timesheet_preview,
            ["date_from"],
        ),
        ToolSpec(
            "timesheet_submit",
            "Create timesheet entries in Nexus for days with commits. REQUIRES HUMAN APPROVAL of "
            "the exact plan. Use only when the user asked to fill the timesheet and told you the "
            "hours per day; never invent hours. Existing entries are only overwritten with "
            "update_existing=true when the user asked for that.",
            {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "hours": {"type": "number", "description": "hours per day, stated by the user"},
                "update_existing": _BOOL,
            },
            _timesheet_submit,
            ["date_from", "hours"],
            True,
        ),
        ToolSpec(
            "protected_git_operation",
            "Destructive or outward-facing git operation. "
            "REQUIRES EXPLICIT HUMAN APPROVAL. Only use when the user explicitly asked for "
            "exactly this operation. Never for vague requests.",
            {
                "operation": {"type": "string", "enum": list(git.PROTECTED_OPERATIONS)},
                "target": {
                    "type": "string",
                    "description": "branch/ref for push, merge, rebase, delete_branch, reset_hard",
                },
                "paths": {"type": "array", "items": _STR},
                "remote": _STR,
                "force": _BOOL,
            },
            _protected,
            ["operation"],
            True,
        ),
    ]
    return ToolRegistry(ctx, specs)
