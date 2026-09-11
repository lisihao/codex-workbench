"""The available adapter observes readiness without installing or requeueing."""
import json
import unittest

from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeResult
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_readiness import ReadinessNodeActions
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError
from tests.test_accepted_source_repair import AcceptedSourceRepairFixture


class ReadinessNodeActionTests(AcceptedSourceRepairFixture, unittest.TestCase):
    def test_readiness_receipt_keeps_original_node_and_source_unchanged(self):
        contract, verifier = self._create_task()
        worktree = self.worktrees.prepare(contract.repository, contract.base_sha,
            contract.task_id, "verify", int(verifier["attempt"]))
        self.store.assign_worktree(contract.task_id, "verify", str(worktree),
            attempt=verifier["attempt"], coordinator_epoch=self.epoch, lease_epoch=verifier["lease_epoch"])
        self.store.settle_claimed(verifier, NodeResult("blocked", "fixture environment wait", verdict="blocked"))
        task = self.store.get_task(contract.task_id)
        recovery = NodeRecoveryStore(self.store)
        recovery.configure_policy(contract.task_id, RecoveryPolicy(enabled=True),
            expected_task_revision=task["state_revision"], actor="fixture")
        config = WorkbenchConfig(self.store.path.parent)
        adapter = ReadinessNodeActions(config, self.store, readiness_request_factory=Coordinator._readiness_request)
        observation = {"task_id": contract.task_id, "node_id": "verify", "node_attempt": 1,
                       "task_revision": task["state_revision"]}
        plan = adapter.prepare(observation, "observe_readiness", "fixture-readiness")
        before = self.store.get_task(contract.task_id)
        receipt = adapter.execute(plan)
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.store.get_task(contract.task_id), before)
        document = json.loads(self.store.artifacts.verify(receipt["observation_patch"]["recovery_readiness_ref"]).read_text())
        self.assertEqual(document["kind"], "node-recovery-readiness/v1")
        self.assertEqual(document["node_attempt"], 1)
        self.assertTrue(document["report"]["ready"])
        self.assertIsNone(adapter.reconcile(plan))
        self.store.transition_task(contract.task_id, "paused", expected_revision=task["state_revision"])
        with self.assertRaises(StateConflictError):
            adapter.execute(plan)


if __name__ == "__main__":
    unittest.main()
