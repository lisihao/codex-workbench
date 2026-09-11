from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from codex_workbench.api import WorkbenchHandler
from codex_workbench.delivery_lifecycle import (
    DeliveryLifecycleReconciler,
    DeliveryStageOutcome,
    FixtureDeliveryStageAdapter,
)
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, now_iso
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class DeliveryLifecyclePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("lifecycle-fixture", "fixture-machine")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _task(
        self,
        task_id: str,
        workers: list[NodeSpec],
        *,
        external_write: bool = False,
    ) -> TaskContract:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.root),
            base_sha="a" * 40,
            objective=f"fixture {task_id}",
            allowed_scope=("src",),
            external_write_permission=external_write,
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "verify",
            "fixture",
            "fixture",
            "verify fixture",
            depends_on=tuple(node.node_id for node in workers),
            verifier=True,
            ordinal=100,
        )
        self.store.create_task(contract, [*workers, verifier], f"create-{task_id}")
        return contract

    @staticmethod
    def _objective_request(*, attempt_limit: int = 3, rollback: bool = False) -> dict:
        deployment = {
            "target": "fixture",
            "health_checks": ["healthz"],
            "functional_checks": ["smoke"],
        }
        if rollback:
            deployment["rollback_policy"] = {"preauthorized": True, "reversible": True}
        return {
            "requested_endpoints": {
                "github": {"remote": "origin", "base_branch": "main", "publish": True},
                "deployment": deployment,
            },
            "scope": {"paths": ["src"], "repository": "fixture"},
            "authority": {"delivery": "explicit-fixture-authority"},
            "budget": {
                "attempt_limit": attempt_limit,
                "time_budget_seconds": 3600,
                "cost_budget": 10,
                "base_backoff_seconds": 1,
                "max_backoff_seconds": 2,
            },
        }

    def _claim_objective(self, objective: dict) -> dict:
        claimed = self.store.claim_delivery_objective(
            objective["objective_id"],
            "fixture-owner",
            self.epoch,
            expected_revision=objective["state_revision"],
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        return claimed

    def _advance_to_stage(self, objective: dict, target_stage: str) -> dict:
        """Fixture-only successful receipts through the stage before target."""

        identities = {
            "plan": {"source": "source-fixture"},
            "verify": {"node": "node-v24.0.0", "pnpm": "pnpm-v10.0.0"},
            "integrate": {"build": "build-fixture"},
        }
        current = objective
        sequence = 0
        while current["stage"] != target_stage:
            stage = current["stage"]
            sequence += 1
            current = self.store.record_delivery_stage_receipt(
                current["objective_id"],
                f"advance-{current['objective_id']}-{stage}-{current['stage_attempt']}",
                stage=stage,
                attempt=current["stage_attempt"],
                expected_revision=current["state_revision"],
                coordinator_epoch=self.epoch,
                lease_epoch=current["lease"]["lease_epoch"],
                receipt={"fixture": "advance", "stage": stage, "sequence": sequence},
                evidence_fingerprint=f"advance-evidence-{stage}-{sequence}",
                identities=identities.get(stage),
            )["objective"]
        return current

    @staticmethod
    def _live_receipt() -> dict:
        return {
            "checks": {
                "health": {"healthz": "passed"},
                "functional": {"smoke": "passed"},
            }
        }

    def test_objective_can_attach_to_a_planning_reservation_without_creating_a_shadow_task(self) -> None:
        task_id = "reserved-lifecycle"
        self.store.enqueue_planning_request(
            "reserve-lifecycle-plan",
            task_id,
            {"objective": "plan the fixture lifecycle"},
        )
        objective = self.store.create_delivery_objective(
            task_id,
            "create-reserved-lifecycle",
            self._objective_request(),
        )
        self.assertEqual((objective["task_id"], objective["stage"]), (task_id, "plan"))
        with self.assertRaises(KeyError):
            self.store.get_task(task_id)

    def test_additive_lifecycle_schema_reinitializes_idempotently_at_v14(self) -> None:
        with self.store.connection() as connection:
            connection.execute("DROP TABLE delivery_stage_dispatches")
            connection.execute("DROP TABLE delivery_stage_receipts")
            connection.execute("DROP TABLE delivery_authorization_receipts")
            connection.execute("DROP TABLE node_admission_waits")
            connection.execute("DROP TABLE delivery_objectives")
            connection.execute("DELETE FROM metadata WHERE key = 'delivery_lifecycle_schema_version'")
        self.store.initialize()
        with self.store.connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            marker = connection.execute(
                "SELECT value FROM metadata WHERE key = 'delivery_lifecycle_schema_version'"
            ).fetchone()
        self.assertTrue(
            {
                "delivery_objectives",
                "delivery_stage_receipts",
                "delivery_stage_dispatches",
                "delivery_authorization_receipts",
                "node_admission_waits",
            }.issubset(tables)
        )
        self.assertEqual(marker["value"], "1")
        self.assertEqual(self.store.health()["schema_version"], 14)

    def _accept_task(self, task_id: str) -> dict:
        self.store.queue_task(task_id)
        while True:
            claimed = self.store.claim_ready_node("accept-worker", self.epoch)
            if claimed is None:
                break
            self.store.settle_claimed(claimed, NodeResult("succeeded", "fixture complete"))
            if self.store.get_task(task_id)["state"] == "accepted":
                break
        task = self.store.get_task(task_id)
        self.assertEqual(task["state"], "accepted")
        return task

    def test_stage_chain_persists_receipts_exact_identities_and_duplicate_is_noop(self) -> None:
        task_id = "lifecycle-chain"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        objective = self.store.create_delivery_objective(
            task_id,
            "create-objective-chain",
            self._objective_request(),
        )
        self.assertEqual(objective["stage"], "plan")
        self.assertEqual(objective["state"], "active")
        self.assertEqual(self.store.health()["schema_version"], 14)
        objective = self._claim_objective(objective)
        duplicate_claim = self.store.claim_delivery_objective(
            objective["objective_id"],
            "fixture-owner",
            self.epoch,
            expected_revision=objective["state_revision"],
        )
        self.assertEqual(duplicate_claim, objective)

        identities_by_stage = {
            "plan": {"source": "sha256:source-fixture"},
            "verify": {"node": "node-v24.0.0", "pnpm": "pnpm-v10.0.0"},
            "integrate": {"build": "build-fixture"},
            "deploy": {"deploy": "deploy-fixture"},
            "live-verify": {"runtime": "runtime-fixture"},
        }
        final_call: dict | None = None
        for index, stage in enumerate(
            ("plan", "implement", "verify", "integrate", "ci", "publish", "deploy", "live-verify"),
            start=1,
        ):
            self.assertEqual((objective["stage"], objective["stage_attempt"]), (stage, 1))
            result = self.store.record_delivery_stage_receipt(
                objective["objective_id"],
                f"receipt-{stage}",
                stage=stage,
                attempt=1,
                expected_revision=objective["state_revision"],
                coordinator_epoch=self.epoch,
                lease_epoch=objective["lease"]["lease_epoch"],
                receipt={
                    "fixture_stage": stage,
                    "sequence": index,
                    **(
                        {
                            "checks": {
                                "health": {"healthz": "passed"},
                                "functional": {"smoke": "passed"},
                            }
                        }
                        if stage == "live-verify"
                        else {}
                    ),
                },
                evidence_fingerprint=f"fingerprint-{stage}",
                identities=identities_by_stage.get(stage),
            )
            self.assertFalse(result["idempotent"])
            objective = result["objective"]
            if stage == "live-verify":
                final_call = result

        self.assertEqual(objective["state"], "complete")
        self.assertEqual(
            set(objective["identities"]),
            {"node", "pnpm", "source", "build", "deploy", "runtime"},
        )
        self.assertEqual(len(objective["stage_receipts"]), 8)
        assert final_call is not None
        duplicate = self.store.record_delivery_stage_receipt(
            objective["objective_id"],
            "receipt-live-verify",
            stage="live-verify",
            attempt=1,
            expected_revision=final_call["objective"]["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=final_call["objective"]["lease"]["lease_epoch"],
            receipt={
                "fixture_stage": "live-verify",
                "sequence": 8,
                "checks": {
                    "health": {"healthz": "passed"},
                    "functional": {"smoke": "passed"},
                },
            },
            evidence_fingerprint="fingerprint-live-verify",
            identities={"runtime": "runtime-fixture"},
        )
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["objective"]["state_revision"], objective["state_revision"])

    def test_retry_budget_and_late_stage_receipt_are_fenced(self) -> None:
        task_id = "lifecycle-retry"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        objective = self._claim_objective(
            self.store.create_delivery_objective(
                task_id, "create-objective-retry", self._objective_request(attempt_limit=2)
            )
        )
        first = self.store.record_delivery_stage_receipt(
            objective["objective_id"],
            "retry-one",
            stage="plan",
            attempt=1,
            expected_revision=objective["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=objective["lease"]["lease_epoch"],
            status="failed",
            receipt={"fixture": "first failure"},
            failure={"kind": "execution-environment", "detail": "fixture environment unavailable"},
            retry_eligible=True,
            next_wakeup_at=now_iso(),
        )["objective"]
        self.assertEqual((first["state"], first["stage_attempt"]), ("waiting", 2))
        resumed = self._claim_objective(first)
        second = self.store.record_delivery_stage_receipt(
            resumed["objective_id"],
            "retry-two",
            stage="plan",
            attempt=2,
            expected_revision=resumed["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=resumed["lease"]["lease_epoch"],
            status="failed",
            receipt={"fixture": "second failure"},
            failure={"kind": "execution-environment", "detail": "fixture environment unavailable"},
            retry_eligible=True,
        )["objective"]
        self.assertEqual(second["state"], "needs_decision")
        self.assertEqual(second["wait_reason"]["resolution"], "choose_recovery_or_stop")
        with self.assertRaisesRegex(StateConflictError, "lease is stale|stale for the current"):
            self.store.record_delivery_stage_receipt(
                resumed["objective_id"],
                "late-retry-one",
                stage="plan",
                attempt=1,
                expected_revision=second["state_revision"],
                coordinator_epoch=self.epoch,
                lease_epoch=resumed["lease"]["lease_epoch"],
                receipt={"late": True},
            )

    def test_live_completion_rejects_process_existence_without_configured_health_and_functional_receipts(self) -> None:
        task_id = "live-check-boundary"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        objective = self._advance_to_stage(
            self._claim_objective(
                self.store.create_delivery_objective(
                    task_id, "create-live-check-boundary", self._objective_request()
                )
            ),
            "live-verify",
        )
        with self.assertRaisesRegex(StateConflictError, "health check results are required"):
            self.store.record_delivery_stage_receipt(
                objective["objective_id"],
                "process-exists-is-not-live-verify",
                stage="live-verify",
                attempt=1,
                expected_revision=objective["state_revision"],
                coordinator_epoch=self.epoch,
                lease_epoch=objective["lease"]["lease_epoch"],
                receipt={"process_exists": True},
                evidence_fingerprint="process-exists-evidence",
                identities={"runtime": "runtime-fixture"},
            )
        retained = self.store.get_delivery_objective(objective["objective_id"])
        self.assertEqual((retained["state"], retained["stage"]), ("active", "live-verify"))

    def test_failure_kind_selects_durable_recovery_or_decision_boundary(self) -> None:
        automatic_actions = {
            "missing-artifact-input": "stage_and_hash_artifact_input",
            "invalid-planning-scopes": "repair_planning_scopes_idempotently",
            "stale-base-ref": "refresh_authoritative_ref_preserve_provenance",
            "execution-environment": "discover_authorized_host_test_capability",
            "provider-unavailable-quota": "wait_for_provider_quota",
            "result-envelope-rejected": "repair_result_envelope",
            "verification-failure": "diagnose_and_repair",
        }
        manual_actions = {
            "unknown-effects": "reconcile_authoritatively",
            "permission-denied": "grant_scope_limited_authorization",
            "missing-essential-user-choice": "provide_essential_user_choice",
        }
        for kind, action in {**automatic_actions, **manual_actions}.items():
            task_id = f"recovery-{kind}"
            self._task(
                task_id,
                [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
            )
            objective = self._claim_objective(
                self.store.create_delivery_objective(
                    task_id,
                    f"create-{task_id}",
                    self._objective_request(attempt_limit=2),
                )
            )
            outcome = self.store.record_delivery_stage_receipt(
                objective["objective_id"],
                f"receipt-{task_id}",
                stage="plan",
                attempt=1,
                expected_revision=objective["state_revision"],
                coordinator_epoch=self.epoch,
                lease_epoch=objective["lease"]["lease_epoch"],
                status="failed",
                receipt={"fixture": kind},
                failure={"kind": kind, "detail": f"fixture {kind}"},
                retry_eligible=True,
                next_wakeup_at=now_iso(),
            )["objective"]
            self.assertEqual(outcome["wait_reason"]["recovery"]["action"], action)
            if kind in automatic_actions:
                self.assertEqual((outcome["state"], outcome["stage_attempt"]), ("waiting", 2))
                self.assertEqual(outcome["next_action"]["action"], action)
                self.assertFalse(outcome["wait_reason"]["recovery"]["requires_human_decision"])
            else:
                self.assertEqual(outcome["state"], "needs_decision")
                self.assertIsNone(outcome["next_wakeup_at"])
                self.assertEqual(outcome["next_action"]["action"], "request_human_decision")
                self.assertEqual(outcome["next_action"]["recommended_recovery"], action)
                self.assertTrue(outcome["wait_reason"]["recovery"]["requires_human_decision"])
                if kind == "unknown-effects":
                    self.assertTrue(
                        outcome["wait_reason"]["recovery"][
                            "requires_authoritative_reconciliation"
                        ]
                    )

    def test_scope_conflict_wait_is_visible_deduplicated_and_resumes_after_release(self) -> None:
        task_id = "scope-wait"
        first = NodeSpec(
            "first", task_id, "first", "fixture", "fixture", "work", write_scopes=("src/shared",), ordinal=1
        )
        blocked = NodeSpec(
            "blocked", task_id, "blocked", "fixture", "fixture", "work", write_scopes=("src/shared",), ordinal=2
        )
        self._task(task_id, [first, blocked])
        self.store.queue_task(task_id)
        claimed_first = self.store.claim_ready_node("scope-first", self.epoch)
        self.assertEqual(claimed_first["node_id"], "first")
        other_id = "unrelated"
        self._task(
            other_id,
            [NodeSpec("other", other_id, "other", "fixture", "fixture", "work", write_scopes=("src/other",), ordinal=1)],
        )
        self.store.queue_task(other_id)
        claimed_other = self.store.claim_ready_node("scope-other", self.epoch)
        self.assertEqual((claimed_other["task_id"], claimed_other["node_id"]), (other_id, "other"))
        self.assertIsNone(self.store.claim_ready_node("scope-wait-scan", self.epoch))

        waiting = next(node for node in self.store.get_task(task_id)["nodes"] if node["node_id"] == "blocked")
        admission_wait = waiting["admission_wait"]
        self.assertIsNotNone(admission_wait)
        assert admission_wait is not None
        self.assertEqual(admission_wait["reason_kind"], "scope_access_conflict")
        self.assertEqual(admission_wait["blocking"]["nodes"][0]["node_id"], "first")
        self.assertEqual(admission_wait["blocking"]["nodes"][0]["conflict_paths"][0]["overlap"], "src/shared")
        before_events = [
            event
            for event in self.store.read_events(task_id=task_id)
            if event["event_type"] == "node.admission_waiting"
        ]
        self.assertEqual(len(before_events), 1)
        self.assertIsNone(self.store.claim_ready_node("scope-repeat", self.epoch))
        after_events = [
            event
            for event in self.store.read_events(task_id=task_id)
            if event["event_type"] == "node.admission_waiting"
        ]
        self.assertEqual(len(after_events), 1)
        self.assertIsNone(next(node for node in self.store.get_task(other_id)["nodes"] if node["node_id"] == "verify")["admission_wait"])

        self.store.settle_claimed(claimed_first, NodeResult("succeeded", "released"))
        claimed_blocked = self.store.claim_ready_node("scope-resumed", self.epoch)
        self.assertEqual((claimed_blocked["task_id"], claimed_blocked["node_id"]), (task_id, "blocked"))
        resumed = [
            event
            for event in self.store.read_events(task_id=task_id)
            if event["event_type"] == "node.admission_resumed"
        ]
        self.assertEqual(len(resumed), 1)
        self.assertIsNone(next(node for node in self.store.get_task(task_id)["nodes"] if node["node_id"] == "blocked")["admission_wait"])
        self.assertIsNone(self.store.claim_ready_node("no-duplicate", self.epoch))

    def test_scope_wait_survives_reopen_and_releases_exactly_one_claim(self) -> None:
        blocker_task = "quota-repair"
        candidate_task = "lifecycle-plan"
        self._task(
            blocker_task,
            [
                NodeSpec(
                    "repair",
                    blocker_task,
                    "quota repair",
                    "fixture",
                    "fixture",
                    "repair",
                    write_scopes=("src/codex_workbench",),
                    ordinal=1,
                )
            ],
        )
        self._task(
            candidate_task,
            [
                NodeSpec(
                    "plan",
                    candidate_task,
                    "lifecycle read",
                    "fixture",
                    "fixture",
                    "read source",
                    read_scopes=("src",),
                    ordinal=1,
                )
            ],
        )
        self.store.queue_task(blocker_task)
        blocker = self.store.claim_ready_node("quota-worker", self.epoch)
        self.assertEqual((blocker["task_id"], blocker["node_id"]), (blocker_task, "repair"))
        self.store.queue_task(candidate_task)
        self.assertIsNone(self.store.claim_ready_node("lifecycle-worker", self.epoch))

        reopened = WorkbenchStore(self.root / "state.sqlite")
        reopened.initialize()
        waits = reopened.pending_node_admission_waits()
        self.assertEqual([(wait["task_id"], wait["node_id"]) for wait in waits], [(candidate_task, "plan")])
        wait = waits[0]
        self.assertEqual(wait["reason_kind"], "scope_access_conflict")
        self.assertEqual(wait["next_action"]["action"], "claim_when_scope_released")
        self.assertEqual(wait["blocking"]["nodes"][0]["task_id"], blocker_task)
        self.assertEqual(
            wait["blocking"]["nodes"][0]["conflict_paths"][0]["overlap"],
            "src/codex_workbench",
        )
        original_wait_cursor = wait["event_cursor"]
        self.assertIsNone(reopened.claim_ready_node("lifecycle-repeat", self.epoch))
        self.assertEqual(reopened.pending_node_admission_waits()[0]["event_cursor"], original_wait_cursor)

        # Only the blocking allocation settles; the pending lifecycle plan has
        # no result, worktree, or state mutation before the coordinator wakes.
        self.store.settle_claimed(blocker, NodeResult("succeeded", "quota repair complete"))
        candidate = reopened.claim_ready_node("lifecycle-resumed", self.epoch)
        self.assertEqual((candidate["task_id"], candidate["node_id"]), (candidate_task, "plan"))
        candidate_snapshot = next(
            node for node in reopened.get_task(candidate_task)["nodes"] if node["node_id"] == "plan"
        )
        self.assertEqual((candidate_snapshot["state"], candidate_snapshot["attempt"]), ("running", 1))
        self.assertIsNone(candidate_snapshot["result"])
        self.assertIsNone(candidate_snapshot["admission_wait"])
        event_types = [
            event["event_type"]
            for event in reopened.read_events(task_id=candidate_task)
            if event["event_type"] in {"node.admission_waiting", "node.admission_resumed", "node.started"}
        ]
        self.assertEqual(event_types.count("node.admission_waiting"), 1)
        self.assertEqual(event_types.count("node.admission_resumed"), 1)
        self.assertEqual(event_types.count("node.started"), 1)

    def test_private_path_receipt_without_enforced_isolation_still_waits(self) -> None:
        task_id = "isolated-scope"
        reader = NodeSpec(
            "reader",
            task_id,
            "reader",
            "codex",
            "gpt-5.6-luna",
            "read only",
            read_scopes=("src/shared",),
            ordinal=1,
        )
        writer = NodeSpec(
            "writer",
            task_id,
            "writer",
            "codex",
            "gpt-5.6-luna",
            "write isolated",
            write_scopes=("src/shared",),
            ordinal=2,
        )
        self._task(task_id, [reader, writer])
        self.store.queue_task(task_id)
        claimed_reader = self.store.claim_ready_node("reader-worker", self.epoch)
        self.assertEqual(claimed_reader["node_id"], "reader")
        manager = WorktreeManager(self.root / "worktrees")
        self.store.assign_worktree(
            task_id,
            "reader",
            str(manager.worktree_path(task_id, "reader", claimed_reader["attempt"])),
            attempt=claimed_reader["attempt"],
            coordinator_epoch=claimed_reader["coordinator_epoch"],
            lease_epoch=claimed_reader["lease_epoch"],
        )
        claimed_writer = self.store.claim_ready_node("writer-worker", self.epoch)
        self.assertIsNone(claimed_writer)
        writer_snapshot = next(node for node in self.store.get_task(task_id)["nodes"] if node["node_id"] == "writer")
        self.assertIsNotNone(writer_snapshot["admission_wait"])

    def test_restart_reconciles_durable_stage_intent_without_replaying_adapter_execution(self) -> None:
        task_id = "dispatch-restart"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        objective = self._claim_objective(
            self.store.create_delivery_objective(
                task_id, "create-dispatch-restart", self._objective_request()
            )
        )
        dispatch = self.store.begin_delivery_stage_dispatch(
            objective["objective_id"],
            stage="plan",
            attempt=1,
            expected_revision=objective["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=objective["lease"]["lease_epoch"],
            adapter_name="fixture-crashed-before-receipt",
        )
        self.assertTrue(dispatch["new"])
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE delivery_objectives SET lease_expires_at = ? WHERE objective_id = ?",
                (now_iso(), objective["objective_id"]),
            )
        adapter = FixtureDeliveryStageAdapter(
            {
                "plan": DeliveryStageOutcome(
                    receipt_id="reconciled-plan",
                    receipt={"fixture": "reconciled"},
                    evidence_fingerprint="reconciled-plan-evidence",
                    identities={"source": "source-fixture"},
                )
            }
        )
        reconciler = DeliveryLifecycleReconciler(
            self.store,
            owner_id="restart-owner",
            coordinator_epoch=self.epoch,
            adapter=adapter,
        )
        results = reconciler.reconcile_once()
        self.assertEqual(results[0]["objective"]["stage"], "implement")
        self.assertEqual([call[0] for call in adapter.calls], ["reconcile"])
        stored = self.store.get_delivery_objective(objective["objective_id"])
        self.assertEqual(stored["stage_dispatches"][0]["state"], "settled")

    def test_safe_deployment_wait_wakes_once_then_completes_after_worker_drain(self) -> None:
        task_id = "safe-rollout"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        accepted = self._accept_task(task_id)
        objective = self._advance_to_stage(
            self._claim_objective(
                self.store.create_delivery_objective(
                    task_id, "create-safe-rollout", self._objective_request()
                )
            ),
            "deploy",
        )
        drain_task = "active-same-repository-worker"
        self._task(
            drain_task,
            [
                NodeSpec(
                    "drain",
                    drain_task,
                    "drain",
                    "fixture",
                    "fixture",
                    "work",
                    write_scopes=("src",),
                    ordinal=1,
                )
            ],
        )
        self.store.queue_task(drain_task)
        draining = self.store.claim_ready_node("drain-worker", self.epoch)
        self.assertEqual((draining["task_id"], draining["node_id"]), (drain_task, "drain"))
        waiting = self.store.defer_delivery_deployment_safe_point(
            objective["objective_id"],
            expected_revision=objective["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=objective["lease"]["lease_epoch"],
            blockers=self.store.delivery_deployment_blockers(objective["objective_id"]),
        )
        self.assertEqual(waiting["next_action"]["action"], "wait_for_active_workers_to_drain")
        self.assertEqual(waiting["wait_reason"]["blockers"][0]["node_id"], "drain")
        self.assertEqual(self.store.reconcile_delivery_safe_point_waits(), 0)
        self.assertIsNone(
            self.store.claim_delivery_objective(
                waiting["objective_id"],
                "fixture-owner",
                self.epoch,
                expected_revision=waiting["state_revision"],
            )
        )
        waiting_events = [
            event
            for event in self.store.read_events(task_id=task_id)
            if event["event_type"] == "delivery_objective.deployment_safe_point_waiting"
        ]
        self.assertEqual(len(waiting_events), 1)
        self.store.settle_claimed(draining, NodeResult("succeeded", "worker drained"))
        self.assertEqual(self.store.reconcile_delivery_safe_point_waits(), 1)
        self.assertEqual(self.store.reconcile_delivery_safe_point_waits(), 0)
        self.store.grant_delivery_authorization(
            task_id,
            "safe-rollout-deploy-grant",
            scope={"deployment": {"target": "fixture"}},
            authority={"actor": "fixture-user", "reason": "safe fixture rollout"},
            objective_id=objective["objective_id"],
            expected_task_revision=accepted["state_revision"],
        )
        adapter = FixtureDeliveryStageAdapter(
            {
                "deploy": DeliveryStageOutcome(
                    receipt_id="safe-deploy",
                    receipt={"fixture": "deploy"},
                    evidence_fingerprint="safe-deploy-evidence",
                    identities={"deploy": "deploy-fixture"},
                ),
                "live-verify": DeliveryStageOutcome(
                    receipt_id="safe-live",
                    receipt=self._live_receipt(),
                    evidence_fingerprint="safe-live-evidence",
                    identities={"runtime": "runtime-fixture"},
                ),
            }
        )
        reconciler = DeliveryLifecycleReconciler(
            self.store,
            owner_id="fixture-owner",
            coordinator_epoch=self.epoch,
            adapter=adapter,
        )
        reconciler.reconcile_once()
        complete = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(complete["state"], "complete")
        self.assertEqual([call[1] for call in adapter.calls], ["deploy", "live-verify"])
        ready_events = [
            event
            for event in self.store.read_events(task_id=task_id)
            if event["event_type"] == "delivery_objective.deployment_safe_point_ready"
        ]
        self.assertEqual(len(ready_events), 1)

    def test_missing_authorization_never_dispatches_and_grant_resumes_integrate_once(self) -> None:
        task_id = "authorization-gated-lifecycle"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        accepted = self._accept_task(task_id)
        objective = self._advance_to_stage(
            self._claim_objective(
                self.store.create_delivery_objective(
                    task_id, "create-authorization-gated", self._objective_request()
                )
            ),
            "integrate",
        )
        adapter = FixtureDeliveryStageAdapter(
            {
                "integrate": DeliveryStageOutcome(
                    receipt_id="authorized-integrate",
                    receipt={"fixture": "integrate"},
                    evidence_fingerprint="authorized-integrate-evidence",
                    identities={"build": "build-fixture"},
                )
            }
        )
        reconciler = DeliveryLifecycleReconciler(
            self.store,
            owner_id="fixture-owner",
            coordinator_epoch=self.epoch,
            adapter=adapter,
        )
        denied = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(denied["state"], "needs_decision")
        self.assertEqual(adapter.calls, [])
        resumed = self.store.grant_delivery_authorization(
            task_id,
            "authorization-gated-grant",
            scope={"remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
            authority={"actor": "fixture-user", "reason": "explicit GitHub delivery"},
            objective_id=objective["objective_id"],
            expected_task_revision=accepted["state_revision"],
        )["objective"]
        assert resumed is not None
        self.assertEqual((resumed["state"], resumed["stage"]), ("active", "integrate"))
        recorded = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(recorded["stage"], "ci")
        self.assertEqual([call[0] for call in adapter.calls], ["execute"])

    def test_preauthorized_deploy_failure_persists_verified_rollback_without_false_completion(self) -> None:
        task_id = "rollback-lifecycle"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        accepted = self._accept_task(task_id)
        objective = self._advance_to_stage(
            self._claim_objective(
                self.store.create_delivery_objective(
                    task_id,
                    "create-rollback-lifecycle",
                    self._objective_request(rollback=True),
                )
            ),
            "deploy",
        )
        self.store.grant_delivery_authorization(
            task_id,
            "rollback-lifecycle-deploy-grant",
            scope={"deployment": {"target": "fixture"}},
            authority={"actor": "fixture-user", "reason": "preauthorized reversible rollout"},
            objective_id=objective["objective_id"],
            expected_task_revision=accepted["state_revision"],
        )
        adapter = FixtureDeliveryStageAdapter(
            {
                "deploy": DeliveryStageOutcome(
                    receipt_id="failed-deploy",
                    status="failed",
                    receipt={"fixture": "deploy-failed"},
                    failure={"kind": "verification-failure", "detail": "fixture deploy failed"},
                    retry_eligible=True,
                )
            },
            rollback_outcomes={"deploy": {"verified": True, "rollback_id": "fixture-rollback"}},
        )
        reconciler = DeliveryLifecycleReconciler(
            self.store,
            owner_id="fixture-owner",
            coordinator_epoch=self.epoch,
            adapter=adapter,
        )
        waiting = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual((waiting["state"], waiting["stage"]), ("waiting", "deploy"))
        self.assertNotEqual(waiting["state"], "complete")
        stored = self.store.get_delivery_objective(objective["objective_id"])
        self.assertEqual(stored["stage_dispatches"][-1]["rollback"]["state"], "settled")
        self.assertTrue(stored["stage_dispatches"][-1]["rollback"]["receipt"]["verified"])
        self.assertEqual(adapter.rollback_calls, [("rollback", "deploy", 1, stored["stage_dispatches"][-1]["dispatch_id"])])

    def test_scope_limited_authorization_overlays_accepted_contract_without_duplicate_task(self) -> None:
        task_id = "authorization-resume"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
            external_write=False,
        )
        accepted = self._accept_task(task_id)
        objective = self._claim_objective(
            self.store.create_delivery_objective(
                task_id, "create-objective-authorization", self._objective_request()
            )
        )
        denied = self.store.record_delivery_stage_receipt(
            objective["objective_id"],
            "permission-denied",
            stage="plan",
            attempt=1,
            expected_revision=objective["state_revision"],
            coordinator_epoch=self.epoch,
            lease_epoch=objective["lease"]["lease_epoch"],
            status="denied",
            receipt={"fixture": "denied"},
            failure={"kind": "permission-denied", "detail": "coordinator delivery receipt missing"},
        )["objective"]
        self.assertEqual(denied["state"], "needs_decision")
        with self.assertRaisesRegex(StateConflictError, "does not authorize"):
            self.store.begin_delivery(
                task_id,
                "delivery-denied",
                {"task_id": task_id, "command_id": "delivery-denied", "remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
            )
        grant = self.store.grant_delivery_authorization(
            task_id,
            "grant-delivery",
            scope={"remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
            authority={"actor": "fixture-user", "reason": "explicit delivery authorization"},
            objective_id=objective["objective_id"],
            expected_task_revision=accepted["state_revision"],
        )
        resumed = grant["objective"]
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual((resumed["state"], resumed["stage"], resumed["stage_attempt"]), ("active", "plan", 1))
        delivery = self.store.begin_delivery(
            task_id,
            "delivery-authorized",
            {"task_id": task_id, "command_id": "delivery-authorized", "remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
        )
        self.assertEqual(delivery["details"]["delivery_authorization_id"], "grant-delivery")
        task = self.store.get_task(task_id)
        self.assertEqual(task["state"], "accepted")
        self.assertEqual(len(task["nodes"]), 2)
        self.store.deny_delivery_authorization(
            task_id,
            "deny-delivery",
            scope={"remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
            authority={"actor": "fixture-user", "reason": "explicit denial regression"},
            objective_id=objective["objective_id"],
            expected_task_revision=accepted["state_revision"],
        )
        with self.assertRaisesRegex(StateConflictError, "does not authorize"):
            self.store.begin_delivery(
                task_id,
                "delivery-denied-after-grant",
                {
                    "task_id": task_id,
                    "command_id": "delivery-denied-after-grant",
                    "remote": "origin",
                    "base_branch": "main",
                    "merge": False,
                    "release_tag": None,
                },
            )

    def test_authenticated_http_authorization_receipt_uses_the_existing_store_api(self) -> None:
        task_id = "authorization-http"
        self._task(
            task_id,
            [NodeSpec("work", task_id, "work", "fixture", "fixture", "work", ordinal=1)],
        )
        accepted = self._accept_task(task_id)
        objective = self.store.create_delivery_objective(
            task_id,
            "create-objective-http",
            self._objective_request(),
        )
        body = {
            "authorization_id": "http-grant",
            "scope": {"remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
            "authority": {"actor": "fixture-user", "reason": "authenticated local API"},
            "objective_id": objective["objective_id"],
            "expected_revision": accepted["state_revision"],
        }
        handler = object.__new__(WorkbenchHandler)
        handler.server = SimpleNamespace(store=self.store)
        handler.path = f"/api/tasks/{task_id}/delivery-authorization"
        handler._host_allowed = lambda: True
        handler._authenticated = lambda: True
        handler._read_body = lambda: json.dumps(body).encode()
        response: dict[str, object] = {}
        handler._json = lambda payload, *_args, **_kwargs: response.update(payload)
        handler.do_POST()
        self.assertTrue(response["ok"])
        authorization = response["authorization"]
        assert isinstance(authorization, dict)
        self.assertEqual(authorization["authorization_id"], "http-grant")

        objective_handler = object.__new__(WorkbenchHandler)
        objective_handler.server = SimpleNamespace(store=self.store)
        objective_handler.path = f"/api/tasks/{task_id}/delivery-objective"
        objective_handler._host_allowed = lambda: True
        exposed: dict[str, object] = {}
        objective_handler._json = lambda payload, *_args, **_kwargs: exposed.update(payload)
        objective_handler.do_GET()
        self.assertEqual(exposed["objective_id"], objective["objective_id"])
        authorizations = exposed["delivery_authorizations"]
        assert isinstance(authorizations, list)
        self.assertEqual(authorizations[0]["authorization_id"], "http-grant")


if __name__ == "__main__":
    unittest.main()
