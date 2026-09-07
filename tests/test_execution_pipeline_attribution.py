from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.execution_attribution import (
    AttributionReference,
    ExecutionAttribution,
    ExecutionStateReference,
    FailureAttribution,
    IdentityProvenance,
    ObservedModelIdentity,
    PhysicalCallIdentity,
    RequestedModelIdentity,
)
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.scheduler_metrics import build_scheduler_metrics
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore


class ExecutionPipelineAttributionTests(unittest.TestCase):
    def test_keyless_assembled_pipeline_preserves_readiness_attribution_and_event_links(self) -> None:
        """Exercise P0 through the real coordinator/store without a model CLI."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("pipeline-attribution", "test-machine")
            coordinator = Coordinator(store, state, coordinator_epoch=epoch, max_workers=1)
            calls: list[tuple[str, str, str, int]] = []

            class KeylessFixtureExecutor:
                def __init__(self, kind: str):
                    self.kind = kind

                def execute(self, request):  # type: ignore[no-untyped-def]
                    calls.append((self.kind, request.task_id, request.node_id, request.attempt))
                    if request.task_id in {"source-a", "source-b"}:
                        return NodeResult(
                            status="succeeded",
                            summary="keyless source fixture completed",
                            provider="codex",
                            result_kind="worker",
                            checks=("fixture-source-check",),
                        )
                    if request.task_id == "resume-verifier":
                        if request.node_id == "verify":
                            raise AssertionError("verifier must not run after readiness blocks")
                        assert request.worktree is not None
                        (request.worktree / "package.json").write_text(
                            json.dumps({"packageManager": "pnpm@11.25.0"}), encoding="utf-8"
                        )
                        (request.worktree / "pnpm-lock.yaml").write_text(
                            "lockfileVersion: '9.0'\n", encoding="utf-8"
                        )
                        return NodeResult(
                            status="succeeded",
                            summary="keyless fixture wrote the worker patch",
                            provider="codex",
                            result_kind="worker",
                            checks=("fixture-worker-check",),
                        )
                    if request.task_id != "fallback-retry":
                        raise AssertionError(f"unexpected keyless request {request.task_id}/{request.node_id}")
                    if request.node_id == "verify":
                        evidence_ref = coordinator.artifacts.put_text(
                            "keyless verifier evidence", "fixture-verifier.txt"
                        )
                        return NodeResult(
                            status="succeeded",
                            summary="keyless verifier accepted the assembled result",
                            artifacts={"test-log": evidence_ref},
                            provider="codex",
                            result_kind="verifier",
                            checks=("fixture-verifier-check",),
                            evidence=(evidence_ref,),
                            verdict="accepted",
                        )
                    if self.kind == "claude":
                        return NodeResult(
                            status="blocked",
                            summary="keyless Claude fixture transport interruption",
                            provider="claude",
                            result_kind="worker",
                            checks=("fixture-claude-attempt",),
                        )
                    if self.kind != "codex":
                        raise AssertionError(f"unexpected fixture executor {self.kind!r}")
                    return self._attested_fallback_result(request, failed=request.attempt == 1)

                @staticmethod
                def _started_cursor(request) -> int:  # type: ignore[no-untyped-def]
                    for event in reversed(store.read_events(task_id=request.task_id)):
                        payload = event.get("payload")
                        if (
                            event.get("event_type") == "node.started"
                            and event.get("node_id") == request.node_id
                            and isinstance(payload, dict)
                            and payload.get("attempt") == request.attempt
                        ):
                            return int(event["cursor"])
                    raise AssertionError("fixture request has no durable node.started event")

                def _attested_fallback_result(self, request, *, failed: bool) -> NodeResult:  # type: ignore[no-untyped-def]
                    # Both attempts intentionally reference the same physical
                    # call. This is local fixture evidence, never an API call.
                    receipt_ref = coordinator.artifacts.put_text(
                        json.dumps(
                            {
                                "kind": "keyless-provider-receipt",
                                "provider": "codex",
                                "call_id": "fixture-physical-call-1",
                                "attempt": request.attempt,
                            },
                            sort_keys=True,
                        ),
                        "fixture-provider-receipt.json",
                    )
                    model_id = str(request.spec["model"])
                    started_cursor = self._started_cursor(request)
                    started_ref = AttributionReference(
                        kind="event", ref="node.started", cursor=started_cursor
                    )
                    receipt = AttributionReference(kind="artifact", ref=receipt_ref)
                    provenance = IdentityProvenance(
                        source="provider_receipt", references=(receipt,)
                    )
                    attribution = ExecutionAttribution(
                        state=ExecutionStateReference(
                            task_id=request.task_id,
                            node_id=request.node_id,
                            attempt=request.attempt,
                            event_cursor=started_cursor,
                        ),
                        requested_model=RequestedModelIdentity(
                            provider="codex",
                            model_id=model_id,
                            provenance=IdentityProvenance(
                                source="routing_decision", references=(started_ref,)
                            ),
                        ),
                        observed_model=ObservedModelIdentity.attested(
                            provider="codex", model_id=model_id, provenance=provenance
                        ),
                        physical_call=PhysicalCallIdentity(
                            status="attested",
                            provider="codex",
                            call_id="fixture-physical-call-1",
                            provenance=provenance,
                        ),
                        failure=(
                            FailureAttribution(
                                origin="transport",
                                detail="keyless fixture requested a retry after transport interruption",
                                references=(receipt,),
                            )
                            if failed
                            else FailureAttribution()
                        ),
                        references=(started_ref,),
                    )
                    return NodeResult(
                        status="failed" if failed else "succeeded",
                        summary=(
                            "keyless fallback transport interruption; retry requested"
                            if failed
                            else "keyless fallback retry completed"
                        ),
                        artifacts={"provider-receipt": receipt_ref},
                        actual_model=model_id,
                        retryable=failed,
                        result_kind="worker",
                        checks=("fixture-fallback-check",),
                        requested_model=model_id,
                        provider="codex",
                        execution_attribution=attribution.to_dict(),
                    )

            executors = {
                "claude": KeylessFixtureExecutor("claude"),
                "codex": KeylessFixtureExecutor("codex"),
            }

            def execute_claim(task_id: str, node_id: str) -> None:
                claimed = store.claim_ready_node(
                    f"fixture-{task_id}-{node_id}",
                    epoch,
                    admissible=lambda spec: (
                        spec.get("task_id") == task_id and spec.get("node_id") == node_id
                    ),
                )
                self.assertIsNotNone(claimed, f"{task_id}/{node_id} should be claimable")
                assert claimed is not None
                coordinator._execute_claimed(claimed)

            try:
                with patch.object(
                    coordinator, "_executor", side_effect=lambda kind: executors[kind]
                ):
                    self._exercise_readiness_failure(root, store, epoch, coordinator, execute_claim)
                    self.assertEqual(calls, [])

                    source_paths = self._exercise_source_isolation(
                        root, store, coordinator, execute_claim
                    )
                    self.assertNotEqual(source_paths["a"], source_paths["b"])

                    self._exercise_verifier_resume(
                        root, store, coordinator, execute_claim, calls
                    )
                    self._exercise_fallback_retry(
                        root, store, execute_claim
                    )
            finally:
                coordinator._pool.shutdown(wait=True)

            self._assert_pipeline_events_and_usage(store, calls)

    def _exercise_readiness_failure(
        self,
        root: Path,
        store: WorkbenchStore,
        epoch: int,
        coordinator: Coordinator,
        execute_claim,
    ) -> None:  # type: ignore[no-untyped-def]
        target = root / "readiness-target"
        target.mkdir()
        (target / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.25.0"}), encoding="utf-8"
        )
        (target / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        contract = TaskContract(
            task_id="readiness-failure",
            repository=str(target),
            base_sha="fixture",
            objective="block an unavailable local environment before any model call",
            allowed_scope=("package.json", "pnpm-lock.yaml"),
            required_artifacts=(),
            task_type="tests",
            complexity="high",
        )
        nodes = [
            NodeSpec("work", contract.task_id, "readiness fixture", "fixture", "fixture", "must not run"),
            NodeSpec(
                "verify", contract.task_id, "fixture verifier", "fixture", "fixture", "accepted",
                depends_on=("work",), verifier=True,
            ),
        ]
        store.create_task(contract, nodes, "readiness-create")
        store.queue_task(contract.task_id)
        execute_claim(contract.task_id, "work")
        task = store.get_task(contract.task_id)
        work = self._node(task, "work")
        self.assertEqual((task["state"], work["state"]), ("blocked", "blocked"))
        result = work["result"]
        assert isinstance(result, dict)
        attribution = result["execution_attribution"]
        self.assertEqual(attribution["failure"]["origin"], "environment")
        self.assertEqual(attribution["observed_model"]["status"], "unknown")
        report = json.loads(
            coordinator.artifacts.verify(result["artifacts"]["execution-readiness"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(report["ready"])

    def _exercise_source_isolation(
        self,
        root: Path,
        store: WorkbenchStore,
        coordinator: Coordinator,
        execute_claim,
    ) -> dict[str, Path]:  # type: ignore[no-untyped-def]
        source_paths: dict[str, Path] = {}
        for suffix, marker in (("a", "SOURCE_A_ONLY"), ("b", "SOURCE_B_ONLY")):
            repository, base_sha = self._repository(
                root,
                f"source-{suffix}-repository",
                {"src/local_package/__init__.py": f"MARKER = {marker!r}\n"},
            )
            task_id = f"source-{suffix}"
            contract = TaskContract(
                task_id=task_id,
                repository=str(repository),
                base_sha=base_sha,
                objective="resolve only the allocated local source tree",
                allowed_scope=("src",),
                required_artifacts=(),
                task_type="tests",
                complexity="high",
            )
            nodes = [
                NodeSpec(
                    "work", task_id, "source fixture", "codex", "gpt-5.6-luna",
                    "inspect target-local source only", read_scopes=("src",),
                    task_type="tests", complexity="high", parallelizable=True,
                ),
                NodeSpec(
                    "verify", task_id, "fixture verifier", "fixture", "fixture", "accepted",
                    depends_on=("work",), verifier=True,
                ),
            ]
            store.create_task(contract, nodes, f"{task_id}-create")
            store.queue_task(task_id)
            execute_claim(task_id, "work")
            node = self._node(store.get_task(task_id), "work")
            result = node["result"]
            assert isinstance(result, dict)
            report = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["execution-readiness"]).read_text(
                    encoding="utf-8"
                )
            )
            source_check = next(
                check for check in report["checks"] if check["id"] == "source:local_package"
            )
            source_paths[suffix] = Path(source_check["detail"]["origin"])
            self.assertEqual(source_check["status"], "passed")
            self.assertEqual(
                source_paths[suffix],
                Path(node["worktree"]) / "src" / "local_package" / "__init__.py",
            )
            self.assertEqual(result["execution_attribution"]["observed_model"]["status"], "unknown")
        return source_paths

    def _exercise_verifier_resume(
        self,
        root: Path,
        store: WorkbenchStore,
        coordinator: Coordinator,
        execute_claim,
        calls: list[tuple[str, str, str, int]],
    ) -> None:  # type: ignore[no-untyped-def]
        repository, base_sha = self._repository(
            root, "resume-repository", {"README.md": "resume fixture\n"}
        )
        contract = TaskContract(
            task_id="resume-verifier",
            repository=str(repository),
            base_sha=base_sha,
            objective="preserve accepted work when verifier readiness blocks",
            allowed_scope=("package.json", "pnpm-lock.yaml"),
            required_artifacts=(),
            task_type="tests",
            complexity="high",
        )
        nodes = [
            NodeSpec(
                "work", contract.task_id, "worker fixture", "codex", "gpt-5.6-luna",
                "create package inputs", write_scopes=("package.json", "pnpm-lock.yaml"),
                task_type="tests", complexity="high", parallelizable=True,
            ),
            NodeSpec(
                "verify", contract.task_id, "verifier fixture", "codex", "gpt-5.6-sol",
                "verify the composed patch", depends_on=("work",),
                read_scopes=("package.json", "pnpm-lock.yaml"), verifier=True,
                task_type="tests", complexity="high", parallelizable=True,
            ),
        ]
        store.create_task(contract, nodes, "resume-create")
        store.queue_task(contract.task_id)
        execute_claim(contract.task_id, "work")
        execute_claim(contract.task_id, "verify")
        task = store.get_task(contract.task_id)
        worker = self._node(task, "work")
        verifier = self._node(task, "verify")
        self.assertEqual(
            (task["state"], worker["state"], verifier["state"]),
            ("blocked", "accepted", "blocked"),
        )
        worker_result = worker["result"]
        assert isinstance(worker_result, dict)
        self.assertIn("patch", worker_result["artifacts"])
        self.assertIn(
            "packageManager",
            coordinator.artifacts.verify(worker_result["artifacts"]["patch"]).read_text(encoding="utf-8"),
        )
        self.assertNotIn(("codex", "resume-verifier", "verify", 1), calls)
        verifier_result = verifier["result"]
        assert isinstance(verifier_result, dict)
        self.assertEqual(verifier_result["execution_attribution"]["failure"]["origin"], "environment")

    def _exercise_fallback_retry(
        self,
        root: Path,
        store: WorkbenchStore,
        execute_claim,
    ) -> None:  # type: ignore[no-untyped-def]
        repository, base_sha = self._repository(
            root, "pipeline-repository", {"README.md": "PIPELINE_SOURCE_SENTINEL\n"}
        )
        store.write_quota(
            QuotaSnapshot(
                observed_at=now_iso(),
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=60,
                weekly_all_remaining=60,
                weekly_sonnet_remaining=60,
                **self._compatible_provenance(),
            )
        )
        contract = TaskContract(
            task_id="fallback-retry",
            repository=str(repository),
            base_sha=base_sha,
            objective="correlate fallback retry and verifier evidence",
            allowed_scope=("README.md",),
            required_artifacts=(),
            task_type="tests",
            complexity="high",
        )
        nodes = [
            NodeSpec(
                "work", contract.task_id, "fallback fixture", "claude", "sonnet",
                "exercise keyless fallback", read_scopes=("README.md",), task_type="tests",
                complexity="high", parallelizable=True, routing_policy_version="model-routing-v3",
            ),
            NodeSpec(
                "verify", contract.task_id, "verifier fixture", "codex", "gpt-5.6-sol",
                "verify the durable retry result", depends_on=("work",), read_scopes=("README.md",),
                verifier=True, task_type="tests", complexity="high", parallelizable=True,
                routing_policy_version="model-routing-v3",
            ),
        ]
        store.create_task(contract, nodes, "pipeline-create")
        store.queue_task(contract.task_id)
        execute_claim(contract.task_id, "work")
        self.assertEqual(self._node(store.get_task(contract.task_id), "work")["state"], "pending")
        execute_claim(contract.task_id, "work")
        execute_claim(contract.task_id, "verify")

    def _assert_pipeline_events_and_usage(
        self, store: WorkbenchStore, calls: list[tuple[str, str, str, int]]
    ) -> None:
        task = store.get_task("fallback-retry")
        self.assertEqual(task["state"], "accepted")
        self.assertEqual(
            [call for call in calls if call[1] == "fallback-retry"],
            [
                ("claude", "fallback-retry", "work", 1),
                ("codex", "fallback-retry", "work", 1),
                ("codex", "fallback-retry", "work", 2),
                ("codex", "fallback-retry", "verify", 1),
            ],
        )
        events = store.read_events(task_id="fallback-retry")
        starts = {
            (event["node_id"], event["payload"]["attempt"]): event["cursor"]
            for event in events
            if event["event_type"] == "node.started"
        }
        terminals = [
            event for event in events if event["event_type"] in {"node.failed", "node.accepted"}
        ]
        self.assertEqual(len(terminals), 3)
        self.assertEqual(len([event for event in events if event["event_type"] == "node.routed"]), 1)
        self.assertEqual(
            len([event for event in events if event["event_type"] == "node.retry_scheduled"]), 1
        )
        for event in terminals:
            payload = event["payload"]
            attribution = payload["result"]["execution_attribution"]
            self.assertEqual(
                attribution["state"]["event_cursor"],
                starts[(event["node_id"], payload["attempt"])],
            )

        first = next(
            event for event in terminals
            if event["node_id"] == "work" and event["payload"]["attempt"] == 1
        )["payload"]["result"]["execution_attribution"]
        second = next(
            event for event in terminals
            if event["node_id"] == "work" and event["payload"]["attempt"] == 2
        )["payload"]["result"]["execution_attribution"]

        # Direct receipt evidence must survive the coordinator boundary. A
        # downgrade here is a runtime integration defect, not model quality.
        with self.subTest("attested physical call survives fallback retry"):
            self.assertEqual(first["observed_model"]["status"], "attested")
            self.assertEqual(first["physical_call"]["status"], "attested")
            self.assertEqual(second["observed_model"]["status"], "attested")
            self.assertEqual(
                first["physical_call"]["usage_deduplication_key"],
                second["physical_call"]["usage_deduplication_key"],
            )

        usage = build_scheduler_metrics(
            store, now=datetime.now(UTC), window_seconds=3_600, max_workers=1
        )["global"]["attribution"]["physical_call_usage"]
        with self.subTest("physical usage is counted once across the retry"):
            self.assertEqual(usage["unique_attested_physical_calls"], 1)
            self.assertGreaterEqual(usage["deduplicated_retry_or_fallback_attempts"], 1)

        # Only references travel through the durable event envelope. The source
        # sentinel was never copied into an event or provider transcript.
        event_json = json.dumps(events, sort_keys=True)
        self.assertNotIn("PIPELINE_SOURCE_SENTINEL", event_json)
        self.assertNotIn("transcript", event_json)

    @staticmethod
    def _node(task: dict[str, object], node_id: str) -> dict[str, object]:
        nodes = task["nodes"]
        assert isinstance(nodes, list)
        return next(node for node in nodes if node["node_id"] == node_id)

    @staticmethod
    def _compatible_provenance() -> dict[str, object]:
        return {
            "source": COMPATIBLE_SOURCE,
            "producer": PRODUCER,
            "producer_schema_version": PRODUCER_SCHEMA_VERSION,
            "claude_version": SUPPORTED_USAGE_VERSION,
        }

    @staticmethod
    def _repository(root: Path, name: str, files: dict[str, str]) -> tuple[Path, str]:
        repository = root / name
        repository.mkdir()
        for relative_path, contents in files.items():
            target = repository / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        for command in (
            ("git", "init", "-b", "main"),
            ("git", "config", "user.email", "fixture@example.invalid"),
            ("git", "config", "user.name", "Fixture"),
            ("git", "add", "."),
            ("git", "commit", "-m", "fixture"),
        ):
            subprocess.run(command, cwd=repository, check=True, capture_output=True, text=True)
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        return repository, base_sha


if __name__ == "__main__":
    unittest.main()
