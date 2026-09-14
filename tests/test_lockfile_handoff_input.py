"""End-to-end input replay coverage for ready lockfile handoffs."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority import authority_machine_id
from codex_workbench.authority_service import AuthorityService
from codex_workbench.accepted_source_repair import AcceptedSourceRepairError
from codex_workbench.config import WorkbenchConfig
from codex_workbench.dependency_inputs import (
    DependencyInput,
    apply_recorded_dependency_input,
    load_recorded_dependency_input,
    rebind_recorded_lockfile_handoffs,
)
from codex_workbench.lockfile_handoff import (
    LockfileHandoffError,
    TOOL_NAME,
    get_ready_lockfile_handoffs,
    lockfile_handoff,
)
from codex_workbench.lockfile_handoff_input import (
    LockfileHandoffInputError,
    apply_pending_lockfile_handoffs,
    normalize_lockfile_handoffs,
)
from codex_workbench.mcp import WorkbenchMCPServer
from codex_workbench.model import (
    NodeResult,
    NodeSpec,
    TaskContract,
    canonical_hash,
    canonical_json,
    now_iso,
)
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager
from tests.process_probe_fixture import isolated_process_catalog


class _RecoveryMaterializer:
    """Return a local frozen-install receipt without invoking pnpm."""

    def materialize(
        self,
        worktree: Path,
        *,
        timeout_seconds: int,
        require_cached_template: bool = False,
    ) -> dict[str, object]:
        if timeout_seconds < 1:
            raise AssertionError("recovery materializer requires a positive timeout")
        linker = worktree / "node_modules"
        (linker / ".bin").mkdir(parents=True, exist_ok=True)
        (linker / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
        return {
            "schema_version": 1,
            "kind": "pnpm-offline-materialization",
            "package_manager": "pnpm@11.25.0",
            "pnpm_version": "11.25.0",
            "lockfile_sha256": sha256(
                (worktree / "pnpm-lock.yaml").read_bytes()
            ).hexdigest(),
            "materialization_timeout_seconds": timeout_seconds,
            "template": {"state": "disabled"},
            "commands": [
                {
                    "command": ["pnpm", "--version"],
                    "exit_code": 0,
                    "stdout": "11.25.0\n",
                    "stderr": "",
                }
            ],
        }


class LockfileHandoffInputTests(unittest.TestCase):
    """Exercise lifecycle-ready bytes through the ordinary recovery scheduler."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
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
        self.epoch = self.store.activate_coordinator(
            "lockfile-handoff-input-fixture",
            "fixture-machine",
        )
        self.worktrees = WorktreeManager(self.config.state_root / "worktrees")
        self.mcp = WorkbenchMCPServer(self.config, self.store)
        self.authority = AuthorityService(
            self.store,
            self._invoke_handoff,
            "lockfile-handoff-input-authority",
        )
        self.enterContext(isolated_process_catalog(()))

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

        write(".gitignore", "node_modules/\n")
        write("pnpm-workspace.yaml", "packages:\n  - packages/*\n")
        write_json(
            "package.json",
            {
                "name": "@fixture/root",
                "private": True,
                "packageManager": "pnpm@11.25.0",
            },
        )
        write_json(
            "packages/b/package.json",
            {"name": "@fixture/b", "version": "1.0.0"},
        )
        write_json(
            "packages/a/package.json",
            {
                "name": "@fixture/a",
                "version": "1.0.0",
                "dependencies": (
                    {"@fixture/b": "workspace:*"} if with_workspace_dependency else {}
                ),
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

    def _invoke_handoff(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        self.assertEqual(name, TOOL_NAME)
        return lockfile_handoff(self.config, self.store, arguments)  # type: ignore[arg-type]

    def _check_command(self) -> tuple[str, ...]:
        source = (
            "import json; "
            "from pathlib import Path; "
            "package = json.loads(Path('packages/a/package.json').read_text()); "
            "assert package['dependencies']['@fixture/b'] == 'workspace:*'; "
            "lock = Path('pnpm-lock.yaml').read_text(); "
            "assert 'specifier: workspace:*' in lock; "
            "assert 'version: link:../b' in lock"
        )
        return (sys.executable, "-c", source)

    def _blocked_task(
        self,
        *,
        task_id: str,
        with_upstream: bool,
        downstream_before_lock_owner: bool = False,
    ) -> tuple[TaskContract, dict[str, object], Path]:
        command = self._check_command()
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="replay a ready lockfile overlay before a blocked worker continues",
            allowed_scope=(".",),
            acceptance_commands=(shlex.join(command),),
            executor_model="fixture",
            verifier_model="fixture",
        )
        upstream = NodeSpec(
            "upstream",
            task_id,
            "accepted no-op predecessor",
            "fixture",
            "fixture",
            "fixture",
            write_scopes=("packages/b",),
            ordinal=0,
        )
        worker = NodeSpec(
            "worker",
            task_id,
            "blocked worker that owns packages/a",
            "deterministic",
            "fixture",
            command=command,
            depends_on=(("upstream",) if with_upstream else ()),
            read_scopes=("packages/a/package.json", "pnpm-lock.yaml"),
            write_scopes=("packages/a",),
            ordinal=1,
        )
        lock_owner = NodeSpec(
            "lock-owner",
            task_id,
            "normal lockfile owner",
            "deterministic",
            "fixture",
            command=command,
            depends_on=("worker",),
            read_scopes=("packages/a/package.json", "pnpm-lock.yaml"),
            write_scopes=("pnpm-lock.yaml",),
            ordinal=3 if downstream_before_lock_owner else 2,
        )
        downstream = NodeSpec(
            "downstream",
            task_id,
            "downstream consumer",
            "deterministic",
            "fixture",
            command=command,
            depends_on=("worker",),
            read_scopes=("packages/a/package.json", "pnpm-lock.yaml"),
            ordinal=2 if downstream_before_lock_owner else 3,
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "final verifier",
            "deterministic",
            "fixture",
            command=command,
            depends_on=(
                ("upstream", "worker", "lock-owner", "downstream")
                if with_upstream
                else ("worker", "lock-owner", "downstream")
            ),
            read_scopes=("packages/a/package.json", "pnpm-lock.yaml"),
            verifier=True,
            ordinal=4,
        )
        nodes = [worker, lock_owner, downstream, verifier]
        if with_upstream:
            nodes.insert(0, upstream)
        self.store.create_task(contract, nodes, "create-" + task_id)
        self.store.queue_task(task_id)
        if with_upstream:
            upstream_claim = self.store.claim_ready_node("upstream-worker", self.epoch)
            assert upstream_claim is not None
            self.assertEqual(upstream_claim["node_id"], "upstream")
            self.store.settle_claimed(
                upstream_claim,
                NodeResult(
                    "succeeded",
                    "upstream has no patch",
                    result_kind="worker",
                    changed_paths=(),
                ),
            )
        worker_claim = self.store.claim_ready_node("blocked-worker", self.epoch)
        assert worker_claim is not None
        self.assertEqual(worker_claim["node_id"], "worker")
        source = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            task_id,
            "worker",
            int(worker_claim["attempt"]),
        )
        self.store.assign_worktree(
            task_id,
            "worker",
            str(source),
            attempt=int(worker_claim["attempt"]),
            coordinator_epoch=int(worker_claim["coordinator_epoch"]),
            lease_epoch=int(worker_claim["lease_epoch"]),
        )
        self._write_workspace(source, with_workspace_dependency=True)
        self.assertEqual(
            self._git(source, "diff", "--name-only", self.base_sha),
            "packages/a/package.json",
        )
        artifacts: dict[str, str] = {}
        if with_upstream:
            dependency_receipt = {
                "schema_version": 1,
                "kind": "accepted-ancestor-patch-input",
                "task_id": task_id,
                "node_id": "worker",
                "contract_base_sha": self.base_sha,
                "input_tree_sha": self._git(source, "rev-parse", f"{self.base_sha}^{{tree}}"),
                "ancestors": [
                    {"node_id": "upstream", "attempt": 1, "patch_ref": None}
                ],
            }
            artifacts["dependency-input"] = self.store.artifacts.put_text(
                canonical_json(dependency_receipt),
                "dependency-input.json",
            )
        self.store.settle_claimed(
            worker_claim,
            NodeResult(
                "blocked",
                "worker needs a separately owned pnpm lockfile",
                result_kind="worker",
                changed_paths=("packages/a/package.json",),
                artifacts=artifacts,
                checks=("fixture blocked after importer change",),
            ),
        )
        task = self.store.get_task(task_id)
        self.assertEqual(task["state"], "blocked")
        return contract, task, source

    def _apply_ready_handoff(
        self,
        contract: TaskContract,
        blocked: dict[str, object],
        request_id: str,
    ) -> dict[str, object]:
        worker = next(
            node for node in blocked["nodes"] if node["node_id"] == "worker"  # type: ignore[index]
        )
        arguments = {
            "op": "preview",
            "task_id": contract.task_id,
            "node_id": "worker",
            "expected_revision": blocked["state_revision"],
            "expected_attempt": worker["attempt"],
            "expected_contract_hash": blocked["contract_hash"],
            "request_id": request_id,
        }
        preview = lockfile_handoff(self.config, self.store, arguments)
        from tests.test_lockfile_handoff import _FixtureMaterializer

        with patch(
            "codex_workbench.lockfile_handoff._pnpm_materializer",
            return_value=_FixtureMaterializer(),
        ):
            response = self.authority.dispatch(
                {
                    "request_id": request_id,
                    "task_id": contract.task_id,
                    "tool": TOOL_NAME,
                    "arguments": {
                        **arguments,
                        "op": "apply",
                        "expected_fingerprint": preview["fingerprint"],
                    },
                }
            )
        self.assertEqual(response["state"], "completed")
        result = response["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["state"], "ready")
        return result

    def _amend_contract_with_rebound_handoffs(
        self,
        task_id: str,
        *,
        node_id: str,
    ) -> dict[str, object]:
        """Apply the contract-only part of the fixed D scope amendment."""

        before = self.store.get_task(task_id)
        old_contract = dict(before["contract"])
        new_contract = {
            **old_contract,
            "allowed_scope": [*old_contract["allowed_scope"], "docs/generated.md"],
        }
        new_contract_hash = canonical_hash(new_contract)
        revision = int(before["state_revision"]) + 1
        payload = {
            "profile_id": "dsh-task-template-integration-v1",
            "revision": revision,
            "old_contract": old_contract,
            "old_contract_hash": before["contract_hash"],
            "new_contract": new_contract,
            "new_contract_hash": new_contract_hash,
        }
        timestamp = now_iso()
        with self.store.transaction() as connection:
            changed = connection.execute(
                "UPDATE tasks SET contract_json = ?, contract_hash = ?, "
                "state_revision = ?, updated_at = ? WHERE task_id = ? "
                "AND state_revision = ? AND contract_hash = ?",
                (
                    canonical_json(new_contract),
                    new_contract_hash,
                    revision,
                    timestamp,
                    task_id,
                    before["state_revision"],
                    before["contract_hash"],
                ),
            ).rowcount
            self.assertEqual(changed, 1)
            self.store._event(
                connection,
                "task.blocked_integration_scope_amended",
                task_id,
                node_id,
                payload,
                created_at=timestamp,
            )
        return self.store.get_task(task_id)

    def _resume_via_control_task(
        self,
        contract: TaskContract,
        blocked: dict[str, object],
    ) -> dict[str, object]:
        worker = next(
            node for node in blocked["nodes"] if node["node_id"] == "worker"  # type: ignore[index]
        )
        response = self.mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "workbench_control_task",
                    "arguments": {
                        "task_id": contract.task_id,
                        "action": "resume",
                        "expected_revision": blocked["state_revision"],
                        "node_id": "worker",
                        "expected_attempt": worker["attempt"],
                        "reason": "continue the blocked worker against its ready lockfile input",
                        "confirm_recovery": True,
                    },
                },
            }
        )
        assert response is not None
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["action"], "resume-blocked-worktree")
        self.assertEqual(payload["task"]["state"], "queued")
        return payload

    def _run_scheduler_to_terminal(self, task_id: str) -> dict[str, Path]:
        coordinator = Coordinator(
            self.store,
            self.config.state_root,
            coordinator_epoch=self.epoch,
            max_workers=1,
            pnpm_materializer=_RecoveryMaterializer(),
        )
        observed: dict[str, Path] = {}
        pnpm = self.root / "pnpm-11-fixture"
        pnpm.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  echo 11.25.0\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        pnpm.chmod(0o700)
        try:
            with patch.dict(os.environ, {"CODEX_WORKBENCH_PNPM": str(pnpm)}):
                for index in range(8):
                    claimed = coordinator._claim_next_ready_node(
                        f"lockfile-handoff-input-{index}"
                    )
                    if claimed is None:
                        self.fail(
                            "scheduler did not claim a queued handoff continuation: "
                            + self._failure_summary(
                                self.store.get_task(task_id),
                                "<none>",
                            )
                        )
                    coordinator._execute_claimed(claimed)
                    task = self.store.get_task(task_id)
                    if task["state"] == "blocked":
                        self.fail(
                            "handoff continuation returned to blocked state: "
                            + self._failure_summary(task, str(claimed["node_id"]))
                        )
                    node = next(
                        item
                        for item in task["nodes"]
                        if item["node_id"] == claimed["node_id"]
                    )
                    if isinstance(node.get("worktree"), str):
                        observed[str(claimed["node_id"])] = Path(node["worktree"])
                    if task["state"] in {"accepted", "blocked", "needs_fix"}:
                        break
        finally:
            coordinator._pool.shutdown(wait=True)
        return observed

    def _run_worker_then_block_downstream(self, task_id: str) -> dict[str, object]:
        """Accept the resumed owner, then create one durable blocked consumer."""

        coordinator = Coordinator(
            self.store,
            self.config.state_root,
            coordinator_epoch=self.epoch,
            max_workers=1,
            pnpm_materializer=_RecoveryMaterializer(),
        )
        pnpm = self.root / "pnpm-11-owner-repair-fixture"
        pnpm.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  echo 11.25.0\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        pnpm.chmod(0o700)
        try:
            with patch.dict(os.environ, {"CODEX_WORKBENCH_PNPM": str(pnpm)}):
                owner = coordinator._claim_next_ready_node("scope-owner-repair-worker")
                assert owner is not None
                self.assertEqual(owner["node_id"], "worker")
                coordinator._execute_claimed(owner)
                consumer = coordinator._claim_next_ready_node("scope-owner-repair-consumer")
                assert consumer is not None
                self.assertEqual(consumer["node_id"], "downstream")
                self.store.settle_claimed(
                    consumer,
                    NodeResult(
                        "blocked",
                        "downstream requires its accepted owner to continue",
                        result_kind="worker",
                        changed_paths=(),
                        checks=("fixture blocked consumer",),
                    ),
                )
                return consumer
        finally:
            coordinator._pool.shutdown(wait=True)

    def _failure_summary(self, task: dict[str, object], node_id: str) -> str:
        node = next(
            (
                item
                for item in task["nodes"]  # type: ignore[index]
                if item["node_id"] == node_id
            ),
            None,
        )
        result = node.get("result") if isinstance(node, dict) else None
        artifacts = result.get("artifacts") if isinstance(result, dict) else None
        failure_ref = (
            artifacts.get("harness-failure")
            if isinstance(artifacts, dict)
            else None
        )
        failure = None
        if isinstance(failure_ref, str):
            failure = self.store.artifacts.verify(failure_ref).read_text(
                encoding="utf-8"
            )
        events = self.store.read_events(task_id=str(task["task_id"]))[-4:]
        return canonical_json(
            {
                "task_state": task["state"],
                "node": {
                    "node_id": node_id,
                    "state": node.get("state") if isinstance(node, dict) else None,
                    "attempt": node.get("attempt") if isinstance(node, dict) else None,
                    "summary": result.get("summary") if isinstance(result, dict) else None,
                    "harness_failure": failure,
                },
                "events": [
                    {
                        "cursor": event["cursor"],
                        "event_type": event["event_type"],
                        "node_id": event["node_id"],
                    }
                    for event in events
                ],
            }
        )

    def test_ready_overlay_replays_through_control_resume_downstream_and_verifier(self) -> None:
        contract, blocked, source = self._blocked_task(
            task_id="handoff-control-resume",
            with_upstream=True,
        )
        original_lock = (source / "pnpm-lock.yaml").read_bytes()
        self._apply_ready_handoff(contract, blocked, "handoff-control-resume-ready")
        ready = get_ready_lockfile_handoffs(self.store, contract.task_id)
        self.assertEqual(len(ready), 1)
        repaired_lock = self.store.artifacts.verify(
            ready[0]["lockfile"]["artifact_ref"]
        ).read_bytes()
        self.assertNotEqual(repaired_lock, original_lock)
        self._resume_via_control_task(contract, self.store.get_task(contract.task_id))
        worktrees = self._run_scheduler_to_terminal(contract.task_id)

        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "accepted")
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        worker_artifacts = worker["result"]["artifacts"]
        input_ref = worker_artifacts["dependency-input"]
        worker_input = load_recorded_dependency_input(
            self.store.artifacts,
            input_ref,
            task_id=contract.task_id,
            node_id="worker",
            base_sha=self.base_sha,
        )
        self.assertEqual(worker_input.receipt["schema_version"], 2)
        self.assertEqual(
            worker_input.receipt["lockfile_handoffs"][0]["lockfile"]["artifact_ref"],
            ready[0]["lockfile"]["artifact_ref"],
        )
        worker_patch = self.store.artifacts.verify(worker_artifacts["patch"]).read_bytes()
        self.assertNotIn(b"pnpm-lock.yaml", worker_patch)
        self.assertEqual((source / "pnpm-lock.yaml").read_bytes(), original_lock)

        replay = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            contract.task_id,
            "recorded-input-replay",
            1,
        )
        replayed_input = apply_recorded_dependency_input(
            self.store.artifacts,
            self.worktrees,
            ref=input_ref,
            task_id=contract.task_id,
            node_id="worker",
            base_sha=self.base_sha,
            worktree=replay,
        )
        self.assertEqual(replayed_input.input_tree_sha, worker_input.input_tree_sha)
        self.assertEqual((replay / "pnpm-lock.yaml").read_bytes(), repaired_lock)
        self.assertNotIn(
            "@fixture/b",
            (replay / "packages" / "a" / "package.json").read_text(encoding="utf-8"),
        )
        self.worktrees.apply_patch(replay, self.store.artifacts.verify(worker_artifacts["patch"]))
        self.assertEqual(
            (replay / "packages" / "a" / "package.json").read_bytes(),
            (worktrees["worker"] / "packages" / "a" / "package.json").read_bytes(),
        )
        self.assertEqual((replay / "pnpm-lock.yaml").read_bytes(), repaired_lock)

        for node_id in ("worker", "lock-owner", "downstream", "verify"):
            self.assertIn(node_id, worktrees)
            self.assertEqual(
                (worktrees[node_id] / "pnpm-lock.yaml").read_bytes(),
                repaired_lock,
            )
            result = next(node for node in task["nodes"] if node["node_id"] == node_id)["result"]
            ref = result["artifacts"]["dependency-input"]
            receipt = json.loads(self.store.artifacts.verify(ref).read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(
                receipt["lockfile_handoffs"][0]["request_id"],
                ready[0]["request_id"],
            )

    def test_base_only_owner_receives_ready_overlay_after_control_resume(self) -> None:
        contract, blocked, _source = self._blocked_task(
            task_id="handoff-base-only",
            with_upstream=False,
        )
        self._apply_ready_handoff(contract, blocked, "handoff-base-only-ready")
        self._resume_via_control_task(contract, self.store.get_task(contract.task_id))
        worktrees = self._run_scheduler_to_terminal(contract.task_id)
        worker = next(
            node for node in self.store.get_task(contract.task_id)["nodes"]
            if node["node_id"] == "worker"
        )
        self.assertEqual((worker["state"], worker["attempt"]), ("accepted", 2))
        self.assertEqual(
            json.loads(
                self.store.artifacts.verify(
                    worker["result"]["artifacts"]["dependency-input"]
                ).read_text(encoding="utf-8")
            )["schema_version"],
            2,
        )
        self.assertIn("worker", worktrees)

    def test_descendant_scope_event_rebinds_ancestor_overlay_and_allows_resume(self) -> None:
        contract, blocked, _source = self._blocked_task(
            task_id="handoff-contract-rebind",
            with_upstream=False,
        )
        self._apply_ready_handoff(contract, blocked, "handoff-contract-rebind-ready")
        ready_before = get_ready_lockfile_handoffs(self.store, contract.task_id)
        self.assertEqual(len(ready_before), 1)

        amended = self._amend_contract_with_rebound_handoffs(
            contract.task_id,
            node_id="D",
        )
        ready_after = get_ready_lockfile_handoffs(self.store, contract.task_id)
        self.assertEqual(len(ready_after), 1)
        self.assertEqual(ready_after[0]["contract_hash"], amended["contract_hash"])
        self.assertEqual(ready_after[0]["task_revision"], amended["state_revision"])
        self.assertEqual(
            {
                key: value
                for key, value in ready_after[0].items()
                if key not in {"contract_hash", "task_revision"}
            },
            {
                key: value
                for key, value in ready_before[0].items()
                if key not in {"contract_hash", "task_revision"}
            },
        )

        self._resume_via_control_task(contract, amended)
        worktrees = self._run_scheduler_to_terminal(contract.task_id)
        task = self.store.get_task(contract.task_id)
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]),
                         ("accepted", "accepted", 2))
        self.assertIn("worker", worktrees)

    def test_scope_amendment_lineage_allows_blocked_consumer_owner_repair(self) -> None:
        contract, blocked, _source = self._blocked_task(
            task_id="handoff-scope-owner-repair",
            with_upstream=False,
            downstream_before_lock_owner=True,
        )
        self._apply_ready_handoff(contract, blocked, "handoff-scope-owner-repair-ready")
        ready_before = get_ready_lockfile_handoffs(self.store, contract.task_id)
        self._resume_via_control_task(contract, self.store.get_task(contract.task_id))
        consumer = self._run_worker_then_block_downstream(contract.task_id)
        before_amendment = self.store.get_task(contract.task_id)
        owner_before = next(
            node for node in before_amendment["nodes"] if node["node_id"] == "worker"
        )
        recorded_ref = owner_before["result"]["artifacts"]["dependency-input"]
        recorded = load_recorded_dependency_input(
            self.store.artifacts,
            recorded_ref,
            task_id=contract.task_id,
            node_id="worker",
            base_sha=self.base_sha,
        )
        self.assertEqual(
            recorded.receipt["lockfile_handoffs"][0]["contract_hash"],
            ready_before[0]["contract_hash"],
        )

        amended = self._amend_contract_with_rebound_handoffs(
            contract.task_id,
            node_id="downstream",
        )
        with self.store.connection() as connection:
            amendment_row = connection.execute(
                "SELECT cursor, payload_json FROM events WHERE task_id = ? "
                "AND event_type = 'task.blocked_integration_scope_amended'",
                (contract.task_id,),
            ).fetchone()
        assert amendment_row is not None
        original_amendment = str(amendment_row["payload_json"])
        forged_amendment = json.loads(original_amendment)
        forged_amendment["new_contract"]["objective"] = "forged lineage"
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (canonical_json(forged_amendment), amendment_row["cursor"]),
            )
        task_before_rejection = self.store.get_task(contract.task_id)
        with self.assertRaisesRegex(
            AcceptedSourceRepairError,
            "dependency input is invalid",
        ):
            self.store.schedule_blocked_consumer_owner_repairs(
                contract.task_id,
                "downstream",
                ["worker"],
                {"worker": "must reject a forged scope lineage"},
                expected_revision=int(amended["state_revision"]),
                expected_attempt=int(consumer["attempt"]),
                reason="forged lineage must not authorize owner repair",
            )
        self.assertEqual(
            self.store.get_task(contract.task_id),
            task_before_rejection,
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (original_amendment, amendment_row["cursor"]),
            )
        receipt = self.store.schedule_blocked_consumer_owner_repairs(
            contract.task_id,
            "downstream",
            ["worker"],
            {"worker": "continue the accepted owner on the amended scope"},
            expected_revision=int(amended["state_revision"]),
            expected_attempt=int(consumer["attempt"]),
            reason="scope-amended downstream requires its accepted owner",
        )

        self.assertEqual(receipt["revision"], int(amended["state_revision"]) + 1)
        task = self.store.get_task(contract.task_id)
        owner = next(node for node in task["nodes"] if node["node_id"] == "worker")
        self.assertEqual((task["state"], owner["state"], owner["attempt"]), ("queued", "pending", 2))
        with self.store.connection() as connection:
            recovery_json = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'worker'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
        binding = json.loads(str(recovery_json))
        self.assertEqual(
            binding["source"]["dependency_input"]["lockfile_handoffs"][0]["contract_hash"],
            ready_before[0]["contract_hash"],
        )
        self.assertEqual(
            get_ready_lockfile_handoffs(self.store, contract.task_id)[0]["contract_hash"],
            amended["contract_hash"],
        )

        coordinator = Coordinator(
            self.store,
            self.config.state_root,
            coordinator_epoch=self.epoch,
            max_workers=1,
            pnpm_materializer=_RecoveryMaterializer(),
        )
        pnpm = self.root / "pnpm-11-owner-continuation-fixture"
        pnpm.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  echo 11.25.0\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        pnpm.chmod(0o700)
        try:
            with patch.dict(os.environ, {"CODEX_WORKBENCH_PNPM": str(pnpm)}):
                claimed = coordinator._claim_next_ready_node("scope-amended-owner-continuation")
                assert claimed is not None
                self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 3))
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        continued = self.store.get_task(contract.task_id)
        owner = next(node for node in continued["nodes"] if node["node_id"] == "worker")
        continued_worktree = Path(owner["worktree"])
        restored_files = {
            "package": (continued_worktree / "packages/a/package.json").read_text(
                encoding="utf-8"
            ),
            "lockfile": (continued_worktree / "pnpm-lock.yaml").read_text(
                encoding="utf-8"
            ),
        }
        self.assertEqual(
            (owner["state"], owner["attempt"]),
            ("accepted", 3),
            self._failure_summary(continued, "worker") + canonical_json(restored_files),
        )
        package = json.loads(
            (continued_worktree / "packages/a/package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["dependencies"]["@fixture/b"], "workspace:*")
        continued_input = load_recorded_dependency_input(
            self.store.artifacts,
            owner["result"]["artifacts"]["dependency-input"],
            task_id=contract.task_id,
            node_id="worker",
            base_sha=self.base_sha,
        )
        self.assertEqual(continued_input.input_tree_sha, recorded.input_tree_sha)
        self.assertEqual(
            continued_input.receipt["lockfile_handoffs"][0]["contract_hash"],
            amended["contract_hash"],
        )

    def test_recorded_overlay_rebind_changes_no_content_identity(self) -> None:
        contract, blocked, _source = self._blocked_task(
            task_id="handoff-recorded-contract-rebind",
            with_upstream=False,
        )
        self._apply_ready_handoff(contract, blocked, "handoff-recorded-rebind-ready")
        ready_before = get_ready_lockfile_handoffs(self.store, contract.task_id)
        self._amend_contract_with_rebound_handoffs(
            contract.task_id,
            node_id="D",
        )
        ready_after = get_ready_lockfile_handoffs(self.store, contract.task_id)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET attempt = 2 WHERE task_id = ? AND node_id = 'worker'",
                (contract.task_id,),
            )
        amended = self.store.get_task(contract.task_id)
        tree = self._git(self.repository, "rev-parse", f"{self.base_sha}^{{tree}}")
        recorded = DependencyInput(
            input_tree_sha=tree,
            receipt={
                "schema_version": 2,
                "kind": "accepted-ancestor-patch-input",
                "task_id": contract.task_id,
                "node_id": "worker",
                "contract_base_sha": self.base_sha,
                "input_tree_sha": tree,
                "ancestors": [],
                "lockfile_handoffs": ready_before,
            },
        )
        rebound = rebind_recorded_lockfile_handoffs(
            amended,
            "worker",
            self.store.artifacts,
            recorded,
            ready_lockfile_handoffs=ready_after,
        )
        self.assertEqual(rebound.input_tree_sha, recorded.input_tree_sha)
        self.assertEqual(
            rebound.receipt["lockfile_handoffs"],
            ready_after,
        )
        self.assertEqual(
            rebound.receipt["lockfile_handoffs"][0]["lockfile"],
            recorded.receipt["lockfile_handoffs"][0]["lockfile"],
        )

    def test_contract_rebinding_event_tampering_is_rejected(self) -> None:
        contract, blocked, _source = self._blocked_task(
            task_id="handoff-contract-rebind-tamper",
            with_upstream=False,
        )
        self._apply_ready_handoff(contract, blocked, "handoff-contract-rebind-tamper-ready")
        self._amend_contract_with_rebound_handoffs(
            contract.task_id,
            node_id="D",
        )
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT cursor, payload_json FROM events WHERE task_id = ? "
                "AND event_type = 'task.blocked_integration_scope_amended'",
                (contract.task_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            payload = json.loads(str(row["payload_json"]))
            payload["new_contract"]["objective"] = "tampered"
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (canonical_json(payload), row["cursor"]),
            )
        with self.assertRaisesRegex(LockfileHandoffError, "new contract is invalid"):
            get_ready_lockfile_handoffs(self.store, contract.task_id)

    def test_manifest_drift_and_bad_artifact_ref_are_rejected_before_overlay_write(self) -> None:
        contract, blocked, _source = self._blocked_task(
            task_id="handoff-input-rejections",
            with_upstream=False,
        )
        self._apply_ready_handoff(contract, blocked, "handoff-input-rejections-ready")
        ready = get_ready_lockfile_handoffs(self.store, contract.task_id)
        malformed = json.loads(canonical_json(ready))
        malformed[0]["lockfile"]["artifact_ref"] = "sha256:" + "0" * 64 + ":lockfile"
        with self.assertRaises(LockfileHandoffInputError):
            normalize_lockfile_handoffs(
                self.store.artifacts,
                malformed,
                task_id=contract.task_id,
                contract_hash=blocked["contract_hash"],
            )

        target = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            contract.task_id,
            "manual-target",
            1,
        )
        package = target / "packages" / "a" / "package.json"
        package.write_text("{\"changed\": true}\n", encoding="utf-8")
        handoff = normalize_lockfile_handoffs(
            self.store.artifacts,
            ready,
            task_id=contract.task_id,
            contract_hash=blocked["contract_hash"],
        )[0]
        with self.assertRaisesRegex(LockfileHandoffInputError, "manifest drifted"):
            apply_pending_lockfile_handoffs(
                target,
                self.store.artifacts,
                (handoff,),
                (),
                applied_request_ids=set(),
            )


if __name__ == "__main__":
    unittest.main()
