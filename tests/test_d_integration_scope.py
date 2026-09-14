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
from codex_workbench.accepted_source_repair import prepare_accepted_source_repair
from codex_workbench.blocked_source_repair import blocked_source_repair
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
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.recovery_processes import RecoveryProcessError
from codex_workbench.service import Coordinator
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

    def _create_real_retained_verifier_lane(self) -> tuple[TaskContract, dict, Path]:
        """Create E-a1 through native verifier failure/reset before D-a2 blocks."""

        task_id = "retained-verifier-fixture"
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="retain a settled verifier allocation while D needs a scoped repair",
            allowed_scope=("src", "legacy"),
            acceptance_commands=("git diff --check",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        nodes = [
            NodeSpec("A", task_id, "A", "fixture", "fixture", write_scopes=("src",), ordinal=1),
            NodeSpec("B", task_id, "B", "fixture", "fixture", depends_on=("A",), write_scopes=("src",), ordinal=2),
            NodeSpec("C", task_id, "C", "fixture", "fixture", depends_on=("B",), write_scopes=("src",), ordinal=3),
            NodeSpec(
                "D", task_id, "D", "fixture", "fixture", depends_on=("A", "B", "C"),
                read_scopes=("src",), write_scopes=("src",), ordinal=4,
            ),
            NodeSpec(
                "E", task_id, "E", "fixture", "fixture", depends_on=("A", "B", "C", "D"),
                read_scopes=("legacy/*.md",), write_scopes=(), verifier=True, ordinal=5,
            ),
        ]
        self.store.create_task(contract, nodes, task_id + "-create")
        self.store.queue_task(task_id)

        def accept_worker(node_id: str, relative: str, content: str) -> None:
            claimed = self.store.claim_ready_node("retained-" + node_id, self.epoch)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["node_id"], node_id)
            worktree = self.worktrees.prepare(
                str(self.repository), self.base_sha, task_id, node_id, int(claimed["attempt"])
            )
            self.store.assign_worktree(
                task_id,
                node_id,
                str(worktree),
                attempt=int(claimed["attempt"]),
                coordinator_epoch=int(claimed["coordinator_epoch"]),
                lease_epoch=int(claimed["lease_epoch"]),
            )
            dependency_input = apply_accepted_ancestor_patches(
                self.store.get_task(task_id), node_id, worktree, self.store.artifacts, self.worktrees,
            )
            input_tree = self.base_sha
            artifacts: dict[str, str] = {}
            if dependency_input is not None:
                input_tree = dependency_input.input_tree_sha
                artifacts["dependency-input"] = self.store.artifacts.put_text(
                    canonical_json(dependency_input.receipt), node_id + "-input.json",
                )
            target = worktree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            artifacts["patch"] = self.store.artifacts.put_bytes(
                self.worktrees.diff_patch(worktree, input_tree), node_id + ".patch",
            )
            self.store.settle_claimed(
                claimed,
                NodeResult(
                    "succeeded", node_id + " accepted", artifacts=artifacts,
                    actual_model="fixture", result_kind="worker", checks=("fixture",),
                    changed_paths=(relative,),
                ),
            )

        accept_worker("A", "src/retained-a.ts", "export const retainedA = 1\n")
        accept_worker("B", "src/retained-b.ts", "export const retainedB = 1\n")
        accept_worker("C", "src/retained-c.ts", "export const retainedC = 1\n")
        accept_worker("D", "src/retained-d.ts", "export const retainedD = 1\n")

        verifier = self.store.claim_ready_node("retained-E", self.epoch)
        self.assertIsNotNone(verifier)
        assert verifier is not None
        self.assertEqual((verifier["node_id"], verifier["attempt"]), ("E", 1))
        verifier_worktree = self.worktrees.prepare(
            str(self.repository), self.base_sha, task_id, "E", int(verifier["attempt"])
        )
        self.store.assign_worktree(
            task_id,
            "E",
            str(verifier_worktree),
            attempt=int(verifier["attempt"]),
            coordinator_epoch=int(verifier["coordinator_epoch"]),
            lease_epoch=int(verifier["lease_epoch"]),
        )
        evidence = self.store.artifacts.put_text("retained verifier evidence\n", "retained-E.log")
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "D requires scoped integration repair",
                artifacts={"test-log": evidence},
                actual_model="fixture",
                result_kind="verifier",
                checks=("fixture verifier",),
                evidence=(evidence,),
                verdict="needs_fix",
                repair_node_ids=("D",),
            ),
        )
        after_failure = self.store.get_task(task_id)
        e_after_failure = next(node for node in after_failure["nodes"] if node["node_id"] == "E")
        self.assertEqual((e_after_failure["state"], e_after_failure["attempt"]), ("pending", 1))
        self.assertIsNone(e_after_failure["worktree"])
        self.assertIsNone(e_after_failure["result"])

        claimed_d = self.store.claim_ready_node("retained-D-repair", self.epoch)
        self.assertIsNotNone(claimed_d)
        assert claimed_d is not None
        self.assertEqual((claimed_d["node_id"], claimed_d["attempt"]), ("D", 2))
        binding = claimed_d.get("accepted_source_repair")
        self.assertIsInstance(binding, dict)
        prepared = prepare_accepted_source_repair(self.store, binding, self.worktrees)
        self.store.assign_worktree(
            task_id,
            "D",
            str(prepared.worktree),
            attempt=int(claimed_d["attempt"]),
            coordinator_epoch=int(claimed_d["coordinator_epoch"]),
            lease_epoch=int(claimed_d["lease_epoch"]),
            recovery_preflight=prepared.receipt,
        )
        blocked_input = self.store.artifacts.put_text(
            canonical_json(prepared.dependency_input.receipt), "retained-D-repair-input.json",
        )
        self.store.settle_claimed(
            claimed_d,
            NodeResult(
                "blocked",
                "D remains blocked pending fixed integration scope coverage",
                artifacts={"dependency-input": blocked_input},
                actual_model="fixture",
                result_kind="worker",
                changed_paths=("src/retained-d.ts",),
            ),
        )
        blocked = self.store.get_task(task_id)
        self.assertEqual(blocked["state"], "blocked")
        return contract, {
            "task_id": task_id,
            "node_id": "D",
            "expected_revision": blocked["state_revision"],
            "expected_attempt": 2,
            "expected_contract_hash": blocked["contract_hash"],
            "profile_id": SCOPE_PROFILE_ID,
            "reason": "bind the retained verifier allocation before amending D scope",
            "dry_run": True,
        }, prepared.worktree

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

    def _repair_blocked_consumer_owners(
        self,
        contract: TaskContract,
        *,
        conflicting_d_path: bool = False,
    ) -> dict[str, str]:
        instructions = {
            "A": "repair A documentation and type findings only",
            "B": "repair B documentation and type findings only",
            "C": "refresh C on the repaired accepted ancestor input",
        }
        blocked = self.store.get_task(contract.task_id)
        receipt = self.store.schedule_blocked_consumer_owner_repairs(
            contract.task_id,
            "D",
            ["A", "B", "C"],
            instructions,
            expected_revision=int(blocked["state_revision"]),
            expected_attempt=2,
            reason="D found exact upstream owner defects before E became reachable",
        )
        self.assertTrue(receipt["blocked_consumer_preserved"])
        owner_wait = self.store.blocked_owner_repair_wait(
            contract.task_id, "D", 2
        )
        assert owner_wait is not None
        self.assertTrue(owner_wait["pending"])
        self.assertEqual(
            [item["node_id"] for item in owner_wait["dependencies"]],
            ["A", "B", "C"],
        )
        for owner in ("A", "B", "C"):
            claimed = self.store.claim_ready_node("repair-" + owner, self.epoch)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), (owner, 2))
            self.assertEqual(claimed["steering"], (instructions[owner],))
            binding = claimed.get("accepted_source_repair")
            self.assertIsInstance(binding, dict)
            prepared = prepare_accepted_source_repair(self.store, binding, self.worktrees)
            self.store.assign_worktree(
                contract.task_id,
                owner,
                str(prepared.worktree),
                attempt=int(claimed["attempt"]),
                coordinator_epoch=int(claimed["coordinator_epoch"]),
                lease_epoch=int(claimed["lease_epoch"]),
                recovery_preflight=prepared.receipt,
            )
            relative = f"src/retained-{owner.lower()}.ts"
            (prepared.worktree / relative).write_text(
                f"export const retained{owner} = 2\n", encoding="utf-8"
            )
            changed_paths = [relative]
            if owner == "A" and conflicting_d_path:
                (prepared.worktree / "src/retained-d.ts").write_text(
                    "export const retainedD = 99\n", encoding="utf-8"
                )
                changed_paths.append("src/retained-d.ts")
            dependency_ref = self.store.artifacts.put_text(
                canonical_json(prepared.dependency_input.receipt),
                owner + "-repair-input.json",
            )
            patch_ref = self.store.artifacts.put_bytes(
                self.worktrees.diff_patch(
                    prepared.worktree, prepared.dependency_input.input_tree_sha
                ),
                owner + "-repair.patch",
            )
            self.store.settle_claimed(
                claimed,
                NodeResult(
                    "succeeded",
                    owner + " owner repair accepted",
                    artifacts={"patch": patch_ref, "dependency-input": dependency_ref},
                    actual_model="fixture",
                    result_kind="worker",
                    checks=("fixture owner repair",),
                    changed_paths=tuple(changed_paths),
                ),
            )
        settled_wait = self.store.blocked_owner_repair_wait(
            contract.task_id, "D", 2
        )
        assert settled_wait is not None
        self.assertFalse(settled_wait["pending"])
        self.assertTrue(
            all(item["satisfied"] for item in settled_wait["dependencies"])
        )
        return instructions

    def _queue_rebased_d_source(self, contract: TaskContract, request_id: str) -> dict:
        task = self.store.get_task(contract.task_id)
        NodeRecoveryStore(self.store).configure_policy(
            contract.task_id,
            RecoveryPolicy(
                enabled=True,
                allowed_actions=("repair_source",),
                max_action_attempts=3,
            ),
            expected_task_revision=int(task["state_revision"]),
            actor="d-integration-rebase-fixture",
        )
        arguments = {
            "task_id": contract.task_id,
            "node_id": "D",
            "expected_revision": int(task["state_revision"]),
            "expected_attempt": 2,
            "expected_contract_hash": task["contract_hash"],
            "request_id": request_id,
            "reason": "replay only the retained D delta on refreshed accepted ancestors",
        }
        preview = blocked_source_repair(self.store, **arguments, dry_run=True)
        self.assertTrue(preview["refresh_accepted_ancestors"])
        return blocked_source_repair(
            self.store,
            **arguments,
            dry_run=False,
            expected_fingerprint=preview["fingerprint"],
        )

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
        self.assertIsNone(preview["source"]["retained_verifier"])
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

    def test_source_change_during_identity_observation_rejects_preview(self) -> None:
        target = self.source / "src" / "d.ts"
        original_identity = scope._source_identity
        identity_calls = 0

        def observe_then_mutate(binding):
            nonlocal identity_calls
            identity = original_identity(binding)
            identity_calls += 1
            if identity_calls == 1:
                target.write_text("export const d = 2\n", encoding="utf-8")
            return identity

        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        with patch.object(scope, "_source_identity", side_effect=observe_then_mutate):
            with self.assertRaisesRegex(
                scope.IntegrationScopeAmendmentError,
                "source delta changed while it was inspected",
            ):
                self._preview()
        self.assertEqual(identity_calls, 1)
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)

    def test_identity_change_during_durable_recheck_rejects_preview(self) -> None:
        marker = self.source / next(iter(REQUIRED_PACKAGE_MARKERS))
        package_name = REQUIRED_PACKAGE_MARKERS[next(iter(REQUIRED_PACKAGE_MARKERS))]
        original_binding = scope._durable_binding
        binding_calls = 0

        def observe_then_mutate(store, arguments):
            nonlocal binding_calls
            binding = original_binding(store, arguments)
            binding_calls += 1
            if binding_calls == 2:
                marker.write_text(
                    json.dumps({"name": package_name, "identity_drift": True}) + "\n",
                    encoding="utf-8",
                )
            return binding

        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        with patch.object(scope, "_durable_binding", side_effect=observe_then_mutate):
            with self.assertRaisesRegex(
                StateConflictError,
                "source identity changed during preview",
            ):
                self._preview()
        self.assertEqual(binding_calls, 2)
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

    def test_native_retained_verifier_allocation_accepts_legacy_event_and_pretty_result(self) -> None:
        contract, arguments, source = self._create_real_retained_verifier_lane()
        with self.store.transaction() as connection:
            repair = connection.execute(
                """
                SELECT cursor, payload_json FROM events
                WHERE task_id = ? AND node_id = 'E' AND event_type = 'task.repair_scheduled'
                """,
                (contract.task_id,),
            ).fetchone()
            assert repair is not None
            payload = json.loads(str(repair["payload_json"]))
            self.assertIn("repair_node_ids", payload)
            payload.pop("repair_node_ids")
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (canonical_json(payload), repair["cursor"]),
            )
            allocation = connection.execute(
                """
                SELECT allocation_id, node_result_json FROM worktree_allocations
                WHERE task_id = ? AND node_id = 'E' AND attempt = 1
                """,
                (contract.task_id,),
            ).fetchone()
            assert allocation is not None
            pretty_result = json.dumps(
                json.loads(str(allocation["node_result_json"])), indent=2, sort_keys=True,
            )
            connection.execute(
                "UPDATE worktree_allocations SET node_result_json = ? WHERE allocation_id = ?",
                (pretty_result, allocation["allocation_id"]),
            )

        preview = scope.amend_blocked_integration_scope(self.config, self.store, arguments)
        with self.store.connection() as connection:
            e_allocation_before = tuple(connection.execute(
                "SELECT * FROM worktree_allocations WHERE task_id = ? AND node_id = 'E' AND attempt = 1",
                (contract.task_id,),
            ).fetchone())
            e_events_before = tuple(
                tuple(row) for row in connection.execute(
                    """
                    SELECT cursor, event_type, payload_json, created_at FROM events
                    WHERE task_id = ? AND node_id = 'E' ORDER BY cursor
                    """,
                    (contract.task_id,),
                ).fetchall()
            )
            ancestors_before = {
                str(row["node_id"]): tuple(row)
                for row in connection.execute(
                    """
                    SELECT node_id, spec_json, state, attempt, worktree, result_json, recovery_json
                    FROM nodes WHERE task_id = ? AND node_id IN ('A', 'B', 'C') ORDER BY node_id
                    """,
                    (contract.task_id,),
                ).fetchall()
            }
        source_before = (source / "src" / "retained-d.ts").read_bytes()
        retained = preview["source"]["retained_verifier"]
        self.assertEqual(retained["attempt"], 1)
        self.assertEqual(retained["idle_observation"], "idle")
        self.assertEqual(set(retained["event_cursors"]), {"allocated", "failed", "repair"})
        self.assertIn("result_sha256", retained)
        self.assertNotIn("node_result_json", retained)
        self.assertNotIn("payload", retained)

        applied = scope.amend_blocked_integration_scope(
            self.config,
            self.store,
            {**arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
        )
        after = self.store.get_task(contract.task_id)
        after_nodes = {node["node_id"]: node for node in after["nodes"]}
        self.assertFalse(applied["dry_run"])
        self.assertEqual((after["state"], after["state_revision"]), (
            "blocked", arguments["expected_revision"] + 1,
        ))
        self.assertEqual(after_nodes["D"]["read_scopes"], ["src", *NODE_READ_ADDITIONS])
        self.assertEqual(after_nodes["D"]["write_scopes"], ["src", *NODE_WRITE_ADDITIONS])
        self.assertEqual(after_nodes["E"]["read_scopes"], ["legacy/*.md", *NODE_READ_ADDITIONS])
        self.assertEqual(after_nodes["E"]["write_scopes"], [])
        self.assertEqual(
            (after_nodes["E"]["state"], after_nodes["E"]["attempt"], after_nodes["E"]["result"]),
            ("pending", 1, None),
        )
        self.assertEqual((source / "src" / "retained-d.ts").read_bytes(), source_before)
        with self.store.connection() as connection:
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT * FROM worktree_allocations WHERE task_id = ? AND node_id = 'E' AND attempt = 1",
                    (contract.task_id,),
                ).fetchone()),
                e_allocation_before,
            )
            self.assertEqual(
                tuple(
                    tuple(row) for row in connection.execute(
                        """
                        SELECT cursor, event_type, payload_json, created_at FROM events
                        WHERE task_id = ? AND node_id = 'E' ORDER BY cursor
                        """,
                        (contract.task_id,),
                    ).fetchall()
                ),
                e_events_before,
            )
            ancestors_after = {
                str(row["node_id"]): tuple(row)
                for row in connection.execute(
                    """
                    SELECT node_id, spec_json, state, attempt, worktree, result_json, recovery_json
                    FROM nodes WHERE task_id = ? AND node_id IN ('A', 'B', 'C') ORDER BY node_id
                    """,
                    (contract.task_id,),
                ).fetchall()
            }
        self.assertEqual(ancestors_after, ancestors_before)

    def test_native_retained_verifier_idle_failure_rejects_preview_without_mutation(self) -> None:
        contract, arguments, _ = self._create_real_retained_verifier_lane()
        before = self.store.get_task(contract.task_id)
        with patch(
            "codex_workbench.d_integration_scope.assert_recovery_source_idle",
            side_effect=RecoveryProcessError("fixture E process remains active"),
        ):
            with self.assertRaisesRegex(StateConflictError, "retained verifier E is idle"):
                scope.amend_blocked_integration_scope(self.config, self.store, arguments)
        self.assertEqual(self.store.get_task(contract.task_id), before)

    def test_retained_verifier_allocation_result_and_event_drift_reject_apply(self) -> None:
        contract, arguments, _ = self._create_real_retained_verifier_lane()
        preview = scope.amend_blocked_integration_scope(self.config, self.store, arguments)
        apply = {**arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]}
        contract_before = self.store.get_task(contract.task_id)
        with self.store.connection() as connection:
            allocation = connection.execute(
                """
                SELECT allocation_id, node_result_json, updated_at FROM worktree_allocations
                WHERE task_id = ? AND node_id = 'E' AND attempt = 1
                """,
                (contract.task_id,),
            ).fetchone()
            repair = connection.execute(
                """
                SELECT cursor, payload_json FROM events
                WHERE task_id = ? AND node_id = 'E' AND event_type = 'task.repair_scheduled'
                """,
                (contract.task_id,),
            ).fetchone()
        assert allocation is not None and repair is not None
        original_result = str(allocation["node_result_json"])
        original_updated_at = str(allocation["updated_at"])
        original_repair = str(repair["payload_json"])

        with self.subTest("allocation"):
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE worktree_allocations SET updated_at = ? WHERE allocation_id = ?",
                    ("2026-09-13T00:00:00+00:00", allocation["allocation_id"]),
                )
            with self.assertRaisesRegex(StateConflictError, "fingerprint changed"):
                scope.amend_blocked_integration_scope(self.config, self.store, apply)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE worktree_allocations SET updated_at = ? WHERE allocation_id = ?",
                    (original_updated_at, allocation["allocation_id"]),
                )

        with self.subTest("result"):
            pretty_result = json.dumps(json.loads(original_result), indent=2, sort_keys=True)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE worktree_allocations SET node_result_json = ? WHERE allocation_id = ?",
                    (pretty_result, allocation["allocation_id"]),
                )
            with self.assertRaisesRegex(StateConflictError, "fingerprint changed"):
                scope.amend_blocked_integration_scope(self.config, self.store, apply)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE worktree_allocations SET node_result_json = ? WHERE allocation_id = ?",
                    (original_result, allocation["allocation_id"]),
                )

        with self.subTest("event"):
            changed_repair = json.loads(original_repair)
            changed_repair["feedback_steering_id"] = "changed-after-preview"
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE cursor = ?",
                    (canonical_json(changed_repair), repair["cursor"]),
                )
            with self.assertRaisesRegex(StateConflictError, "fingerprint changed"):
                scope.amend_blocked_integration_scope(self.config, self.store, apply)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE cursor = ?",
                    (original_repair, repair["cursor"]),
                )

        self.assertEqual(self.store.get_task(contract.task_id), contract_before)

    def test_native_retained_verifier_rejects_future_allocation_and_later_unresolved_event(self) -> None:
        contract, arguments, _ = self._create_real_retained_verifier_lane()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO worktree_allocations(
                    allocation_id, task_id, node_id, attempt, repository, base_sha,
                    branch, current_path, state, created_at, updated_at
                ) VALUES(?, ?, 'E', 2, ?, ?, ?, ?, 'superseded', ?, ?)
                """,
                (
                    "retained-e-future",
                    contract.task_id,
                    str(self.repository),
                    self.base_sha,
                    WorktreeManager.branch_name(contract.task_id, "E", 2),
                    str(self.worktrees.worktree_path(contract.task_id, "E", 1)),
                    "2026-09-13T00:00:00+00:00",
                    "2026-09-13T00:00:00+00:00",
                ),
            )
        with self.assertRaisesRegex(StateConflictError, "future allocation"):
            scope.amend_blocked_integration_scope(self.config, self.store, arguments)
        with self.store.transaction() as connection:
            connection.execute(
                "DELETE FROM worktree_allocations WHERE allocation_id = 'retained-e-future'"
            )
            WorkbenchStore._event(
                connection,
                "node.indeterminate",
                contract.task_id,
                "E",
                {"attempt": 2, "result": {"status": "indeterminate"}},
            )
        with self.assertRaisesRegex(StateConflictError, "later lifecycle"):
            scope.amend_blocked_integration_scope(self.config, self.store, arguments)

    def test_blocked_consumer_repairs_owners_then_replays_only_d_delta_before_e(self) -> None:
        contract, arguments, d_source = self._create_real_retained_verifier_lane()
        preview = scope.amend_blocked_integration_scope(self.config, self.store, arguments)
        scope.amend_blocked_integration_scope(
            self.config,
            self.store,
            {**arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
        )
        d_result_before = next(
            node for node in self.store.get_task(contract.task_id)["nodes"]
            if node["node_id"] == "D"
        )["result"]
        d_source_before = (d_source / "src/retained-d.ts").read_bytes()

        self._repair_blocked_consumer_owners(contract)
        after_owners = self.store.get_task(contract.task_id)
        d_after_owners = next(
            node for node in after_owners["nodes"] if node["node_id"] == "D"
        )
        self.assertEqual((after_owners["state"], d_after_owners["state"]), ("blocked", "blocked"))
        self.assertEqual(d_after_owners["result"], d_result_before)
        self.assertEqual((d_source / "src/retained-d.ts").read_bytes(), d_source_before)

        queued = self._queue_rebased_d_source(contract, "rebase-d-after-owners")
        self.assertTrue(queued["refresh_accepted_ancestors"])
        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        observed: dict[str, object] = {}
        try:
            claimed_d = coordinator._claim_next_ready_node("rebased-D")
            self.assertIsNotNone(claimed_d)
            assert claimed_d is not None
            self.assertEqual((claimed_d["node_id"], claimed_d["attempt"]), ("D", 3))

            def execute_d(request: object) -> NodeResult:
                worktree = request.worktree  # type: ignore[attr-defined]
                assert worktree is not None
                observed["A"] = (worktree / "src/retained-a.ts").read_text(encoding="utf-8")
                observed["B"] = (worktree / "src/retained-b.ts").read_text(encoding="utf-8")
                observed["C"] = (worktree / "src/retained-c.ts").read_text(encoding="utf-8")
                observed["D"] = (worktree / "src/retained-d.ts").read_text(encoding="utf-8")
                observed["ancestors"] = [
                    (item["node_id"], item["attempt"])
                    for item in request.input_receipt["ancestors"]  # type: ignore[attr-defined]
                ]
                return NodeResult(
                    "succeeded", "D continued on refreshed owners", checks=("fixture D",)
                )

            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute_d
                coordinator._execute_claimed(claimed_d)
            self.assertEqual(
                observed,
                {
                    "A": "export const retainedA = 2\n",
                    "B": "export const retainedB = 2\n",
                    "C": "export const retainedC = 2\n",
                    "D": "export const retainedD = 1\n",
                    "ancestors": [("A", 2), ("B", 2), ("C", 2)],
                },
            )
            verifier = coordinator._claim_next_ready_node("final-E")
            self.assertIsNotNone(verifier)
            assert verifier is not None
            self.assertEqual(verifier["node_id"], "E")
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.return_value = NodeResult(
                    "succeeded",
                    "E accepted the owner repairs and rebased D delta",
                    actual_model="fixture",
                    result_kind="verifier",
                    checks=("fixture E",),
                    verdict="accepted",
                )
                coordinator._execute_claimed(verifier)
        finally:
            coordinator._pool.shutdown(wait=True)

        accepted = self.store.get_task(contract.task_id)
        self.assertEqual(accepted["state"], "accepted")
        d_result = next(node for node in accepted["nodes"] if node["node_id"] == "D")["result"]
        dependency_ref = d_result["artifacts"]["dependency-input"]
        dependency_receipt = json.loads(
            self.store.artifacts.verify(dependency_ref).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [(item["node_id"], item["attempt"]) for item in dependency_receipt["ancestors"]],
            [("A", 2), ("B", 2), ("C", 2)],
        )

    def test_rebased_d_accepts_owner_attempts_beyond_first_repair_target(self) -> None:
        contract, arguments, _d_source = self._create_real_retained_verifier_lane()
        preview = scope.amend_blocked_integration_scope(
            self.config, self.store, arguments
        )
        scope.amend_blocked_integration_scope(
            self.config,
            self.store,
            {**arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
        )
        self._repair_blocked_consumer_owners(contract)
        with self.store.transaction() as connection:
            for owner_id in ("A", "B", "C"):
                connection.execute(
                    "UPDATE nodes SET attempt = attempt + 2 WHERE task_id = ? AND node_id = ?",
                    (contract.task_id, owner_id),
                )
        wait = self.store.blocked_owner_repair_wait(contract.task_id, "D", 2)
        assert wait is not None
        self.assertFalse(wait["pending"])
        self.assertTrue(all(item["satisfied"] for item in wait["dependencies"]))

        queued = self._queue_rebased_d_source(
            contract,
            "rebase-d-after-multiple-owner-attempts",
        )

        self.assertTrue(queued["refresh_accepted_ancestors"])
        self.assertEqual(queued["next_attempt"], 3)

    def test_rebased_d_conflict_rolls_back_without_repairing_owners_again(self) -> None:
        contract, arguments, d_source = self._create_real_retained_verifier_lane()
        preview = scope.amend_blocked_integration_scope(self.config, self.store, arguments)
        scope.amend_blocked_integration_scope(
            self.config,
            self.store,
            {**arguments, "dry_run": False, "expected_fingerprint": preview["fingerprint"]},
        )
        self._repair_blocked_consumer_owners(contract, conflicting_d_path=True)
        self._queue_rebased_d_source(contract, "conflicting-rebase-d")

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        try:
            claimed_d = coordinator._claim_next_ready_node("conflicting-D")
            self.assertIsNotNone(claimed_d)
            assert claimed_d is not None
            with patch.object(coordinator, "_executor") as executor:
                coordinator._execute_claimed(claimed_d)
                executor.return_value.execute.assert_not_called()
        finally:
            coordinator._pool.shutdown(wait=True)

        rolled_back = self.store.get_task(contract.task_id)
        nodes = {node["node_id"]: node for node in rolled_back["nodes"]}
        self.assertEqual((rolled_back["state"], nodes["D"]["state"], nodes["D"]["attempt"]),
                         ("blocked", "blocked", 2))
        self.assertEqual(
            (d_source / "src/retained-d.ts").read_text(encoding="utf-8"),
            "export const retainedD = 1\n",
        )
        for owner in ("A", "B", "C"):
            self.assertEqual((nodes[owner]["state"], nodes[owner]["attempt"]), ("accepted", 2))
            self.assertIsNone(nodes[owner].get("recovery"))
        rollback = next(
            event for event in reversed(self.store.read_events(task_id=contract.task_id))
            if event["event_type"] == "node.blocked_worktree_recovery_rolled_back"
        )
        self.assertTrue(rollback["payload"]["refresh_accepted_ancestors"])
        self.assertEqual(rollback["payload"]["origin_task_state"], "blocked")
        refreshed_ref = rollback["payload"]["preparation_result"]["artifacts"][
            "dependency-input"
        ]
        refreshed = json.loads(
            self.store.artifacts.verify(refreshed_ref).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [(item["node_id"], item["attempt"]) for item in refreshed["ancestors"]],
            [("A", 2), ("B", 2), ("C", 2)],
        )


if __name__ == "__main__":
    unittest.main()
