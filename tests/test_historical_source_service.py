"""Execution-boundary checks for historical-source preparation and control fencing."""
import unittest
from unittest.mock import patch

from codex_workbench.historical_accepted_source import historical_accepted_source
from codex_workbench.model import NodeResult
from codex_workbench.service import Coordinator
from tests.test_historical_accepted_source import HistoricalAcceptedSourceFixture


class HistoricalSourceServiceTests(HistoricalAcceptedSourceFixture, unittest.TestCase):
    def _authorized_claim(self):
        fixture = self._fixture()
        arguments = self._arguments(fixture)
        preview = historical_accepted_source(self.store, arguments)
        self._apply(arguments, preview)
        claimed = self.store.claim_ready_node("historical-consumer", self.epoch)
        self.assertIsNotNone(claimed)
        self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 3))
        return fixture, claimed

    def test_executor_receives_historical_patch_and_prepared_receipt(self):
        fixture, claimed = self._authorized_claim()
        observed = []
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        settlement_errors = []
        settle_node = self.store.settle_node

        def settle(*args, **kwargs):
            try:
                return settle_node(*args, **kwargs)
            except Exception as error:
                settlement_errors.append(str(error))
                raise

        def execute(request):
            observed.append((request.attempt, (request.worktree / "src/b.txt").read_text()))
            self.assertEqual((request.worktree / "src/a.txt").read_text(), "historical A\n")
            self.assertEqual(request.input_receipt["ancestors"][0]["node_id"], "A")
            return NodeResult("succeeded", "fixture received historical source", checks=("fixture",))

        try:
            with patch.object(coordinator, "_executor") as executor, patch.object(self.store, "settle_node", side_effect=settle):
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)
        self.assertEqual(observed, [(3, "historical B\n")])
        task = self.store.get_task(claimed["task_id"])
        node = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual(node["state"], "accepted", settlement_errors)
        self.assertIn("historical-accepted-source", node["result"]["artifacts"])
        self.assertFalse((fixture["current_tree"] / "src/b.txt").exists())

    def _control_during_materialization(self, state):
        _fixture, claimed = self._authorized_claim()
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)

        def materialize(*args, **kwargs):
            task = self.store.get_task(claimed["task_id"])
            self.store.transition_task(claimed["task_id"], state, expected_revision=task["state_revision"])

        try:
            with patch.object(coordinator, "_materialize_worktree_dependencies", side_effect=materialize), patch.object(coordinator, "_executor") as executor:
                coordinator._execute_claimed(claimed)
                executor.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)
        task = self.store.get_task(claimed["task_id"])
        self.assertEqual(task["state"], state)
        node = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual(node["state"], "blocked")
        self.assertIn("historical-accepted-source", node["result"]["artifacts"])

    def test_pause_during_materialization_prevents_executor_dispatch(self):
        self._control_during_materialization("paused")

    def test_cancel_during_materialization_prevents_executor_dispatch(self):
        self._control_during_materialization("cancelled")
