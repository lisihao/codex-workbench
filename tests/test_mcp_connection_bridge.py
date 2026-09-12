from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "scripts" / "workbench-mcp-bridge.py"


def _load_bridge() -> object:
    spec = importlib.util.spec_from_file_location("workbench_mcp_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


FAKE_CHILD = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


mode = sys.argv[1]
root = Path(sys.argv[2])
root.mkdir(parents=True, exist_ok=True)
counter = root / "launches"
launch = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(launch))
with (root / "pids").open("a") as stream:
    stream.write(str(os.getpid()) + "\n")


def log(value: str) -> None:
    with (root / "log").open("a") as stream:
        stream.write(value + "\n")


def send(request_id: object, result: object) -> None:
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n"
    if mode == "chunked":
        encoded = payload.encode("utf-8")
        for offset in range(0, len(encoded), 7):
            sys.stdout.buffer.write(encoded[offset:offset + 7])
            sys.stdout.buffer.flush()
        return
    print(payload, end="", flush=True)


if mode == "exit255":
    for index in range(20):
        sys.stderr.write(f"token=bridge-fixture-secret-{index} /Users/example/private.log\n")
    sys.stderr.flush()
    raise SystemExit(255)

version = "2.0.0" if mode == "upgrade" and launch >= 2 else "1.0.0"
tools = [
    {"name": "workbench_read", "annotations": {"readOnlyHint": True}},
    {"name": "workbench_read_events", "annotations": {"readOnlyHint": True}},
    {"name": "workbench_get_service_request", "annotations": {"readOnlyHint": True}},
    {"name": "workbench_write", "annotations": {"readOnlyHint": False}},
    {"name": "workbench_handoff_lockfile", "annotations": {"readOnlyHint": False}},
]
if mode == "upgrade" and launch >= 2:
    tools.append({"name": "new_read_tool", "annotations": {"readOnlyHint": True}})


for raw in sys.stdin:
    request = json.loads(raw)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        log("initialize")
        send(request_id, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fake", "version": version},
        })
        continue
    if method == "notifications/initialized":
        log("initialized")
        continue
    if method == "tools/list":
        log("tools/list")
        send(request_id, {"tools": tools})
        continue
    if method == "ping":
        log("ping")
        send(request_id, {})
        continue
    if method != "tools/call":
        send(request_id, {"content": [{"type": "text", "text": "unsupported"}], "isError": True})
        continue
    params = request.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "workbench_read":
        log("read")
        if mode == "large":
            send(request_id, {"content": [{"type": "text", "text": "x" * (8 * 1024 * 1024)}]})
            continue
        if mode == "oversize":
            send(request_id, {"content": [{"type": "text", "text": "x" * (16 * 1024 * 1024 + 1)}]})
            continue
        if mode == "echo-large":
            payload = arguments.get("payload")
            send(
                request_id,
                {"content": [{"type": "text", "text": str(len(payload)) if isinstance(payload, str) else "missing"}]},
            )
            continue
        if mode in {"read-crash", "upgrade", "events", "events-list"} and launch == 1:
            sys.stderr.write("prompt=do-not-store-this-secret\n")
            sys.stderr.flush()
            os._exit(0)
        send(request_id, {"content": [{"type": "text", "text": "read-ok"}]})
        continue
    if name == "workbench_read_events":
        after = arguments.get("after")
        log("events:" + ("none" if after is None else str(after)))
        if arguments.get("task_id") == "empty-task":
            send(request_id, {"content": [{"type": "text", "text": "[]"}]})
            continue
        events = [{"event_id": "event-1"}]
        cursor = 5
        if launch >= 2:
            events.append({"event_id": "event-2"})
            cursor = 8
        if mode == "events-list":
            event_cursors = [5] if launch == 1 else [5, 8]
            events = [
                {"cursor": event_cursor, **event}
                for event_cursor, event in zip(event_cursors, events, strict=True)
            ]
            send(request_id, {"content": [{"type": "text", "text": json.dumps(events)}]})
        else:
            send(request_id, {"content": [{"type": "text", "text": json.dumps({"events": events, "cursor": cursor})}]})
        continue
    if name == "workbench_handoff_lockfile" and arguments.get("op") in {"preview", "status"}:
        log(arguments["op"])
        send(request_id, {"content": [{"type": "text", "text": arguments["op"] + "-ok"}]})
        continue
    if name in {"workbench_write", "workbench_handoff_lockfile"}:
        log("write")
        (root / "last-write-arguments").write_text(json.dumps(arguments, sort_keys=True))
        with (root / "effects").open("a") as stream:
            stream.write("effect\n")
        if mode in {"write-completed", "write-unknown"}:
            os._exit(0)
        send(request_id, {"content": [{"type": "text", "text": "write-ok"}]})
        continue
    if name == "workbench_get_service_request":
        log("query")
        supplied = arguments.get("request_id")
        (root / "last-query-id").write_text(str(supplied))
        if mode == "write-completed":
            payload = {
                "request_id": supplied,
                "state": "completed",
                "result": {"content": [{"type": "text", "text": "persisted-result"}]},
            }
        else:
            payload = {"request_id": supplied, "state": "unknown"}
        send(request_id, {"content": [{"type": "text", "text": json.dumps(payload)}]})
        continue
    send(request_id, {"content": [{"type": "text", "text": "unknown"}], "isError": True})
