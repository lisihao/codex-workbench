from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest

from codex_workbench.acceptance import build_acceptance_report
from codex_workbench.artifacts import ArtifactStore
from codex_workbench.cli import command_acceptance
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract
from codex_workbench.store import WorkbenchStore


class AcceptanceTests(unittest.TestCase):
    def test_external_journeys_require_durable_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            contract = TaskContract(
                task_id="offline-work",
                repository=str(root),
                base_sha="fixture",
                objective="finish while the MacBook is offline",
                allowed_scope=("result",),
                required_artifacts=(),
            )
            nodes = [
                NodeSpec("work", contract.task_id, "work", "deterministic", "local", command=("true",)),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "deterministic",
                    "local",
                    command=("true",),
                    depends_on=("work",),
                    verifier=True,
                ),
            ]
            store.create_task(contract, nodes, "offline-create")
            store.queue_task(contract.task_id)
            store.settle_node(
                contract.task_id,
                store.claim_ready_node("worker")["node_id"],
                NodeResult("succeeded", "work complete", actual_model="local"),
            )
            store.settle_node(
                contract.task_id,
                store.claim_ready_node("verifier")["node_id"],
                NodeResult("succeeded", "accepted", actual_model="local"),
            )
            first = store.record_client_heartbeat("macbook-fixture", "macbook")
            second = store.record_client_heartbeat("macbook-fixture", "macbook")
            with store.connection() as connection:
                connection.execute(
                    "UPDATE events SET created_at = ? WHERE cursor = ?",
                    ("2026-08-26T00:00:00+00:00", first),
                )
                connection.execute(
                    "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
                    ("2026-08-26T04:00:00+00:00", contract.task_id),
                )
                connection.execute(
                    "UPDATE events SET created_at = ? WHERE cursor = ?",
                    ("2026-08-26T09:00:00+00:00", second),
                )
            store.record_client_observation(
                "phone-fixture",
                "phone",
                store.health()["cursor"],
                store.health()["cursor"],
                "iPhone fixture",
            )
            ppt_data = b"pptx fixture"
            artifact_ref = ArtifactStore(root / "artifacts").put_bytes(ppt_data, "pptx")
            store.record_acceptance_attestation(
                "A12",
                artifact_ref,
                "slides.pptx",
                len(ppt_data),
                "2026-W35",
                "completed in Claude web using the reserved pool",
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A1"]["status"], "ok")
            self.assertEqual(checks["A2"]["status"], "ok")
            self.assertEqual(checks["A12"]["status"], "ok")

    def test_a12_cli_imports_a_content_addressed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "slides.pptx"
            artifact.write_bytes(b"pptx fixture")
            args = SimpleNamespace(
                home=str(root / "state"),
                acceptance_action="attest-a12",
                artifact=str(artifact),
                quota_window="2026-W35",
                note="completed in Claude web using the reserved pool",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(command_acceptance(args), 0)
            receipt = json.loads(output.getvalue())
            self.assertTrue(receipt["artifact_ref"].startswith("sha256:"))
            store = WorkbenchStore(root / "state" / "state.sqlite")
            self.assertEqual(
                {check["id"]: check for check in build_acceptance_report(store)["checks"]}["A12"]["status"],
                "ok",
            )

    def test_report_is_evidence_based_and_keeps_external_checks_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkbenchStore(Path(directory) / "state.sqlite")
            store.initialize()
            store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": "authority-1",
                    "pid": 123,
                    "host": "mac-mini",
                    "boot_id": "boot-1",
                    "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            for window, remaining in (("five-1", 80), ("five-2", 22)):
                store.write_quota(
                    QuotaSnapshot(
                        observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                        auth_ok=True,
                        auth_method="native-subscription",
                        five_hour_remaining=remaining,
                        weekly_all_remaining=65,
                        weekly_sonnet_remaining=60,
                        source="settings-usage",
                        five_hour_window_id=window,
                        weekly_window_id="week-1",
                    )
                )
            store.write_quota(
                QuotaSnapshot(
                    observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=1,
                    weekly_all_remaining=1,
                    weekly_sonnet_remaining=1,
                    source="acceptance-fixture",
                    five_hour_window_id="fixture-window",
                    weekly_window_id="fixture-week",
                )
            )
            store.write_quota(
                QuotaSnapshot(
                    observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=70,
                    weekly_all_remaining=70,
                    weekly_sonnet_remaining=70,
                    source="latest-settings-usage",
                    five_hour_window_id="five-2",
                    weekly_window_id="week-1",
                )
            )

            report = build_acceptance_report(store)
            checks = {check["id"]: check for check in report["checks"]}

            self.assertFalse(report["complete"])
            self.assertEqual(checks["A1"]["status"], "pending")
            self.assertEqual(checks["A6"]["status"], "ok")
            self.assertEqual(checks["A7"]["status"], "ok")
            self.assertEqual(checks["A11"]["status"], "ok")

    def test_restart_and_quota_fallback_require_durable_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": "authority-1",
                    "pid": 123,
                    "host": "mac-mini",
                    "boot_id": "boot-1",
                    "started_at": "2026-08-25T00:00:00+00:00",
                    "ledger_cursor_before_start": 0,
                    "ledger_task_count": 0,
                },
            )
            contract = TaskContract(
                task_id="fallback",
                repository=str(root),
                base_sha="fixture",
                objective="prove quota fallback",
                allowed_scope=("tests",),
                required_artifacts=(),
                executor_model="gpt-5.6-luna",
            )
            nodes = [
                NodeSpec("work", contract.task_id, "work", "claude", "sonnet", "work"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("work",),
                    verifier=True,
                ),
            ]
            store.create_task(contract, nodes, "fallback-create")
            store.queue_task(contract.task_id)
            store.claim_ready_node("worker")
            store.record_node_event(
                "node.routed",
                contract.task_id,
                "work",
                {
                    "attempt": 1,
                    "from": "claude",
                    "to": "codex",
                    "model": "gpt-5.6-luna",
                    "reason": "Claude quota protection active at 25.0% remaining",
                },
            )
            store.settle_node(
                contract.task_id,
                "work",
                NodeResult("succeeded", "fallback complete", actual_model="gpt-5.6-luna"),
            )
            store.claim_ready_node("verifier")
            store.settle_node(contract.task_id, "verify", NodeResult("succeeded", "accepted"))
            cursor_before_restart = store.health()["cursor"]
            store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": "authority-2",
                    "pid": 456,
                    "host": "mac-mini",
                    "boot_id": "boot-2",
                    "started_at": "2026-08-26T00:00:00+00:00",
                    "ledger_cursor_before_start": cursor_before_restart,
                    "ledger_task_count": 1,
                },
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A3"]["status"], "ok")
            self.assertEqual(checks["A8"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
