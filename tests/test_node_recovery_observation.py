"""Focused, model-free fixtures for the Authority recovery observation view."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.execution_attribution import (
    ExecutionAttribution,
    ExecutionStateReference,
    FailureAttribution,
    RequestedModelIdentity,
)
from codex_workbench.node_recovery_observation import (
    _ARTIFACT_BYTES_LIMIT,
    collect_node_observation,
)
from codex_workbench.model import canonical_json
from tests import test_acceptance_amendment as _acceptance_amendment


class _FixtureStore:
    def __init__(self, root: Path, task: dict[str, object]) -> None:
        self.artifacts = ArtifactStore(root / "artifacts")
        self.task = task
        self.rollback: dict[str, object] | None = None

    def get_task(self, task_id: str) -> dict[str, object]:
        if task_id != self.task["task_id"]:
            raise KeyError(task_id)
        return self.task

    def current_blocked_worktree_recovery_rollback(self, *_args: object, **_kwargs: object) -> dict[str, object] | None:
        return self.rollback


def _attribution(task_id: str, node_id: str, attempt: int, origin: str, cursor: int = 11) -> dict[str, object]:
    value = ExecutionAttribution(
        state=ExecutionStateReference(task_id, node_id, attempt, event_cursor=cursor),
        requested_model=RequestedModelIdentity(provider="fixture", model_id="fixture"),
    )
    return replace(value, failure=FailureAttribution(origin=origin)).to_dict()  # type: ignore[arg-type]


def _readiness(*, ready: bool = False, code: str = "missing-dependency", check_id: str = "pnpm:linker") -> dict[str, object]:
    failures = [] if ready else [{
        "origin": "environment",
        "check_id": check_id,
        "code": code,
        "message": "diagnostic text is not used for attribution",
        "remediation": "fixture",
    }]
    return {
        "schema_version": 1,
        "worktree": "/private/fixture/worktree",
        "ready": ready,
        "failure_origin": None if ready else "environment",
        "summary": "contains no authority for classification",
        "elapsed_ms": 37,
        "checks": [],
        "failures": failures,
    }


def _task(
    result: dict[str, object] | None,
    *,
    task_state: str = "blocked",
    node_state: str = "blocked",
    attempt: int = 1,
    verifier: bool = False,
    depends_on: tuple[str, ...] = (),
    ancestors: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    nodes: list[dict[str, object]] = ancestors or []
    nodes.append({
        "node_id": "verify" if verifier else "work",
        "state": node_state,
        "attempt": attempt,
        "verifier": verifier,
        "depends_on": list(depends_on),
        "result": result,
    })
    return {
        "task_id": "task-1",
        "state": task_state,
        "state_revision": 7,
        "contract": {"base_sha": "fixture-base"},
        "nodes": nodes,
    }


class NodeRecoveryObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="node-recovery-observation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _store(self, task: dict[str, object]) -> _FixtureStore:
        return _FixtureStore(self.root, task)

    def _readiness_ref(self, store: _FixtureStore, **kwargs: object) -> str:
        return store.artifacts.put_text(
            json.dumps(_readiness(**kwargs), sort_keys=True),
            "execution-readiness.json",
        )

    def _rollback(
        self,
        store: _FixtureStore,
        preparation: dict[str, object] | None,
        *,
        source_attempt: int = 1,
        preparation_attempt: int = 2,
    ) -> None:
        store.rollback = {
            "state": "available",
            "rollback_event_cursor": 41,
            "rollback_provenance": {
                "event_type": "node.blocked_worktree_recovery_rolled_back",
                "event_cursor": 41,
                "created_at": "2026-09-11T00:00:00+00:00",
                "task_revision": 7,
                "source_attempt": source_attempt,
                "preparation_attempt": preparation_attempt,
                "source_allocation_id": "allocation-1",
                "source_worktree": "/private/fixture/worktree",
                "source_branch": "fixture/task-1/work/1",
            },
            "preparation_result": preparation,
        }

    def _recovery_preparation_ref(
        self,
        store: _FixtureStore,
        *,
        source_attempt: int = 1,
        recovery_attempt: int = 2,
        phase: str = "acceptance",
        code: str = "acceptance-command-failed",
        evidence_refs: dict[str, str] | None = None,
    ) -> str:
        return store.artifacts.put_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "recovery-preparation-failure",
                    "task_id": "task-1",
                    "node_id": "work",
                    "source_attempt": source_attempt,
                    "recovery_attempt": recovery_attempt,
                    "phase": phase,
                    "code": code,
                    "executor_started": False,
                    "evidence_refs": evidence_refs or {},
                },
                sort_keys=True,
            ),
            "recovery-preparation.json",
        )

    def test_missing_result_is_unknown_and_fingerprint_is_stable(self) -> None:
        store = self._store(_task(None, node_state="pending", task_state="queued", attempt=0))
        first = collect_node_observation(store, "task-1", "work")
        second = collect_node_observation(store, "task-1", "work")

        self.assertEqual(first["category"], "unknown")
        self.assertEqual(first["origin"], "unknown")
        self.assertEqual(first["evidence_refs"], {})
        self.assertNotIn("validation_succeeded", first)
        self.assertNotIn("readiness_ready", first)
        self.assertFalse(first["material_progress"])
        self.assertEqual(first["failure_fingerprint"], second["failure_fingerprint"])

    def test_current_readiness_failure_maps_dependency_and_pre_execution(self) -> None:
        store = self._store(_task(None))
        ref = self._readiness_ref(store)
        result = {
            "status": "blocked",
            "summary": "arbitrary summary",
            "changed_paths": ["a" * 100_000],
            "artifacts": {"execution-readiness": ref},
            "execution_attribution": _attribution("task-1", "work", 1, "environment"),
        }
        store.task["nodes"][0]["result"] = result  # type: ignore[index]
        observation = collect_node_observation(store, "task-1", "work", source_event_cursor=4)

        self.assertEqual(observation["category"], "dependency")
        self.assertEqual(observation["origin"], "environment")
        self.assertEqual(observation["phase"], "pre_execution")
        self.assertFalse(observation["implementation_ready"])
        self.assertTrue(observation["only_missing_dependency"])
        self.assertFalse(observation["dependencies_ready"])
        self.assertEqual(observation["source_event_cursor"], 11)

    def test_stale_attribution_does_not_turn_readiness_into_environment(self) -> None:
        store = self._store(_task(None))
        ref = self._readiness_ref(store)
        store.task["nodes"][0]["result"] = {  # type: ignore[index]
            "status": "blocked",
            "summary": "environment words do not matter",
            "artifacts": {"execution-readiness": ref},
            "execution_attribution": _attribution("task-1", "work", 2, "environment"),
        }

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["category"], "unknown")
        self.assertEqual(observation["origin"], "unknown")
        self.assertEqual(observation["phase"], "blocked_observed")
        self.assertIsNone(observation["implementation_ready"])

    def test_auth_attribution_is_not_relabelled_by_readiness_artifact(self) -> None:
        store = self._store(_task(None))
        ref = self._readiness_ref(store)
        store.task["nodes"][0]["result"] = {  # type: ignore[index]
            "status": "blocked",
            "summary": "authentication failure",
            "artifacts": {"execution-readiness": ref},
            "execution_attribution": _attribution("task-1", "work", 1, "auth"),
        }

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["category"], "auth")
        self.assertEqual(observation["origin"], "auth")

    def test_verifier_wait_preserves_accepted_ancestor_source_ref(self) -> None:
        store = self._store(_task(None))
        patch_ref = store.artifacts.put_text("fixture patch", "patch")
        ancestor = {
            "node_id": "work",
            "state": "accepted",
            "attempt": 1,
            "verifier": False,
            "depends_on": [],
            "result": {"status": "succeeded", "artifacts": {"patch": patch_ref}},
        }
        store.task = _task(
            None,
            verifier=True,
            depends_on=("work",),
            ancestors=[ancestor],
        )
        ref = self._readiness_ref(store)
        store.task["nodes"][-1]["result"] = {  # type: ignore[index]
            "status": "blocked",
            "artifacts": {"execution-readiness": ref},
            "execution_attribution": _attribution("task-1", "verify", 1, "environment"),
        }

        observation = collect_node_observation(store, "task-1", "verify")

        self.assertTrue(observation["verification_wait"])
        self.assertEqual(observation["accepted_ancestors"], [
            {"node_id": "work", "attempt": 1, "patch_ref": patch_ref}
        ])
        self.assertIn(patch_ref, observation["source_refs"])

    def test_materialization_failure_is_dependency_without_summary_inference(self) -> None:
        store = self._store(_task(None))
        ref = store.artifacts.put_text(
            json.dumps({
                "schema_version": 1,
                "kind": "pnpm-offline-materialization",
                "status": "blocked",
                "reason": "summary must not be parsed",
            }),
            "dependency-materialization.json",
        )
        store.task["nodes"][0]["result"] = {  # type: ignore[index]
            "status": "blocked",
            "summary": "not a dependency code",
            "artifacts": {"dependency-materialization": ref},
            "execution_attribution": _attribution("task-1", "work", 1, "environment"),
        }

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["category"], "dependency")
        self.assertFalse(observation["dependencies_ready"])
        self.assertTrue(observation["only_missing_dependency"])

    def test_verification_failure_stays_validation_failure_despite_summary(self) -> None:
        store = self._store(_task(None))
        store.task["nodes"][0]["result"] = {  # type: ignore[index]
            "status": "failed",
            "summary": "pnpm missing dependency and network login",
            "execution_attribution": _attribution("task-1", "work", 1, "verification"),
        }

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["category"], "validation_failure")
        self.assertEqual(observation["origin"], "verification")

    def test_untrusted_result_validation_fields_do_not_authorize_a_profile(self) -> None:
        store = self._store(_task(None))
        store.task["nodes"][0]["result"] = {  # type: ignore[index]
            "status": "failed",
            "validation_profile": "dsh-b-ipc-v1",
            "validation_succeeded": True,
            "execution_attribution": _attribution("task-1", "work", 1, "verification"),
        }

        observation = collect_node_observation(store, "task-1", "work")

        self.assertIsNone(observation["validation_profile"])
        self.assertNotIn("validation_succeeded", observation)

    def test_oversized_artifact_is_not_read_and_validation_profile_is_bounded(self) -> None:
        store = self._store(_task(None))
        oversized_ref = store.artifacts.put_text(
            "{" + "x" * _ARTIFACT_BYTES_LIMIT + "}",
            "execution-readiness.json",
        )
        validation_ref = store.artifacts.put_text(
            json.dumps({"schema_version": 1, "check_id": "dsh-b-ipc-v1", "ok": False}),
            "validation.json",
        )
        store.task["nodes"][0]["result"] = {  # type: ignore[index]
            "status": "blocked",
            "artifacts": {"execution-readiness": oversized_ref, "validation": validation_ref},
            "execution_attribution": _attribution("task-1", "work", 1, "verification"),
        }

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["category"], "validation_failure")
        self.assertEqual(observation["validation_profile"], "dsh-b-ipc-v1")
        self.assertEqual(observation["evidence_refs"]["execution-readiness"], oversized_ref)

    def test_rollback_preparation_uses_a2_attribution_and_preserves_source_refs(self) -> None:
        source_dependency = self._store(_task(None)).artifacts.put_text("source dependency", "dependency-input.json")
        store = self._store(
            _task(
                {
                    "status": "blocked",
                    "artifacts": {"dependency-input": source_dependency},
                    "execution_attribution": _attribution("task-1", "work", 1, "auth"),
                }
            )
        )
        recovery_ref = store.artifacts.put_text("recovery evidence", "recovery.log")
        preparation_ref = self._recovery_preparation_ref(
            store,
            evidence_refs={"recovery": recovery_ref},
        )
        self._rollback(
            store,
            {
                "status": "failed",
                "artifacts": {"recovery-preparation": preparation_ref},
                "execution_attribution": _attribution("task-1", "work", 2, "verification", cursor=42),
            },
        )

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["node_attempt"], 1)
        self.assertEqual(observation["preparation_attempt"], 2)
        self.assertEqual(observation["phase"], "recovery_preparation")
        self.assertEqual(observation["category"], "validation_failure")
        self.assertEqual(observation["origin"], "verification")
        self.assertEqual(observation["failure_code"], "acceptance-command-failed")
        self.assertEqual(observation["preparation_phase"], "acceptance")
        self.assertTrue(observation["executor_not_started"])
        self.assertEqual(observation["rollback_event_cursor"], 41)
        self.assertEqual(observation["source_evidence_refs"]["dependency-input"], source_dependency)
        self.assertIn(source_dependency, observation["dependency_refs"])
        assert store.rollback is not None
        store.rollback["rollback_event_cursor"] = 42
        provenance = store.rollback["rollback_provenance"]
        assert isinstance(provenance, dict)
        provenance["event_cursor"] = 42

        repeated = collect_node_observation(store, "task-1", "work")

        self.assertEqual(repeated["rollback_event_cursor"], 42)
        self.assertEqual(repeated["failure_fingerprint"], observation["failure_fingerprint"])

    def test_rollback_acceptance_failure_without_a2_attribution_remains_unknown(self) -> None:
        store = self._store(_task({"status": "blocked", "execution_attribution": _attribution("task-1", "work", 1, "auth")}))
        preparation_ref = self._recovery_preparation_ref(store)
        self._rollback(
            store,
            {"status": "failed", "artifacts": {"recovery-preparation": preparation_ref}},
        )

        observation = collect_node_observation(store, "task-1", "work")

        self.assertEqual(observation["category"], "unknown")
        self.assertEqual(observation["origin"], "unknown")
        self.assertEqual(observation["phase"], "recovery_preparation")
        self.assertEqual(observation["failure_code"], "acceptance-command-failed")
        self.assertTrue(observation["executor_not_started"])

class RollbackObservationStoreLifecycleTests(unittest.TestCase):
    """Use the retained-source rollback fixture without rediscovering its tests."""

    def _fixture(self) -> _acceptance_amendment.AcceptanceAmendmentTests:
        fixture = _acceptance_amendment.AcceptanceAmendmentTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    @staticmethod
    def _worker(task: dict[str, object]) -> dict[str, object]:
        return next(node for node in task["nodes"] if node["node_id"] == "worker")  # type: ignore[index, return-value]

    def _rollback_projection(
        self,
        fixture: _acceptance_amendment.AcceptanceAmendmentTests,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        restored = fixture._rollback_source_only_recovery()
        worker = self._worker(restored)
        projection = fixture.store.current_blocked_worktree_recovery_rollback(
            fixture.contract.task_id,
            "worker",
            task_revision=int(restored["state_revision"]),
            node_state=str(worker["state"]),
            node_attempt=int(worker["attempt"]),
            settled_at=worker["settled_at"],
            worktree=worker["worktree"],
        )
        self.assertIsNotNone(projection)
        return restored, worker, projection  # type: ignore[return-value]

    def test_real_rollback_projects_a2_without_old_worker_attribution(self) -> None:
        fixture = self._fixture()
        restored, worker, projection = self._rollback_projection(fixture)

        self.assertEqual(projection["state"], "available")
        preparation = projection["preparation_result"]
        self.assertIsInstance(preparation, dict)
        self.assertEqual(preparation["status"], "blocked")  # type: ignore[index]
        self.assertEqual(set(preparation), {"status", "artifacts"})  # type: ignore[arg-type]
        event = next(
            event for event in fixture.store.read_events(task_id=fixture.contract.task_id)
            if event["event_type"] == "node.blocked_worktree_recovery_rolled_back"
        )
        self.assertIn("summary", event["payload"]["preparation_result"])
        self.assertIn("checks", event["payload"]["preparation_result"])

        observation = collect_node_observation(fixture.store, fixture.contract.task_id, "worker")
        ancestor = next(node for node in restored["nodes"] if node["node_id"] == "ancestor")

        self.assertEqual((observation["node_attempt"], observation["preparation_attempt"]), (1, 2))
        self.assertEqual(observation["phase"], "recovery_preparation")
        self.assertEqual((observation["category"], observation["origin"]), ("unknown", "unknown"))
        self.assertEqual(observation["rollback_event_cursor"], event["cursor"])
        self.assertEqual(observation["source_evidence_refs"]["dependency-input"], fixture.dependency_input_ref)
        self.assertIn(fixture.dependency_input_ref, observation["dependency_refs"])
        self.assertEqual(observation["accepted_ancestors"], [
            {
                "node_id": "ancestor",
                "attempt": 1,
                "patch_ref": ancestor["result"]["artifacts"]["patch"],
            }
        ])
        self.assertEqual(worker["worktree"], str(fixture.source))

    def test_lookup_ignores_stale_other_node_and_old_settlement_events_without_copying_checks(self) -> None:
        fixture = self._fixture()
        restored, worker, projection = self._rollback_projection(fixture)
        original = next(
            event for event in fixture.store.read_events(task_id=fixture.contract.task_id)
            if event["event_type"] == "node.blocked_worktree_recovery_rolled_back"
        )
        matching_payload = json.loads(json.dumps(original["payload"]))
        matching_payload["preparation_result"]["summary"] = "x" * (2 * _ARTIFACT_BYTES_LIMIT)
        matching_payload["preparation_result"]["checks"] = ["y" * (2 * _ARTIFACT_BYTES_LIMIT)]
        stale_payload = json.loads(json.dumps(original["payload"]))
        stale_payload["source_attempt"] = 0
        stale_payload["attempt"] = 1
        old_payload = json.loads(json.dumps(original["payload"]))
        with fixture.store.transaction() as connection:
            fixture.store._event(
                connection,
                "node.blocked_worktree_recovery_rolled_back",
                fixture.contract.task_id,
                "worker",
                matching_payload,
                created_at=str(worker["settled_at"]),
            )
            fixture.store._event(
                connection,
                "node.blocked_worktree_recovery_rolled_back",
                fixture.contract.task_id,
                "worker",
                stale_payload,
                created_at=str(worker["settled_at"]),
            )
            fixture.store._event(
                connection,
                "node.blocked_worktree_recovery_rolled_back",
                fixture.contract.task_id,
                "verify",
                matching_payload,
                created_at=str(worker["settled_at"]),
            )
            fixture.store._event(
                connection,
                "node.blocked_worktree_recovery_rolled_back",
                fixture.contract.task_id,
                "worker",
                old_payload,
                created_at="2000-01-01T00:00:00+00:00",
            )

        updated = fixture.store.current_blocked_worktree_recovery_rollback(
            fixture.contract.task_id,
            "worker",
            task_revision=int(restored["state_revision"]),
            node_state=str(worker["state"]),
            node_attempt=int(worker["attempt"]),
            settled_at=worker["settled_at"],
            worktree=worker["worktree"],
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated["state"], "available")  # type: ignore[index]
        self.assertNotIn("summary", updated["preparation_result"])  # type: ignore[index]
        self.assertNotIn("checks", updated["preparation_result"])  # type: ignore[index]
        self.assertGreater(updated["rollback_event_cursor"], projection["rollback_event_cursor"])  # type: ignore[index]
        with fixture.store.connection() as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT cursor FROM events
                WHERE task_id = ? AND node_id = ?
                  AND event_type = 'node.blocked_worktree_recovery_rolled_back'
                  AND created_at = ?
                ORDER BY cursor DESC LIMIT 1
                """,
                (fixture.contract.task_id, "worker", worker["settled_at"]),
            ).fetchall()
        self.assertTrue(any("events_task_node_type_created_cursor_idx" in str(row[3]) for row in plan))

    def test_current_drift_and_malformed_current_receipts_are_unavailable(self) -> None:
        fixture = self._fixture()
        restored, worker, _projection = self._rollback_projection(fixture)
        with fixture.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET worktree = ? WHERE task_id = ? AND node_id = ?",
                ("/private/drifted-worktree", fixture.contract.task_id, "worker"),
            )
        drifted = collect_node_observation(fixture.store, fixture.contract.task_id, "worker")
        self.assertFalse(drifted["observation_available"])
        self.assertEqual(drifted["observation_unavailable_reason"], "snapshot_conflict")
        self.assertEqual((drifted["category"], drifted["origin"]), ("unknown", "unknown"))

        fixture = self._fixture()
        restored, worker, _projection = self._rollback_projection(fixture)
        event = next(
            event for event in fixture.store.read_events(task_id=fixture.contract.task_id)
            if event["event_type"] == "node.blocked_worktree_recovery_rolled_back"
        )
        malformed = json.loads(json.dumps(event["payload"]))
        malformed["preparation_result"]["artifacts"] = "not-an-artifact-map"
        with fixture.store.transaction() as connection:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (canonical_json(malformed), event["cursor"]),
            )
        unavailable = collect_node_observation(fixture.store, fixture.contract.task_id, "worker")
        self.assertFalse(unavailable["observation_available"])
        self.assertEqual(unavailable["observation_unavailable_reason"], "malformed_rollback_event")
        self.assertEqual((unavailable["category"], unavailable["origin"]), ("unknown", "unknown"))


if __name__ == "__main__":
    unittest.main()
