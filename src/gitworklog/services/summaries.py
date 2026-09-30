"""Daily/weekly development summaries and standups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from gitworklog.llm import ChatBackend, LLMError
from gitworklog.models import Commit, Task
from gitworklog.services.grouping import group_commits
from gitworklog.services.worklog import DateRange, collect_commits, enrich_tasks
from gitworklog.tools.git import GitRunner, git_status, operation_in_progress

AREA_ORDER = ["Backend", "Frontend", "Tests", "Docs", "Config/CI", "Other"]


@dataclass
class Summary:
    range: DateRange
    author: str | None
    commits: list[Commit]
    tasks: list[Task]
    notes: list[str] = field(default_factory=list)

    @property
    def files(self) -> set[str]:
        return {f.path for c in self.commits for f in c.files}

    def by_area(self) -> dict[str, list[Task]]:
        areas: dict[str, list[Task]] = {}
        for task in self.tasks:
            if task.kind != "fix":
                areas.setdefault(task.area, []).append(task)
        return {a: areas[a] for a in AREA_ORDER if a in areas}

    @property
    def fixes(self) -> list[Task]:
        return [t for t in self.tasks if t.kind == "fix"]


def _maybe_enrich(
    llm: ChatBackend | None, tasks: list[Task], git: GitRunner, notes: list[str]
) -> None:
    if llm is None or not tasks:
        return
    try:
        enrich_tasks(llm, tasks, git)
    except LLMError as exc:
        notes.append(f"LLM enrichment unavailable ({exc}); showing deterministic output.")


def build_summary(
    git: GitRunner,
    rng: DateRange,
    *,
    author: str | None = None,
    all_authors: bool = False,
    llm: ChatBackend | None = None,
) -> Summary:
    commits, who = collect_commits(git, rng, author, all_authors)
    tasks = group_commits(commits)
    summary = Summary(range=rng, author=who, commits=commits, tasks=tasks)
    _maybe_enrich(llm, tasks, git, summary.notes)
    return summary


def render_summary(summary: Summary) -> str:
    title = {
        "today": "Today's development",
        "yesterday": "Yesterday's development",
        "this week": "This week's development",
    }.get(summary.range.label, f"Development: {summary.range.label}")
    lines = [title]
    if not summary.commits:
        return "\n".join([*lines, "", "No commits found in this period.", *summary.notes])
    for area, tasks in summary.by_area().items():
        lines += ["", area]
        lines += [f"- {t.title}" for t in tasks]
    if summary.fixes:
        lines += ["", "Bug fixes"]
        lines += [f"- {t.title}" for t in summary.fixes]
    adds = sum(c.additions for c in summary.commits)
    dels = sum(c.deletions for c in summary.commits)
    lines += [
        "",
        f"Commits: {len(summary.commits)}",
        f"Files changed: {len(summary.files)}",
        f"Lines: +{adds} / -{dels}",
        *summary.notes,
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------ standup


@dataclass
class StandupItem:
    text: str
    label: str  # Observed / Inferred / Suggested / User-provided


@dataclass
class Standup:
    previous_heading: str
    previous: list[StandupItem]
    today: list[StandupItem]
    blockers: list[StandupItem]
    notes: list[str] = field(default_factory=list)


def build_standup(
    git: GitRunner,
    *,
    period: str = "yesterday",
    today: date | None = None,
    plan: list[str] | None = None,
    author: str | None = None,
    all_authors: bool = False,
    llm: ChatBackend | None = None,
) -> Standup:
    today = today or date.today()
    notes: list[str] = []
    if period == "week":
        monday = today - timedelta(days=today.weekday())
        rng = DateRange(monday, today, "this week")
        heading = "This week"
        commits, _ = collect_commits(git, rng, author, all_authors)
    else:
        # Most recent day with activity before today (covers weekends), up to 7 days back.
        rng = DateRange(today - timedelta(days=7), today - timedelta(days=1), "last 7 days")
        recent, _ = collect_commits(git, rng, author, all_authors)
        last_day = max((c.day for c in recent), default=None)
        commits = [c for c in recent if c.day == last_day]
        if last_day is None:
            heading = "Yesterday"
        elif last_day == today - timedelta(days=1):
            heading = f"Yesterday ({last_day:%b %d})"
        else:
            heading = f"Last active day ({last_day:%a %b %d})"
    tasks = group_commits(commits)
    _maybe_enrich(llm, tasks, git, notes)
    previous = [StandupItem(t.title, "Observed") for t in tasks] or [
        StandupItem("No commits found in Git history for this period", "Observed")
    ]

    today_items: list[StandupItem] = []
    todays, _ = collect_commits(git, DateRange(today, today, "today"), author, all_authors)
    for task in group_commits(todays) if period != "week" else []:
        today_items.append(StandupItem(f"Committed so far: {task.title}", "Observed"))
    status = git_status(git)
    changed = [e.path for e in status.entries]
    if changed:
        shown = ", ".join(changed[:5]) + (
            f" (+{len(changed) - 5} more)" if len(changed) > 5 else ""
        )
        today_items.append(StandupItem(f"Uncommitted work in progress: {shown}", "Observed"))
    today_items += [StandupItem(p.strip(), "User-provided") for p in plan or [] if p.strip()]
    if not today_items:
        today_items.append(StandupItem("No confirmed future work from Git history", "Observed"))

    blockers: list[StandupItem] = []
    if status.conflicted:
        blockers.append(
            StandupItem(
                f"Unresolved merge conflicts in {len(status.conflicted)} file(s)", "Observed"
            )
        )
    for op in operation_in_progress(git):
        blockers.append(StandupItem(f"A git {op} is in progress", "Observed"))
    if status.ahead and status.behind:
        blockers.append(
            StandupItem(
                f"Branch has diverged from {status.upstream} "
                f"(ahead {status.ahead}, behind {status.behind})",
                "Observed",
            )
        )
    if not blockers:
        blockers.append(StandupItem("No blockers observable from repository evidence", "Observed"))
    return Standup(heading, previous, today_items, blockers, notes)


def render_standup(standup: Standup) -> str:
    def section(title: str, items: list[StandupItem]) -> list[str]:
        return [title, *[f"- {i.text}  [{i.label}]" for i in items], ""]

    lines = section(standup.previous_heading, standup.previous)
    lines += section("Today", standup.today)
    lines += section("Blockers", standup.blockers)
    return "\n".join(lines + standup.notes).rstrip()
