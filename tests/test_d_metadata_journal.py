"""Real D task-state/journal integration with a deterministic writer double."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_workbench import controlled_validation_service as validation
from codex_workbench.authority_service import AuthorityService
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import canonical_hash, canonical_json
from tests import test_d_integration_scope as scope_fixtures


class DMetadataJournalTests(unittest.TestCase):
    def test_note_preview_and_idempotent_write_preserve_actual_d_lane(self) -> None:
        fixture = scope_fixtures.DIntegrationScopeTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.store
        task_id = fixture.contract.task_id
        # Supply the note scope as part of this isolated fixture's input contract.
        # Production scope changes use the separately exercised amendment action.
        with store.transaction() as connection:
            row = connection.execute("SELECT contract_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            contract = json.loads(row[0])
            contract["allowed_scope"].append(".agents/notes/implemented")
            connection.execute("UPDATE tasks SET contract_json = ?, contract_hash = ? WHERE task_id = ?",
                               (canonical_json(contract), canonical_hash(contract), task_id))
            row = connection.execute("SELECT spec_json FROM nodes WHERE task_id = ? AND node_id = 'D'", (task_id,)).fetchone()
            spec = json.loads(row[0])
            for field in ("read_scopes", "write_scopes"):
                spec[field].append(".agents/notes/implemented")
            connection.execute("UPDATE nodes SET spec_json = ? WHERE task_id = ? AND node_id = 'D'",
                               (canonical_json(spec), task_id))
        fixture.config.install_manifest.parent.mkdir(parents=True, exist_ok=True)
        fixture.config.install_manifest.write_text('{"version":"fixture"}\n')
        anchor = ".agents/notes/implemented/feature/2026-09-13-fixture.md"
        chinese_path = anchor.removesuffix(".md") + ".zh.md"
        (fixture.source / anchor).parent.mkdir(parents=True)
        arguments = {
            "task_id": task_id, "node_id": "D",
            "expected_revision": store.get_task(task_id)["state_revision"], "expected_attempt": 2,
            "worktree": str(fixture.source), "check_id": validation.D_NOTE_WRITE,
            "reason": "fixture exact note", "dry_run": True,
            "note_anchor": anchor, "note_english": "English fixture\n", "note_chinese": "中文 fixture\n",
        }
        plan = SimpleNamespace(to_dict=lambda: {"check_id": validation.D_NOTE_WRITE, "fixture": True})

        def write_note(_plan, _artifacts):
            (fixture.source / anchor).write_text(arguments["note_english"])
            (fixture.source / chinese_path).write_text(arguments["note_chinese"])
            return SimpleNamespace(to_dict=lambda: {"ok": True})

        server = WorkbenchMCPServer(fixture.config, store)
        authority = AuthorityService(store, server._tool_result, "d-note-journal-fixture")

        def dispatch(fields, request_id=None):
            envelope = {"tool": validation.TOOL_NAME, "arguments": fields, "task_id": task_id}
            if request_id:
                envelope["request_id"] = request_id
            return authority.dispatch(envelope)

        def decoded(receipt):
            self.assertFalse(receipt["result"].get("isError"), receipt)
            return json.loads(receipt["result"]["content"][0]["text"])

        before_task = store.get_task(task_id)
        with store.connection() as connection:
            before_database = tuple(connection.iterdump())
        before_artifacts = tuple(sorted(store.artifacts.root.rglob("*")))
        with patch.object(validation, "resolve_runtime", return_value=object()), \
                patch.object(validation, "plan_validation", return_value=plan), \
                patch.object(validation, "run_validation", side_effect=write_note) as runner:
            preview = decoded(dispatch(arguments, "preview-d-note"))
            with store.connection() as connection:
                self.assertEqual(tuple(connection.iterdump()), before_database)
            self.assertEqual(tuple(sorted(store.artifacts.root.rglob("*"))), before_artifacts)
            runner.assert_not_called()
            request_id = "write-d-note"
            apply_arguments = {
                **arguments, "dry_run": False, "validation_id": request_id,
                "expected_fingerprint": preview["fingerprint"], "confirm_note_write": True,
            }
            receipt = dispatch(apply_arguments, request_id)
            result = decoded(receipt)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["requires_pairing"])
            self.assertNotEqual(result["source_delta_before"], result["source_delta_after"])
            self.assertEqual(dispatch(apply_arguments, request_id), receipt)
            self.assertEqual(authority.get_request(request_id), receipt)
            runner.assert_called_once()
        self.assertEqual(store.get_task(task_id), before_task)
        self.assertEqual((fixture.source / anchor).read_text(), arguments["note_english"])
        self.assertTrue(store.artifacts.verify(result["audit_ref"]).is_file())


if __name__ == "__main__":
    unittest.main()
