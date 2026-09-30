import json

import pytest

from gitworklog.config import ConfigError, load_repo_config, load_settings
from gitworklog.safety import (
    DenyAllApprover,
    OperationDenied,
    PreApproved,
    ProposedOperation,
    ScriptedApprover,
    find_secrets,
    is_sensitive_path,
    mask_secrets,
    require_approval,
)

FAKE_NVAPI = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
FAKE_OPENAI = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz0123"
FAKE_GH = "ghp_" + "a" * 36


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"NVIDIA_API_KEY={FAKE_NVAPI}", FAKE_NVAPI),
        (f'client = OpenAI(api_key="{FAKE_OPENAI}")', FAKE_OPENAI),
        (f"token: {FAKE_GH}", FAKE_GH),
        ("aws = 'AKIAABCDEFGHIJKLMNOP'", "AKIAABCDEFGHIJKLMNOP"),
        ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
        ("+SECRET_TOKEN=zzzzzzzzzz", "zzzzzzzzzz"),
        ('password = "correct-horse-battery"', "correct-horse-battery"),
        ("url = https://bob:s3cretpass@example.com/repo.git", "s3cretpass"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ],
)
def test_mask_secrets_removes_values(text, secret):
    masked = mask_secrets(text)
    assert secret not in masked
    assert "[REDACTED:" in masked


@pytest.mark.parametrize(
    "text",
    [
        "token = get_token()",
        "password_field = form.password",
        "MAX_TOKENS=8192",
        "def check_secret(value):",
        "the api key is loaded from the environment",
    ],
)
def test_mask_secrets_leaves_normal_code(text):
    assert mask_secrets(text) == text


def test_find_secrets_reports_lines_not_values():
    hits = find_secrets(f"a = 1\nkey = '{FAKE_NVAPI}'\n")
    assert hits and hits[0].line == 2
    assert all(FAKE_NVAPI not in repr(h) for h in hits)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", True),
        ("config/.env.production", True),
        ("id_rsa", True),
        ("certs/server.pem", True),
        (".env.example", False),
        ("src/env.py", False),
        ("id_rsa.pub", False),
        ("README.md", False),
    ],
)
def test_is_sensitive_path(path, expected):
    assert is_sensitive_path(path) is expected


def _op(cmd=("commit", "-m", "x")):
    return ProposedOperation(kind="commit", summary="s", command=list(cmd))


def test_require_approval_denies_by_default():
    with pytest.raises(OperationDenied):
        require_approval(DenyAllApprover(), _op())


def test_scripted_approver_records_questions():
    approver = ScriptedApprover([True, False])
    require_approval(approver, _op())
    with pytest.raises(OperationDenied):
        require_approval(approver, _op())
    assert len(approver.asked) == 2


def test_pre_approved_only_matches_exact_commands():
    granted = PreApproved([["commit", "-m", "x"]])
    assert granted.confirm(_op())
    assert not granted.confirm(_op(("push", "origin", "main")))


def test_repo_config_rejects_secrets(tmp_path):
    (tmp_path / ".gitworklog").mkdir()
    (tmp_path / ".gitworklog" / "config.json").write_text(json.dumps({"api_key": "x"}))
    with pytest.raises(ConfigError):
        load_repo_config(tmp_path)


def test_repo_config_applies_known_keys(tmp_path):
    (tmp_path / ".gitworklog").mkdir()
    (tmp_path / ".gitworklog" / "config.json").write_text(
        json.dumps({"base_branch": "develop", "max_tool_calls": 5, "unknown": 1})
    )
    settings = load_settings(tmp_path)
    assert settings.base_branch == "develop"
    assert settings.max_tool_calls == 5


def test_repo_config_invalid_json(tmp_path):
    (tmp_path / ".gitworklog").mkdir()
    (tmp_path / ".gitworklog" / "config.json").write_text("{not json")
    with pytest.raises(ConfigError):
        load_repo_config(tmp_path)


def test_target_repo_env_file_is_not_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("GITWORKLOG_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITWORKLOG_BASE_URL=http://attacker.invalid/v1\n")
    assert load_settings(tmp_path).base_url == "https://integrate.api.nvidia.com/v1"


def test_commit_limits_config(tmp_path):
    (tmp_path / ".gitworklog").mkdir()
    cfg = tmp_path / ".gitworklog" / "config.json"
    cfg.write_text(json.dumps({"commit_subject_max": 50, "commit_body_max_lines": 0}))
    settings = load_settings(tmp_path)
    assert (settings.commit_subject_max, settings.commit_body_max_lines) == (50, 0)
    cfg.write_text(json.dumps({"commit_subject_max": 5}))
    with pytest.raises(ConfigError, match="commit_subject_max"):
        load_settings(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        'ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")',
        "+API_KEY = settings.api_key",
        "SECRET_KEY=get_secret()",
    ],
)
def test_env_pattern_ignores_code(text):
    assert mask_secrets(text) == text


def test_env_pattern_still_masks_env_values():
    assert "hunter2hunter2" not in mask_secrets("DB_PASSWORD=hunter2hunter2  # prod")
    assert "abcdef123456" not in mask_secrets("+export API_TOKEN=abcdef123456")
