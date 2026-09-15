from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.accepted_source_repair import (
    AcceptedSourceRepairError,
    build_accepted_repair_bindings,
    parse_accepted_source_repair_binding,
    prepare_accepted_source_repair,
)
from codex_workbench.blocked_source_repair import blocked_source_repair
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.dirty_worktree_recovery import DirtyWorktreeRecoveryError
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager
from tests.process_probe_fixture import isolated_process_catalog


class AcceptedSourceRepairFixture:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "user.name", "Fixture")
        (self.repository / "src" / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(self.repository, "add", "src/base.txt")
        self._git(self.repository, "commit", "-m", "base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.state_root = self.root / "state"
        self.store = WorkbenchStore(self.state_root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("accepted-source-fixture", "fixture")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _create_task(
        self,
        *,
        include_descendant: bool = False,
        retry_limit: int = 3,
    ) -> tuple[TaskContract, dict[str, object]]:
        contract = TaskContract(
            task_id="accepted-source-fixture",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="continue only the accepted owner patch after verifier feedback",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
            retry_limit=retry_limit,
        )
        a = NodeSpec(
            "A",
            contract.task_id,
            "accepted ancestor",
            "fixture",
            "fixture",
            write_scopes=("src/a.txt",),
        )
        b = NodeSpec(
            "B",
            contract.task_id,
            "accepted source owner",
            "fixture",
            "fixture",
            depends_on=(a.node_id,),
            read_scopes=("src/a.txt",),
            write_scopes=("src/b.txt",),
        )
        nodes = [a, b]
        if include_descendant:
            c = NodeSpec(
                "C",
                contract.task_id,
                "accepted descendant",
                "fixture",
                "fixture",
                depends_on=(b.node_id,),
                read_scopes=("src/b.txt",),
                write_scopes=("src/c.txt",),
            )
            nodes.append(c)
        verify = NodeSpec(
            "verify",
            contract.task_id,
            "failed verifier",
            "fixture",
            "fixture",
            depends_on=tuple(node.node_id for node in nodes),
            verifier=True,
        )
        self.store.create_task(contract, [*nodes, verify], "accepted-source-create")
        self.store.queue_task(contract.task_id)
        self._accept_worker(a.node_id, "src/a.txt", "ancestor\n")
        self._accept_worker(b.node_id, "src/b.txt", "owner patch\n")
        if include_descendant:
            self._accept_worker("C", "src/c.txt", "descendant\n")
        claimed_verifier = self.store.claim_ready_node("verifier-worker", self.epoch)
        assert claimed_verifier is not None
        self.assertEqual(claimed_verifier["node_id"], "verify")
        return contract, claimed_verifier

    def _accept_worker(self, node_id: str, changed_path: str, content: str) -> None:
        claimed = self.store.claim_ready_node(f"{node_id}-worker", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], node_id)
        worktree = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            str(claimed["task_id"]),
            node_id,
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            str(claimed["task_id"]),
            node_id,
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(str(claimed["task_id"])),
            node_id,
            worktree,
            self.store.artifacts,
            self.worktrees,
        )
        dependency_ref: str | None = None
        input_tree = self.base_sha
        if dependency_input is not None:
            dependency_ref = self.store.artifacts.put_text(
                canonical_json(dependency_input.receipt), "dependency-input.json"
            )
            input_tree = dependency_input.input_tree_sha
        target = worktree / changed_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        patch = self.store.artifacts.put_bytes(
            self.worktrees.diff_patch(worktree, input_tree), "patch"
        )
        artifacts = {"patch": patch}
        if dependency_ref is not None:
            artifacts["dependency-input"] = dependency_ref
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                f"{node_id} accepted",
                artifacts=artifacts,
                changed_paths=(changed_path,),
                checks=("fixture",),
            ),
        )

    def _bindings(
        self,
        contract: TaskContract,
        verifier: dict[str, object],
        repair_node_ids: tuple[str, ...] = ("B",),
    ) -> dict[str, dict]:
        revision = int(self.store.get_task(contract.task_id)["state_revision"])
        with self.store.connection() as connection:
            return build_accepted_repair_bindings(
                self.store,
                connection,
                contract.task_id,
                repair_node_ids,
                "verify",
                int(verifier["attempt"]),
                revision + 1,
            )

    def _stage_claimed_repair(self, binding: dict) -> None:
        source_attempt = int(binding["source"]["attempt"])
        with self.store.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE nodes
                SET state = 'running', attempt = ?, worker_id = 'repair-worker',
                    worktree = NULL, result_json = NULL, recovery_json = ?,
                    coordinator_epoch = ?, lease_epoch = 7, updated_at = updated_at
                WHERE task_id = ? AND node_id = ? AND state = 'accepted' AND attempt = ?
                """,
                (
                    source_attempt + 1,
                    canonical_json(binding),
                    self.epoch,
                    binding["task_id"],
                    binding["node_id"],
                    source_attempt,
                ),
            ).rowcount
        self.assertEqual(changed, 1)


class AcceptedSourceRepairTests(AcceptedSourceRepairFixture, unittest.TestCase):
    def test_preassignment_failure_restores_the_accepted_source_attempt(self) -> None:
        contract, verifier = self._create_task()
        accepted_before = next(
            node
            for node in self.store.get_task(contract.task_id)["nodes"]
            if node["node_id"] == "B"
        )
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B must update its accepted implementation",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        coordinator = Coordinator(
            self.store, self.state_root, coordinator_epoch=self.epoch
        )
        try:
            claimed = coordinator._claim_next_ready_node("accepted-owner-preparation")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 2))
            with patch(
                "codex_workbench.accepted_source_repair.prepare_accepted_source_repair",
                side_effect=AcceptedSourceRepairError(
                    "fixture accepted-source target preparation failed"
                ),
            ):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        owner = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual((task["state"], owner["state"], owner["attempt"]), (
            "queued", "pending", 2,
        ))
        self.assertIsNone(owner["worktree"])
        self.assertIsNone(owner["result"])
        with self.store.connection() as connection:
            recovery_json = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
        retry_binding = parse_accepted_source_repair_binding(
            recovery_json, next_attempt=3
        )
        assert retry_binding is not None
        self.assertEqual(
            json.loads(retry_binding["source_result_json"]), accepted_before["result"]
        )
        self.assertEqual(self.store.list_approvals(), [])
        rollback = next(
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.accepted_source_repair_rolled_back"
        )
        self.assertEqual(rollback["payload"]["source_attempt"], 1)
        self.assertEqual(
            rollback["payload"]["preparation_result"]["status"], "blocked"
        )
        self.assertTrue(rollback["payload"]["retry_scheduled"])
        self.assertEqual(rollback["payload"]["next_attempt"], 3)

        retry = coordinator._claim_next_ready_node("accepted-owner-retry")
        assert retry is not None
        self.assertEqual((retry["node_id"], retry["attempt"]), ("B", 3))
        with patch.object(coordinator, "_executor") as executor:
            executor.return_value.execute.return_value = NodeResult(
                "succeeded", "accepted owner preparation recovered", checks=("fixture",)
            )
            coordinator._execute_claimed(retry)
        completed = self.store.get_task(contract.task_id)
        completed_owner = next(
            node for node in completed["nodes"] if node["node_id"] == "B"
        )
        self.assertEqual((completed_owner["state"], completed_owner["attempt"]), (
            "accepted", 3,
        ))

    def test_restart_before_accepted_source_assignment_restores_the_source(self) -> None:
        contract, verifier = self._create_task()
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B must update its accepted implementation",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        claimed = self.store.claim_ready_node("accepted-owner-preparation", self.epoch)
        assert claimed is not None
        self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 2))

        recovered, orphans = self.store.recover_interrupted_with_orphans()

        self.assertEqual((recovered, orphans), (1, ()))
        task = self.store.get_task(contract.task_id)
        owner = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual((task["state"], owner["state"], owner["attempt"]), (
            "queued", "pending", 2,
        ))
        self.assertEqual(self.store.list_approvals(), [])
        rollback = next(
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.accepted_source_repair_rolled_back"
        )
        self.assertIn("restarted before", rollback["payload"]["preparation_result"]["summary"])
        retry = self.store.claim_ready_node("accepted-owner-retry", self.epoch)
        assert retry is not None
        self.assertEqual((retry["node_id"], retry["attempt"]), ("B", 3))

    def test_preassignment_retries_are_bounded_by_the_task_contract(self) -> None:
        contract, verifier = self._create_task(retry_limit=1)
        accepted_before = next(
            node
            for node in self.store.get_task(contract.task_id)["nodes"]
            if node["node_id"] == "B"
        )
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B must update its accepted implementation",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        coordinator = Coordinator(
            self.store, self.state_root, coordinator_epoch=self.epoch
        )
        try:
            with patch(
                "codex_workbench.accepted_source_repair.prepare_accepted_source_repair",
                side_effect=AcceptedSourceRepairError("persistent preparation failure"),
            ):
                first = coordinator._claim_next_ready_node("accepted-owner-first")
                assert first is not None
                coordinator._execute_claimed(first)
                second = coordinator._claim_next_ready_node("accepted-owner-second")
                assert second is not None
                coordinator._execute_claimed(second)
        finally:
            coordinator._pool.shutdown(wait=True)

        task = self.store.get_task(contract.task_id)
        owner = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual((task["state"], owner["state"], owner["attempt"]), (
            "needs_fix", "blocked", 3,
        ))
        with self.store.connection() as connection:
            recovery_json = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
        binding = parse_accepted_source_repair_binding(recovery_json)
        assert binding is not None
        self.assertEqual(json.loads(binding["source_result_json"]), accepted_before["result"])
        self.assertIsNone(self.store.claim_ready_node("must-not-loop", self.epoch))
        rollbacks = [
            event
            for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.accepted_source_repair_rolled_back"
        ]
        self.assertEqual(
            [event["payload"]["retry_scheduled"] for event in rollbacks],
            [True, False],
        )

    def test_manual_owner_repair_resumes_an_exhausted_failed_verifier(self) -> None:
        contract, verifier = self._create_task(retry_limit=0)
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B requires one evidenced source repair",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        failed = self.store.get_task(contract.task_id)
        self.assertEqual(failed["state"], "needs_fix")

        receipt = self.store.schedule_blocked_consumer_owner_repairs(
            contract.task_id,
            "verify",
            ["B"],
            {"B": "remove the one verifier-identified source defect"},
            expected_revision=int(failed["state_revision"]),
            expected_attempt=1,
            reason="continue exact owners from the exhausted verifier receipt",
        )

        self.assertEqual(receipt["requester_kind"], "settled_verifier")
        resumed = self.store.get_task(contract.task_id)
        owner = next(node for node in resumed["nodes"] if node["node_id"] == "B")
        requester = next(node for node in resumed["nodes"] if node["node_id"] == "verify")
        self.assertEqual(
            (resumed["state"], owner["state"], requester["state"]),
            ("queued", "pending", "pending"),
        )
        self.assertIsNone(requester["result"])
        with self.store.connection() as connection:
            recovery_json = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
        binding = parse_accepted_source_repair_binding(recovery_json)
        assert binding is not None
        self.assertEqual(binding["requester"]["kind"], "settled_verifier")
        next_claim = self.store.claim_ready_node("manual-owner-repair", self.epoch)
        assert next_claim is not None
        self.assertEqual((next_claim["node_id"], next_claim["attempt"]), ("B", 2))

    def test_manual_failed_verifier_owner_repair_requires_exact_receipt_ids(self) -> None:
        contract, verifier = self._create_task(retry_limit=0)
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B requires repair",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        failed = self.store.get_task(contract.task_id)

        with self.assertRaisesRegex(StateConflictError, "does not match"):
            self.store.schedule_blocked_consumer_owner_repairs(
                contract.task_id,
                "verify",
                ["A"],
                {"A": "unrequested repair"},
                expected_revision=int(failed["state_revision"]),
                expected_attempt=1,
                reason="must not widen verifier-selected owners",
            )

    def test_settled_verifier_repair_refreshes_current_accepted_ancestors(self) -> None:
        contract, verifier = self._create_task(retry_limit=0)
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B requires repair on the current accepted ancestor input",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        failed = self.store.get_task(contract.task_id)
        self.store.schedule_blocked_consumer_owner_repairs(
            contract.task_id,
            "verify",
            ["B"],
            {"B": "repair B on the refreshed accepted ancestor input"},
            expected_revision=int(failed["state_revision"]),
            expected_attempt=1,
            reason="continue the settled verifier repair",
        )
        claimed = self.store.claim_ready_node("settled-verifier-refresh", self.epoch)
        assert claimed is not None
        self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 2))

        with patch(
            "codex_workbench.accepted_source_repair._restore_dependency_input",
            side_effect=AssertionError("settled verifier repair must refresh ancestors"),
        ), patch(
            "codex_workbench.accepted_source_repair.apply_accepted_ancestor_patches",
            wraps=apply_accepted_ancestor_patches,
        ) as refresh:
            prepared = prepare_accepted_source_repair(
                self.store, claimed["accepted_source_repair"], self.worktrees
            )

        refresh.assert_called_once()
        self.assertEqual(
            (prepared.worktree / "src/a.txt").read_text(encoding="utf-8"),
            "ancestor\n",
        )

    def test_operator_can_resume_exhausted_preexecution_accepted_source_repair(self) -> None:
        contract, verifier = self._create_task(retry_limit=0)
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B requires repair",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        failed = self.store.get_task(contract.task_id)
        self.store.schedule_blocked_consumer_owner_repairs(
            contract.task_id,
            "verify",
            ["B"],
            {"B": "repair B after the preparation implementation is corrected"},
            expected_revision=int(failed["state_revision"]),
            expected_attempt=1,
            reason="continue the settled verifier repair",
        )
        coordinator = Coordinator(
            self.store, self.state_root, coordinator_epoch=self.epoch
        )
        try:
            claimed = coordinator._claim_next_ready_node("exhausted-preparation")
            assert claimed is not None
            with patch(
                "codex_workbench.accepted_source_repair.prepare_accepted_source_repair",
                side_effect=AcceptedSourceRepairError(
                    "accepted-source repair fixture preparation defect"
                ),
            ):
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        stopped = self.store.get_task(contract.task_id)
        owner = next(node for node in stopped["nodes"] if node["node_id"] == "B")
        self.assertEqual(
            (stopped["state"], owner["state"], owner["attempt"], owner["worktree"]),
            ("needs_fix", "blocked", 2, None),
        )
        receipt = self.store.resume_exhausted_accepted_source_repair(
            contract.task_id,
            "B",
            expected_revision=int(stopped["state_revision"]),
            expected_attempt=2,
            reason="the accepted-source preparation implementation is now corrected",
        )

        self.assertEqual((receipt["state"], receipt["next_attempt"]), ("queued", 3))
        resumed = self.store.get_task(contract.task_id)
        owner = next(node for node in resumed["nodes"] if node["node_id"] == "B")
        self.assertEqual((resumed["state"], owner["state"], owner["attempt"]), (
            "queued", "pending", 2,
        ))
        with self.store.connection() as connection:
            recovery_json = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
        self.assertIsNotNone(recovery_json)
        next_claim = self.store.claim_ready_node("resumed-preparation", self.epoch)
        assert next_claim is not None
        self.assertEqual((next_claim["node_id"], next_claim["attempt"]), ("B", 3))

    def test_preparation_block_preserves_staged_patch_and_attempt_guidance(self) -> None:
        self.enterContext(isolated_process_catalog(()))
        contract, verifier = self._create_task(retry_limit=1)
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B must update its accepted implementation",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        guidance = "keep the accepted B patch and apply the current owner feedback"
        with self.store.transaction() as connection:
            sequence = self.store._next_steering_sequence(
                connection, contract.task_id
            )
            connection.execute(
                """
                INSERT INTO task_steering(
                    steering_id, task_id, instruction, created_at, sequence,
                    scope, target_node_id, target_attempt
                ) VALUES(
                    'fixture-owner-guidance', ?, ?, '2026-09-14T00:00:00+00:00',
                    ?, 'attempt', 'B', 2
                )
                """,
                (contract.task_id, guidance, sequence),
            )
            self.store._event(
                connection,
                "node.accepted_source_repair_authorized",
                contract.task_id,
                "B",
                {
                    "source_attempt": 1,
                    "requester_kind": "blocked_consumer",
                    "requester_node_id": "verify",
                    "requester_attempt": 1,
                    "authorization_revision": int(
                        self.store.get_task(contract.task_id)["state_revision"]
                    ),
                    "steering_id": "fixture-owner-guidance",
                },
            )

        coordinator = Coordinator(
            self.store, self.state_root, coordinator_epoch=self.epoch
        )
        try:
            claimed = coordinator._claim_next_ready_node("accepted-owner-preparation")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 2))
            self.assertIn(guidance, claimed["steering"])
            with patch.object(
                coordinator,
                "_materialize_worktree_dependencies",
                side_effect=DirtyWorktreeRecoveryError(
                    "fixture dependency materialization blocked"
                ),
            ):
                coordinator._execute_claimed(claimed)

            blocked = self.store.get_task(contract.task_id)
            owner = next(node for node in blocked["nodes"] if node["node_id"] == "B")
            self.assertEqual((blocked["state"], owner["state"], owner["attempt"]), ("blocked", "blocked", 2))
            self.assertEqual(owner["result"]["changed_paths"], [])
            source = Path(str(owner["worktree"]))
            self.assertIn(
                "src/b.txt",
                self._git(source, "diff", "--cached", "--name-only").splitlines(),
            )
            with self.assertRaisesRegex(
                StateConflictError,
                "blocked result with non-empty changed_paths",
            ):
                self.store.blocked_worktree_recovery_candidate(
                    contract.task_id,
                    "B",
                    expected_revision=int(blocked["state_revision"]),
                    expected_attempt=2,
                )

            NodeRecoveryStore(self.store).configure_policy(
                contract.task_id,
                RecoveryPolicy(
                    enabled=True,
                    allowed_actions=("repair_source",),
                    max_action_attempts=3,
                ),
                expected_task_revision=int(blocked["state_revision"]),
                actor="accepted-source-preparation-fixture",
            )
            arguments = {
                "task_id": contract.task_id,
                "node_id": "B",
                "expected_revision": int(blocked["state_revision"]),
                "expected_attempt": 2,
                "expected_contract_hash": blocked["contract_hash"],
                "request_id": "accepted-source-preparation-recovery",
                "reason": "preserve the prepared accepted patch after dependency setup blocked",
            }
            preview = blocked_source_repair(
                self.store, **arguments, dry_run=True
            )
            self.assertEqual(preview["changed_paths"], ["src/b.txt"])
            self.assertEqual(preview["continued_steering_count"], 1)
            queued = blocked_source_repair(
                self.store,
                **arguments,
                dry_run=False,
                expected_fingerprint=preview["fingerprint"],
            )
            self.assertEqual(len(queued["continued_steering"]), 1)

            retry = coordinator._claim_next_ready_node("accepted-owner-retry")
            assert retry is not None
            self.assertEqual((retry["node_id"], retry["attempt"]), ("B", 3))
            self.assertIn(guidance, retry["steering"])
            observed: dict[str, str] = {}

            def block_after_recovery(request: object) -> NodeResult:
                worktree = request.worktree  # type: ignore[attr-defined]
                assert worktree is not None
                observed["owner_patch"] = (worktree / "src/b.txt").read_text(
                    encoding="utf-8"
                )
                return NodeResult("blocked", "fixture blocks after recovery")

            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = block_after_recovery
                coordinator._execute_claimed(retry)
            self.assertEqual(observed["owner_patch"], "owner patch\n")

            chained = self.store.get_task(contract.task_id)
            chained_arguments = {
                **arguments,
                "expected_revision": int(chained["state_revision"]),
                "expected_attempt": 3,
                "request_id": "accepted-source-preparation-recovery-chain",
            }
            chained_preview = blocked_source_repair(
                self.store, **chained_arguments, dry_run=True
            )
            self.assertEqual(chained_preview["continued_steering_count"], 1)
            chained_queue = blocked_source_repair(
                self.store,
                **chained_arguments,
                dry_run=False,
                expected_fingerprint=chained_preview["fingerprint"],
            )
            self.assertEqual(len(chained_queue["continued_steering"]), 1)
            with self.store.connection() as connection:
                steering_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_steering WHERE task_id = ?",
                        (contract.task_id,),
                    ).fetchone()[0]
                )
            replay = blocked_source_repair(
                self.store,
                **chained_arguments,
                dry_run=False,
                expected_fingerprint=chained_preview["fingerprint"],
            )
            self.assertEqual(replay, chained_queue)
            with self.store.connection() as connection:
                self.assertEqual(
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM task_steering WHERE task_id = ?",
                            (contract.task_id,),
                        ).fetchone()[0]
                    ),
                    steering_count,
                )
        finally:
            coordinator._pool.shutdown(wait=True)

    def test_verifier_failure_without_owner_ids_preserves_accepted_workers(self) -> None:
        contract, verifier = self._create_task()
        before = self.store.get_task(contract.task_id)
        accepted_before = {
            node["node_id"]: node["result"]
            for node in before["nodes"]
            if node["node_id"] in {"A", "B"}
        }

        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "verifier needs an explicit source owner",
                verdict="needs_fix",
                checks=("fixture verifier",),
            ),
        )

        task = self.store.get_task(contract.task_id)
        nodes = {node["node_id"]: node for node in task["nodes"]}
        self.assertEqual(task["state"], "needs_fix")
        self.assertEqual((nodes["verify"]["state"], nodes["verify"]["attempt"]), ("failed", 1))
        for node_id in ("A", "B"):
            self.assertEqual((nodes[node_id]["state"], nodes[node_id]["attempt"]), ("accepted", 1))
            self.assertEqual(nodes[node_id]["result"], accepted_before[node_id])
        self.assertIsNone(self.store.claim_ready_node("must-not-reset-workers", self.epoch))
        events = self.store.read_events(task_id=contract.task_id)
        event_types = {event["event_type"] for event in events}
        self.assertIn("task.repair_owner_required", event_types)
        self.assertNotIn("task.repair_scheduled", event_types)

    def test_verifier_repair_claim_continues_only_the_selected_accepted_owner(self) -> None:
        contract, verifier = self._create_task()
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B must update its accepted implementation",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        after_verifier = self.store.get_task(contract.task_id)
        a_after_verifier = next(
            node for node in after_verifier["nodes"] if node["node_id"] == "A"
        )
        b_after_verifier = next(
            node for node in after_verifier["nodes"] if node["node_id"] == "B"
        )
        self.assertEqual((after_verifier["state"], a_after_verifier["state"]), ("queued", "accepted"))
        self.assertEqual((b_after_verifier["state"], b_after_verifier["attempt"]), ("pending", 1))
        with self.store.connection() as connection:
            stored = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
            feedback = connection.execute(
                """
                SELECT instruction, scope, target_node_id, target_attempt
                FROM task_steering WHERE task_id = ? ORDER BY sequence
                """,
                (contract.task_id,),
            ).fetchall()
        self.assertIsNotNone(parse_accepted_source_repair_binding(stored, next_attempt=2))
        self.assertEqual(
            [
                (
                    row["instruction"], row["scope"],
                    row["target_node_id"], row["target_attempt"],
                )
                for row in feedback
            ],
            [("Verifier rejected attempt 1: B must update its accepted implementation", "node", "B", 2)],
        )

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        observed: dict[str, object] = {}

        def execute(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            observed["node_id"] = request.node_id  # type: ignore[attr-defined]
            observed["attempt"] = request.attempt  # type: ignore[attr-defined]
            observed["ancestor"] = (worktree / "src/a.txt").read_text(encoding="utf-8")
            observed["owner_patch"] = (worktree / "src/b.txt").read_text(encoding="utf-8")
            return NodeResult("succeeded", "accepted owner continued", checks=("fixture",))

        try:
            claimed = coordinator._claim_next_ready_node("accepted-owner-repair")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 2))
            self.assertEqual(
                claimed["steering"],
                ("Verifier rejected attempt 1: B must update its accepted implementation",),
            )
            self.assertIn("accepted_source_repair", claimed)
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "node_id": "B",
                "attempt": 2,
                "ancestor": "ancestor\n",
                "owner_patch": "owner patch\n",
            },
        )
        completed = self.store.get_task(contract.task_id)
        a_completed = next(node for node in completed["nodes"] if node["node_id"] == "A")
        b_completed = next(node for node in completed["nodes"] if node["node_id"] == "B")
        self.assertEqual((a_completed["state"], a_completed["attempt"]), ("accepted", 1))
        self.assertEqual((b_completed["state"], b_completed["attempt"]), ("accepted", 2))
        source = self.worktrees.worktree_path(contract.task_id, "B", 1)
        self.assertEqual((source / "src/b.txt").read_text(encoding="utf-8"), "owner patch\n")
        allocations = self.store.list_worktree_allocations()
        self.assertEqual(
            next(item for item in allocations if item["node_id"] == "B" and item["attempt"] == 1)["state"],
            "superseded",
        )

    def test_prepares_only_accepted_owner_on_recorded_ancestor_input(self) -> None:
        contract, verifier = self._create_task()
        binding = self._bindings(contract, verifier)["B"]
        before = self.store.get_task(contract.task_id)
        a_before = next(node for node in before["nodes"] if node["node_id"] == "A")
        self.assertEqual((a_before["state"], a_before["attempt"]), ("accepted", 1))

        self._stage_claimed_repair(binding)
        prepared = prepare_accepted_source_repair(self.store, binding, self.worktrees)

        self.assertEqual(prepared.worktree, self.worktrees.worktree_path(contract.task_id, "B", 2))
        self.assertEqual((prepared.worktree / "src/a.txt").read_text(encoding="utf-8"), "ancestor\n")
        self.assertEqual((prepared.worktree / "src/b.txt").read_text(encoding="utf-8"), "owner patch\n")
        self.assertEqual(
            prepared.dependency_input.receipt["ancestors"],
            [{
                "node_id": "A",
                "attempt": 1,
                "patch_ref": next(
                    node for node in before["nodes"] if node["node_id"] == "A"
                )["result"]["artifacts"]["patch"],
            }],
        )
        self.assertEqual(
            prepared.receipt,
            {
                "schema_version": 2,
                "kind": "accepted-source-repair-v2",
                "state": "prepared",
                "authorization_revision": int(before["state_revision"]) + 1,
                "source_allocation_id": binding["source_allocation_id"],
                "source_attempt": 1,
                "source_status": "succeeded",
                "target_attempt": 2,
                "target_worktree": str(prepared.worktree),
                "target_branch": self.worktrees.branch_name(contract.task_id, "B", 2),
                "dependency_input_ref": binding["source"]["dependency_input_ref"],
                "dependency_input_tree_sha": prepared.dependency_input.input_tree_sha,
                "patch_ref": binding["source"]["patch_ref"],
                "patch_sha256": binding["source"]["patch_sha256"],
                "binding_sha256": sha256(canonical_json(binding).encode()).hexdigest(),
            },
        )
        after = self.store.get_task(contract.task_id)
        a_after = next(node for node in after["nodes"] if node["node_id"] == "A")
        self.assertEqual((a_after["state"], a_after["attempt"]), ("accepted", 1))
        self.assertEqual(a_after["result"], a_before["result"])

    def test_parser_rejects_forged_failed_source_status(self) -> None:
        contract, verifier = self._create_task()
        binding = self._bindings(contract, verifier)["B"]
        forged = json.loads(canonical_json(binding))
        forged["source_status"] = "failed"

        with self.assertRaisesRegex(AcceptedSourceRepairError, "source_status"):
            parse_accepted_source_repair_binding(forged)

        forged = json.loads(canonical_json(binding))
        source_result = json.loads(forged["source_result_json"])
        source_result["status"] = "failed"
        forged["source_result_json"] = canonical_json(source_result)
        with self.assertRaisesRegex(AcceptedSourceRepairError, "not succeeded"):
            parse_accepted_source_repair_binding(forged)

    def test_prepare_rejects_a_validly_hashed_non_patch_artifact(self) -> None:
        contract, verifier = self._create_task()
        binding = json.loads(canonical_json(self._bindings(contract, verifier)["B"]))
        invalid_patch = self.store.artifacts.put_bytes(b"not a git patch\n", "patch")
        source_result = json.loads(binding["source_result_json"])
        source_result["artifacts"]["patch"] = invalid_patch
        binding["source_result_json"] = canonical_json(source_result)
        binding["source"]["patch_ref"] = invalid_patch
        binding["source"]["patch_sha256"] = sha256(b"not a git patch\n").hexdigest()
        self._stage_claimed_repair(binding)

        with self.assertRaisesRegex(AcceptedSourceRepairError, "preparation failed"):
            prepare_accepted_source_repair(self.store, binding, self.worktrees)

    def test_build_rejects_bad_patch_lineage_base_and_allocation(self) -> None:
        contract, verifier = self._create_task()
        with self.store.transaction() as connection:
            result_row = connection.execute(
                "SELECT result_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()
            original_result = str(result_row["result_json"])
            result = json.loads(original_result)
            result["artifacts"]["patch"] = "sha256:" + "0" * 64 + ":patch"
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (canonical_json(result), contract.task_id),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "patch is unavailable or invalid"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (original_result, contract.task_id),
            )

        bad_receipt = {
            "schema_version": 1,
            "kind": "accepted-ancestor-patch-input",
            "task_id": "other-task",
            "node_id": "B",
            "contract_base_sha": self.base_sha,
            "input_tree_sha": self.base_sha,
            "ancestors": [],
        }
        bad_ref = self.store.artifacts.put_text(
            canonical_json(bad_receipt), "dependency-input.json"
        )
        with self.store.transaction() as connection:
            result = json.loads(original_result)
            result["artifacts"]["dependency-input"] = bad_ref
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (canonical_json(result), contract.task_id),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "dependency input is invalid"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (original_result, contract.task_id),
            )

        with self.store.transaction() as connection:
            allocation = connection.execute(
                """
                SELECT base_sha, state FROM worktree_allocations
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (contract.task_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE worktree_allocations SET base_sha = 'different-base'
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (contract.task_id,),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "allocation does not match"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET base_sha = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (allocation["base_sha"], contract.task_id),
            )

        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET state = 'superseded'
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (contract.task_id,),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "allocation does not match"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET state = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (allocation["state"], contract.task_id),
            )

    def test_build_rejects_bad_owner_duplicate_verifier_and_nonaccepted_source(self) -> None:
        contract, verifier = self._create_task()
        for repair_ids, message in (
            (("missing",), "owner missing is missing"),
            (("B", "B"), "repair_node_ids are duplicated"),
            (("verify",), "cannot select its requester"),
        ):
            with self.subTest(repair_ids=repair_ids), self.assertRaisesRegex(
                AcceptedSourceRepairError, message
            ):
                self._bindings(contract, verifier, repair_ids)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'failed' WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "expected accepted"):
            self._bindings(contract, verifier)

    def test_build_rejects_external_or_destructive_contract(self) -> None:
        contract, verifier = self._create_task()
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT contract_json FROM tasks WHERE task_id = ?", (contract.task_id,)
            ).fetchone()
            original = str(row["contract_json"])
        for field in ("external_write_permission", "destructive_action_permission"):
            with self.subTest(field=field):
                with self.store.transaction() as connection:
                    current = json.loads(original)
                    current[field] = True
                    connection.execute(
                        "UPDATE tasks SET contract_json = ? WHERE task_id = ?",
                        (canonical_json(current), contract.task_id),
                    )
                with self.assertRaisesRegex(AcceptedSourceRepairError, "external or destructive"):
                    self._bindings(contract, verifier)
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET contract_json = ? WHERE task_id = ?",
                        (original, contract.task_id),
                    )

    def test_build_rejects_unselected_accepted_descendant(self) -> None:
        contract, verifier = self._create_task(include_descendant=True)

        with self.assertRaisesRegex(AcceptedSourceRepairError, "accepted descendant C"):
            self._bindings(contract, verifier, ("B",))

    def test_build_allows_intermediate_accepted_descendant_below_repair_frontier(self) -> None:
        contract, verifier = self._create_task(include_descendant=True)

        bindings = self._bindings(contract, verifier, ("A", "C"))

        self.assertEqual(set(bindings), {"A", "C"})
        self.assertEqual(bindings["A"]["repair_node_ids"], ["A", "C"])
        self.assertEqual(bindings["C"]["repair_node_ids"], ["A", "C"])
