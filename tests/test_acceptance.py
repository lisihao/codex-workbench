from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from codex_workbench.acceptance import build_acceptance_report
from codex_workbench.artifacts import ArtifactStore
from codex_workbench.cli import command_acceptance
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract
from codex_workbench.store import WorkbenchStore


class AcceptanceTests(unittest.TestCase):
    def test_external_journeys_require_durable_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("acceptance-offline", "test-machine")
            contract = TaskContract(
                task_id="offline-work",
                repository=str(root),
                base_sha="fixture",
                objective="finish while the MacBook is offline",
                allowed_scope=("result",),
                required_artifacts=(),
                verifier_model="fixture",
            )
            nodes = [
                NodeSpec("work", contract.task_id, "work", "deterministic", "fixture", command=("true",)),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "deterministic",
                    "fixture",
                    command=("true",),
                    depends_on=("work",),
                    verifier=True,
                ),
            ]
            store.create_task(contract, nodes, "offline-create")
            store.queue_task(contract.task_id)
            worker = store.claim_ready_node("worker", epoch)
            store.settle_claimed(
                worker,
                NodeResult("succeeded", "work complete", actual_model="local"),
            )
            verifier = store.claim_ready_node("verifier", epoch)
            store.settle_claimed(
                verifier,
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
            presentation = root / "slides.pptx"
            with zipfile.ZipFile(presentation, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            ppt_data = presentation.read_bytes()
            artifact_ref = ArtifactStore(root / "artifacts").put_bytes(ppt_data, "pptx")
            store.record_acceptance_attestation(
                "A12",
                artifact_ref,
                "slides.pptx",
                len(ppt_data),
                "2026-W35",
                "claude-web-session",
                "completed in Claude web using the reserved pool",
            )

            report = build_acceptance_report(store)
            checks = {check["id"]: check for check in report["checks"]}
            backlog = {check["id"]: check for check in report["backlog"]}
            self.assertEqual(checks["A1"]["status"], "ok")
            self.assertNotIn("A2", checks)
            self.assertEqual(backlog["A2"]["status"], "deferred")
            self.assertEqual(checks["A12"]["status"], "pending")

    def test_a12_cli_imports_a_content_addressed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "slides.pptx"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            args = SimpleNamespace(
                home=str(root / "state"),
                acceptance_action="attest-a12",
                artifact=str(artifact),
                quota_window="2026-W35",
                source_session_id="claude-web-session",
                note="completed in Claude web using the reserved pool",
            )
            WorkbenchConfig(
                root / "state",
                deployment_role="authority",
                authority_host=__import__("socket").gethostname(),
                authority_machine_id="test-machine",
            ).initialize()
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {"CODEX_WORKBENCH_PROCESS_HOME": str(root / "process-home")},
            ), patch(
                "codex_workbench.config.authority_machine_id",
                return_value="test-machine",
            ), redirect_stdout(output):
                self.assertEqual(command_acceptance(args), 0)
            receipt = json.loads(output.getvalue())
            self.assertTrue(receipt["artifact_ref"].startswith("sha256:"))
            store = WorkbenchStore(root / "state" / "state.sqlite")
            self.assertEqual(
                {check["id"]: check for check in build_acceptance_report(store)["checks"]}["A12"]["status"],
                "pending",
            )

    def test_report_is_evidence_based_and_keeps_external_checks_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkbenchStore(Path(directory) / "state.sqlite")
            store.initialize()
            coordinator_epoch = store.activate_coordinator("authority-1", "test-machine")
            store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": "authority-1",
                    "machine_id": "test-machine",
                    "coordinator_epoch": coordinator_epoch,
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
            self.assertEqual(checks["A6"]["status"], "pending")
            self.assertEqual(checks["A7"]["status"], "pending")
            self.assertEqual(checks["A11"]["status"], "ok")

    def test_quota_reserve_requires_continuous_window_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkbenchStore(Path(directory) / "state.sqlite")
            store.initialize()
            start = datetime(2026, 8, 24, tzinfo=UTC)
            for window_index in range(2):
                window_start = start + timedelta(hours=5 * window_index)
                for sample_index in range(11):
                    observed = window_start + timedelta(minutes=30 * sample_index)
                    store.write_quota(
                        QuotaSnapshot(
                            observed_at=observed.isoformat(timespec="seconds"),
                            auth_ok=True,
                            auth_method="native-subscription",
                            five_hour_remaining=80 - sample_index,
                            weekly_all_remaining=75,
                            weekly_sonnet_remaining=70,
                            source="settings-usage-export",
                            five_hour_window_id=f"five-{window_index}",
                            weekly_window_id="week-covered",
                        )
                    )
            for sample_index in range(13):
                observed = start + timedelta(hours=12 * sample_index)
                store.write_quota(
                    QuotaSnapshot(
                        observed_at=observed.isoformat(timespec="seconds"),
                        auth_ok=True,
                        auth_method="native-subscription",
                        five_hour_remaining=70,
                        weekly_all_remaining=75 - sample_index,
                        weekly_sonnet_remaining=70 - sample_index,
                        source="settings-usage-export",
                        weekly_window_id="week-covered",
                    )
                )

            checks = {
                check["id"]: check
                for check in build_acceptance_report(store)["checks"]
            }
            self.assertEqual(checks["A6"]["status"], "ok")
            self.assertEqual(checks["A7"]["status"], "ok")

    def test_lifecycle_event_without_current_authority_proof_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkbenchStore(Path(directory) / "state.sqlite")
            store.initialize()
            store.record_system_event(
                "coordinator.started",
                {"instance_id": "only-in-a-log", "pid": 123, "host": "mac-mini"},
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A11"]["status"], "pending")

    def test_new_coordinator_epoch_fences_an_unclosed_crashed_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkbenchStore(Path(directory) / "state.sqlite")
            store.initialize()
            first_epoch = store.activate_coordinator("authority-old", "test-machine")
            store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": "authority-old",
                    "machine_id": "test-machine",
                    "coordinator_epoch": first_epoch,
                    "pid": 111,
                    "host": "mac-mini",
                },
            )
            second_epoch = store.activate_coordinator("authority-new", "test-machine")
            store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": "authority-new",
                    "machine_id": "test-machine",
                    "coordinator_epoch": second_epoch,
                    "pid": 222,
                    "host": "mac-mini",
                },
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A11"]["status"], "ok")

    def test_fixture_quota_provenance_cannot_prove_a8_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("fixture-a8", "test-machine")
            store.write_quota(
                QuotaSnapshot(
                    observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=24,
                    weekly_all_remaining=70,
                    weekly_sonnet_remaining=70,
                    source="acceptance-fixture",
                    five_hour_window_id="fixture-five-hour",
                    weekly_window_id="fixture-week",
                )
            )
            contract = TaskContract(
                task_id="fixture-a8",
                repository=str(root),
                base_sha="fixture",
                objective="fixture quota must not prove fallback",
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
            store.create_task(contract, nodes, "fixture-a8-create")
            store.queue_task(contract.task_id)
            worker = store.claim_ready_node("worker", epoch)
            assert worker is not None
            store.record_node_route(
                contract.task_id,
                "work",
                executor="codex",
                model="gpt-5.6-luna",
                payload={
                    "attempt": 1,
                    "from": "claude",
                    "to": "codex",
                    "model": "gpt-5.6-luna",
                    "zone": "protected",
                    "reason": "Claude quota protection active at 24.0% remaining",
                },
                attempt=worker["attempt"],
                coordinator_epoch=worker["coordinator_epoch"],
                lease_epoch=worker["lease_epoch"],
            )
            store.settle_claimed(
                worker,
                NodeResult(
                    "succeeded",
                    "Codex fallback completed",
                    actual_model="gpt-5.6-luna",
                    result_kind="worker",
                    checks=("fixture-check",),
                ),
            )
            verifier = store.claim_ready_node("verifier", epoch)
            assert verifier is not None
            store.settle_claimed(verifier, NodeResult("succeeded", "accepted"))

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A8"]["status"], "pending")

    def test_deterministic_logs_are_not_real_model_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("deterministic-a10", "test-machine")
            contract = TaskContract(
                task_id="deterministic-a10",
                repository=str(root),
                base_sha="fixture",
                objective="must not count local deterministic output as model delivery",
                allowed_scope=("src",),
            )
            patch_ref = store.artifacts.put_text("fake diff", "patch")
            log_ref = store.artifacts.put_text("stdout from deterministic process", "stdout.log")
            receipt_ref = store.artifacts.put_text("deterministic verdict", "result.json")
            nodes = [
                NodeSpec(
                    "worker",
                    contract.task_id,
                    "worker",
                    "deterministic",
                    "local",
                    command=("true",),
                ),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    command=("true",),
                    depends_on=("worker",),
                    verifier=True,
                ),
            ]
            store.create_task(contract, nodes, "deterministic-a10-create")
            store.queue_task(contract.task_id)
            worker = store.claim_ready_node("worker", epoch)
            assert worker is not None
            store.settle_claimed(
                worker,
                NodeResult(
                    "succeeded",
                    "deterministic worker output",
                    artifacts={"patch": patch_ref, "stdout": log_ref},
                    actual_model="local-deterministic",
                    result_kind="worker",
                    checks=("true",),
                ),
            )
            verifier = store.claim_ready_node("verifier", epoch)
            assert verifier is not None
            store.settle_claimed(
                verifier,
                NodeResult(
                    "succeeded",
                    "deterministic accepted",
                    artifacts={"test-log": receipt_ref, "verdict": receipt_ref},
                    actual_model="local-deterministic",
                    result_kind="verifier",
                    checks=("true",),
                    evidence=(receipt_ref,),
                    verdict="accepted",
                ),
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A10"]["status"], "pending")

    def test_a10_allows_empty_diagnostic_stderr_when_required_evidence_is_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("a10-empty-stderr", "test-machine")
            contract = TaskContract(
                task_id="a10-empty-stderr",
                repository=str(root),
                base_sha="fixture",
                objective="prove empty stderr is a valid diagnostic artifact",
                allowed_scope=("src",),
                required_artifacts=("diff", "test-log", "verdict"),
                executor_model="gpt-5.6-luna",
                verifier_model="gpt-5.6-sol",
            )
            nodes = [
                NodeSpec("work", contract.task_id, "work", "codex", "gpt-5.6-luna", "work"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "codex",
                    "gpt-5.6-sol",
                    "verify",
                    depends_on=("work",),
                    verifier=True,
                ),
            ]
            patch_ref = store.artifacts.put_text("diff --git a/src/a b/src/a\n", "patch")
            empty_stderr = store.artifacts.put_text("", "stderr.log")
            test_ref = store.artifacts.put_text("tests passed", "test-log")
            verdict_ref = store.artifacts.put_text("accepted by Sol", "verdict")
            store.create_task(contract, nodes, "a10-empty-stderr-create")
            store.queue_task(contract.task_id)
            worker = store.claim_ready_node("worker", epoch)
            assert worker is not None
            store.settle_claimed(
                worker,
                NodeResult(
                    "succeeded",
                    "worker complete",
                    artifacts={"patch": patch_ref, "stderr": empty_stderr},
                    actual_model="gpt-5.6-luna",
                    result_kind="worker",
                    checks=("tests passed",),
                ),
            )
            verifier = store.claim_ready_node("verifier", epoch)
            assert verifier is not None
            store.settle_claimed(
                verifier,
                NodeResult(
                    "succeeded",
                    "accepted",
                    artifacts={
                        "test-log": test_ref,
                        "verdict": verdict_ref,
                        "stderr": empty_stderr,
                    },
                    actual_model="gpt-5.6-sol",
                    result_kind="verifier",
                    checks=("tests passed",),
                    evidence=(test_ref, verdict_ref),
                    verdict="accepted",
                ),
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A10"]["status"], "ok")

    def test_a4_requires_real_worker_artifacts_and_sol_verifier_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("a4-authority", "test-machine")

            def finish_task(
                task_id: str,
                executor: str,
                leased_model: str,
                actual_model: str,
                *,
                with_evidence: bool,
            ) -> None:
                work_id = f"{task_id}-work"
                verify_id = f"{task_id}-verify"
                contract = TaskContract(
                    task_id=task_id,
                    repository=str(root),
                    base_sha="fixture",
                    objective=f"prove {actual_model}",
                    allowed_scope=("src",),
                    required_artifacts=(),
                )
                nodes = [
                    NodeSpec(work_id, task_id, "work", executor, leased_model, prompt="perform work"),
                    NodeSpec(
                        verify_id,
                        task_id,
                        "verify",
                        "codex",
                        "gpt-5.6-sol",
                        prompt="verify work",
                        depends_on=(work_id,),
                        verifier=True,
                    ),
                ]
                store.create_task(contract, nodes, f"{task_id}-create")
                store.queue_task(task_id)
                worker = store.claim_ready_node(f"{task_id}-worker", epoch)
                assert worker is not None
                self.assertEqual((worker["task_id"], worker["node_id"]), (task_id, work_id))
                worker_artifacts = (
                    {"patch": store.artifacts.put_text(f"{task_id} patch", "patch")}
                    if with_evidence
                    else {}
                )
                store.settle_claimed(
                    worker,
                    NodeResult(
                        "succeeded",
                        f"{actual_model} completed",
                        artifacts=worker_artifacts,
                        actual_model=actual_model,
                        result_kind="worker",
                        checks=("focused-check",),
                    ),
                )
                verifier = store.claim_ready_node(f"{task_id}-verifier", epoch)
                assert verifier is not None
                self.assertEqual((verifier["task_id"], verifier["node_id"]), (task_id, verify_id))
                verifier_ref = store.artifacts.put_text(f"{task_id} accepted", "json")
                store.settle_claimed(
                    verifier,
                    NodeResult(
                        "succeeded",
                        "Sol accepted",
                        artifacts={"verdict": verifier_ref},
                        actual_model="gpt-5.6-sol",
                        result_kind="verifier",
                        checks=("focused-check",),
                        evidence=(verifier_ref,),
                        verdict="accepted",
                    ),
                )

            finish_task(
                "a4-name-only-luna",
                "codex",
                "gpt-5.6-luna",
                "gpt-5.6-luna",
                with_evidence=False,
            )
            finish_task(
                "a4-name-only-sonnet",
                "claude",
                "sonnet",
                "claude-sonnet-4-6",
                with_evidence=False,
            )
            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A4"]["status"], "pending")

            finish_task(
                "a4-evidenced-luna",
                "codex",
                "gpt-5.6-luna",
                "gpt-5.6-luna",
                with_evidence=True,
            )
            finish_task(
                "a4-evidenced-sonnet",
                "claude",
                "sonnet",
                "claude-sonnet-4-6",
                with_evidence=True,
            )
            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A4"]["status"], "ok")

    def test_a12_passes_only_with_export_receipt_and_real_quota_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            store.write_quota(
                QuotaSnapshot(
                    observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=65,
                    weekly_all_remaining=70,
                    weekly_sonnet_remaining=75,
                    source="settings-usage-export",
                    five_hour_window_id="a12-five-hour",
                    weekly_window_id="a12-week",
                )
            )
            presentation = root / "claude-export.pptx"
            with zipfile.ZipFile(presentation, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            presentation_data = presentation.read_bytes()
            artifact_ref = store.artifacts.put_bytes(presentation_data, "pptx")
            receipt = {
                "receipt_type": "claude-export",
                "provider": "claude-web",
                "status": "completed",
                "source_session_id": "claude-web-real-session",
                "artifact_ref": artifact_ref,
                "quota_window_id": "a12-five-hour",
            }
            receipt_ref = store.artifacts.put_text(json.dumps(receipt), "json")
            store.record_acceptance_attestation(
                "A12",
                artifact_ref,
                presentation.name,
                len(presentation_data),
                "a12-five-hour",
                "claude-web-real-session",
                "completed in Claude web with the reserved pool",
                export_receipt_ref=receipt_ref,
            )

            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A12"]["status"], "ok")

    def test_a12_cli_accepts_a_verifiable_export_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "slides.pptx"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            artifact_digest = sha256(artifact.read_bytes()).hexdigest()
            receipt = root / "export-receipt.json"
            receipt.write_text(
                json.dumps(
                    {
                        "receipt_type": "claude-export",
                        "provider": "claude-web",
                        "status": "completed",
                        "source_session_id": "claude-web-cli-session",
                        "artifact_sha256": artifact_digest,
                        "quota_window_id": "cli-five-hour",
                    }
                )
            )
            args = SimpleNamespace(
                home=str(root / "state"),
                acceptance_action="attest-a12",
                artifact=str(artifact),
                export_receipt=str(receipt),
                quota_window="cli-five-hour",
                source_session_id="claude-web-cli-session",
                note="completed in Claude web using the reserved pool",
            )
            config = WorkbenchConfig(
                root / "state",
                deployment_role="authority",
                authority_host=__import__("socket").gethostname(),
                authority_machine_id="test-machine",
            )
            config.initialize()
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {"CODEX_WORKBENCH_PROCESS_HOME": str(root / "process-home")},
            ), patch(
                "codex_workbench.config.authority_machine_id",
                return_value="test-machine",
            ), redirect_stdout(output):
                self.assertEqual(command_acceptance(args), 0)
            receipt_output = json.loads(output.getvalue())
            store = WorkbenchStore(root / "state" / "state.sqlite")
            event = store.read_events()[-1]
            self.assertEqual(event["event_type"], "acceptance.attested")
            self.assertTrue(receipt_output["artifact_ref"].startswith("sha256:"))
            self.assertIn("export_receipt_ref", event["payload"])

    def test_restart_and_quota_fallback_require_durable_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("acceptance-fallback", "test-machine")
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
            store.write_quota(
                QuotaSnapshot(
                    observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=24,
                    weekly_all_remaining=70,
                    weekly_sonnet_remaining=70,
                    source="settings-usage-export",
                    five_hour_window_id="fallback-five-hour",
                    weekly_window_id="fallback-week",
                )
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
            worker = store.claim_ready_node("worker", epoch)
            store.record_node_route(
                contract.task_id,
                "work",
                executor="codex",
                model="gpt-5.6-luna",
                payload={
                    "attempt": 1,
                    "from": "claude",
                    "to": "codex",
                    "model": "gpt-5.6-luna",
                    "reason": "Claude quota protection active at 25.0% remaining",
                },
                attempt=worker["attempt"],
                coordinator_epoch=worker["coordinator_epoch"],
                lease_epoch=worker["lease_epoch"],
            )
            store.settle_claimed(
                worker,
                NodeResult(
                    "succeeded", "fallback complete", actual_model="gpt-5.6-luna",
                    result_kind="worker", checks=("fixture-check",),
                ),
            )
            verifier = store.claim_ready_node("verifier", epoch)
            store.settle_claimed(verifier, NodeResult("succeeded", "accepted"))
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
            self.assertEqual(checks["A3"]["status"], "pending")
            self.assertEqual(checks["A8"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
