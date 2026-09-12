from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.authority_service import AUTHORITY_REQUEST_JOURNAL_DDL
from codex_workbench.config import WorkbenchConfig
from codex_workbench.lockfile_handoff import TOOL as HANDOFF_TOOL
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore


class AuthorityServiceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = WorkbenchConfig(Path(self.temp.name), port=0)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        with self.store.connection() as connection:
            connection.executescript(AUTHORITY_REQUEST_JOURNAL_DDL)
        contract = TaskContract("fixture", self.temp.name, "fixture-base", "preserve task identity", ("src",))
        self.store.create_task(contract, [NodeSpec("verify", "fixture", "verify", "fixture", "fixture", "accepted", verifier=True)], "create-fixture")
        self.server = WorkbenchHTTPServer(self.config, self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, path, body=None, *, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.config.token()
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_authenticated_service_catalog_and_request_receipt(self):
        status = self._request("/api/service/status")
        self.assertEqual(status["service_protocol"], "workbench-authority-service/v1")
        tools = self._request("/api/service/tools")["tools"]
        for tool in tools:
            required = tool["inputSchema"].get("required", [])
            self.assertEqual(len(required), len(set(required)), tool["name"])
        handoff = next(tool for tool in tools if tool["name"] == "workbench_handoff_lockfile")
        responsibility = next(tool for tool in tools if tool["name"] == "workbench_responsibility")
        self.assertNotIn("request_id", responsibility["inputSchema"]["required"])
        open_schema = next(item for item in responsibility["inputSchema"]["oneOf"]
                           if item["properties"]["op"]["const"] == "open")
        self.assertIn("request_id", open_schema["required"])
        self.assertEqual(handoff["inputSchema"]["properties"]["request_id"],
                         HANDOFF_TOOL["inputSchema"]["properties"]["request_id"])
        control = next(tool for tool in tools if tool["name"] == "workbench_control_task")
        self.assertIn("request_id", control["inputSchema"]["required"])
        self.assertFalse(control["annotations"]["readOnlyHint"])
        revision = self.store.get_task("fixture")["state_revision"]
        envelope = {"request_id": "queue-1", "tool": "workbench_control_task", "task_id": "fixture",
                    "arguments": {"task_id": "fixture", "action": "queue", "expected_revision": revision}}
        first = self._request("/api/service/requests", envelope)
        after = self.store.get_task("fixture")
        self.assertEqual(first["state"], "completed")
        self.assertEqual(after["state"], "queued")
        self.assertEqual(self._request("/api/service/requests", envelope), first)
        self.assertEqual(self._request("/api/service/requests/queue-1"), first)
        self.assertEqual(self.store.get_task("fixture"), after)

    def test_no_auth_cannot_read_private_receipts_or_mutate(self):
        before = self.store.get_task("fixture")
        for path, body in (("/api/service/tools", None), ("/api/service/status", None),
                           ("/api/service/requests/anything", None),
                           ("/api/service/requests", {"request_id": "bad"})):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self._request(path, body, authenticated=False)
            self.assertEqual(error.exception.code, HTTPStatus.UNAUTHORIZED)
            error.exception.close()
        self.assertEqual(self.store.get_task("fixture"), before)

    def test_draining_returns_explicit_conflict_without_new_receipt(self):
        before = self.store.get_task("fixture")
        self.server.authority_service.begin_drain()
        with self.assertRaises(HTTPError) as error:
            self._request("/api/service/requests", {
                "request_id": "after-drain", "tool": "workbench_control_task",
                "task_id": "fixture", "arguments": {
                    "task_id": "fixture", "action": "queue", "expected_revision": before["state_revision"],
                },
            })
        self.assertEqual(error.exception.code, HTTPStatus.CONFLICT)
        self.assertIn("draining", json.load(error.exception)["error"])
        error.exception.close()
        self.assertTrue(self._request("/api/service/status")["ok"])
        with self.assertRaises(KeyError):
            self.server.authority_service.get_request("after-drain")
        self.assertEqual(self.store.get_task("fixture"), before)

    def test_stale_revision_and_conflicting_request_do_not_change_task(self):
        before = self.store.get_task("fixture")
        envelope = {"request_id": "stale", "tool": "workbench_control_task",
                    "arguments": {"task_id": "fixture", "action": "queue", "expected_revision": -1}}
        rejected = self._request("/api/service/requests", envelope)
        self.assertEqual(rejected["state"], "completed")
        self.assertTrue(rejected["result"]["isError"])
        with self.assertRaises(HTTPError) as error:
            self._request("/api/service/requests", {**envelope, "arguments": {**envelope["arguments"], "expected_revision": before["state_revision"]}})
        self.assertEqual(error.exception.code, HTTPStatus.CONFLICT)
        error.exception.close()
        self.assertEqual(self.store.get_task("fixture"), before)
