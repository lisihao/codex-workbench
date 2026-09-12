from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import socket
import tempfile
import unittest

from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeSpec, TaskContract, canonical_json
from codex_workbench.node_recovery_deployment import observe_repair_delivery
from codex_workbench.store import WorkbenchStore


_COMMIT = "a" * 40
_VERSION = "9.9.9"
_TARGET = "macos-fixture"
_MACHINE = "deployment-fixture-machine"
_INSTANCE = "deployment-fixture-instance"
_TIME = "2026-09-11T12:00:00+00:00"
_PARENT_TASK = "blocked-parent-task"
_PARENT_NODE = "blocked-parent-node"
_PARENT_ATTEMPT = 3
_PARENT_REVISION = 7


def _source() -> dict:
    return {
        "commit": _COMMIT,
        "version": _VERSION,
        "manifest": {
            "algorithm": "sha256",
            "files": {
                "pyproject.toml": "1" * 64,
                "src/codex_workbench/__init__.py": "2" * 64,
                "src/codex_workbench/deployment_helper.py": "3" * 64,
                "scripts/install-macos.py": "4" * 64,
            },
        },
    }


class RepairDeploymentObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="node-recovery-deployment-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.config = WorkbenchConfig(
            self.root / "state",
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=_MACHINE,
        )
        self.config.initialize()
        self.config.config_file.write_text(
            json.dumps(
                {
                    "local_deployment": {
                        "schema_version": 1,
                        "target": _TARGET,
                    }
                }
            ),
            encoding="utf-8",
        )
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator(_INSTANCE, _MACHINE)
        self.store.record_system_event(
            "coordinator.started",
            {
                "instance_id": _INSTANCE,
                "machine_id": _MACHINE,
                "coordinator_epoch": self.epoch,
                "pid": 1,
                "host": "fixture-host",
            },
        )
        self.task_id = "repair-task"
        self.request_id = "repair-request"
        self.repair_fingerprint = "b" * 64

    def _episode(self, *, readiness_ref: str | None = None) -> dict:
        return {
            "episode_id": "episode-fixture",
            "task_id": _PARENT_TASK,
            "node_id": _PARENT_NODE,
            "node_attempt": _PARENT_ATTEMPT,
            "task_revision": _PARENT_REVISION,
            "observation": (
                {"recovery_readiness_ref": readiness_ref}
                if readiness_ref is not None
                else {}
            ),
            "repair": {
                "repair_request_id": self.request_id,
                "repair_task_id": self.task_id,
                "repair_fingerprint": self.repair_fingerprint,
            },
        }

    def _create_task(self, state: str = "accepted") -> None:
        contract = TaskContract(
            task_id=self.task_id,
            repository=str(self.repository),
            base_sha=_COMMIT,
            objective="fixture linked repair",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("worker", self.task_id, "repair", "fixture", "fixture"),
                NodeSpec(
                    "verify",
                    self.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    depends_on=("worker",),
                    verifier=True,
                ),
            ],
            "create-repair-task",
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET state = ?, state_revision = 2 WHERE task_id = ?",
                (state, self.task_id),
            )
            connection.execute(
                """
                INSERT INTO planning_requests(
                    command_id, request_hash, task_id, request_json, state, attempt,
                    coordinator_epoch, result_json, error, started_at, settled_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'succeeded', 1, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    self.request_id,
                    "planning-request-hash",
                    self.task_id,
                    canonical_json({"task_id": self.task_id}),
                    self.epoch,
                    canonical_json({"materialized": True}),
                    _TIME,
                    _TIME,
                    _TIME,
                    _TIME,
                ),
            )

    def _insert_reservation(self, state: str) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO planning_requests(
                    command_id, request_hash, task_id, request_json, state, attempt,
                    coordinator_epoch, result_json, error, started_at, settled_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    self.request_id,
                    "planning-request-hash",
                    self.task_id,
                    canonical_json({"task_id": self.task_id}),
                    state,
                    self.epoch,
                    "fixture reservation state",
                    _TIME,
                    _TIME,
                    _TIME,
                    _TIME,
                ),
            )

    def _objective_request(self) -> dict:
        return {
            "requested_endpoints": {
                "deployment": {
                    "target": _TARGET,
                    "health_checks": ["health"],
                    "functional_checks": ["functional"],
                }
            },
            "scope": {"paths": ["src"], "repository": str(self.repository)},
            "authority": {"delivery": "fixture-local-authority"},
            "budget": {
                "attempt_limit": 2,
                "time_budget_seconds": 300,
                "cost_budget": 1,
                "base_backoff_seconds": 1,
                "max_backoff_seconds": 2,
            },
        }

    def _complete_local_delivery(self) -> tuple[dict, dict, dict]:
        self._create_task()
        objective = self.store.create_delivery_objective(
            self.task_id, "create-local-objective", self._objective_request()
        )
        claimed = self.store.claim_delivery_objective(
            objective["objective_id"],
            "fixture-delivery-owner",
            self.epoch,
            expected_revision=objective["state_revision"],
        )
        assert claimed is not None
        current = claimed
        source = _source()
        current, _ = self._record_stage(
            current,
            "plan",
            {"stage": "plan"},
            {"source": source},
        )
        current, _ = self._record_stage(
            current,
            "implement",
            {"stage": "implement"},
            {"build": {"commit": _COMMIT, "version": _VERSION}},
        )
        current, _ = self._record_stage(
            current,
            "verify",
            {"stage": "verify"},
            {"node": "node-v24", "pnpm": "pnpm-v11"},
        )
        deploy_identity = {
            "target": _TARGET,
            "dispatch_id": "",
            "request_fingerprint": "c" * 64,
            "commit": _COMMIT,
            "version": _VERSION,
            "source_manifest": source["manifest"],
            "pre_install_coordinator_epoch": self.epoch,
        }
        deploy_body = {
            "target": _TARGET,
            "source": source,
            "helper_status": "succeeded",
        }
        dispatch = self.store.begin_delivery_stage_dispatch(
            current["objective_id"],
            stage="deploy",
            attempt=current["stage_attempt"],
            expected_revision=current["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=current["lease"]["lease_epoch"],
            adapter_name="fixture-local-deployment",
        )
        deploy_identity["dispatch_id"] = dispatch["dispatch_id"]
        current, deploy_receipt = self._record_stage(
            current,
            "deploy",
            deploy_body,
            {"deploy": deploy_identity},
            dispatch=dispatch,
        )
        self.config.install_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.config.install_manifest.write_text(
            json.dumps({"commit": _COMMIT, "version": _VERSION}), encoding="utf-8"
        )
        runtime_identity = {
            "target": _TARGET,
            "commit": _COMMIT,
            "version": _VERSION,
            "health_url": "http://127.0.0.1:8766/health",
            "functional_url": "http://127.0.0.1:8766/api/snapshot",
            "coordinator_epoch": self.epoch,
            "coordinator_instance_id": _INSTANCE,
        }
        check = {
            "status": "passed",
            "http_status": 200,
            "version": _VERSION,
            "build_commit": _COMMIT,
            "build_version": _VERSION,
            "coordinator_epoch": self.epoch,
            "coordinator_instance_id": _INSTANCE,
        }
        live_body = {
            "target": _TARGET,
            "deployment_dispatch_id": dispatch["dispatch_id"],
            "expected_source": source,
            "installed_manifest": {
                "path": str(self.config.install_manifest),
                "commit": _COMMIT,
                "version": _VERSION,
            },
            "checks": {
                "health": {"health": check},
                "functional": {"functional": dict(check)},
            },
        }
        live_dispatch = self.store.begin_delivery_stage_dispatch(
            current["objective_id"],
            stage="live-verify",
            attempt=current["stage_attempt"],
            expected_revision=current["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=current["lease"]["lease_epoch"],
            adapter_name="fixture-local-deployment",
        )
        current, live_receipt = self._record_stage(
            current,
            "live-verify",
            live_body,
            {"runtime": runtime_identity},
            dispatch=live_dispatch,
        )
        self.assertEqual((current["state"], current["stage"]), ("complete", "live-verify"))
        return current, deploy_receipt, live_receipt

    def _record_stage(
        self,
        objective: dict,
        stage: str,
        receipt: dict,
        identities: dict,
        *,
        dispatch: dict | None = None,
    ) -> tuple[dict, dict]:
        result = self.store.record_delivery_stage_receipt(
            objective["objective_id"],
            f"{stage}-receipt",
            stage=stage,
            attempt=objective["stage_attempt"],
            expected_revision=objective["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=objective["lease"]["lease_epoch"],
            receipt=receipt,
            evidence_fingerprint=f"{stage}-evidence",
            identities=identities,
            dispatch_id=dispatch["dispatch_id"] if dispatch is not None else None,
        )
        return result["objective"], result["receipt"]

    def _replace_authority(self) -> tuple[int, str]:
        instance = "replacement-instance"
        epoch = self.store.activate_coordinator(instance, _MACHINE)
        self.store.record_system_event(
            "coordinator.started",
            {
                "instance_id": instance,
                "machine_id": _MACHINE,
                "coordinator_epoch": epoch,
                "pid": 2,
                "host": "fixture-host",
            },
        )
        return epoch, instance

    def _readiness_proof(
        self,
        *,
        task_id: str = _PARENT_TASK,
        node_id: str = _PARENT_NODE,
        node_attempt: int = _PARENT_ATTEMPT,
        task_revision: int = _PARENT_REVISION,
        authority_epoch: int | None = None,
        authority_instance_id: str | None = None,
        install_manifest_sha256: str | None = None,
        report: dict | None = None,
    ) -> str:
        authority = self.store.authority_status()
        assert authority is not None
        current_epoch = authority["authority_epoch"]
        current_instance = authority["instance_id"]
        manifest_bytes = self.config.install_manifest.read_bytes()
        payload = {
            "kind": "node-recovery-readiness/v1",
            "task_id": task_id,
            "node_id": node_id,
            "node_attempt": node_attempt,
            "task_revision": task_revision,
            "authority_epoch": (
                current_epoch if authority_epoch is None else authority_epoch
            ),
            "authority_instance_id": (
                current_instance if authority_instance_id is None else authority_instance_id
            ),
            "install_manifest_sha256": (
                sha256(manifest_bytes).hexdigest()
                if install_manifest_sha256 is None
                else install_manifest_sha256
            ),
            "report": (
                {
                    "schema_version": 1,
                    "worktree": str(self.repository),
                    "ready": True,
                    "failure_origin": None,
                    "summary": "execution environment is ready",
                    "elapsed_ms": 1,
                    "checks": [
                        {
                            "id": "fixture-runtime",
                            "kind": "tool",
                            "status": "passed",
                            "detail": {},
                        }
                    ],
                    "failures": [],
                }
                if report is None
                else report
            ),
        }
        return self.store.artifacts.put_text(
            canonical_json(payload), "node-recovery-readiness.json"
        )

    def test_accepted_repair_without_deployment_waits(self) -> None:
        self._create_task()

        observed = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            observed,
            {
                "state": "waiting",
                "reason_kind": "repair_accepted_not_deployed",
                "owner": "authority",
                "requires_authorization": False,
                "repair_request_id": self.request_id,
                "repair_task_id": self.task_id,
                "expected_repair_fingerprint": self.repair_fingerprint,
            },
        )

    def test_pending_approval_and_failed_repair_are_typed(self) -> None:
        self._create_task("needs_approval")

        approval = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            (approval["state"], approval["reason_kind"], approval["owner"], approval["requires_authorization"]),
            ("needs_action", "repair_task_needs_approval", "operator", True),
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET state = 'needs_fix' WHERE task_id = ?", (self.task_id,)
            )
        failed = observe_repair_delivery(self.store, self.config, self._episode())
        self.assertEqual(
            (failed["state"], failed["reason_kind"], failed["owner"], failed["requires_authorization"]),
            ("needs_action", "repair_task_failed", "authority", False),
        )

    def test_unknown_reservation_stays_waiting_without_requeue(self) -> None:
        self._insert_reservation("indeterminate")
        before = self.store.read_events()

        observed = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            (observed["state"], observed["reason_kind"]),
            ("waiting", "repair_reservation_unknown"),
        )
        self.assertEqual(self.store.read_events(), before)

    def test_queued_and_failed_planning_are_typed_without_dispatch(self) -> None:
        self._insert_reservation("pending")

        queued = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            (queued["state"], queued["reason_kind"], queued["owner"], queued["requires_authorization"]),
            ("waiting", "repair_planning_pending", "authority", False),
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE planning_requests SET state = 'failed' WHERE command_id = ?",
                (self.request_id,),
            )
        failed = observe_repair_delivery(self.store, self.config, self._episode())
        self.assertEqual(
            (failed["state"], failed["reason_kind"], failed["owner"], failed["requires_authorization"]),
            ("needs_action", "repair_planning_failed", "authority", False),
        )

    def test_matching_local_delivery_chain_is_verified(self) -> None:
        objective, deploy, live = self._complete_local_delivery()

        observed = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            (observed["state"], observed["reason_kind"], observed["owner"], observed["requires_authorization"]),
            ("verified", "repair_deployment_verified", "authority", False),
        )
        self.assertEqual(observed["expected_repair_fingerprint"], self.repair_fingerprint)
        self.assertNotEqual(
            observed["verified_deployment_fingerprint"], observed["expected_repair_fingerprint"]
        )
        self.assertEqual(
            observed["evidence_refs"],
            {
                "objective_id": objective["objective_id"],
                "deploy_receipt_id": deploy["receipt_id"],
                "live_verify_receipt_id": live["receipt_id"],
            },
        )
        self.assertFalse(observed["fresh_readiness_confirmed"])
        self.assertTrue(observed["historical_live_verification_retained"])

    def test_wrong_manifest_or_dispatch_is_unknown(self) -> None:
        _objective, _deploy, live = self._complete_local_delivery()
        self.config.install_manifest.write_text(
            json.dumps({"commit": "d" * 40, "version": _VERSION}), encoding="utf-8"
        )
        wrong_manifest = observe_repair_delivery(self.store, self.config, self._episode())
        self.assertEqual(
            (wrong_manifest["state"], wrong_manifest["reason_kind"]),
            ("waiting", "repair_delivery_unknown"),
        )
        self.config.install_manifest.write_text(
            json.dumps({"commit": _COMMIT, "version": _VERSION}), encoding="utf-8"
        )
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM delivery_stage_receipts WHERE receipt_id = ?",
                (live["receipt_id"],),
            ).fetchone()
            receipt = json.loads(row["receipt_json"])
            receipt["deployment_dispatch_id"] = "delivery-dispatch-wrong"
            connection.execute(
                "UPDATE delivery_stage_receipts SET receipt_json = ? WHERE receipt_id = ?",
                (canonical_json(receipt), live["receipt_id"]),
            )
        wrong_dispatch = observe_repair_delivery(self.store, self.config, self._episode())
        self.assertEqual(
            (wrong_dispatch["state"], wrong_dispatch["reason_kind"]),
            ("waiting", "repair_delivery_unknown"),
        )

    def test_different_configured_target_is_unknown(self) -> None:
        self._complete_local_delivery()
        self.config.config_file.write_text(
            json.dumps({"local_deployment": {"schema_version": 1, "target": "other-target"}}),
            encoding="utf-8",
        )

        observed = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            (observed["state"], observed["reason_kind"]),
            ("waiting", "repair_delivery_unknown"),
        )

    def test_new_authority_epoch_requires_fresh_readiness(self) -> None:
        self._complete_local_delivery()
        self._replace_authority()

        observed = observe_repair_delivery(self.store, self.config, self._episode())

        self.assertEqual(
            (observed["state"], observed["reason_kind"], observed["requires_authorization"]),
            ("waiting", "fresh_authority_readiness_required", False),
        )
        self.assertTrue(observed["fresh_readiness_required"])

    def test_epoch_changed_ready_proof_restores_verified_delivery(self) -> None:
        objective, deploy, live = self._complete_local_delivery()
        self._replace_authority()
        proof = self._readiness_proof()

        observed = observe_repair_delivery(
            self.store, self.config, self._episode(readiness_ref=proof)
        )

        self.assertEqual(
            (observed["state"], observed["reason_kind"]),
            ("verified", "repair_deployment_verified"),
        )
        self.assertTrue(observed["fresh_readiness_confirmed"])
        self.assertTrue(observed["historical_live_verification_retained"])
        self.assertEqual(
            observed["evidence_refs"],
            {
                "objective_id": objective["objective_id"],
                "deploy_receipt_id": deploy["receipt_id"],
                "live_verify_receipt_id": live["receipt_id"],
                "recovery_readiness_ref": proof,
            },
        )

    def test_epoch_changed_rejects_stale_or_unbound_readiness_proofs(self) -> None:
        self._complete_local_delivery()
        self._replace_authority()
        proofs = {
            "old_epoch": self._readiness_proof(
                authority_epoch=self.epoch,
                authority_instance_id=_INSTANCE,
            ),
            "wrong_node": self._readiness_proof(node_id="another-blocked-node"),
            "wrong_install_manifest": self._readiness_proof(
                install_manifest_sha256="f" * 64
            ),
            "forged_report_shape": self._readiness_proof(report={"ready": True}),
        }

        for name, proof in proofs.items():
            with self.subTest(proof=name):
                observed = observe_repair_delivery(
                    self.store, self.config, self._episode(readiness_ref=proof)
                )
                self.assertEqual(
                    (observed["state"], observed["reason_kind"]),
                    ("waiting", "fresh_authority_readiness_required"),
                )
                self.assertTrue(observed["fresh_readiness_required"])
