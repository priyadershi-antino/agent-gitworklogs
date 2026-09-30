"""Evidence-based worklogs and timesheets."""

from __future__ import annotations

import contextlib
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from gitworklog.llm import ChatBackend, LLMError, complete_json
from gitworklog.models import Commit, Task
from gitworklog.prompts import WORKLOG_SYSTEM
from gitworklog.services.grouping import group_commits, is_vague
from gitworklog.tools.git import GitRunner, current_user_email, git_log, git_show

HOURS_NOTICE = (
    "Git history can identify development activity, but it cannot reliably determine "
    "the exact number of hours worked."
)
FORMATS = ("normal", "formal", "structured", "csv", "json")
PERIODS = ("today", "yesterday", "week", "last-week", "month", "last-month")


class WorklogError(ValueError):
    pass


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


def _parse_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorklogError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def resolve_range(
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    today: date | None = None,
) -> DateRange:
    """Turn a period keyword or --from/--to into an inclusive date range."""
    today = today or date.today()
    if date_from or date_to:
        start = _parse_date(date_from, "--from") if date_from else None
        end = _parse_date(date_to, "--to") if date_to else today
        start = start or end
        if start > end:
            raise WorklogError("--from must not be after --to")
        return DateRange(start, end, f"{start.isoformat()} to {end.isoformat()}")
    period = (period or "today").lower().replace("this-", "").replace("_", "-")
    monday = today - timedelta(days=today.weekday())
    if period == "today":
        return DateRange(today, today, "today")
    if period == "yesterday":
        day = today - timedelta(days=1)
        return DateRange(day, day, "yesterday")
    if period == "week":
        return DateRange(monday, today, "this week")
    if period == "last-week":
        return DateRange(monday - timedelta(days=7), monday - timedelta(days=1), "last week")
    if period == "month":
        return DateRange(today.replace(day=1), today, "this month")
    if period == "last-month":
        end = today.replace(day=1) - timedelta(days=1)
        return DateRange(end.replace(day=1), end, "last month")
    raise WorklogError(f"Unknown period {period!r}; use one of {', '.join(PERIODS)}")


def collect_commits(
    git: GitRunner, rng: DateRange, author: str | None = None, all_authors: bool = False
) -> tuple[list[Commit], str | None]:
    """Commits authored within the range (author's local date), across all branches."""
    who = None if all_authors else (author or current_user_email(git))
    # --since filters by committer date (>= author date), so widen it and filter precisely below.
    since = (rng.start - timedelta(days=2)).isoformat()
    commits = git_log(git, since=since, all_branches=True, author=who)
    selected = [c for c in commits if rng.start <= c.day <= rng.end]
    return sorted(selected, key=lambda c: c.authored_at), who


@dataclass
class DayLog:
    day: date
    tasks: list[Task]
    commits: list[Commit]
    hours: float | None = None  # user-provided only

    @property
    def files(self) -> set[str]:
        return {f.path for c in self.commits for f in c.files}

    @property
    def activity_window(self) -> str:
        times = sorted(c.authored_at for c in self.commits)
        return f"{times[0]:%H:%M}-{times[-1]:%H:%M}"


@dataclass
class Worklog:
    range: DateRange
    author: str | None
    days: list[DayLog]
    hours_per_day: float | None = None
    split_hours: bool = False
    enriched: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def commits(self) -> list[Commit]:
        return [c for d in self.days for c in d.commits]

    @property
    def tasks(self) -> list[Task]:
        return [t for d in self.days for t in d.tasks]


def suggested_split(tasks: list[Task], hours: float) -> dict[str, float]:
    """Proportional split of USER-PROVIDED hours by task size (an estimate, not a measurement)."""
    total = sum(t.weight for t in tasks) or 1.0
    split = {t.id: round(hours * t.weight / total * 4) / 4 for t in tasks}
    return split


