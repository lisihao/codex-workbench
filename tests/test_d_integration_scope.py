"""Durable Git/SQLite coverage for the fixed blocked D integration scope amendment."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench import d_integration_scope as scope
from codex_workbench.authority import authority_machine_id
from codex_workbench.config import WorkbenchConfig
from codex_workbench.d_integration_profile import (
    NODE_READ_ADDITIONS,
    NODE_WRITE_ADDITIONS,
    REQUIRED_PACKAGE_MARKERS,
    SCOPE_PROFILE_ID,
    TASK_ACCESS_ADDITIONS,
)
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_hash, canonical_json
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class DIntegrationScopeTests(unittest.TestCase):
    """Exercise the fixed A/B/C/D/E amendment against a retained Git worktree."""

    def setUp(self) -> None:
        # This fixture owns no executor process. Host /proc access is tested in
        # test_recovery_processes, not a prerequisite for these Git/SQLite cases.
        self.enterContext(patch(
            "codex_workbench.recovery_processes.source_process_ids", return_value=(),
        ))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self._initialize_repository()
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
        self.epoch = self.store.activate_coordinator("d-integration-scope", "fixture-machine")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self._create_blocked_lane()

    def test_unobservable_source_processes_still_reject_preview(self) -> None:
        from codex_workbench.recovery_processes import RecoveryProcessError

        before = self.store.get_task(self.contract.task_id)
        with patch("codex_workbench.recovery_processes.source_process_ids",
                   side_effect=RecoveryProcessError("source process inspection was incomplete")):
            with self.assertRaisesRegex(scope.IntegrationScopeAmendmentError, "cannot prove.*idle"):
                self._preview()
        self.assertEqual(self.store.get_task(self.contract.task_id), before)

    def _initialize_repository(self) -> None:
        self.repository.mkdir()
        self._run_git(self.repository, "init", "-b", "main")
        self._run_git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._run_git(self.repository, "config", "user.name", "Fixture")
        (self.repository / "src").mkdir()
        (self.repository / "legacy").mkdir()
        for name in ("a", "b", "c", "d"):
            (self.repository / "src" / f"{name}.ts").write_text(
                f"export const {name} = 0\n", encoding="utf-8"
            )
        (self.repository / "legacy" / "existing.md").write_text("legacy\n", encoding="utf-8")
        for relative, package_name in REQUIRED_PACKAGE_MARKERS.items():
            target = self.repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"name": package_name}) + "\n", encoding="utf-8")
        self._run_git(self.repository, "add", ".")
        self._run_git(self.repository, "commit", "-m", "fixture base")
        self.base_sha = self._run_git(self.repository, "rev-parse", "HEAD")

    @staticmethod
    def _run_git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _create_blocked_lane(self) -> None:
        self.contract = TaskContract(
            task_id="d-integration-scope",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="finish the fixed D integration lane after its blocked source is retained",
            allowed_scope=("src", "legacy"),
            acceptance_commands=("git diff --check",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        nodes = [
            NodeSpec("A", self.contract.task_id, "A", "fixture", "fixture", write_scopes=("src",), ordinal=1),
            NodeSpec("B", self.contract.task_id, "B", "fixture", "fixture", depends_on=("A",), write_scopes=("src",), ordinal=2),
            NodeSpec("C", self.contract.task_id, "C", "fixture", "fixture", depends_on=("B",), write_scopes=("src",), ordinal=3),
            NodeSpec(
                "D",
                self.contract.task_id,
                "D",
                "fixture",
                "fixture",
                depends_on=("A", "B", "C"),
                read_scopes=("src",),
                write_scopes=("src",),
                ordinal=4,
            ),
            NodeSpec(
                "E",
                self.contract.task_id,
                "E",
                "fixture",
                "fixture",
                depends_on=("A", "B", "C", "D"),
                read_scopes=("legacy/*.md",),
                write_scopes=(),
                verifier=True,
                ordinal=5,
            ),
        ]
        self.store.create_task(self.contract, nodes, "d-integration-scope-create")
        self.store.queue_task(self.contract.task_id)
        for node_id in ("A", "B", "C"):
            claimed = self.store.claim_ready_node("fixture-" + node_id, self.epoch)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["node_id"], node_id)
            worktree = self.worktrees.prepare(
                str(self.repository), self.base_sha, self.contract.task_id, node_id, int(claimed["attempt"])
            )
            self.store.assign_worktree(
                self.contract.task_id,
                node_id,
                str(worktree),
                attempt=int(claimed["attempt"]),
                coordinator_epoch=int(claimed["coordinator_epoch"]),
                lease_epoch=int(claimed["lease_epoch"]),
            )
            if node_id != "A":
                receipt = apply_accepted_ancestor_patches(
                    self.store.get_task(self.contract.task_id),
                    node_id,
                    worktree,
                    self.store.artifacts,
                    self.worktrees,
                )
                self.assertIsNotNone(receipt)
            path = worktree / "src" / f"{node_id.lower()}.ts"
            path.write_text(f"export const {node_id.lower()} = 1\n", encoding="utf-8")
            patch = self.worktrees.diff_patch(worktree, self.base_sha)
            self.store.settle_claimed(
                claimed,
                NodeResult(
                    "succeeded",
                    f"fixture {node_id} accepted",
                    artifacts={"patch": self.store.artifacts.put_bytes(patch, f"{node_id}.patch")},
                    actual_model="fixture",
                    result_kind="worker",
                    changed_paths=(f"src/{node_id.lower()}.ts",),
                ),
            )
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE nodes SET attempt = 4 WHERE task_id = ? AND node_id = ? AND state = 'accepted'",
                    (self.contract.task_id, node_id),
                )

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET attempt = 1 WHERE task_id = ? AND node_id = 'D' AND state = 'pending'",
                (self.contract.task_id,),
            )

        claimed_d = self.store.claim_ready_node("fixture-D", self.epoch)
        self.assertIsNotNone(claimed_d)
        assert claimed_d is not None
        self.assertEqual(claimed_d["node_id"], "D")
        self.source = self.worktrees.prepare(
            str(self.repository), self.base_sha, self.contract.task_id, "D", int(claimed_d["attempt"])
        )
        self.store.assign_worktree(
            self.contract.task_id,
            "D",
            str(self.source),
            attempt=int(claimed_d["attempt"]),
            coordinator_epoch=int(claimed_d["coordinator_epoch"]),
            lease_epoch=int(claimed_d["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(self.contract.task_id),
            "D",
            self.source,
            self.store.artifacts,
            self.worktrees,
        )
        self.assertIsNotNone(dependency_input)
        assert dependency_input is not None
        self.dependency_input_ref = self.store.artifacts.put_text(
            json.dumps(dependency_input.receipt, ensure_ascii=False, sort_keys=True),
            "dependency-input.json",
        )
        (self.source / "src" / "d.ts").write_text("export const d = 1\n", encoding="utf-8")
        self.store.settle_claimed(
            claimed_d,
            NodeResult(
                "blocked",
                "fixture D is blocked before integration source scopes are complete",
                artifacts={"dependency-input": self.dependency_input_ref},
                actual_model="fixture",
                result_kind="worker",
                changed_paths=("src/d.ts",),
            ),
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET attempt = 1 WHERE task_id = ? AND node_id = 'E' AND state = 'pending'",
                (self.contract.task_id,),
            )
        self.blocked = self.store.get_task(self.contract.task_id)
        self.assertEqual(self.blocked["state"], "blocked")
        self.arguments = {
            "task_id": self.contract.task_id,
            "node_id": "D",
            "expected_revision": self.blocked["state_revision"],
            "expected_attempt": 2,
            "expected_contract_hash": self.blocked["contract_hash"],
            "profile_id": SCOPE_PROFILE_ID,
            "reason": "add the fixed integration source and verifier read coverage",
            "dry_run": True,
        }

    def _preview(self) -> dict:
        return scope.amend_blocked_integration_scope(self.config, self.store, self.arguments)

    def _raw_nodes(self) -> dict[str, tuple[object, ...]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT node_id, spec_json, state, attempt, worker_id, worktree,
                       effective_executor, effective_model, started_at, settled_at,
                       result_json, recovery_json, coordinator_epoch, lease_epoch
                FROM nodes WHERE task_id = ? ORDER BY node_id
                """,
                (self.contract.task_id,),
            ).fetchall()
        return {str(row["node_id"]): tuple(row) for row in rows}

    def _raw_allocations(self) -> tuple[tuple[object, ...], ...]:
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT allocation_id, task_id, node_id, attempt, repository, base_sha,
                       branch, current_path, state, node_result_json
                FROM worktree_allocations WHERE task_id = ? ORDER BY allocation_id
                """,
                (self.contract.task_id,),
            ).fetchall()
        return tuple(tuple(row) for row in rows)

    def _raw_contract_row(self) -> tuple[object, ...]:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT contract_json, contract_hash, state, state_revision, blocker, verdict
                FROM tasks WHERE task_id = ?
                """,
                (self.contract.task_id,),
            ).fetchone()
        assert row is not None
        return tuple(row)

    def _replace_contract(self, replacement: dict) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET contract_json = ?, contract_hash = ?, state_revision = state_revision + 1
                WHERE task_id = ?
                """,
                (canonical_json(replacement), canonical_hash(replacement), self.contract.task_id),
            )
        current = self.store.get_task(self.contract.task_id)
        self.arguments.update(
            expected_revision=current["state_revision"],
            expected_contract_hash=current["contract_hash"],
        )

    def test_schema_is_strict_and_targets_only_literal_d(self) -> None:
        schema = scope._ARGUMENT_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["node_id"]["const"], "D")
        self.assertEqual(schema["properties"]["profile_id"]["const"], SCOPE_PROFILE_ID)
        self.assertIn("expected_fingerprint", schema["properties"])
        with self.assertRaises(ValueError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "allowed_scope": ["."]},
            )
        with self.assertRaises(ValueError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "node_id": "other"},
            )

    def test_preview_is_read_only_and_separates_exact_additions(self) -> None:
        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        artifacts_before = tuple(sorted(self.store.artifacts.root.rglob("*")))
        marker_before = {
            path: (self.source / path).read_bytes() for path in REQUIRED_PACKAGE_MARKERS
        }
        source_before = (self.source / "src" / "d.ts").read_bytes()

        preview = self._preview()

        self.assertTrue(preview["dry_run"])
        self.assertRegex(preview["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(preview["scope_changes"]["task_allowed_scope"]["added"], list(TASK_ACCESS_ADDITIONS))
        self.assertEqual(preview["scope_changes"]["D"]["read_scopes"]["added"], list(NODE_READ_ADDITIONS))
        self.assertEqual(preview["scope_changes"]["D"]["write_scopes"]["added"], list(NODE_WRITE_ADDITIONS))
        self.assertEqual(preview["scope_changes"]["E"]["read_scopes"]["before"], ["legacy/*.md"])
        self.assertEqual(preview["scope_changes"]["E"]["read_scopes"]["added"], list(NODE_READ_ADDITIONS))
        self.assertEqual(preview["scope_changes"]["E"]["write_scopes"]["after"], [])
        self.assertTrue(preview["nodes_would_change"])
        self.assertFalse(preview["nodes_changed"])
        self.assertFalse(preview["queued"])
        self.assertFalse(preview["commands_executed"])
        self.assertFalse(preview["source_written"])
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)
        self.assertEqual(tuple(sorted(self.store.artifacts.root.rglob("*"))), artifacts_before)
        self.assertEqual((self.source / "src" / "d.ts").read_bytes(), source_before)
        self.assertEqual(
            {path: (self.source / path).read_bytes() for path in REQUIRED_PACKAGE_MARKERS},
            marker_before,
        )

    def test_apply_preserves_accepted_results_and_all_other_fields(self) -> None:
        preview = self._preview()
        before = self.store.get_task(self.contract.task_id)
        nodes_before = self._raw_nodes()
        allocations_before = self._raw_allocations()
        result = scope.amend_blocked_integration_scope(
            self.config,
            self.store,
            {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
        )
        after = self.store.get_task(self.contract.task_id)
        nodes_after = self._raw_nodes()

        self.assertFalse(result["dry_run"])
        self.assertEqual(after["state"], "blocked")
        self.assertEqual(after["state_revision"], before["state_revision"] + 1)
        before_contract = dict(before["contract"])
        after_contract = dict(after["contract"])
        self.assertEqual(
            {key: value for key, value in after_contract.items() if key != "allowed_scope"},
            {key: value for key, value in before_contract.items() if key != "allowed_scope"},
        )
        self.assertEqual(
            after_contract["allowed_scope"],
            [*before_contract["allowed_scope"], *TASK_ACCESS_ADDITIONS],
        )
        after_nodes = {node["node_id"]: node for node in after["nodes"]}
        self.assertEqual(
            after_nodes["D"]["read_scopes"],
            ["src", *NODE_READ_ADDITIONS],
        )
        self.assertEqual(
            after_nodes["D"]["write_scopes"],
            ["src", *NODE_WRITE_ADDITIONS],
        )
        self.assertEqual(
            after_nodes["E"]["read_scopes"],
            ["legacy/*.md", *NODE_READ_ADDITIONS],
        )
        self.assertEqual(after_nodes["E"]["write_scopes"], [])
        for node_id in ("A", "B", "C"):
            self.assertEqual(nodes_after[node_id], nodes_before[node_id])
            self.assertEqual(after_nodes[node_id]["state"], "accepted")
            self.assertEqual(after_nodes[node_id]["result"], before["nodes"]["ABC".index(node_id)]["result"])
        self.assertEqual(
            nodes_after["D"][2:14],
            nodes_before["D"][2:14],
        )
        self.assertEqual(
            nodes_after["E"][2:14],
            nodes_before["E"][2:14],
        )
        self.assertEqual(self._raw_allocations(), allocations_before)
        event = self.store.read_events(task_id=self.contract.task_id)[-1]
        self.assertEqual(event["event_type"], "task.blocked_integration_scope_amended")
        self.assertEqual(event["payload"]["old_contract"], before_contract)
        self.assertEqual(event["payload"]["new_contract"], after_contract)
        self.assertEqual(event["payload"]["d_scope_change"]["new_spec"], {
            key: value for key, value in after_nodes["D"].items()
            if key not in {
                "state", "attempt", "worker_id", "worktree", "effective_executor",
                "effective_model", "coordinator_epoch", "lease_epoch", "started_at",
                "settled_at", "updated_at", "result", "admission_wait",
            }
        })
        with self.assertRaises(StateConflictError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
            )

    def test_source_drift_rejects_the_preview_fingerprint_without_mutation(self) -> None:
        preview = self._preview()
        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        (self.source / "src" / "d.ts").write_text("export const d = 2\n", encoding="utf-8")

        with self.assertRaises(StateConflictError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
            )
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)

    def test_contract_and_spec_drift_are_rejected(self) -> None:
        preview = self._preview()
        changed_contract = {
            **self.blocked["contract"],
            "objective": "a changed contract must need a fresh bounded preview",
        }
        self._replace_contract(changed_contract)
        with self.assertRaises(StateConflictError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
            )

        preview = self._preview()
        current = self.store.get_task(self.contract.task_id)
        current_e = next(node for node in current["nodes"] if node["node_id"] == "E")
        changed_e = {
            key: value for key, value in current_e.items()
            if key not in {
                "state", "attempt", "worker_id", "worktree", "effective_executor", "effective_model",
                "coordinator_epoch", "lease_epoch", "started_at", "settled_at", "updated_at", "result", "admission_wait",
            }
        }
        changed_e["read_scopes"] = [*changed_e["read_scopes"], "legacy/other/*.md"]
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET spec_json = ? WHERE task_id = ? AND node_id = 'E'",
                (canonical_json(changed_e), self.contract.task_id),
            )
        with self.assertRaises(StateConflictError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
            )

    def test_forbidden_fixed_scope_is_rejected_without_reinterpreting_legacy_e_wildcard(self) -> None:
        changed_contract = {
            **self.blocked["contract"],
            "forbidden_scope": [NODE_READ_ADDITIONS[0]],
        }
        self._replace_contract(changed_contract)
        with self.assertRaises(scope.IntegrationScopeAmendmentError):
            self._preview()

    def test_fixed_scope_path_facts_allow_missing_output_parent_but_reject_symlink_escape(self) -> None:
        root = self.source.resolve()
        facts = scope._fixed_scope_path_facts(root)
        self.assertEqual(facts["scripts/gen-cordis-catalog.ts"]["state"], "missing")
        self.assertEqual(
            facts["scripts/gen-cordis-catalog.ts"]["first_missing_component"],
            "scripts",
        )
        escaped = self.root / "escaped-fixed-scope"
        escaped.mkdir()
        (self.source / "scripts").symlink_to(escaped, target_is_directory=True)
        with self.assertRaisesRegex(scope.IntegrationScopeAmendmentError, "symlink"):
            scope._fixed_scope_path_facts(root)

    def test_e_running_after_preview_rejects_apply_without_contract_mutation(self) -> None:
        preview = self._preview()
        contract_before = self._raw_contract_row()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'running' WHERE task_id = ? AND node_id = 'E'",
                (self.contract.task_id,),
            )
        with self.assertRaises(StateConflictError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
            )
        self.assertEqual(self._raw_contract_row(), contract_before)

    def test_e_allocation_after_preview_rejects_apply_without_contract_mutation(self) -> None:
        preview = self._preview()
        contract_before = self._raw_contract_row()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO worktree_allocations(
                    allocation_id, task_id, node_id, attempt, repository, base_sha,
                    branch, current_path, state, created_at, updated_at
                ) VALUES(?, ?, 'E', 1, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    "fixture-e-active-allocation",
                    self.contract.task_id,
                    str(self.repository),
                    self.base_sha,
                    WorktreeManager.branch_name(self.contract.task_id, "E", 1),
                    str(self.source),
                    "2026-09-13T00:00:00+00:00",
                    "2026-09-13T00:00:00+00:00",
                ),
            )
        with self.assertRaises(StateConflictError):
            scope.amend_blocked_integration_scope(
                self.config,
                self.store,
                {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
            )
        self.assertEqual(self._raw_contract_row(), contract_before)

    def test_event_failure_rolls_back_task_and_both_scope_updates_atomically(self) -> None:
        preview = self._preview()
        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        with patch.object(WorkbenchStore, "_event", side_effect=RuntimeError("injected event failure")):
            with self.assertRaisesRegex(RuntimeError, "injected event failure"):
                scope.amend_blocked_integration_scope(
                    self.config,
                    self.store,
                    {**self.arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
                )
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)


if __name__ == "__main__":
    unittest.main()
