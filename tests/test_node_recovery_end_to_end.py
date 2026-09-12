"""Real-ledger end-to-end coverage for the bounded blocked-source repair lane."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from codex_workbench.authority_service import AuthorityService
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.execution_attribution import (
    ExecutionAttribution,
    ExecutionStateReference,
    FailureAttribution,
    RequestedModelIdentity,
)
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.node_recovery_observation import collect_node_observation
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.service import Coordinator
from codex_workbench import node_recovery_source_repair
from tests.process_probe_fixture import isolated_process_catalog
from tests.test_failed_attempt_recovery import _FailedAttemptRecoveryFixture


class NodeRecoveryEndToEndTests(_FailedAttemptRecoveryFixture, unittest.TestCase):
    """Exercise collector, reconciler, retry, and original verification together."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(isolated_process_catalog(()))
        self.authority = AuthorityService(
            self.store,
            self.mcp._tool_result,
            "node-recovery-e2e-authority",
        )

    def _coordinator(self, epoch: int | None = None) -> Coordinator:
        coordinator = Coordinator(
            self.store,
            self.state_root,
            coordinator_epoch=self.epoch if epoch is None else epoch,
            config=self.config,
        )
        coordinator.bind_authority_service(self.authority)
        self.addCleanup(coordinator._pool.shutdown, wait=True)
        self.addCleanup(coordinator._delivery_pool.shutdown, wait=True)
        return coordinator

    def _configure_repair_policy(self, task_id: str, revision: int) -> None:
        response = self.authority.dispatch(
            {
                "request_id": f"configure-{task_id}",
                "tool": "workbench_configure_node_recovery",
                "task_id": task_id,
                "arguments": {
                    "task_id": task_id,
                    "expected_revision": revision,
                    "policy": {
                        "enabled": True,
                        "allowed_actions": ["repair_source"],
                        "max_action_attempts": 1,
                    },
                },
            }
        )
        self.assertEqual(response["state"], "completed")

    def _readiness_ref(self, worktree: str) -> str:
        return self.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "worktree": worktree,
                    "ready": True,
                    "failure_origin": None,
                    "summary": "fixture readiness is complete",
                    "elapsed_ms": 1,
                    "checks": [],
                    "failures": [],
                },
                sort_keys=True,
            ),
            "execution-readiness.json",
        )

    @staticmethod
    def _verification_attribution(task_id: str, node_id: str, attempt: int, cursor: int) -> dict:
        attribution = ExecutionAttribution(
            state=ExecutionStateReference(task_id, node_id, attempt, event_cursor=cursor),
            requested_model=RequestedModelIdentity(provider="fixture", model_id="fixture"),
        )
        return replace(
            attribution,
            failure=FailureAttribution(origin="verification"),
        ).to_dict()

    def _blocked_validation_task(self, *, task_id: str) -> tuple[TaskContract, object, str, dict]:
        """Create a real blocked worker with accepted ancestor and readiness evidence."""

        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="repair a blocked implementation without accepting it early",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        ancestor = NodeSpec(
            "ancestor",
            task_id,
            "produce an accepted immutable input",
            "fixture",
            "fixture",
            "fixture ancestor",
            write_scopes=("src",),
        )
        worker = NodeSpec(
            "worker",
            task_id,
            "repair the blocked implementation",
            "fixture",
            "fixture",
            "fixture worker",
            depends_on=("ancestor",),
            write_scopes=("src",),
        )
        downstream = NodeSpec(
            "downstream",
            task_id,
            "run the dependent product step",
            "codex",
            "fixture",
            "fixture downstream",
            depends_on=("worker",),
            write_scopes=("src",),
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "perform final original-task verification",
            "codex",
            "fixture",
            "fixture verifier",
            depends_on=("ancestor", "worker", "downstream"),
            verifier=True,
        )
        self.store.create_task(contract, [ancestor, worker, downstream, verifier], f"create-{task_id}")
        self.store.queue_task(task_id)

        claimed_ancestor = self.store.claim_ready_node("e2e-ancestor", self.epoch)
        assert claimed_ancestor is not None
        ancestor_tree = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            task_id,
            "ancestor",
            int(claimed_ancestor["attempt"]),
        )
        self.store.assign_worktree(
            task_id,
            "ancestor",
            str(ancestor_tree),
            attempt=int(claimed_ancestor["attempt"]),
            coordinator_epoch=int(claimed_ancestor["coordinator_epoch"]),
            lease_epoch=int(claimed_ancestor["lease_epoch"]),
        )
        (ancestor_tree / "src" / "ancestor.txt").write_text("accepted ancestor\n", encoding="utf-8")
        ancestor_patch = self.artifacts.put_bytes(
            self.worktrees.diff_patch(ancestor_tree, contract.base_sha),
            "patch",
        )
        self.store.settle_claimed(
            claimed_ancestor,
            NodeResult(
                "succeeded",
                "accepted ancestor",
                artifacts={"patch": ancestor_patch},
                changed_paths=("src/ancestor.txt",),
                checks=("ancestor fixture",),
            ),
        )

        claimed_worker = self.store.claim_ready_node("e2e-blocked-worker", self.epoch)
        assert claimed_worker is not None
        source = self.worktrees.prepare(
            contract.repository,
            contract.base_sha,
            task_id,
            "worker",
            int(claimed_worker["attempt"]),
        )
        self.store.assign_worktree(
            task_id,
            "worker",
            str(source),
            attempt=int(claimed_worker["attempt"]),
            coordinator_epoch=int(claimed_worker["coordinator_epoch"]),
            lease_epoch=int(claimed_worker["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(task_id),
            "worker",
            source,
            self.artifacts,
            self.worktrees,
        )
        assert dependency_input is not None
        dependency_ref = self.artifacts.put_text(
            json.dumps(dependency_input.receipt, sort_keys=True),
            "dependency-input.json",
        )
        (source / "src" / "value.txt").write_text("unfixed business behavior\n", encoding="utf-8")
        readiness_ref = self._readiness_ref(str(source))
        self.store.settle_claimed(
            claimed_worker,
            NodeResult(
                "blocked",
                "the original business validation rejected the implementation",
                artifacts={
                    "dependency-input": dependency_ref,
                    "execution-readiness": readiness_ref,
                },
                changed_paths=("src/value.txt",),
                checks=("original business validation failed",),
                execution_attribution=self._verification_attribution(
                    task_id,
                    "worker",
                    int(claimed_worker["attempt"]),
                    int(claimed_worker["started_event_cursor"]),
                ),
            ),
        )
        blocked = self.store.get_task(task_id)
        self.assertEqual(blocked["state"], "blocked")
        return contract, source, dependency_ref, blocked

    @staticmethod
    def _node(task: dict, node_id: str) -> dict:
        return next(node for node in task["nodes"] if node["node_id"] == node_id)

    def test_real_collector_reconciler_repairs_then_accepts_original_task(self) -> None:
        contract, source, dependency_ref, blocked = self._blocked_validation_task(task_id="e2e-repair")
        ancestor_before = deepcopy(self._node(blocked, "ancestor"))
        self._configure_repair_policy(contract.task_id, int(blocked["state_revision"]))
        coordinator = self._coordinator()
        assert coordinator.node_recovery is not None
        loop = coordinator.node_recovery

        observed = collect_node_observation(self.store, contract.task_id, "worker")
        self.assertTrue(observed["observation_available"])
        self.assertEqual((observed["category"], observed["origin"]), ("validation_failure", "verification"))
        self.assertTrue(observed["readiness_ready"])
        self.assertEqual(observed["evidence_refs"]["execution-readiness"], self._node(blocked, "worker")["result"]["artifacts"]["execution-readiness"])
        self.assertEqual(observed["accepted_ancestors"], [{
            "node_id": "ancestor", "attempt": 1,
            "patch_ref": ancestor_before["result"]["artifacts"]["patch"],
        }])
        ready = loop._refresh_node(contract.task_id, "worker", 0)
        self.assertEqual((ready["state"], ready["action"], ready["owner"]), ("ready", "repair_source", "authority"))
        self.assertEqual(ready["evidence_refs"]["dependency-input"], dependency_ref)
        self.assertEqual(ready["decision"]["reason_kind"], "blocked_source_repair_authorized")

        result = loop.reconcile_once()
        self.assertEqual(len(result), 1)
        queued_events = [
            event for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.blocked_source_repair_queued"
        ]
        self.assertEqual(len(queued_events), 1)
        episode = NodeRecoveryStore(self.store).get_episode(ready["episode_id"])
        self.assertEqual((episode["state"], episode["action"], episode["owner"]), ("resolved", None, "authority"))
        self.assertEqual(episode["stage_attempts"], {"repair_source": 1})
        self.assertEqual(len(episode["actions"]), 1)
        self.assertEqual((episode["actions"][0]["stage_key"], episode["actions"][0]["state"]), ("repair_source", "completed"))
        self.assertTrue(episode["actions"][0]["effect_dispatched"])

        # A duplicate source event only refreshes the resolved episode; its
        # durable request receipt prevents another source-repair dispatch.
        with self.store.transaction() as connection:
            self.store._event(
                connection,
                "node.blocked",
                contract.task_id,
                "worker",
                {"attempt": 1, "result": self._node(blocked, "worker")["result"]},
            )
        self.assertEqual(loop.reconcile_once(), [])
        self.assertEqual(
            len([
                event for event in self.store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.blocked_source_repair_queued"
            ]),
            1,
        )

        worker_claim = coordinator._claim_next_ready_node("e2e-repaired-worker")
        assert worker_claim is not None
        self.assertEqual((worker_claim["node_id"], worker_claim["attempt"]), ("worker", 2))
        self.assertIn("blocked_source_repair", worker_claim)

        def execute_worker(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            self.assertEqual((worktree / "src" / "ancestor.txt").read_text(encoding="utf-8"), "accepted ancestor\n")
            self.assertEqual((worktree / "src" / "value.txt").read_text(encoding="utf-8"), "unfixed business behavior\n")
            (worktree / "src" / "value.txt").write_text("fixed business behavior\n", encoding="utf-8")
            return NodeResult("succeeded", "fixture repaired the source", checks=("worker fixed source",))

        with patch.object(coordinator, "_executor") as executor:
            executor.return_value.execute.side_effect = execute_worker
            coordinator._execute_claimed(worker_claim)

        after_worker = self.store.get_task(contract.task_id)
        repaired_worker = self._node(after_worker, "worker")
        self.assertEqual((repaired_worker["state"], repaired_worker["attempt"]), ("accepted", 2))
        self.assertIn("patch", repaired_worker["result"]["artifacts"])
        self.assertEqual(self._node(after_worker, "ancestor"), ancestor_before)

        downstream_claim = coordinator._claim_next_ready_node("e2e-downstream")
        assert downstream_claim is not None
        self.assertEqual(downstream_claim["node_id"], "downstream")

        def execute_downstream(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            self.assertEqual(
                (worktree / "src" / "value.txt").read_text(encoding="utf-8"),
                "fixed business behavior\n",
            )
            return NodeResult("succeeded", "dependent fixture observed repaired source", checks=("downstream fixture",))

        with patch.object(coordinator, "_executor") as executor:
            executor.return_value.execute.side_effect = execute_downstream
            coordinator._execute_claimed(downstream_claim)

        verifier_claim = coordinator._claim_next_ready_node("e2e-verifier")
        assert verifier_claim is not None
        self.assertEqual(verifier_claim["node_id"], "verify")

        def execute_verifier(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            self.assertEqual(
                (worktree / "src" / "value.txt").read_text(encoding="utf-8"),
                "fixed business behavior\n",
            )
            test_log = coordinator.artifacts.put_text(
                "final verifier observed the repaired source\n",
                "final-verifier-test.log",
            )
            return NodeResult(
                "succeeded",
                "original verifier accepted the repaired implementation",
                result_kind="verifier",
                verdict="accepted",
                artifacts={"test-log": test_log},
                checks=("final business validation passed",),
                evidence=(test_log,),
            )

        with patch.object(coordinator, "_executor") as executor:
            executor.return_value.execute.side_effect = execute_verifier
            coordinator._execute_claimed(verifier_claim)

        accepted = self.store.get_task(contract.task_id)
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual((self._node(accepted, "verify")["state"], self._node(accepted, "downstream")["state"]), ("accepted", "accepted"))
        self.assertEqual(self._node(accepted, "ancestor"), ancestor_before)
        self.assertEqual((source / "src" / "value.txt").read_text(encoding="utf-8"), "unfixed business behavior\n")

    def test_restart_reconciles_lost_response_read_only_without_second_dispatch(self) -> None:
        contract, _source, _dependency_ref, blocked = self._blocked_validation_task(task_id="e2e-restart")
        self._configure_repair_policy(contract.task_id, int(blocked["state_revision"]))
        coordinator = self._coordinator()
        assert coordinator.node_recovery is not None
        loop = coordinator.node_recovery
        adapter = loop.adapters["repair_source"]
        original_execute = adapter.execute
        apply_calls: list[str] = []

        def lose_response_after_queue(plan: dict) -> dict:
            apply_calls.append(plan["request_id"])
            original_execute(plan)
            raise SystemExit("fixture response lost after source repair queue")

        with patch.object(adapter, "execute", side_effect=lose_response_after_queue):
            with self.assertRaisesRegex(SystemExit, "response lost"):
                loop.reconcile_once()
        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(
            len([
                event for event in self.store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.blocked_source_repair_queued"
            ]),
            1,
        )

        restarted_epoch = self.store.activate_coordinator("e2e-restart", "fixture-machine")
        restarted = self._coordinator(restarted_epoch)
        assert restarted.node_recovery is not None
        restarted.node_recovery.reconcile_once()
        episode = NodeRecoveryStore(self.store).list_summary(task_id=contract.task_id)[0]
        complete = NodeRecoveryStore(self.store).get_episode(episode["episode_id"])
        self.assertEqual((complete["state"], complete["actions"][0]["state"]), ("resolved", "completed"))
        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(
            len([
                event for event in self.store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.blocked_source_repair_queued"
            ]),
            1,
        )

    def test_pause_cancel_and_pending_approval_never_queue_source_repair(self) -> None:
        for control in ("paused", "cancelled", "approval"):
            with self.subTest(control=control):
                contract, _source, _dependency_ref, blocked = self._blocked_validation_task(
                    task_id=f"e2e-control-{control}"
                )
                self._configure_repair_policy(contract.task_id, int(blocked["state_revision"]))
                if control in {"paused", "cancelled"}:
                    self.store.transition_task(
                        contract.task_id,
                        control,
                        expected_revision=int(blocked["state_revision"]),
                    )
                else:
                    with self.store.transaction() as connection:
                        connection.execute(
                            """
                            INSERT INTO approvals(approval_id, task_id, kind, request_json, created_at)
                            VALUES(?, ?, 'fixture_pending', ?, ?)
                            """,
                            (
                                f"approval-{control}",
                                contract.task_id,
                                json.dumps({"node_id": "worker"}, sort_keys=True),
                                "2026-09-11T00:00:00+00:00",
                            ),
                        )
                coordinator = self._coordinator()
                assert coordinator.node_recovery is not None
                coordinator.node_recovery.reconcile_once()
                self.assertEqual(
                    [
                        event for event in self.store.read_events(task_id=contract.task_id)
                        if event["event_type"] == "node.blocked_source_repair_queued"
                    ],
                    [],
                )
                task = self.store.get_task(contract.task_id)
                self.assertEqual(self._node(task, "worker")["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
