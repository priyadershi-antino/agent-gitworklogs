"""Nexus timesheet integration: client, planning, execution, CLI and agent tools."""

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from fake_nexus import DEV_ID, OTHER_PROJECT_ID, PROJECT_ID, FakeNexus, make_token
from gitworklog.cli import app
from gitworklog.config import ConfigError, load_settings, save_repo_config_value
from gitworklog.safety import DenyAllApprover, OperationDenied, ScriptedApprover
from gitworklog.services.timesheet import (
    CREATE,
    SKIP_EXISTS,
    SKIP_MULTIPLE,
    SKIP_NO_HOURS,
    SKIP_SAME,
    UPDATE,
    NothingToSubmit,
    TimesheetSession,
    collect_plan,
    execute_plan,
    parse_day_hours,
    plan_timesheet,
    render_plan,
    split_hours,
    task_description,
)
from gitworklog.services.worklog import WorklogError, build_worklog, resolve_range
from gitworklog.tools.git import GitRunner
from gitworklog.tools.nexus import (
    NexusAuthError,
    NexusClient,
    NexusError,
    TimesheetEntry,
    check_not_expired,
    clean_token,
    developer_id_for,
    jwt_claims,
    refresh_access_token,
    resolve_access_token,
)
from gitworklog.tools.registry import ToolContext, build_registry

DAY1, DAY2 = "2026-09-25", "2026-09-26"


@pytest.fixture
def nexus():
    server = FakeNexus(make_token(developer_id=DEV_ID))
    server.start()
    yield server
    server.stop()


@pytest.fixture
def session(nexus):
    return TimesheetSession(
        client=NexusClient(nexus.base_url, nexus.token),
        developer_id=DEV_ID,
        base_url=nexus.base_url,
        project_id=PROJECT_ID,
    )


def _log(git, start=DAY1, end=DAY2):
    return build_worklog(git, resolve_range(date_from=start, date_to=end))


def _plan(git, existing=(), **kw):
    kw.setdefault("hours", 8)
    return plan_timesheet(
        _log(git),
        list(existing),
        project_id=PROJECT_ID,
        project_name="Rider App",
        developer_id=DEV_ID,
        **kw,
    )


# ------------------------------------------------------------------------------ tokens


def test_jwt_claims_and_expiry():
    token = make_token(developer_id=DEV_ID)
    assert jwt_claims(token)["developer_id"] == DEV_ID
    assert jwt_claims("not-a-jwt") == {} and jwt_claims("a.b!.c") == {}
    check_not_expired(token)
    with pytest.raises(NexusAuthError, match="expired"):
        check_not_expired(make_token(exp_in=-5))
    assert developer_id_for(token) == DEV_ID


def test_developer_id_override_and_missing(monkeypatch):
    monkeypatch.setenv("NEXUS_DEVELOPER_ID", "other-dev-123")
    assert developer_id_for(make_token()) == "other-dev-123"
    monkeypatch.delenv("NEXUS_DEVELOPER_ID")
    with pytest.raises(NexusError, match="developer id"):
        developer_id_for(make_token())


def test_clean_token_accepts_bearer_prefix_and_rejects_junk():
    token = make_token()
    assert clean_token(f'  "Bearer {token}" ') == token
    for junk in ("hello", "a.b", "a..c", ""):
        with pytest.raises(NexusAuthError):
            clean_token(junk)


def test_resolve_access_token(monkeypatch):
    monkeypatch.setenv("NEXUS_ACCESS_TOKEN", "")
    monkeypatch.setenv("NEXUS_TOKEN", "")
    with pytest.raises(NexusAuthError, match="NEXUS_ACCESS_TOKEN"):
        resolve_access_token(prompt=False)
    monkeypatch.setenv("NEXUS_ACCESS_TOKEN", make_token(exp_in=-1))
    monkeypatch.setenv("NEXUS_TOKEN", "")
    with pytest.raises(NexusAuthError, match="expired"):
        resolve_access_token(prompt=False)
    token = make_token()
    monkeypatch.setenv("NEXUS_ACCESS_TOKEN", f"Bearer {token}")
    monkeypatch.setenv("NEXUS_TOKEN", "")
    assert resolve_access_token(prompt=False) == token


