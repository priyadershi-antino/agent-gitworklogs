"""Configuration: environment variables for credentials, optional per-repo JSON for behaviour."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"
CONFIG_RELATIVE_PATH = Path(".gitworklog") / "config.json"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
USER_ENV_FILE = Path.home() / ".gitworklog" / ".env"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Limits:
    """Caps that keep tool output and LLM context small."""

    max_tool_output_chars: int = 12_000
    max_diff_chars: int = 60_000
    max_diff_chars_per_file: int = 8_000
    max_file_chars: int = 20_000
    max_log_commits: int = 2_000
    max_search_results: int = 50
    max_history_chars: int = 80_000
    large_file_bytes: int = 5 * 1024 * 1024


@dataclass
class Settings:
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 8192
    request_timeout: float = 120.0
    max_retries: int = 2
    base_branch: str = "main"
    commit_style: str = "conventional"  # only Conventional Commits are supported so far
    commit_subject_max: int = 72  # whole first line, including "type(scope): "
    commit_body_max_lines: int = 6  # 0 = subject line only
    commit_body_line_max: int = 100
    commit_max_words: int = 80  # whole message
    commit_target_words: int = 15  # what generated messages aim for
    max_tool_calls: int = 20
    limits: Limits = field(default_factory=Limits)

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "NVIDIA_API_KEY is not set. Export it or add it to a .env file "
                "(see .env.example). Use --no-llm for deterministic output."
            )
        return self.api_key


_REPO_CONFIG_KEYS = {
    "base_branch",
    "commit_style",
    "commit_subject_max",
    "commit_body_max_lines",
    "commit_body_line_max",
    "commit_max_words",
    "commit_target_words",
    "max_tool_calls",
    "model",
    "temperature",
}
# name -> (minimum, maximum) for integer settings
_INT_RANGES = {
    "max_tool_calls": (1, 100),
    "commit_subject_max": (20, 200),
    "commit_body_max_lines": (0, 50),
    "commit_body_line_max": (40, 500),
    "commit_max_words": (5, 300),
    "commit_target_words": (3, 300),
}
_FORBIDDEN_CONFIG_KEYS = {"api_key", "token", "password", "secret"}


def load_repo_config(repo_root: Path) -> dict[str, Any]:
    """Read `.gitworklog/config.json` if present. Secrets are rejected."""
    path = repo_root / CONFIG_RELATIVE_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Invalid config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object")
    forbidden = {k for k in data if any(word in k.lower() for word in _FORBIDDEN_CONFIG_KEYS)}
    if forbidden:
        raise ConfigError(
            f"Config file {path} must not contain credentials ({', '.join(sorted(forbidden))}); "
            "use environment variables instead."
        )
    return {k: v for k, v in data.items() if k in _REPO_CONFIG_KEYS}


def load_settings(repo_root: Path | None = None) -> Settings:
    """Build settings from environment variables, the tool's own `.env` and repo config.

    The analysed repository's `.env` is deliberately NOT loaded: it belongs to that project,
    and letting it set our key or base URL would let any repo redirect our LLM traffic.
    """
    for env_file in (USER_ENV_FILE, PACKAGE_ROOT / ".env"):
        load_dotenv(env_file, override=False)  # never overrides real environment variables

    settings = Settings(
        api_key=os.environ.get("NVIDIA_API_KEY") or None,
        base_url=os.environ.get("GITWORKLOG_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("GITWORKLOG_MODEL", DEFAULT_MODEL),
    )
    if repo_root is not None:
        known = {f.name for f in fields(Settings)}
        for key, value in load_repo_config(repo_root).items():
            if key in known:
                setattr(settings, key, value)
    for name, (low, high) in _INT_RANGES.items():
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"{name} must be an integer between {low} and {high}")
    if settings.commit_target_words > settings.commit_max_words:
        raise ConfigError("commit_target_words must not exceed commit_max_words")
    return settings
