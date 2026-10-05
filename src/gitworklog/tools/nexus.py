"""Client for the Nexus (RMS) timesheet API. Standard library only.

Security rules for this module:
- The access token is sent only to the configured base URL, and redirects are never followed
  (urllib would otherwise forward the Authorization header to the redirect target).
- Tokens are never logged or put in error messages; everything shown is passed through
  `mask_secrets`.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from gitworklog.safety import mask_secrets

TOKEN_ENV = "NEXUS_ACCESS_TOKEN"
TOKEN_ENVS = ("NEXUS_TOKEN", TOKEN_ENV)  # first one set wins; NEXUS_TOKEN is the .env name
DEVELOPER_ENV = "NEXUS_DEVELOPER_ID"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LIST_KEYS = ("data", "items", "results", "entries", "projects", "rows", "records")


class NexusError(Exception):
    """The timesheet API could not be used."""


class NexusAuthError(NexusError):
    """The access token is missing, expired or was rejected."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str


@dataclass(frozen=True)
class TimesheetEntry:
    id: str
    date: str  # YYYY-MM-DD
    description: str
    hours: int
    minutes: int
    project_id: str | None = None

    @property
    def total_minutes(self) -> int:
        return self.hours * 60 + self.minutes


# ------------------------------------------------------------------------------ tokens


def jwt_claims(token: str) -> dict[str, Any]:
    """Read the (unverified) payload of a JWT. Only used to read our own claims."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def clean_token(raw: str) -> str:
    """Strip whitespace, quotes and a leading `Bearer `; reject anything that is not a JWT."""
    token = raw.strip().strip("\"'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if len(token.split(".")) != 3 or not all(token.split(".")):
        raise NexusAuthError("That does not look like an access token (expected a JWT).")
    return token


def check_not_expired(token: str, now: float | None = None) -> None:
    exp = jwt_claims(token).get("exp")
    if isinstance(exp, int | float) and exp <= (now if now is not None else time.time()):
        when = datetime.fromtimestamp(exp).astimezone()
        raise NexusAuthError(
            f"The access token expired at {when:%Y-%m-%d %H:%M %Z}. "
            "Copy a fresh one from the browser (or set up a refresh token)."
        )


def resolve_access_token(prompt: bool) -> str:
    """Token from `NEXUS_TOKEN` (e.g. in .env) or `NEXUS_ACCESS_TOKEN`, else a hidden prompt."""
    raw = next((os.environ[k] for k in TOKEN_ENVS if os.environ.get(k, "").strip()), "")
    if not raw.strip() and prompt:
        raw = getpass.getpass("Nexus access token (input is hidden): ")
    if not raw.strip():
        raise NexusAuthError(
            f"No access token. Set {TOKEN_ENVS[0]} in .env (or {TOKEN_ENV} in the environment), "
            "or run in a terminal to be asked for it."
        )
    token = clean_token(raw)
    check_not_expired(token)
    return token


def developer_id_for(token: str) -> str:
    """Developer id from `NEXUS_DEVELOPER_ID`, or the `developer_id` claim of the token."""
    value = os.environ.get(DEVELOPER_ENV) or jwt_claims(token).get("developer_id")
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise NexusError(f"Could not determine your developer id; set {DEVELOPER_ENV}.")
    return value


# ----------------------------------------------------------------------------- parsing


def _items(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find the list of records in a response, whatever envelope the API uses."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and depth < 3:
        for key in _LIST_KEYS:
            if key in payload and isinstance(payload[key], list | dict):
                return _items(payload[key], depth + 1)
    keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
    raise NexusError(f"Unexpected response shape from Nexus (top-level: {keys})")


def _first(item: dict[str, Any], *keys: str) -> Any:
    return next((item[k] for k in keys if item.get(k) not in (None, "")), None)


def _as_int(value: Any) -> int:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return 0


def parse_projects(payload: Any) -> list[Project]:
    projects = []
    for item in _items(payload):
        pid = _first(item, "id", "project_id")
        if pid is not None:
            projects.append(
                Project(str(pid), str(_first(item, "name", "project_name", "title") or pid))
            )
    return projects


def parse_entries(payload: Any) -> list[TimesheetEntry]:
    entries = []
    for item in _items(payload):
        entry_id = _first(item, "id", "entry_id")
        day = _first(item, "entry_date", "date")
        if entry_id is None or day is None:
            continue
        project = item.get("project")
        project_id = _first(item, "project_id") or (
            project.get("id") if isinstance(project, dict) else None
        )
        entries.append(
            TimesheetEntry(
                id=str(entry_id),
                date=str(day)[:10],
                description=str(_first(item, "task_description", "description") or ""),
                hours=_as_int(item.get("hours")),
                minutes=_as_int(item.get("minutes")),
                project_id=str(project_id) if project_id else None,
            )
        )
    return entries


# ------------------------------------------------------------------------------ client


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise NexusError(f"Refusing to follow a redirect ({code}) from the Nexus API")


class NexusClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:  # never show the token
        return f"NexusClient({self.base_url!r})"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "gitworklog/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            detail = mask_secrets(exc.read(500).decode("utf-8", errors="replace").strip())
            if exc.code in (401, 403):
                raise NexusAuthError(
                    f"Nexus rejected the access token (HTTP {exc.code}); it may have expired."
                ) from None
            raise NexusError(f"Nexus returned HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise NexusError(f"Could not reach Nexus: {mask_secrets(str(exc.reason))}") from None
        except TimeoutError:
            raise NexusError("Nexus did not respond in time") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise NexusError("Nexus returned a response that is not JSON") from None

    def projects(self, developer_id: str) -> list[Project]:
        payload = self._request("GET", "/timesheet/projects", {"developer_id": developer_id})
        return parse_projects(payload)

    def entries(
        self, developer_id: str, start: date, end: date, project_id: str | None = None
    ) -> list[TimesheetEntry]:
        payload = self._request(
            "GET",
            "/timesheet/entries",
            {
                "developer_id": developer_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "project_id": project_id,
            },
        )
        return parse_entries(payload) if payload is not None else []

    def create_entry(
        self,
        *,
        project_id: str,
        developer_id: str,
        entry_date: date,
        description: str,
        hours: int,
        minutes: int,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/timesheet/entries",
            body={
                "project_id": project_id,
                "task_description": description,
                "hours": hours,
                "minutes": minutes,
                "entry_date": entry_date.isoformat(),
                "developer_id": developer_id,
            },
        )
        return result if isinstance(result, dict) else {}

    def update_entry(
        self, entry_id: str, *, description: str, hours: int, minutes: int
    ) -> dict[str, Any]:
        if not _ID_RE.match(entry_id):
            raise NexusError("Invalid entry id")
        result = self._request(
            "PUT",
            f"/timesheet/entries/{urllib.parse.quote(entry_id)}",
            body={"task_description": description, "hours": hours, "minutes": minutes},
        )
        return result if isinstance(result, dict) else {}