'''


class MCPConnectionBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.child = self.root / "fake-child.py"
        self.child.write_text(FAKE_CHILD, encoding="utf-8")
        self.fixture = self.root / "fixture"
        self.state_file = self.root / "private-state.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _config(self, mode: str, *, request_timeout_seconds: float = 0.5) -> object:
        return bridge.validate_config(
            {
                "schema_version": 1,
                "command": [sys.executable, "-u", str(self.child), mode, str(self.fixture)],
                "state_file": str(self.state_file),
                "connect_timeout_seconds": 0.5,
                "request_timeout_seconds": request_timeout_seconds,
                "max_reconnect_attempts": 3,
                "initial_backoff_seconds": 0.001,
                "max_backoff_seconds": 0.004,
            }
        )

    @staticmethod
    def _initialize() -> list[dict[str, object]]:
        return [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test"}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]

    def _run(
        self,
        mode: str,
        messages: list[dict[str, object]],
        *,
        request_timeout_seconds: float = 0.5,
    ) -> list[dict[str, object]]:
        input_stream = io.StringIO("\n".join(json.dumps(item) for item in messages) + "\n")
        output_stream = io.StringIO()
        server = bridge.MCPConnectionBridge(
            self._config(mode, request_timeout_seconds=request_timeout_seconds),
            output_stream,
            sleep=lambda _: None,
        )
        server.run(input_stream)
        return [json.loads(line) for line in output_stream.getvalue().splitlines()]

    @staticmethod
    def _by_id(messages: list[dict[str, object]], request_id: int) -> dict[str, object]:
        return next(item for item in messages if item.get("id") == request_id)

    def _log(self) -> list[str]:
        path = self.fixture / "log"
        return path.read_text().splitlines() if path.exists() else []

    def test_reconnects_read_only_request_with_handshake_and_catalog_check(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_read", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
        ]
        output = self._run("read-crash", messages)
        self.assertEqual(
            self._by_id(output, 3)["result"]["content"][0]["text"], "read-ok"
        )
        self.assertTrue(
            self._by_id(output, 1)["result"]["capabilities"]["tools"]["listChanged"]
        )
        self.assertEqual(self._by_id(output, 4)["result"], {})
        log = self._log()
        self.assertGreaterEqual(log.count("initialize"), 2)
        self.assertGreaterEqual(log.count("initialized"), 2)
        self.assertGreaterEqual(log.count("tools/list"), 2)
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["connection"]["state"], "connected")
        self.assertRegex(state["server"]["canonical_tools_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)

    def test_buffered_pipe_returns_eight_megabyte_json_response_within_budget(self) -> None:
        started = time.monotonic()
        output = self._run(
            "large",
            [
                *self._initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "workbench_read", "arguments": {}},
                },
            ],
            request_timeout_seconds=5.0,
        )
        elapsed = time.monotonic() - started
        text = self._by_id(output, 3)["result"]["content"][0]["text"]
        self.assertEqual(len(text), 8 * 1024 * 1024)
        self.assertLess(elapsed, 5.0)

    def test_chunked_jsonl_response_preserves_complete_bytes(self) -> None:
        output = self._run(
            "chunked",
            [
                *self._initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "workbench_read", "arguments": {}},
                },
            ],
        )
        self.assertEqual(
            self._by_id(output, 3)["result"]["content"][0]["text"],
            "read-ok",
        )

    def test_buffered_stdin_sends_complete_large_jsonl_request(self) -> None:
        payload = "y" * (8 * 1024 * 1024)
        output = self._run(
            "echo-large",
            [
                *self._initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "workbench_read", "arguments": {"payload": payload}},
                },
            ],
            request_timeout_seconds=5.0,
        )
        self.assertEqual(
            self._by_id(output, 3)["result"]["content"][0]["text"],
            str(len(payload)),
        )

    def test_oversize_response_is_explicit_and_never_retries_the_request(self) -> None:
        started = time.monotonic()
        output = self._run(
            "oversize",
            [
                *self._initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "workbench_read", "arguments": {}},
                },
            ],
            request_timeout_seconds=5.0,
        )
        elapsed = time.monotonic() - started
        result = self._by_id(output, 3)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("response_too_large", result["content"][0]["text"])
        self.assertTrue(
            any(
                item.get("method") == "notifications/message"
                and "response_too_large" in str(item)
                for item in output
            )
        )
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self._log().count("read"), 1)
        self.assertEqual((self.fixture / "launches").read_text(), "1")
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["connection"]["error"]["kind"], "response_too_large")

    def test_server_upgrade_notifies_once_and_requires_host_tools_list_before_write(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_read", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {"request_id": "write-1"}},
            },
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {"request_id": "write-1"}},
            },
        ]
        output = self._run("upgrade", messages)
        notifications = [item for item in output if item.get("method") == "notifications/tools/list_changed"]
        self.assertEqual(len(notifications), 1)
        blocked = self._by_id(output, 4)["result"]
        self.assertTrue(blocked["isError"])
        self.assertIn("tools_list_refresh_required", blocked["content"][0]["text"])
        self.assertEqual(
            self._by_id(output, 6)["result"]["content"][0]["text"], "write-ok"
        )
        self.assertEqual(self._log().count("write"), 1)

    def test_lost_write_response_queries_completed_journal_without_replay(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {"request_id": "stable-write"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {"request_id": "stable-write"}},
            },
        ]
        output = self._run("write-completed", messages)
        self.assertEqual(
            self._by_id(output, 3)["result"]["content"][0]["text"], "persisted-result"
        )
        self.assertEqual(
            self._by_id(output, 4)["result"]["content"][0]["text"], "persisted-result"
        )
        self.assertEqual((self.fixture / "effects").read_text().splitlines(), ["effect"])
        self.assertEqual(self._log().count("write"), 1)
        self.assertEqual(self._log().count("query"), 2)
        persisted = self.state_file.read_text()
        self.assertNotIn("stable-write", persisted)
        self.assertNotIn("persisted-result", persisted)

    def test_handoff_preview_and_status_do_not_poison_apply_identity(self) -> None:
        messages = self._initialize()
        for number, op in enumerate(("preview", "status", "apply", "apply"), 3):
            messages.append({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                             "params": {"name": "workbench_handoff_lockfile",
                                        "arguments": {"op": op, "request_id": "handoff-1"}}})
        output = self._run("write-completed", messages)
        self.assertEqual(self._by_id(output, 3)["result"]["content"][0]["text"], "preview-ok")
        self.assertEqual(self._by_id(output, 4)["result"]["content"][0]["text"], "status-ok")
        for number in (5, 6):
            self.assertEqual(self._by_id(output, number)["result"]["content"][0]["text"], "persisted-result")
        self.assertEqual(self._log().count("write"), 1)
        self.assertEqual((self.fixture / "last-query-id").read_text(), "handoff-1")

    def test_handoff_cancel_reconcile_use_operation_id_for_lost_response(self) -> None:
        messages = self._initialize()
        for number, op in enumerate(("cancel", "cancel", "reconcile", "reconcile"), 3):
            messages.append({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                             "params": {"name": "workbench_handoff_lockfile", "arguments": {
                                 "op": op, "request_id": "handoff-1", "operation_id": op + "-1"}}})
        output = self._run("write-completed", messages)
        for number in range(3, 7):
            self.assertEqual(self._by_id(output, number)["result"]["content"][0]["text"], "persisted-result")
        self.assertEqual(self._log().count("write"), 2)
        self.assertEqual((self.fixture / "last-query-id").read_text(), "reconcile-1")
        self.assertEqual(json.loads((self.fixture / "last-write-arguments").read_text())["request_id"], "handoff-1")

    def test_handoff_cancel_without_operation_id_never_reuses_original_handoff_id(self) -> None:
        output = self._run("normal", [*self._initialize(), {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "workbench_handoff_lockfile",
                       "arguments": {"op": "cancel", "request_id": "handoff-1"}},
        }])
        self.assertTrue(self._by_id(output, 3)["result"]["isError"])
        self.assertNotIn("write", self._log())
        self.assertNotIn("query", self._log())

    def test_unknown_write_status_is_indeterminate_and_never_replayed(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {"request_id": "stable-write"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {"request_id": "stable-write"}},
            },
        ]
        output = self._run("write-unknown", messages)
        for request_id in (3, 4):
            result = self._by_id(output, request_id)["result"]
            self.assertTrue(result["isError"])
            self.assertIn("write_indeterminate", result["content"][0]["text"])
            self.assertIn("stable-write", result["content"][0]["text"])
        self.assertEqual((self.fixture / "effects").read_text().splitlines(), ["effect"])
        self.assertEqual(self._log().count("write"), 1)

    def test_unknown_tool_and_missing_stable_id_are_never_forwarded(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "not_in_catalog", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": {}},
            },
        ]
        output = self._run("hold", messages)
        result = self._by_id(output, 3)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("stable_request_id_required", result["content"][0]["text"])
        self.assertNotIn("write", self._log())

    def test_write_forwards_existing_revision_and_binding_arguments_unchanged(self) -> None:
        arguments = {
            "request_id": "stable-write",
            "expected_revision": 7,
            "task_id": "task-a",
            "source_thread_id": "session-a",
        }
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_write", "arguments": arguments},
            },
        ]
        output = self._run("hold", messages)
        self.assertEqual(self._by_id(output, 3)["result"]["content"][0]["text"], "write-ok")
        self.assertEqual(json.loads((self.fixture / "last-write-arguments").read_text()), arguments)

    def test_reconnect_resumes_cursor_and_deduplicates_only_implicit_resume(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_read_events", "arguments": {"task_id": "task-a"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "workbench_read", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "workbench_read_events", "arguments": {"task_id": "task-a"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "workbench_read_events",
                    "arguments": {"task_id": "task-a", "after": 0},
                },
            },
        ]
        output = self._run("events", messages)
        resumed = json.loads(self._by_id(output, 5)["result"]["content"][0]["text"])
        self.assertEqual(resumed["cursor"], 8)
        self.assertEqual(resumed["events"], [{"event_id": "event-2"}])
        explicit = json.loads(self._by_id(output, 6)["result"]["content"][0]["text"])
        self.assertEqual(explicit["events"], [{"event_id": "event-1"}, {"event_id": "event-2"}])
        self.assertIn("events:5", self._log())
        self.assertIn("events:0", self._log())
        state = json.loads(self.state_file.read_text())
        self.assertNotIn("task-a", json.dumps(state))
        self.assertIn(8, state["event_cursors"].values())

    def test_native_event_list_reconnects_by_cursor_without_losing_explicit_history(self) -> None:
        output_stream = io.StringIO()
        server = bridge.MCPConnectionBridge(self._config("events-list"), output_stream, sleep=lambda _: None)
        responses: dict[int, dict[str, object]] = {}
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workbench_read_events", "arguments": {"task_id": "task-a"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "workbench_read", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "workbench_read_events", "arguments": {"task_id": "task-a"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "workbench_read_events",
                    "arguments": {"task_id": "task-a", "after": 0},
                },
            },
        ]
        try:
            for message in messages:
                response = server.handle(message)
                if response is not None and isinstance(response.get("id"), int):
                    responses[response["id"]] = response
            before_empty = json.loads(self.state_file.read_text())["event_cursors"]
            empty_response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "workbench_read_events",
                        "arguments": {"task_id": "empty-task"},
                    },
                }
            )
            self.assertIsNotNone(empty_response)
            after_empty = json.loads(self.state_file.read_text())["event_cursors"]
        finally:
            server.close()
        resumed = json.loads(responses[5]["result"]["content"][0]["text"])
        self.assertEqual(resumed, [{"cursor": 8, "event_id": "event-2"}])
        explicit = json.loads(responses[6]["result"]["content"][0]["text"])
        self.assertEqual(
            explicit,
            [{"cursor": 5, "event_id": "event-1"}, {"cursor": 8, "event_id": "event-2"}],
        )
        self.assertEqual(before_empty, after_empty)
        self.assertIn("events:5", self._log())
        self.assertIn("events:0", self._log())

    def test_exit_255_stays_reportable_without_stderr_secret(self) -> None:
        output = self._run("exit255", self._initialize())
        response = self._by_id(output, 1)
        self.assertEqual(response["error"]["message"], "connection_unavailable")
        persisted = self.state_file.read_text()
        self.assertNotIn("bridge-fixture-secret", persisted)
        self.assertNotIn("/Users/example", persisted)
        state = json.loads(persisted)
        self.assertEqual(state["connection"]["state"], "circuit_open")
        self.assertEqual(state["connection"]["last_error"]["exit_code"], 255)
        self.assertEqual(len(state["connection"]["last_error"]["stderr_tail"]), 16)

    def test_closes_owned_child_when_host_eof_arrives(self) -> None:
        self._run("hold", self._initialize())
        pids = [int(value) for value in (self.fixture / "pids").read_text().splitlines()]
        deadline = time.monotonic() + 1.0
        alive = set(pids)
        while alive and time.monotonic() < deadline:
            for pid in tuple(alive):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive.remove(pid)
            if alive:
                time.sleep(0.01)
        self.assertFalse(alive, f"owned child processes survived EOF: {sorted(alive)}")

    def test_config_enforces_bounded_backoff_and_default_schedule(self) -> None:
        config = bridge.validate_config(
            {
                "schema_version": 1,
                "command": [sys.executable, "-V"],
                "state_file": str(self.state_file),
            }
        )
        self.assertEqual(config.max_reconnect_attempts, 3)
        self.assertEqual(config.initial_backoff_seconds, 0.2)
        self.assertEqual(config.max_backoff_seconds, 1.0)
        with self.assertRaises(bridge.ConfigError):
            bridge.validate_config(
                {
                    "schema_version": 1,
                    "command": [sys.executable, "-V"],
                    "state_file": str(self.state_file),
                    "initial_backoff_seconds": 2,
                    "max_backoff_seconds": 1,
                }
            )

    def test_existing_invalid_state_is_preserved_and_never_starts_a_child(self) -> None:
        invalid_states = (
            ("invalid JSON", b"{not-json\n"),
            (
                "invalid safety records",
                json.dumps(
                    {
                        "schema_version": 1,
                        "connection": {},
                        "server": {},
                        "writes": "not-a-map",
                        "event_cursors": {},
                        "event_dedup": {},
                        "initialize_recorded": False,
                    }
                ).encode(),
            ),
        )
        for label, payload in invalid_states:
            with self.subTest(label=label):
                self.state_file.write_bytes(payload)
                with self.assertRaises(bridge.StateError):
                    bridge.MCPConnectionBridge(
                        self._config("hold"), io.StringIO(), sleep=lambda _: None
                    )
                self.assertEqual(self.state_file.read_bytes(), payload)
                self.assertFalse((self.fixture / "launches").exists())

    def test_request_id_longer_than_authority_schema_is_rejected_before_send(self) -> None:
        messages = [
            *self._initialize(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "workbench_write",
                    "arguments": {"request_id": "x" * 201},
                },
            },
        ]
        output = self._run("hold", messages)
        result = self._by_id(output, 3)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("stable_request_id_required", result["content"][0]["text"])
        self.assertNotIn("write", self._log())

    def test_cli_uses_private_config_and_preserves_jsonl_stdio(self) -> None:
        config_path = self.root / "bridge-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": [sys.executable, "-u", str(self.child), "hold", str(self.fixture)],
                    "state_file": str(self.state_file),
                    "connect_timeout_seconds": 0.5,
                    "request_timeout_seconds": 0.5,
                    "initial_backoff_seconds": 0.001,
                    "max_backoff_seconds": 0.004,
                }
            ),
            encoding="utf-8",
        )
        input_text = "\n".join(json.dumps(item) for item in self._initialize()) + "\n"
        result = subprocess.run(
            [sys.executable, str(BRIDGE_PATH), "--config", str(config_path)],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(self._by_id(output, 2)["result"]["tools"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
