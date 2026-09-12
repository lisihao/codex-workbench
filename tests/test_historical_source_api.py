"""Focused MCP/Authority tests for accepted-source restoration wiring."""

from pathlib import Path
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_workbench.authority_service import AuthorityService
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import (
    HISTORICAL_SOURCE_TOOL,
    TOOLS,
    WorkbenchMCPServer,
    _historical_source_arguments,
)
from codex_workbench.service_mcp import AuthorityMCPAdapter
from codex_workbench.store import WorkbenchStore
from tests.test_mcp_connection_bridge import bridge


TOOL_NAME = "workbench_restore_accepted_source"
_HASH = "a" * 64


class HistoricalSourceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="historical-source-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.config = WorkbenchConfig(self.root, port=0)
        self.config.initialize()
        self.server = WorkbenchMCPServer(self.config, self.store)

    @staticmethod
    def _status(request_id: str = "status-1") -> dict[str, object]:
        return {"op": "status", "task_id": "historical-task", "request_id": request_id}

    @staticmethod
    def _preview(request_id: str = "preview-1") -> dict[str, object]:
        return {
            "op": "preview",
            "task_id": "historical-task",
            "node_id": "worker",
            "accepted_event_cursor": 17,
            "expected_attempt": 1,
            "expected_revision": 4,
            "expected_contract_hash": _HASH,
            "request_id": request_id,
        }

    @staticmethod
    def _apply(request_id: str = "apply-1") -> dict[str, object]:
        return {
            **HistoricalSourceAPITests._preview(request_id),
            "op": "apply",
            "expected_fingerprint": _HASH,
        }

    def test_schema_and_argument_fence_are_operation_specific(self):
        schema = HISTORICAL_SOURCE_TOOL["inputSchema"]
        self.assertIn(HISTORICAL_SOURCE_TOOL, TOOLS)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["op"]["enum"], ["preview", "apply", "status"])
        self.assertEqual(schema["required"], ["op", "task_id", "request_id"])
        self.assertEqual(_historical_source_arguments(self._status()), self._status())
        self.assertEqual(_historical_source_arguments(self._preview()), self._preview())
        self.assertEqual(_historical_source_arguments(self._apply()), self._apply())
        invalid = (
            {**self._status(), "node_id": "unexpected"},
            {**self._preview(), "expected_fingerprint": _HASH},
            {**self._apply(), "unexpected": True},
        )
        examples = {value["op"]: value for value in (self._status(), self._preview(), self._apply())}
        for branch in schema["oneOf"]:
            example = examples[branch["properties"]["op"]["const"]]
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["properties"]), set(example))
            self.assertEqual(set(branch["required"]), set(example))
        try:
            from jsonschema import Draft7Validator
        except ImportError:
            # Exact schema properties/required checks above run without optional tooling.
            Draft7Validator = None
        else:
            validator = Draft7Validator(schema)
            validator.validate(self._apply())
            for operation in invalid:
                with self.subTest(operation=operation["op"]):
                    self.assertTrue(list(validator.iter_errors(operation)))
        for operation in invalid:
            with self.subTest(operation=operation["op"]), self.assertRaises(ValueError):
                _historical_source_arguments(operation)
        for operation in (self._status(), self._preview(), self._apply()):
            with self.subTest(operation=operation["op"]), self.assertRaises(ValueError):
                _historical_source_arguments({**operation, "unexpected": True})

    def test_dispatch_forwards_only_validated_arguments_to_core_adapter(self):
        arguments = self._preview()
        with patch(
            "codex_workbench.mcp._invoke_historical_source",
            return_value={"ok": True, "operation": "preview"},
        ) as invoke:
            result = self.server._tool_result(TOOL_NAME, arguments)
        invoke.assert_called_once_with(self.store, arguments)
        self.assertEqual(
            json.loads(result["content"][0]["text"]),
            {"ok": True, "operation": "preview"},
        )

    def test_authority_preview_and_status_are_read_only_but_apply_replays_one_receipt(self):
        calls: list[dict[str, object]] = []

        def invoke(name: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append({"name": name, "arguments": arguments})
            return {"ok": True, "op": arguments["op"]}

        authority = AuthorityService(self.store, invoke, "fixture-authority")
        for arguments in (self._preview(), self._status()):
            response = authority.dispatch({"tool": TOOL_NAME, "arguments": arguments})
            self.assertEqual(response["result"]["ok"], True)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM authority_requests").fetchone()[0],
                0,
            )

        envelope = {
            "request_id": "apply-1",
            "tool": TOOL_NAME,
            "task_id": "historical-task",
            "arguments": self._apply(),
        }
        first = authority.dispatch(envelope)
        second = authority.dispatch(envelope)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]["arguments"], self._apply())
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT request_id, state FROM authority_requests WHERE request_id = ?",
                ("apply-1",),
            ).fetchone()
        self.assertEqual((row["request_id"], row["state"]), ("apply-1", "completed"))

    def test_authority_apply_requires_outer_request_id_to_match_business_id(self):
        with self.assertRaises(ValueError):
            AuthorityService(self.store, Mock(), "fixture-authority").dispatch({
                "request_id": "outer-id",
                "tool": TOOL_NAME,
                "task_id": "historical-task",
                "arguments": self._apply("inner-id"),
            })

    def test_service_adapter_preserves_business_id_and_classifies_read_operations(self):
        client = Mock()
        client.tools.return_value = {
            "tools": [{"name": TOOL_NAME, "annotations": {"readOnlyHint": False}}]
        }
        result = {"content": [{"type": "text", "text": "{}"}]}
        client.dispatch.return_value = {"state": "completed", "result": result}
        adapter = AuthorityMCPAdapter(client)
        for operation in (self._preview(), self._status(), self._apply()):
            response = adapter.handle({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": operation},
            })
            self.assertEqual(response["result"], result)
            envelope = client.dispatch.call_args.args[0]
            self.assertEqual(envelope["request_id"], operation["request_id"])
            self.assertEqual(envelope["arguments"], operation)
            self.assertEqual(
                client.dispatch.call_args.kwargs["read_only"],
                operation["op"] in {"preview", "status"},
            )

    def test_bridge_classifies_preview_as_read_and_apply_as_one_replayable_write(self):
        bridge_config = bridge.validate_config({
            "schema_version": 1,
            "command": [sys.executable, "-V"],
            "state_file": str(self.root / "bridge-state.json"),
        })
        supervisor = bridge.MCPConnectionBridge(bridge_config, io.StringIO(), sleep=lambda _: None)
        supervisor.tool_read_only = {TOOL_NAME: False}
        read_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "preview"}]},
        }
        supervisor._forward_read_only = Mock(return_value=read_response)
        preview_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": self._preview()},
        }
        self.assertEqual(supervisor._handle_tool_call(preview_message), read_response)
        supervisor._forward_read_only.assert_called_once_with(preview_message)
        self.assertFalse(supervisor.state.has_write("preview-1"))

        persisted_response = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "applied"}]},
        }
        journal_response = {
            "jsonrpc": "2.0",
            "id": "query",
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "request_id": "apply-1",
                        "state": "completed",
                        "result": persisted_response["result"],
                    }),
                }],
            },
        }
        supervisor._recover_connection = Mock(return_value=True)
        supervisor._send_request = Mock(side_effect=[persisted_response, journal_response])
        apply_message = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": self._apply()},
        }
        first = supervisor._handle_tool_call(apply_message)
        second = supervisor._handle_tool_call({**apply_message, "id": 3})
        self.assertEqual(first, persisted_response)
        self.assertEqual(second["result"], persisted_response["result"])
        self.assertEqual(supervisor._send_request.call_count, 2)
        self.assertEqual(
            supervisor._send_request.call_args_list[0].args[0]["params"]["arguments"],
            self._apply(),
        )


if __name__ == "__main__":
    unittest.main()
