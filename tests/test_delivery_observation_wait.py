"""Pending observations retain one durable dispatch without spending retry budget."""

from datetime import UTC, datetime, timedelta
import unittest
import time
from unittest.mock import patch

from codex_workbench.delivery_lifecycle import (
    DeliveryLifecycleReconciler,
    DeliveryStageOutcome,
    FixtureDeliveryStageAdapter,
    normalize_wait_reason,
    required_completion_identities,
)
from codex_workbench.model import NodeResult, NodeSpec
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore
from tests import test_delivery_lifecycle as fixtures


class DeliveryObservationWaitTests(unittest.TestCase):
    def test_expired_authorization_preserves_started_intent_as_unknown_effect(self):
        accepted = self.fixture._accept_task(self.task_id)
        current = self.fixture._advance_to_stage(self.fixture._claim_objective(self.objective), "integrate")
        timestamp = datetime.now(UTC)
        self.store.grant_delivery_authorization(self.task_id, "expiring-grant",
            scope={"remote": "origin", "base_branch": "main", "merge": False, "release_tag": None},
            authority={"actor": "fixture", "reason": "explicit grant"}, objective_id=current["objective_id"],
            expected_task_revision=accepted["state_revision"], expires_at=(timestamp + timedelta(seconds=10)).isoformat())
        dispatch = self.store.begin_delivery_stage_dispatch(current["objective_id"], stage="integrate", attempt=current["stage_attempt"],
            expected_revision=current["state_revision"], coordinator_epoch=self.fixture.epoch,
            lease_epoch=current["lease"]["lease_epoch"], adapter_name="fixture")
        adapter = FixtureDeliveryStageAdapter({})
        reconciler = DeliveryLifecycleReconciler(self.store, owner_id="fixture-owner", coordinator_epoch=self.fixture.epoch, adapter=adapter)
        with patch("codex_workbench.store.now_iso", return_value=(timestamp + timedelta(seconds=20)).isoformat()):
            result = reconciler.reconcile_once()[0]
        self.assertEqual(result["receipt"]["state"], "indeterminate")
        wait = result["objective"]["wait_reason"]
        self.assertEqual(wait["kind"], "unknown-effects")
        self.assertEqual(wait["wait_kind"], "indeterminate")
        self.assertEqual(wait["release_condition"], "authoritative_reconciliation_is_complete")
        self.assertEqual(wait["responsible_owner"], "fixture-owner")
        self.assertIsNone(wait["next_recheck_at"])
        recorded = self.store.get_delivery_stage_dispatch(current["objective_id"], "integrate", current["stage_attempt"])
        self.assertEqual(recorded["dispatch_id"], dispatch["dispatch_id"])
        self.assertEqual(recorded["receipt_id"], result["receipt"]["receipt_id"])
        self.assertEqual(adapter.calls, [])

    def test_inflight_adapter_renews_lease_and_records_with_latest_revision(self):
        class SlowAdapter:
            def execute_stage(adapter_self, context):
                time.sleep(2.1)
                return DeliveryStageOutcome(receipt_id="slow-plan", evidence_fingerprint="slow-evidence")
        reconciler = DeliveryLifecycleReconciler(self.store, owner_id="slow", coordinator_epoch=self.fixture.epoch,
                                                 adapter=SlowAdapter(), lease_seconds=2, heartbeat_seconds=0.1)
        current = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(current["stage"], "implement")
        self.assertTrue(any(event["event_type"] == "delivery_objective.lease_renewed"
                            for event in self.store.read_events(task_id=self.task_id)))

    def test_lost_inflight_lease_leaves_dispatch_unsettled_for_reconciliation(self):
        store = self.store
        class FencedAdapter:
            def execute_stage(adapter_self, context):
                store.activate_coordinator("new-authority", "fixture-machine")
                time.sleep(0.15)
                return DeliveryStageOutcome(receipt_id="late-plan", evidence_fingerprint="late-evidence")
        reconciler = DeliveryLifecycleReconciler(self.store, owner_id="old", coordinator_epoch=self.fixture.epoch,
                                                 adapter=FencedAdapter(), lease_seconds=2, heartbeat_seconds=0.05)
        with self.assertRaises(StateConflictError):
            reconciler.reconcile_once()
        current = self.store.get_delivery_objective(self.objective["objective_id"])
        self.assertEqual(current["stage_receipts"], [])
        self.assertEqual(current["stage_dispatches"][0]["state"], "started")

    def test_github_runtime_identities_follow_explicit_endpoint_requirements(self):
        request = {"requested_endpoints": {"github": {"base_branch": "main"}}}
        self.assertEqual(required_completion_identities(request), ("source", "build"))
        request["requested_endpoints"]["github"]["required_runtime_identities"] = ["node", "pnpm"]
        self.assertEqual(required_completion_identities(request), ("node", "pnpm", "source", "build"))
        request["requested_endpoints"]["github"]["required_runtime_identities"] = [{}]
        with self.assertRaises(ValueError):
            required_completion_identities(request)

    def setUp(self):
        self.fixture = fixtures.DeliveryLifecyclePersistenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.task_id = "pending-observation"
        self.fixture._task(self.task_id, [NodeSpec("work", self.task_id, "work", "fixture", "fixture", "work", ordinal=1)])
        self.objective = self.store.create_delivery_objective(self.task_id, "create", self.fixture._objective_request(attempt_limit=1))
        self.wait = DeliveryStageOutcome(
            receipt_id="observation", status="deferred",
            failure={"kind": "execution-environment", "detail": "task is still planning"},
        )
        self.adapter = FixtureDeliveryStageAdapter({"plan": self.wait})
        self.reconciler = DeliveryLifecycleReconciler(self.store, owner_id="observer", coordinator_epoch=self.fixture.epoch, adapter=self.adapter)

    def test_repeated_wait_and_restart_reconcile_one_intent_without_consuming_budget(self):
        timestamp = datetime.now(UTC)
        for index in range(5):
            with patch("codex_workbench.store.now_iso", return_value=(timestamp + timedelta(seconds=index * 10)).isoformat()):
                result = self.reconciler.reconcile_once()[0]
                self.assertEqual(result["status"], "deferred")
                current = result["objective"]
                self.assertEqual(current["state"], "waiting")
                self.assertEqual(current["stage_attempt"], 1)
                self.assertEqual(current["budget"]["attempts_used"], 0)
                self.assertEqual(current["stage_receipts"], [])
                self.assertEqual(len(current["stage_dispatches"]), 1)
                self.assertEqual(current["stage_dispatches"][0]["state"], "started")
                wait = current["wait_reason"]
                self.assertEqual(wait["wait_kind"], "environment")
                self.assertEqual(wait["release_condition"], "execution_environment_is_available")
                self.assertEqual(wait["responsible_owner"], "observer")
                self.assertEqual(wait["next_recheck_at"], current["next_wakeup_at"])
        events = self.store.read_events(task_id=self.task_id)
        self.assertEqual(sum(event["event_type"] == "delivery_objective.observation_waiting" for event in events), 1)
        reopened = WorkbenchStore(self.store.path)
        reopened.initialize()
        epoch = reopened.activate_coordinator("restarted-observer", "fixture-machine")
        adapter = FixtureDeliveryStageAdapter({"plan": DeliveryStageOutcome(receipt_id="plan-finished", evidence_fingerprint="actual-plan-receipt", identities={"source": "fixture-source"})})
        reconciler = DeliveryLifecycleReconciler(reopened, owner_id="restarted", coordinator_epoch=epoch, adapter=adapter)
        with patch("codex_workbench.store.now_iso", return_value=(timestamp + timedelta(seconds=60)).isoformat()):
            current = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(current["stage"], "implement")
        self.assertEqual([call[0] for call in self.adapter.calls], ["execute", "reconcile", "reconcile", "reconcile", "reconcile"])
        self.assertEqual([call[0] for call in adapter.calls], ["reconcile"])

    def test_reassigned_wait_owner_is_a_material_observation_change(self):
        timestamp = datetime.now(UTC)
        with patch("codex_workbench.store.now_iso", return_value=timestamp.isoformat()):
            first = self.reconciler.reconcile_once()[0]["objective"]
        with patch(
            "codex_workbench.store.now_iso",
            return_value=(timestamp + timedelta(seconds=10)).isoformat(),
        ):
            replacement = self.store.claim_delivery_objective(
                first["objective_id"],
                "replacement-observer",
                self.fixture.epoch,
                expected_revision=first["state_revision"],
            )
            self.assertIsNotNone(replacement)
            assert replacement is not None
            second = self.store.defer_delivery_observation(
                replacement["objective_id"],
                expected_revision=replacement["state_revision"],
                coordinator_epoch=self.fixture.epoch,
                lease_epoch=replacement["lease"]["lease_epoch"],
                reason=dict(self.wait.failure),
                next_wakeup_at=None,
                dispatch_id=first["stage_dispatches"][0]["dispatch_id"],
            )
        self.assertEqual(second["budget"]["attempts_used"], 0)
        self.assertEqual(len(second["stage_dispatches"]), 1)
        self.assertEqual(second["wait_reason"]["responsible_owner"], "replacement-observer")
        events = self.store.read_events(task_id=self.task_id)
        self.assertEqual(
            sum(event["event_type"] == "delivery_objective.observation_waiting" for event in events),
            2,
        )

    def test_late_wait_cannot_overwrite_newer_owner(self):
        claimed = self.fixture._claim_objective(self.objective)
        dispatch = self.store.begin_delivery_stage_dispatch(claimed["objective_id"], stage="plan", attempt=1, expected_revision=claimed["state_revision"], coordinator_epoch=self.fixture.epoch, lease_epoch=claimed["lease"]["lease_epoch"], adapter_name="fixture")
        arguments = dict(expected_revision=claimed["state_revision"], coordinator_epoch=self.fixture.epoch, lease_epoch=claimed["lease"]["lease_epoch"], reason=dict(self.wait.failure), next_wakeup_at=None, dispatch_id=dispatch["dispatch_id"])
        self.store.defer_delivery_observation(claimed["objective_id"], **arguments)
        with self.assertRaises(StateConflictError):
            self.store.defer_delivery_observation(claimed["objective_id"], **arguments)

    def test_deployment_safe_point_wait_records_resource_owner_and_recheck(self):
        objective = self.fixture._advance_to_stage(
            self.fixture._claim_objective(self.objective), "deploy"
        )
        drain_task = "typed-safe-point-blocker"
        self.fixture._task(
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
        draining = self.store.claim_ready_node("drain-worker", self.fixture.epoch)
        self.assertIsNotNone(draining)
        assert draining is not None
        waiting = self.store.defer_delivery_deployment_safe_point(
            objective["objective_id"],
            expected_revision=objective["state_revision"],
            coordinator_epoch=self.fixture.epoch,
            lease_epoch=objective["lease"]["lease_epoch"],
            blockers=self.store.delivery_deployment_blockers(objective["objective_id"]),
        )
        wait = waiting["wait_reason"]
        self.assertEqual(wait["wait_kind"], "resource")
        self.assertEqual(wait["release_condition"], "active_workers_reach_safe_point")
        self.assertEqual(wait["responsible_owner"], "fixture-owner")
        self.assertEqual(wait["next_recheck_at"], waiting["next_wakeup_at"])
        self.store.settle_claimed(draining, NodeResult("succeeded", "fixture worker drained"))
        self.assertEqual(self.store.reconcile_delivery_safe_point_waits(), 1)
        ready = self.store.get_delivery_objective(waiting["objective_id"])
        self.assertTrue(ready["wait_reason"]["safe_point_ready"])
        self.assertEqual(ready["wait_reason"]["next_recheck_at"], ready["next_wakeup_at"])

    def test_explicit_typed_receipt_fields_participate_in_idempotency(self):
        claimed = self.fixture._claim_objective(self.objective)
        arguments = dict(
            stage="plan",
            attempt=1,
            expected_revision=claimed["state_revision"],
            coordinator_epoch=self.fixture.epoch,
            lease_epoch=claimed["lease"]["lease_epoch"],
            status="failed",
            receipt={"fixture": "explicit typed wait"},
            retry_eligible=False,
        )
        receipt_id = "explicit-typed-wait"
        failure = {
            "kind": "execution-environment",
            "detail": "fixture unavailable",
            "release_condition": "fixture_environment_is_restored",
        }
        first = self.store.record_delivery_stage_receipt(
            claimed["objective_id"], receipt_id, failure=failure, **arguments
        )
        self.assertFalse(first["idempotent"])
        self.assertEqual(
            first["objective"]["wait_reason"]["release_condition"],
            "fixture_environment_is_restored",
        )
        duplicate = self.store.record_delivery_stage_receipt(
            claimed["objective_id"], receipt_id, failure=failure, **arguments
        )
        self.assertTrue(duplicate["idempotent"])
        with self.assertRaises(CommandConflictError):
            self.store.record_delivery_stage_receipt(
                claimed["objective_id"],
                receipt_id,
                failure={**failure, "release_condition": "different_environment_condition"},
                **arguments,
            )

    def test_typed_wait_fields_reject_misclassification_and_false_claim_context(self):
        with self.assertRaisesRegex(ValueError, "wait_kind must match"):
            normalize_wait_reason(
                {
                    "kind": "execution-environment",
                    "detail": "fixture unavailable",
                    "wait_kind": "approval",
                }
            )
        with self.assertRaisesRegex(ValueError, "release_condition"):
            normalize_wait_reason(
                {
                    "kind": "execution-environment",
                    "detail": "fixture unavailable",
                    "release_condition": " ",
                }
            )
        with self.assertRaisesRegex(ValueError, "responsible_owner must match"):
            normalize_wait_reason(
                {
                    "kind": "execution-environment",
                    "detail": "fixture unavailable",
                    "responsible_owner": "unrelated-owner",
                },
                responsible_owner="fixture-owner",
                next_recheck_at="2026-09-12T12:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "next_recheck_at"):
            normalize_wait_reason(
                {"kind": "execution-environment", "detail": "fixture unavailable"},
                next_recheck_at="tomorrow",
            )
        self.assertEqual(
            normalize_wait_reason(
                {
                    "kind": "missing-essential-user-choice",
                    "detail": "fixture user choice is required",
                }
            )["wait_kind"],
            "approval",
        )
