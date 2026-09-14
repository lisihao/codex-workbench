"""Regression coverage for blocked-consumer owner preparation continuation."""

from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from codex_workbench import accepted_source_repair
from codex_workbench.accepted_source_repair import AcceptedSourceRepairError
from codex_workbench.model import NodeResult
from codex_workbench.node_recovery_observation import collect_node_observation
from codex_workbench.node_recovery_policy import RecoveryPolicy, plan_recovery
from codex_workbench.service import Coordinator
from tests.process_probe_fixture import isolated_process_catalog


class BlockedOwnerAutoContinuationTests(unittest.TestCase):
    """Keep a blocked consumer and accepted owner evidence across preparation retry."""

    def test_blocked_consumer_owner_preparation_failure_rejoins_the_scheduler(self) -> None:
        # Reuse the existing A/B/C/D/E Git/SQLite fixture; its D attempt is a
        # real blocked consumer with a retained source allocation and result.
        from tests.test_d_integration_scope import DIntegrationScopeTests

        fixture = DIntegrationScopeTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        with isolated_process_catalog(()):
            contract, _arguments, d_source = fixture._create_real_retained_verifier_lane()
            store = fixture.store
            blocked = store.get_task(contract.task_id)
            b_before = next(node for node in blocked["nodes"] if node["node_id"] == "B")
            d_before = next(node for node in blocked["nodes"] if node["node_id"] == "D")
            b_source = Path(str(b_before["worktree"]))
            b_source_patch = (b_source / "src" / "retained-b.ts").read_bytes()
            b_result_before = b_before["result"]
            d_result_before = d_before["result"]
            d_source_before = (d_source / "src" / "retained-d.ts").read_bytes()

            requested = store.schedule_blocked_consumer_owner_repairs(
                contract.task_id,
                "D",
                ["B", "C"],
                {
                    "B": "repair the accepted B owner before D continues",
                    "C": "refresh the accepted C owner after B continues",
                },
                expected_revision=int(blocked["state_revision"]),
                expected_attempt=int(d_before["attempt"]),
                reason="D found defects in its accepted ancestor owners",
            )
            self.assertTrue(requested["blocked_consumer_preserved"])

            coordinator = Coordinator(
                store,
                fixture.state_root,
                coordinator_epoch=fixture.epoch,
                max_workers=1,
                poll_seconds=0.01,
                config=fixture.config,
            )

            first = coordinator._claim_next_ready_node("blocked-owner-B-first")
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual((first["node_id"], first["attempt"]), ("B", 2))
            self.assertIn("accepted_source_repair", first)
            with patch.object(
                accepted_source_repair,
                "prepare_accepted_source_repair",
                side_effect=AcceptedSourceRepairError(
                    "fixture owner preparation fault removed after this attempt"
                ),
            ):
                coordinator._execute_claimed(first)

            after_failure = store.get_task(contract.task_id)
            owner_after_failure = next(
                node for node in after_failure["nodes"] if node["node_id"] == "B"
            )
            consumer_after_failure = next(
                node for node in after_failure["nodes"] if node["node_id"] == "D"
            )
            self.assertEqual(
                (after_failure["state"], owner_after_failure["state"], owner_after_failure["attempt"]),
                ("queued", "pending", 2),
            )
            self.assertEqual(consumer_after_failure["state"], "blocked")
            self.assertEqual(consumer_after_failure["result"], d_result_before)
            self.assertEqual(
                (b_source / "src" / "retained-b.ts").read_bytes(), b_source_patch
            )
            b_source_allocation = next(
                allocation
                for allocation in store.list_worktree_allocations()
                if allocation["task_id"] == contract.task_id
                and allocation["node_id"] == "B"
                and allocation["attempt"] == 1
            )
            self.assertEqual(b_source_allocation["node_result"], b_result_before)
            self.assertEqual(
                (d_source / "src" / "retained-d.ts").read_bytes(), d_source_before
            )

            executed: list[tuple[str, int]] = []
            completed = threading.Event()

            self_test = self

            class RecordingExecutor:
                def execute(self, request: object) -> NodeResult:
                    node_id = str(request.node_id)  # type: ignore[attr-defined]
                    attempt = int(request.attempt)  # type: ignore[attr-defined]
                    executed.append((node_id, attempt))
                    self_test.assertNotEqual(node_id, "D")
                    if node_id == "C":
                        completed.set()
                    return NodeResult("succeeded", f"fixture continued {node_id}")

            loop = threading.Thread(target=coordinator.run_forever, daemon=True)
            with patch.object(coordinator, "_executor", return_value=RecordingExecutor()):
                try:
                    loop.start()
                    self.assertTrue(completed.wait(10), "Coordinator did not continue B/C")
                finally:
                    coordinator.stop()
                    loop.join(10)
                    coordinator._pool.shutdown(wait=True, cancel_futures=True)
                    coordinator._delivery_pool.shutdown(wait=True, cancel_futures=True)
            self.assertFalse(loop.is_alive(), "Coordinator loop did not stop")

            self.assertEqual(executed, [("B", 3), ("C", 2)])
            settled = store.get_task(contract.task_id)
            b_settled = next(node for node in settled["nodes"] if node["node_id"] == "B")
            c_settled = next(node for node in settled["nodes"] if node["node_id"] == "C")
            d_settled = next(node for node in settled["nodes"] if node["node_id"] == "D")
            self.assertEqual((b_settled["state"], b_settled["attempt"]), ("accepted", 3))
            self.assertEqual((c_settled["state"], c_settled["attempt"]), ("accepted", 2))
            self.assertEqual((d_settled["state"], d_settled["result"]), ("blocked", d_result_before))
            owner_wait = store.blocked_owner_repair_wait(contract.task_id, "D", int(d_before["attempt"]))
            self.assertIsNotNone(owner_wait)
            assert owner_wait is not None
            self.assertFalse(owner_wait["pending"])
            self.assertEqual(
                [(item["node_id"], item["current_attempt"]) for item in owner_wait["dependencies"]],
                [("B", 3), ("C", 2)],
            )
            self.assertEqual(
                (d_source / "src" / "retained-d.ts").read_bytes(), d_source_before
            )
            # The owner loop has cleared D's dependency wait; D itself remains
            # blocked and is only ready for the normal subsequent reevaluation.
            d_observation = collect_node_observation(store, contract.task_id, "D")
            self.assertFalse(d_observation["blocked_owner_repairs_pending"])

    def test_exhausted_owner_preparation_has_an_explicit_authority_repair_path(self) -> None:
        from tests.test_d_integration_scope import DIntegrationScopeTests

        fixture = DIntegrationScopeTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        with isolated_process_catalog(()):
            contract, _arguments, _d_source = fixture._create_real_retained_verifier_lane()
            store = fixture.store
            blocked = store.get_task(contract.task_id)
            d_before = next(node for node in blocked["nodes"] if node["node_id"] == "D")
            store.schedule_blocked_consumer_owner_repairs(
                contract.task_id,
                "D",
                ["B", "C"],
                {"B": "repair B", "C": "repair C"},
                expected_revision=int(blocked["state_revision"]),
                expected_attempt=int(d_before["attempt"]),
                reason="exercise bounded accepted-owner preparation failures",
            )
            coordinator = Coordinator(
                store,
                fixture.state_root,
                coordinator_epoch=fixture.epoch,
                max_workers=1,
                config=fixture.config,
            )
            attempts: list[tuple[str, int]] = []
            try:
                with patch.object(
                    accepted_source_repair,
                    "prepare_accepted_source_repair",
                    side_effect=AcceptedSourceRepairError(
                        "persistent accepted-owner preparation failure"
                    ),
                ):
                    for _ in range(12):
                        claim = coordinator._claim_next_ready_node("owner-exhaustion")
                        if claim is None:
                            break
                        attempts.append((str(claim["node_id"]), int(claim["attempt"])))
                        coordinator._execute_claimed(claim)
            finally:
                coordinator._pool.shutdown(wait=True)

            self.assertEqual(
                attempts,
                [
                    ("B", 2), ("B", 3), ("B", 4), ("B", 5),
                    ("C", 2), ("C", 3), ("C", 4), ("C", 5),
                ],
            )
            task = store.get_task(contract.task_id)
            nodes = {node["node_id"]: node for node in task["nodes"]}
            self.assertEqual(task["state"], "blocked")
            self.assertEqual((nodes["B"]["state"], nodes["B"]["attempt"]), ("accepted", 1))
            self.assertEqual((nodes["C"]["state"], nodes["C"]["attempt"]), ("accepted", 1))
            self.assertEqual(nodes["D"]["result"], d_before["result"])
            self.assertIsNone(coordinator._claim_next_ready_node("must-not-spin"))

            owner_wait = store.blocked_owner_repair_wait(
                contract.task_id, "D", int(d_before["attempt"])
            )
            assert owner_wait is not None
            self.assertTrue(owner_wait["pending"])
            self.assertTrue(owner_wait["preparation_exhausted"])
            self.assertTrue(all(
                dependency["preparation_exhausted"]
                for dependency in owner_wait["dependencies"]
            ))
            observed = collect_node_observation(store, contract.task_id, "D")
            self.assertTrue(observed["blocked_owner_repair_preparation_exhausted"])
            observed.update({
                "now": "2026-09-14T16:00:00+00:00",
                "elapsed_seconds": 3_600,
                "action_attempts": 3,
            })
            decision = plan_recovery(
                RecoveryPolicy(
                    enabled=True,
                    allowed_actions=("repair_source",),
                    backoff_seconds=30,
                ),
                observed,
            )
            self.assertEqual(
                (
                    decision["state"], decision["reason_kind"], decision["owner"],
                    decision["requires_authorization"], decision["next_wakeup_at"],
                ),
                (
                    "needs_action",
                    "accepted_owner_repair_repair_action_unconfigured",
                    "authority",
                    False,
                    None,
                ),
            )

            repair_decision = plan_recovery(
                RecoveryPolicy(
                    enabled=True,
                    allowed_actions=("request_repair",),
                    repair_repository=str(fixture.repository),
                    repair_allowed_scopes=("src",),
                ),
                {**observed, "elapsed_seconds": 0, "action_attempts": 0},
            )
            self.assertEqual(
                (
                    repair_decision["state"], repair_decision["action"],
                    repair_decision["reason_kind"], repair_decision["owner"],
                    repair_decision["requires_authorization"],
                ),
                (
                    "ready",
                    "request_repair",
                    "accepted_owner_repair_preparation_exhausted",
                    "authority",
                    False,
                ),
            )

            timed_out = plan_recovery(
                RecoveryPolicy(
                    enabled=True,
                    allowed_actions=("request_repair",),
                    repair_repository=str(fixture.repository),
                    repair_allowed_scopes=("src",),
                    time_budget_seconds=10,
                ),
                {**observed, "elapsed_seconds": 600, "action_attempts": 0},
            )
            self.assertEqual(
                (timed_out["action"], timed_out["reason_kind"], timed_out["owner"]),
                (None, "accepted_owner_repair_repair_budget_exhausted", "authority"),
            )


if __name__ == "__main__":
    unittest.main()
