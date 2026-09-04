from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .acceptance import build_acceptance_report
from .ai_frontier import WorkbenchAIFrontier
from .artifacts import ArtifactStore
from .capabilities import CapabilityRegistry
from .config import WorkbenchConfig
from .governance import code_as_harness_health, governance_status
from .model import DEFAULT_QUOTA_TTL_SECONDS
from .performance import PerformanceRegistry
from .quota_productivity import build_quota_productivity
from .radar import WorkbenchRadar
from .scheduler_metrics import build_scheduler_metrics
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
            store_health = self.server.store.health()
            harness_health = code_as_harness_health(self.server.config)
            capability_registry = self._capability_registry_summary()
            overall_ok = bool(store_health["ok"]) and bool(harness_health["ok"])
            return self._json(
                {
                    "version": __version__,
                    "build": self._build_manifest(),
                    "governance": governance_status(),
                    **store_health,
                    "harness": harness_health,
                    "capability_registry": capability_registry,
                    "radar": self._radar_summary(),
                    "ai_frontier": self._ai_frontier_summary(),
                    "ok": overall_ok,
                },
                HTTPStatus.OK if overall_ok else HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if parsed.path == "/api/snapshot":
            quota = self.server.store.latest_quota()
            return self._json(
                {
                    "version": __version__,
                    "build": self._build_manifest(),
                    "governance": governance_status(),
                    "harness": code_as_harness_health(self.server.config),
                    "capability_registry": self._capability_registry_summary(),
                    "performance": self._performance_registry_summary(),
                    "radar": self._radar_summary(),
                    "ai_frontier": self._ai_frontier_summary(),
                    "scheduler": self._scheduler_metrics(),
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
        if parsed.path == "/api/performance":
            return self._json(self._performance_registry_summary())
        if parsed.path == "/api/radar":
            return self._json(self._radar_summary())
        if parsed.path == "/api/ai-frontier":
            return self._json(self._ai_frontier_summary())
        if parsed.path == "/api/scheduler":
            return self._json(self._scheduler_metrics())
        if parsed.path == "/api/capabilities":
            status = self._capability_registry().status()
            return self._json(
                {
                    "ok": bool(status.get("ok")),
                    "status": status,
                    "active": status.get("active"),
                }
            )
        if parsed.path == "/api/acceptance":
            return self._json(build_acceptance_report(self.server.store))
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except (TypeError, ValueError):
                return self._json(
                    {"error": "after must be an integer"}, HTTPStatus.BAD_REQUEST
                )
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

    def _capability_registry(self) -> CapabilityRegistry:
        """Use the configured local binaries without probing them on GET."""

        return CapabilityRegistry(
            self.server.config.state_root,
            codex_binary=os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
            claude_binary=os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude"),
        )

    def _capability_registry_summary(self) -> dict[str, object]:
        """Return a small, read-only status suitable for health and snapshots."""

        status = self._capability_registry().status()
        active = status.get("active")
        active_summary: dict[str, object] | None = None
        if isinstance(active, dict):
            agents = active.get("agents")
            agent_summary = {
                provider: {
                    "status": agent.get("status"),
                    "cli_version": agent.get("cli_version"),
                }
                for provider, agent in (agents.items() if isinstance(agents, dict) else ())
                if isinstance(agent, dict)
            }
            models = active.get("models")
            model_summary = [
                {
                    "provider": model.get("provider"),
                    "model_id": model.get("model_id"),
                    "model_family": model.get("model_family"),
                    "status": model.get("status"),
                    "routable": model.get("routable") is True
                    and model.get("status") == "available",
                    "roles": list(model.get("roles", ()))
                    if isinstance(model.get("roles"), list)
                    else [],
                    "task_types": list(model.get("task_types", ()))
                    if isinstance(model.get("task_types"), list)
                    else [],
                    "quality": model.get("quality"),
                    "cost": model.get("cost"),
                    "latency": model.get("latency"),
                    "concurrency": model.get("concurrency"),
                    "reasoning": model.get("reasoning"),
                    "features": model.get("features"),
                    "policy_origin": model.get("policy_origin"),
                    "agent_cli_version": model.get("agent_cli_version"),
                }
                for model in (models if isinstance(models, list) else ())
                if isinstance(model, dict)
            ]
            active_summary = {
                "catalog_id": active.get("catalog_id"),
                "observed_at": active.get("observed_at"),
                "agents": agent_summary,
                "models": model_summary,
            }
        return {
            "ok": bool(status.get("ok")),
            "active_generation_id": status.get("active_generation_id"),
            "generation_count": status.get("generation_count", 0),
            "last_refresh": status.get("last_refresh"),
            "error": status.get("error"),
            "active": active_summary,
        }

    def _performance_registry(self) -> PerformanceRegistry:
        """Read the local, materialized performance ledger without probing models."""

        return PerformanceRegistry(self.server.config.state_root)

    def _radar_summary(self) -> dict[str, object]:
        """Expose provider/cache state without refreshing or touching credentials."""

        config = self.server.config
        status = WorkbenchRadar(
            state_root=config.effective_radar_state_root,
            authorization_file=config.effective_radar_authorization_file,
            enabled=config.radar_enabled,
            stale_after_seconds=config.radar_stale_after_seconds,
            expire_after_seconds=config.radar_expire_after_seconds,
        ).status()
        snapshot = status.pop("snapshot", None)
        active = None
        if isinstance(snapshot, dict):
            active = {
                "snapshot_id": snapshot.get("snapshot_id"),
                "digest": snapshot.get("digest"),
                "upstream": snapshot.get("upstream"),
                "source_urls": snapshot.get("source_urls"),
                "fetched_at": snapshot.get("fetched_at"),
                "source_updated_at": snapshot.get("source_updated_at"),
                "models": snapshot.get("models") if isinstance(snapshot.get("models"), list) else [],
                "insights": snapshot.get("insights") if isinstance(snapshot.get("insights"), dict) else {},
                "attribution": snapshot.get("attribution"),
            }
        return {**status, "active": active, "read_only": True, "network_requested": False}

    def _ai_frontier_summary(self) -> dict[str, object]:
        """Expose AI Frontier cache state without refreshing or credentials."""

        config = self.server.config
        frontier = WorkbenchAIFrontier(
            state_root=config.effective_ai_frontier_state_root,
            authorization_file=config.effective_ai_frontier_authorization_file,
            enabled=config.ai_frontier_enabled,
            stale_after_seconds=config.ai_frontier_stale_after_seconds,
            expire_after_seconds=config.ai_frontier_expire_after_seconds,
        )
        status = dict(frontier.status())
        snapshot = status.pop("snapshot", None)
        active = None
        if isinstance(snapshot, dict):
            active = {
                "snapshot_id": snapshot.get("snapshot_id"),
                "digest": snapshot.get("digest"),
                "upstream": snapshot.get("upstream"),
                "source_urls": snapshot.get("source_urls"),
                "source_ids": snapshot.get("source_ids"),
                "fetched_at": snapshot.get("fetched_at"),
                "source_updated_at": snapshot.get("source_updated_at"),
                "models": snapshot.get("models")
                if isinstance(snapshot.get("models"), list)
                else [],
                "categories": snapshot.get("categories")
                if isinstance(snapshot.get("categories"), list)
                else [],
                "benchmarks": snapshot.get("benchmarks")
                if isinstance(snapshot.get("benchmarks"), list)
                else [],
                "routing_boundary": snapshot.get("routing_boundary"),
                "attribution": snapshot.get("attribution"),
            }
        return {**status, "active": active, "read_only": True, "network_requested": False}

    def _performance_registry_summary(self) -> dict[str, object]:
        """Return the active calibrated snapshot, if one has been materialized.

        This endpoint deliberately never refreshes the registry.  Refreshing is
        a separate, explicit CLI/submission action so observing the cockpit
        remains read-only and cannot run a model or mutate the task ledger.
        """

        status = self._performance_registry().status()
        active = status.get("active")
        if not isinstance(active, dict):
            return {
                "ok": bool(status.get("ok")),
                "active_generation_id": status.get("active_generation_id"),
                "generation_count": status.get("generation_count", 0),
                "error": status.get("error"),
                "active": None,
            }
        baseline = active.get("baseline")
        ledger = active.get("ledger")
        return {
            "ok": bool(status.get("ok")),
            "active_generation_id": status.get("active_generation_id"),
            "generation_count": status.get("generation_count", 0),
            "error": status.get("error"),
            "active": {
                "snapshot_id": active.get("snapshot_id"),
                "digest": active.get("digest"),
                "event_cursor": active.get("event_cursor"),
                "catalog": active.get("catalog"),
                "baseline": {
                    "baseline_id": baseline.get("baseline_id"),
                    "digest": baseline.get("digest"),
                    "record_count": len(baseline.get("records", ())),
                }
                if isinstance(baseline, dict)
                else None,
                "ledger": ledger if isinstance(ledger, dict) else None,
                "pools": active.get("pools"),
                "metrics": active.get("metrics") if isinstance(active.get("metrics"), list) else [],
                "advisory_policy": active.get("advisory_policy"),
            },
        }

    def _scheduler_metrics(self) -> dict[str, object]:
        """Replay scheduler evidence into lane metrics without asserting provider quota."""

        return build_scheduler_metrics(
            self.server.store,
            max_workers=self.server.config.max_workers,
            spark_workers=self.server.config.effective_spark_workers,
        )

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
