"""Program-driven recovery fixtures; no chat, provider or subscription calls."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, UTC
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_hash
from codex_workbench.node_recovery import NodeRecoveryReconciler
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.store import StateConflictError, WorkbenchStore


class FixtureActions:
    def __init__(self, store: WorkbenchStore):
        self.store = store
        self.calls: list[str] = []
        self.receipts: dict[str, dict] = {}
        self.lose_receipt = False
        self.fail_readiness = False
        self.crash = False

    def prepare(self, observation, action, request_id, **kwargs):
        return {
            "action": action, "request_id": request_id, "stage_key": action,
            **{key: observation[key] for key in ("task_id", "node_id", "task_revision", "node_attempt")},
        }

    def execute(self, plan):
        self.calls.append(plan["action"])
        action = plan["action"]
        patch = {}
        ok = True
        if action == "materialize_dependencies":
            patch = {"dependencies_ready": True, "readiness_ready": False}
        elif action == "observe_readiness":
            ok = not self.fail_readiness
            patch = {"readiness_ready": ok}
        elif action in {"resume_node", "source_only_recovery"}:
            self.store.retry_blocked_node(
                plan["task_id"], plan["node_id"], expected_revision=plan["task_revision"],
                expected_attempt=plan["node_attempt"], reason="fixture executor never ran",
                confirm_no_side_effects=True,
            )
            patch = {"recovery_resumed": True}
        elif action == "narrow_validation":
            patch = {"last_validation_profile": "dsh-b-ipc-v1", "validated_source_delta": "a" * 64}
        receipt = {
            "ok": ok, "known_effects": True, "stage_succeeded": ok,
            "observation_patch": patch, "evidence_refs": [],
        }
        self.receipts[plan["request_id"]] = receipt
        if self.crash:
            raise SystemExit("fixture authority stopped after effect")
        if self.lose_receipt:
            raise OSError("fixture lost response after one effect")
        return receipt

    def reconcile(self, plan):
        return self.receipts.get(plan["request_id"])


class NodeRecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-recovery-loop-")
        self.addCleanup(self.temp.cleanup)
        self.store = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("recovery-loop", "fixture")
        self.task_id = "loop-task"
        self.contract = TaskContract(
            self.task_id, self.temp.name, "fixture", "continue original implementation",
            allowed_scope=("src",), retry_limit=0,
        )
        self.store.create_task(self.contract, [
            NodeSpec("B", self.task_id, "implement", "fixture", "fixture", "blocked", write_scopes=("src/b",)),
            NodeSpec("D", self.task_id, "implement", "fixture", "fixture", "depends", depends_on=("B",), write_scopes=("src/d",)),
            NodeSpec("I", self.task_id, "implement", "fixture", "fixture", "independent", write_scopes=("src/i",)),
            NodeSpec("V", self.task_id, "verify", "fixture", "fixture", "accepted", depends_on=("B", "D", "I"), verifier=True),
        ], "create-loop")
        self.store.queue_task(self.task_id)
        claimed = self.store.claim_ready_node("fixture-b", self.epoch)
        self.assertEqual(claimed["node_id"], "B")
        self.store.settle_claimed(claimed, NodeResult("blocked", "fixture readiness unavailable"))
        self.recovery = NodeRecoveryStore(self.store)
        self.policy = RecoveryPolicy(enabled=True, allowed_actions=("materialize_dependencies", "observe_readiness", "resume_node"))
        self.recovery.configure_policy(
            self.task_id, self.policy, expected_task_revision=self.task()["state_revision"], actor="fixture",
        )
        self.actions = FixtureActions(self.store)
        self.category = "dependency"
        self.loop = self.make_loop()

    def task(self):
        return self.store.get_task(self.task_id)

    def observe(self, store, task_id, node_id, *, source_event_cursor=0):
        task = store.get_task(task_id)
        node = next(node for node in task["nodes"] if node["node_id"] == node_id)
        return {
            "task_id": task_id, "node_id": node_id, "node_attempt": node["attempt"],
            "task_revision": task["state_revision"], "task_state": task["state"], "node_state": node["state"],
            "category": self.category, "origin": "environment", "phase": "pre_execution",
            "implementation_ready": self.category == "validation_failure",
            "only_missing_dependency": True, "dependencies_ready": False,
            "source_event_cursor": source_event_cursor, "evidence_refs": [],
            "failure_fingerprint": canonical_hash({"node": node_id, "attempt": node["attempt"]}),
        }

    def make_loop(self):
        return NodeRecoveryReconciler(
            self.store, coordinator_epoch=self.epoch, observer=self.observe,
            adapters={action: self.actions for action in self.policy.allowed_actions},
        )

    def test_dependency_recovery_continues_original_node_without_worker_reimplementation(self):
        for _ in range(3):
            self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["materialize_dependencies", "observe_readiness", "resume_node"])
        task = self.task()
        original = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual((original["state"], original["attempt"]), ("pending", 1))
        self.assertEqual(task["state"], "queued")
        self.assertEqual(self.recovery.list_summary(task_id=self.task_id)[0]["state"], "resolved")

    def test_blocked_node_does_not_starve_independent_node_or_release_dependent(self):
        independent = self.store.claim_ready_node("fixture-i", self.epoch)
        self.assertEqual(independent["node_id"], "I")
        self.store.settle_claimed(independent, NodeResult("succeeded", "independent complete"))
        self.assertIsNone(self.store.claim_ready_node("fixture-d", self.epoch))
        self.assertEqual(self.task()["state"], "blocked")

    def test_lost_write_response_queries_original_receipt_without_duplicate_effect(self):
        self.actions.lose_receipt = True
        self.loop.reconcile_once()
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["materialize_dependencies", "observe_readiness"])
        self.assertEqual(self.recovery.metrics(task_id=self.task_id)["unknown_action_count"], 0)

    def test_pause_and_cancel_override_program_recovery(self):
        self.store.transition_task(self.task_id, "paused", expected_revision=self.task()["state_revision"])
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, [])
        self.store.transition_task(self.task_id, "cancelled", expected_revision=self.task()["state_revision"])
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, [])

    def test_restart_settles_original_receipt_without_replaying_completed_action(self):
        self.actions.crash = True
        with self.assertRaises(SystemExit):
            self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["materialize_dependencies"])
        self.epoch = self.store.activate_coordinator("recovery-restarted", "fixture")
        self.recovery.recover_interrupted()
        self.actions.crash = False
        self.loop = self.make_loop()
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["materialize_dependencies", "observe_readiness"])
        self.assertEqual(self.recovery.metrics(task_id=self.task_id)["unknown_action_count"], 0)
    def test_disabled_policy_and_unknown_evidence_do_not_call_a_model(self):
        self.recovery.configure_policy(
            self.task_id, RecoveryPolicy(), expected_task_revision=self.task()["state_revision"], actor="fixture",
        )
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, [])
        self.assertEqual(self.recovery.metrics(task_id=self.task_id)["total_action_count"], 0)

    def test_persistent_same_fault_has_bounded_actions_and_one_notification(self):
        self.actions.fail_readiness = True
        stamp = datetime.now(UTC)
        self.loop.monotonic = lambda: stamp.timestamp()
        for _ in range(10):
            with patch("codex_workbench.node_recovery.now_iso", return_value=stamp.isoformat()), patch(
                "codex_workbench.node_recovery_store.now_iso", return_value=stamp.isoformat()
            ):
                self.loop.reconcile_once()
            stamp += timedelta(seconds=31)
        self.assertEqual(self.actions.calls.count("materialize_dependencies"), 1)
        self.assertEqual(self.actions.calls.count("observe_readiness"), 3)
        notices = [event for event in self.store.read_events(task_id=self.task_id)
                   if event["event_type"] == "node_recovery.needs_action"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["payload"]["reason_kind"], "budget_exhausted")

    def test_restart_reuses_passed_validation_receipt_instead_of_running_it_again(self):
        self.category = "validation_failure"
        self.policy = RecoveryPolicy(
            enabled=True, allowed_actions=("narrow_validation", "source_only_recovery"),
            validation_profiles=("dsh-b-ipc-v1",),
        )
        self.recovery.configure_policy(self.task_id, self.policy,
            expected_task_revision=self.task()["state_revision"], actor="fixture")
        self.loop = self.make_loop()
        self.actions.crash = True
        with self.assertRaises(SystemExit):
            self.loop.reconcile_once()
        self.epoch = self.store.activate_coordinator("validation-restarted", "fixture")
        self.recovery.recover_interrupted()
        self.actions.crash = False
        self.loop = self.make_loop()
        self.loop.reconcile_once()
        self.assertEqual(self.actions.calls, ["narrow_validation", "source_only_recovery"])
        self.assertEqual(self.recovery.list_summary(task_id=self.task_id)[0]["state"], "resolved")

    def test_check_reuse_is_invalidated_by_source_or_runtime_drift(self):
        policy = RecoveryPolicy(enabled=True, allowed_actions=("narrow_validation",),
            validation_profiles=("dsh-b-ipc-v1", "dsh-b-pairing-check-v1"))
        episode = {"policy": policy.to_dict(), "observation": {
            "validated_profiles": ["dsh-b-ipc-v1"], "validated_source_delta": "a" * 64,
            "validated_install_manifest": "b" * 64, "validated_runtime_fingerprint": "c" * 64,
        }}
        preview = {"source_delta_sha256": "a" * 64, "install_manifest_sha256": "b" * 64,
                   "runtime_fingerprint": "c" * 64}
        receipt = {"ok": True, "known_effects": True, "observation_patch": {}}
        for changed_field in (None, "source_delta_sha256", "runtime_fingerprint", "install_manifest_sha256"):
            current = dict(preview)
            if changed_field is not None:
                current[changed_field] = "d" * 64
            observed, _ = self.loop._receipt_progress(episode, {
                "action": "narrow_validation", "fresh_preview": current,
                "arguments": {"check_id": "dsh-b-pairing-check-v1"},
            }, receipt)
            self.assertEqual(observed["validation_succeeded"], changed_field is None)
            self.assertEqual("dsh-b-ipc-v1" in observed["validated_profiles"], changed_field is None)


if __name__ == "__main__":
    unittest.main()
