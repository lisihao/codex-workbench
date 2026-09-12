from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import codex_workbench.service as service_module
from codex_workbench.execution_attribution import (
    ExecutionAttribution,
    ExecutionStateReference,
    FailureAttribution,
    RequestedModelIdentity,
)
from codex_workbench.model import NodeSpec, NodeResult, TaskContract
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore


def _verified(nodes: list[NodeSpec], task_id: str) -> list[NodeSpec]:
    if any(node.verifier for node in nodes):
        return nodes
    return [
        *nodes,
        NodeSpec(
            "verify", task_id, "verify", "fixture", "fixture", "accepted",
            depends_on=tuple(node.node_id for node in nodes), verifier=True,
        ),
    ]


class HarnessFailureTests(unittest.TestCase):
    def _fixture(
        self,
        *,
        with_source_package: bool = False,
        verifier_node: bool = False,
    ) -> tuple[Path, WorkbenchStore, int, dict]:
        directory = tempfile.TemporaryDirectory(prefix="workbench-harness-failure-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        if with_source_package:
            package = root / "src" / "fixture_package"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("fixture = True\n", encoding="utf-8")
        store = WorkbenchStore(root / "state.sqlite")
        store.initialize()
        epoch = store.activate_coordinator("harness-failure-fixture", "fixture-machine")
        task_id = "harness-failure-task"
        contract = TaskContract(
            task_id=task_id,
            repository=str(root),
            base_sha="fixture-base",
            objective="bounded harness failure fixture",
            allowed_scope=("src",),
            required_artifacts=(),
        )
        nodes = (
            [NodeSpec("verify", task_id, "verify", "fixture", "fixture", "run", verifier=True)]
            if verifier_node
            else _verified([NodeSpec("work", task_id, "work", "fixture", "fixture", "run")], task_id)
        )
        store.create_task(contract, nodes, "harness-failure-create")
        store.queue_task(task_id)
        claimed = store.claim_ready_node("harness-failure-worker", epoch)
        assert claimed is not None
        return root, store, epoch, claimed

    def test_internal_pre_execution_failure_is_typed_and_executor_is_not_called(self) -> None:
        root, store, epoch, claimed = self._fixture(with_source_package=True)
        coordinator = Coordinator(store, root, coordinator_epoch=epoch)
        executor = MagicMock()
        try:
            with patch.object(service_module, "SourceResolutionRequirement", 1), patch.object(
                coordinator, "_executor", return_value=executor
            ):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        executor.execute.assert_not_called()
        node = next(item for item in store.get_task(claimed["task_id"])["nodes"] if item["node_id"] == "work")
        result = node["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["execution_attribution"]["failure"]["origin"], "tooling_bug")
        self.assertEqual(result["result_kind"], "worker")
        self.assertIsNone(result["verdict"])
        ref = result["artifacts"]["harness-failure"]
        evidence = json.loads(coordinator.artifacts.verify(ref).read_text(encoding="utf-8"))
        self.assertEqual(
            set(evidence),
            {"kind", "task_id", "node_id", "attempt", "phase", "error_type", "relative_module", "line", "executor_started"},
        )
        self.assertEqual(evidence["kind"], "harness-failure")
        self.assertEqual(evidence["phase"], "pre_execution")
        self.assertEqual(evidence["error_type"], "TypeError")
        self.assertEqual(evidence["relative_module"], "service.py")
        self.assertFalse(evidence["executor_started"])
        from codex_workbench.node_recovery_observation import collect_node_observation

        observed = collect_node_observation(store, claimed["task_id"], "work")
        self.assertEqual(observed["category"], "tooling_bug")
        self.assertEqual(observed["phase"], "pre_execution")
        self.assertTrue(observed["executor_not_started"])
        # Exercise the real metadata-only planning adapter from this actual
        # Coordinator receipt; no planning consumer or provider is started.
        import subprocess
        from codex_workbench.authority_service import AuthorityService
        from codex_workbench.config import WorkbenchConfig
        from codex_workbench.mcp import WorkbenchMCPServer
        from codex_workbench.node_recovery import NodeRecoveryReconciler
        from codex_workbench.node_recovery_policy import RecoveryPolicy
        from codex_workbench.node_recovery_repair import RepairNodeActions
        from codex_workbench.node_recovery_store import NodeRecoveryStore

        repository = root / "repair-repository"
        repository.mkdir()
        (repository / "src").mkdir()
        (repository / "src" / "fixture.py").write_text("value = 1\n")
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(["git", "add", "src/fixture.py"], cwd=repository, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "fixture"], cwd=repository, check=True)
        config = WorkbenchConfig(root)
        projection = NodeRecoveryStore(store)
        projection.configure_policy(claimed["task_id"], RecoveryPolicy(
            enabled=True, allowed_actions=("request_repair",), repair_repository=str(repository),
            repair_allowed_scopes=("src",),
        ), expected_task_revision=store.get_task(claimed["task_id"])["state_revision"], actor="fixture")
        mcp = WorkbenchMCPServer(config, store)
        authority = AuthorityService(store, mcp._tool_result, "fixture-authority")
        loop = NodeRecoveryReconciler(store, coordinator_epoch=epoch,
            adapters={"request_repair": RepairNodeActions(config, projection, authority)})
        loop.reconcile_once()
        loop.reconcile_once()
        episode = projection.get_episode(projection.list_summary(task_id=claimed["task_id"])[0]["episode_id"])
        self.assertIsNotNone(episode["repair"])
        self.assertEqual(episode["state"], "waiting")
        request = store.get_planning_request(episode["repair"]["repair_request_id"])
        self.assertEqual(request["state"], "pending")
        self.assertEqual(projection.metrics(task_id=claimed["task_id"])["total_action_count"], 1)
        self.assertEqual(projection.metrics(task_id=claimed["task_id"])["unknown_action_count"], 0)
        self.assertEqual(
            result["execution_attribution"]["failure"]["references"][0]["ref"],
            ref,
        )

    def test_external_pre_execution_failure_remains_indeterminate(self) -> None:
        root, store, epoch, claimed = self._fixture(with_source_package=True)
        coordinator = Coordinator(store, root, coordinator_epoch=epoch)

        def external_failure(*_args: object, **_kwargs: object) -> object:
            raise TypeError("fixture external failure")

        try:
            with patch.object(service_module, "SourceResolutionRequirement", external_failure):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        node = next(item for item in store.get_task(claimed["task_id"])["nodes"] if item["node_id"] == "work")
        result = node["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "indeterminate")
        self.assertNotIn("harness-failure", result["artifacts"])
        self.assertEqual(result["execution_attribution"]["failure"]["origin"], "unknown")

    def test_internal_pre_execution_verifier_failure_preserves_blocked_verdict(self) -> None:
        root, store, epoch, claimed = self._fixture(
            with_source_package=True, verifier_node=True
        )
        coordinator = Coordinator(store, root, coordinator_epoch=epoch)
        try:
            with patch.object(service_module, "SourceResolutionRequirement", 1):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        node = next(item for item in store.get_task(claimed["task_id"])["nodes"] if item["node_id"] == "verify")
        result = node["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["result_kind"], "verifier")
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["execution_attribution"]["failure"]["origin"], "tooling_bug")

    def test_executor_started_failure_remains_indeterminate(self) -> None:
        root, store, epoch, claimed = self._fixture()
        coordinator = Coordinator(store, root, coordinator_epoch=epoch)

        class FailingExecutor:
            def execute(self, _request: object) -> NodeResult:
                raise TypeError("executor fixture failure")

        try:
            with patch.object(coordinator, "_executor", return_value=FailingExecutor()):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        node = next(item for item in store.get_task(claimed["task_id"])["nodes"] if item["node_id"] == "work")
        result = node["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "indeterminate")
        self.assertNotIn("harness-failure", result["artifacts"])
        self.assertEqual(result["execution_attribution"]["failure"]["origin"], "unknown")

    def test_tooling_bug_origin_round_trips_as_typed_attribution(self) -> None:
        raw = ExecutionAttribution(
            state=ExecutionStateReference("task", "node", 1),
            requested_model=RequestedModelIdentity(provider="fixture", model_id="fixture"),
            failure=FailureAttribution(origin="tooling_bug"),
        ).to_dict()
        restored = ExecutionAttribution.from_dict(raw)
        self.assertEqual(restored.failure.origin, "tooling_bug")


if __name__ == "__main__":
    unittest.main()
