from __future__ import annotations

import io
import json
import unittest
from unittest.mock import Mock

from codex_workbench.service_client import IndeterminateServiceRequest
from codex_workbench.service_mcp import AuthorityMCPAdapter, serve_authority_stdio


class ServiceMCPTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.status.return_value = {"ok": True, "version": "fixture", "service_instance": "instance-1"}
        self.client.tools.return_value = {"tools": [
            {"name": "workbench_list_tasks", "annotations": {"readOnlyHint": True}},
            {"name": "workbench_control_task", "annotations": {"readOnlyHint": False}},
            {"name": "workbench_handoff_lockfile", "annotations": {"readOnlyHint": False}},
            {"name": "workbench_get_service_request", "annotations": {"readOnlyHint": True}},
        ]}
        self.result = {"content": [{"type": "text", "text": "accepted request, not task acceptance"}]}
        self.client.dispatch.return_value = {"state": "completed", "result": self.result}
        self.adapter = AuthorityMCPAdapter(self.client)

    def call(self, name, arguments):
        return self.adapter.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})["result"]

    def test_mutation_passes_original_cas_through_shared_service(self):
        result = self.call("workbench_control_task", {
            "request_id": "mutation-1", "task_id": "task", "action": "pause", "expected_revision": 7,
        })
        self.assertEqual(result, self.result)
        envelope = self.client.dispatch.call_args.args[0]
        self.assertEqual(envelope["request_id"], "mutation-1")
        self.assertEqual(envelope["arguments"], {"task_id": "task", "action": "pause", "expected_revision": 7})
        self.assertFalse(self.client.dispatch.call_args.kwargs["read_only"])

    def test_handoff_preserves_business_id_and_maps_operation_identity(self):
        for op in ("preview", "status", "apply", "cancel", "reconcile"):
            with self.subTest(op=op):
                arguments = {"op": op, "task_id": "fixture", "request_id": "handoff-1"}
                if op in {"cancel", "reconcile"}:
                    arguments["operation_id"] = op + "-1"
                self.assertEqual(self.call("workbench_handoff_lockfile", arguments), self.result)
                envelope = self.client.dispatch.call_args.args[0]
                self.assertEqual(envelope["arguments"], arguments)
                self.assertEqual(envelope["request_id"], arguments.get("operation_id", "handoff-1"))
                self.assertEqual(self.client.dispatch.call_args.kwargs["read_only"], op in {"preview", "status"})

    def test_handoff_cancel_and_reconcile_require_distinct_operation_field(self):
        for op in ("cancel", "reconcile"):
            with self.subTest(op=op):
                result = self.call("workbench_handoff_lockfile", {"op": op, "request_id": "handoff-1"})
                self.assertTrue(result["isError"])
        self.client.dispatch.assert_not_called()

    def test_mutation_requires_stable_id_but_read_does_not(self):
        result = self.call("workbench_control_task", {"task_id": "task", "action": "pause"})
        self.assertTrue(result["isError"])
        self.client.dispatch.assert_not_called()
        self.assertEqual(self.call("workbench_list_tasks", {}), self.result)
        self.assertTrue(self.client.dispatch.call_args.kwargs["read_only"])

    def test_unknown_write_returns_original_id_without_retry(self):
        self.client.dispatch.side_effect = IndeterminateServiceRequest("mutation-1")
        result = self.call("workbench_control_task", {"request_id": "mutation-1"})
        self.assertTrue(result["isError"])
        receipt = json.loads(result["content"][0]["text"])
        self.assertEqual(receipt["request_id"], "mutation-1")
        self.assertEqual(receipt["state"], "indeterminate")
        self.client.dispatch.assert_called_once()

    def test_request_lookup_does_not_dispatch_mutation(self):
        self.client.get_request.return_value = {"request_id": "mutation-1", "state": "unknown"}
        result = self.call("workbench_get_service_request", {"request_id": "mutation-1"})
        self.assertEqual(json.loads(result["content"][0]["text"])["state"], "unknown")
        self.client.dispatch.assert_not_called()

    def test_mixed_dry_runs_match_authority_classification(self):
        names = ("workbench_control_task", "workbench_validate_blocked_node", "workbench_amend_task_acceptance")
        self.client.tools.return_value = {"tools": [
            {"name": name, "annotations": {"readOnlyHint": False}} for name in names
        ]}
        for name in names:
            for dry_run in (True, False, "true", 1, None):
                with self.subTest(name=name, dry_run=dry_run):
                    arguments = {"dry_run": dry_run, "request_id": "same-id"}
                    self.assertEqual(self.call(name, arguments), self.result)
                    self.assertEqual(self.client.dispatch.call_args.kwargs["read_only"], dry_run is True)
        self.assertEqual(self.call(names[0], {"dry_run": True}), self.result)
        self.assertTrue(self.client.dispatch.call_args.kwargs["read_only"])

    def test_catalog_hint_cannot_make_mutation_or_unknown_tool_read_only(self):
        for name in ("workbench_control_task", "future_unknown_tool"):
            with self.subTest(name=name):
                self.adapter = AuthorityMCPAdapter(self.client)
                self.client.tools.return_value = {"tools": [{
                    "name": name, "annotations": {"readOnlyHint": True},
                }]}
                self.client.dispatch.reset_mock()
                self.assertTrue(self.call(name, {})["isError"])
                self.client.dispatch.assert_not_called()
                self.assertEqual(self.call(name, {"request_id": "mutating"}), self.result)
                self.assertFalse(self.client.dispatch.call_args.kwargs["read_only"])

    def test_stdio_bad_request_does_not_close_connection(self):
        source = io.StringIO('not-json\n' + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize"}) + '\n')
        output = io.StringIO()
        serve_authority_stdio(self.client, source, output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"]["serverInfo"]["version"], "fixture")
