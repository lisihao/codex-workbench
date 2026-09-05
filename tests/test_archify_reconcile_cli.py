from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from codex_workbench.authority import authority_machine_id
from codex_workbench.cli import build_parser, command_task
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.planner import archify_internal_directive
from codex_workbench.store import WorkbenchStore


def verified(nodes: list[NodeSpec], task_id: str) -> list[NodeSpec]:
    return [*nodes, NodeSpec(
        "verify", task_id, "verify", "fixture", "fixture", "accepted",
        depends_on=tuple(node.node_id for node in nodes), verifier=True,
    )]


class ArchifyReconcileCLITests(unittest.TestCase):
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
        self.contract = TaskContract(
            task_id="archify-cli",
            repository=str(self.root.resolve()),
            base_sha="abc123",
            objective="repair the task without producing an architecture artifact",
            allowed_scope=("src",),
            task_type="architecture",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _run(args) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = command_task(args)
        return code, json.loads(output.getvalue())

    def _create_paused_task(self) -> dict:
        nodes = [
            NodeSpec(
                node_id,
                self.contract.task_id,
                node_id,
                "codex",
                "gpt-5.6-terra",
                f"raw prompt for {node_id}\n\nArchify directive (stale): require an artifact",
                write_scopes=("src",),
                ordinal=ordinal,
                archify=archify_internal_directive("architecture", True),
            )
            for ordinal, node_id in enumerate(("one", "two", "three"), start=1)
        ]
        self.store.create_task(self.contract, verified(nodes, self.contract.task_id), "archify-cli-create")
        self.store.queue_task(self.contract.task_id)
        queued = self.store.get_task(self.contract.task_id)
        self.store.transition_task(
            self.contract.task_id,
            "paused",
            expected_revision=queued["state_revision"],
        )
        return self.store.get_task(self.contract.task_id)

    @staticmethod
    def _proposals(task: dict) -> tuple[dict, ...]:
        return tuple(
            {
                "node_id": node["node_id"],
                "before": {"archify": node["archify"], "prompt": node["prompt"]},
                "after": {
                    "archify": archify_internal_directive("architecture", False),
                    "prompt": f"raw prompt for {node['node_id']}\n\nArchify directive (reconciled): no artifact",
                },
            }
            for node in task["nodes"]
            if node["node_id"] in {"one", "two", "three"}
        )

    def _args(self, task: dict, *, dry_run: bool) -> object:
        command = [
            "--home", str(self.root),
            "task", "reconcile-archify", self.contract.task_id,
            "--expected-revision", str(task["state_revision"]),
            "--reason", "the frozen contract explicitly declines an artifact",
        ]
        if dry_run:
            command.append("--dry-run")
        return build_parser().parse_args(command)

    def test_dry_run_is_read_only_and_actual_reconciliation_keeps_paused_state(self) -> None:
        paused = self._create_paused_task()
        proposals = self._proposals(paused)
        events_before = self.store.read_events(task_id=self.contract.task_id)
        with mock.patch(
            "codex_workbench.store.propose_archify_reconciliation",
            return_value=proposals,
        ):
            code, preview = self._run(self._args(paused, dry_run=True))

        self.assertEqual(code, 0)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["action"], "reconcile-archify")
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["status"], "would-reconcile")
        self.assertEqual(preview["task"], {
            "task_id": self.contract.task_id,
            "state": "paused",
            "revision": paused["state_revision"],
        })
        self.assertEqual(preview["changes"], list(proposals))
        self.assertEqual(self.store.get_task(self.contract.task_id), paused)
        self.assertEqual(self.store.read_events(task_id=self.contract.task_id), events_before)

        with mock.patch(
            "codex_workbench.store.propose_archify_reconciliation",
            return_value=proposals,
        ):
            code, result = self._run(self._args(paused, dry_run=False))
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "reconciled")
        self.assertFalse(result["dry_run"])
        task = self.store.get_task(self.contract.task_id)
        self.assertEqual(task["state"], "paused")
        self.assertEqual(task["state_revision"], paused["state_revision"] + 1)
        self.assertEqual(
            [node["archify"] for node in task["nodes"] if node["node_id"] in {"one", "two", "three"}],
            [archify_internal_directive("architecture", False)] * 3,
        )


if __name__ == "__main__":
    unittest.main()
