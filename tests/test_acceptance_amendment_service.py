"""The real MCP dispatch and Authority journal fence an acceptance amendment."""
from __future__ import annotations

import json
import unittest

from codex_workbench.authority_service import AuthorityService
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.store import CommandConflictError
from tests import test_acceptance_amendment as amendment_fixture


class AcceptanceAmendmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = amendment_fixture.AcceptanceAmendmentTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        server = WorkbenchMCPServer(self.fixture.config, self.store)

        def invoke(name, arguments):
            return server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })["result"]

        self.service = AuthorityService(self.store, invoke, "amendment-fixture")

    def test_preview_is_read_only_and_apply_replays_one_journal_receipt(self):
        arguments = self.fixture.arguments
        task_id = arguments["task_id"]
        before = self.store.get_task(task_id)
        cursor = self.store.health()["cursor"]
        envelope = {"tool": "workbench_amend_task_acceptance", "task_id": task_id}
        preview = self.service.dispatch({**envelope, "arguments": arguments})
        self.assertFalse(preview["result"].get("isError"), preview)
        plan = json.loads(preview["result"]["content"][0]["text"])
        self.assertEqual(self.store.health()["cursor"], cursor)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM authority_requests").fetchone()[0], 0)
        request = {
            **envelope, "request_id": "fixed-host-prerequisite-amendment",
            "arguments": {**arguments, "dry_run": False, "expected_fingerprint": plan["fingerprint"]},
        }
        first = self.service.dispatch(request)
        self.assertFalse(first["result"].get("isError"), first)
        cursor = self.store.health()["cursor"]
        second = self.service.dispatch(request)
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(self.store.health()["cursor"], cursor)
        after = self.store.get_task(task_id)
        self.assertEqual(after["state_revision"], before["state_revision"] + 1)
        self.assertEqual(after["state"], "blocked")
        self.assertEqual(after["nodes"], before["nodes"])
        with self.assertRaises(CommandConflictError):
            self.service.dispatch({**request, "arguments": {**request["arguments"], "reason": "different request"}})


if __name__ == "__main__":
    unittest.main()
