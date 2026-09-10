from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority import authority_machine_id
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager
from tests.process_probe_fixture import isolated_process_catalog


class ScopeNormalizationFixture:
    """Own one disposable Git source and indeterminate worker per test."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        (self.repository / "src" / "task-template.spec.ts").write_text(
            "fixture source", encoding="utf-8"
        )
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.invalid"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.state_root = self.root / "state"
        self.config = WorkbenchConfig(
            self.state_root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        )
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator(
            "scope-normalization-fixture", "fixture-machine"
        )
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self.mcp = WorkbenchMCPServer(self.config, self.store)
        self.enterContext(isolated_process_catalog(()))

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _make_indeterminate(
        self,
        *,
        task_id: str = "scope-normalization",
        scope_pattern: str = "src/task-template*.ts",
    ) -> tuple[TaskContract, Path]:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="normalize one legacy source scope without resuming the worker",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
            external_write_permission=False,
            destructive_action_permission=False,
        )
        worker = NodeSpec(
            "worker",
            task_id,
            "legacy scope worker",
            "fixture",
            "fixture",
            "fixture worker",
            read_scopes=(scope_pattern,),
            write_scopes=(scope_pattern,),
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            "fixture verifier",
            depends_on=("worker",),
            verifier=True,
        )
        self.store.create_task(contract, [worker, verifier], f"{task_id}-create")
        self.store.queue_task(task_id)
        claimed = self.store.claim_ready_node(f"{task_id}-worker", self.epoch)
        assert claimed is not None
        source = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            task_id,
            "worker",
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            task_id,
            "worker",
            str(source),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "indeterminate",
                "fixture source stopped after declared legacy scope work",
                result_kind="worker",
            ),
        )
        task = self.store.get_task(task_id)
        node = next(item for item in task["nodes"] if item["node_id"] == "worker")
        assert (task["state"], node["state"], node["attempt"]) == (
            "needs_approval",
            "indeterminate",
            1,
        )
        return contract, source

    def _control(self, **arguments: object) -> dict[str, object]:
        response = self.mcp.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workbench_control_task", "arguments": arguments},
        })
        assert response is not None
        result = response["result"]
        assert "isError" not in result, result
        return json.loads(result["content"][0]["text"])

    def _normalization_arguments(
        self,
        contract: TaskContract,
        **overrides: object,
    ) -> dict[str, object]:
        task = self.store.get_task(contract.task_id)
        arguments: dict[str, object] = {
            "task_id": contract.task_id,
            "node_id": "worker",
            "action": "normalize_indeterminate_scope",
            "expected_revision": int(task["state_revision"]),
            "expected_attempt": 1,
            "scope_pattern": "src/task-template*.ts",
            "exact_path": "src/task-template.spec.ts",
            "reason": "normalize the one legacy scope to its owned source file",
            "dry_run": True,
        }
        arguments.update(overrides)
        return arguments


class ScopeNormalizationStoreTests(ScopeNormalizationFixture, unittest.TestCase):
    """The explicit operation is previewable, source-bound, and auditable."""

    def test_mcp_preview_has_zero_database_artifact_or_source_writes(self) -> None:
        contract, source = self._make_indeterminate()
        target = source / "src" / "task-template.spec.ts"
        source_before = target.read_bytes()
        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        artifact_root = self.state_root / "artifacts"
        artifacts_before = self._artifact_bytes(artifact_root)

        preview = self._control(**self._normalization_arguments(contract))

        self.assertEqual(preview["action"], "normalize_indeterminate_scope")
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["changed"])
        self.assertIsNone(preview["event_cursor"])
        self.assertEqual(preview["matching_paths"], ["src/task-template.spec.ts"])
        self.assertEqual(
            preview["after"],
            {
                "read_scopes": ["src/task-template.spec.ts"],
                "write_scopes": ["src/task-template.spec.ts"],
            },
        )
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)
        self.assertEqual(self._artifact_bytes(artifact_root), artifacts_before)
        self.assertEqual(target.read_bytes(), source_before)
        task = self.store.get_task(contract.task_id)
        node = next(item for item in task["nodes"] if item["node_id"] == "worker")
        self.assertEqual(node["write_scopes"], ["src/task-template*.ts"])

    def test_mcp_apply_replaces_scopes_and_preserves_indeterminate_history(self) -> None:
        contract, _ = self._make_indeterminate()
        before = self.store.get_task(contract.task_id)
        before_node = next(item for item in before["nodes"] if item["node_id"] == "worker")
        preview = self._control(**self._normalization_arguments(contract))

        applied = self._control(**self._normalization_arguments(
            contract,
            dry_run=False,
            confirm_scope_normalization=True,
            expected_file_sha256=preview["file_sha256"],
        ))

        self.assertTrue(applied["ok"])
        self.assertTrue(applied["changed"])
        self.assertIsInstance(applied["event_cursor"], int)
        after = self.store.get_task(contract.task_id)
        after_node = next(item for item in after["nodes"] if item["node_id"] == "worker")
        self.assertEqual(after["state"], "needs_approval")
        self.assertEqual(after["state_revision"], int(before["state_revision"]) + 1)
        self.assertEqual((after_node["state"], after_node["attempt"]), ("indeterminate", 1))
        self.assertEqual(after_node["result"], before_node["result"])
        self.assertEqual(after_node["read_scopes"], ["src/task-template.spec.ts"])
        self.assertEqual(after_node["write_scopes"], ["src/task-template.spec.ts"])
        event = next(
            item for item in self.store.read_events(task_id=contract.task_id)
            if item["cursor"] == applied["event_cursor"]
        )
        self.assertEqual(event["event_type"], "node.indeterminate_scope_normalized")
        self.assertTrue(event["payload"]["historical_result_unchanged"])
        self.assertFalse(event["payload"]["retrospective_compliance_claimed"])

    def test_stale_revision_is_a_zero_write_compare_and_set_failure(self) -> None:
        contract, _ = self._make_indeterminate()
        task = self.store.get_task(contract.task_id)
        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())

        with self.assertRaisesRegex(StateConflictError, "expected task revision"):
            self.store.normalize_indeterminate_scope(
                contract.task_id,
                "worker",
                expected_revision=int(task["state_revision"]) - 1,
                expected_attempt=1,
                scope_pattern="src/task-template*.ts",
                exact_path="src/task-template.spec.ts",
                reason="reject stale normalization",
                dry_run=True,
            )

        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)

    def test_apply_rejects_a_file_hash_changed_after_preview(self) -> None:
        contract, source = self._make_indeterminate()
        preview = self._control(**self._normalization_arguments(contract))
        target = source / "src" / "task-template.spec.ts"
        target.write_text("changed after preview", encoding="utf-8")
        before = self.store.get_task(contract.task_id)

        with self.assertRaisesRegex(ValueError, "expected_file_sha256"):
            self.store.normalize_indeterminate_scope(
                contract.task_id,
                "worker",
                expected_revision=int(before["state_revision"]),
                expected_attempt=1,
                scope_pattern="src/task-template*.ts",
                exact_path="src/task-template.spec.ts",
                reason="refuse stale source digest",
                expected_file_sha256=str(preview["file_sha256"]),
                confirm_scope_normalization=True,
            )

        self.assertEqual(self.store.get_task(contract.task_id), before)

    def test_apply_rejects_a_revision_change_between_preflight_and_commit(self) -> None:
        contract, _ = self._make_indeterminate()
        preview = self._control(**self._normalization_arguments(contract))
        original_prepare = self.store._prepare_indeterminate_scope_normalization
        calls = 0

        def prepare(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            prepared = original_prepare(*args, **kwargs)
            calls += 1
            if calls == 2:
                current = self.store.get_task(contract.task_id)
                self.store.transition_task(
                    contract.task_id,
                    "queued",
                    expected_revision=int(current["state_revision"]),
                )
            return prepared

        with patch.object(
            self.store,
            "_prepare_indeterminate_scope_normalization",
            side_effect=prepare,
        ):
            with self.assertRaisesRegex(StateConflictError, "expected task revision"):
                self.store.normalize_indeterminate_scope(
                    contract.task_id,
                    "worker",
                    expected_revision=int(preview["revision_before"]),
                    expected_attempt=1,
                    scope_pattern="src/task-template*.ts",
                    exact_path="src/task-template.spec.ts",
                    reason="reject a task state change after the source preflight",
                    expected_file_sha256=str(preview["file_sha256"]),
                    confirm_scope_normalization=True,
                )

        normalized = [
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.indeterminate_scope_normalized"
        ]
        self.assertEqual(normalized, [])

    @staticmethod
    def _artifact_bytes(root: Path) -> tuple[tuple[str, bytes], ...]:
        if not root.exists():
            return ()
        return tuple(sorted(
            (str(path.relative_to(root)), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        ))
