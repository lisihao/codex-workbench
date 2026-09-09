"""Pending observations retain one durable dispatch without spending retry budget."""

from datetime import UTC, datetime, timedelta
import unittest
import time
from unittest.mock import patch

from codex_workbench.delivery_lifecycle import DeliveryLifecycleReconciler, DeliveryStageOutcome, FixtureDeliveryStageAdapter, required_completion_identities
from codex_workbench.model import NodeSpec
from codex_workbench.store import StateConflictError, WorkbenchStore
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
        self.assertEqual(result["objective"]["wait_reason"]["kind"], "unknown-effects")
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

    def test_late_wait_cannot_overwrite_newer_owner(self):
        claimed = self.fixture._claim_objective(self.objective)
        dispatch = self.store.begin_delivery_stage_dispatch(claimed["objective_id"], stage="plan", attempt=1, expected_revision=claimed["state_revision"], coordinator_epoch=self.fixture.epoch, lease_epoch=claimed["lease"]["lease_epoch"], adapter_name="fixture")
        arguments = dict(expected_revision=claimed["state_revision"], coordinator_epoch=self.fixture.epoch, lease_epoch=claimed["lease"]["lease_epoch"], reason=dict(self.wait.failure), next_wakeup_at=None, dispatch_id=dispatch["dispatch_id"])
        self.store.defer_delivery_observation(claimed["objective_id"], **arguments)
        with self.assertRaises(StateConflictError):
            self.store.defer_delivery_observation(claimed["objective_id"], **arguments)
