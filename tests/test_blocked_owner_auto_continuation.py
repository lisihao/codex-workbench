"""Regression coverage for blocked-consumer owner preparation continuation."""

from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from codex_workbench import accepted_source_repair
from codex_workbench.accepted_source_repair import AcceptedSourceRepairError
from codex_workbench.model import NodeResult, canonical_hash
from codex_workbench.node_recovery import NodeRecoveryReconciler
from codex_workbench.node_recovery_observation import collect_node_observation
from codex_workbench.node_recovery_owner_repair import OwnerRepairNodeActions
from codex_workbench.node_recovery_policy import RecoveryPolicy, plan_recovery
from codex_workbench.node_recovery_store import NodeRecoveryStore
from codex_workbench.service import Coordinator
from codex_workbench.store import StateConflictError
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
                ],
            )
            task = store.get_task(contract.task_id)
            nodes = {node["node_id"]: node for node in task["nodes"]}
            self.assertEqual(task["state"], "blocked")
            self.assertEqual((nodes["B"]["state"], nodes["B"]["attempt"]), ("blocked", 5))
            self.assertEqual((nodes["C"]["state"], nodes["C"]["attempt"]), ("pending", 1))
            self.assertEqual(nodes["D"]["result"], d_before["result"])
            self.assertIsNone(coordinator._claim_next_ready_node("must-not-spin"))

            owner_wait = store.blocked_owner_repair_wait(
                contract.task_id, "D", int(d_before["attempt"])
            )
            assert owner_wait is not None
            self.assertTrue(owner_wait["pending"])
            self.assertTrue(owner_wait["preparation_exhausted"])
            exhaustion = {
                dependency["node_id"]: dependency["preparation_exhausted"]
                for dependency in owner_wait["dependencies"]
            }
            self.assertEqual(exhaustion, {"B": True, "C": False})
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

            current = store.get_task(contract.task_id)
            recovery = NodeRecoveryStore(store)
            resume_policy = RecoveryPolicy(
                enabled=True,
                allowed_actions=("request_repair", "resume_owner_repairs"),
                repair_repository=str(fixture.repository),
                repair_allowed_scopes=("src",),
            )
            recovery.configure_policy(
                contract.task_id,
                resume_policy,
                expected_task_revision=int(current["state_revision"]),
                actor="verified-repair-fixture",
            )
            initial_repair_observation = collect_node_observation(
                store, contract.task_id, "D"
            )
            initial_repair_observation.update({
                "now": "2026-09-14T16:05:00+00:00",
                "elapsed_seconds": 0,
                "action_attempts": 0,
            })
            request_decision = plan_recovery(
                resume_policy, initial_repair_observation
            )
            self.assertEqual(
                (request_decision["state"], request_decision["action"]),
                ("ready", "request_repair"),
            )
            adapter = OwnerRepairNodeActions(store)
            repair_fingerprint = canonical_hash({"repair": "verified-owner-harness"})
            deployment_fingerprint = canonical_hash({"deployment": "verified-owner-harness"})
            episode = recovery.record_episode(
                initial_repair_observation,
                request_decision,
            )
            claimed_episode = recovery.claim_due(
                episode["episode_id"],
                "repair-link-fixture",
                fixture.epoch,
                expected_revision=episode["revision"],
                expected_node_attempt=int(d_before["attempt"]),
            )
            assert claimed_episode is not None
            repair_action_fingerprint = canonical_hash({
                "episode_id": claimed_episode["episode_id"],
                "action": "request_repair",
            })
            intended = recovery.begin_action(
                claimed_episode["episode_id"],
                "fixture-request-repair",
                repair_action_fingerprint,
                {"stage_key": "request_repair"},
                owner_id="repair-link-fixture",
                coordinator_epoch=fixture.epoch,
                lease_epoch=claimed_episode["lease_epoch"],
                expected_revision=claimed_episode["revision"],
                expected_node_attempt=int(d_before["attempt"]),
            )
            linked = recovery.link_repair(
                intended["episode"]["episode_id"],
                owner_id="repair-link-fixture",
                coordinator_epoch=fixture.epoch,
                lease_epoch=intended["episode"]["lease_epoch"],
                expected_revision=intended["episode"]["revision"],
                repair_request_id="repair-owner-harness",
                repair_task_id="repair-owner-harness-task",
                repair_fingerprint=repair_fingerprint,
            )
            settled_repair_request = recovery.settle_action(
                linked["episode_id"],
                "fixture-request-repair",
                owner_id="repair-link-fixture",
                coordinator_epoch=fixture.epoch,
                lease_epoch=linked["lease_epoch"],
                expected_revision=linked["revision"],
                expected_node_attempt=int(d_before["attempt"]),
                receipt={
                    "ok": True,
                    "known_effects": True,
                    "stage_succeeded": True,
                    "evidence_refs": {},
                    "observation_patch": {
                        "repair_requested": True,
                        "repair_linked": True,
                        "repair_fingerprint": repair_fingerprint,
                    },
                },
                next_decision={
                    "category": "tooling_bug",
                    "state": "waiting",
                    "action": None,
                    "owner": "repair",
                    "reason_kind": "repair_deployment_wait",
                    "requires_authorization": False,
                    "next_wakeup_at": "2000-01-01T00:00:00+00:00",
                },
                receipt_state="completed",
                effect_dispatched=True,
                material_progress=True,
            )["episode"]
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE node_recovery_episodes SET time_budget_deadline_at = ? WHERE episode_id = ?",
                    ("2000-01-01T00:00:00+00:00", episode["episode_id"]),
                )
            deployment_ref = store.artifacts.put_text(
                "verified owner repair deployment\n", "deployment.json"
            )
            deployed = recovery.mark_repair_deployed(
                settled_repair_request["episode_id"],
                expected_revision=settled_repair_request["revision"],
                expected_node_attempt=int(d_before["attempt"]),
                verified_deployment_fingerprint=deployment_fingerprint,
                expected_repair_fingerprint=repair_fingerprint,
                evidence_refs={"deployment": deployment_ref},
                verified_by="fixture-deployment-verifier",
                fresh_readiness_verified=True,
            )
            self.assertNotEqual(
                deployed["time_budget_deadline_at"], "2000-01-01T00:00:00+00:00"
            )
            repaired_observation = {
                **initial_repair_observation,
                "repair_deployed_verified": True,
                "repair_deployed": True,
                "readiness_ready": True,
                "repair_fingerprint": repair_fingerprint,
                "validated_runtime_fingerprint": deployment_fingerprint,
            }

            def observe_repaired(
                observed_store: object,
                task_id: str,
                node_id: str,
                *,
                source_event_cursor: int = 0,
            ) -> dict[str, object]:
                snapshot = collect_node_observation(
                    observed_store,  # type: ignore[arg-type]
                    task_id,
                    node_id,
                    source_event_cursor=source_event_cursor,
                )
                if node_id == "D":
                    snapshot.update({
                        "repair_fingerprint": repair_fingerprint,
                        "repair_deployed": True,
                        "repair_deployed_verified": True,
                        "readiness_ready": True,
                        "validated_runtime_fingerprint": deployment_fingerprint,
                    })
                return snapshot

            loop = NodeRecoveryReconciler(
                store,
                coordinator_epoch=fixture.epoch,
                adapters={"resume_owner_repairs": adapter},
                observer=observe_repaired,
                monotonic=lambda: 0,
            )
            completed_recovery = loop.reconcile_once()
            self.assertEqual(len(completed_recovery), 1)
            completed_episode = recovery.get_episode(episode["episode_id"])
            resume_actions = [
                item
                for item in completed_episode["actions"]
                if item["stage_key"] == "resume_owner_repairs"
            ]
            self.assertEqual(len(resume_actions), 1)
            action = resume_actions[0]
            self.assertEqual(
                action["action_input"]["action"], "resume_owner_repairs"
            )
            self.assertTrue(action["receipt"]["stage_succeeded"])
            self.assertIn(
                "owner-repair-resume", action["receipt"]["evidence_refs"]
            )
            replayed_action = adapter.execute(action["action_input"])
            self.assertEqual(
                replayed_action["request_id"], action["action_input"]["request_id"]
            )
            resume_events = [
                event
                for event in store.read_events(task_id=contract.task_id)
                if event["event_type"] == "task.exhausted_owner_repairs_resumed"
            ]
            self.assertEqual(len(resume_events), 1)
            self.assertEqual(
                resume_events[0]["payload"]["request_id"],
                action["action_input"]["request_id"],
            )

            resumed_task = store.get_task(contract.task_id)
            resumed_nodes = {node["node_id"]: node for node in resumed_task["nodes"]}
            self.assertEqual(resumed_task["state"], "queued")
            self.assertEqual((resumed_nodes["B"]["state"], resumed_nodes["B"]["attempt"]), (
                "pending", 5,
            ))
            self.assertEqual((resumed_nodes["C"]["state"], resumed_nodes["C"]["attempt"]), (
                "pending", 1,
            ))

            with patch.object(
                accepted_source_repair,
                "prepare_accepted_source_repair",
                side_effect=AcceptedSourceRepairError(
                    "first verified repair did not fix owner preparation"
                ),
            ):
                failed_after_repair = coordinator._claim_next_ready_node(
                    "owner-after-first-repair"
                )
                assert failed_after_repair is not None
                self.assertEqual(
                    (failed_after_repair["node_id"], failed_after_repair["attempt"]),
                    ("B", 6),
                )
                self.assertIn("repair B", failed_after_repair["steering"])
                coordinator._execute_claimed(failed_after_repair)

            same_evidence_observation = collect_node_observation(
                store, contract.task_id, "D"
            )
            same_evidence_observation.update(repaired_observation)
            same_evidence_observation["task_revision"] = store.get_task(
                contract.task_id
            )["state_revision"]
            replay_plan = adapter.prepare(
                same_evidence_observation,
                "resume_owner_repairs",
                "resume-with-reused-repair-evidence",
            )
            with self.assertRaisesRegex(StateConflictError, "already consumed"):
                adapter.execute(replay_plan)

            refreshed_readiness_plan = adapter.prepare(
                {
                    **same_evidence_observation,
                    "validated_runtime_fingerprint": "runtime-" + "e" * 64,
                },
                "resume_owner_repairs",
                "resume-with-reused-repair-new-readiness",
            )
            with self.assertRaisesRegex(StateConflictError, "already consumed"):
                adapter.execute(refreshed_readiness_plan)

            new_evidence_observation = {
                **same_evidence_observation,
                "repair_fingerprint": "repair-" + "c" * 64,
                "validated_runtime_fingerprint": "runtime-" + "d" * 64,
            }
            second_plan = adapter.prepare(
                new_evidence_observation,
                "resume_owner_repairs",
                "resume-with-new-repair-evidence",
            )
            second_resumed = adapter.execute(second_plan)
            self.assertTrue(second_resumed["stage_succeeded"])

            completed_attempts: list[tuple[str, int, tuple[str, ...]]] = []

            class RepairedExecutor:
                def execute(self, request: object) -> NodeResult:
                    completed_attempts.append((
                        str(request.node_id),  # type: ignore[attr-defined]
                        int(request.attempt),  # type: ignore[attr-defined]
                        tuple(request.steering),  # type: ignore[attr-defined]
                    ))
                    return NodeResult(
                        "succeeded",
                        f"verified repair continued {request.node_id}",  # type: ignore[attr-defined]
                    )

            with patch.object(coordinator, "_executor", return_value=RepairedExecutor()):
                b_claim = coordinator._claim_next_ready_node("owner-after-repair-B")
                assert b_claim is not None
                coordinator._execute_claimed(b_claim)
                c_claim = coordinator._claim_next_ready_node("owner-after-repair-C")
                assert c_claim is not None
                coordinator._execute_claimed(c_claim)
            self.assertEqual(
                [(node_id, attempt) for node_id, attempt, _ in completed_attempts],
                [("B", 7), ("C", 2)],
            )
            self.assertIn("repair B", completed_attempts[0][2])
            self.assertIn("repair C", completed_attempts[1][2])
            final_wait = store.blocked_owner_repair_wait(
                contract.task_id, "D", int(d_before["attempt"])
            )
            assert final_wait is not None
            self.assertFalse(final_wait["pending"])
            final_task = store.get_task(contract.task_id)
            final_d = next(node for node in final_task["nodes"] if node["node_id"] == "D")
            self.assertEqual(final_d["result"], d_before["result"])


if __name__ == "__main__":
    unittest.main()
