"""A tiny fake Nexus API (real HTTP on localhost) for tests."""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

DEV_ID = "e1aba46a-413a-5a3f-3091-e02f4ccf801a"
PROJECT_ID = "f4464148-1810-4174-ba8f-545b574be7b6"
OTHER_PROJECT_ID = "0a0a0a0a-1111-2222-3333-444444444444"


def make_token(exp_in: int = 900, **claims: Any) -> str:
    """A syntactically valid (unsigned) JWT carrying the given claims."""

    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    now = int(time.time())
    payload = {"id": "u1", "iat": now, "exp": now + exp_in, **claims}
    return f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc(payload)}.c2lnbmF0dXJl"


class FakeNexus:
    def __init__(
        self,
        token: str,
        *,
        shape: str = "list",
        fail_post_for: str | None = None,
        redirect_to: str | None = None,
        error_body: str = "database exploded",
    ):
        self.token = token
        self.shape = shape  # list | data | nested | weird
        self.fail_post_for = fail_post_for  # entry_date that makes POST return HTTP 500
        self.redirect_to = redirect_to
        self.error_body = error_body
        self.projects = [
            {"id": PROJECT_ID, "name": "Rider App"},
            {"id": OTHER_PROJECT_ID, "name": "Internal Tools"},
        ]
        self.entries: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self._next_id = 1

    # -- helpers for tests --------------------------------------------------------------

    def add_entry(
        self, day: str, description: str, hours: int, minutes: int = 0, project_id: str = PROJECT_ID
    ) -> str:
        entry_id = f"entry-{self._next_id}"
        self._next_id += 1
        self.entries.append(
            {
                "id": entry_id,
                "entry_date": f"{day}T00:00:00.000Z",
                "task_description": description,
                "hours": hours,
                "minutes": minutes,
                "project_id": project_id,
                "developer_id": DEV_ID,
            }
        )
        return entry_id

    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] in ("POST", "PUT")]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def _wrap(self, items: list[dict], key: str) -> Any:
        if self.shape == "data":
            return {"data": items}
        if self.shape == "nested":
            return {"success": True, "data": {key: items}}
        if self.shape == "weird":
            return {"hello": "world"}
        return items

    # -- server -------------------------------------------------------------------------

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, payload: Any = None, headers: dict | None = None) -> None:
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                outer.requests.append(
                    {
                        "method": method,
                        "path": parsed.path,
                        "query": query,
                        "body": body,
                        "auth": self.headers.get("Authorization"),
                    }
                )
                if outer.redirect_to:
                    return self._send(302, headers={"Location": outer.redirect_to})
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"message": "unauthorized"})
                route(self, method, parsed.path, query, body)

        def route(h: Handler, method: str, path: str, query: dict, body: Any) -> None:
            if method == "GET" and path.endswith("/timesheet/projects"):
                return h._send(200, outer._wrap(outer.projects, "projects"))
            if method == "GET" and path.endswith("/timesheet/entries"):
                items = [
                    e
                    for e in outer.entries
                    if query.get("start_date", "")
                    <= e["entry_date"][:10]
                    <= query.get("end_date", "9")
                    and query.get("project_id") in (None, e["project_id"])
                ]
                return h._send(200, outer._wrap(items, "entries"))
            if method == "POST" and path.endswith("/timesheet/entries"):
                needed = {
                    "project_id",
                    "task_description",
                    "hours",
                    "minutes",
                    "entry_date",
                    "developer_id",
                }
                if not isinstance(body, dict) or not needed <= set(body):
                    return h._send(400, {"message": "missing fields"})
                if body["entry_date"] == outer.fail_post_for:
                    return h._send(500, {"error": outer.error_body})
                entry_id = outer.add_entry(
                    body["entry_date"],
                    body["task_description"],
                    body["hours"],
                    body["minutes"],
                    body["project_id"],
                )
                return h._send(201, {"data": {"id": entry_id}})
            if method == "PUT" and "/timesheet/entries/" in path:
                entry_id = path.rsplit("/", 1)[1]
                for entry in outer.entries:
                    if entry["id"] == entry_id:
                        entry.update(
                            task_description=body["task_description"],
                            hours=body["hours"],
                            minutes=body["minutes"],
                        )
                        return h._send(200, {"id": entry_id})
                return h._send(404, {"message": "not found"})
            return h._send(404, {"message": "no such route"})

        for verb in ("GET", "POST", "PUT"):
            setattr(Handler, f"do_{verb}", lambda self, v=verb: self._handle(v))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
