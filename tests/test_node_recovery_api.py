"""Task-policy activation uses the same authenticated Authority journal."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority_service import AuthorityService
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.node_recovery_api import (
    recovery_tool,
    resume_owner_repairs_tool,
)
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NodeRecoveryStore
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

    def test_authenticated_configure_accepts_journal_request_id(self):
        response = recovery_tool(self.store, "workbench_configure_node_recovery", {
            "task_id": "policy-task",
            "expected_revision": 1,
            "policy": {"enabled": True},
            "request_id": "configure-policy-through-authority-journal",
        })
        self.assertTrue(response["policy"]["enabled"])

    def test_external_deployment_resume_verifies_identity_before_store_cas(self):
        config = WorkbenchConfig(Path(self.temp.name) / "runtime")
        config.install_manifest.parent.mkdir(parents=True)
        config.install_manifest.write_text(json.dumps({
            "version": "1.19.30",
            "commit": "a" * 40,
            "tag": "v1.19.30",
            "installed_at": "2026-09-14T20:00:00+00:00",
        }), encoding="utf-8")
        NodeRecoveryStore(self.store).configure_policy(
            "policy-task",
            RecoveryPolicy(enabled=True, allowed_actions=("resume_owner_repairs",)),
            expected_task_revision=1,
            actor="fixture",
        )
        with self.store.transaction() as connection:
            failure_cursor = self.store._event(
                connection,
                "node.accepted_source_repair_rolled_back",
                "policy-task",
                "worker",
                {"attempt": 3},
                created_at="2026-09-14T19:00:00+00:00",
            )
        candidate = {
            "task_revision": 1,
            "event_cursor": 17,
            "progress_fingerprint": "progress-fixture",
            "owner_node_ids": ["worker"],
            "owner_attempts": {"worker": 3},
            "failure_event_cursors": {"worker": failure_cursor},
        }
        stored = {
            "request_id": "resume-external-deployment",
            "revision": 2,
            "event_cursor": 18,
            "owner_node_ids": ["worker"],
        }
        authority = {
            "active": True,
            "instance_id": "authority-fixture",
            "authority_epoch": 2,
            "started_at": "2026-09-14T20:00:05+00:00",
        }
        with (
            patch.object(
                self.store,
                "exhausted_blocked_owner_repair_candidate",
                return_value=candidate,
            ),
            patch.object(self.store, "authority_status", return_value=authority),
            patch.object(
                self.store,
                "resume_exhausted_blocked_owner_repairs",
                return_value=stored,
            ) as resume,
        ):
            result = resume_owner_repairs_tool(config, self.store, {
                "task_id": "policy-task",
                "node_id": "verify",
                "expected_revision": 1,
                "expected_attempt": 1,
                "expected_version": "1.19.30",
                "expected_commit": "a" * 40,
                "expected_tag": "v1.19.30",
                "confirm_deployment": True,
                "request_id": "resume-external-deployment",
            })
        self.assertTrue(result["ok"])
        self.assertEqual(result["deployment"]["authority_epoch"], 2)
        self.assertEqual(result["owner_node_ids"], ["worker"])
        call = resume.call_args.kwargs
        self.assertEqual(call["expected_event_cursor"], 17)
        self.assertEqual(len(call["repair_fingerprint"]), 64)
        self.assertEqual(len(call["readiness_fingerprint"]), 64)


if __name__ == "__main__":
    unittest.main()
