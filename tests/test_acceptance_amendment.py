"""Durable Git/SQLite coverage for the fixed Host acceptance amendment."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from codex_workbench import acceptance_amendment as amendment
from codex_workbench.authority import authority_machine_id
from codex_workbench.config import WorkbenchConfig
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.model import (
    NodeResult,
    NodeSpec,
    TaskContract,
    canonical_hash,
    canonical_json,
    now_iso,
)
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


B_CLIENT_PROJECTS = (
    "packages/prompt/task-template-rpc",
    "packages/client/ui-task-template",
    "packages/orchestration/orchestration-local",
    "packages/orchestration/tool-debate",
    "packages/orchestration/tool-orchestration",
    "packages/physical-operator/tool-physical-operator",
)


class AcceptanceAmendmentTests(unittest.TestCase):
    """Exercise preview and CAS against one real blocked worktree allocation."""

    def setUp(self) -> None:
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
        self._write_authority_runtime()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("acceptance-amendment", "fixture-machine")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self._create_blocked_task()

    def _initialize_repository(self) -> None:
        self.repository.mkdir()
        self._run_git(self.repository, "init", "-b", "main")
        self._run_git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._run_git(self.repository, "config", "user.name", "Fixture")
        (self.repository / "src").mkdir()
        (self.repository / "src" / "value.ts").write_text("export const value = 1\n")
        (self.repository / ".gitignore").write_text("node_modules/\n")
        (self.repository / "package.json").write_text(
            json.dumps(
                {
                    "name": "fixture-dsh",
                    "scripts": {
                        "build:lib:host": "tsc -b tsconfig.host.json && tsdown --env.DSH_BUILD_FACE host"
                    },
                }
            )
            + "\n"
        )
        (self.repository / "tsconfig.host.json").write_text('{"files": []}\n')
        (self.repository / "tsdown.config.ts").write_text("export default {}\n")
        self._run_git(
            self.repository,
            "add",
            "package.json",
            ".gitignore",
            "src/value.ts",
            "tsconfig.host.json",
            "tsdown.config.ts",
        )
        self._run_git(self.repository, "commit", "-m", "fixture base")
        self.base_sha = self._run_git(self.repository, "rev-parse", "HEAD")

    def _write_authority_runtime(self) -> None:
        runtime_root = self.root / "runtime"
        self.node_binary = self._write_executable(runtime_root / "node")
        pnpm_binary = self._write_executable(runtime_root / "pnpm")
        codex_binary = self._write_executable(runtime_root / "codex")
        self.config.install_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.config.install_manifest.write_text(
            json.dumps(
                {
                    "codex_binary": str(codex_binary),
                    "pnpm_recovery_runtime": {
                        "binary": str(pnpm_binary),
                        "node_binary": str(self.node_binary),
                    },
                }
            )
            + "\n"
        )

    @staticmethod
    def _write_executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
        return path

    @staticmethod
    def _run_git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _install_fixture_dependencies(self, worktree: Path) -> None:
        external = self.root / "pnpm-store"
        self.host_tsc = external / "typescript" / "bin" / "tsc"
        self.host_tsdown = external / "tsdown" / "dist" / "run.mjs"
        self.host_tsc.parent.mkdir(parents=True, exist_ok=True)
        self.host_tsdown.parent.mkdir(parents=True, exist_ok=True)
        self.host_tsc.write_text("export {}\n")
        self.host_tsdown.write_text("export {}\n")
        modules = worktree / "node_modules"
        modules.mkdir()
        (modules / "typescript").symlink_to(external / "typescript", target_is_directory=True)
        (modules / "tsdown").symlink_to(external / "tsdown", target_is_directory=True)
        (modules / ".bin").mkdir()
        (modules / ".bin" / "tsc").symlink_to("../typescript/bin/tsc")

    def _create_blocked_task(self) -> None:
        self.client_tsc_command = "node_modules/.bin/tsc -b " + " ".join(B_CLIENT_PROJECTS)
        self.contract = TaskContract(
            task_id="acceptance-amendment",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="add the Host prerequisite before the existing Client build check",
            allowed_scope=("src",),
            acceptance_commands=(
                "node_modules/.bin/vitest run packages/client/example.spec.ts",
                self.client_tsc_command,
                "git diff --check",
            ),
            executor_model="fixture",
            verifier_model="fixture",
        )
        ancestor = NodeSpec(
            "ancestor",
            self.contract.task_id,
            "accepted dependency",
            "fixture",
            "fixture",
            write_scopes=("src",),
        )
        worker = NodeSpec(
            "worker",
            self.contract.task_id,
            "blocked Client task",
            "fixture",
            "fixture",
            depends_on=(ancestor.node_id,),
            write_scopes=("src",),
        )
        verifier = NodeSpec(
            "verify",
            self.contract.task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            depends_on=(ancestor.node_id, worker.node_id),
            verifier=True,
        )
        self.store.create_task(self.contract, [ancestor, worker, verifier], "acceptance-amendment-create")
        self.store.queue_task(self.contract.task_id)
        claimed_ancestor = self.store.claim_ready_node("fixture-ancestor", self.epoch)
        self.assertIsNotNone(claimed_ancestor)
        self.assertEqual(claimed_ancestor["node_id"], ancestor.node_id)
        ancestor_source = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            self.contract.task_id,
            ancestor.node_id,
            int(claimed_ancestor["attempt"]),
        )
        self.store.assign_worktree(
            self.contract.task_id,
            ancestor.node_id,
            str(ancestor_source),
            attempt=int(claimed_ancestor["attempt"]),
            coordinator_epoch=int(claimed_ancestor["coordinator_epoch"]),
            lease_epoch=int(claimed_ancestor["lease_epoch"]),
        )
        (ancestor_source / "src" / "ancestor.ts").write_text("export const ancestor = true\n")
        self.ancestor_artifact = self.store.artifacts.put_text(
            "accepted ancestor evidence\n", "ancestor.log"
        )
        ancestor_patch = self.worktrees.diff_patch(ancestor_source, self.base_sha)
        self.store.settle_claimed(
            claimed_ancestor,
            NodeResult(
                "succeeded",
                "fixture predecessor completed",
                artifacts={
                    "patch": self.store.artifacts.put_bytes(ancestor_patch, "ancestor.patch"),
                    "test-log": self.ancestor_artifact,
                },
                actual_model="fixture",
                result_kind="worker",
                changed_paths=("src/ancestor.ts",),
            ),
        )
        claimed_worker = self.store.claim_ready_node("fixture-worker", self.epoch)
        self.assertIsNotNone(claimed_worker)
        self.assertEqual(claimed_worker["node_id"], worker.node_id)
        self.source = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            self.contract.task_id,
            worker.node_id,
            int(claimed_worker["attempt"]),
        )
        self._install_fixture_dependencies(self.source)
        self.store.assign_worktree(
            self.contract.task_id,
            worker.node_id,
            str(self.source),
            attempt=int(claimed_worker["attempt"]),
            coordinator_epoch=int(claimed_worker["coordinator_epoch"]),
            lease_epoch=int(claimed_worker["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(self.contract.task_id),
            worker.node_id,
            self.source,
            self.store.artifacts,
            self.worktrees,
        )
        self.assertIsNotNone(dependency_input)
        self.dependency_input_ref = self.store.artifacts.put_text(
            json.dumps(dependency_input.receipt, ensure_ascii=False, sort_keys=True),
            "dependency-input.json",
        )
        (self.source / "src" / "value.ts").write_text("export const value = 2\n")
        self.store.settle_claimed(
            claimed_worker,
            NodeResult(
                "blocked",
                "fixture stops before the Client aggregate can run",
                artifacts={"dependency-input": self.dependency_input_ref},
                actual_model="fixture",
                result_kind="worker",
                changed_paths=("src/value.ts",),
            ),
        )
        self.blocked = self.store.get_task(self.contract.task_id)
        self.assertEqual(self.blocked["state"], "blocked")
        self.arguments = {
            "task_id": self.contract.task_id,
            "node_id": "worker",
            "expected_revision": self.blocked["state_revision"],
            "expected_attempt": 1,
            "expected_contract_hash": self.blocked["contract_hash"],
            "profile_id": amendment.PROFILE_ID,
            "reason": "add the required Host types before the existing Client build",
            "dry_run": True,
        }

    def _preview(self) -> dict:
        return amendment.amend_task_acceptance(self.config, self.store, self.arguments)

    def _replace_contract_commands(self, commands: tuple[str, ...]) -> None:
        current = self.store.get_task(self.contract.task_id)
        replacement = TaskContract.from_dict(
            {**current["contract"], "acceptance_commands": list(commands)}
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET contract_json = ?, contract_hash = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    canonical_json(replacement.to_dict()),
                    replacement.digest,
                    now_iso(),
                    self.contract.task_id,
                ),
            )
        self.blocked = self.store.get_task(self.contract.task_id)
        self.arguments.update(
            expected_revision=self.blocked["state_revision"],
            expected_contract_hash=self.blocked["contract_hash"],
        )

    def _raw_allocations(self) -> tuple[tuple[object, ...], ...]:
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT allocation_id, task_id, node_id, attempt, repository, base_sha, branch,
                       current_path, state, node_result_json, created_at, updated_at
                FROM worktree_allocations
                WHERE task_id = ?
                ORDER BY allocation_id
                """,
                (self.contract.task_id,),
            ).fetchall()
        return tuple(tuple(row) for row in rows)

    def _worker_source_allocation(self) -> dict:
        return next(
            allocation
            for allocation in self.store.list_worktree_allocations(states=("active",))
            if allocation["node_id"] == "worker" and allocation["attempt"] == 1
        )

    def _source_only_hold_ids(self) -> set[str]:
        with self.store.connection() as connection:
            return self.store._source_only_recovery_hold_ids(connection)

    def _rollback_source_only_recovery(self) -> dict:
        """Authorize a real source-only retry and settle its unprepared a2 back to a1."""

        blocked = self.store.get_task(self.contract.task_id)
        worker = next(node for node in blocked["nodes"] if node["node_id"] == "worker")
        preview = self.store.capture_and_resume_blocked_worktree(
            self.contract.task_id,
            "worker",
            expected_revision=int(blocked["state_revision"]),
            expected_attempt=int(worker["attempt"]),
            reason="preview source-only recovery for the retained original source",
            source_only=True,
            confirm_preserve_unknown_ignored=True,
            confirm_source_only_extraction=True,
            dry_run=True,
        )
        self.store.capture_and_resume_blocked_worktree(
            self.contract.task_id,
            "worker",
            expected_revision=int(blocked["state_revision"]),
            expected_attempt=int(worker["attempt"]),
            reason="authorize source-only recovery for the retained original source",
            source_only=True,
            confirm_preserve_unknown_ignored=True,
            confirm_source_only_extraction=True,
            expected_source_delta_sha256=preview["source_delta_sha256"],
        )
        claimed = self.store.claim_ready_node("source-only-rollback", self.epoch)
        self.assertIsNotNone(claimed)
        self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 2))
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "blocked",
                "source-only preparation failed before allocating a later worktree",
                artifacts={
                    "recovery": self.store.artifacts.put_text(
                        "source-only preparation did not allocate a2\n",
                        "source-only-preparation.log",
                    )
                },
                provider="workbench-dirty-worktree-recovery",
                result_kind="worker",
                checks=("source-only recovery preparation was not allocated",),
            ),
        )
        restored = self.store.get_task(self.contract.task_id)
        restored_worker = next(node for node in restored["nodes"] if node["node_id"] == "worker")
        self.assertEqual(
            (restored["state"], restored_worker["state"], restored_worker["attempt"]),
            ("blocked", "blocked", 1),
        )
        self.assertEqual(restored_worker["worktree"], str(self.source))
        self.assertIn(
            "node.blocked_worktree_recovery_rolled_back",
            [event["event_type"] for event in self.store.read_events(task_id=self.contract.task_id)],
        )
        self.blocked = restored
        self.arguments.update(
            expected_revision=restored["state_revision"],
            expected_attempt=1,
            expected_contract_hash=restored["contract_hash"],
        )
        return restored

    def test_tool_schema_has_only_explicit_preview_and_cas_fields(self) -> None:
        schema = amendment.ACCEPTANCE_AMENDMENT_TOOL["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["profile_id"]["const"], amendment.PROFILE_ID)
        self.assertIn("expected_fingerprint", schema["properties"])
        self.assertNotIn("expected_fingerprint", schema["required"])

    def test_preview_is_read_only_and_inserts_fixed_host_prerequisites(self) -> None:
        with self.store.connection() as connection:
            database_before = tuple(connection.iterdump())
        artifacts_before = tuple(sorted(self.store.artifacts.root.rglob("*")))
        package_before = (self.source / "package.json").read_bytes()
        entrypoint_before = self.host_tsc.read_bytes()

        preview = self._preview()

        self.assertTrue(preview["dry_run"])
        self.assertRegex(preview["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(preview["old_contract_hash"], self.blocked["contract_hash"])
        self.assertEqual(preview["new_revision"], self.blocked["state_revision"] + 1)
        self.assertEqual(
            preview["exact_commands"],
            [
                ["node_modules/.bin/vitest", "run", "packages/client/example.spec.ts"],
                [str(self.node_binary.resolve()), "node_modules/typescript/bin/tsc", "-b", "tsconfig.host.json"],
                [
                    str(self.node_binary.resolve()),
                    "node_modules/tsdown/dist/run.mjs",
                    "--env.DSH_BUILD_FACE",
                    "host",
                ],
                ["node_modules/.bin/tsc", "-b", *B_CLIENT_PROJECTS],
                ["git", "diff", "--check"],
            ],
        )
        self.assertEqual(
            preview["metadata"]["entrypoint_sha256"][str(self.host_tsc.resolve())],
            sha256(entrypoint_before).hexdigest(),
        )
        self.assertFalse(preview["queued"])
        self.assertFalse(preview["commands_executed"])
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), database_before)
        self.assertEqual(tuple(sorted(self.store.artifacts.root.rglob("*"))), artifacts_before)
        self.assertEqual((self.source / "package.json").read_bytes(), package_before)
        self.assertEqual(self.host_tsc.read_bytes(), entrypoint_before)

    def test_run_changes_only_contract_and_retains_ancestor_nodes_allocations_and_artifacts(self) -> None:
        preview = self._preview()
        before = self.store.get_task(self.contract.task_id)
        before_nodes = before["nodes"]
        before_allocation = self._raw_allocations()
        ancestor_bytes = self.store.artifacts.verify(self.ancestor_artifact).read_bytes()
        source_before = (self.source / "package.json").read_bytes()
        result = amendment.amend_task_acceptance(
            self.config,
            self.store,
            {
                **self.arguments,
                "dry_run": False,
                "expected_fingerprint": preview["fingerprint"],
            },
        )
        after = self.store.get_task(self.contract.task_id)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["old_contract_hash"], before["contract_hash"])
        self.assertEqual(result["new_contract_hash"], after["contract_hash"])
        self.assertEqual(result["old_revision"], before["state_revision"])
        self.assertEqual(result["new_revision"], after["state_revision"])
        self.assertEqual(after["state"], "blocked")
        self.assertEqual(after["state_revision"], before["state_revision"] + 1)
        self.assertEqual(after["nodes"], before_nodes)
        self.assertEqual(self._raw_allocations(), before_allocation)
        self.assertEqual(self.store.artifacts.verify(self.ancestor_artifact).read_bytes(), ancestor_bytes)
        ancestor = next(node for node in after["nodes"] if node["node_id"] == "ancestor")
        self.assertEqual(ancestor["state"], "accepted")
        self.assertEqual(ancestor["result"]["artifacts"]["test-log"], self.ancestor_artifact)
        self.assertEqual(after["contract"]["acceptance_commands"], result["acceptance_commands"])
        self.assertEqual(
            {key: value for key, value in after["contract"].items() if key != "acceptance_commands"},
            {key: value for key, value in before["contract"].items() if key != "acceptance_commands"},
        )
        self.assertEqual(TaskContract.from_dict(after["contract"]).digest, after["contract_hash"])
        self.assertEqual((self.source / "package.json").read_bytes(), source_before)
        event = self.store.read_events(task_id=self.contract.task_id)[-1]
        self.assertEqual(event["event_type"], "task.acceptance_amended")
        self.assertEqual(event["node_id"], "worker")
        self.assertEqual(event["payload"]["old_contract"], before["contract"])
        self.assertEqual(event["payload"]["new_contract"], after["contract"])
        self.assertEqual(event["payload"]["preview_fingerprint"], preview["fingerprint"])
        self.assertEqual(result["event_cursor"], event["cursor"])

    def test_stale_contract_revision_and_attempt_are_rejected(self) -> None:
        before = self.store.get_task(self.contract.task_id)
        for changes in (
            {"expected_contract_hash": "a" * 64},
            {"expected_revision": self.arguments["expected_revision"] + 1},
            {"expected_attempt": self.arguments["expected_attempt"] + 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(StateConflictError):
                amendment.amend_task_acceptance(
                    self.config, self.store, {**self.arguments, **changes}
                )
        self.assertEqual(self.store.get_task(self.contract.task_id), before)

    def test_unknown_profile_invalid_script_ambiguous_target_and_duplicate_host_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            amendment.amend_task_acceptance(
                self.config,
                self.store,
                {**self.arguments, "profile_id": "unrecognized-profile"},
            )
        package = json.loads((self.source / "package.json").read_text())
        package["scripts"]["build:lib:host"] = "pnpm run arbitrary"
        (self.source / "package.json").write_text(json.dumps(package) + "\n")
        with self.assertRaisesRegex(ValueError, "build:lib:host"):
            self._preview()
        package["scripts"]["build:lib:host"] = "tsc -b tsconfig.host.json && tsdown --env.DSH_BUILD_FACE host"
        (self.source / "package.json").write_text(json.dumps(package) + "\n")
        self._replace_contract_commands(
            (
                self.client_tsc_command,
                self.client_tsc_command,
                "git diff --check",
            )
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self._preview()
        self._replace_contract_commands(
            (
                "node_modules/.bin/tsc -b tsconfig.host.json",
                self.client_tsc_command,
            )
        )
        with self.assertRaisesRegex(ValueError, "already contains a Host tsc"):
            self._preview()

    def test_entrypoint_drift_cannot_commit_a_previewed_recipe(self) -> None:
        preview = self._preview()
        before = self.store.get_task(self.contract.task_id)
        self.host_tsc.write_text("changed after preview\n")
        with self.assertRaisesRegex(StateConflictError, "fingerprint changed"):
            amendment.amend_task_acceptance(
                self.config,
                self.store,
                {
                    **self.arguments,
                    "dry_run": False,
                    "expected_fingerprint": preview["fingerprint"],
                },
            )
        self.assertEqual(self.store.get_task(self.contract.task_id), before)

    def test_legacy_contract_normalization_outside_acceptance_commands_is_rejected(self) -> None:
        raw = dict(self.blocked["contract"])
        raw["planner_model"] = "legacy-control-plane-name"
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET contract_json = ?, contract_hash = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    canonical_json(raw),
                    canonical_hash(raw),
                    now_iso(),
                    self.contract.task_id,
                ),
            )
        self.blocked = self.store.get_task(self.contract.task_id)
        self.arguments.update(
            expected_revision=self.blocked["state_revision"],
            expected_contract_hash=self.blocked["contract_hash"],
        )
        with self.assertRaisesRegex(ValueError, "normalize a non-acceptance"):
            self._preview()

    def test_paused_task_is_fenced(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET state = 'paused' WHERE task_id = ?",
                (self.contract.task_id,),
            )
        with self.assertRaisesRegex(StateConflictError, "expected blocked"):
            self._preview()

    def test_running_node_and_allocation_worktree_drift_are_fenced(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'running' WHERE task_id = ? AND node_id = 'verify'",
                (self.contract.task_id,),
            )
        with self.assertRaisesRegex(StateConflictError, "every task node to have stopped"):
            self._preview()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'pending' WHERE task_id = ? AND node_id = 'verify'",
                (self.contract.task_id,),
            )
            connection.execute(
                """
                UPDATE worktree_allocations SET current_path = ?
                WHERE task_id = ? AND node_id = 'worker' AND attempt = 1
                """,
                (str(self.root / "other-worktree"), self.contract.task_id),
            )
        with self.assertRaisesRegex(StateConflictError, "does not match its active allocation"):
            self._preview()

    def test_active_validation_and_pending_approval_are_fenced(self) -> None:
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO authority_requests(
                    request_id, request_fingerprint, tool, task_id, session_id, actor,
                    state, instance_id, result_json, created_at, updated_at, settled_at
                ) VALUES(?, ?, ?, ?, NULL, ?, 'executing', ?, NULL, ?, ?, NULL)
                """,
                (
                    "active-validation",
                    "a" * 64,
                    "workbench_validate_blocked_node",
                    self.contract.task_id,
                    "fixture",
                    "fixture-instance",
                    timestamp,
                    timestamp,
                ),
            )
        with self.assertRaisesRegex(StateConflictError, "controlled validation"):
            self._preview()
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM authority_requests WHERE request_id = ?", ("active-validation",))
            connection.execute(
                """
                INSERT INTO approvals(
                    approval_id, task_id, kind, request_json, decision, decided_at, created_at
                ) VALUES(?, ?, ?, ?, NULL, NULL, ?)
                """,
                ("pending-approval", self.contract.task_id, "fixture", "{}", timestamp),
            )
        with self.assertRaisesRegex(StateConflictError, "pending approval"):
            self._preview()

    def test_retained_source_after_real_source_only_rollback_permits_amendment(self) -> None:
        before = self._rollback_source_only_recovery()
        ancestor_before = next(node for node in before["nodes"] if node["node_id"] == "ancestor")
        worker_before = next(node for node in before["nodes"] if node["node_id"] == "worker")
        source_bytes = {
            relative: (self.source / relative).read_bytes()
            for relative in (
                "package.json",
                "src/ancestor.ts",
                "src/value.ts",
                "tsconfig.host.json",
                "tsdown.config.ts",
            )
        }
        events_before = self.store.read_events(task_id=self.contract.task_id)
        allocation_id = self._worker_source_allocation()["allocation_id"]
        allocations_before = self._raw_allocations()
        self.assertIn(allocation_id, self._source_only_hold_ids())

        preview = self._preview()
        applied = amendment.amend_task_acceptance(
            self.config,
            self.store,
            {
                **self.arguments,
                "dry_run": False,
                "expected_fingerprint": preview["fingerprint"],
            },
        )

        after = self.store.get_task(self.contract.task_id)
        ancestor_after = next(node for node in after["nodes"] if node["node_id"] == "ancestor")
        worker_after = next(node for node in after["nodes"] if node["node_id"] == "worker")
        self.assertEqual(after["state"], "blocked")
        self.assertEqual((worker_after["state"], worker_after["attempt"]), ("blocked", 1))
        self.assertEqual(ancestor_after, ancestor_before)
        self.assertEqual(worker_after, worker_before)
        self.assertFalse(applied["queued"])
        self.assertFalse(applied["nodes_changed"])
        self.assertFalse(applied["allocations_changed"])
        self.assertEqual(self._raw_allocations(), allocations_before)
        self.assertIn(allocation_id, self._source_only_hold_ids())
        self.assertEqual(
            {
                relative: (self.source / relative).read_bytes()
                for relative in source_bytes
            },
            source_bytes,
        )
        events_after = self.store.read_events(task_id=self.contract.task_id)
        self.assertEqual(events_after[:-1], events_before)
        self.assertEqual(events_after[-1]["event_type"], "task.acceptance_amended")
        with self.assertRaisesRegex(StateConflictError, "source-only recovery retains"):
            self.store.begin_worktree_quarantine(
                allocation_id,
                str(self.root / "quarantine"),
            )

    def test_active_or_quarantine_pending_later_attempt_allocation_is_fenced(self) -> None:
        target = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            self.contract.task_id,
            "worker",
            2,
        )
        timestamp = now_iso()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO worktree_allocations(
                    allocation_id, task_id, node_id, attempt, repository, base_sha, branch,
                    current_path, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "later-attempt-allocation",
                    self.contract.task_id,
                    "worker",
                    2,
                    str(self.repository),
                    self.base_sha,
                    self.worktrees.branch_name(self.contract.task_id, "worker", 2),
                    str(target),
                    "active",
                    timestamp,
                    timestamp,
                ),
            )
        for state in ("active", "quarantine_pending"):
            with self.subTest(state=state):
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE worktree_allocations SET state = ? WHERE allocation_id = ?",
                        (state, "later-attempt-allocation"),
                    )
                with self.assertRaisesRegex(
                    StateConflictError,
                    "active later-attempt allocation",
                ):
                    self._preview()


if __name__ == "__main__":
    unittest.main()
