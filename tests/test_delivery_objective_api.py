"""Authenticated creation uses the durable objective API without granting delivery."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from codex_workbench.api import WorkbenchHandler
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore


class DeliveryObjectiveAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkbenchStore(Path(self.temp.name) / "state.sqlite")
        self.store.initialize()
        self.store.create_task(
            TaskContract("api-objective", self.temp.name, "a" * 40, "fixture", allowed_scope=("src",)),
            [NodeSpec("work", "api-objective", "work", "fixture", "fixture", "work", ordinal=1),
             NodeSpec("verify", "api-objective", "verify", "fixture", "fixture", "verify", depends_on=("work",), verifier=True, ordinal=2)],
            "create-api-task",
        )
        self.body = {
            "command_id": "create-delivery",
            "request": {
                "requested_endpoints": {"github": {"base_branch": "main", "merge": False}},
                "scope": {"paths": ["src"]},
                "authority": {"reason": "fixture request, not a grant"},
            },
        }

    def post(self, body, authenticated=True):
        handler = object.__new__(WorkbenchHandler)
        handler.server = SimpleNamespace(store=self.store)
        handler.path = "/api/tasks/api-objective/delivery-objective"
        handler._host_allowed = lambda: True
        handler._authenticated = lambda: authenticated
        handler._read_body = lambda: json.dumps(body).encode()
        response = {}
        handler._json = lambda payload, status=200: response.update(payload=payload, status=int(status))
        handler.do_POST()
        return response

    def test_authenticated_create_is_idempotent_and_does_not_grant_publication(self):
        first = self.post(self.body)
        self.assertEqual(first["status"], 200)
        objective = first["payload"]["objective"]
        second = self.post(self.body)
        self.assertEqual(second["payload"]["objective"]["objective_id"], objective["objective_id"])
        self.assertFalse(self.store.delivery_stage_authorization(objective["objective_id"], "integrate")["authorized"])
        self.assertEqual(self.store.get_task("api-objective")["state"], "inbox")

    def test_missing_authentication_cannot_create_an_objective(self):
        self.assertEqual(self.post(self.body, authenticated=False)["status"], 401)
        self.assertIsNone(self.store.get_delivery_objective_for_task("api-objective"))

    def test_same_command_different_endpoint_is_conflict(self):
        self.post(self.body)
        self.body["request"]["requested_endpoints"]["github"]["merge"] = True
        self.assertEqual(self.post(self.body)["status"], 409)

    def test_malformed_requests_are_reported(self):
        for body in ([], {}, {"command_id": 7, "request": self.body["request"]}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)["status"], 400)

    def test_mcp_creates_and_reads_the_same_durable_objective(self):
        server = WorkbenchMCPServer(WorkbenchConfig(Path(self.temp.name)), self.store)
        def call(name, arguments):
            return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        created = call("workbench_create_delivery_objective", {"task_id": "api-objective", **self.body})
        self.assertNotIn("error", created)
        self.assertFalse(created["result"].get("isError", False))
        objective = json.loads(created["result"]["content"][0]["text"])
        read = call("workbench_get_delivery_objective", {"task_id": "api-objective"})
        self.assertEqual(json.loads(read["result"]["content"][0]["text"])["objective_id"], objective["objective_id"])
        self.body["request"]["requested_endpoints"]["github"]["merge"] = True
        conflict = call("workbench_create_delivery_objective", {"task_id": "api-objective", **self.body})
        self.assertTrue(conflict["result"]["isError"])
