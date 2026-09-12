"""Task-policy activation uses the same authenticated Authority journal."""
from pathlib import Path
import tempfile
import unittest

from codex_workbench.authority_service import AuthorityService
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.node_recovery_api import recovery_tool
from codex_workbench.node_recovery_source_repair import SourceRepairNodeActions
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError, WorkbenchStore


class NodeRecoveryAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-api-")
        self.addCleanup(self.temp.cleanup)
        self.store = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.store.initialize()
        self.store.activate_coordinator("fixture", "fixture")
        task = TaskContract("policy-task", self.temp.name, "fixture", "keep the original objective", allowed_scope=("src",))
        self.store.create_task(task, [
            NodeSpec("worker", task.task_id, "work", "fixture", "fixture", "work"),
            NodeSpec("verify", task.task_id, "verify", "fixture", "fixture", "verify", depends_on=("worker",), verifier=True),
        ], "create-policy-task")
        self.service = AuthorityService(self.store, lambda name, args: recovery_tool(self.store, name, args), "fixture")

    def test_read_only_status_keeps_default_disabled_and_event_cursor_unchanged(self):
        before = self.store.health()["cursor"]
        response = self.service.dispatch({"tool": "workbench_get_node_recovery", "arguments": {"task_id": "policy-task"}})
        self.assertFalse(response["result"]["policy"]["enabled"])
        self.assertEqual(self.store.health()["cursor"], before)

    def test_advertised_source_repair_is_bound_to_existing_coordinator(self):
        coordinator = Coordinator(self.store, Path(self.temp.name), coordinator_epoch=1)
        self.addCleanup(coordinator._pool.shutdown, wait=True)
        before = self.store.get_task("policy-task")
        coordinator.bind_authority_service(self.service)
        self.assertIsInstance(coordinator.node_recovery.adapters["repair_source"], SourceRepairNodeActions)
        self.assertIs(coordinator.node_recovery.adapters["repair_source"].store, self.store)
        self.assertEqual(before, self.store.get_task("policy-task"))
        self.assertFalse(coordinator.node_recovery.recovery.get_policy("policy-task")["policy"]["enabled"])

    def test_activation_is_explicit_and_repeated_request_returns_one_receipt(self):
        request = {
            "request_id": "enable-fixture-policy", "task_id": "policy-task",
            "tool": "workbench_configure_node_recovery",
            "arguments": {"task_id": "policy-task", "expected_revision": 1, "policy": {"enabled": True}},
        }
        first = self.service.dispatch(request)
        cursor = self.store.health()["cursor"]
        second = self.service.dispatch(request)
        self.assertTrue(first["result"]["policy"]["enabled"])
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(cursor, self.store.health()["cursor"])
        self.assertEqual(self.store.get_task("policy-task")["state"], "inbox")

    def test_policy_rejects_stale_revision_and_undeclared_capabilities(self):
        with self.assertRaises(StateConflictError):
            recovery_tool(self.store, "workbench_configure_node_recovery", {
                "task_id": "policy-task", "expected_revision": 2, "policy": {"enabled": True},
            })
        with self.assertRaises(ValueError):
            recovery_tool(self.store, "workbench_configure_node_recovery", {
                "task_id": "policy-task", "expected_revision": 1,
                "policy": {"enabled": True, "allowed_actions": ["arbitrary_shell"]},
            })

    def test_local_actions_can_be_explicitly_enabled_without_dispatch(self):
        before = self.store.get_task("policy-task")
        response = recovery_tool(self.store, "workbench_configure_node_recovery", {
            "task_id": "policy-task", "expected_revision": 1,
            "policy": {"enabled": True, "allowed_actions": ["materialize_dependencies", "resume_node", "repair_source"]},
        })
        self.assertIn("resume_node", response["available_actions"])
        self.assertIn("materialize_dependencies", response["available_actions"])
        self.assertIn("repair_source", response["available_actions"])
        after = self.store.get_task("policy-task")
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertEqual(after["state"], "inbox")


if __name__ == "__main__":
    unittest.main()