def build_worklog(
    git: GitRunner,
    rng: DateRange,
    *,
    author: str | None = None,
    all_authors: bool = False,
    hours: float | None = None,
    split_hours: bool = False,
    llm: ChatBackend | None = None,
) -> Worklog:
    if hours is not None and not 0 < hours <= 24:
        raise WorklogError("--hours must be between 0 and 24 (hours per active day)")
    if split_hours and hours is None:
        raise WorklogError("--split-hours requires --hours")
    commits, who = collect_commits(git, rng, author, all_authors)
    by_day: dict[date, list[Commit]] = {}
    for c in commits:
        by_day.setdefault(c.day, []).append(c)
    days = [
        DayLog(
            day=d, tasks=group_commits(cs, id_prefix=f"{d.isoformat()}-T"), commits=cs, hours=hours
        )
        for d, cs in sorted(by_day.items())
    ]
    log = Worklog(range=rng, author=who, days=days, hours_per_day=hours, split_hours=split_hours)
    if llm is not None and log.tasks:
        try:
            log.enriched = enrich_tasks(llm, log.tasks, git)
        except LLMError as exc:
            log.notes.append(f"LLM enrichment unavailable ({exc}); showing deterministic output.")
    return log


def _evidence_payload(tasks: list[Task], git: GitRunner | None) -> list[dict[str, Any]]:
    payload = []
    for task in tasks:
        item = task.evidence_dict()
        if git is not None:
            for commit in item["commits"]:
                if is_vague(commit["subject"]):  # give the model real content for vague commits
                    # evidence is optional; never fail the worklog because of it
                    with contextlib.suppress(Exception):
                        commit["diff_excerpt"] = git_show(git, commit["sha"])["patch"][:1500]
        payload.append(item)
    return payload


def enrich_tasks(
    llm: ChatBackend, tasks: list[Task], git: GitRunner | None = None, batch_size: int = 25
) -> bool:
    """Let the LLM rewrite titles/bullets. Output is validated against the task IDs."""
    changed = False
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start : start + batch_size]
        by_id = {t.id: t for t in batch}
        user = json.dumps({"tasks": _evidence_payload(batch, git)}, indent=1)
        data = complete_json(llm, WORKLOG_SYSTEM, user)
        for item in data.get("tasks", []) if isinstance(data.get("tasks"), list) else []:
            task = by_id.get(str(item.get("id"))) if isinstance(item, dict) else None
            if task is None:
                continue  # never accept tasks the evidence does not contain
            title = item.get("title")
            if isinstance(title, str) and 3 <= len(title.strip()) <= 140:
                task.title = title.strip().rstrip(".")
                changed = True
            bullets = item.get("bullets")
            if isinstance(bullets, list):
                clean = [b.strip() for b in bullets if isinstance(b, str) and b.strip()][:5]
                if clean:
                    task.bullets = clean
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                task.summary = summary.strip()[:400]
    return changed


# ---------------------------------------------------------------------------- rendering


def _task_hours(log: Worklog, day: DayLog) -> dict[str, float]:
    if log.split_hours and day.hours is not None:
        return suggested_split(day.tasks, day.hours)
    return {}


def _evidence_line(task: Task) -> str:
    shas = ", ".join(c.short_sha for c in task.commits[:5])
    more = f" +{len(task.commits) - 5}" if len(task.commits) > 5 else ""
    return f"{len(task.commits)} commit(s) [{shas}{more}], {len(task.files)} file(s)"


