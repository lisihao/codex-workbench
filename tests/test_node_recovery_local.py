"""Fixture-only coverage for bounded local blocked-node recovery actions."""

from __future__ import annotations

import subprocess
import tempfile
import unittest

from collections.abc import Callable
from pathlib import Path

from codex_workbench.config import WorkbenchConfig
from codex_workbench.execution_attribution import (
    AttributionReference,
    ExecutionAttribution,
    ExecutionCondition,
    ExecutionConditions,
    ExecutionStateReference,
    ExecutionTimings,
    FailureAttribution,
    PhaseTiming,
    RequestedModelIdentity,
)
from codex_workbench.execution_readiness import (
    ExecutionReadinessReport,
    ExecutionReadinessRequest,
    ReadinessFailure,
)
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_hash, canonical_json
from codex_workbench.node_recovery import NodeRecoveryReconciler
from codex_workbench.node_recovery_local import LocalNodeActions
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_readiness import ReadinessNodeActions
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager
from tests.process_probe_fixture import isolated_process_catalog


class _FixtureMaterializer:
    """Record only the fixed materializer inputs; never invokes pnpm."""

    binary = "fixture-pnpm"
    store_dir = None
    template_dir = None

    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[Path, int, bool]] = []
        self.callback: Callable[[], None] | None = None
        self.result = result or {
            "schema_version": 1,
            "kind": "pnpm-offline-materialization",
            "status": "succeeded",
        }

    def materialize(
        self,
        worktree: Path,
        *,
        timeout_seconds: int,
        require_cached_template: bool,
    ) -> dict[str, object]:
        self.calls.append((worktree, timeout_seconds, require_cached_template))
        if self.callback is not None:
            self.callback()
        return dict(self.result)


class _UncertainMaterializer(_FixtureMaterializer):
    """Model an interrupted cached-template operation after dispatch."""

    def materialize(
        self,
        worktree: Path,
        *,
        timeout_seconds: int,
        require_cached_template: bool,
    ) -> dict[str, object]:
        self.calls.append((worktree, timeout_seconds, require_cached_template))
        raise OSError("fixture interrupted after local materialization began")


class LocalNodeActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_process_catalog(()))
        self.temp = tempfile.TemporaryDirectory(prefix="node-recovery-local-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "user.name", "Fixture")
        (self.repository / "src" / "base.py").write_text("BASE = True\n", encoding="utf-8")
        self._git(self.repository, "add", "src/base.py")
        self._git(self.repository, "commit", "-m", "base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.state_root = self.root / "state"
        self.store = WorkbenchStore(self.state_root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("local-recovery-fixture", "fixture")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")
        self.config = WorkbenchConfig(self.state_root)
        self.materializer = _FixtureMaterializer()
        self.adapter = LocalNodeActions(
            self.config,
            self.store,
            readiness_request_factory=lambda worktree: ExecutionReadinessRequest(
                worktree=worktree, pnpm=None
            ),
            materializer=self.materializer,  # type: ignore[arg-type]
        )

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def _blocked_fixture(self) -> tuple[TaskContract, dict[str, object], Path]:
        contract = TaskContract(
            task_id="local-recovery-fixture",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="prove only a clean pre-execution readiness block can resume",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "blocked before execution",
            "fixture",
            "fixture",
            "do not run",
            write_scopes=("src",),
        )
        verifier = NodeSpec(
            "verify",
            contract.task_id,
            "fixture verifier",
            "fixture",
            "fixture",
            "accepted",
            depends_on=(worker.node_id,),
            verifier=True,
        )
        self.store.create_task(contract, [worker, verifier], "local-recovery-create")
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], "worker")
        worktree = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            contract.task_id,
            "worker",
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            contract.task_id,
            "worker",
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        readiness_ref = self.store.artifacts.put_text(
            canonical_json(
                ExecutionReadinessReport(
                    worktree=str(worktree),
                    ready=False,
                    checks=(),
                    failures=(
                        ReadinessFailure(
                            "pnpm:linker",
                            "missing-dependency",
                            "fixture dependency is absent",
                            "use the cached linker template",
                        ),
                    ),
                    elapsed_ms=0,
                ).to_dict()
            ),
            "execution-readiness.json",
        )
        reference = AttributionReference(kind="readiness_report", ref=readiness_ref)
        attribution = ExecutionAttribution(
            state=ExecutionStateReference(contract.task_id, "worker", int(claimed["attempt"])),
            requested_model=RequestedModelIdentity("fixture", "fixture"),
            failure=FailureAttribution("environment", "fixture pre-execution readiness failure", (reference,)),
            conditions=ExecutionConditions(
                environment_readiness=ExecutionCondition(
                    "environment_readiness",
                    "observed",
                    detail="the Authority readiness report blocked execution",
                    references=(reference,),
                )
            ),
            timings=ExecutionTimings(
                prepare=PhaseTiming(), execute=PhaseTiming(), verify=PhaseTiming()
            ),
        ).to_dict()
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "blocked",
                "fixture readiness blocked before the executor started",
                artifacts={"execution-readiness": readiness_ref},
                execution_attribution=attribution,
            ),
        )
        task = self.store.get_task(contract.task_id)
        NodeRecoveryStore(self.store).configure_policy(
            contract.task_id,
            RecoveryPolicy(
                enabled=True,
                allowed_actions=(
                    "observe_readiness",
                    "materialize_dependencies",
                    "resume_node",
                ),
            ),
            expected_task_revision=task["state_revision"],
            actor="fixture",
        )
        return contract, task, worktree

    @staticmethod
    def _node(task: dict[str, object], node_id: str) -> dict[str, object]:
        nodes = task["nodes"]
        assert isinstance(nodes, list)
        return next(node for node in nodes if node["node_id"] == node_id)

    def _observation(
        self,
        task: dict[str, object],
        *,
        readiness_ready: bool = True,
    ) -> dict[str, object]:
        worker = self._node(task, "worker")
        return {
            "task_id": task["task_id"],
            "node_id": "worker",
            "node_attempt": worker["attempt"],
            "task_revision": task["state_revision"],
            "phase": "pre_execution",
            "readiness_ready": readiness_ready,
            "only_missing_dependency": True,
            "executor_not_started": True,
        }

    def test_observe_readiness_delegates_to_existing_read_only_adapter(self) -> None:
        _, task, _ = self._blocked_fixture()
        plan = self.adapter.prepare(self._observation(task), "observe_readiness", "observe-1")
        before = self.store.get_task(str(task["task_id"]))
        receipt = self.adapter.execute(plan)
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["stage_succeeded"])
        self.assertTrue(receipt["known_effects"])
        self.assertEqual(self.store.get_task(str(task["task_id"])), before)
        self.assertTrue(receipt["observation_patch"]["readiness_ready"])
        self.assertIsNone(self.adapter.reconcile(plan))

    def test_materialization_is_cached_template_only_and_is_not_replayed(self) -> None:
        _, task, worktree = self._blocked_fixture()
        plan = self.adapter.prepare(self._observation(task), "materialize_dependencies", "materialize-1")
        receipt = self.adapter.execute(plan)
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.materializer.calls, [(worktree, 60, True)])
        self.assertEqual(
            receipt["observation_patch"],
            {"dependencies_ready": True, "readiness_ready": False},
        )
        with self.assertRaisesRegex(StateConflictError, "may have been dispatched"):
            self.adapter.execute(plan)
        self.assertEqual(len(self.materializer.calls), 1)

    def test_uncertain_materialization_is_not_replayed(self) -> None:
        _, task, _ = self._blocked_fixture()
        materializer = _UncertainMaterializer()
        adapter = LocalNodeActions(
            self.config,
            self.store,
            readiness_request_factory=lambda worktree: ExecutionReadinessRequest(
                worktree=worktree, pnpm=None
            ),
            materializer=materializer,  # type: ignore[arg-type]
        )
        plan = adapter.prepare(self._observation(task), "materialize_dependencies", "materialize-unknown")
        receipt = adapter.execute(plan)
        self.assertFalse(receipt["known_effects"])
        self.assertEqual(receipt["reason_kind"], "materialization_outcome_unknown")
        with self.assertRaisesRegex(StateConflictError, "may have been dispatched"):
            adapter.execute(plan)
        self.assertEqual(len(materializer.calls), 1)

    def test_materialization_does_not_report_ready_after_a_pause(self) -> None:
        contract, task, _ = self._blocked_fixture()
        self.materializer.callback = lambda: self.store.transition_task(
            contract.task_id, "paused", expected_revision=int(task["state_revision"])
        )
        plan = self.adapter.prepare(self._observation(task), "materialize_dependencies", "materialize-paused-during")
        receipt = self.adapter.execute(plan)
        self.assertFalse(receipt["ok"])
        self.assertTrue(receipt["known_effects"])
        self.assertEqual(receipt["reason_kind"], "materialization_postcondition_changed")
        self.assertEqual(len(self.materializer.calls), 1)

    def test_resume_retries_only_a_clean_current_pre_execution_block(self) -> None:
        contract, task, _ = self._blocked_fixture()
        original = self._node(task, "worker")["result"]
        plan = self.adapter.prepare(self._observation(task), "resume_node", "resume-1")
        receipt = self.adapter.execute(plan)
        retried = self.store.get_task(contract.task_id)
        worker = self._node(retried, "worker")
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["observation_patch"]["recovery_resumed"])
        self.assertEqual((retried["state"], worker["state"], worker["attempt"]), ("queued", "pending", 1))
        self.assertEqual(worker["result"], original)
        authorizations = [
            event for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.blocked_retry_authorized"
        ]
        self.assertEqual(len(authorizations), 1)
        # Without the outer NodeRecoveryStore intent, an event alone cannot
        # settle a possibly lost local action response.
        self.assertIsNone(self.adapter.reconcile(plan))

    def test_controller_loop_advances_same_node_through_materialize_readiness_and_resume(self) -> None:
        """Exercise the real outer action intent and source-checked retry CAS."""

        contract, _, _ = self._blocked_fixture()
        readiness = ReadinessNodeActions(
            self.config,
            self.store,
            readiness_request_factory=lambda worktree: ExecutionReadinessRequest(
                worktree=worktree, pnpm=None
            ),
        )
        local = LocalNodeActions(
            self.config,
            self.store,
            readiness_request_factory=lambda worktree: ExecutionReadinessRequest(
                worktree=worktree, pnpm=None
            ),
            materializer=self.materializer,  # type: ignore[arg-type]
        )
        loop = NodeRecoveryReconciler(
            self.store,
            coordinator_epoch=self.epoch,
            adapters={
                "observe_readiness": readiness,
                "materialize_dependencies": local,
                "resume_node": local,
            },
        )
        for _ in range(3):
            loop.reconcile_once()

        task = self.store.get_task(contract.task_id)
        worker = self._node(task, "worker")
        self.assertEqual((task["state"], worker["state"], worker["attempt"]), ("queued", "pending", 1))
        episode = NodeRecoveryStore(self.store).get_episode(
            NodeRecoveryStore(self.store).list_summary(task_id=contract.task_id)[0]["episode_id"]
        )
        actions = episode["actions"]
        intended_stages = [
            event["payload"]["stage_key"]
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node_recovery.action_intended"
        ]
        self.assertEqual(
            intended_stages,
            ["materialize_dependencies", "observe_readiness", "resume_node"],
        )
        for action in actions:
            self.assertEqual(action["action_fingerprint"], canonical_hash(action["action_input"]))
        resume_action = next(
            action for action in actions if action["action_input"]["action"] == "resume_node"
        )
        reconciled = local.reconcile(resume_action["action_input"])
        assert reconciled is not None
        self.assertTrue(reconciled["known_effects"])
        self.assertTrue(reconciled["observation_patch"]["recovery_resumed"])
        self.assertEqual(episode["observation"]["recovery_resumed"], True)
        self.assertEqual(self.materializer.calls[0][1:], (60, True))

    def test_resume_rejects_a_real_dirty_source_delta(self) -> None:
        contract, task, worktree = self._blocked_fixture()
        (worktree / "src" / "dirty.py").write_text("DIRTY = True\n", encoding="utf-8")
        receipt = self.adapter.execute(
            self.adapter.prepare(self._observation(task), "resume_node", "resume-dirty")
        )
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason_kind"], "resume_source_delta_not_empty")
        self.assertEqual(self.store.get_task(contract.task_id)["state"], "blocked")

    def test_resume_rejects_a_real_source_base_change(self) -> None:
        contract, task, worktree = self._blocked_fixture()
        self._git(worktree, "commit", "--allow-empty", "-m", "source base changed")
        receipt = self.adapter.execute(
            self.adapter.prepare(self._observation(task), "resume_node", "resume-base")
        )
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason_kind"], "resume_source_delta_unproven")
        self.assertEqual(self.store.get_task(contract.task_id)["state"], "blocked")

    def test_resume_rejects_allocation_mismatch_and_pause_rechecks(self) -> None:
        contract, task, _ = self._blocked_fixture()
        plan = self.adapter.prepare(self._observation(task), "resume_node", "resume-allocation")
        paused_plan = self.adapter.prepare(
            self._observation(task), "materialize_dependencies", "materialize-paused"
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET current_path = ?
                WHERE task_id = ? AND node_id = ? AND attempt = ?
                """,
                (str(self.root / "other"), contract.task_id, "worker", 1),
            )
        receipt = self.adapter.execute(plan)
        self.assertEqual(receipt["reason_kind"], "resume_durable_binding_unproven")
        self.assertEqual(self.store.get_task(contract.task_id)["state"], "blocked")

        self.store.transition_task(
            contract.task_id, "paused", expected_revision=int(task["state_revision"])
        )
        with self.assertRaisesRegex(StateConflictError, "pause or cancellation"):
            self.adapter.execute(paused_plan)


if __name__ == "__main__":
    unittest.main()
