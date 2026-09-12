"""Focused Git/SQLite coverage for policy-authorized blocked source repair."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_workbench.blocked_source_repair import (
    blocked_source_repair,
    blocked_source_repair_receipt,
)
from codex_workbench.dirty_worktree_recovery import DirtyWorktreeRecoveryError
from codex_workbench.model import NodeResult
from codex_workbench.node_recovery_policy import RecoveryPolicy
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError
from tests.process_probe_fixture import isolated_process_catalog
from tests.test_failed_attempt_recovery import _FailedAttemptRecoveryFixture


class BlockedSourceRepairTests(_FailedAttemptRecoveryFixture, unittest.TestCase):
    """The new binding reuses failed-attempt capture without bypassing verification."""

    def setUp(self) -> None:
        super().setUp()
        # The workers below are in-process fixture callbacks, never native
        # executors. Keep source-only process inspection scoped to this test.
        self.enterContext(isolated_process_catalog(()))
        self.recovery = NodeRecoveryStore(self.store)

    def _blocked_source(self, *, task_id: str = "blocked-source-repair") -> tuple[object, Path, dict]:
        contract, source = self._dirty_failed_task(
            task_id=task_id,
            result_status="blocked",
        )
        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "blocked")
        return contract, source, task

    def _configure(self, task_id: str, revision: int, *, enabled: bool = True, actions: tuple[str, ...] = ("repair_source",), max_attempts: int = 3) -> None:
        self.recovery.configure_policy(
            task_id,
            RecoveryPolicy(
                enabled=enabled,
                allowed_actions=actions,
                max_action_attempts=max_attempts,
            ),
            expected_task_revision=revision,
            actor="blocked-source-repair-fixture",
        )

    def _arguments(self, contract: object, task: dict, *, request_id: str = "repair-source-request", reason: str = "repair the source after its validation failure") -> dict:
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        return {
            "task_id": contract.task_id,  # type: ignore[attr-defined]
            "node_id": "worker",
            "expected_revision": int(task["state_revision"]),
            "expected_attempt": int(worker["attempt"]),
            "expected_contract_hash": task["contract_hash"],
            "request_id": request_id,
            "reason": reason,
        }

    @staticmethod
    def _raw_result(store: object, task_id: str) -> str:
        with store.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                "SELECT result_json FROM nodes WHERE task_id = ? AND node_id = 'worker'",
                (task_id,),
            ).fetchone()
        assert row is not None
        return str(row["result_json"])

    def _preview_and_queue(self, contract: object, task: dict, *, request_id: str = "repair-source-request") -> tuple[dict, dict]:
        arguments = self._arguments(contract, task, request_id=request_id)
        preview = blocked_source_repair(self.store, **arguments, dry_run=True)
        queued = blocked_source_repair(
            self.store,
            **arguments,
            dry_run=False,
            expected_fingerprint=preview["fingerprint"],
        )
        return preview, queued

    def test_repair_reaches_the_normal_worker_before_final_verification(self) -> None:
        contract, source, blocked = self._blocked_source()
        self._configure(contract.task_id, int(blocked["state_revision"]))
        original_result_json = self._raw_result(self.store, contract.task_id)
        ignored = source / ".workbench-ignored" / "retain.bin"
        ignored.parent.mkdir()
        ignored.write_bytes(b"source-only retention\0")
        before_artifacts = tuple(sorted(path.name for path in self.artifacts.root.glob("**/*") if path.is_file()))

        preview, queued = self._preview_and_queue(contract, blocked)
        self.assertTrue(preview["dry_run"])
        self.assertTrue(queued["queued"])
        self.assertEqual(preview["request_fingerprint"], queued["request_fingerprint"])
        self.assertEqual(preview["fingerprint"], queued["fingerprint"])
        self.assertEqual(
            before_artifacts,
            tuple(sorted(path.name for path in self.artifacts.root.glob("**/*") if path.is_file())),
        )
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'worker'",
                (contract.task_id,),
            ).fetchone()
        assert row is not None
        binding = json.loads(str(row["recovery_json"]))
        self.assertEqual(binding["mode"], "blocked_source_repair")
        self.assertEqual(binding["source_result_json"], original_result_json)
        self.assertEqual(binding["source"]["changed_paths"], preview["changed_paths"])

        source_allocation = next(
            item
            for item in self.store.list_worktree_allocations()
            if item["task_id"] == contract.task_id and item["node_id"] == "worker" and item["attempt"] == 1
        )
        with self.assertRaisesRegex(StateConflictError, "retains this source worktree"):
            self.store.begin_worktree_quarantine(
                source_allocation["allocation_id"],
                str(self.state_root / "quarantine" / source_allocation["allocation_id"]),
            )

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        observed: dict[str, object] = {}
        try:
            claimed = coordinator._claim_next_ready_node("blocked-source-worker")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("worker", 2))
            self.assertEqual(claimed["blocked_source_repair"]["source_attempt"], 1)

            def worker_executor(request: object) -> NodeResult:
                worktree = request.worktree  # type: ignore[attr-defined]
                assert worktree is not None
                observed["worker_called"] = True
                observed["value"] = (worktree / "src" / "value.txt").read_text(encoding="utf-8")
                observed["continuation"] = (worktree / "src" / "continuation.txt").read_text(encoding="utf-8")
                # The original business check would still reject this value;
                # this proves no full acceptance command ran before the worker.
                self.assertNotEqual(observed["value"], "fixed business behavior\n")
                return NodeResult("succeeded", "worker reached normal executor", checks=("fixture worker",))

            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = worker_executor
                coordinator._execute_claimed(claimed)

            verifier = coordinator._claim_next_ready_node("blocked-source-verifier")
            assert verifier is not None
            self.assertEqual(verifier["node_id"], "verify")
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.return_value = NodeResult(
                    "failed",
                    "original verifier rejected the unfixed business behavior",
                    result_kind="verifier",
                    verdict="needs_fix",
                    checks=("business check failed",),
                )
                coordinator._execute_claimed(verifier)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "worker_called": True,
                "value": "dirty prior attempt\n",
                "continuation": "untracked prior attempt\n",
            },
        )
        task = self.store.get_task(contract.task_id)
        ancestor = next(node for node in task["nodes"] if node["node_id"] == "ancestor")
        worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
        verifier_node = next(node for node in task["nodes"] if node["node_id"] == "verify")
        self.assertEqual((task["state"], ancestor["state"], worker["state"], verifier_node["state"]),
                         ("needs_fix", "accepted", "accepted", "failed"))
        self.assertEqual((source / "src" / "ancestor.txt").read_text(encoding="utf-8"), "accepted ancestor\n")
        self.assertEqual((source / "src" / "value.txt").read_text(encoding="utf-8"), "dirty prior attempt\n")
        self.assertEqual(ignored.read_bytes(), b"source-only retention\0")
        with self.store.connection() as connection:
            source_row = connection.execute(
                """
                SELECT node_result_json FROM worktree_allocations
                WHERE task_id = ? AND node_id = 'worker' AND attempt = 1
                """,
                (contract.task_id,),
            ).fetchone()
        assert source_row is not None
        self.assertEqual(source_row["node_result_json"], original_result_json)

    def test_preparation_failure_and_restart_restore_the_original_block(self) -> None:
        for scenario in ("prepare", "restart"):
            with self.subTest(scenario=scenario):
                contract, source, blocked = self._blocked_source(task_id=f"blocked-source-{scenario}")
                self._configure(contract.task_id, int(blocked["state_revision"]))
                original_result_json = self._raw_result(self.store, contract.task_id)
                _, queued = self._preview_and_queue(contract, blocked, request_id=f"{scenario}-request")
                self.assertTrue(queued["queued"])
                coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
                try:
                    claimed = coordinator._claim_next_ready_node(f"{scenario}-worker")
                    assert claimed is not None
                    if scenario == "prepare":
                        with (
                            patch.object(
                                coordinator.failed_attempt_recovery,
                                "prepare_for_retry",
                                side_effect=DirtyWorktreeRecoveryError("fixture preparation failed"),
                            ),
                            patch.object(coordinator, "_executor") as executor,
                        ):
                            coordinator._execute_claimed(claimed)
                            executor.assert_not_called()
                    else:
                        self.store.recover_interrupted_with_orphans()
                finally:
                    coordinator._pool.shutdown(wait=True)

                task = self.store.get_task(contract.task_id)
                worker = next(node for node in task["nodes"] if node["node_id"] == "worker")
                self.assertEqual((task["state"], worker["state"], worker["attempt"], worker["worktree"]),
                                 ("blocked", "blocked", 1, str(source)))
                self.assertEqual(self._raw_result(self.store, contract.task_id), original_result_json)
                rollback = [
                    event for event in self.store.read_events(task_id=contract.task_id)
                    if event["event_type"] == "node.blocked_worktree_recovery_rolled_back"
                ]
                self.assertEqual(len(rollback), 1)
                self.assertEqual(rollback[0]["payload"]["mode"], "blocked_source_repair")
                self.assertEqual(
                    rollback[0]["payload"]["recovery"]["source_worktree"],
                    str(source),
                )

    def test_policy_fingerprint_budget_and_idempotency_fences(self) -> None:
        contract, _, blocked = self._blocked_source(task_id="blocked-source-controls")
        arguments = self._arguments(contract, blocked, request_id="controls-request")
        with self.assertRaisesRegex(StateConflictError, "policy"):
            blocked_source_repair(self.store, **arguments, dry_run=True)
        self._configure(contract.task_id, int(blocked["state_revision"]), actions=("resume_node",))
        with self.assertRaisesRegex(StateConflictError, "authorized"):
            blocked_source_repair(self.store, **arguments, dry_run=True)
        self._configure(
            contract.task_id,
            int(blocked["state_revision"]),
            actions=("repair_source",),
            max_attempts=1,
        )
        with self.assertRaisesRegex(StateConflictError, "expected task revision"):
            blocked_source_repair(
                self.store,
                **{
                    **arguments,
                    "expected_revision": arguments["expected_revision"] + 1,
                    "request_id": "stale-revision-request",
                },
                dry_run=True,
            )
        with self.assertRaisesRegex(StateConflictError, "contract hash"):
            blocked_source_repair(
                self.store,
                **{**arguments, "expected_contract_hash": "0" * 64},
                dry_run=True,
            )
        preview = blocked_source_repair(self.store, **arguments, dry_run=True)
        with self.assertRaisesRegex(StateConflictError, "fingerprint"):
            blocked_source_repair(
                self.store,
                **arguments,
                dry_run=False,
                expected_fingerprint="0" * 64,
            )
        queued = blocked_source_repair(
            self.store,
            **arguments,
            dry_run=False,
            expected_fingerprint=preview["fingerprint"],
        )
        # The original expected revision is stale now, but the same exact
        # idempotency input must return the durable receipt before stale-state
        # checking. A changed payload under the same ID must fail closed.
        self.assertEqual(
            blocked_source_repair(
                self.store,
                **arguments,
                dry_run=False,
                expected_fingerprint=preview["fingerprint"],
            ),
            queued,
        )
        with self.assertRaisesRegex(StateConflictError, "different input"):
            blocked_source_repair(
                self.store,
                **{**arguments, "reason": "changed reason"},
                dry_run=False,
                expected_fingerprint=preview["fingerprint"],
            )
        self.assertEqual(blocked_source_repair_receipt(self.store, arguments["request_id"]), queued)

        # Restore the source block without changing policy revision, then the
        # policy's single allowed action has already been consumed.
        claimed = self.store.claim_ready_node("controls-worker", self.epoch)
        assert claimed is not None
        self.store.recover_interrupted_with_orphans()
        current = self.store.get_task(contract.task_id)
        next_arguments = self._arguments(contract, current, request_id="budget-request")
        queued_events = [
            event for event in self.store.read_events(task_id=contract.task_id)
            if event["event_type"] == "node.blocked_source_repair_queued"
        ]
        self.assertEqual(len(queued_events), 1)
        self.assertEqual(queued_events[0]["payload"]["policy_revision"], 2)
        policy_row = self.recovery.get_policy(contract.task_id)
        self.assertEqual((policy_row["policy_revision"], policy_row["policy"]["max_action_attempts"]), (2, 1))
        with self.store.connection() as connection:
            prior = connection.execute(
                """
                SELECT COUNT(*) FROM events
                WHERE event_type = ? AND task_id = ? AND node_id = ?
                  AND json_extract(payload_json, '$.policy_revision') = ?
                """,
                ("node.blocked_source_repair_queued", contract.task_id, "worker", 2),
            ).fetchone()[0]
        self.assertEqual(prior, 1)
        budget_preview = blocked_source_repair(self.store, **next_arguments, dry_run=True)
        with self.assertRaisesRegex(StateConflictError, "budget"):
            blocked_source_repair(
                self.store,
                **next_arguments,
                dry_run=False,
                expected_fingerprint=budget_preview["fingerprint"],
            )

        for control_state in ("paused", "cancelled"):
            with self.subTest(control_state=control_state):
                controlled_contract, _, controlled_blocked = self._blocked_source(
                    task_id=f"blocked-source-{control_state}"
                )
                self._configure(
                    controlled_contract.task_id,
                    int(controlled_blocked["state_revision"]),
                )
                self.store.transition_task(
                    controlled_contract.task_id,
                    control_state,
                    expected_revision=int(controlled_blocked["state_revision"]),
                )
                controlled_current = self.store.get_task(controlled_contract.task_id)
                with self.assertRaisesRegex(StateConflictError, "expected blocked|blocked"):
                    blocked_source_repair(
                        self.store,
                        **self._arguments(
                            controlled_contract,
                            controlled_current,
                            request_id=f"{control_state}-request",
                        ),
                        dry_run=True,
                    )

    def test_receipt_lookup_uses_the_partial_request_index(self) -> None:
        contract, _, blocked = self._blocked_source(task_id="blocked-source-index")
        self._configure(contract.task_id, int(blocked["state_revision"]))
        for index in range(160):
            self.store.record_system_event("fixture.unrelated", {"index": index})
        preview, _ = self._preview_and_queue(contract, blocked, request_id="indexed-request")
        self.assertTrue(preview["dry_run"])
        with self.store.connection() as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT cursor, payload_json FROM events
                WHERE event_type = ? AND json_extract(payload_json, '$.request_id') = ?
                ORDER BY cursor LIMIT 2
                """,
                ("node.blocked_source_repair_queued", "indexed-request"),
            ).fetchall()
        self.assertTrue(
            any("events_blocked_source_repair_request_id_idx" in str(row[3]) for row in plan),
            plan,
        )

    def test_undeclared_untracked_source_path_is_not_preserved(self) -> None:
        contract, source, blocked = self._blocked_source(task_id="blocked-source-undeclared")
        self._configure(contract.task_id, int(blocked["state_revision"]))
        (source / "outside.txt").write_text("must not be copied\n", encoding="utf-8")
        with self.assertRaisesRegex(StateConflictError, "outside task scope"):
            blocked_source_repair(
                self.store,
                **self._arguments(contract, blocked, request_id="undeclared-request"),
                dry_run=True,
            )


if __name__ == "__main__":
    unittest.main()
