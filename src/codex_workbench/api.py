from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .acceptance import build_acceptance_report
from .artifacts import ArtifactStore
from .config import WorkbenchConfig
from .governance import governance_status
from .model import DEFAULT_QUOTA_TTL_SECONDS
from .quota_productivity import build_quota_productivity
from .store import StateConflictError, WorkbenchStore


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: WorkbenchConfig, store: WorkbenchStore):
        super().__init__((config.host, config.port), WorkbenchHandler)
        self.config = config
        self.store = store
        self.artifacts = ArtifactStore(config.state_root / "artifacts")


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._html(self._static("index.html"))
        if parsed.path == "/app.css":
            return self._text(self._static("app.css"), "text/css; charset=utf-8")
        if parsed.path == "/app.js":
            return self._text(self._static("app.js"), "application/javascript; charset=utf-8")
        if parsed.path == "/login":
            return self._html(self._login_page())
        if parsed.path == "/health":
            health = self.server.store.health()
            return self._json(
                {
                    "version": __version__,
                    "build": self._build_manifest(),
                    "governance": governance_status(),
                    **health,
                },
                HTTPStatus.OK if health["ok"] else HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if parsed.path == "/api/snapshot":
            quota = self.server.store.latest_quota()
            return self._json(
                {
                    "version": __version__,
                    "build": self._build_manifest(),
                    "governance": governance_status(),
                    "health": self.server.store.health(),
                    "tasks": self.server.store.list_tasks(),
                    "approvals": self.server.store.list_approvals(),
                    "alerts": self.server.store.list_alerts(),
                    "quota": quota.__dict__ if quota else None,
                    "quota_policy": quota.policy_summary(
                        max_age_seconds=DEFAULT_QUOTA_TTL_SECONDS
                    ) if quota else None,
                    "quota_productivity": build_quota_productivity(self.server.store),
                    "acceptance": build_acceptance_report(self.server.store),
                    "diagnostics": {"stale_tasks": self.server.store.stale_tasks()},
                    "authenticated": self._authenticated(),
                }
            )
        if parsed.path == "/api/acceptance":
            return self._json(build_acceptance_report(self.server.store))
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            task_id = query.get("task_id", [None])[0]
            return self._json(
                {"events": self.server.store.read_events(after=after, task_id=task_id)}
            )
        if parsed.path == "/api/tasks":
            return self._json({"tasks": self.server.store.list_tasks()})
        if parsed.path.startswith("/api/artifacts/"):
            if not self._authenticated():
                return self._json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
            artifact_ref = unquote(parsed.path.removeprefix("/api/artifacts/"))
            try:
                artifact = self.server.artifacts.path_for(artifact_ref)
                if not artifact.is_file():
                    raise FileNotFoundError(artifact)
                content_type = (
                    "text/plain; charset=utf-8"
                    if artifact.suffix in {".txt", ".log", ".json", ".patch"}
                    else "application/octet-stream"
                )
                return self._bytes(artifact.read_bytes(), content_type)
            except (FileNotFoundError, ValueError):
                return self._json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
        if parsed.path.startswith("/api/tasks/"):
            task_id = unquote(parsed.path.removeprefix("/api/tasks/"))
            try:
                return self._json(self.server.store.get_task(task_id))
            except KeyError:
                return self._json({"error": "task not found"}, HTTPStatus.NOT_FOUND)
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            body = parse_qs(self._read_body().decode())
            token = body.get("token", [""])[0]
            if token != self.server.config.token():
                return self._html(self._login_page("控制令牌无效"), HTTPStatus.UNAUTHORIZED)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"workbench_token={quote(token)}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            return
        if not self._authenticated():
            return self._json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
        if parsed.path == "/api/clients/observe":
            try:
                body = json.loads(self._read_body() or b"{}")
                user_agent = self.headers.get("User-Agent", "")
                lowered = user_agent.lower()
                device_class = (
                    "phone"
                    if any(marker in lowered for marker in ("iphone", "android", "mobile"))
                    else "desktop"
                )
                current_cursor = int(self.server.store.health()["cursor"])
                cursor = self.server.store.record_client_observation(
                    str(body.get("client_id", "")),
                    device_class,
                    int(body.get("snapshot_cursor", -1)),
                    current_cursor,
                    user_agent,
                )
                return self._json(
                    {"ok": True, "event_cursor": cursor, "device_class": device_class}
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if parsed.path.startswith("/api/approvals/") and parsed.path.endswith("/decide"):
            approval_id = unquote(
                parsed.path.removeprefix("/api/approvals/").removesuffix("/decide")
            )
            try:
                body = json.loads(self._read_body() or b"{}")
                revision = self.server.store.decide_approval(
                    approval_id,
                    str(body["decision"]),
                    expected_revision=int(body["expected_revision"]),
                )
                return self._json({"ok": True, "revision": revision})
            except KeyError:
                return self._json({"error": "approval not found"}, HTTPStatus.NOT_FOUND)
            except (StateConflictError, TypeError, ValueError, json.JSONDecodeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/steer"):
            task_id = unquote(parsed.path.removeprefix("/api/tasks/").removesuffix("/steer"))
            try:
                body = json.loads(self._read_body() or b"{}")
                revision = self.server.store.append_task_steering(
                    task_id,
                    str(body["instruction"]),
                    expected_revision=int(body["expected_revision"]),
                )
                return self._json({"ok": True, "revision": revision})
            except KeyError:
                return self._json({"error": "task not found"}, HTTPStatus.NOT_FOUND)
            except (StateConflictError, TypeError, ValueError, json.JSONDecodeError) as error:
                return self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/control"):
            task_id = unquote(parsed.path.removeprefix("/api/tasks/").removesuffix("/control"))
            try:
                body = json.loads(self._read_body() or b"{}")
                action = body.get("action")
                task = self.server.store.get_task(task_id)
                if action in {"queue", "resume"}:
                    revision = self.server.store.queue_task(task_id)
                elif action == "pause":
                    revision = self.server.store.transition_task(
                        task_id, "paused", expected_revision=task["state_revision"]
                    )
                elif action == "cancel":
                    revision = self.server.store.transition_task(
                        task_id, "cancelled", expected_revision=task["state_revision"]
                    )
                elif action == "set_priority":
                    revision = self.server.store.set_task_priority(
                        task_id,
                        int(body["priority"]),
                        expected_revision=int(body["expected_revision"]),
                    )
                elif action == "resolve_indeterminate":
                    revision = self.server.store.resolve_indeterminate(
                        task_id,
                        body["node_id"],
                        body["resolution"],
                        expected_revision=int(body["expected_revision"]),
                    )
                else:
                    return self._json({"error": "unsupported action"}, HTTPStatus.BAD_REQUEST)
                return self._json({"ok": True, "revision": revision})
            except KeyError:
                return self._json({"error": "task not found"}, HTTPStatus.NOT_FOUND)
            except (StateConflictError, ValueError) as error:
                return self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _authenticated(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        if authorization == f"Bearer {self.server.config.token()}":
            return True
        cookie = SimpleCookie(self.headers.get("Cookie"))
        value = cookie.get("workbench_token")
        return value is not None and unquote(value.value) == self.server.config.token()

    def _static(self, name: str) -> str:
        return (Path(__file__).parent / "static" / name).read_text()

    def _build_manifest(self) -> dict | None:
        path = self.server.config.install_manifest
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"error": "install manifest unreadable"}

    def _login_page(self, error: str = "") -> str:
        notice = f"<p class='error'>{error}</p>" if error else ""
        return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Codex Workbench 登录</title><link rel='stylesheet' href='/app.css'></head>
        <body><main class='login'><h1>Codex Workbench</h1>{notice}
        <p>输入 Mac mini 上的本地控制令牌。只读状态无需登录。</p>
        <form method='post'><input type='password' name='token' autocomplete='current-password' required>
        <button type='submit'>登录控制面</button></form></main></body></html>"""

    def _json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, value: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._text(value, "text/html; charset=utf-8", status)

    def _bytes(
        self,
        data: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(
        self, value: str, content_type: str, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        data = value.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(config: WorkbenchConfig, store: WorkbenchStore) -> WorkbenchHTTPServer:
    server = WorkbenchHTTPServer(config, store)
    server.serve_forever(poll_interval=0.5)
    return server
