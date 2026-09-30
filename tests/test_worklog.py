import csv
import io
import json
from datetime import date

import pytest

from gitworklog.services.worklog import (
    HOURS_NOTICE,
    WorklogError,
    build_worklog,
    render,
    resolve_range,
)
from gitworklog.tools.git import GitRunner
from helpers import OTHER, FakeLLM, init_repo

WED = date(2026, 9, 30)


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("today", WED, WED),
        ("yesterday", date(2026, 9, 29), date(2026, 9, 29)),
        ("week", date(2026, 9, 28), WED),
        ("this-week", date(2026, 9, 28), WED),
        ("last-week", date(2026, 9, 21), date(2026, 9, 27)),
        ("month", date(2026, 9, 1), WED),
        ("last-month", date(2026, 8, 1), date(2026, 8, 31)),
    ],
)
def test_resolve_range_periods(period, start, end):
    rng = resolve_range(period, today=WED)
    assert (rng.start, rng.end) == (start, end)


def test_resolve_range_explicit_and_errors():
    rng = resolve_range(date_from="2026-09-01", date_to="2026-09-30", today=WED)
    assert (rng.start, rng.end) == (date(2026, 9, 1), WED)
    assert resolve_range(date_from="2026-09-10", today=WED).end == WED
    with pytest.raises(WorklogError):
        resolve_range(date_from="2026-09-30", date_to="2026-09-01")
    with pytest.raises(WorklogError):
        resolve_range(date_from="30/09/2026")
    with pytest.raises(WorklogError):
        resolve_range("fortnight")


def _log(git, start="2026-09-25", end="2026-09-26", **kw):
    return build_worklog(git, resolve_range(date_from=start, date_to=end), **kw)


def test_worklog_groups_per_day_for_current_user(sample_git):
    log = _log(sample_git)
    assert log.author == "dev@example.com"
    assert [d.day for d in log.days] == [date(2026, 9, 25), date(2026, 9, 26)]
    day1 = log.days[0]
    assert len(day1.commits) == 7
    topics = {t.topic.split()[0] for t in day1.tasks}
    assert {"rider", "auth", "ui"} <= topics
    assert len(day1.tasks) < len(day1.commits)  # grouped, not one task per commit
    assert all(c.email == "dev@example.com" for c in log.commits)


def test_worklog_date_range_is_inclusive_and_author_filtered(sample_git):
    only_24 = _log(sample_git, "2026-09-24", "2026-09-24")
    assert only_24.days == []  # the 24th only has another author's commit
    everyone = _log(sample_git, "2026-09-24", "2026-09-24", all_authors=True)
    assert [c.email for c in everyone.commits] == [OTHER[1]]
    other = _log(sample_git, "2026-09-20", "2026-09-30", author="Other")
    assert len(other.commits) == 1


def test_worklog_uses_author_local_date(tmp_path):
    repo = init_repo(tmp_path / "tz")
    # 23:30 on the 25th in UTC-7 is already the 26th in UTC.
    repo.commit("feat: late night work", {"a.py": "a\n"}, when="2026-09-25T23:30:00-07:00")
    log = _log(GitRunner(repo.path), "2026-09-25", "2026-09-25")
    assert len(log.commits) == 1


def test_normal_render_never_invents_hours(sample_git):
    text = render(_log(sample_git), "normal")
    assert HOURS_NOTICE in text
    assert "Hours:" not in text and "SUGGESTED" not in text
    assert "timestamps, not hours" in text


def test_user_provided_hours_and_suggested_split(sample_git):
    text = render(_log(sample_git, hours=7), "normal")
    assert "Hours: 7 [USER-PROVIDED]" in text
    log = _log(sample_git, hours=8, split_hours=True)
    rows = list(csv.DictReader(io.StringIO(render(log, "csv"))))
    day1 = [r for r in rows if r["date"] == "2026-09-25"]
    assert abs(sum(float(r["hours_suggested_split"]) for r in day1) - 8) <= 0.5
    assert "SUGGESTED" in render(log, "normal")
    with pytest.raises(WorklogError):
        _log(sample_git, split_hours=True)
    with pytest.raises(WorklogError):
        _log(sample_git, hours=30)


def test_csv_json_structured_formal(sample_git):
    log = _log(sample_git)
    rows = list(csv.DictReader(io.StringIO(render(log, "csv"))))
    assert len(rows) == len(log.tasks)
    assert {"date", "task", "commits", "confidence", "hours_user_provided"} <= set(rows[0])
    assert all(r["hours_user_provided"] == "" for r in rows)
    data = json.loads(render(log, "json"))
    assert data["hours_notice"] == HOURS_NOTICE
    assert sum(len(d["tasks"]) for d in data["days"]) == len(log.tasks)
    assert "Date | Task | Evidence | Confidence" in render(log, "structured")
    assert render(log, "formal").startswith("Sep 25, 2026:")
    with pytest.raises(WorklogError):
        render(log, "xml")


def test_empty_period(sample_git):
    log = _log(sample_git, "2026-01-01", "2026-01-02")
    assert "No commits found" in render(log, "normal")
    assert render(log, "csv").count("\n") == 1  # header only


def test_llm_enrichment_is_validated(sample_git):
    base = _log(sample_git, "2026-09-26", "2026-09-26")
    ids = [t.id for t in base.tasks]
    reply = {
        "tasks": [
            {
                "id": ids[0],
                "title": "Refined settings page",
                "bullets": ["Updated Settings.jsx"],
                "summary": "Refined the settings page.",
            },
            {"id": "2026-09-26-T999", "title": "Invented task", "bullets": ["made up"]},
        ]
    }
    log = _log(sample_git, "2026-09-26", "2026-09-26", llm=FakeLLM([reply]))
    assert log.enriched
    assert [t.id for t in log.tasks] == ids  # no task added
    assert log.tasks[0].title == "Refined settings page"
    assert "Invented task" not in render(log, "normal")


def test_llm_failure_falls_back(sample_git):
    log = _log(sample_git, "2026-09-26", "2026-09-26", llm=FakeLLM(default="not json"))
    assert not log.enriched
    assert any("LLM enrichment unavailable" in n for n in log.notes)
