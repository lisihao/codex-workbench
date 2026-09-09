"""Authority rollout intent and worker admission cannot pass each other."""

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch

from codex_workbench.delivery_lifecycle import DeliveryAdmissionBusy, DeliveryLifecycleReconciler, DeliveryStageOutcome, FixtureDeliveryStageAdapter
from codex_workbench.model import NodeSpec, TaskContract
from tests import test_delivery_lifecycle as fixtures


class DeliveryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeliveryLifecyclePersistenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.epoch = self.fixture.epoch
        self.task = "rollout-owner"
        self.fixture._task(self.task, [NodeSpec("work", self.task, "work", "fixture", "fixture", "work", ordinal=1)])
        accepted = self.fixture._accept_task(self.task)
        objective = self.store.create_delivery_objective(self.task, "create-rollout", self.fixture._objective_request())
        self.current = self.fixture._advance_to_stage(self.fixture._claim_objective(objective), "deploy")
        self.store.grant_delivery_authorization(self.task, "rollout-grant", scope={"deployment": {"target": "fixture"}},
            authority={"actor": "fixture", "reason": "explicit rollout"}, objective_id=objective["objective_id"],
            expected_task_revision=accepted["state_revision"])

    def queue_other(self):
        task_id = "other-repository"
        contract = TaskContract(task_id, str(self.fixture.root / "another-repository"), "b" * 40, "other work", allowed_scope=("src",))
        self.store.create_task(contract, [NodeSpec("worker", task_id, "worker", "fixture", "fixture", "work", ordinal=1),
            NodeSpec("verify", task_id, "verify", "fixture", "fixture", "verify", depends_on=("worker",), verifier=True, ordinal=2)], "create-other")
        self.store.queue_task(task_id)

    def begin(self):
        return self.store.begin_delivery_stage_dispatch(self.current["objective_id"], stage="deploy", attempt=self.current["stage_attempt"],
            expected_revision=self.current["state_revision"], coordinator_epoch=self.epoch,
            lease_epoch=self.current["lease"]["lease_epoch"], adapter_name="fixture")

    def test_worker_started_after_snapshot_prevents_atomic_deploy_dispatch(self):
        self.assertEqual(self.store.delivery_deployment_blockers(self.current["objective_id"]), [])
        self.queue_other()
        self.assertIsNotNone(self.store.claim_ready_node("racing-worker", self.epoch))
        with self.assertRaises(DeliveryAdmissionBusy):
            self.begin()
        self.assertIsNone(self.store.get_delivery_stage_dispatch(self.current["objective_id"], "deploy", 1))
        self.assertEqual(self.store.delivery_deployment_blockers(self.current["objective_id"])[0]["task_id"], "other-repository")

    def test_started_deploy_blocks_new_workers_and_planning(self):
        self.begin()
        self.queue_other()
        self.store.enqueue_planning_request("next-plan", "next-plan-task", {"objective": "next"})
        self.assertIsNone(self.store.claim_ready_node("late-worker", self.epoch))
        self.assertIsNone(self.store.claim_planning_request(self.epoch))
        self.assertEqual(self.store.health()["deployment_admission_gate"]["objective_id"], self.current["objective_id"])

    def test_deferred_helper_keeps_admission_closed_until_live_verification(self):
        wait = DeliveryStageOutcome(receipt_id="helper-pending", status="deferred",
                                   failure={"kind": "execution-environment", "detail": "helper running"})
        adapter = FixtureDeliveryStageAdapter({"deploy": wait})
        reconciler = DeliveryLifecycleReconciler(self.store, owner_id="fixture-owner", coordinator_epoch=self.epoch, adapter=adapter)
        first = reconciler.reconcile_once()[0]
        self.assertEqual(first["status"], "deferred")
        self.queue_other()
        self.assertIsNone(self.store.claim_ready_node("waiting-worker", self.epoch))
        timestamp = datetime.now(UTC) + timedelta(seconds=10)
        with patch("codex_workbench.store.now_iso", return_value=timestamp.isoformat()):
            self.assertEqual(reconciler.reconcile_once()[0]["status"], "deferred")
        adapter.outcomes["deploy"] = DeliveryStageOutcome(receipt_id="deployed", evidence_fingerprint="deploy-proof", identities={"deploy": "deployment"})
        with patch("codex_workbench.store.now_iso", return_value=(timestamp + timedelta(seconds=10)).isoformat()):
            live = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(live["stage"], "live-verify")
        self.assertIsNone(self.store.claim_ready_node("before-live-proof", self.epoch))
        adapter.outcomes["live-verify"] = DeliveryStageOutcome(receipt_id="live", evidence_fingerprint="live-proof", identities={"runtime": "runtime"},
            receipt={"health": {"healthz": True}, "functional": {"smoke": True}})
        with patch("codex_workbench.store.now_iso", return_value=(timestamp + timedelta(seconds=20)).isoformat()):
            complete = reconciler.reconcile_once()[0]["objective"]
        self.assertEqual(complete["state"], "complete")
        self.assertIsNone(self.store.delivery_admission_gate())
        self.assertIsNotNone(self.store.claim_ready_node("after-live-proof", self.epoch))
