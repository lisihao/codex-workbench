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


class _FixtureStore:
    def __init__(self, root: Path, task: dict[str, object]) -> None:
        self.artifacts = ArtifactStore(root / "artifacts")
        self.task = task

    def get_task(self, task_id: str) -> dict[str, object]:
        if task_id != self.task["task_id"]:
            raise KeyError(task_id)
        return self.task


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


if __name__ == "__main__":
    unittest.main()
