"""Fill the Nexus timesheet from the evidence-based worklog.

Rules that keep this honest:
- Hours are NEVER derived from Git. They come from the user (`--hours`, `--day DATE=HOURS`).
- Only days that have commits are filled; there is no evidence for other days.
- An existing entry is never overwritten unless the user passes `--update`.
- Nothing is sent until the user approves a plan listing every request.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from gitworklog.llm import ChatBackend
from gitworklog.safety import Approver, ProposedOperation, mask_secrets, require_approval
from gitworklog.services.worklog import (
    HOURS_NOTICE,
    DateRange,
    Worklog,
    WorklogError,
    build_worklog,
    resolve_range,
)
from gitworklog.tools.git import GitRunner
from gitworklog.tools.nexus import (
    NexusClient,
    NexusError,
    Project,
    TimesheetEntry,
    developer_id_for,
    resolve_access_token,
)

MAX_HOURS_PER_DAY = 24
CREATE, UPDATE = "create", "update"
SKIP_SAME, SKIP_EXISTS, SKIP_MULTIPLE, SKIP_NO_HOURS = (
    "skip-same",
    "skip-exists",
    "skip-multiple",
    "skip-no-hours",
)


class NothingToSubmit(ValueError):
    pass


@dataclass
class TimesheetSession:
    """An authenticated connection plus the project to fill."""

    client: NexusClient
    developer_id: str
    base_url: str
    project_id: str | None = None
    description_max: int = 500
    llm: ChatBackend | None = None

    def require_project(self) -> str:
        if not self.project_id:
            raise NexusError(
                "No Nexus project is set for this repository. Run `gitworklog timesheet init`."
            )
        return self.project_id


def open_session(
    base_url: str, project_id: str | None, description_max: int, prompt: bool
) -> TimesheetSession:
    token = resolve_access_token(prompt)
    return TimesheetSession(
        client=NexusClient(base_url, token),
        developer_id=developer_id_for(token),
        base_url=base_url,
        project_id=project_id,
        description_max=description_max,
    )


# ----------------------------------------------------------------------------- hours


def split_hours(value: float) -> tuple[int, int]:
    """Decimal hours to (hours, minutes), e.g. 7.5 -> (7, 30)."""
    if isinstance(value, bool) or not 0 < value <= MAX_HOURS_PER_DAY:
        raise WorklogError(f"Hours must be greater than 0 and at most {MAX_HOURS_PER_DAY} per day")
    hours, minutes = divmod(round(value * 60), 60)
    return int(hours), int(minutes)


def require_hours(hours: float | None, day_hours: dict[date, float] | None) -> None:
    if hours is None and not day_hours:
        raise WorklogError(
            "Hours are required: pass --hours N (per day) and/or --day DATE=HOURS. "
            "Git history cannot determine how long you worked."
        )


def parse_day_hours(items: list[str] | None) -> dict[date, float]:
    """Parse repeated `--day 2026-10-02=4` options."""
    result: dict[date, float] = {}
    for item in items or []:
        day_text, sep, hours_text = item.partition("=")
        try:
            day = date.fromisoformat(day_text.strip())
            hours = float(hours_text)
        except ValueError:
            raise WorklogError(f"--day must look like 2026-10-02=4 (got {item!r})") from None
        if not sep:
            raise WorklogError(f"--day must look like 2026-10-02=4 (got {item!r})")
        split_hours(hours)  # validates the range
        result[day] = hours
    return result


# ----------------------------------------------------------------------------- planning


@dataclass
class PlannedEntry:
    day: date
    action: str
    description: str
    hours: int
    minutes: int
    commits: int
    existing: TimesheetEntry | None = None
    reason: str = ""
    truncated: bool = False

    @property
    def writes(self) -> bool:
        return self.action in (CREATE, UPDATE)

    @property
    def total_minutes(self) -> int:
        return self.hours * 60 + self.minutes

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.day.isoformat(),
            "action": self.action,
            "description": self.description,
            "hours": self.hours,
            "minutes": self.minutes,
            "commits": self.commits,
            "existing": (
                {
                    "id": self.existing.id,
                    "hours": self.existing.hours,
                    "minutes": self.existing.minutes,
                    "description": self.existing.description,
                }
                if self.existing
                else None
            ),
            "reason": self.reason,
        }


@dataclass
class TimesheetPlan:
    project_id: str
    project_name: str
    developer_id: str
    range: DateRange
    entries: list[PlannedEntry]
    notes: list[str] = field(default_factory=list)

    @property
    def writes(self) -> list[PlannedEntry]:
        return [e for e in self.entries if e.writes]

    def to_dict(self) -> dict[str, object]:
        return {
            "project": {"id": self.project_id, "name": self.project_name},
            "range": [self.range.start.isoformat(), self.range.end.isoformat()],
            "entries": [e.to_dict() for e in self.entries],
            "notes": self.notes,
            "hours_notice": HOURS_NOTICE,
        }


def task_description(titles: list[str], limit: int) -> tuple[str, bool]:
    """Join task titles into one line, cut at a task boundary when over `limit` characters."""
    unique = list(dict.fromkeys(t.strip().rstrip(".") for t in titles if t.strip()))
    text = mask_secrets("; ".join(unique)) or "Development work"
    if len(text) <= limit:
        return text, False
    cut = text[: limit - 1]
    if text[len(cut) : len(cut) + 2] != "; " and "; " in cut:
        cut = cut.rsplit("; ", 1)[0]  # drop the task that was cut in half
    return cut.rstrip(" ;,") + "…", True


def plan_timesheet(
    log: Worklog,
    existing: list[TimesheetEntry],
    *,
    project_id: str,
    project_name: str,
    developer_id: str,
    hours: float | None,
    day_hours: dict[date, float] | None = None,
    update_existing: bool = False,
    description_max: int = 500,
) -> TimesheetPlan:
    """Decide, per day with commits, whether to create, update or skip an entry."""
    day_hours = day_hours or {}
    require_hours(hours, day_hours)
    if hours is not None:
        split_hours(hours)
    by_date: dict[str, list[TimesheetEntry]] = {}
    for entry in existing:
        by_date.setdefault(entry.date, []).append(entry)

    plan = TimesheetPlan(project_id, project_name, developer_id, log.range, [])
    commit_days = {d.day for d in log.days}
    for day in sorted(set(day_hours) - commit_days):
        if log.range.start <= day <= log.range.end:
            plan.notes.append(f"{day}: no commits found, so no entry is created for that day.")
        else:
            plan.notes.append(f"{day}: outside the selected date range, ignored.")

    for day_log in log.days:
        value = day_hours.get(day_log.day, hours)
        description, truncated = task_description([t.title for t in day_log.tasks], description_max)
        h, m = split_hours(value) if value is not None else (0, 0)
        found = by_date.get(day_log.day.isoformat(), [])
        entry = PlannedEntry(
            day_log.day, CREATE, description, h, m, len(day_log.commits), truncated=truncated
        )
        if value is None:
            entry.action, entry.reason = SKIP_NO_HOURS, "no hours given for this day"
        elif len(found) > 1:
            entry.action = SKIP_MULTIPLE
            entry.reason = f"{len(found)} entries already exist; resolve them in Nexus"
        elif found:
            entry.existing = found[0]
            same = (
                found[0].total_minutes == entry.total_minutes
                and found[0].description.strip() == description
            )
            if same:
                entry.action, entry.reason = SKIP_SAME, "already up to date"
            elif update_existing:
                entry.action, entry.reason = UPDATE, "overwrites the existing entry"
            else:
                entry.action = SKIP_EXISTS
                entry.reason = "an entry exists (use --update to overwrite it)"
        if truncated:
            plan.notes.append(
                f"{day_log.day}: description shortened to {description_max} characters."
            )
        plan.entries.append(entry)
    return plan


def collect_plan(
    git: GitRunner,
    session: TimesheetSession,
    rng: DateRange,
    *,
    hours: float | None,
    day_hours: dict[date, float] | None = None,
    update_existing: bool = False,
    author: str | None = None,
    all_authors: bool = False,
) -> TimesheetPlan:
    """Read the worklog and the existing entries, then build the plan. Writes nothing."""
    project_id = session.require_project()
    require_hours(hours, day_hours)  # fail before any network call
    log = build_worklog(git, rng, author=author, all_authors=all_authors, llm=session.llm)
    name = project_id
    notes: list[str] = []
    for project in session.client.projects(session.developer_id):
        if project.id == project_id:
            name = project.name
            break
    else:
        notes.append("This project id is not in your Nexus project list; check the setup.")
    existing = session.client.entries(session.developer_id, rng.start, rng.end, project_id)
    plan = plan_timesheet(
        log,
        existing,
        project_id=project_id,
        project_name=name,
        developer_id=session.developer_id,
        hours=hours,
        day_hours=day_hours,
        update_existing=update_existing,
        description_max=session.description_max,
    )
    plan.notes = [*notes, *log.notes, *plan.notes]
    return plan


# ---------------------------------------------------------------------------- rendering


def _hm(hours: int, minutes: int) -> str:
    return f"{hours}h {minutes:02d}m"


def render_plan(plan: TimesheetPlan) -> str:
    lines = [
        f"Timesheet plan for project: {plan.project_name} ({plan.project_id})",
        f"Dates: {plan.range.start} to {plan.range.end}",
    ]
    if not plan.entries:
        lines += ["", "No commits found in this period, so there is nothing to fill."]
    for e in plan.entries:
        label = e.action.upper().replace("SKIP-", "SKIP ")
        hours = _hm(e.hours, e.minutes) if e.action != SKIP_NO_HOURS else "-"
        lines += [
            "",
            f"{e.day}  {label:<14} {hours:<8} ({e.commits} commit(s))",
            f"    {e.description}",
        ]
        if e.existing:
            old = f'{_hm(e.existing.hours, e.existing.minutes)}: "{e.existing.description}"'
            lines.append(f"    existing entry: {old}")
        if e.reason:
            lines.append(f"    note: {e.reason}")
    lines += ["", f"Hours are USER-PROVIDED. {HOURS_NOTICE}"]
    lines += [f"Note: {n}" for n in plan.notes]
    return mask_secrets("\n".join(lines))


def plan_operation(plan: TimesheetPlan, base_url: str) -> ProposedOperation:
    """One approval request listing every request that will be sent."""
    actions, preview = [], []
    for e in plan.writes:
        if e.action == CREATE:
            actions.append(
                f"POST {base_url}/timesheet/entries  [{e.day}  {_hm(e.hours, e.minutes)}]"
            )
        else:
            actions.append(
                f"PUT  {base_url}/timesheet/entries/{e.existing.id if e.existing else '?'}  "
                f"[{e.day}  {_hm(e.hours, e.minutes)}]"
            )
        preview += [
            f"{e.day}  {_hm(e.hours, e.minutes)}  (hours provided by you)",
            f"  {e.description}",
        ]
        if e.existing:
            old = _hm(e.existing.hours, e.existing.minutes)
            preview.append(f'  replaces: {old} "{e.existing.description}"')
    overwriting = any(e.action == UPDATE for e in plan.writes)
    creating = sum(e.action == CREATE for e in plan.writes)
    updating = len(plan.writes) - creating
    return ProposedOperation(
        kind="timesheet",
        summary=(
            f"Submit {len(plan.writes)} timesheet entr{'y' if len(plan.writes) == 1 else 'ies'} "
            f"to project {plan.project_name} ({creating} new, {updating} overwritten)"
        ),
        command=[],
        actions=actions,
        consequences=[
            "Entries are written to the company timesheet system and are visible to others.",
            "Hours come from you; Git cannot determine hours worked.",
            *(
                ["Existing entries are overwritten (old values are shown below)."]
                if overwriting
                else []
            ),
        ],
        preview=mask_secrets("\n".join(preview)),
        destructive=overwriting,
    )


# ---------------------------------------------------------------------------- execution


@dataclass
class SubmitResult:
    day: date
    action: str
    ok: bool
    verified: bool = False
    entry_id: str | None = None
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.day.isoformat(),
            "action": self.action,
            "ok": self.ok,
            "verified": self.verified,
            "entry_id": self.entry_id,
            "error": self.error,
        }


def execute_plan(
    session: TimesheetSession, approver: Approver, plan: TimesheetPlan
) -> list[SubmitResult]:
    """Ask for ONE approval, send the entries in date order, then check them in Nexus.

    Stops at the first failure and reports what had already been sent.
    """
    writes = plan.writes
    if not writes:
        raise NothingToSubmit("There is nothing to submit for this plan.")
    require_approval(approver, plan_operation(plan, session.base_url))
    client = session.client
    results: list[SubmitResult] = []
    for e in writes:
        try:
            if e.action == CREATE:
                response = client.create_entry(
                    project_id=plan.project_id,
                    developer_id=plan.developer_id,
                    entry_date=e.day,
                    description=e.description,
                    hours=e.hours,
                    minutes=e.minutes,
                )
            else:
                assert e.existing is not None
                response = client.update_entry(
                    e.existing.id, description=e.description, hours=e.hours, minutes=e.minutes
                )
        except NexusError as exc:
            results.append(SubmitResult(e.day, e.action, False, error=str(exc)))
            break
        raw_id = response.get("id") or (response.get("data") or {}).get("id") if response else None
        results.append(
            SubmitResult(e.day, e.action, True, entry_id=str(raw_id) if raw_id else None)
        )
    _verify(session, plan, results)
    return results


def _verify(session: TimesheetSession, plan: TimesheetPlan, results: list[SubmitResult]) -> None:
    sent = [r for r in results if r.ok]
    if not sent:
        return
    expected = {e.day: e for e in plan.writes}
    days = [r.day for r in sent]
    try:
        found = session.client.entries(plan.developer_id, min(days), max(days), plan.project_id)
    except NexusError as exc:
        for r in sent:
            r.error = f"sent, but could not verify: {exc}"
        return
    for r in sent:
        want = expected[r.day].total_minutes
        r.verified = any(f.date == r.day.isoformat() and f.total_minutes == want for f in found)


# ------------------------------------------------------------------------------ listing


def match_project(projects: list[Project], text: str) -> Project:
    """Find one project by id, exact name or part of its name."""
    wanted = text.strip().lower()
    matches = [p for p in projects if p.id == text.strip() or wanted in p.name.lower()]
    exact = [p for p in matches if p.id == text.strip() or p.name.lower() == wanted]
    matches = exact or matches
    if len(matches) != 1:
        names = ", ".join(p.name for p in matches) or "none"
        raise NexusError(f"--project must match exactly one project (matched: {names}).")
    return matches[0]


def _month_range(text: str) -> DateRange:
    try:
        year_text, month_text = text.strip().split("-")
        first = date(int(year_text), int(month_text), 1)
    except ValueError:
        raise WorklogError(f"--month must look like 2026-09 (got {text!r})") from None
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    return DateRange(first, last, first.strftime("%B %Y"))


def _week_range(text: str) -> DateRange:
    """`2026-W40` (ISO week) or any date inside the wanted week (Monday to Sunday)."""
    value = text.strip()
    try:
        if "W" in value.upper():
            year_text, week_text = value.upper().split("-W")
            monday = date.fromisocalendar(int(year_text), int(week_text), 1)
        else:
            day = date.fromisoformat(value)
            monday = day - timedelta(days=day.weekday())
    except ValueError:
        raise WorklogError(
            f"--week must look like 2026-W40 or a date such as 2026-10-02 (got {text!r})"
        ) from None
    sunday = monday + timedelta(days=6)
    return DateRange(monday, sunday, f"week of {monday} to {sunday}")


def resolve_listing_range(
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    day: str | None = None,
    week: str | None = None,
    month: str | None = None,
    today: date | None = None,
) -> DateRange:
    """One date range from a period keyword, --from/--to, --date, --week or --month."""
    chosen = [
        name
        for name, value in (
            ("a period", period),
            ("--from/--to", date_from or date_to),
            ("--date", day),
            ("--week", week),
            ("--month", month),
        )
        if value
    ]
    if len(chosen) > 1:
        raise WorklogError(f"Use only one of: {', '.join(chosen)}.")
    if day:
        try:
            single = date.fromisoformat(day.strip())
        except ValueError:
            raise WorklogError(f"--date must look like 2026-10-02 (got {day!r})") from None
        return DateRange(single, single, str(single))
    if week:
        return _week_range(week)
    if month:
        return _month_range(month)
    return resolve_range(period or "week", date_from, date_to, today=today)


@dataclass
class ProjectEntries:
    project: Project
    entries: list[TimesheetEntry]

    @property
    def total_minutes(self) -> int:
        return sum(e.total_minutes for e in self.entries)


def list_entries(
    session: TimesheetSession,
    rng: DateRange,
    *,
    project: str | None = None,
    all_projects: bool = False,
) -> list[ProjectEntries]:
    """Entries for one project (the repository's, or `project`) or for every project.

    Reads only; nothing is written.
    """
    if all_projects and project:
        raise WorklogError("Use either --project or --all-projects, not both.")
    client, dev = session.client, session.developer_id
    projects = client.projects(dev)
    if all_projects:
        chosen = projects
    elif project:
        chosen = [match_project(projects, project)]
    else:
        project_id = session.require_project()
        chosen = [
            next((p for p in projects if p.id == project_id), Project(project_id, project_id))
        ]
    # One request per project works whether or not the API allows omitting project_id.
    return [ProjectEntries(p, client.entries(dev, rng.start, rng.end, p.id)) for p in chosen]


def _group_key(day: str, by: str) -> str:
    when = date.fromisoformat(day)
    if by == "week":
        monday = when - timedelta(days=when.weekday())
        return f"week of {monday}"
    return when.strftime("%B %Y")


def _total(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def render_entries(groups: list[ProjectEntries], rng: DateRange, by: str = "none") -> str:
    """Entries sorted by date, with optional weekly or monthly subtotals and totals."""
    if by not in ("none", "week", "month"):
        raise WorklogError("--by must be none, week or month")
    lines = [f"Timesheet entries: {rng.start} to {rng.end}"]
    grand = 0
    for group in groups:
        entries = sorted(group.entries, key=lambda e: (e.date, e.id))
        lines += ["", f"Project: {group.project.name}  [{group.project.id}]"]
        if not entries:
            lines.append("  (no entries)")
            continue
        current, subtotal = None, 0
        for entry in entries:
            key = _group_key(entry.date, by) if by != "none" else None
            if key != current and current is not None:
                lines.append(f"  -- {current}: {_total(subtotal)}")
                subtotal = 0
            current = key if key is not None else current
            subtotal += entry.total_minutes
            lines.append(
                f"  {entry.date}  {entry.hours}h {entry.minutes:02d}m  {entry.description}"
            )
        if by != "none" and current is not None:
            lines.append(f"  -- {current}: {_total(subtotal)}")
        lines.append(
            f"  Total: {_total(group.total_minutes)} in {len(entries)} entr"
            f"{'y' if len(entries) == 1 else 'ies'}"
        )
        grand += group.total_minutes
    if len(groups) > 1:
        lines += ["", f"All projects: {_total(grand)}"]
    return mask_secrets("\n".join(lines))
