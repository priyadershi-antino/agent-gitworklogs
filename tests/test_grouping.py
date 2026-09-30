from datetime import datetime, timedelta, timezone

import pytest

from gitworklog.models import Commit, FileChange
from gitworklog.services.grouping import (
    file_area,
    file_module,
    group_commits,
    is_vague,
    parse_subject,
    split_words,
)

T0 = datetime(2026, 9, 25, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
_counter = iter(range(10_000))


def mk(subject: str, *files: str) -> Commit:
    n = next(_counter)
    return Commit(
        sha=f"{n:040x}",
        author="Dev",
        email="dev@example.com",
        authored_at=T0 + timedelta(minutes=10 * n),
        subject=subject,
        files=[FileChange(f, 10, 2) for f in files],
    )


def fifteen_commits() -> list[Commit]:
    rider = [
        mk("feat(rider): add availability endpoint", "backend/riderController.py"),
        mk("feat(rider): add availability service", "backend/riderService.py"),
        mk("feat(rider): persist availability", "backend/riderRepository.py"),
        mk("feat(rider): expose availability status", "backend/riderController.py"),
        mk("test(rider): add availability tests", "tests/test_rider.py"),
        mk("feat(rider): validate availability window", "backend/riderService.py"),
    ]
    api = [
        mk("fix(api): validate request payloads", "backend/api/validation.py"),
        mk("fix(api): return 422 on invalid body", "backend/api/validation.py"),
        mk("fix(api): handle upstream timeouts", "backend/api/errors.py"),
        mk("fix(api): map errors to json responses", "backend/api/errors.py"),
        mk("fix(api): log unexpected errors", "backend/api/errors.py"),
    ]
    profile = [
        mk("feat(profile): add avatar upload", "frontend/RiderProfile.jsx"),
        mk("feat(profile): show availability badge", "frontend/RiderProfile.jsx"),
        mk("feat(profile): improve loading state", "frontend/ProfileSkeleton.jsx"),
        mk("style(profile): align profile header", "frontend/RiderProfile.css"),
    ]
    return rider + api + profile


def test_fifteen_commits_become_three_tasks():
    commits = fifteen_commits()
    tasks = group_commits(commits)
    assert len(tasks) == 3
    assert sum(len(t.commits) for t in tasks) == 15  # nothing dropped or invented
    by_topic = {t.topic.split()[0]: t for t in tasks}
    assert set(by_topic) == {"rider", "api", "profile"}
    rider = by_topic["rider"]
    assert rider.kind == "feat" and rider.title == "Implemented rider availability"
    assert by_topic["api"].title == "Fixed api errors"
    assert by_topic["api"].kind == "fix" and by_topic["api"].area == "Backend"
    assert by_topic["profile"].area == "Frontend"
    assert all(t.confidence == "High" for t in tasks)
    assert "backend/riderController.py" in by_topic["rider"].files


def test_single_descriptive_commit_uses_subject():
    [task] = group_commits([mk("feat(auth): handle expired access tokens", "auth/tokens.py")])
    assert task.title == "Handle expired access tokens"
    assert task.confidence == "High"


def test_vague_commits_are_low_confidence_and_described_by_files():
    [task] = group_commits(
        [mk("wip", "src/billing/invoice.py"), mk("wip", "src/billing/invoice.py")]
    )
    assert task.confidence == "Low"
    assert task.bullets == ["Changes in invoice.py"]
    assert "billing" in task.title or "invoice" in task.title


def test_similar_unscoped_messages_group_with_medium_confidence():
    tasks = group_commits(
        [
            mk("Add rider availability endpoint", "a/one.py"),
            mk("Improve rider availability validation", "b/two.py"),
            mk("Update invoice pdf layout", "c/pdf.py"),
        ]
    )
    grouped = [t for t in tasks if len(t.commits) == 2]
    assert len(tasks) == 2 and grouped
    assert grouped[0].confidence == "Medium"


def test_distinct_scopes_do_not_merge_on_similarity():
    tasks = group_commits(
        [
            mk("feat(rider): add rider availability", "a.py"),
            mk("feat(profile): add rider availability badge", "b.jsx"),
        ]
    )
    assert len(tasks) == 2


def test_empty_input():
    assert group_commits([]) == []


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("feat(rider): add x", ("feat", "rider", "add x")),
        ("fix: bug", ("fix", None, "bug")),
        ("Fixed crash on login", ("fix", None, "Fixed crash on login")),
        ("random words", ("other", None, "random words")),
    ],
)
def test_parse_subject(subject, expected):
    assert parse_subject(subject) == expected


@pytest.mark.parametrize(
    ("path", "area"),
    [
        ("tests/test_a.py", "Tests"),
        ("src/app.test.ts", "Tests"),
        ("docs/guide.md", "Docs"),
        ("frontend/src/App.jsx", "Frontend"),
        ("backend/api/views.py", "Backend"),
        (".github/workflows/ci.yml", "Config/CI"),
        ("image.png", "Other"),
    ],
)
def test_file_area(path, area):
    assert file_area(path) == area


def test_helpers():
    assert split_words("riderController_v2") == ["rider", "controller", "v2"]
    assert file_module("backend/controllers/riderController.py") == "rider"
    assert file_module("src/billing/invoice.py") == "billing"
    assert is_vague("wip") and is_vague("fix") and not is_vague("fix(auth): handle expiry")


def test_distinct_scopes_do_not_merge_even_with_shared_files():
    tasks = group_commits(
        [
            mk("feat(rider): add availability", "backend/riderService.py"),
            mk("feat(profile): add bio field", "backend/riderService.py"),
        ]
    )
    assert len(tasks) == 2
