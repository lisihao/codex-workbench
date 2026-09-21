from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.authority_service import AUTHORITY_REQUEST_JOURNAL_DDL
from codex_workbench.config import WorkbenchConfig
from codex_workbench.lockfile_handoff import TOOL as HANDOFF_TOOL
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore
from codex_workbench.submission import planning_request_receipt


class AuthorityServiceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = WorkbenchConfig(self.root, port=0)
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

    def test_invalid_workbench_request_strategy_is_rejected_before_enqueue(self):
        envelope = {
            "request_id": "invalid-strategy",
            "tool": "workbench_request",
            "task_id": "invalid-strategy-task",
            "arguments": {
                "request_id": "invalid-strategy",
                "command_id": "invalid-strategy-command",
                "task_id": "invalid-strategy-task",
                "objective": "reject unsupported routing metadata",
                "repository": str(self.root),
                "allowed_scopes": ["src"],
                "strategy": {
                    "complexity": "high",
                    "bounded": True,
                    "independent_slice": True,
                },
            },
        }
        with patch("codex_workbench.mcp.enqueue_natural_language_request") as enqueue:
            first = self._request("/api/service/requests", envelope)

        self.assertEqual(first["state"], "rejected")
        self.assertEqual(first["effects"], "none")
        self.assertFalse(first["enqueued"])
        self.assertEqual(first["rejection"]["invalid_fields"], [
            "strategy.bounded", "strategy.independent_slice",
        ])
        self.assertIn("strategy.version", first["rejection"]["allowed_fields"])
        enqueue.assert_not_called()
        self.assertEqual(self._request("/api/service/requests", envelope), first)
        self.assertEqual(self._request("/api/service/requests/invalid-strategy"), first)
        with self.assertRaises(KeyError):
            self.store.get_planning_request("invalid-strategy-command")
        with self.assertRaises(HTTPError) as error:
            self._request("/api/service/requests", {
                **envelope,
                "arguments": {
                    **envelope["arguments"],
                    "strategy": {"version": "model-routing-v2"},
                },
            })
        self.assertEqual(error.exception.code, HTTPStatus.CONFLICT)
        error.exception.close()

    def test_valid_terra_request_and_command_identity_are_deduplicated(self):
        repository = self.root / "terra-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "Fixture"], check=True)
        (repository / "README.md").write_text("fixture\n")
        subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)

        def enqueue(_config, _store, **kwargs):
            request = {
                "objective": kwargs["objective"],
                "repository": kwargs["repository"],
                "allowed_scope": list(kwargs["allowed_scope"]),
                "strategy": kwargs["strategy"],
            }
            return planning_request_receipt(self.store.enqueue_planning_request(
                kwargs["command_id"], kwargs["task_id"], request,
            ))

        arguments = {
            "request_id": "terra-request-1",
            "command_id": "terra-command",
            "task_id": "terra-task",
            "objective": "one independent Terra delivery slice",
            "repository": str(repository),
            "allowed_scopes": ["src"],
            "complexity": "high",
            "parallelizable": True,
            "executor_model": "gpt-5.6-terra",
            "strategy": {
                "version": "model-routing-v2",
                "task_type": "implementation",
                "complexity": "high",
                "parallelizable": True,
                "claude_allowed": True,
            },
        }
        first_envelope = {
            "request_id": "terra-request-1",
            "tool": "workbench_request",
            "task_id": "terra-task",
            "arguments": arguments,
        }
        with patch(
            "codex_workbench.mcp.enqueue_natural_language_request",
            side_effect=enqueue,
        ) as submit:
            first = self._request("/api/service/requests", first_envelope)
            repeated = self._request("/api/service/requests", first_envelope)
            second = self._request("/api/service/requests", {
                **first_envelope,
                "request_id": "terra-request-2",
                "arguments": {**arguments, "request_id": "terra-request-2"},
            })

        self.assertEqual(first["state"], "completed")
        self.assertEqual(repeated, first)
        self.assertEqual(second["state"], "completed")
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(
            self.store.get_planning_request("terra-command")["task_id"],
            "terra-task",
        )
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM planning_requests WHERE command_id = 'terra-command'"
            ).fetchone()
        assert count is not None
        self.assertEqual(count["count"], 1)
        self.assertTrue(submit.call_args.kwargs["parallelizable"])
        self.assertEqual(submit.call_args.kwargs["strategy"]["complexity"], "high")
