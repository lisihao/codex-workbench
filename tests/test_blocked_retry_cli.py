from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest

from codex_workbench.authority import authority_machine_id
from codex_workbench.cli import build_parser, command_task
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore


def verified(nodes: list[NodeSpec], task_id: str) -> list[NodeSpec]:
    return [*nodes, NodeSpec(
        "verify", task_id, "verify", "fixture", "fixture", "accepted",
        depends_on=tuple(node.node_id for node in nodes), verifier=True,
    )]


class BlockedRetryCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = WorkbenchConfig(
            self.root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        )
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("blocked-retry-cli", "test-machine")
        self.contract = TaskContract(
            task_id="blocked-cli",
            repository=str(self.root.resolve()),
            base_sha="abc123",
            objective="recover an infrastructure-only block",
            allowed_scope=("src",),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _run(args) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = command_task(args)
        return code, json.loads(output.getvalue())

    def _create_blocked_task(self) -> dict:
        worker = NodeSpec(
            "worker",
            self.contract.task_id,
            "worker",
            "codex",
            "gpt-5.6-terra",
            "perform bounded work",
            write_scopes=("src",),
        )
        self.store.create_task(
            self.contract,
            verified([worker], self.contract.task_id),
            "blocked-cli-create",
        )
        self.store.queue_task(self.contract.task_id)
        claimed = self.store.claim_ready_node("blocked-cli-worker", self.epoch)
        assert claimed is not None
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "blocked",
                "sandbox writable root initialization failed before any file edit",
                actual_model="gpt-5.6-terra",
                exit_code=0,
                result_kind="worker",
                changed_paths=(),
                checks=("sandbox writable-root preflight",),
                governance_profile=self.contract.governance_profile,
                verification_tier=self.contract.verification_tier,
            ),
        )
        return self.store.get_task(self.contract.task_id)

    def _args(self, task: dict, *, dry_run: bool) -> object:
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        command = [
            "--home", str(self.root),
            "task", "retry-blocked", self.contract.task_id, "worker",
            "--expected-revision", str(task["state_revision"]),
            "--expected-attempt", str(worker["attempt"]),
            "--reason", "the worker failed before editing files",
            "--confirm-no-side-effects",
        ]
        if dry_run:
            command.append("--dry-run")
        return build_parser().parse_args(command)

    def test_parser_requires_explicit_no_side_effects_confirmation(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([
                "task", "retry-blocked", "task", "node",
                "--expected-revision", "4",
                "--expected-attempt", "1",
                "--reason", "missing acknowledgment",
            ])

    def test_dry_run_returns_would_retry_without_state_cursor_or_attempt_changes(self) -> None:
        blocked = self._create_blocked_task()
        events_before = self.store.read_events(task_id=self.contract.task_id)
        code, payload = self._run(self._args(blocked, dry_run=True))

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "retry-blocked")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["task"], {
            "task_id": self.contract.task_id,
            "state": "blocked",
            "revision": blocked["state_revision"],
        })
        self.assertEqual(payload["node"], {"node_id": "worker", "state": "blocked", "attempt": 1})
        self.assertEqual(payload["would_retry"]["attempt"], 2)
        self.assertEqual(payload["would_retry"]["model"], "gpt-5.6-terra")
        self.assertEqual(payload["would_retry"]["model_reasoning_effort"], "max")
        self.assertTrue(payload["operator_asserted"])
        self.assertFalse(payload["automatically_verified"])
        self.assertEqual(self.store.get_task(self.contract.task_id), blocked)
        self.assertEqual(self.store.read_events(task_id=self.contract.task_id), events_before)

    def test_cli_authorizes_only_the_blocked_node_without_starting_a_model(self) -> None:
        blocked = self._create_blocked_task()
        code, payload = self._run(self._args(blocked, dry_run=False))

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["operator_asserted"])
        self.assertFalse(payload["automatically_verified"])
        task = self.store.get_task(self.contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        verifier = next(node for node in task["nodes"] if node["node_id"] == "verify")
        self.assertEqual(task["state"], "queued")
        self.assertEqual((worker["state"], worker["attempt"]), ("pending", 1))
        self.assertEqual((verifier["state"], verifier["attempt"]), ("pending", 0))
        authorization = [
            event for event in self.store.read_events(task_id=self.contract.task_id)
            if event["event_type"] == "node.blocked_retry_authorized"
        ]
        self.assertEqual(len(authorization), 1)
        self.assertEqual(authorization[0]["payload"]["effective_route"]["model"], "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
