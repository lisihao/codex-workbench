from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_workbench.api import WorkbenchHTTPServer
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
                self.assertEqual(snapshot["performance"]["active_generation_id"], refreshed["active_generation_id"])
                self.assertEqual(snapshot["scheduler"]["lanes"]["spark"]["capacity"], 2)
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