def test_refresh_access_token(monkeypatch):
    old_token = make_token(exp_in=-1)
    refresh_token = "refresh-token-for-tests"
    server = FakeNexus(old_token, refresh_token=refresh_token)
    server.start()
    try:
        monkeypatch.delenv("NEXUS_TOKEN", raising=False)
        monkeypatch.setenv("NEXUS_ACCESS_TOKEN", old_token)
        monkeypatch.setenv("NEXUS_REFRESH_TOKEN", refresh_token)
        monkeypatch.setenv("NEXUS_API_URL", server.base_url)
        refreshed = refresh_access_token(server.base_url)
        assert refreshed != old_token
        assert jwt_claims(refreshed)["developer_id"] == DEV_ID
    finally:
        server.stop()


# ------------------------------------------------------------------------------ client


@pytest.mark.parametrize("shape", ["list", "data", "nested"])
def test_client_parses_every_response_envelope(shape):
    server = FakeNexus(make_token(), shape=shape)
    server.start()
    try:
        server.add_entry(DAY1, "work", 5, 30)
        client = NexusClient(server.base_url, server.token)
        assert [p.name for p in client.projects(DEV_ID)] == ["Rider App", "Internal Tools"]
        [entry] = client.entries(DEV_ID, date(2026, 9, 25), date(2026, 9, 26), PROJECT_ID)
        assert (entry.date, entry.hours, entry.minutes) == (DAY1, 5, 30)  # ISO datetime trimmed
        assert entry.total_minutes == 330
    finally:
        server.stop()


def test_client_unexpected_shape_is_a_clear_error():
    server = FakeNexus(make_token(), shape="weird")
    server.start()
    try:
        with pytest.raises(NexusError, match="Unexpected response shape"):
            NexusClient(server.base_url, server.token).projects(DEV_ID)
    finally:
        server.stop()


def test_entries_query_and_auth_header(nexus):
    NexusClient(nexus.base_url, nexus.token).entries(
        DEV_ID, date(2026, 9, 28), date(2026, 10, 4), PROJECT_ID
    )
    request = nexus.requests[0]
    assert request["query"] == {
        "developer_id": DEV_ID,
        "start_date": "2026-09-28",
        "end_date": "2026-10-04",
        "project_id": PROJECT_ID,
    }
    assert request["auth"] == f"Bearer {nexus.token}"


def test_create_and_update_send_the_documented_bodies(nexus):
    client = NexusClient(nexus.base_url, nexus.token)
    result = client.create_entry(
        project_id=PROJECT_ID,
        developer_id=DEV_ID,
        entry_date=date(2026, 10, 4),
        description="feature refinement and bug fixes",
        hours=5,
        minutes=0,
    )
    entry_id = result["data"]["id"]
    client.update_entry(entry_id, description="changed", hours=6, minutes=15)
    create, update = nexus.writes()
    assert (create["method"], create["path"]) == ("POST", "/api/v1/timesheet/entries")
    assert create["body"] == {
        "project_id": PROJECT_ID,
        "task_description": "feature refinement and bug fixes",
        "hours": 5,
        "minutes": 0,
        "entry_date": "2026-10-04",
        "developer_id": DEV_ID,
    }
    assert (update["method"], update["path"]) == ("PUT", f"/api/v1/timesheet/entries/{entry_id}")
    assert update["body"] == {"task_description": "changed", "hours": 6, "minutes": 15}
    with pytest.raises(NexusError, match="Invalid entry id"):
        client.update_entry("../../admin", description="x", hours=1, minutes=0)


def test_rejected_token_does_not_leak_it(nexus):
    bad = make_token(developer_id=DEV_ID, extra="different")
    with pytest.raises(NexusAuthError) as caught:
        NexusClient(nexus.base_url, bad).projects(DEV_ID)
    assert bad not in str(caught.value) and "expired" in str(caught.value)
    assert bad not in repr(NexusClient(nexus.base_url, bad))


