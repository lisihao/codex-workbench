"""Public MCP and Authority contracts for the existing blocked-source repair."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_workbench.authority_service import AuthorityService, is_read_only_tool
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import (
    BLOCKED_SOURCE_REPAIR_TOOL,
    TOOLS,
    WorkbenchMCPServer,
    _blocked_source_repair_arguments,
)
from codex_workbench.service_mcp import AuthorityMCPAdapter
from codex_workbench.store import WorkbenchStore


TOOL_NAME = "workbench_repair_blocked_source"
HASH = "a" * 64


class BlockedSourceRepairAPITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="blocked-source-repair-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = WorkbenchConfig(self.root)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.server = WorkbenchMCPServer(self.config, self.store)

    @staticmethod
    def status(request_id: str = "repair-1") -> dict[str, object]:
        return {"op": "status", "task_id": "task", "request_id": request_id}

    @staticmethod
    def preview(request_id: str = "repair-1") -> dict[str, object]:
        return {
            "op": "preview",
            "task_id": "task",
            "node_id": "worker",
            "expected_attempt": 2,
            "expected_revision": 7,
            "expected_contract_hash": HASH,
            "request_id": request_id,
            "reason": "continue the existing blocked source through its verifier",
        }

    @staticmethod
    def apply(request_id: str = "repair-1") -> dict[str, object]:
        return {
            **BlockedSourceRepairAPITests.preview(request_id),
            "op": "apply",
            "expected_fingerprint": HASH,
        }

    def test_schema_and_argument_grammar_accept_each_exact_operation(self) -> None:
        schema = BLOCKED_SOURCE_REPAIR_TOOL["inputSchema"]
        self.assertIn(BLOCKED_SOURCE_REPAIR_TOOL, TOOLS)
        self.assertEqual(schema["properties"]["op"]["enum"], ["status", "preview", "apply"])
        examples = {item["op"]: item for item in (self.status(), self.preview(), self.apply())}
        for branch in schema["oneOf"]:
            operation = branch["properties"]["op"]["const"]
            self.assertEqual(set(branch["required"]), set(examples[operation]))
            self.assertEqual(_blocked_source_repair_arguments(examples[operation]), examples[operation])
        for invalid in (
            {**self.status(), "node_id": "unexpected"},
            {**self.preview(), "expected_fingerprint": HASH},
            {**self.apply(), "reason": ""},
        ):
            with self.assertRaises(ValueError):
                _blocked_source_repair_arguments(invalid)

    def test_json_schema_validates_all_operations_and_rejects_cross_operation_fields(self) -> None:
        try:
            from jsonschema import Draft7Validator
        except ImportError:
            self.skipTest("jsonschema is not installed")
        validator = Draft7Validator(BLOCKED_SOURCE_REPAIR_TOOL["inputSchema"])
        for example in (self.status(), self.preview(), self.apply()):
            validator.validate(example)
        for invalid in (
            {**self.status(), "node_id": "unexpected"},
            {**self.preview(), "expected_fingerprint": HASH},
            {**self.apply(), "reason": ""},
        ):
            self.assertTrue(list(validator.iter_errors(invalid)))

    def test_mcp_dispatches_only_validated_arguments(self) -> None:
        arguments = self.preview()
        with patch(
            "codex_workbench.mcp._invoke_blocked_source_repair",
            return_value={"ok": True, "dry_run": True},
        ) as invoke:
            result = self.server._tool_result(TOOL_NAME, arguments)
        invoke.assert_called_once_with(self.store, arguments)
        self.assertTrue(json.loads(result["content"][0]["text"])["dry_run"])

    def test_authority_and_service_adapter_preserve_one_business_request_id(self) -> None:
        calls: list[dict[str, object]] = []

        def invoke(_name: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append(arguments)
            return {"ok": True, "op": arguments["op"]}

        authority = AuthorityService(self.store, invoke, "repair-api-authority")
        for arguments in (self.preview(), self.status()):
            self.assertTrue(authority.dispatch({"tool": TOOL_NAME, "arguments": arguments})["result"]["ok"])
        apply = self.apply()
        envelope = {"request_id": "repair-1", "tool": TOOL_NAME, "task_id": "task", "arguments": apply}
        first = authority.dispatch(envelope)
        self.assertEqual(authority.dispatch(envelope), first)
        self.assertEqual(len(calls), 3)
        with self.assertRaises(ValueError):
            authority.dispatch({**envelope, "request_id": "different"})

        client = Mock()
        client.tools.return_value = {"tools": [{"name": TOOL_NAME, "annotations": {"readOnlyHint": False}}]}
        client.dispatch.return_value = {"state": "completed", "result": {"content": []}}
        adapter = AuthorityMCPAdapter(client)
        for arguments in (self.preview(), self.status(), self.apply()):
            adapter.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": arguments},
            })
            forwarded = client.dispatch.call_args.args[0]
            self.assertEqual(forwarded["request_id"], arguments["request_id"])
            self.assertEqual(forwarded["arguments"], arguments)
            self.assertEqual(
                client.dispatch.call_args.kwargs["read_only"],
                arguments["op"] in {"preview", "status"},
            )
        self.assertTrue(is_read_only_tool(TOOL_NAME, self.preview()))
        self.assertTrue(is_read_only_tool(TOOL_NAME, self.status()))
        self.assertFalse(is_read_only_tool(TOOL_NAME, self.apply()))


if __name__ == "__main__":
    unittest.main()
