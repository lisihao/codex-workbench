"""Git/SQLite lifecycle coverage for the one-time lockfile handoff."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_workbench.authority import authority_machine_id
from codex_workbench.authority_service import AuthorityService
from codex_workbench.config import WorkbenchConfig
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.dirty_worktree_recovery import DirtyWorktreeRecoveryError
from codex_workbench.lockfile_handoff import (
    TOOL_NAME,
    LockfileHandoffError,
    _preflight,
    _reserve,
    active_lockfile_handoff,
    get_ready_lockfile_handoffs,
    lockfile_handoff,
)
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_hash, canonical_json
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager
from tests.process_probe_fixture import isolated_process_catalog


class _FixtureMaterializer:
    """A deterministic injected frozen-pnpm receipt; it never invokes pnpm."""

    def __init__(self, *, error: Exception | None = None, started: threading.Event | None = None,
                 release: threading.Event | None = None):
        self.error = error
        self.started = started
        self.release = release
        self.calls = 0

    def materialize(self, worktree: Path, *, timeout_seconds: int) -> dict[str, object]:
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            if not self.release.wait(timeout=5):
                raise AssertionError("fixture frozen check was not released")
        if self.error is not None:
            raise self.error
        return {
            "schema_version": 1,
            "kind": "pnpm-offline-materialization",
            "pnpm_version": "11.25.0",
            "lockfile_sha256": sha256((worktree / "pnpm-lock.yaml").read_bytes()).hexdigest(),
            "commands": [
                {"command": ["pnpm", "--version"], "exit_code": 0},
                {
                    "command": ["pnpm", "install", "--offline", "--frozen-lockfile", "--ignore-scripts"],
                    "exit_code": 0,
                },
            ],
            "template": {"state": "fixture"},
        }


class LockfileHandoffTests(unittest.TestCase):
    """Exercise reserve, isolated repair, terminal fencing, and replay behavior."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._write_workspace(self.repository, with_workspace_dependency=False)
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "user.name", "Fixture")
        self._git(self.repository, "add", ".")
        self._git(self.repository, "commit", "-m", "base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.config = WorkbenchConfig(
            self.root / "state",
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
        )
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("lockfile-handoff-fixture", "fixture-machine")
        self.worktrees = WorktreeManager(self.config.state_root / "worktrees")
        self.enterContext(isolated_process_catalog(()))
        self.service = AuthorityService(self.store, self._invoke, "handoff-authority")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @staticmethod
    def _lock(importers: str) -> str:
        return (
            "lockfileVersion: '9.0'\n"
            "\n"
            "settings:\n"
            "  autoInstallPeers: true\n"
            "\n"
            "importers:\n"
            "\n"
            + importers
            + "\n"
            "packages:\n"
            "\n"
            "  third-party@1.0.0:\n"
            "    resolution: {integrity: sha512-unchanged}\n"
            "\n"
            "snapshots:\n"
            "\n"
            "  third-party@1.0.0: {}\n"
        )

    @classmethod
    def _write_workspace(cls, root: Path, *, with_workspace_dependency: bool) -> None:
        def write(relative: str, content: str) -> None:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        def write_json(relative: str, value: dict[str, object]) -> None:
            write(relative, json.dumps(value, indent=2) + "\n")

        write(".gitignore", ".env\nnode_modules/\n")
        write("pnpm-workspace.yaml", "packages:\n  - packages/*\n")
        write_json(
            "package.json",
            {"name": "@fixture/root", "private": True, "packageManager": "pnpm@11.25.0"},
        )
        write_json("packages/b/package.json", {"name": "@fixture/b", "version": "1.0.0"})
        write_json(
            "packages/a/package.json",
            {
                "name": "@fixture/a",
                "version": "1.0.0",
                "dependencies": {"@fixture/b": "workspace:*"} if with_workspace_dependency else {},
            },
        )
        write(
            "pnpm-lock.yaml",
            cls._lock(
                "  .: {}\n"
                "\n"
                "  packages/a:\n"
                "    dependencies:\n"
                "      third-party:\n"
                "        specifier: ^1.0.0\n"
                "        version: 1.0.0\n"
                "\n"
                "  packages/b: {}\n"
            ),
        )

    def _invoke(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.assertEqual(name, TOOL_NAME)
        return lockfile_handoff(self.config, self.store, arguments)  # type: ignore[arg-type]

    def _blocked_fixture(
        self,
        task_id: str = "lockfile-handoff",
        *,
        forbidden_scope: tuple[str, ...] = (),
    ) -> tuple[TaskContract, dict[str, object], Path]:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="repair only the missing importer link in pnpm-lock.yaml",
            allowed_scope=(".",),
            forbidden_scope=forbidden_scope,
            executor_model="fixture",
            verifier_model="fixture",
        )
        upstream = NodeSpec(
            "upstream", task_id, "accepted upstream", "fixture", "fixture", "fixture",
            write_scopes=("package.json",), ordinal=0,
        )
        worker = NodeSpec(
            "worker", task_id, "blocked importer worker", "fixture", "fixture", "fixture",
            depends_on=("upstream",), write_scopes=("packages/a",), ordinal=1,
        )
        lock_owner = NodeSpec(
            "lock-owner", task_id, "normal lockfile owner", "fixture", "fixture", "fixture",
            depends_on=("worker",), write_scopes=((".",) if forbidden_scope else ("pnpm-lock.yaml",)), ordinal=2,
        )
        verifier = NodeSpec(
            "verify", task_id, "verifier", "fixture", "fixture", "fixture",
            depends_on=("upstream", "worker", "lock-owner"), verifier=True, ordinal=3,
        )
        self.store.create_task(contract, [upstream, worker, lock_owner, verifier], "create-" + task_id)
        self.store.queue_task(task_id)
        upstream_claim = self.store.claim_ready_node("upstream-fixture", self.epoch)
        assert upstream_claim is not None
        self.assertEqual(upstream_claim["node_id"], "upstream")
        upstream_source = self.worktrees.prepare(
            str(self.repository), self.base_sha, task_id, "upstream", int(upstream_claim["attempt"])
        )
        self.store.assign_worktree(
            task_id,
            "upstream",
            str(upstream_source),
            attempt=int(upstream_claim["attempt"]),
            coordinator_epoch=int(upstream_claim["coordinator_epoch"]),
            lease_epoch=int(upstream_claim["lease_epoch"]),
        )
        upstream_manifest = json.loads((upstream_source / "package.json").read_text(encoding="utf-8"))
        upstream_manifest["description"] = "accepted upstream manifest"
        (upstream_source / "package.json").write_text(
            json.dumps(upstream_manifest, indent=2) + "\n", encoding="utf-8"
        )
        upstream_patch_ref = self.store.artifacts.put_bytes(
            self.worktrees.diff_patch(upstream_source, self.base_sha), "upstream.patch"
        )
        self.store.settle_claimed(
            upstream_claim,
            NodeResult(
                "succeeded",
                "upstream accepted",
                result_kind="worker",
                changed_paths=("package.json",),
                artifacts={"patch": upstream_patch_ref},
            ),
        )
        worker_claim = self.store.claim_ready_node("worker-fixture", self.epoch)
        assert worker_claim is not None
        self.assertEqual(worker_claim["node_id"], "worker")
        source = self.worktrees.prepare(
            str(self.repository), self.base_sha, task_id, "worker", int(worker_claim["attempt"])
        )
        self.store.assign_worktree(
            task_id,
            "worker",
            str(source),
            attempt=int(worker_claim["attempt"]),
            coordinator_epoch=int(worker_claim["coordinator_epoch"]),
            lease_epoch=int(worker_claim["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(task_id), "worker", source, self.store.artifacts, self.worktrees
        )
        assert dependency_input is not None
        worker_manifest = json.loads((source / "packages/a/package.json").read_text(encoding="utf-8"))
        worker_manifest["dependencies"] = {"@fixture/b": "workspace:*"}
        (source / "packages/a/package.json").write_text(
            json.dumps(worker_manifest, indent=2) + "\n", encoding="utf-8"
        )
        (source / ".env").write_text("private runtime value\n", encoding="utf-8")
        dependency_ref = self.store.artifacts.put_text(
            canonical_json(dependency_input.receipt), "dependency-input.json"
        )
        self.store.settle_claimed(
            worker_claim,
            NodeResult(
                "blocked",
                "fixture worker needs the separately owned pnpm lockfile",
                result_kind="worker",
                changed_paths=("packages/a/package.json",),
                artifacts={"dependency-input": dependency_ref},
                checks=("fixture blocked after package manifest update",),
            ),
        )
        task = self.store.get_task(task_id)
        self.assertEqual(task["state"], "blocked")
        return contract, task, source

    @staticmethod
    def _worker(task: dict[str, object]) -> dict[str, object]:
        return next(node for node in task["nodes"] if node["node_id"] == "worker")  # type: ignore[index]

    def _arguments(self, contract: TaskContract, task: dict[str, object], request_id: str) -> dict[str, object]:
        worker = self._worker(task)
        return {
            "op": "preview",
            "task_id": contract.task_id,
            "node_id": "worker",
            "expected_revision": task["state_revision"],
            "expected_attempt": worker["attempt"],
            "expected_contract_hash": task["contract_hash"],
            "request_id": request_id,
        }

    def _apply(self, arguments: dict[str, object], preview: dict[str, object]) -> dict[str, object]:
        applied = {**arguments, "op": "apply", "expected_fingerprint": preview["fingerprint"]}
        response = self.service.dispatch(
            {
                "request_id": arguments["request_id"],
                "task_id": arguments["task_id"],
                "tool": TOOL_NAME,
                "arguments": applied,
            }
        )
        self.assertEqual(response["state"], "completed")
        return response["result"]  # type: ignore[return-value]

    def test_success_keeps_task_blocked_and_returns_replayable_ready_overlay(self) -> None:
        contract, before, source = self._blocked_fixture()
        arguments = self._arguments(contract, before, "handoff-success")
        preview = lockfile_handoff(self.config, self.store, arguments)
        self.assertEqual(preview["state"], "preview")
        self.assertNotIn("input_fingerprints", preview)
        original_result = self._worker(before)["result"]
        original_owner = next(node for node in before["nodes"] if node["node_id"] == "lock-owner")
        original_lock = (source / "pnpm-lock.yaml").read_bytes()
        fixture = _FixtureMaterializer()
        with patch("codex_workbench.lockfile_handoff._pnpm_materializer", return_value=fixture):
            result = self._apply(arguments, preview)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(fixture.calls, 1)
        after = self.store.get_task(contract.task_id)
        worker = self._worker(after)
        self.assertEqual((after["state"], after["state_revision"]), ("blocked", int(before["state_revision"]) + 1))
        self.assertEqual((worker["state"], worker["attempt"], worker["result"]), ("blocked", 1, original_result))
        owner_after = next(node for node in after["nodes"] if node["node_id"] == "lock-owner")
        self.assertEqual(
            {key: original_owner[key] for key in ("node_id", "write_scopes", "depends_on", "verifier")},
            {key: owner_after[key] for key in ("node_id", "write_scopes", "depends_on", "verifier")},
        )
        self.assertEqual((source / "pnpm-lock.yaml").read_bytes(), original_lock)
        self.assertTrue((source / ".env").is_file())
        ready = get_ready_lockfile_handoffs(self.store, contract.task_id)
        self.assertEqual(len(ready), 1)
        overlay = ready[0]
        self.assertEqual(overlay["state"], "ready")
        upstream = next(node for node in before["nodes"] if node["node_id"] == "upstream")
        self.assertEqual(
            overlay["after_ancestors"],
            [{"node_id": "upstream", "attempt": 1, "patch_ref": upstream["result"]["artifacts"]["patch"]}],
        )
        self.assertEqual(
            self.store.artifacts.verify(overlay["lockfile"]["before_artifact_ref"]).read_bytes(),
            original_lock,
        )
        repaired = self.store.artifacts.verify(overlay["lockfile"]["artifact_ref"]).read_bytes()
        self.assertNotEqual(repaired, original_lock)
        self.assertIn(b"specifier: workspace:*", repaired)
        self.assertTrue(Path(str(result["repair_worktree"])).is_dir())
        self.assertFalse((Path(str(result["repair_worktree"])) / ".env").exists())
        self.assertEqual(
            json.loads((Path(str(result["repair_worktree"])) / "package.json").read_text(encoding="utf-8"))["description"],
            "accepted upstream manifest",
        )
        with self.store.connection() as connection:
            self.assertFalse(active_lockfile_handoff(connection, str(self.repository.resolve())))
        duplicate = lockfile_handoff(self.config, self.store, {**arguments, "op": "apply", "expected_fingerprint": preview["fingerprint"]})
        self.assertEqual(duplicate["state"], "ready")
        self.assertEqual(fixture.calls, 1)
        with self.assertRaisesRegex(LockfileHandoffError, "different input"):
            lockfile_handoff(
                self.config,
                self.store,
                {**arguments, "op": "apply", "expected_attempt": 2, "expected_fingerprint": preview["fingerprint"]},
            )

    def test_frozen_failure_releases_without_attaching_overlay(self) -> None:
        contract, task, source = self._blocked_fixture("frozen-failure")
        arguments = self._arguments(contract, task, "handoff-frozen-failure")
        preview = lockfile_handoff(self.config, self.store, arguments)
        original_lock = (source / "pnpm-lock.yaml").read_bytes()
        fixture = _FixtureMaterializer(error=DirtyWorktreeRecoveryError("fixture frozen install failed"))
        with patch("codex_workbench.lockfile_handoff._pnpm_materializer", return_value=fixture):
            result = self._apply(arguments, preview)
        self.assertEqual(result["state"], "failed")
        self.assertIn("fixture frozen install failed", result["detail"])
        self.assertEqual(self.store.get_task(contract.task_id)["state_revision"], task["state_revision"])
        self.assertEqual(get_ready_lockfile_handoffs(self.store, contract.task_id), [])
        self.assertEqual((source / "pnpm-lock.yaml").read_bytes(), original_lock)
        with self.store.connection() as connection:
            self.assertFalse(active_lockfile_handoff(connection, str(self.repository.resolve())))

    def test_source_input_drift_rejects_apply_before_reservation(self) -> None:
        contract, task, source = self._blocked_fixture("input-drift")
        arguments = self._arguments(contract, task, "handoff-drift")
        preview = lockfile_handoff(self.config, self.store, arguments)
        package = json.loads((source / "packages/a/package.json").read_text(encoding="utf-8"))
        package["description"] = "drift after preview"
        (source / "packages/a/package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
        response = self.service.dispatch(
            {
                "request_id": "handoff-drift",
                "task_id": contract.task_id,
                "tool": TOOL_NAME,
                "arguments": {**arguments, "op": "apply", "expected_fingerprint": preview["fingerprint"]},
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(get_ready_lockfile_handoffs(self.store, contract.task_id), [])
        with self.store.connection() as connection:
            self.assertFalse(active_lockfile_handoff(connection, str(self.repository.resolve())))

    def test_other_task_running_lock_owner_fences_preview(self) -> None:
        contract, task, _source = self._blocked_fixture("concurrent-owner")
        other = TaskContract(
            task_id="other-lock-owner",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="concurrent ordinary lockfile owner",
            allowed_scope=(".",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        owner = NodeSpec(
            "owner", other.task_id, "owner", "fixture", "fixture", "fixture",
            write_scopes=("pnpm-lock.yaml",),
        )
        verifier = NodeSpec(
            "verify", other.task_id, "verifier", "fixture", "fixture", "fixture",
            depends_on=("owner",), verifier=True,
        )
        self.store.create_task(other, [owner, verifier], "create-other-lock-owner")
        self.store.queue_task(other.task_id)
        claimed = self.store.claim_ready_node("other-owner", self.epoch)
        self.assertIsNotNone(claimed)
        arguments = self._arguments(contract, task, "handoff-concurrent")
        with self.assertRaisesRegex(LockfileHandoffError, "active lockfile owner"):
            lockfile_handoff(self.config, self.store, arguments)

    def test_task_cancel_fences_a_running_repair_before_ready_publication(self) -> None:
        contract, task, _source = self._blocked_fixture("cancel")
        arguments = self._arguments(contract, task, "handoff-cancel")
        preview = lockfile_handoff(self.config, self.store, arguments)
        original_worker = self._worker(task)
        original_result = original_worker["result"]
        original_attempt = original_worker["attempt"]
        started = threading.Event()
        release = threading.Event()
        fixture = _FixtureMaterializer(started=started, release=release)
        outcome: dict[str, object] = {}

        def apply() -> None:
            with patch("codex_workbench.lockfile_handoff._pnpm_materializer", return_value=fixture):
                outcome["response"] = self.service.dispatch(
                    {
                        "request_id": "handoff-cancel",
                        "task_id": contract.task_id,
                        "tool": TOOL_NAME,
                        "arguments": {**arguments, "op": "apply", "expected_fingerprint": preview["fingerprint"]},
                    }
                )

        thread = threading.Thread(target=apply)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        # This is the native task-control transition used by the MCP cancel
        # action. It must fence an already-running external frozen check.
        cancellation_revision = self.store.transition_task(
            contract.task_id,
            "cancelled",
            expected_revision=int(task["state_revision"]),
        )
        self.assertEqual(cancellation_revision, int(task["state_revision"]) + 1)
        cancelled_task = self.store.get_task(contract.task_id)
        cancelled_worker = self._worker(cancelled_task)
        self.assertEqual(cancelled_task["state"], "cancelled")
        self.assertEqual(
            (cancelled_worker["state"], cancelled_worker["attempt"], cancelled_worker["result"]),
            ("blocked", original_attempt, original_result),
        )
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["response"]["result"]["state"], "cancelled")  # type: ignore[index]
        after = self.store.get_task(contract.task_id)
        worker = self._worker(after)
        self.assertEqual(
            (after["state"], after["state_revision"]),
            ("cancelled", cancellation_revision),
        )
        self.assertEqual(
            (worker["state"], worker["attempt"], worker["result"]),
            ("blocked", original_attempt, original_result),
        )
        self.assertEqual(get_ready_lockfile_handoffs(self.store, contract.task_id), [])
        with self.store.connection() as connection:
            self.assertFalse(active_lockfile_handoff(connection, str(self.repository.resolve())))

    def test_reserved_upstream_patch_identity_drift_is_fenced_before_ready(self) -> None:
        contract, task, _source = self._blocked_fixture("accepted-input-drift")
        arguments = self._arguments(contract, task, "handoff-accepted-input-drift")
        preview = lockfile_handoff(self.config, self.store, arguments)
        original_worker = self._worker(task)
        started = threading.Event()
        release = threading.Event()
        fixture = _FixtureMaterializer(started=started, release=release)
        outcome: dict[str, object] = {}

        def apply() -> None:
            with patch("codex_workbench.lockfile_handoff._pnpm_materializer", return_value=fixture):
                outcome["response"] = self.service.dispatch(
                    {
                        "request_id": "handoff-accepted-input-drift",
                        "task_id": contract.task_id,
                        "tool": TOOL_NAME,
                        "arguments": {
                            **arguments,
                            "op": "apply",
                            "expected_fingerprint": preview["fingerprint"],
                        },
                    }
                )

        thread = threading.Thread(target=apply)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        upstream = next(node for node in task["nodes"] if node["node_id"] == "upstream")
        upstream_result = dict(upstream["result"])
        upstream_artifacts = dict(upstream_result["artifacts"])
        original_patch = self.store.artifacts.verify(upstream_artifacts["patch"]).read_bytes()
        replacement_patch = original_patch.replace(
            b"accepted upstream manifest", b"replacement upstream manifest"
        )
        self.assertNotEqual(replacement_patch, original_patch)
        replacement_ref = self.store.artifacts.put_bytes(replacement_patch, "upstream-replacement.patch")
        upstream_artifacts["patch"] = replacement_ref
        upstream_result["artifacts"] = upstream_artifacts
        # Simulate a valid durable accepted-result identity change while the
        # handoff owns its private repair tree. The task revision remains
        # unchanged so this proves the sealed ancestor input itself is bound.
        with self.store.transaction() as connection:
            changed = connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = ?",
                (canonical_json(upstream_result), contract.task_id, "upstream"),
            ).rowcount
        self.assertEqual(changed, 1)
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        response = outcome["response"]  # type: ignore[assignment]
        self.assertEqual(response["result"]["state"], "failed")  # type: ignore[index]
        self.assertIn("ancestor lineage", response["result"]["detail"])  # type: ignore[index]
        after = self.store.get_task(contract.task_id)
        worker = self._worker(after)
        self.assertEqual(
            (after["state"], after["state_revision"]),
            ("blocked", task["state_revision"]),
        )
        self.assertEqual(
            (worker["state"], worker["attempt"], worker["result"]),
            ("blocked", original_worker["attempt"], original_worker["result"]),
        )
        self.assertEqual(get_ready_lockfile_handoffs(self.store, contract.task_id), [])
        with self.store.connection() as connection:
            self.assertFalse(active_lockfile_handoff(connection, str(self.repository.resolve())))

    def test_reserved_restart_reconciles_without_replaying_and_permission_is_fail_closed(self) -> None:
        contract, task, _source = self._blocked_fixture("restart")
        arguments = self._arguments(contract, task, "handoff-restart")
        preview = lockfile_handoff(self.config, self.store, arguments)
        preflight = _preflight(self.config, self.store, arguments)
        reservation = _reserve(self.store, preflight)
        self.assertEqual(reservation["state"], "reserved")
        with self.store.connection() as connection:
            self.assertTrue(active_lockfile_handoff(connection, str(self.repository)))
        status = lockfile_handoff(
            self.config, self.store,
            {"op": "status", "task_id": contract.task_id, "request_id": "handoff-restart"},
        )
        self.assertEqual(status["state"], "reserved")
        reconciled = self.service.dispatch(
            {
                "request_id": "restart-reconcile",
                "task_id": contract.task_id,
                "tool": TOOL_NAME,
                "arguments": {
                    "op": "reconcile", "task_id": contract.task_id,
                    "request_id": "handoff-restart", "operation_id": "restart-reconcile",
                },
            }
        )
        self.assertEqual(reconciled["result"]["state"], "needs_action")
        self.assertEqual(get_ready_lockfile_handoffs(self.store, contract.task_id), [])
        with self.store.connection() as connection:
            self.assertFalse(active_lockfile_handoff(connection, str(self.repository)))

        rejected_contract, rejected, _ = self._blocked_fixture("scope-rejected")
        denied_contract = dict(rejected["contract"])
        denied_contract["allowed_scope"] = ["packages/a"]
        denied_hash = canonical_hash(denied_contract)
        # Planner validation prevents creating this obsolete/invalid plan in
        # the first place. The durable-row mutation exercises the handoff
        # module's independent scope fence against a legacy authority record.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET contract_json = ?, contract_hash = ? WHERE task_id = ?",
                (canonical_json(denied_contract), denied_hash, rejected_contract.task_id),
            )
        rejected = self.store.get_task(rejected_contract.task_id)
        with self.assertRaisesRegex(LockfileHandoffError, "outside the task allowed scope"):
            lockfile_handoff(
                self.config,
                self.store,
                {
                    "op": "preview", "task_id": rejected_contract.task_id, "node_id": "worker",
                    "expected_revision": rejected["state_revision"], "expected_attempt": 1,
                    "expected_contract_hash": rejected["contract_hash"], "request_id": "scope-rejected-request",
                },
            )


if __name__ == "__main__":
    unittest.main()