def test_server_error_text_is_masked():
    server = FakeNexus(make_token(), error_body='failed token="ghp_' + "z" * 36 + '"')
    server.start()
    try:
        server.fail_post_for = "2026-10-04"
        with pytest.raises(NexusError) as caught:
            NexusClient(server.base_url, server.token).create_entry(
                project_id=PROJECT_ID,
                developer_id=DEV_ID,
                entry_date=date(2026, 10, 4),
                description="x",
                hours=1,
                minutes=0,
            )
        assert "ghp_" + "z" * 36 not in str(caught.value) and "HTTP 500" in str(caught.value)
    finally:
        server.stop()


def test_redirects_are_never_followed(nexus):
    other = FakeNexus(nexus.token)
    other.start()
    try:
        nexus.redirect_to = other.base_url + "/timesheet/projects"
        with pytest.raises(NexusError, match="redirect"):
            NexusClient(nexus.base_url, nexus.token).projects(DEV_ID)
        assert other.requests == []  # the token never reached the redirect target
    finally:
        other.stop()


def test_unreachable_server_is_a_clear_error():
    client = NexusClient("http://127.0.0.1:1/api/v1", make_token(), timeout=2)
    with pytest.raises(NexusError, match="Could not reach Nexus"):
        client.projects(DEV_ID)


# ---------------------------------------------------------------------------- planning


def test_split_hours_and_validation():
    assert split_hours(7.5) == (7, 30) and split_hours(8) == (8, 0) and split_hours(0.25) == (0, 15)
    for bad in (0, -1, 24.5, True):
        with pytest.raises(WorklogError):
            split_hours(bad)


def test_parse_day_hours():
    assert parse_day_hours(["2026-10-02=4", "2026-10-03=7.5"]) == {
        date(2026, 10, 2): 4.0,
        date(2026, 10, 3): 7.5,
    }
    for bad in ("2026-10-02", "tomorrow=4", "2026-10-02=abc", "2026-10-02=30"):
        with pytest.raises(WorklogError):
            parse_day_hours([bad])


def test_hours_are_required(sample_git):
    with pytest.raises(WorklogError, match="Hours are required"):
        _plan(sample_git, hours=None)


def test_plan_creates_one_entry_per_commit_day(sample_git):
    plan = _plan(sample_git, hours=7.5)
    assert [(e.day.isoformat(), e.action, e.hours, e.minutes) for e in plan.entries] == [
        (DAY1, CREATE, 7, 30),
        (DAY2, CREATE, 7, 30),
    ]
    first = plan.entries[0]
    assert first.commits == 7 and "rider availability" in first.description.lower()
    assert ";" in first.description  # several tasks joined on one line


def test_per_day_hours_override_and_days_without_commits(sample_git):
    plan = _plan(sample_git, hours=8, day_hours={date(2026, 9, 26): 3.0, date(2026, 9, 24): 6.0})
    assert [(e.hours, e.minutes) for e in plan.entries] == [(8, 0), (3, 0)]
    assert any("2026-09-24" in n and "outside" in n for n in plan.notes)
    only_override = _plan(sample_git, hours=None, day_hours={date(2026, 9, 26): 3.0})
    assert [e.action for e in only_override.entries] == [SKIP_NO_HOURS, CREATE]


def test_existing_entries_are_not_overwritten_by_default(sample_git):
    old = TimesheetEntry("e1", DAY1, "feature refinement", 5, 0, PROJECT_ID)
    plan = _plan(sample_git, [old])
    assert [e.action for e in plan.entries] == [SKIP_EXISTS, CREATE]
    assert plan.entries[0].existing == old and "--update" in plan.entries[0].reason
    updated = _plan(sample_git, [old], update_existing=True)
    assert [e.action for e in updated.entries] == [UPDATE, CREATE]


def test_identical_and_duplicate_entries(sample_git):
    first = _plan(sample_git).entries[0]
    same = TimesheetEntry("e1", DAY1, first.description, first.hours, first.minutes)
    assert _plan(sample_git, [same], update_existing=True).entries[0].action == SKIP_SAME
    twins = [TimesheetEntry("e2", DAY1, "a", 1, 0), TimesheetEntry("e3", DAY1, "b", 2, 0)]
    assert _plan(sample_git, twins, update_existing=True).entries[0].action == SKIP_MULTIPLE