def render_normal(log: Worklog) -> str:
    lines = [f"Worklog: {log.range.label}" + (f" (author: {log.author})" if log.author else "")]
    if not log.days:
        lines += ["", "No commits found in this period.", "", HOURS_NOTICE]
        return "\n".join(lines)
    for day in log.days:
        lines += ["", f"{day.day:%b %d} ({day.day:%A})"]
        split = _task_hours(log, day)
        for task in day.tasks:
            est = f"  [SUGGESTED ~{split[task.id]:g}h]" if task.id in split else ""
            lines.append(f"- {task.title}{est}")
            lines += [f"    - {b}" for b in task.bullets if b != task.title]
        lines.append(
            f"  Evidence: {len(day.commits)} commit(s), {len(day.files)} file(s); "
            f"commit activity {day.activity_window} (timestamps, not hours)"
        )
        if day.hours is not None:
            lines.append(f"  Hours: {day.hours:g} [USER-PROVIDED]")
    lines += ["", _footer(log)]
    return "\n".join(lines)


def _footer(log: Worklog) -> str:
    parts = [HOURS_NOTICE]
    if log.split_hours:
        parts.append(
            "SUGGESTED hour splits divide your provided hours by relative change size; "
            "they are estimates, not measurements."
        )
    return "\n".join(parts + log.notes)


def render_formal(log: Worklog) -> str:
    if not log.days:
        return f"No development activity was recorded in Git for {log.range.label}.\n{HOURS_NOTICE}"
    out = []
    for day in log.days:
        sentences = []
        for task in day.tasks:
            if task.summary:
                sentences.append(task.summary.rstrip(".") + ".")
            else:
                detail = "; ".join(b[:1].lower() + b[1:] for b in task.bullets[:3])
                sentences.append(f"{task.title}" + (f" ({detail})." if detail else "."))
        hours = f" [{day.hours:g} hours, user-provided]" if day.hours is not None else ""
        out.append(f"{day.day:%b %d, %Y}{hours}: " + " ".join(sentences))
    return "\n\n".join(out) + f"\n\n{_footer(log)}"


def structured_rows(log: Worklog) -> list[dict[str, Any]]:
    rows = []
    for day in log.days:
        split = _task_hours(log, day)
        for task in day.tasks:
            rows.append(
                {
                    "date": day.day.isoformat(),
                    "task": task.title,
                    "details": "; ".join(task.bullets),
                    "commits": " ".join(c.short_sha for c in task.commits),
                    "files": len(task.files),
                    "evidence": _evidence_line(task),
                    "confidence": task.confidence,
                    "hours_user_provided": day.hours if day.hours is not None else "",
                    "hours_suggested_split": split.get(task.id, ""),
                }
            )
    return rows


def render_csv(log: Worklog) -> str:
    buffer = io.StringIO()
    fields = [
        "date",
        "task",
        "details",
        "commits",
        "files",
        "confidence",
        "hours_user_provided",
        "hours_suggested_split",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(structured_rows(log))
    return buffer.getvalue()


def render_json(log: Worklog) -> str:
    data = {
        "range": {"from": log.range.start.isoformat(), "to": log.range.end.isoformat()},
        "author": log.author,
        "hours_notice": HOURS_NOTICE,
        "days": [
            {
                "date": d.day.isoformat(),
                "commits": len(d.commits),
                "files": len(d.files),
                "commit_activity_window": d.activity_window,
                "hours_user_provided": d.hours,
                "tasks": [
                    {
                        **t.evidence_dict(),
                        "title": t.title,
                        "bullets": t.bullets,
                        "summary": t.summary,
                    }
                    for t in d.tasks
                ],
            }
            for d in log.days
        ],
        "notes": log.notes,
    }
    return json.dumps(data, indent=2)


def render(log: Worklog, fmt: str) -> str:
    renderers = {
        "normal": render_normal,
        "formal": render_formal,
        "csv": render_csv,
        "json": render_json,
    }
    if fmt not in FORMATS:
        raise WorklogError(f"Unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
    if fmt == "structured":  # plain-text table; the CLI renders a Rich table instead
        rows = structured_rows(log)
        lines = ["Date | Task | Evidence | Confidence"]
        lines += [f"{r['date']} | {r['task']} | {r['evidence']} | {r['confidence']}" for r in rows]
        return "\n".join(lines) + f"\n\n{_footer(log)}"
    return renderers[fmt](log)
