from __future__ import annotations

import pytest

from gitworklog.tools.git import GitRunner
from helpers import Repo, build_sample_repo, init_repo


@pytest.fixture
def repo(tmp_path) -> Repo:
    return init_repo(tmp_path / "repo")


@pytest.fixture
def sample(tmp_path) -> Repo:
    return build_sample_repo(tmp_path / "sample")


@pytest.fixture
def sample_git(sample) -> GitRunner:
    return GitRunner(sample.path)