def test_description_is_cut_at_a_task_boundary():
    titles = ["Implemented rider availability", "Fixed auth tokens", "Implemented ui profile"]
    assert task_description(titles, 200) == (
        "Implemented rider availability; Fixed auth tokens; Implemented ui profile",
        False,
    )
    text, truncated = task_description(titles, 50)
    assert truncated and len(text) <= 50 and text.endswith("…")
    assert text == "Implemented rider availability; Fixed auth tokens…"
    assert task_description([], 50) == ("Development work", False)
    assert task_description(["Same.", "Same"], 50)[0] == "Same"


def test_secrets_in_commit_subjects_never_reach_the_description(repo):
    token = "ghp_" + "a" * 36
    repo.commit(
        f"feat(auth): use token {token} for login",
        {"a.py": "x\n"},
        when="2026-09-25T10:00:00+05:30",
    )
    git = GitRunner(repo.path)
    plan = plan_timesheet(
        _log(git), [], project_id=PROJECT_ID, project_name="P", developer_id=DEV_ID, hours=8
    )
    assert token not in plan.entries[0].description
    assert token not in render_plan(plan)


def test_render_plan_labels_hours_as_user_provided(sample_git):
    text = render_plan(_plan(sample_git, [TimesheetEntry("e1", DAY1, "old text", 5, 0)]))
    assert "USER-PROVIDED" in text and "cannot reliably determine" in text
    assert 'existing entry: 5h 00m: "old text"' in text and "SKIP EXISTS" in text


# --------------------------------------------------------------------------- execution


def _collect(sample_git, session, **kw):
    kw.setdefault("hours", 8)
    return collect_plan(sample_git, session, resolve_range(date_from=DAY1, date_to=DAY2), **kw)


def test_collect_plan_reads_project_name_and_existing_entries(sample_git, session, nexus):
    nexus.add_entry(DAY1, "typed by hand", 4)
    nexus.add_entry(DAY2, "other project", 2, project_id=OTHER_PROJECT_ID)
    plan = _collect(sample_git, session)
    assert plan.project_name == "Rider App"
    assert [e.action for e in plan.entries] == [
        SKIP_EXISTS,
        CREATE,
    ]  # other project's entry ignored
    assert nexus.writes() == []  # planning never writes


def test_collect_plan_needs_project_and_hours(sample_git, session):
    with pytest.raises(WorklogError, match="Hours are required"):
        _collect(sample_git, session, hours=None)
    session.project_id = None
    with pytest.raises(NexusError, match="timesheet init"):
        _collect(sample_git, session)


def test_approved_plan_is_sent_and_verified(sample_git, session, nexus):
    plan = _collect(sample_git, session, hours=7.5)
    approver = ScriptedApprover([True])
    results = execute_plan(session, approver, plan)
    assert len(approver.asked) == 1  # one approval for the whole plan
    op = approver.asked[0]
    assert len(op.all_commands) == 2 and all(c.startswith("POST ") for c in op.all_commands)
    assert "hours provided by you" in op.preview and not op.destructive
    assert [(r.ok, r.verified) for r in results] == [(True, True), (True, True)]
    sent = [w["body"] for w in nexus.writes()]
    assert [(b["entry_date"], b["hours"], b["minutes"]) for b in sent] == [
        (DAY1, 7, 30),
        (DAY2, 7, 30),
    ]
    assert {b["project_id"] for b in sent} == {PROJECT_ID}
    assert {b["developer_id"] for b in sent} == {DEV_ID}


def test_denied_plan_sends_nothing(sample_git, session, nexus):
    plan = _collect(sample_git, session)
    with pytest.raises(OperationDenied):
        execute_plan(session, DenyAllApprover(), plan)
    assert nexus.writes() == []


def test_overwriting_needs_destructive_approval_and_uses_put(sample_git, session, nexus):
    entry_id = nexus.add_entry(DAY1, "typed by hand", 4)
    plan = _collect(sample_git, session, update_existing=True)
    approver = ScriptedApprover([True])
    execute_plan(session, approver, plan)
    op = approver.asked[0]
    assert op.destructive and 'replaces: 4h 00m "typed by hand"' in op.preview
    assert any(c.startswith("PUT ") and entry_id in c for c in op.all_commands)
    assert [w["method"] for w in nexus.writes()] == ["PUT", "POST"]
    assert nexus.entries[0]["hours"] == 8


