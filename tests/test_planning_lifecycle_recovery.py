"""Planner repair reuses one reservation and never spends an unbounded retry loop."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority_delivery import TaskDeliveryStageAdapter, BoundedVerificationIdentityObserver
from codex_workbench.config import WorkbenchConfig
from codex_workbench.delivery_lifecycle import DeliveryLifecycleReconciler
from codex_workbench.planner import PlannerError
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError, WorkbenchStore


class PlanningLifecycleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("planning", "fixture-machine")
        self.request = {"objective": "keep original goal", "queue": True, "retry_limit": 2}
        self.original = self.store.enqueue_planning_request("plan-command", "plan-task", self.request)
        self.first = self.store.claim_planning_request(self.epoch)
        self.store.fail_planning_request("plan-command", attempt=1, coordinator_epoch=self.epoch,
                                        error="PlannerError: parallel nodes have overlapping access")

    def retry(self, **kwargs):
        return self.store.retry_planning_request("plan-command", expected_attempt=1, max_attempts=3, reason="fix DAG", coordinator_epoch=self.epoch, **kwargs)

    def test_retry_preserves_request_identity_and_repeated_queue_is_idempotent(self):
        first = self.retry()
        second = self.retry()
        self.assertEqual(first, second)
        self.assertEqual(first["request_hash"], self.original["request_hash"])
        self.assertEqual(first["task_id"], "plan-task")
        self.assertEqual(first["request"], self.request)
        self.assertEqual(len([e for e in self.store.read_events(task_id="plan-task") if e["event_type"] == "planning_request.retry_queued"]), 1)
        claim = self.store.claim_planning_request(self.epoch)
        self.assertEqual(claim["attempt"], 2)
        self.assertIn("overlapping access", claim["previous_error"])
        with self.assertRaises(StateConflictError):
            self.retry()

    def test_request_retry_limit_cannot_be_overridden_by_objective_budget(self):
        self.retry()
        self.store.claim_planning_request(self.epoch)
        self.store.fail_planning_request("plan-command", attempt=2, coordinator_epoch=self.epoch, error="PlannerError: still invalid")
        with self.assertRaisesRegex(StateConflictError, "budget"):
            self.store.retry_planning_request("plan-command", expected_attempt=2, max_attempts=99, reason="bounded", coordinator_epoch=self.epoch)

    def test_stale_coordinator_cannot_requeue_a_model_attempt(self):
        self.store.activate_coordinator("replacement", "fixture-machine")
        with self.assertRaises(StateConflictError):
            self.retry()
        self.assertEqual(self.store.get_planning_request("plan-command")["state"], "failed")

    def test_coordinator_passes_recorded_feedback_without_rewriting_original_goal(self):
        self.retry()
        claim = self.store.claim_planning_request(self.epoch)
        coordinator = object.__new__(Coordinator)
        coordinator.store = self.store
        coordinator.config = SimpleNamespace()
        with patch("codex_workbench.service.compile_natural_language_request", side_effect=PlannerError("fixture second validation")) as compile_request:
            coordinator._execute_planning_request(claim)
        self.assertEqual(compile_request.call_args.kwargs["objective"], self.request["objective"])
        self.assertIn("overlapping access", compile_request.call_args.kwargs["planning_feedback"])
        current = self.store.get_planning_request("plan-command")
        self.assertEqual(current["request_hash"], self.original["request_hash"])
        self.assertEqual(current["state"], "failed")

    def test_lifecycle_automatically_requeues_validation_failure_on_same_reservation(self):
        objective = self.store.create_delivery_objective("plan-task", "objective-command", {
            "requested_endpoints": {"github": {"base_branch": "main"}},
            "scope": {"paths": ["src"]}, "authority": {"reason": "fixture"},
        })
        adapter = TaskDeliveryStageAdapter(self.store, BoundedVerificationIdentityObserver(WorkbenchConfig(self.root)))
        reconciler = DeliveryLifecycleReconciler(self.store, owner_id="planner-repair", coordinator_epoch=self.epoch, adapter=adapter)
        result = reconciler.reconcile_once()[0]
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.store.get_planning_request("plan-command")["state"], "pending")
        self.assertEqual(self.store.get_delivery_objective(objective["objective_id"])["stage"], "plan")
        with self.assertRaises(KeyError):
            self.store.get_task("plan-task")
