from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore


class MCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = WorkbenchConfig(self.root)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("mcp-test")
        self.server = WorkbenchMCPServer(self.config, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, name: str, arguments: dict) -> dict:
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        self.assertIsNotNone(response)
        return response["result"]

    def test_lists_codex_native_tools(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "workbench_request",
                "workbench_sync_github",
                "workbench_list_tasks",
                "workbench_inspect_task",
                "workbench_control_task",
                "workbench_deliver_github",
                "workbench_read_events",
                "workbench_read_artifact",
                "workbench_acceptance_report",
                "workbench_list_approvals",
                "workbench_decide_approval",
            },
        )

        report = json.loads(self.call("workbench_acceptance_report", {})["content"][0]["text"])
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["checks"]), 12)

    def test_inspects_controls_and_reads_evidence_without_a_model_call(self) -> None:
        contract = TaskContract(
            task_id="mcp-task",
            repository=str(self.root),
            base_sha="fixture",
            objective="MCP fixture",
            allowed_scope=("tests",),
        )
        nodes = [
            NodeSpec("work", "mcp-task", "work", "fixture", "fixture", "ok"),
            NodeSpec("verify", "mcp-task", "verify", "fixture", "fixture", "accepted", depends_on=("work",), verifier=True),
        ]
        self.store.create_task(contract, nodes, "mcp-create")
        inspected = json.loads(self.call("workbench_inspect_task", {"task_id": "mcp-task"})["content"][0]["text"])
        self.assertEqual(inspected["state"], "inbox")
        controlled = json.loads(
            self.call("workbench_control_task", {"task_id": "mcp-task", "action": "queue"})["content"][0]["text"]
        )
        self.assertEqual(controlled["revision"], 2)
        events = json.loads(self.call("workbench_read_events", {"task_id": "mcp-task"})["content"][0]["text"])
        self.assertIn("task.state_changed", {event["event_type"] for event in events})

        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        self.store.settle_claimed(
            claimed,
            NodeResult("indeterminate", "fixture outcome unknown"),
        )
        approvals = json.loads(
            self.call("workbench_list_approvals", {})["content"][0]["text"]
        )
        self.assertEqual(len(approvals), 1)
        decided = json.loads(
            self.call(
                "workbench_decide_approval",
                {
                    "approval_id": approvals[0]["approval_id"],
                    "decision": "retry",
                    "expected_revision": approvals[0]["task_revision"],
                },
            )["content"][0]["text"]
        )
        self.assertTrue(decided["ok"])
        self.assertEqual(self.store.get_task("mcp-task")["state"], "queued")

        ref = ArtifactStore(self.root / "artifacts").put_text("verified evidence", "txt")
        artifact = json.loads(self.call("workbench_read_artifact", {"artifact_ref": ref})["content"][0]["text"])
        self.assertEqual(artifact["text"], "verified evidence")
        self.assertFalse(artifact["truncated"])


if __name__ == "__main__":
    unittest.main()