def test_failure_stops_and_reports_what_was_sent(sample_git, session, nexus):
    nexus.fail_post_for = DAY2
    plan = _collect(sample_git, session)
    results = execute_plan(session, ScriptedApprover([True]), plan)
    assert [r.ok for r in results] == [True, False]
    assert "HTTP 500" in results[1].error and results[0].verified
    assert len(nexus.entries) == 1


def test_nothing_to_submit(sample_git, session, nexus):
    nexus.add_entry(DAY1, "x", 1)
    nexus.add_entry(DAY2, "y", 1)
    plan = _collect(sample_git, session)
    assert not plan.writes
    with pytest.raises(NothingToSubmit):
        execute_plan(session, ScriptedApprover([True]), plan)


# --------------------------------------------------------------------------------- CLI

runner = CliRunner()


@pytest.fixture
def cli_repo(sample):
    save_repo_config_value(sample.path, "timesheet_project_id", PROJECT_ID)
    return sample


def run(repo, nexus, *args, input=None, piped_approval=False, token=None):
    env = {
        "NEXUS_API_URL": nexus.base_url,
        "NEXUS_TOKEN": "",  # a real .env must not win over the test token
        "NEXUS_ACCESS_TOKEN": token or nexus.token,
    }
    if piped_approval:
        env["GITWORKLOG_ALLOW_PIPED_APPROVAL"] = "1"
    return runner.invoke(
        app, ["--repo", str(repo.path), "--no-llm", "timesheet", *args], input=input, env=env
    )


FILL = ("fill", "--from", DAY1, "--to", DAY2, "--hours", "7")


