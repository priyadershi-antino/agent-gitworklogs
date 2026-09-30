"""Safety layer: secret masking, sensitive-file detection and the human approval gate.

Nothing that mutates the repository may run without passing an `Approver`. The LLM never
answers approval prompts; only a human (or an explicit test double) does.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

REDACTED = "[REDACTED:{kind}]"

# (kind, pattern). Patterns with a group named "value" only mask that group.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
        ),
    ),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("nvidia-api-key", re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}")),
    ("openai-api-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]+:(?P<value>[^@\s/]{3,})(?=@)")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b[\w\-]*(?:api[_\-]?key|secret|token|passw(?:or)?d|pwd|access[_\-]?key|"
            r"private[_\-]?key|client[_\-]?secret|auth[_\-]?key)[\w\-]*[\"']?\s*(?:=|:|=>)\s*"
            r"(?P<q>[\"'])(?P<value>[^\"'\n]{6,})(?P=q)"
        ),
    ),
    (
        "env-secret",
        re.compile(
            r"(?m)^[+\- ]?\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|"
            # .env style `KEY=value` (no spaces) with a literal value; `KEY = os.environ.get(...)`
            # is code. Quoted literals in code are caught by "assigned-secret" above.
            r"CREDENTIALS?)[A-Z0-9_]*=(?P<value>[^\s\"'#()]{6,})(?=\s*(?:#|$))"
        ),
    ),
]

_SENSITIVE_FILE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "service-account*.json",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
]
_SAFE_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".pub")


def mask_secrets(text: str) -> str:
    """Replace anything that looks like a credential with a redaction marker."""
    if not text:
        return text
    for kind, pattern in _SECRET_PATTERNS:
        marker = REDACTED.format(kind=kind)
        if "value" in pattern.groupindex:

            def _sub(match: re.Match[str], marker: str = marker) -> str:
                start, end = match.span("value")
                offset = match.start()
                whole = match.group(0)
                return whole[: start - offset] + marker + whole[end - offset :]

            text = pattern.sub(_sub, text)
        else:
            text = pattern.sub(marker, text)
    return text


@dataclass(frozen=True)
class SecretHit:
    line: int
    kind: str


def find_secrets(text: str) -> list[SecretHit]:
    """Locate likely secrets without returning their values."""
    hits: set[SecretHit] = set()
    for kind, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if "[REDACTED:" in match.group(0):
                continue
            hits.add(SecretHit(line=text.count("\n", 0, match.start()) + 1, kind=kind))
    return sorted(hits, key=lambda h: (h.line, h.kind))


def is_sensitive_path(path: str) -> bool:
    """True for files that commonly hold credentials (.env, private keys, ...)."""
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    if name.endswith(_SAFE_TEMPLATE_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SENSITIVE_FILE_PATTERNS)


def withheld_notice(path: str) -> str:
    return f"Potential secret file: {path}\nThe content has been withheld."


# --------------------------------------------------------------------------- approval gate


class OperationDenied(Exception):
    """The human did not approve the proposed operation."""


@dataclass
class ProposedOperation:
    """Everything a human needs to decide on a mutating operation."""

    kind: str  # stage, commit, push, destructive, edit
    summary: str  # what will happen
    command: list[str]  # exact git argv (without the leading "git"), or [] for file edits
    consequences: list[str] = field(default_factory=list)
    preview: str = ""
    destructive: bool = False
    pre_commands: list[list[str]] = field(default_factory=list)  # run before `command`

    @property
    def command_text(self) -> str:
        return _git_text(self.command) if self.command else "(file edit)"

    @property
    def all_commands(self) -> list[str]:
        """Every command this approval covers, in execution order."""
        return [*(_git_text(c) for c in self.pre_commands), self.command_text]


def _git_text(argv: list[str]) -> str:
    return "git " + " ".join(_quote(a) for a in argv)


def _quote(arg: str) -> str:
    return f'"{arg}"' if (not arg or any(c.isspace() for c in arg)) else arg


class Approver(Protocol):
    def confirm(self, op: ProposedOperation) -> bool: ...


class DenyAllApprover:
    """Default for non-interactive runs: nothing is ever approved."""

    def confirm(self, op: ProposedOperation) -> bool:
        return False


class ScriptedApprover:
    """Test double that answers from a fixed list and records what it was asked."""

    def __init__(self, answers: list[bool]):
        self.answers = list(answers)
        self.asked: list[ProposedOperation] = []

    def confirm(self, op: ProposedOperation) -> bool:
        self.asked.append(op)
        return self.answers.pop(0) if self.answers else False


class PreApproved:
    """Approves only the exact commands a human already approved as one combined step."""

    def __init__(self, commands: list[list[str]]):
        self.commands = [list(c) for c in commands]

    def confirm(self, op: ProposedOperation) -> bool:
        return op.command in self.commands


def require_approval(approver: Approver, op: ProposedOperation) -> None:
    """Raise `OperationDenied` unless the human explicitly approves `op`."""
    if not approver.confirm(op):
        raise OperationDenied(f"Not approved: {op.command_text}")
