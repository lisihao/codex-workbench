"""Focused regression coverage for bridge dry-run classification."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from collections.abc import Mapping
from typing import Any, Callable
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "scripts" / "workbench-mcp-bridge.py"
MIXED_OPERATION_TOOLS = (
    "workbench_control_task",
    "workbench_validate_blocked_node",
    "workbench_amend_task_acceptance",
)


def _load_bridge() -> Any:
    spec = importlib.util.spec_from_file_location("dry_run_bridge_regression", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class FakeTransport:
    """Record bridge requests without starting a child process or using a network."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def send(self, message: Mapping[str, object]) -> None:
        self.requests.append(copy.deepcopy(dict(message)))

    def close(self) -> None:
        pass


class DryRunBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dry-run-bridge-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def _make_bridge(
        self,
        label: str,
        responder: Callable[[Mapping[str, object]], dict[str, object]],
    ) -> tuple[Any, FakeTransport, Mock, Path]:
        state_file = self.root / f"{label}.json"
        connection = bridge.MCPConnectionBridge(
            bridge.validate_config(
                {
                    "schema_version": 1,
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "state_file": str(state_file),
                    "connect_timeout_seconds": 0.05,
                    "request_timeout_seconds": 0.05,
                    "max_reconnect_attempts": 1,
                    "initial_backoff_seconds": 0.001,
                    "max_backoff_seconds": 0.001,
                }
            ),
            io.StringIO(),
            sleep=lambda _: None,
        )
        transport = FakeTransport()
        connection.transport = transport
        connection.tool_read_only = {
            "workbench_get_service_request": True,
            **{name: False for name in MIXED_OPERATION_TOOLS},
        }

        def send_request(message: Mapping[str, object], *_: object) -> dict[str, object]:
            transport.send(message)
            return responder(message)

        fake_send_request = Mock(side_effect=send_request)
        connection._send_request = fake_send_request
        self.addCleanup(connection.close)
        return connection, transport, fake_send_request, state_file

    @staticmethod
    def _tool_call(
        name: str,
        arguments: Mapping[str, object],
        rpc_id: int = 1,
    ) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        }

    @staticmethod
    def _forwarded_response(message: Mapping[str, object]) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"content": [{"type": "text", "text": "forwarded"}]},
        }

    @staticmethod
    def _query_response(message: Mapping[str, object]) -> dict[str, object]:
        params = message.get("params")
        assert isinstance(params, Mapping)
        arguments = params.get("arguments")
        assert isinstance(arguments, Mapping)
        request_id = arguments.get("request_id")
        payload = {
            "request_id": request_id,
            "state": "completed",
            "result": {"content": [{"type": "text", "text": "prior-result"}]},
        }
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }

    def test_exact_true_bypasses_existing_sent_record_and_preserves_it(self) -> None:
        for index, name in enumerate(MIXED_OPERATION_TOOLS):
            with self.subTest(tool=name):
                def responder(message: Mapping[str, object]) -> dict[str, object]:
                    return self._forwarded_response(message)

                connection, transport, fake_send_request, state_file = self._make_bridge(
                    f"existing-{index}", responder
                )
                request_id = f"existing-sent-{index}"
                connection.state.mark_write_sent(request_id)
                writes_before = copy.deepcopy(connection.state.data["writes"])

                response = connection.handle(
                    self._tool_call(
                        name,
                        {"task_id": "task-1", "request_id": request_id, "dry_run": True},
                    )
                )

                self.assertIsNotNone(response)
                assert response is not None
                self.assertEqual(response["result"]["content"][0]["text"], "forwarded")
                self.assertEqual(
                    [request["params"]["name"] for request in transport.requests],
                    [name],
                )
                fake_send_request.assert_called_once()
                self.assertEqual(connection.state.data["writes"], writes_before)
                persisted = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(persisted["writes"], writes_before)

    def test_validation_error_then_corrected_same_id_forwards_twice_without_journal(self) -> None:
        for index, name in enumerate(MIXED_OPERATION_TOOLS):
            with self.subTest(tool=name):
                responses = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "isError": True,
                            "content": [
                                {"type": "text", "text": "confirm_old_executor_ended is required"}
                            ],
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"content": [{"type": "text", "text": "corrected"}]},
                    },
                ]

                def responder(message: Mapping[str, object]) -> dict[str, object]:
                    params = message.get("params")
                    assert isinstance(params, Mapping)
                    if params.get("name") != name:
                        raise AssertionError("dry-run retry must not query the mutation journal")
                    return responses.pop(0)

                connection, transport, fake_send_request, state_file = self._make_bridge(
                    f"retry-{index}", responder
                )
                request_id = f"validation-retry-{index}"
                initial_arguments = {
                    "task_id": "task-1",
                    "request_id": request_id,
                    "dry_run": True,
                }
                corrected_arguments = {
                    **initial_arguments,
                    "confirm_old_executor_ended": True,
                }

                first = connection.handle(self._tool_call(name, initial_arguments, rpc_id=1))
                second = connection.handle(self._tool_call(name, corrected_arguments, rpc_id=2))

                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                assert first is not None and second is not None
                self.assertTrue(first["result"]["isError"])
                self.assertEqual(second["result"]["content"][0]["text"], "corrected")
                self.assertEqual(
                    [request["params"]["name"] for request in transport.requests],
                    [name, name],
                )
                self.assertEqual(
                    transport.requests[0]["params"]["arguments"], initial_arguments
                )
                self.assertEqual(
                    transport.requests[1]["params"]["arguments"], corrected_arguments
                )
                self.assertEqual(fake_send_request.call_count, 2)
                self.assertEqual(connection.state.data["writes"], {})
                self.assertFalse(state_file.exists())
                self.assertEqual(responses, [])

    def test_false_and_string_true_query_prior_sent_identity_as_mutations(self) -> None:
        for index, name in enumerate(MIXED_OPERATION_TOOLS):
            for dry_run in (False, "true"):
                with self.subTest(tool=name, dry_run=dry_run):
                    def responder(message: Mapping[str, object]) -> dict[str, object]:
                        params = message.get("params")
                        assert isinstance(params, Mapping)
                        if params.get("name") != "workbench_get_service_request":
                            raise AssertionError("a prior sent mutation must be query-only")
                        return self._query_response(message)

                    connection, transport, fake_send_request, state_file = self._make_bridge(
                        f"query-{index}-{dry_run}", responder
                    )
                    request_id = f"query-existing-{index}-{dry_run}"
                    connection.state.mark_write_sent(request_id)
                    writes_before = copy.deepcopy(connection.state.data["writes"])

                    response = connection.handle(
                        self._tool_call(
                            name,
                            {
                                "task_id": "task-1",
                                "request_id": request_id,
                                "dry_run": dry_run,
                            },
                        )
                    )

                    self.assertIsNotNone(response)
                    assert response is not None
                    self.assertEqual(response["result"]["content"][0]["text"], "prior-result")
                    self.assertEqual(
                        [request["params"]["name"] for request in transport.requests],
                        ["workbench_get_service_request"],
                    )
                    query_arguments = transport.requests[0]["params"]["arguments"]
                    self.assertEqual(query_arguments, {"request_id": request_id})
                    fake_send_request.assert_called_once()
                    self.assertEqual(connection.state.data["writes"], writes_before)
                    persisted = json.loads(state_file.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["writes"], writes_before)

    def test_unknown_tool_with_dry_run_true_stays_on_mutation_path(self) -> None:
        unknown_name = "workbench_future_mutation"

        def responder(message: Mapping[str, object]) -> dict[str, object]:
            return self._forwarded_response(message)

        connection, transport, fake_send_request, _ = self._make_bridge("unknown", responder)
        request_id = "unknown-dry-run"
        response = connection.handle(
            self._tool_call(
                unknown_name,
                {"task_id": "task-1", "request_id": request_id, "dry_run": True},
            )
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response["result"]["content"][0]["text"], "forwarded")
        self.assertEqual(
            [request["params"]["name"] for request in transport.requests],
            [unknown_name],
        )
        fake_send_request.assert_called_once()
        self.assertTrue(connection.state.has_write(request_id))


if __name__ == "__main__":
    unittest.main()