def test_cli_dry_run_shows_plan_and_sends_nothing(cli_repo, nexus):
    result = run(cli_repo, nexus, *FILL, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Rider App" in result.output and "CREATE" in result.output and "7h 00m" in result.output
    assert nexus.writes() == []


def test_cli_fill_with_approval(cli_repo, nexus):
    result = run(cli_repo, nexus, *FILL, input="y\n", piped_approval=True)
    assert result.exit_code == 0, result.output
    assert "Approval required" in result.output and "POST " in result.output
    assert result.output.count("verified in Nexus") == 2
    assert len(nexus.writes()) == 2 and nexus.token not in result.output


def test_cli_fill_rejected_or_non_interactive_sends_nothing(cli_repo, nexus):
    assert (
        "Nothing was sent" in run(cli_repo, nexus, *FILL, input="n\n", piped_approval=True).output
    )
    result = run(cli_repo, nexus, *FILL, input="y\n")  # piped input is never an approval
    assert "NOT approved" in result.output and nexus.writes() == []


def test_cli_update_requires_typing_yes(cli_repo, nexus):
    nexus.add_entry(DAY1, "typed by hand", 4)
    args = (*FILL, "--update")
    refused = run(cli_repo, nexus, *args, input="y\n", piped_approval=True)
    assert "Nothing was sent" in refused.output and nexus.writes() == []
    done = run(cli_repo, nexus, *args, input="yes\n", piped_approval=True)
    assert done.exit_code == 0 and "UPDATE" in done.output
    assert nexus.entries[0]["hours"] == 7 and len(nexus.entries) == 2


def test_cli_errors_are_clean(cli_repo, nexus):
    no_hours = run(cli_repo, nexus, "fill", "--from", DAY1, "--to", DAY2)
    assert no_hours.exit_code == 1 and "Hours are required" in no_hours.output
    expired = run(cli_repo, nexus, *FILL, token=make_token(exp_in=-10))
    assert expired.exit_code == 1 and "expired" in expired.output
    no_token = runner.invoke(
        app,
        ["--repo", str(cli_repo.path), "--no-llm", "timesheet", "projects"],
        env={"NEXUS_TOKEN": "", "NEXUS_ACCESS_TOKEN": "", "NEXUS_API_URL": nexus.base_url},
    )
    assert no_token.exit_code == 1 and "NEXUS_ACCESS_TOKEN" in no_token.output


def test_cli_projects_init_and_show(sample, nexus):
    listing = run(sample, nexus, "projects")
    assert "Rider App" in listing.output and "Internal Tools" in listing.output
    assert run(sample, nexus, "fill", "--hours", "8").exit_code == 1  # no project chosen yet
    result = run(sample, nexus, "init", "--project", "rider")
    assert result.exit_code == 0 and "Rider App" in result.output
    config = json.loads((sample.path / ".gitworklog" / "config.json").read_text())
    assert config == {"timesheet_project_id": PROJECT_ID}
    ambiguous = run(sample, nexus, "init", "--project", "e")  # matches both names
    assert ambiguous.exit_code == 1 and "exactly one" in ambiguous.output
    nexus.add_entry(DAY1, "feature refinement", 5, 30)
    shown = run(sample, nexus, "show", "--from", DAY1, "--to", DAY2)
    assert "5h 30m" in shown.output and "Total: 5h 30m" in shown.output


# ---------------------------------------------------------------------------- agent tools


def _registry(sample_git, session, approver):
    return build_registry(
        ToolContext(
            git=sample_git,
            approver=approver,
            timesheet_session=lambda: session,
            today=date(2026, 9, 30),
        )
    )


def test_agent_preview_is_read_only(sample_git, session, nexus):
    reg = _registry(sample_git, session, DenyAllApprover())
    result = reg.dispatch(
        "timesheet_preview", json.dumps({"date_from": DAY1, "date_to": DAY2, "hours": 8})
    )
    assert result["ok"] and len(result["result"]["entries"]) == 2
    assert nexus.writes() == []
    no_hours = reg.dispatch("timesheet_preview", json.dumps({"date_from": DAY1}))
    assert not no_hours["ok"] and "Hours are required" in no_hours["error"]


def test_agent_submit_needs_hours_and_approval(sample_git, session, nexus):
    denied = _registry(sample_git, session, DenyAllApprover()).dispatch(
        "timesheet_submit", json.dumps({"date_from": DAY1, "date_to": DAY2, "hours": 8})
    )
    assert denied["denied"] and nexus.writes() == []
    missing = _registry(sample_git, session, DenyAllApprover()).dispatch(
        "timesheet_submit", json.dumps({"date_from": DAY1})
    )
    assert not missing["ok"] and "Missing required" in missing["error"]
    boolean = _registry(sample_git, session, DenyAllApprover()).dispatch(
        "timesheet_submit", json.dumps({"date_from": DAY1, "hours": True})
    )
    assert not boolean["ok"] and "must be of type" in boolean["error"]
    ok = _registry(sample_git, session, ScriptedApprover([True])).dispatch(
        "timesheet_submit", json.dumps({"date_from": DAY1, "date_to": DAY2, "hours": 8})
    )
    assert ok["ok"] and all(r["verified"] for r in ok["result"]["results"])
    assert len(nexus.writes()) == 2


def test_agent_tools_unavailable_without_a_session(sample_git):
    reg = build_registry(ToolContext(git=sample_git, approver=DenyAllApprover()))
    result = reg.dispatch("timesheet_preview", json.dumps({"date_from": DAY1}))
    assert not result["ok"] and "not available" in result["error"]


# --------------------------------------------------------------------------------- config


def test_nexus_url_comes_only_from_the_environment(tmp_path, monkeypatch):
    (tmp_path / ".gitworklog").mkdir()
    (tmp_path / ".gitworklog" / "config.json").write_text(
        json.dumps(
            {"timesheet_base_url": "https://evil.example/api", "timesheet_description_max": 120}
        )
    )
    monkeypatch.delenv("NEXUS_API_URL", raising=False)
    settings = load_settings(tmp_path)
    assert settings.timesheet_base_url == "https://rms2-be.antino.ca/api/v1"  # repo value ignored
    assert settings.timesheet_description_max == 120


@pytest.mark.parametrize(
    "url", ["http://example.com/api", "ftp://x/api", "https://user:pw@host/api", "https:///x"]
)
def test_unsafe_nexus_urls_are_rejected(tmp_path, monkeypatch, url):
    monkeypatch.setenv("NEXUS_API_URL", url)
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_project_id_validation_and_save_keeps_other_keys(tmp_path):
    save_repo_config_value(tmp_path, "base_branch", "develop")
    path = save_repo_config_value(tmp_path, "timesheet_project_id", PROJECT_ID)
    assert json.loads(path.read_text()) == {
        "base_branch": "develop",
        "timesheet_project_id": PROJECT_ID,
    }
    with pytest.raises(ConfigError):
        save_repo_config_value(tmp_path, "api_key", "x")
    path.write_text(json.dumps({"timesheet_project_id": "bad id!"}))
    with pytest.raises(ConfigError, match="project id"):
        load_settings(tmp_path)


# ------------------------------------------------------------------------------ listing

from gitworklog.services.timesheet import (  # noqa: E402
    list_entries,
    match_project,
    render_entries,
    resolve_listing_range,
)
from gitworklog.tools.nexus import Project  # noqa: E402

TODAY = date(2026, 10, 7)  # a Wednesday


@pytest.mark.parametrize(
    ("kwargs", "start", "end"),
    [
        ({}, "2026-10-05", "2026-10-07"),  # default: this week
        ({"period": "last-week"}, "2026-09-28", "2026-10-04"),
        ({"period": "month"}, "2026-10-01", "2026-10-07"),
        ({"period": "last-month"}, "2026-09-01", "2026-09-30"),
        ({"day": "2026-10-02"}, "2026-10-02", "2026-10-02"),
        ({"week": "2026-W40"}, "2026-09-28", "2026-10-04"),
        ({"week": "2026-10-02"}, "2026-09-28", "2026-10-04"),  # any date inside the week
        ({"month": "2026-09"}, "2026-09-01", "2026-09-30"),
        ({"month": "2028-02"}, "2028-02-01", "2028-02-29"),  # leap year
        ({"date_from": "2026-09-10", "date_to": "2026-09-20"}, "2026-09-10", "2026-09-20"),
    ],
)
def test_listing_ranges(kwargs, start, end):
    rng = resolve_listing_range(today=TODAY, **kwargs)
    assert (rng.start.isoformat(), rng.end.isoformat()) == (start, end)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"day": "02/10/2026"},
        {"week": "2026-W99"},
        {"week": "soon"},
        {"month": "2026-13"},
        {"month": "September"},
        {"month": "2026-09", "day": "2026-10-02"},
        {"period": "week", "month": "2026-09"},
        {"period": "fortnight"},
    ],
)
def test_listing_range_errors(kwargs):
    with pytest.raises(WorklogError):
        resolve_listing_range(today=TODAY, **kwargs)


