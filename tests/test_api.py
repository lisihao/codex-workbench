from __future__ import annotations

import hmac
from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from codex_workbench.api import (
    ANONYMOUS_MAX_COLLECTION_ITEMS,
    ANONYMOUS_MAX_STRING_LENGTH,
    LOGIN_FAILURE_LIMIT,
    LOGIN_FAILURE_WINDOW_SECONDS,
    WorkbenchHTTPServer,
)
from codex_workbench.artifacts import ArtifactStore
from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.performance import PerformanceRegistry
from codex_workbench.store import WorkbenchStore


class APITests(unittest.TestCase):
    def test_ai_frontier_endpoint_and_summaries_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            fake_frontier = mock.Mock()
            fake_frontier.status.return_value = {
                "schema_version": 1,
                "provider": "ai-frontier-provider",
                "ok": True,
                "state": "fresh",
                "routing_prior_eligible": True,
                "network_requested": False,
                "snapshot_id": "frontier-api-snapshot",
                "digest": "f" * 64,
                "snapshot": {
                    "snapshot_id": "frontier-api-snapshot",
                    "digest": "f" * 64,
                    "source_ids": ["openai/gpt-5.6-luna"],
                    "models": [{"model": "gpt-5.6-luna", "quality": 0.9}],
                },
            }
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with (
                    mock.patch("codex_workbench.api.WorkbenchAIFrontier", return_value=fake_frontier),
                    mock.patch("codex_workbench.api.code_as_harness_health", return_value={"ok": True}),
                ):
                    with urlopen(
                        f"http://127.0.0.1:{port}/api/ai-frontier", timeout=2
                    ) as response:
                        frontier = json.load(response)
                    with urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2
                    ) as response:
                        health = json.load(response)
                    with urlopen(
                        f"http://127.0.0.1:{port}/api/snapshot", timeout=2
                    ) as response:
                        snapshot = json.load(response)

                self.assertTrue(frontier["ok"])
                self.assertTrue(frontier["read_only"])
                self.assertFalse(frontier["network_requested"])
                self.assertEqual(frontier["active"]["snapshot_id"], "frontier-api-snapshot")
                self.assertIn("ai_frontier", health)
                self.assertIn("ai_frontier", snapshot)
                self.assertEqual(
                    snapshot["ai_frontier"]["active"]["source_ids"],
                    ["openai/gpt-5.6-luna"],
                )
                fake_frontier.refresh.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_performance_and_scheduler_endpoints_are_read_only_and_expose_spark_na_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(
                root,
                host="127.0.0.1",
                port=0,
                max_workers=3,
                spark_workers=2,
            )
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            refreshed = PerformanceRegistry(config.state_root).refresh(
                store,
                {
                    "catalog_id": "catalog-api-performance",
                    "digest": "a" * 64,
                    "models": [],
                    "agents": {},
                },
            )
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/performance", timeout=2) as response:
                    performance = json.load(response)
                with urlopen(f"http://127.0.0.1:{port}/api/scheduler", timeout=2) as response:
                    scheduler = json.load(response)
                with urlopen(f"http://127.0.0.1:{port}/api/radar", timeout=2) as response:
                    radar = json.load(response)
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)

                self.assertTrue(performance["ok"])
                self.assertEqual(
                    performance["active"]["snapshot_id"],
                    refreshed["active_generation_id"],
                )
                self.assertEqual(performance["active"]["pools"]["spark"]["remaining_display"], "N/A")
                self.assertEqual(scheduler["lanes"]["spark"]["capacity"], 2)
                self.assertEqual(scheduler["quota_pools"]["codex-spark"]["status"], "N/A")
                self.assertEqual(radar["state"], "unauthorized")
                self.assertTrue(radar["read_only"])
                self.assertFalse(radar["network_requested"])
                self.assertIsNone(radar["active"])
                self.assertEqual(snapshot["performance"]["active_generation_id"], refreshed["active_generation_id"])
                self.assertEqual(snapshot["scheduler"]["lanes"]["spark"]["capacity"], 2)
                self.assertEqual(snapshot["radar"]["state"], "unauthorized")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_events_rejects_invalid_after_as_json_400(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(
                        f"http://127.0.0.1:{server.server_address[1]}/api/events?after=not-an-int",
                        timeout=2,
                    )
                self.assertEqual(caught.exception.code, HTTPStatus.BAD_REQUEST)
                self.assertEqual(json.load(caught.exception), {"error": "after must be an integer"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_health_is_unavailable_when_harness_or_archify_health_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch(
                    "codex_workbench.api.code_as_harness_health",
                    return_value={"ok": False, "archify": {"ok": False}},
                ):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(f"http://127.0.0.1:{server.server_address[1]}/health", timeout=2)
                payload = json.load(caught.exception)
                self.assertFalse(payload["ok"])
                self.assertFalse(payload["harness"]["ok"])
                self.assertFalse(payload["harness"]["archify"]["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_snapshot_is_readable_and_control_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            epoch = store.activate_coordinator("api-test", "test-machine")
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)
                self.assertTrue(snapshot["health"]["ok"])
                self.assertEqual(snapshot["governance"]["profile"], "code-as-harness/v1")
                self.assertIn("harness", snapshot)
                self.assertIn("archify", snapshot["harness"])
                self.assertFalse(snapshot["harness"]["archify"]["authentication_checked"])
                self.assertFalse(snapshot["harness"]["archify"]["model_called"])
                self.assertTrue(snapshot["governance"]["enforced"])
                self.assertEqual(snapshot["governance"]["execution_location"], "authority")
                self.assertFalse(snapshot["authenticated"])
                self.assertIsNone(snapshot["build"])
                self.assertIsNone(snapshot["quota_policy"])
                self.assertEqual(len(snapshot["acceptance"]["checks"]), 12)
                self.assertEqual(snapshot["acceptance"]["backlog"], [])
                with mock.patch(
                    "codex_workbench.api.code_as_harness_health",
                    return_value={"ok": True, "archify": {"ok": True}},
                ):
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        health_endpoint = json.load(response)
                self.assertIn("harness", health_endpoint)
                self.assertIn("archify", health_endpoint["harness"])
                with urlopen(f"http://127.0.0.1:{port}/api/acceptance", timeout=2) as response:
                    acceptance = json.load(response)
                self.assertFalse(acceptance["complete"])
                request = Request(
                    f"http://127.0.0.1:{port}/api/tasks/missing/control",
                    data=b'{"action":"pause"}',
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()

                store.write_quota(
                    QuotaSnapshot(
                        observed_at=now_iso(),
                        auth_ok=True,
                        auth_method="native-subscription",
                        five_hour_remaining=35,
                        weekly_all_remaining=60,
                        weekly_sonnet_remaining=60,
                        source=COMPATIBLE_SOURCE,
                        producer=PRODUCER,
                        producer_schema_version=PRODUCER_SCHEMA_VERSION,
                        claude_version=SUPPORTED_USAGE_VERSION,
                    )
                )
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)
                self.assertEqual(snapshot["quota_policy"]["zone"], "yellow")
                self.assertEqual(snapshot["quota_policy"]["zones"]["sonnet"], "yellow")
                self.assertEqual(snapshot["quota_policy"]["models"]["sonnet"]["max_concurrency"], 1)

                observation = Request(
                    f"http://127.0.0.1:{port}/api/clients/observe",
                    data=json.dumps(
                        {"client_id": "phone-fixture", "snapshot_cursor": snapshot["health"]["cursor"]}
                    ).encode(),
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {config.token()}",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (iPhone; Mobile)",
                    },
                )
                with urlopen(observation, timeout=2) as response:
                    observation_receipt = json.load(response)
                self.assertEqual(observation_receipt["device_class"], "phone")
                with urlopen(f"http://127.0.0.1:{port}/api/acceptance", timeout=2) as response:
                    acceptance = json.load(response)
                checks = {check["id"]: check for check in acceptance["checks"]}
                backlog = {check["id"]: check for check in acceptance["backlog"]}
                self.assertEqual(checks["A2"]["status"], "ok")
                self.assertNotIn("A2", backlog)

                contract = TaskContract(
                    task_id="phone-approval",
                    repository=str(root),
                    base_sha="fixture",
                    objective="approve an indeterminate node from the phone cockpit",
                    allowed_scope=("tests",),
                )
                store.create_task(
                    contract,
                    [
                        NodeSpec("work", contract.task_id, "work", "fixture", "fixture", "ok"),
                        NodeSpec("verify", contract.task_id, "verify", "fixture", "fixture", "accepted", depends_on=("work",), verifier=True),
                    ],
                    "phone-approval-create",
                )
                task = store.get_task(contract.task_id)
                priority = Request(
                    f"http://127.0.0.1:{port}/api/tasks/{contract.task_id}/control",
                    data=json.dumps(
                        {
                            "action": "set_priority",
                            "priority": 5,
                            "expected_revision": task["state_revision"],
                        }
                    ).encode(),
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {config.token()}",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(priority, timeout=2) as response:
                    priority_receipt = json.load(response)
                steering = Request(
                    f"http://127.0.0.1:{port}/api/tasks/{contract.task_id}/steer",
                    data=json.dumps(
                        {
                            "instruction": "保留公开接口",
                            "expected_revision": priority_receipt["revision"],
                        }
                    ).encode(),
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {config.token()}",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(steering, timeout=2) as response:
                    steering_receipt = json.load(response)
                self.assertTrue(steering_receipt["ok"])
                self.assertEqual(store.get_task(contract.task_id)["priority"], 5)
                store.queue_task(contract.task_id)
                claimed = store.claim_ready_node("worker", epoch)
                store.settle_claimed(
                    claimed,
                    NodeResult("indeterminate", "fixture outcome unknown"),
                )
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)
                approval = snapshot["approvals"][0]
                self.assertIn(
                    "approval.requested",
                    {alert["event_type"] for alert in snapshot["alerts"]},
                )
                decision = Request(
                    f"http://127.0.0.1:{port}/api/approvals/{approval['approval_id']}/decide",
                    data=json.dumps(
                        {
                            "decision": "retry",
                            "expected_revision": approval["task_revision"],
                        }
                    ).encode(),
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {config.token()}",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(decision, timeout=2) as response:
                    receipt = json.load(response)
                self.assertTrue(receipt["ok"])
                self.assertEqual(store.get_task(contract.task_id)["state"], "queued")

                artifact_ref = ArtifactStore(root / "artifacts").put_text(
                    "phone-visible evidence",
                    "txt",
                )
                artifact_request = Request(
                    f"http://127.0.0.1:{port}/api/artifacts/{artifact_ref}",
                    headers={"Authorization": f"Bearer {config.token()}"},
                )
                with urlopen(artifact_request, timeout=2) as response:
                    artifact_body = response.read().decode()
                self.assertEqual(artifact_body, "phone-visible evidence")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_task_control_cas_and_receipts_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()

            def create_task(task_id: str, command_id: str) -> None:
                contract = TaskContract(
                    task_id=task_id,
                    repository=str(root),
                    base_sha="fixture",
                    objective="exercise task control",
                    allowed_scope=("tests",),
                )
                store.create_task(
                    contract,
                    [
                        NodeSpec(task_id + "-work", task_id, "work", "fixture", "fixture", "ok"),
                        NodeSpec(
                            task_id + "-verify",
                            task_id,
                            "verify",
                            "fixture",
                            "fixture",
                            "accepted",
                            depends_on=(task_id + "-work",),
                            verifier=True,
                        ),
                    ],
                    command_id,
                )

            create_task("control-cas", "control-cas-create")
            create_task("steer-receipt", "steer-receipt-create")
            create_task("atomic-queue", "atomic-queue-create")
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            headers = {
                "Authorization": f"Bearer {config.token()}",
                "Content-Type": "application/json",
            }

            def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                request = Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(payload).encode(),
                    method="POST",
                    headers=headers,
                )
                with urlopen(request, timeout=2) as response:
                    return json.load(response)

            def expect_conflict(path: str, payload: dict[str, object]) -> dict[str, object]:
                request = Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(payload).encode(),
                    method="POST",
                    headers=headers,
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, HTTPStatus.CONFLICT)
                result = json.load(caught.exception)
                caught.exception.close()
                return result

            try:
                stale = expect_conflict(
                    "/api/tasks/control-cas/control",
                    {"action": "queue", "expected_revision": 0},
                )
                self.assertIn("expected task revision", stale["error"])
                self.assertEqual(store.get_task("control-cas")["state_revision"], 1)

                queued = post(
                    "/api/tasks/control-cas/control",
                    {"action": "queue", "expected_revision": 1},
                )
                self.assertEqual(queued["revision"], 2)
                paused = post(
                    "/api/tasks/control-cas/control",
                    {"action": "pause", "expected_revision": 2},
                )
                self.assertEqual(paused["revision"], 3)
                expect_conflict(
                    "/api/tasks/control-cas/control",
                    {"action": "resume", "expected_revision": 2},
                )
                resumed = post(
                    "/api/tasks/control-cas/control",
                    {"action": "resume", "expected_revision": 3},
                )
                self.assertEqual(resumed["revision"], 4)
                expect_conflict(
                    "/api/tasks/control-cas/control",
                    {"action": "cancel", "expected_revision": 3},
                )
                cancelled = post(
                    "/api/tasks/control-cas/control",
                    {"action": "cancel", "expected_revision": 4},
                )
                self.assertEqual(cancelled["revision"], 5)

                queued_steer = post(
                    "/api/tasks/steer-receipt/control",
                    {"action": "queue", "expected_revision": 1},
                )
                steering = post(
                    "/api/tasks/steer-receipt/steer",
                    {
                        "instruction": "保留公开接口",
                        "expected_revision": queued_steer["revision"],
                    },
                )
                self.assertTrue(steering["steering_id"])
                self.assertEqual(steering["revision"], 3)
                self.assertIn("delivery", steering)
                self.assertEqual(store.get_task("steer-receipt")["state"], "queued")

                for instruction in (None, True, "x" * 501):
                    expect_conflict(
                        "/api/tasks/steer-receipt/steer",
                        {"instruction": instruction, "expected_revision": 3},
                    )
                    unchanged = store.get_task("steer-receipt")
                    self.assertEqual(unchanged["state_revision"], 3)
                    self.assertEqual(len(unchanged["steering"]), 1)

                invalid = "/api/tasks/atomic-queue/control"
                for instruction in ("x" * 501, "   "):
                    expect_conflict(
                        invalid,
                        {
                            "action": "queue",
                            "expected_revision": 1,
                            "instruction": instruction,
                        },
                    )
                    unchanged = store.get_task("atomic-queue")
                    self.assertEqual(unchanged["state"], "inbox")
                    self.assertEqual(unchanged["state_revision"], 1)
                    self.assertEqual(unchanged["steering"], [])

                started = post(
                    invalid,
                    {
                        "action": "queue",
                        "expected_revision": 1,
                        "instruction": "保留公开接口并补测试",
                    },
                )
                self.assertEqual(started["state"], "queued")
                self.assertEqual(started["revision"], 3)
                self.assertTrue(started["steering"]["steering_id"])
                self.assertIn("delivery", started["steering"])
                self.assertEqual(
                    store.get_task("atomic-queue")["steering"][0]["instruction"],
                    "保留公开接口并补测试",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_task_control_queue_rejects_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            contract = TaskContract(
                task_id="dry-run-reject",
                repository=str(root),
                base_sha="fixture",
                objective="exercise dry_run regression",
                allowed_scope=("tests",),
            )
            store.create_task(
                contract,
                [
                    NodeSpec(
                        "work",
                        "dry-run-reject",
                        "work",
                        "fixture",
                        "fixture",
                        "ok",
                    ),
                    NodeSpec(
                        "verify",
                        "dry-run-reject",
                        "verify",
                        "fixture",
                        "fixture",
                        "accepted",
                        depends_on=("work",),
                        verifier=True,
                    ),
                ],
                "dry-run-reject-create",
            )
            task_before = store.get_task("dry-run-reject")
            events_before = store.read_events(task_id="dry-run-reject")
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            headers = {
                "Authorization": f"Bearer {config.token()}",
                "Content-Type": "application/json",
            }

            def assert_conflict(payload: dict[str, object]) -> None:
                request = Request(
                    f"http://127.0.0.1:{port}/api/tasks/dry-run-reject/control",
                    data=json.dumps(payload).encode(),
                    method="POST",
                    headers=headers,
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, HTTPStatus.CONFLICT)
                caught.exception.close()

            try:
                for payload in (
                    {
                        "action": "queue",
                        "expected_revision": task_before["state_revision"],
                        "dry_run": True,
                    },
                    {
                        "action": "queue",
                        "expected_revision": task_before["state_revision"],
                        "dry_run": "true",
                    },
                ):
                    with self.subTest(payload=payload["dry_run"]):
                        assert_conflict(payload)
                        task_after = store.get_task("dry-run-reject")
                        events_after = store.read_events(task_id="dry-run-reject")
                        self.assertEqual(task_before["state"], task_after["state"])
                        self.assertEqual(
                            task_before["state_revision"],
                            task_after["state_revision"],
                        )
                        self.assertEqual(task_before["steering"], task_after["steering"])
                        self.assertEqual(events_before, events_after)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_host_allowlist_and_security_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]

            def login_request(host: str) -> Request:
                return Request(
                    f"http://127.0.0.1:{port}/login",
                    headers={"Host": host},
                )

            try:
                for host in (
                    f"localhost:{port}",
                    f"control.example.ts.net:{port}",
                    f"100.64.0.9:{port}",
                ):
                    with urlopen(login_request(host), timeout=2) as response:
                        self.assertEqual(response.status, HTTPStatus.OK)
                        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                        self.assertIn(
                            "frame-ancestors 'none'",
                            response.headers["Content-Security-Policy"],
                        )

                with self.assertRaises(HTTPError) as caught:
                    urlopen(login_request(f"untrusted.example:{port}"), timeout=2)
                self.assertEqual(caught.exception.code, HTTPStatus.BAD_REQUEST)
                self.assertEqual(caught.exception.headers["X-Frame-Options"], "DENY")
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_anonymous_task_projection_is_bounded_and_authenticated_detail_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            private_task = {
                "task_id": "private-task",
                "objective": "private objective",
                "display": "s" * (ANONYMOUS_MAX_STRING_LENGTH + 1),
                "blocker": "private blocker",
                "notes": list(range(ANONYMOUS_MAX_COLLECTION_ITEMS + 1)),
                "nodes": [
                    {
                        "node_id": "private-node",
                        "prompt": "private node prompt",
                        "system_prompt": "private system prompt",
                        "command": ["/bin/sh", "-c", "private command"],
                        "worktree": "/private/absolute/worktree",
                        "state": "queued",
                        "result": {
                            "status": "accepted",
                            "summary": "private process output",
                            "checks": ["private check output"],
                        },
                    }
                ],
            }
            url = f"http://127.0.0.1:{port}/api/tasks/private-task"
            try:
                with mock.patch.object(store, "get_task", return_value=private_task):
                    with urlopen(url, timeout=2) as response:
                        anonymous = json.load(response)
                    self.assertNotIn("objective", anonymous)
                    self.assertNotIn("blocker", anonymous)
                    self.assertNotIn("prompt", anonymous["nodes"][0])
                    self.assertNotIn("system_prompt", anonymous["nodes"][0])
                    self.assertNotIn("command", anonymous["nodes"][0])
                    self.assertNotIn("worktree", anonymous["nodes"][0])
                    self.assertNotIn("result", anonymous["nodes"][0])
                    self.assertEqual(
                        len(anonymous["notes"]), ANONYMOUS_MAX_COLLECTION_ITEMS
                    )
                    self.assertLessEqual(
                        len(anonymous["display"]), ANONYMOUS_MAX_STRING_LENGTH
                    )

                    request = Request(
                        url,
                        headers={"Authorization": f"Bearer {config.token()}"},
                    )
                    original_compare_digest = hmac.compare_digest
                    with mock.patch(
                        "codex_workbench.api.hmac.compare_digest",
                        wraps=original_compare_digest,
                    ) as compare_digest:
                        with urlopen(request, timeout=2) as response:
                            authenticated = json.load(response)
                    self.assertTrue(compare_digest.called)
                    self.assertEqual(authenticated, private_task)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_login_throttling_limits_and_recovers_at_window_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            clock = [1000.0]

            def failed_login() -> HTTPError:
                request = Request(
                    f"http://127.0.0.1:{port}/login",
                    data=urlencode({"token": "incorrect"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                return caught.exception

            try:
                with mock.patch(
                    "codex_workbench.api.time.monotonic",
                    side_effect=lambda: clock[0],
                ):
                    for _ in range(LOGIN_FAILURE_LIMIT):
                        failure = failed_login()
                        self.assertEqual(failure.code, HTTPStatus.UNAUTHORIZED)
                        failure.close()

                    throttled = failed_login()
                    self.assertEqual(throttled.code, HTTPStatus.TOO_MANY_REQUESTS)
                    self.assertEqual(
                        throttled.headers["Retry-After"],
                        str(int(LOGIN_FAILURE_WINDOW_SECONDS)),
                    )
                    throttled.close()

                    clock[0] += LOGIN_FAILURE_WINDOW_SECONDS - 0.001
                    before_boundary = failed_login()
                    self.assertEqual(before_boundary.code, HTTPStatus.TOO_MANY_REQUESTS)
                    before_boundary.close()

                    clock[0] += 0.001
                    at_boundary = failed_login()
                    self.assertEqual(at_boundary.code, HTTPStatus.UNAUTHORIZED)
                    at_boundary.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_capability_endpoints_are_read_only_and_expose_active_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            empty_status = {
                "ok": False,
                "active_generation_id": None,
                "active": None,
                "generation_count": 0,
                "generations": [],
                "last_refresh": None,
                "error": None,
            }
            active = {
                "catalog_id": "catalog-0123456789abcdef",
                "observed_at": "2026-09-02T00:00:00+00:00",
                "agents": {
                    "codex": {"status": "available", "cli_version": "0.149.1"},
                    "claude": {"status": "available", "cli_version": "2.1.239"},
                },
                "models": [
                    {
                        "provider": "codex",
                        "model_id": "gpt-5.6-luna",
                        "model_family": "luna",
                        "status": "available",
                        "routable": True,
                        "roles": ["worker"],
                        "agent_cli_version": "0.149.1",
                    },
                    {
                        "provider": "codex",
                        "model_id": "gpt-9.9-unknown",
                        "model_family": "unknown",
                        "status": "observed",
                        "routable": False,
                        "roles": [],
                        "agent_cli_version": "0.149.1",
                    },
                ],
            }
            active_status = {**empty_status, "ok": True, "active_generation_id": active["catalog_id"], "active": active, "generation_count": 1, "generations": [active["catalog_id"]]}
            try:
                with mock.patch("codex_workbench.api.CapabilityRegistry") as registry_class:
                    registry = registry_class.return_value
                    registry.status.return_value = empty_status
                    with urlopen(f"http://127.0.0.1:{port}/api/capabilities", timeout=2) as response:
                        empty_payload = json.load(response)
                    self.assertFalse(empty_payload["ok"])
                    self.assertIsNone(empty_payload["active"])
                    self.assertFalse(empty_payload["status"]["ok"])
                    with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                        empty_snapshot = json.load(response)
                    self.assertFalse(empty_snapshot["capability_registry"]["ok"])
                    registry.refresh.assert_not_called()

                    registry.status.return_value = active_status
                    with urlopen(f"http://127.0.0.1:{port}/api/capabilities", timeout=2) as response:
                        active_payload = json.load(response)
                    self.assertTrue(active_payload["ok"])
                    self.assertEqual(active_payload["active"]["catalog_id"], active["catalog_id"])
                    # This test owns capability serialization, not host-global
                    # harness installation.  Keep CI and developer machines on
                    # the same explicit healthy-harness input.
                    with mock.patch(
                        "codex_workbench.api.code_as_harness_health",
                        return_value={"ok": True, "archify": {"ok": True}},
                    ):
                        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                            health = json.load(response)
                    self.assertEqual(health["capability_registry"]["active_generation_id"], active["catalog_id"])
                    self.assertEqual(
                        health["capability_registry"]["active"]["models"][0]["model_id"],
                        "gpt-5.6-luna",
                    )
                    self.assertFalse(health["capability_registry"]["active"]["models"][1]["routable"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
