"""Real durable task fixtures around a deterministic sandbox-runner double."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_workbench.authority_service import AuthorityService
from codex_workbench import controlled_validation_service as validation
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.store import StateConflictError
from tests import test_blocked_worktree_recovery as recovery_fixtures


class ControlledValidationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = recovery_fixtures.BlockedWorktreeRecoveryTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.contract, self.blocked, self.source, self.dependency_ref, _ = self.fixture._blocked_dependent_task()
        self.store = self.fixture.store
        self.fixture.config.install_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.config.install_manifest.write_text(json.dumps({"version": "fixture", "commit": "fixture-build"}))
        self.task_id = self.contract.task_id
        self.arguments = {
            "task_id": self.task_id, "node_id": "worker",
            "expected_revision": self.blocked["state_revision"], "expected_attempt": 1,
            "worktree": str(self.source), "check_id": "dsh-b-ipc-v1",
            "reason": "validate only fixture IPC on this exact source", "dry_run": True,
        }
        self.plan_data = {"worktree": str(self.source), "check_id": "dsh-b-ipc-v1",
                          "commands": [["fixed-fixture-command"]], "write_paths": []}
        self.plan = SimpleNamespace(to_dict=lambda: dict(self.plan_data))
        self.enterContext(patch.object(validation, "resolve_runtime", return_value=object()))
        self.enterContext(patch.object(validation, "plan_validation", return_value=self.plan))
        self.runner = self.enterContext(patch.object(
            validation, "run_validation", return_value=SimpleNamespace(to_dict=lambda: {"ok": True}),
        ))
        server = WorkbenchMCPServer(self.fixture.config, self.store)

        def invoke(name, arguments):
            return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})["result"]

        self.service = AuthorityService(self.store, invoke, "validation-fixture")

    def _call(self, arguments: dict, request_id: str | None = None) -> dict:
        envelope = {"tool": validation.TOOL_NAME, "arguments": arguments, "task_id": self.task_id}
        if request_id is not None:
            envelope["request_id"] = request_id
        return self.service.dispatch(envelope)

    @staticmethod
    def _decoded(receipt: dict) -> dict:
        result = receipt["result"]
        if result.get("isError"):
            raise AssertionError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def _preview(self) -> dict:
        return self._decoded(self._call(self.arguments))

    def _run_arguments(self, identifier: str = "run-1") -> dict:
        return {**self.arguments, "dry_run": False, "validation_id": identifier,
                "expected_fingerprint": self._preview()["fingerprint"]}

    def test_preview_has_no_persistent_or_source_writes(self) -> None:
        with self.store.connection() as connection:
            before_database = tuple(connection.iterdump())
        before_artifacts = tuple(sorted(self.store.artifacts.root.rglob("*")))
        before_source = (self.source / "src/value.txt").read_bytes()
        preview = self._preview()
        self.assertRegex(preview["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertTrue(preview["dry_run"])
        self.runner.assert_not_called()
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), before_database)
        self.assertEqual(tuple(sorted(self.store.artifacts.root.rglob("*"))), before_artifacts)
        self.assertEqual((self.source / "src/value.txt").read_bytes(), before_source)

    def test_run_is_audited_and_idempotent_without_changing_ancestor_or_attempt(self) -> None:
        arguments = self._run_arguments()
        before = self.store.get_task(self.task_id)
        dependency_bytes = self.store.artifacts.verify(self.dependency_ref).read_bytes()
        receipt = self._call(arguments, "run-1")
        value = self._decoded(receipt)
        self.assertTrue(value["ok"])
        self.assertEqual(value["status"], "passed")
        audit = json.loads(self.store.artifacts.verify(value["audit_ref"]).read_text())
        self.assertEqual(audit["plan"], self.plan_data)
        self.assertEqual(audit["fingerprint"], arguments["expected_fingerprint"])
        self.assertEqual(self._call(arguments, "run-1"), receipt)
        self.assertEqual(self.service.get_request("run-1"), receipt)
        self.runner.assert_called_once()
        self.assertEqual(self.store.get_task(self.task_id), before)
        self.assertEqual(self.store.artifacts.verify(self.dependency_ref).read_bytes(), dependency_bytes)
        events = self.store.read_events(task_id=self.task_id)
        self.assertEqual(events[-1]["event_type"], "authority_request.completed")

    def test_stale_source_or_plan_cannot_run(self) -> None:
        arguments = self._run_arguments()
        (self.source / "src/value.txt").write_text("changed after preview\n")
        response = self._call(arguments, "run-1")["result"]
        self.assertTrue(response["isError"])
        self.assertIn("fingerprint changed", response["content"][0]["text"])
        self.runner.assert_not_called()
        arguments = self._run_arguments("run-2")
        self.plan_data["commands"] = [["another-fixed-fixture-command"]]
        response = self._call(arguments, "run-2")["result"]
        self.assertTrue(response["isError"])
        self.assertIn("fingerprint changed", response["content"][0]["text"])
        self.runner.assert_not_called()

    def test_malformed_scope_and_identity_are_rejected(self) -> None:
        for changes in (
            {"environment": {"DSH_HOME": "/production"}},
            {"command": "arbitrary-shell"}, {"expected_attempt": True},
            {"worktree": str(self.fixture.repository)},
            {"expected_revision": self.blocked["state_revision"] + 1},
            {"check_id": "unknown-check"},
        ):
            with self.subTest(changes=changes):
                response = self._call({**self.arguments, **changes})["result"]
                self.assertTrue(response["isError"])
        self.runner.assert_not_called()

    def test_pairing_write_requires_confirmation_and_owned_sidecars(self) -> None:
        arguments = {**self.arguments, "check_id": validation.PAIRING_WRITE,
                     "dry_run": False, "validation_id": "write-1", "expected_fingerprint": "a" * 64}
        response = self._call(arguments, "write-1")["result"]
        self.assertIn("confirm_pairing_write", response["content"][0]["text"])
        response = self._call({**arguments, "validation_id": "write-2", "confirm_pairing_write": True}, "write-2")["result"]
        self.assertIn("outside the owned scope", response["content"][0]["text"])
        self.runner.assert_not_called()

    def test_recovery_cas_is_fenced_while_the_runner_is_active(self) -> None:
        arguments = self._run_arguments()

        def run(*_args):
            with self.assertRaisesRegex(StateConflictError, "controlled validation is executing"):
                self.store.retry_blocked_node(
                    self.task_id, "worker", expected_revision=self.blocked["state_revision"],
                    expected_attempt=1, reason="concurrent retry", confirm_no_side_effects=True,
                )
            with self.assertRaises(StateConflictError):
                self.service.dispatch({
                    "request_id": "concurrent-control", "task_id": self.task_id,
                    "tool": "workbench_control_task", "arguments": {
                        "task_id": self.task_id, "action": "resume",
                        "expected_revision": self.blocked["state_revision"],
                    },
                })
            return SimpleNamespace(to_dict=lambda: {"ok": True})

        self.runner.side_effect = run
        self.assertTrue(self._decoded(self._call(arguments, "run-1"))["ok"])
        self.assertEqual(self.store.get_task(self.task_id), self.blocked)

    def test_nonzero_and_unexpected_source_writes_are_failed_receipts(self) -> None:
        arguments = self._run_arguments()
        self.runner.return_value = SimpleNamespace(to_dict=lambda: {"ok": False, "exit_code": 7})
        value = self._decoded(self._call(arguments, "run-1"))
        self.assertFalse(value["ok"])
        self.assertEqual(self.store.get_task(self.task_id), self.blocked)
        arguments = self._run_arguments("run-2")

        def drift(*_args):
            (self.source / "src/value.txt").write_text("unexpected mutation\n")
            return SimpleNamespace(to_dict=lambda: {"ok": True})

        self.runner.side_effect = drift
        value = self._decoded(self._call(arguments, "run-2"))
        self.assertFalse(value["ok"])
        self.assertIn("outside its previewed sidecars", value["postflight_error"])
        self.assertTrue(self.store.artifacts.verify(value["audit_ref"]).is_file())
        self.assertEqual(self.store.get_task(self.task_id), self.blocked)

    def test_direct_run_without_service_reservation_is_rejected(self) -> None:
        with self.assertRaisesRegex(StateConflictError, "reserved Authority service request"):
            validation.validate_blocked_node(self.fixture.config, self.store, self._run_arguments())
        self.runner.assert_not_called()