def _seed(nexus):
    nexus.add_entry("2026-09-29", "api work", 6)
    nexus.add_entry("2026-09-30", "bug fixes", 7, 30)
    nexus.add_entry("2026-10-02", "ui polish", 5)
    nexus.add_entry("2026-10-01", "ops", 3, project_id=OTHER_PROJECT_ID)
    nexus.add_entry("2026-09-15", "earlier month", 8)


def test_list_entries_for_the_repository_project_by_default(session, nexus):
    _seed(nexus)
    [group] = list_entries(session, resolve_listing_range(month="2026-09"))
    assert group.project.name == "Rider App"
    assert sorted(e.date for e in group.entries) == ["2026-09-15", "2026-09-29", "2026-09-30"]
    assert group.total_minutes == (6 + 7 + 8) * 60 + 30


def test_list_entries_for_any_project_or_all(session, nexus):
    _seed(nexus)
    rng = resolve_listing_range(week="2026-W40")  # 28 Sep to 4 Oct
    [other] = list_entries(session, rng, project="internal")  # part of the name
    assert other.project.name == "Internal Tools"
    assert [e.description for e in other.entries] == ["ops"]
    both = list_entries(session, rng, all_projects=True)
    assert [g.project.name for g in both] == ["Rider App", "Internal Tools"]
    assert [len(g.entries) for g in both] == [3, 1]
    assert all(r["method"] == "GET" for r in nexus.requests)  # listing never writes
    with pytest.raises(WorklogError, match="not both"):
        list_entries(session, rng, project="rider", all_projects=True)
    with pytest.raises(NexusError, match="exactly one"):
        list_entries(session, rng, project="zzz")


