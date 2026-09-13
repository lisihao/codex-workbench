"""MCP routing and transport classification for the fixed D scope amendment."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority_service import is_read_only_tool
from codex_workbench.config import WorkbenchConfig
from codex_workbench.d_integration_profile import SCOPE_PROFILE_ID
from codex_workbench.mcp import TOOLS, WorkbenchMCPServer
from codex_workbench.store import WorkbenchStore


class IntegrationScopeMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = WorkbenchConfig(Path(directory.name))
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.server = WorkbenchMCPServer(self.config, self.store)

    def test_catalog_exposes_fixed_profile_and_fingerprint(self) -> None:
        tool = next(item for item in TOOLS if item["name"] == "workbench_control_task")
        properties = tool["inputSchema"]["properties"]
        self.assertIn("amend_blocked_integration_scope", properties["action"]["enum"])
        self.assertEqual(properties["profile_id"], {"const": SCOPE_PROFILE_ID})
        self.assertIn("pattern", properties["expected_contract_hash"])
        self.assertIn("pattern", properties["expected_fingerprint"])

    def test_routes_exact_amendment_arguments_without_launching_task(self) -> None:
        arguments = {
            "task_id": "fixture-dsh", "action": "amend_blocked_integration_scope",
            "node_id": "D", "expected_revision": 37, "expected_attempt": 2,
            "expected_contract_hash": "a" * 64, "profile_id": SCOPE_PROFILE_ID,
            "reason": "reviewed integration closure", "dry_run": True,
        }
        with patch("codex_workbench.mcp.amend_blocked_integration_scope", return_value={"dry_run": True}) as amend:
            result = self.server._tool_result("workbench_control_task", arguments)
        self.assertEqual(json.loads(result["content"][0]["text"]), {"dry_run": True})
        amend.assert_called_once_with(
            self.config, self.store, {key: value for key, value in arguments.items() if key != "action"},
        )

    def test_amendment_fields_are_rejected_on_other_actions(self) -> None:
        for field, value in (("profile_id", SCOPE_PROFILE_ID),
                             ("expected_contract_hash", "a" * 64),
                             ("expected_fingerprint", "b" * 64)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "only supported"):
                self.server._tool_result("workbench_control_task", {
                    "task_id": "nonexistent", "action": "queue", "expected_revision": 1,
                    field: value,
                })

    def test_preview_is_read_only_but_apply_uses_write_journal(self) -> None:
        for dry_run, expected in ((True, True), (False, False), ("true", False), (1, False)):
            with self.subTest(dry_run=dry_run):
                self.assertEqual(is_read_only_tool("workbench_control_task", {
                    "action": "amend_blocked_integration_scope", "dry_run": dry_run,
                }), expected)


if __name__ == "__main__":
    unittest.main()