def test_match_project_rules():
    projects = [Project("id-aaaaaa", "Rider App"), Project("id-bbbbbb", "Rider Admin")]
    assert match_project(projects, "id-bbbbbb").name == "Rider Admin"
    assert match_project(projects, "rider app").name == "Rider App"  # exact beats partial
    with pytest.raises(NexusError):
        match_project(projects, "rider")  # ambiguous


def test_render_entries_with_weekly_and_monthly_subtotals(session, nexus):
    _seed(nexus)
    rng = resolve_listing_range(date_from="2026-09-01", date_to="2026-10-31")
    groups = list_entries(session, rng, all_projects=True)
    plain = render_entries(groups, rng)
    assert "Project: Rider App" in plain and "Project: Internal Tools" in plain
    assert "2026-09-30  7h 30m  bug fixes" in plain and "All projects: 29h 30m" in plain
    weekly = render_entries(groups, rng, by="week")
    assert "-- week of 2026-09-28: 18h 30m" in weekly  # 6h + 7h30 + 5h (2 Oct)
    assert "-- week of 2026-09-14: 8h 00m" in weekly
    monthly = render_entries(groups, rng, by="month")
    assert "-- September 2026: 21h 30m" in monthly and "-- October 2026: 5h 00m" in monthly
    empty = resolve_listing_range(day="2026-01-01")
    assert "(no entries)" in render_entries(list_entries(session, empty, all_projects=True), empty)
    with pytest.raises(WorklogError):
        render_entries(groups, rng, by="year")


def test_cli_show_variants(cli_repo, nexus):
    _seed(nexus)
    month = run(cli_repo, nexus, "show", "--month", "2026-09")
    assert month.exit_code == 0, month.output
    assert "api work" in month.output and "ui polish" not in month.output
    assert "Total: 21h 30m in 3 entries" in month.output
    day = run(cli_repo, nexus, "show", "--date", "2026-09-30")
    assert "bug fixes" in day.output and "api work" not in day.output
    allp = run(cli_repo, nexus, "show", "--week", "2026-W40", "--all-projects", "--by", "week")
    assert "Internal Tools" in allp.output and "All projects: 21h 30m" in allp.output
    assert "-- week of 2026-09-28" in allp.output
    named = run(cli_repo, nexus, "show", "last-month", "--project", "internal")
    assert named.exit_code == 0 and "Internal Tools" in named.output


def test_cli_show_works_without_a_repo_project(sample, nexus):
    nexus.add_entry("2026-09-30", "bug fixes", 7, 30)
    result = run(sample, nexus, "show", "--month", "2026-09", "--project", "rider")
    assert result.exit_code == 0 and "7h 30m" in result.output
    assert run(sample, nexus, "show", "--month", "2026-09").exit_code == 1  # no project chosen


def test_cli_show_rejects_conflicting_options(cli_repo, nexus):
    result = run(cli_repo, nexus, "show", "--month", "2026-09", "--date", "2026-09-30")
    assert result.exit_code == 1 and "Use only one of" in result.output
    assert run(cli_repo, nexus, "show", "--by", "year").exit_code == 1


def test_agent_can_list_entries_read_only(sample_git, session, nexus):
    _seed(nexus)
    reg = _registry(sample_git, session, DenyAllApprover())
    args = json.dumps({"month": "2026-09", "all_projects": True})
    result = reg.dispatch("timesheet_entries", args)
    assert result["ok"]
    projects = {p["project"]: p for p in result["result"]["projects"]}
    assert projects["Rider App"]["total_minutes"] == (6 + 7 + 8) * 60 + 30
    assert projects["Internal Tools"]["entries"] == []  # its only entry is in October
    assert all(r["method"] == "GET" for r in nexus.requests)
    assert not reg.dispatch("timesheet_entries", json.dumps({"month": "bad"}))["ok"]
