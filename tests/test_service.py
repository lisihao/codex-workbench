from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from codex_workbench.acceptance import build_acceptance_report
from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.config import WorkbenchConfig
from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.evidence import evidence_fingerprint
from codex_workbench.dirty_worktree_recovery import PnpmOfflineMaterializer
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.service import Coordinator, _ClaimRoute
from codex_workbench.submission import CompiledNaturalLanguageRequest
from codex_workbench.executors import ExecutionRequest, FixtureExecutor
from codex_workbench.planner import PlannerError, archify_internal_directive
from codex_workbench.store import CommandConflictError, WorkbenchStore


def verified(nodes: list[NodeSpec], task_id: str) -> list[NodeSpec]:
    if any(node.verifier for node in nodes):
        return nodes
    return [*nodes, NodeSpec(
        "verify", task_id, "verify", "fixture", "fixture", "accepted",
        depends_on=tuple(node.node_id for node in nodes), verifier=True,
    )]


def compatible_provenance() -> dict[str, object]:
    return {
        "source": COMPATIBLE_SOURCE,
        "producer": PRODUCER,
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "claude_version": SUPPORTED_USAGE_VERSION,
    }


class ServiceTests(unittest.TestCase):
    def test_evidence_fingerprint_binds_repository_base_and_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            (repository / "checked.txt").write_text("stable\n")
            subprocess.run(["git", "add", "checked.txt"], cwd=repository, check=True)
            subprocess.run(
                ["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "base"],
                cwd=repository,
                check=True,
            )
            contract = {
                "repository": str(repository),
                "base_sha": "base-sha",
                "objective": "verify the declared input",
                "allowed_scope": ("checked.txt",),
                "forbidden_scope": (),
                "required_artifacts": ("test-log", "verdict"),
                "acceptance_commands": ("python -m unittest",),
                "verification_tier": "L3",
                "governance_profile": "code-as-harness/v1",
            }
            spec = {
                "executor": "deterministic",
                "verifier": True,
                "read_scopes": ("checked.txt",),
                "write_scopes": (),
                "model": "fixture",
            }
            baseline = evidence_fingerprint(contract, spec, repository)

            for field, value in (
                ("repository", "/different/repository"),
                ("base_sha", "different-base-sha"),
                ("required_artifacts", ("test-log", "verdict", "manifest")),
            ):
                changed = {**contract, field: value}
                self.assertNotEqual(
                    baseline,
                    evidence_fingerprint(changed, spec, repository),
                    field,
                )

    def test_runtime_rechecks_contract_before_executing_a_persisted_claude_node(self) -> None:
        contract = TaskContract(
            task_id="runtime-policy",
            repository="/tmp/runtime-policy",
            base_sha="base",
            objective="Claude is explicitly disabled",
            allowed_scope=("README.md",),
            required_artifacts=(),
            task_type="implementation",
            complexity="low",
            claude_allowed=False,
        )
        quota = QuotaSnapshot(
            observed_at=now_iso(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=80,
            weekly_all_remaining=80,
            weekly_sonnet_remaining=80,
            **compatible_provenance(),
        )

        decision = Coordinator._claude_decision(
            {"executor": "claude", "model": "sonnet"},
            contract.to_dict(),
            quota,
        )

        assert decision is not None
        self.assertEqual(decision.action, "codex")
        self.assertIn("does not admit Claude", decision.reason)

    def test_runtime_routes_high_architecture_node_using_node_metadata(self) -> None:
        contract = TaskContract(
            task_id="node-routing-architecture",
            repository="/tmp/node-routing-architecture",
            base_sha="base",
            objective="ordinary implementation task",
            allowed_scope=("README.md",),
            task_type="implementation",
            complexity="standard",
        )
        quota = QuotaSnapshot(
            observed_at=now_iso(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=80,
            weekly_all_remaining=80,
            weekly_sonnet_remaining=80,
            **compatible_provenance(),
        )
        decision = Coordinator._claude_decision(
            {
                "executor": "claude",
                "model": "opus",
                "routing_strategy": "model-routing-v2",
                "task_type": "architecture",
                "complexity": "high",
                "parallelizable": True,
                "claude_allowed": True,
            },
            contract.to_dict(),
            quota,
        )

        assert decision is not None
        self.assertEqual(decision.action, "claude")

    def test_runtime_honors_node_claude_allowed_false(self) -> None:
        contract = TaskContract(
            task_id="node-routing-no-claude",
            repository="/tmp/node-routing-no-claude",
            base_sha="base",
            objective="architecture review",
            allowed_scope=("README.md",),
            task_type="architecture",
            complexity="high",
            claude_allowed=True,
        )
        quota = QuotaSnapshot(
            observed_at=now_iso(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=80,
            weekly_all_remaining=80,
            weekly_sonnet_remaining=80,
            **compatible_provenance(),
        )
        decision = Coordinator._claude_decision(
            {
                "executor": "claude",
                "model": "opus",
                "routing_strategy": "model-routing-v2",
                "task_type": "architecture",
                "complexity": "high",
                "parallelizable": True,
                "claude_allowed": False,
            },
            contract.to_dict(),
            quota,
        )

        assert decision is not None
        self.assertEqual(decision.action, "codex")
        self.assertIn("does not admit Claude", decision.reason)

    def test_codex_fallback_uses_high_architecture_node_tier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            coordinator = Coordinator(store, root, coordinator_epoch=7)
            request = ExecutionRequest(
                task_id="node-fallback",
                node_id="architecture",
                attempt=1,
                contract=TaskContract(
                    task_id="node-fallback",
                    repository=str(root),
                    base_sha="base",
                    objective="ordinary implementation",
                    allowed_scope=("README.md",),
                    task_type="implementation",
                    complexity="standard",
                ).to_dict(),
                spec={
                    "executor": "claude",
                    "model": "opus",
                    "routing_strategy": "model-routing-v2",
                    "task_type": "architecture",
                    "complexity": "high",
                    "parallelizable": True,
                    "claude_allowed": True,
                },
                worktree=root,
            )
            claimed = {
                "task_id": "node-fallback",
                "node_id": "architecture",
                "attempt": 1,
                "coordinator_epoch": 7,
                "lease_epoch": 1,
            }
            codex = MagicMock()
            codex.execute.return_value = NodeResult(
                status="succeeded",
                summary="fallback complete",
                actual_model="gpt-5.6-terra",
                checks=("focused-check",),
            )
            with patch.object(coordinator, "_executor", return_value=codex):
                routed, _ = coordinator._execute_codex_fallback(
                    claimed,
                    request,
                    "Claude unavailable",
                    "unknown",
                    fallback_kind="test",
                )

            self.assertEqual(routed.spec["model"], "gpt-5.6-terra")
            route_payload = store.record_node_route.call_args.kwargs["payload"]
            self.assertEqual(route_payload["model"], "gpt-5.6-terra")

    def test_archify_receipt_packets_require_host_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            coordinator = Coordinator(store, root, coordinator_epoch=7)
            receipt_ref = coordinator.artifacts.put_text(
                json.dumps({"command": "validate"}),
                "archify-receipt.json",
            )
            store.get_task.return_value = {
                "nodes": [
                    {
                        "node_id": "worker",
                        "state": "accepted",
                        "worktree": str(root),
                        "read_scopes": ["src"],
                        "write_scopes": [],
                        "archify": archify_internal_directive("architecture", True),
                        "result": {"artifacts": {"archify-receipt": receipt_ref}},
                    }
                ]
            }

            with self.assertRaisesRegex(ValueError, "lacks validated receipt evidence"):
                coordinator._archify_receipt_packets("archify-validate")

    def test_worker_future_exception_exits_process_and_persists_failed_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = """
from concurrent.futures import Future
from pathlib import Path
import sys

from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore

root = Path(sys.argv[1])
store = WorkbenchStore(root / "state.sqlite")
store.initialize()
epoch = store.activate_coordinator("fatal-worker", "test-machine")
store.record_system_event("coordinator.started", {"instance_id": "fatal-worker"})
coordinator = Coordinator(store, root, coordinator_epoch=epoch)
future = Future()
future.set_exception(RuntimeError("subprocess worker exploded"))
coordinator._futures[future] = ("task/node", None)
coordinator._collect()
raise AssertionError("fatal coordinator failure returned")
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            result = subprocess.run(
                [sys.executable, "-c", script, str(root)],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 70, result.stderr)
            health = WorkbenchStore(root / "state.sqlite").health()
            self.assertFalse(health["ok"])
            self.assertIn("subprocess worker exploded", health["coordinator_failure"]["error"])

    def test_worker_future_exception_is_persisted_and_fails_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("future-failure", "test-machine")
            store.record_system_event("coordinator.started", {"instance_id": "future-failure"})
            fatal_exit_codes: list[int] = []
            coordinator = Coordinator(
                store,
                root,
                coordinator_epoch=epoch,
                fatal_exit=fatal_exit_codes.append,
            )
            future: Future[None] = Future()
            future.set_exception(RuntimeError("fixture worker exploded"))
            coordinator._futures[future] = ("task/node", None)
            coordinator._collect()
            coordinator._pool.shutdown(wait=True)

            self.assertEqual(fatal_exit_codes, [70])
            health = store.health()
            self.assertFalse(health["ok"])
            self.assertIn("fixture worker exploded", health["coordinator_failure"]["error"])
            self.assertIn(
                "coordinator.failed",
                {event["event_type"] for event in store.read_events()},
            )
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            server = WorkbenchHTTPServer(config, store)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                port = server.server_address[1]
                with self.assertRaises(HTTPError) as caught:
                    urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                self.assertEqual(caught.exception.code, 503)
                payload = json.load(caught.exception)
                self.assertIn("fixture worker exploded", payload["coordinator_failure"]["error"])
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

    def test_normal_stop_does_not_request_fatal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("normal-stop", "test-machine")
            fatal_exit_codes: list[int] = []
            coordinator = Coordinator(
                store,
                root,
                coordinator_epoch=epoch,
                poll_seconds=0.01,
                fatal_exit=fatal_exit_codes.append,
            )
            thread = threading.Thread(target=coordinator.run_forever)
            thread.start()
            coordinator.stop()
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(fatal_exit_codes, [])

    @staticmethod
    def _planning_claim(
        command_id: str = "planning-command",
        task_id: str = "planning-task",
    ) -> dict[str, object]:
        return {
            "command_id": command_id,
            "task_id": task_id,
            "attempt": 1,
            "coordinator_epoch": 7,
            "state": "running",
            "request": {
                "request_schema": "natural-language-planning-v1",
                "objective": "compile the bounded task",
                "repository": "/tmp/planning-repository",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "task_id": task_id,
                "command_id": command_id,
                "planner_model": "fixture",
                "executor_model": "fixture",
                "verifier_model": "fixture",
                "timeout_seconds": 60,
                "retry_limit": 0,
                "external_write_permission": False,
                "queue": True,
                "base_sha": "fixture-base",
                "routing_strategy": "model-routing-v2",
                "task_type": "implementation",
                "complexity": "standard",
                "parallelizable": True,
                "claude_allowed": False,
                "task_points": 1.0,
                "verification_tier": "L2",
                "strategy": {
                    "version": "model-routing-v2",
                    "task_type": "implementation",
                    "complexity": "standard",
                    "parallelizable": True,
                    "claude_allowed": False,
                },
                "source_thread_id": None,
                "context_bundle_ref": None,
            },
        }

    @staticmethod
    def _compiled_planning_request(
        command_id: str = "planning-command",
        task_id: str = "planning-task",
        *,
        source_thread_id: str | None = None,
        context_bundle_ref: str | None = None,
    ) -> CompiledNaturalLanguageRequest:
        contract = TaskContract(
            task_id=task_id,
            repository="/tmp/planning-repository",
            base_sha="fixture-base",
            objective="compile the bounded task",
            allowed_scope=("src",),
            planner_model="fixture",
            executor_model="fixture",
            verifier_model="fixture",
            retry_limit=0,
            claude_allowed=False,
            source_thread_id=source_thread_id,
            context_bundle_ref=context_bundle_ref,
        )
        nodes = (
            NodeSpec(
                "verify",
                task_id,
                "verify compiled task",
                "fixture",
                "fixture",
                "fixture verifier",
                verifier=True,
            ),
        )
        return CompiledNaturalLanguageRequest(
            contract=contract,
            nodes=nodes,
            command_id=command_id,
            result={
                "ok": True,
                "task_id": task_id,
                "command_id": command_id,
                "base_sha": "fixture-base",
                "node_count": len(nodes),
            },
        )

    def test_planning_request_completes_in_shared_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("planning-success", "test-machine")
            store.enqueue_planning_request(
                "planning-command",
                "planning-task",
                self._planning_claim()["request"],
            )
            coordinator = Coordinator(store, root, coordinator_epoch=epoch, max_workers=2)
            compiled = self._compiled_planning_request()
            try:
                with (
                    patch(
                        "codex_workbench.service.compile_natural_language_request",
                        return_value=compiled,
                    ) as compile_request,
                    patch.object(
                        store,
                        "materialize_planning_request",
                        wraps=store.materialize_planning_request,
                    ) as materialize,
                ):
                    self.assertTrue(coordinator._dispatch_one_planning_request())
                    future = next(iter(coordinator._futures))
                    future.result(timeout=2)
                    coordinator._collect()

                compile_request.assert_called_once()
                self.assertEqual(compile_request.call_args.args, (coordinator.config, store))
                self.assertEqual(
                    compile_request.call_args.kwargs["objective"],
                    "compile the bounded task",
                )
                self.assertIsNone(compile_request.call_args.kwargs["context_excerpt"])
                materialize.assert_called_once_with(
                    "planning-command",
                    attempt=1,
                    coordinator_epoch=epoch,
                    contract=compiled.contract,
                    nodes=list(compiled.nodes),
                    result=compiled.result,
                    queue=True,
                    source_thread_id=None,
                )
                receipt = store.get_planning_request("planning-command")
                self.assertEqual(receipt["state"], "succeeded")
                self.assertEqual(receipt["result"], compiled.result)
                self.assertEqual(receipt["attempt"], 1)
                self.assertEqual(receipt["coordinator_epoch"], epoch)
                self.assertEqual(store.get_task("planning-task")["state"], "queued")
                self.assertFalse(coordinator._futures)
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_planning_error_is_durable_and_does_not_fail_the_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("planning-failure", "test-machine")
            store.enqueue_planning_request(
                "planning-command",
                "planning-task",
                self._planning_claim()["request"],
            )
            fatal_exit_codes: list[int] = []
            coordinator = Coordinator(
                store,
                root,
                coordinator_epoch=epoch,
                fatal_exit=fatal_exit_codes.append,
            )
            try:
                with patch(
                    "codex_workbench.service.compile_natural_language_request",
                    side_effect=PlannerError("planner rejected the request"),
                ):
                    self.assertTrue(coordinator._dispatch_one_planning_request())
                    future = next(iter(coordinator._futures))
                    future.result(timeout=2)
                    coordinator._collect()

                receipt = store.get_planning_request("planning-command")
                self.assertEqual(receipt["state"], "failed")
                self.assertIn("PlannerError: planner rejected the request", receipt["error"])
                self.assertEqual(fatal_exit_codes, [])
                self.assertFalse(coordinator._stop.is_set())
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_planning_compile_conflict_is_durable_and_not_replanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("planning-conflict", "test-machine")
            store.enqueue_planning_request(
                "planning-command",
                "planning-task",
                self._planning_claim()["request"],
            )
            coordinator = Coordinator(store, root, coordinator_epoch=epoch)
            try:
                with patch(
                    "codex_workbench.service.compile_natural_language_request",
                    side_effect=CommandConflictError("command already owns another task"),
                ) as compile_request:
                    self.assertTrue(coordinator._dispatch_one_planning_request())
                    future = next(iter(coordinator._futures))
                    future.result(timeout=2)
                    coordinator._collect()

                receipt = store.get_planning_request("planning-command")
                self.assertEqual(receipt["state"], "failed")
                self.assertIn("command already owns another task", receipt["error"])
                self.assertEqual(compile_request.call_count, 1)
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_fenced_planning_completion_is_ignored_without_a_second_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("planning-fence-old", "test-machine")
            store.enqueue_planning_request(
                "planning-command",
                "planning-task",
                self._planning_claim()["request"],
            )
            coordinator = Coordinator(store, root, coordinator_epoch=epoch)
            started = threading.Event()
            release = threading.Event()

            def block_planner(
                *_args: object,
                **_kwargs: object,
            ) -> CompiledNaturalLanguageRequest:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return self._compiled_planning_request()

            try:
                with patch(
                    "codex_workbench.service.compile_natural_language_request",
                    side_effect=block_planner,
                ):
                    self.assertTrue(coordinator._dispatch_one_planning_request())
                    self.assertTrue(started.wait(timeout=2))
                    store.activate_coordinator("planning-fence-new", "test-machine")
                    release.set()
                    future = next(iter(coordinator._futures))
                    future.result(timeout=2)
                    coordinator._collect()

                receipt = store.get_planning_request("planning-command")
                self.assertEqual(receipt["state"], "running")
                self.assertEqual(receipt["attempt"], 1)
                self.assertEqual(receipt["coordinator_epoch"], epoch)
                self.assertFalse(coordinator._futures)
                with self.assertRaises(KeyError):
                    store.get_task("planning-task")
            finally:
                release.set()
                coordinator._pool.shutdown(wait=True)

    def test_only_one_planning_attempt_is_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            coordinator = Coordinator(store, root, coordinator_epoch=7, max_workers=2)
            store.claim_planning_request.return_value = self._planning_claim()
            started = threading.Event()
            release = threading.Event()

            def block_planner(
                *_args: object,
                **_kwargs: object,
            ) -> CompiledNaturalLanguageRequest:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return self._compiled_planning_request()

            try:
                with patch(
                    "codex_workbench.service.compile_natural_language_request",
                    side_effect=block_planner,
                ):
                    self.assertTrue(coordinator._dispatch_one_planning_request())
                    self.assertTrue(started.wait(timeout=2))
                    self.assertFalse(coordinator._dispatch_one_planning_request())
                    self.assertEqual(store.claim_planning_request.call_count, 1)
                    self.assertEqual(len(coordinator._futures), 1)
                    release.set()
                    future = next(iter(coordinator._futures))
                    future.result(timeout=2)
                    coordinator._collect()
            finally:
                release.set()
                coordinator._pool.shutdown(wait=True)

    def test_planning_reads_exact_frozen_context_before_compiling(self) -> None:
        """A changed active binding cannot replace the claimed context reference."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            source_thread_id = "thread-fixture"
            context_ref = "sha256:" + "a" * 64
            claim = self._planning_claim()
            request = claim["request"]
            assert isinstance(request, dict)
            request["source_thread_id"] = source_thread_id
            request["context_bundle_ref"] = context_ref
            store.claim_planning_request.return_value = claim
            store.get_session_context.return_value = {
                "source_thread_id": source_thread_id,
                "context_ref": context_ref,
                "context_excerpt": "frozen history, not the current binding",
            }
            compiled = self._compiled_planning_request(
                source_thread_id=source_thread_id,
                context_bundle_ref=context_ref,
            )
            coordinator = Coordinator(store, root, coordinator_epoch=7)
            try:
                with patch(
                    "codex_workbench.service.compile_natural_language_request",
                    return_value=compiled,
                ) as compile_request:
                    self.assertTrue(coordinator._dispatch_one_planning_request())
                    future = next(iter(coordinator._futures))
                    future.result(timeout=2)
                    coordinator._collect()

                store.get_session_context.assert_called_once_with(source_thread_id, context_ref)
                self.assertEqual(
                    compile_request.call_args.kwargs["context_excerpt"],
                    "frozen history, not the current binding",
                )
                self.assertNotIn("context_excerpt", request)
                self.assertEqual(
                    store.materialize_planning_request.call_args.kwargs["source_thread_id"],
                    source_thread_id,
                )
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_malformed_planning_claim_with_complete_fence_is_durably_failed(self) -> None:
        """A corrupt request must not wait for a restart when it can be fenced."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            claim = self._planning_claim()
            claim["request"] = None
            store.claim_planning_request.return_value = claim
            coordinator = Coordinator(store, root, coordinator_epoch=7)
            try:
                self.assertTrue(coordinator._dispatch_one_planning_request())
                future = next(iter(coordinator._futures))
                future.result(timeout=2)
                coordinator._collect()

                store.fail_planning_request.assert_called_once()
                self.assertEqual(
                    store.fail_planning_request.call_args.args[0],
                    "planning-command",
                )
                self.assertEqual(
                    store.fail_planning_request.call_args.kwargs["attempt"],
                    1,
                )
                self.assertEqual(
                    store.fail_planning_request.call_args.kwargs["coordinator_epoch"],
                    7,
                )
                store.materialize_planning_request.assert_not_called()
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_claim_without_complete_fence_is_left_for_startup_indeterminate_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            store.claim_planning_request.return_value = {
                "command_id": "planning-command",
                "attempt": 1,
                "coordinator_epoch": "stale",
            }
            coordinator = Coordinator(store, root, coordinator_epoch=7)
            try:
                self.assertFalse(coordinator._dispatch_one_planning_request())
                store.fail_planning_request.assert_not_called()
                store.record_system_event.assert_called_once()
                self.assertEqual(
                    store.record_system_event.call_args.args[0],
                    "planning.claim_invalid",
                )
                self.assertFalse(coordinator._futures)
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_recover_marks_interrupted_planning_before_node_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("planning-recovery", "test-machine")
            store.enqueue_planning_request(
                "planning-command",
                "planning-task",
                self._planning_claim()["request"],
            )
            claimed = store.claim_planning_request(epoch)
            assert claimed is not None
            coordinator = Coordinator(store, root, coordinator_epoch=epoch)
            try:
                self.assertEqual(coordinator.recover(), 1)
                receipt = store.get_planning_request("planning-command")
                self.assertEqual(receipt["state"], "indeterminate")
                self.assertEqual(receipt["attempt"], claimed["attempt"])
                self.assertIn("explicit resolution required", receipt["error"])
            finally:
                coordinator._pool.shutdown(wait=True)

    def test_planning_uses_one_slot_while_another_slot_executes_a_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MagicMock()
            store.latest_quota.return_value = None
            planning_started = threading.Event()
            release_planning = threading.Event()
            node_started = threading.Event()
            coordinator = Coordinator(
                store,
                root,
                coordinator_epoch=7,
                max_workers=2,
                poll_seconds=0.01,
            )
            planning_claim = self._planning_claim()
            node_claim = {
                "task_id": "independent-task",
                "node_id": "worker",
                "spec": {"executor": "fixture", "model": "fixture"},
                "contract": {},
            }

            def block_planner(
                *_args: object,
                **_kwargs: object,
            ) -> CompiledNaturalLanguageRequest:
                planning_started.set()
                self.assertTrue(release_planning.wait(timeout=2))
                return self._compiled_planning_request()

            def execute_node(*_args: object, **_kwargs: object) -> None:
                node_started.set()

            try:
                with (
                    patch(
                        "codex_workbench.service.compile_natural_language_request",
                        side_effect=block_planner,
                    ),
                    patch.object(
                        coordinator,
                        "_claim_next_ready_node",
                        side_effect=(node_claim, None),
                    ),
                    patch.object(coordinator, "_claim_time_decision", return_value=None),
                    patch.object(coordinator, "_execute_claimed", side_effect=execute_node),
                    patch.object(coordinator._recovery_thread, "start"),
                    patch.object(coordinator._recovery_thread, "join"),
                ):
                    store.claim_planning_request.return_value = planning_claim
                    thread = threading.Thread(target=coordinator.run_forever)
                    thread.start()
                    self.assertTrue(planning_started.wait(timeout=2))
                    self.assertTrue(node_started.wait(timeout=2))
                    coordinator.stop()
                    release_planning.set()
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
            finally:
                release_planning.set()
                coordinator._pool.shutdown(wait=True)

    @staticmethod
    def run_until_terminal(store: WorkbenchStore, state: Path, task_id: str) -> dict:
        epoch = store.activate_coordinator(f"run-{task_id}", "test-machine")
        coordinator = Coordinator(
            store, state, coordinator_epoch=epoch, max_workers=1, poll_seconds=0.01
        )
        thread = threading.Thread(target=coordinator.run_forever)
        thread.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and store.get_task(task_id)["state"] not in {
            "accepted",
            "blocked",
            "needs_fix",
            "needs_approval",
        }:
            time.sleep(0.02)
        coordinator.stop()
        thread.join(timeout=3)
        return store.get_task(task_id)

    def test_fixture_dag_reaches_independent_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("fixture-dag", "test-machine")
            contract = TaskContract(
                task_id="e2e",
                repository=str(root),
                base_sha="fixture",
                objective="parallel fixture",
                allowed_scope=("tests",),
            )
            nodes = [
                NodeSpec("a", "e2e", "A", "fixture", "fixture", "A", write_scopes=("tests/a",)),
                NodeSpec("b", "e2e", "B", "fixture", "fixture", "B", write_scopes=("tests/b",)),
                NodeSpec("v", "e2e", "V", "fixture", "fixture", "accepted", depends_on=("a", "b"), verifier=True),
            ]
            store.create_task(contract, nodes, "e2e-create")
            store.queue_task("e2e")
            coordinator = Coordinator(
                store, root, coordinator_epoch=epoch, max_workers=2, poll_seconds=0.01
            )
            thread = threading.Thread(target=coordinator.run_forever)
            thread.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and store.get_task("e2e")["state"] != "accepted":
                time.sleep(0.01)
            coordinator.stop()
            thread.join(timeout=2)
            task = store.get_task("e2e")
            self.assertEqual(task["state"], "accepted")
            self.assertEqual({node["state"] for node in task["nodes"]}, {"accepted"})
            events = store.read_events(task_id="e2e")
            cursors = [event["cursor"] for event in events]
            self.assertEqual(cursors, sorted(cursors))
            self.assertIn("task.state_changed", {event["event_type"] for event in events})

    def test_readiness_failure_blocks_before_fixture_executor_and_persists_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # This is deliberately a pnpm-shaped target without a linker. The
            # readiness checker must report the local environment fault and
            # never attempt an install or a fixture/model execution.
            (root / "package.json").write_text(json.dumps({"packageManager": "pnpm@11.25.0"}))
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("readiness-block", "test-machine")
            contract = TaskContract(
                task_id="readiness-block",
                repository=str(root),
                base_sha="fixture",
                objective="block unavailable local dependencies before execution",
                allowed_scope=("package.json",),
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "work",
                "fixture",
                "fixture",
                "would execute only when ready",
            )
            store.create_task(contract, verified([node], contract.task_id), "readiness-block-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("readiness-worker", epoch)
            assert claimed is not None
            coordinator = Coordinator(store, root, coordinator_epoch=epoch)
            executor = MagicMock()
            try:
                with patch.object(coordinator, "_executor", return_value=executor):
                    coordinator._execute_claimed(claimed)
            finally:
                coordinator._pool.shutdown(wait=True)

            executor.execute.assert_not_called()
            task = store.get_task(contract.task_id)
            work = next(item for item in task["nodes"] if item["node_id"] == "work")
            self.assertEqual((task["state"], work["state"]), ("blocked", "blocked"))
            result = work["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["changed_paths"], [])
            attribution = result["execution_attribution"]
            self.assertEqual(attribution["failure"]["origin"], "environment")
            self.assertEqual(attribution["conditions"]["environment_readiness"]["status"], "observed")
            self.assertEqual(attribution["observed_model"]["status"], "unknown")
            report = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["execution-readiness"]).read_text()
            )
            self.assertFalse(report["ready"])
            self.assertTrue(report["failures"])

    def test_readiness_resolves_a_package_from_the_allocated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            package = repository / "src" / "local_package"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE = 'target-worktree'\n")
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            subprocess.run(["git", "add", "src/local_package/__init__.py"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("readiness-source", "test-machine")
            contract = TaskContract(
                task_id="readiness-source",
                repository=str(repository),
                base_sha=base_sha,
                objective="resolve source from the allocated worktree",
                allowed_scope=("src",),
                required_artifacts=(),
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "work",
                "codex",
                "gpt-5.6-luna",
                "inspect only the local package",
                read_scopes=("src",),
            )
            store.create_task(contract, verified([node], contract.task_id), "readiness-source-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("readiness-source-worker", epoch)
            assert claimed is not None
            coordinator = Coordinator(store, state, coordinator_epoch=epoch)
            executor = MagicMock()
            executor.execute.return_value = NodeResult(
                status="succeeded",
                summary="local source inspected",
                provider="codex",
                result_kind="worker",
                checks=("focused-check",),
            )
            try:
                with patch.object(coordinator, "_executor", return_value=executor):
                    coordinator._execute_claimed(claimed)
            finally:
                coordinator._pool.shutdown(wait=True)

            executor.execute.assert_called_once()
            work = next(item for item in store.get_task(contract.task_id)["nodes"] if item["node_id"] == "work")
            result = work["result"]
            assert isinstance(result, dict)
            report = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["execution-readiness"]).read_text()
            )
            source_check = next(check for check in report["checks"] if check["id"] == "source:local_package")
            self.assertEqual(source_check["status"], "passed")
            self.assertEqual(
                source_check["detail"]["origin"],
                str((Path(work["worktree"]) / "src" / "local_package" / "__init__.py").resolve()),
            )
            attribution = result["execution_attribution"]
            self.assertEqual(attribution["requested_model"]["model_id"], "gpt-5.6-luna")
            self.assertEqual(attribution["observed_model"]["status"], "unknown")

    def test_fresh_pnpm_worktree_materializes_before_readiness_and_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            (repository / "package.json").write_text(
                json.dumps({"packageManager": "pnpm@11.25.0"}), encoding="utf-8"
            )
            (repository / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()
            shim = root / "pnpm"
            shim.write_text("#!/bin/sh\nprintf '%s\\n' '11.25.0'\n", encoding="utf-8")
            shim.chmod(0o755)
            materializer_calls: list[tuple[str, ...]] = []

            def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                materializer_calls.append(tuple(args))
                if args[-1] == "--version":
                    return subprocess.CompletedProcess(args, 0, "11.25.0\n", "")
                cwd = kwargs["cwd"]
                assert isinstance(cwd, Path)
                node_modules = cwd / "node_modules"
                node_modules.mkdir()
                (node_modules / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
                (node_modules / ".bin").mkdir()
                return subprocess.CompletedProcess(args, 0, "offline fixture ok\n", "")

            materializer = PnpmOfflineMaterializer(binary=sys.executable, runner=runner)
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("fresh-pnpm", "test-machine")
            contract = TaskContract(
                task_id="fresh-pnpm",
                repository=str(repository),
                base_sha=base_sha,
                objective="materialize a fresh worker linker before execution",
                allowed_scope=("package.json", "pnpm-lock.yaml"),
                required_artifacts=(),
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "fresh pnpm worker",
                "codex",
                "gpt-5.6-luna",
                "inspect the prepared worktree",
                read_scopes=("package.json", "pnpm-lock.yaml"),
            )
            store.create_task(contract, verified([node], contract.task_id), "fresh-pnpm-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("fresh-pnpm-worker", epoch)
            assert claimed is not None
            executor = MagicMock()
            execution_worktrees: list[Path] = []
            executor.execute.return_value = NodeResult(
                status="succeeded",
                summary="prepared worktree inspected",
                provider="codex",
                result_kind="worker",
                checks=("fixture-check",),
            )
            coordinator = Coordinator(
                store,
                state,
                coordinator_epoch=epoch,
                pnpm_materializer=materializer,
            )
            try:
                with (
                    patch.dict(os.environ, {"CODEX_WORKBENCH_PNPM": str(shim)}),
                    patch.object(coordinator, "_executor", return_value=executor),
                ):
                    def assert_prepared(request: ExecutionRequest) -> NodeResult:
                        assert request.worktree is not None
                        execution_worktrees.append(request.worktree)
                        self.assertTrue((request.worktree / "node_modules" / ".modules.yaml").is_file())
                        return executor.execute.return_value

                    executor.execute.side_effect = assert_prepared
                    coordinator._execute_claimed(claimed)
            finally:
                coordinator._pool.shutdown(wait=True)

            executor.execute.assert_called_once()
            self.assertEqual(len(materializer_calls), 2)
            self.assertEqual(materializer_calls[0][-1], "--version")
            self.assertEqual(materializer_calls[1][1], "install")
            self.assertEqual(len(execution_worktrees), 1)
            work = next(
                item for item in store.get_task(contract.task_id)["nodes"] if item["node_id"] == "work"
            )
            result = work["result"]
            assert isinstance(result, dict)
            receipt = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["dependency-materialization"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["kind"], "pnpm-offline-materialization")
            report = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["execution-readiness"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(report["ready"])

    def test_non_node_worktree_records_non_applicable_materialization_without_pnpm_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            (repository / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()
            calls: list[tuple[str, ...]] = []

            def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(tuple(args))
                raise AssertionError("non-Node worktrees must not probe pnpm")

            materializer = PnpmOfflineMaterializer(binary=sys.executable, runner=runner)
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("non-node", "test-machine")
            contract = TaskContract(
                task_id="non-node",
                repository=str(repository),
                base_sha=base_sha,
                objective="run a non-Node worker",
                allowed_scope=("README.md",),
                required_artifacts=(),
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "non-Node worker",
                "codex",
                "gpt-5.6-luna",
                "inspect README",
                read_scopes=("README.md",),
            )
            store.create_task(contract, verified([node], contract.task_id), "non-node-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("non-node-worker", epoch)
            assert claimed is not None
            executor = MagicMock()
            executor.execute.return_value = NodeResult(
                status="succeeded",
                summary="non-Node work completed",
                provider="codex",
                result_kind="worker",
                checks=("fixture-check",),
            )
            coordinator = Coordinator(
                store,
                state,
                coordinator_epoch=epoch,
                pnpm_materializer=materializer,
            )
            try:
                with patch.object(coordinator, "_executor", return_value=executor):
                    coordinator._execute_claimed(claimed)
            finally:
                coordinator._pool.shutdown(wait=True)

            executor.execute.assert_called_once()
            self.assertEqual(calls, [])
            work = next(
                item for item in store.get_task(contract.task_id)["nodes"] if item["node_id"] == "work"
            )
            result = work["result"]
            assert isinstance(result, dict)
            receipt = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["dependency-materialization"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["kind"], "not-applicable")

    def test_pnpm_materialization_failure_blocks_before_executor_with_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            (repository / "package.json").write_text(
                json.dumps({"packageManager": "pnpm@11.25.0"}), encoding="utf-8"
            )
            (repository / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()

            def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[-1] == "--version":
                    return subprocess.CompletedProcess(args, 0, "11.25.0\n", "")
                return subprocess.CompletedProcess(args, 1, "", "offline store miss\n")

            materializer = PnpmOfflineMaterializer(binary=sys.executable, runner=runner)
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("pnpm-failure", "test-machine")
            contract = TaskContract(
                task_id="pnpm-failure",
                repository=str(repository),
                base_sha=base_sha,
                objective="block when offline dependency materialization fails",
                allowed_scope=("package.json", "pnpm-lock.yaml"),
                required_artifacts=(),
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "failing pnpm worker",
                "codex",
                "gpt-5.6-luna",
                "must not execute after materialization failure",
                read_scopes=("package.json", "pnpm-lock.yaml"),
            )
            store.create_task(contract, verified([node], contract.task_id), "pnpm-failure-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("pnpm-failure-worker", epoch)
            assert claimed is not None
            executor = MagicMock()
            coordinator = Coordinator(
                store,
                state,
                coordinator_epoch=epoch,
                pnpm_materializer=materializer,
            )
            try:
                with patch.object(coordinator, "_executor", return_value=executor):
                    coordinator._execute_claimed(claimed)
            finally:
                coordinator._pool.shutdown(wait=True)

            executor.execute.assert_not_called()
            task = store.get_task(contract.task_id)
            work = next(item for item in task["nodes"] if item["node_id"] == "work")
            self.assertEqual((task["state"], work["state"]), ("blocked", "blocked"))
            result = work["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["execution_attribution"]["failure"]["origin"], "environment")
            receipt = json.loads(
                coordinator.artifacts.verify(result["artifacts"]["dependency-materialization"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["status"], "blocked")
            self.assertIn("offline pnpm materialization failed", receipt["reason"])

    def test_complete_pnpm_linker_is_reused_without_second_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            template_root = worktree / "templates"
            (worktree / "package.json").write_text(
                json.dumps({"packageManager": "pnpm@11.25.0"}), encoding="utf-8"
            )
            (worktree / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n", encoding="utf-8"
            )
            install_calls = 0

            def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal install_calls
                if args[0] == "/bin/cp":
                    shutil.copytree(Path(args[-2]), Path(args[-1]), symlinks=True)
                    return subprocess.CompletedProcess(args, 0, "template fixture ok\n", "")
                if args[-1] == "--version":
                    return subprocess.CompletedProcess(args, 0, "11.25.0\n", "")
                install_calls += 1
                cwd = kwargs["cwd"]
                assert isinstance(cwd, Path)
                node_modules = cwd / "node_modules"
                node_modules.mkdir()
                (node_modules / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")
                (node_modules / ".bin").mkdir()
                return subprocess.CompletedProcess(args, 0, "offline fixture ok\n", "")

            materializer = PnpmOfflineMaterializer(
                binary=sys.executable,
                template_dir=template_root,
                runner=runner,
            )
            first = materializer.materialize(worktree, timeout_seconds=5)
            second = materializer.materialize(worktree, timeout_seconds=5)

            self.assertEqual(install_calls, 1)
            self.assertEqual(first["kind"], "pnpm-offline-materialization")
            self.assertEqual(second["template"]["state"], "reuse")

    def test_verifier_readiness_block_preserves_accepted_worker_patch_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("resume-verifier", "test-machine")
            contract = TaskContract(
                task_id="resume-verifier",
                repository=str(repository),
                base_sha=base_sha,
                objective="retain accepted worker evidence when verifier readiness blocks",
                allowed_scope=("package.json", "pnpm-lock.yaml"),
                required_artifacts=(),
            )
            worker = NodeSpec(
                "work",
                contract.task_id,
                "work",
                "codex",
                "gpt-5.6-luna",
                "create local package inputs",
                write_scopes=("package.json", "pnpm-lock.yaml"),
            )
            verifier = NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "codex",
                "gpt-5.6-sol",
                "independently verify the composed patch",
                depends_on=("work",),
                verifier=True,
            )
            store.create_task(contract, [worker, verifier], "resume-verifier-create")
            store.queue_task(contract.task_id)
            coordinator = Coordinator(store, state, coordinator_epoch=epoch)
            executions: list[str] = []

            class Executor:
                def execute(self, request: ExecutionRequest) -> NodeResult:
                    executions.append(request.node_id)
                    if request.node_id == "work":
                        assert request.worktree is not None
                        (request.worktree / "package.json").write_text(
                            json.dumps({"packageManager": "pnpm@11.25.0"})
                        )
                        (request.worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
                        return NodeResult(
                            status="succeeded",
                            summary="package inputs created",
                            provider="codex",
                            result_kind="worker",
                            checks=("worker-check",),
                        )
                    raise AssertionError("verifier executor must not run after readiness failure")

            executor = Executor()
            try:
                with patch.object(coordinator, "_executor", return_value=executor):
                    claimed_worker = store.claim_ready_node("resume-worker", epoch)
                    assert claimed_worker is not None
                    coordinator._execute_claimed(claimed_worker)
                    claimed_verifier = store.claim_ready_node("resume-verifier", epoch)
                    assert claimed_verifier is not None
                    coordinator._execute_claimed(claimed_verifier)
            finally:
                coordinator._pool.shutdown(wait=True)

            task = store.get_task(contract.task_id)
            accepted_worker = next(item for item in task["nodes"] if item["node_id"] == "work")
            blocked_verifier = next(item for item in task["nodes"] if item["node_id"] == "verify")
            self.assertEqual(executions, ["work"])
            self.assertEqual((task["state"], accepted_worker["state"], blocked_verifier["state"]), ("blocked", "accepted", "blocked"))
            self.assertIn("patch", accepted_worker["result"]["artifacts"])
            self.assertEqual(blocked_verifier["result"]["changed_paths"], [])
            self.assertEqual(
                blocked_verifier["result"]["execution_attribution"]["failure"]["origin"],
                "environment",
            )

    def test_spark_lane_is_claimed_before_higher_priority_general_work_and_general_borrows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("spark-priority", "test-machine")
            spark_contract = TaskContract(
                task_id="spark-task",
                repository=str(root),
                base_sha="fixture",
                objective="bounded Spark work",
                allowed_scope=("src",),
            )
            general_contract = TaskContract(
                task_id="general-task",
                repository=str(root),
                base_sha="fixture",
                objective="higher priority general work",
                allowed_scope=("tests",),
            )
            store.create_task(
                spark_contract,
                verified([NodeSpec("spark", "spark-task", "spark", "codex", "gpt-5.3-codex-spark", "bounded work")], "spark-task"),
                "spark-create",
            )
            store.create_task(
                general_contract,
                verified([NodeSpec("general", "general-task", "general", "fixture", "fixture", "ok")], "general-task"),
                "general-create",
            )
            store.queue_task("spark-task")
            store.queue_task("general-task")
            general = store.get_task("general-task")
            store.set_task_priority("general-task", 10, expected_revision=general["state_revision"])
            coordinator = Coordinator(
                store,
                root,
                coordinator_epoch=epoch,
                max_workers=2,
                spark_workers=1,
            )
            try:
                first = coordinator._claim_next_ready_node("worker-1")
                second = coordinator._claim_next_ready_node("worker-2")
            finally:
                coordinator._pool.shutdown(wait=True)

            assert first is not None and second is not None
            self.assertEqual((first["task_id"], first["node_id"]), ("spark-task", "spark"))
            self.assertEqual((second["task_id"], second["node_id"]), ("general-task", "general"))

    def test_general_work_uses_an_idle_global_slot_when_spark_has_no_ready_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("spark-borrow", "test-machine")
            contract = TaskContract(
                task_id="general-only",
                repository=str(root),
                base_sha="fixture",
                objective="general work",
                allowed_scope=("src",),
            )
            store.create_task(
                contract,
                verified([NodeSpec("general", "general-only", "general", "fixture", "fixture", "ok")], "general-only"),
                "general-only-create",
            )
            store.queue_task("general-only")
            coordinator = Coordinator(
                store,
                root,
                coordinator_epoch=epoch,
                max_workers=2,
                spark_workers=1,
            )
            try:
                claimed = coordinator._claim_next_ready_node("worker-1")
            finally:
                coordinator._pool.shutdown(wait=True)

            assert claimed is not None
            self.assertEqual(claimed["node_id"], "general")
            started = [
                event for event in store.read_events(task_id="general-only")
                if event["event_type"] == "node.started"
            ][-1]
            self.assertEqual(started["payload"]["execution_lane"], "general")
            self.assertEqual(started["payload"]["lane_capacity"], 2)

    def test_unavailable_claude_node_falls_back_once_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("fallback", "test-machine")
            store.write_quota(
                QuotaSnapshot(
                    observed_at=now_iso(),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=60,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    **compatible_provenance(),
                )
            )
            contract = TaskContract(
                task_id="fallback",
                repository=str(repository),
                base_sha=base_sha,
                objective="fallback without replaying Claude",
                allowed_scope=("README.md",),
                executor_model="gpt-5.6-luna",
            )
            node = NodeSpec(
                "work",
                "fallback",
                "work",
                "claude",
                "sonnet",
                "inspect the fixture",
                read_scopes=("README.md",),
            )
            store.create_task(contract, verified([node], contract.task_id), "fallback-create")
            store.queue_task("fallback")
            claimed = store.claim_ready_node("worker-1", epoch)
            coordinator = Coordinator(store, state, coordinator_epoch=epoch, max_workers=1)

            class StubExecutor:
                def __init__(self, result: NodeResult):
                    self.result = result
                    self.calls = 0

                def execute(self, _request):
                    self.calls += 1
                    return self.result

            claude = StubExecutor(NodeResult("blocked", "Claude native-subscription authentication is unavailable"))
            codex = StubExecutor(NodeResult(
                "succeeded", "Codex fallback completed", actual_model="gpt-5.6-luna",
                result_kind="worker", checks=("fixture-check",),
            ))
            with patch.object(coordinator, "_executor", side_effect=lambda kind: claude if kind == "claude" else codex):
                coordinator._execute_claimed(claimed)
            coordinator._pool.shutdown(wait=True)

            task = store.get_task("fallback")
            work = next(node for node in task["nodes"] if node["node_id"] == "work")
            self.assertEqual(work["state"], "accepted")
            self.assertEqual(work["result"]["actual_model"], "gpt-5.6-luna")
            self.assertEqual(work["effective_executor"], "codex")
            self.assertEqual(work["effective_model"], "gpt-5.6-luna")
            self.assertEqual(claude.calls, 1)
            self.assertEqual(codex.calls, 1)
            attribution = work["result"]["execution_attribution"]
            self.assertEqual(attribution["requested_model"]["model_id"], "gpt-5.6-luna")
            self.assertEqual(attribution["observed_model"]["status"], "unattested")
            self.assertIsNone(attribution["physical_call"]["usage_deduplication_key"])
            decisions = attribution["candidate_decisions"]
            self.assertEqual(
                [(decision["candidate_id"], decision["disposition"]) for decision in decisions],
                [("claude:sonnet", "selected"), ("codex:gpt-5.6-luna", "selected")],
            )
            self.assertTrue(all(
                decision["references"][0]["ref"] == "node.routed"
                for decision in decisions
            ))
            routed = [event for event in store.read_events(task_id="fallback") if event["event_type"] == "node.routed"]
            self.assertEqual(routed[0]["payload"]["reason"], "Claude native-subscription authentication is unavailable")
            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A8"]["status"], "pending")
            self.assertEqual(checks["A9"]["status"], "ok")

    def test_failed_claude_node_falls_back_once_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("failed-fallback", "test-machine")
            store.write_quota(
                QuotaSnapshot(
                    observed_at=now_iso(),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=60,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    **compatible_provenance(),
                )
            )
            contract = TaskContract(
                task_id="failed-fallback",
                repository=str(repository),
                base_sha=base_sha,
                objective="fallback after a failed Claude attempt",
                allowed_scope=("README.md",),
                executor_model="gpt-5.6-luna",
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "work",
                "claude",
                "sonnet",
                "inspect the fixture",
                read_scopes=("README.md",),
            )
            store.create_task(contract, verified([node], contract.task_id), "failed-fallback-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("worker-1", epoch)
            coordinator = Coordinator(store, state, coordinator_epoch=epoch, max_workers=1)

            class StubExecutor:
                def __init__(self, result: NodeResult):
                    self.result = result
                    self.calls = 0

                def execute(self, _request):
                    self.calls += 1
                    return self.result

            claude = StubExecutor(NodeResult("failed", "Claude execution failed"))
            codex = StubExecutor(NodeResult(
                "succeeded", "Codex fallback completed", actual_model="gpt-5.6-luna",
                result_kind="worker", checks=("fixture-check",),
            ))
            with patch.object(coordinator, "_executor", side_effect=lambda kind: claude if kind == "claude" else codex):
                coordinator._execute_claimed(claimed)
            coordinator._pool.shutdown(wait=True)

            task = store.get_task(contract.task_id)
            work = next(node for node in task["nodes"] if node["node_id"] == "work")
            self.assertEqual(work["state"], "accepted")
            self.assertEqual(work["effective_executor"], "codex")
            self.assertEqual(work["effective_model"], "gpt-5.6-luna")
            self.assertEqual(claude.calls, 1)
            self.assertEqual(codex.calls, 1)
            routed = [
                event
                for event in store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.routed"
            ]
            self.assertEqual(len(routed), 1)
            self.assertEqual(routed[0]["payload"]["reason"], "Claude execution failed")

    def test_red_quota_zone_routes_to_codex_without_starting_claude(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("red-fallback", "test-machine")
            store.write_quota(
                QuotaSnapshot(
                    observed_at=now_iso(),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=27,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    **compatible_provenance(),
                )
            )
            contract = TaskContract(
                task_id="red-fallback",
                repository=str(repository),
                base_sha=base_sha,
                objective="route before starting Claude",
                allowed_scope=("README.md",),
                required_artifacts=(),
                task_type="architecture",
                complexity="high",
            )
            node = NodeSpec(
                "work",
                contract.task_id,
                "work",
                "claude",
                "opus",
                "inspect the fixture",
                read_scopes=("README.md",),
            )
            store.create_task(contract, verified([node], contract.task_id), "red-fallback-create")
            store.queue_task(contract.task_id)
            claimed = store.claim_ready_node("worker-1", epoch)
            coordinator = Coordinator(store, state, coordinator_epoch=epoch, max_workers=1)

            class StubExecutor:
                def __init__(self, result: NodeResult):
                    self.result = result
                    self.calls = 0

                def execute(self, _request):
                    self.calls += 1
                    return self.result

            claude = StubExecutor(NodeResult("succeeded", "must not run", actual_model="sonnet"))
            codex = StubExecutor(NodeResult(
                "succeeded", "Codex completed", actual_model="gpt-5.6-terra",
                result_kind="worker", checks=("fixture-check",),
            ))
            with patch.object(coordinator, "_executor", side_effect=lambda kind: claude if kind == "claude" else codex):
                coordinator._execute_claimed(claimed)
            coordinator._pool.shutdown(wait=True)

            self.assertEqual(claude.calls, 0)
            self.assertEqual(codex.calls, 1)
            routed_node = next(
                node for node in store.get_task(contract.task_id)["nodes"]
                if node["node_id"] == "work"
            )
            self.assertEqual(routed_node["effective_executor"], "codex")
            self.assertEqual(routed_node["effective_model"], "gpt-5.6-terra")
            routed = [event for event in store.read_events(task_id=contract.task_id) if event["event_type"] == "node.routed"]
            self.assertEqual(routed[0]["payload"]["zone"], "red")

    def test_green_shared_capacity_persists_wait_then_resumes_same_claude_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("green-shared-capacity", "test-machine")
            store.write_quota(
                QuotaSnapshot(
                    observed_at=now_iso(),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=60,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    **compatible_provenance(),
                )
            )
            contract = TaskContract(
                task_id="green-shared-capacity",
                repository=str(repository),
                base_sha=base_sha,
                objective="enforce green shared Claude capacity",
                allowed_scope=("README.md",),
                required_artifacts=(),
            )
            nodes = [
                NodeSpec("a", contract.task_id, "A", "claude", "sonnet", "A", read_scopes=("README.md",)),
                NodeSpec("b", contract.task_id, "B", "claude", "sonnet", "B", read_scopes=("README.md",)),
                NodeSpec("c", contract.task_id, "C", "claude", "sonnet", "C", read_scopes=("README.md",)),
            ]
            store.create_task(contract, verified(nodes, contract.task_id), "green-shared-capacity-create")
            store.queue_task(contract.task_id)
            first_wave_started = threading.Event()
            release_first_wave = threading.Event()

            class ClaudeStub:
                def __init__(self):
                    self.calls = 0
                    self.active = 0
                    self.max_active = 0
                    self.lock = threading.Lock()

                def execute(self, _request):
                    with self.lock:
                        self.calls += 1
                        call = self.calls
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                        if self.active == 2:
                            first_wave_started.set()
                    if call <= 2:
                        release_first_wave.wait(timeout=3)
                    with self.lock:
                        self.active -= 1
                    return NodeResult(
                        "succeeded", "Sonnet completed", actual_model="sonnet",
                        result_kind="worker", checks=("fixture-check",),
                    )

            class CodexStub:
                def __init__(self):
                    self.calls = 0

                def execute(self, _request):
                    self.calls += 1
                    return NodeResult(
                        "succeeded", "Codex completed", actual_model="gpt-5.6-luna",
                        result_kind="worker", checks=("fixture-check",),
                    )

            claude = ClaudeStub()
            codex = CodexStub()
            # This fixture owns its durable green quota snapshot.  Do not let
            # an ambient service-level snapshot path replace it while testing
            # shared-capacity routing.
            with patch.dict(os.environ, {"CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE": ""}):
                coordinator = Coordinator(
                    store, state, coordinator_epoch=epoch, max_workers=3, poll_seconds=0.01
                )
            fixture = FixtureExecutor(coordinator.artifacts)
            with patch.object(
                coordinator,
                "_executor",
                side_effect=lambda kind: claude if kind == "claude" else fixture if kind == "fixture" else codex,
            ):
                thread = threading.Thread(target=coordinator.run_forever)
                thread.start()
                try:
                    self.assertTrue(first_wave_started.wait(timeout=2))
                    deadline = time.monotonic() + 2
                    deferred: list[dict] = []
                    while time.monotonic() < deadline:
                        deferred = [
                            event
                            for event in store.read_events(task_id=contract.task_id)
                            if event["event_type"] == "node.admission_deferred"
                        ]
                        if deferred:
                            break
                        time.sleep(0.02)
                    self.assertEqual(len(deferred), 1)
                    waiting = next(
                        node
                        for node in store.get_task(contract.task_id)["nodes"]
                        if node["node_id"] == "c"
                    )
                    self.assertEqual((waiting["state"], waiting["attempt"]), ("pending", 0))
                    self.assertEqual(codex.calls, 0)
                    self.assertEqual(deferred[0]["node_id"], "c")
                    self.assertEqual(deferred[0]["payload"]["reason_kind"], "temporary-capacity")
                    self.assertEqual(deferred[0]["payload"]["next_attempt"], 1)
                    self.assertIn("enough shared capacity", deferred[0]["payload"]["resume_condition"])
                    self.assertIsInstance(deferred[0]["payload"].get("quota_snapshot_id"), int)

                    release_first_wave.set()
                    deadline = time.monotonic() + 2
                    refresh_wait: dict | None = None
                    while time.monotonic() < deadline:
                        events = store.read_events(task_id=contract.task_id)
                        refresh_wait = next(
                            (
                                event
                                for event in events
                                if event["event_type"] == "node.admission_deferred"
                                and event["payload"]["reason_kind"]
                                == "quota-refresh-required"
                            ),
                            None,
                        )
                        if refresh_wait is not None:
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(refresh_wait)
                    assert refresh_wait is not None
                    self.assertEqual(refresh_wait["node_id"], "c")
                    self.assertIn(
                        "newer than the most recent Claude completion",
                        refresh_wait["payload"]["resume_condition"],
                    )

                    # Quota observations use second precision. Wait for a
                    # genuinely newer receipt instead of forging a future one.
                    time.sleep(1.05)
                    store.write_quota(
                        QuotaSnapshot(
                            observed_at=now_iso(),
                            auth_ok=True,
                            auth_method="native-subscription",
                            five_hour_remaining=60,
                            weekly_all_remaining=60,
                            weekly_sonnet_remaining=60,
                            **compatible_provenance(),
                        )
                    )
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if all(
                            node["state"] == "accepted"
                            for node in store.get_task(contract.task_id)["nodes"]
                        ):
                            break
                        time.sleep(0.02)
                finally:
                    release_first_wave.set()
                    coordinator.stop()
                    thread.join(timeout=3)

            self.assertEqual(claude.calls, 3)
            self.assertEqual(claude.max_active, 2)
            self.assertEqual(codex.calls, 0)
            self.assertTrue(all(
                node["state"] == "accepted"
                for node in store.get_task(contract.task_id)["nodes"]
            ))
            routed = [
                event for event in store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.routed"
            ]
            self.assertEqual(routed, [])
            resumed = next(
                event
                for event in store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.started" and event["node_id"] == "c"
            )
            resumed_deferred_cursor = resumed["payload"]["admission_deferred_event_cursor"]
            resumed_deferred = next(
                event
                for event in store.read_events(task_id=contract.task_id)
                if event["cursor"] == resumed_deferred_cursor
            )
            self.assertEqual(resumed_deferred["event_type"], "node.admission_deferred")
            self.assertEqual(resumed_deferred["node_id"], "c")
            self.assertEqual(
                resumed_deferred["payload"]["reason_kind"],
                "quota-refresh-required",
            )
            self.assertGreaterEqual(resumed_deferred_cursor, refresh_wait["cursor"])
            completed = next(
                node
                for node in store.get_task(contract.task_id)["nodes"]
                if node["node_id"] == "c"
            )
            self.assertEqual(
                (completed["effective_executor"], completed["effective_model"]),
                ("claude", "sonnet"),
            )

    def test_completed_claude_node_requires_a_newer_quota_snapshot_before_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("fresh-quota", "test-machine")
            stale_after_completion = QuotaSnapshot(
                observed_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=60,
                weekly_all_remaining=60,
                weekly_sonnet_remaining=60,
                **compatible_provenance(),
            )
            store.write_quota(stale_after_completion)

            first_contract = TaskContract(
                task_id="first-claude",
                repository=str(repository),
                base_sha=base_sha,
                objective="record a completed Claude turn",
                allowed_scope=("README.md",),
                required_artifacts=(),
                verifier_model="fixture",
            )
            first_worker = NodeSpec(
                "work",
                first_contract.task_id,
                "work",
                "claude",
                "sonnet",
                "inspect the fixture",
                read_scopes=("README.md",),
            )
            store.create_task(first_contract, verified([first_worker], first_contract.task_id), "first-create")
            store.queue_task(first_contract.task_id)
            first_claim = store.claim_ready_node(
                "first-worker", epoch, admissible=lambda spec: spec["node_id"] == "work"
            )
            assert first_claim is not None
            store.settle_node(
                first_contract.task_id,
                "work",
                NodeResult(
                    "succeeded",
                    "Sonnet completed",
                    actual_model="sonnet",
                    result_kind="worker",
                    checks=("fixture-check",),
                ),
                attempt=first_claim["attempt"],
                coordinator_epoch=first_claim["coordinator_epoch"],
                lease_epoch=first_claim["lease_epoch"],
            )

            second_contract = TaskContract(
                task_id="second-claude",
                repository=str(repository),
                base_sha=base_sha,
                objective="must not consume Claude before a fresh snapshot",
                allowed_scope=("README.md",),
                required_artifacts=(),
                verifier_model="fixture",
            )
            second_worker = NodeSpec(
                "work",
                second_contract.task_id,
                "work",
                "claude",
                "sonnet",
                "inspect the fixture",
                read_scopes=("README.md",),
            )
            store.create_task(second_contract, verified([second_worker], second_contract.task_id), "second-create")
            store.queue_task(second_contract.task_id)
            second_claim = store.claim_ready_node(
                "second-worker", epoch, admissible=lambda spec: spec["task_id"] == second_contract.task_id
            )
            assert second_claim is not None
            coordinator = Coordinator(store, state, coordinator_epoch=epoch, max_workers=1)

            class StubExecutor:
                def __init__(self, result: NodeResult):
                    self.result = result
                    self.calls = 0

                def execute(self, _request):
                    self.calls += 1
                    return self.result

            claude = StubExecutor(NodeResult("succeeded", "must not run", actual_model="sonnet"))
            codex = StubExecutor(
                NodeResult(
                    "succeeded",
                    "Codex completed",
                    actual_model="gpt-5.6-luna",
                    result_kind="worker",
                    checks=("fixture-check",),
                )
            )
            with patch.object(
                coordinator,
                "_executor",
                side_effect=lambda kind: claude if kind == "claude" else codex,
            ):
                coordinator._execute_claimed(second_claim)
            coordinator._pool.shutdown(wait=True)

            self.assertEqual(claude.calls, 0)
            self.assertEqual(codex.calls, 1)
            route = next(
                event for event in store.read_events(task_id=second_contract.task_id)
                if event["event_type"] == "node.routed"
            )
            self.assertEqual(route["payload"]["fallback_kind"], "quota-refresh-required")

            refreshed = QuotaSnapshot(
                observed_at=datetime.now(UTC).isoformat(),
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=60,
                weekly_all_remaining=60,
                weekly_sonnet_remaining=60,
                **compatible_provenance(),
            )
            store.write_quota(refreshed)
            self.assertEqual(
                coordinator._claim_time_decision(
                    {"executor": "claude", "model": "sonnet"},
                    second_contract.to_dict(),
                    refreshed,
                    (),
                ).action,
                "claude",
            )

    def test_parallel_worktree_patches_are_composed_for_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

            state = root / "state"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("compose", "test-machine")
            make_a = (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('tests').mkdir(); Path('tests/a.txt').write_text('A')",
            )
            make_b = (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('tests').mkdir(); Path('tests/b.txt').write_text('B')",
            )
            verify = (
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('tests/a.txt').read_text() == 'A'; assert Path('tests/b.txt').read_text() == 'B'",
            )
            contract = TaskContract(
                task_id="compose",
                repository=str(repository),
                base_sha=base_sha,
                objective="compose parallel changes",
                allowed_scope=("tests",),
                acceptance_commands=tuple(
                    shlex.join(command) for command in (make_a, make_b, verify)
                ),
                verifier_model="fixture",
            )
            nodes = [
                NodeSpec("a", "compose", "A", "deterministic", "local", command=make_a, write_scopes=("tests/a.txt",)),
                NodeSpec("b", "compose", "B", "deterministic", "local", command=make_b, write_scopes=("tests/b.txt",)),
                NodeSpec(
                    "verify",
                    "compose",
                    "verify",
                    "deterministic",
                    "fixture",
                    command=verify,
                    depends_on=("a", "b"),
                    verifier=True,
                    ordinal=2,
                ),
            ]
            store.create_task(contract, nodes, "compose-create")
            store.queue_task("compose")
            coordinator = Coordinator(
                store, state, coordinator_epoch=epoch, max_workers=2, poll_seconds=0.01
            )
            thread = threading.Thread(target=coordinator.run_forever)
            thread.start()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and store.get_task("compose")["state"] not in {
                "accepted",
                "blocked",
                "needs_fix",
                "needs_approval",
            }:
                time.sleep(0.02)
            coordinator.stop()
            thread.join(timeout=3)
            task = store.get_task("compose")
            self.assertEqual(task["state"], "accepted", task)
            verifier = next(node for node in task["nodes"] if node["node_id"] == "verify")
            verifier_worktree = Path(verifier["worktree"])
            self.assertEqual((verifier_worktree / "tests/a.txt").read_text(), "A")
            self.assertEqual((verifier_worktree / "tests/b.txt").read_text(), "B")

    def test_cached_verifier_revalidates_current_archify_packets_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            worktree = root / "worktree"
            state.mkdir()
            worktree.mkdir()
            store = MagicMock()
            coordinator = Coordinator(store, state, coordinator_epoch=7)
            packet = {"node_id": "worker-a", "role": "architecture"}
            cached = NodeResult(
                status="succeeded",
                summary="cached verifier result",
                result_kind="verifier",
                verdict="accepted",
            )
            store.cached_evidence.return_value = {"result": cached.to_dict()}
            claimed = {
                "task_id": "cache-host-gate",
                "node_id": "verify",
                "attempt": 1,
                "coordinator_epoch": 7,
                "lease_epoch": 1,
                "steering": (),
                "contract": {
                    "repository": str(worktree),
                    "base_sha": "fixture-base",
                    "allowed_scope": (),
                    "forbidden_scope": (),
                    "timeout_seconds": 3600,
                },
                "spec": {
                    "executor": "deterministic",
                    "model": "fixture",
                    "verifier": True,
                    "read_scopes": (),
                    "write_scopes": (),
                },
            }
            route = _ClaimRoute(None, (), None)
            with (
                patch.object(coordinator.worktrees, "prepare", return_value=worktree),
                patch.object(coordinator, "_compose_worker_patches", return_value=None),
                patch.object(coordinator, "_archify_receipt_packets", return_value=(packet,)),
                patch("codex_workbench.service.reusable_evidence_key", return_value="cache-key"),
                patch(
                    "codex_workbench.service.validate_archify_verifier_packets",
                    return_value=(None, ("receipt-ref", "execution-ref")),
                ) as host_gate,
            ):
                coordinator._execute_claimed(claimed, route)

            host_gate.assert_called_once()
            request = host_gate.call_args.args[0]
            self.assertEqual(request.archify_receipts, (packet,))
            store.record_evidence_reuse.assert_called_once_with(
                "cache-key", "cache-host-gate", "verify"
            )
            reused_result = store.settle_node.call_args.args[2]
            self.assertEqual(set(reused_result.evidence), {"receipt-ref", "execution-ref"})

            store.reset_mock()
            store.cached_evidence.return_value = {"result": cached.to_dict()}
            executor = MagicMock()
            executor.execute.return_value = NodeResult(
                status="succeeded",
                summary="host gate reran verifier",
                result_kind="verifier",
                verdict="accepted",
            )
            with (
                patch.object(coordinator.worktrees, "prepare", return_value=worktree),
                patch.object(coordinator, "_compose_worker_patches", return_value=None),
                patch.object(coordinator, "_archify_receipt_packets", return_value=(packet,)),
                patch.object(coordinator, "_executor", return_value=executor),
                patch("codex_workbench.service.reusable_evidence_key", return_value="cache-key"),
                patch(
                    "codex_workbench.service.validate_archify_verifier_packets",
                    return_value=("forged provenance", ()),
                ) as host_gate,
                patch("codex_workbench.service.validate_worker_scope", side_effect=lambda _, __, result: result),
            ):
                coordinator._execute_claimed(claimed, route)

            host_gate.assert_called_once()
            store.record_evidence_reuse.assert_not_called()
            executor.execute.assert_called_once()

    def test_verified_evidence_is_reused_until_its_declared_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "checked.txt").write_text("stable\n")
            (repository / "unrelated.txt").write_text("one\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)
            state = root / "state"
            counter = root / "counter.txt"
            store = WorkbenchStore(state / "state.sqlite")
            store.initialize()

            def create_and_run(task_id: str, verification_tier: str = "L3") -> dict:
                base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
                script = (
                    "from pathlib import Path; "
                    f"p=Path({str(counter)!r}); p.write_text((p.read_text() if p.exists() else '')+'run\\n'); "
                    "assert Path('checked.txt').read_text()"
                )
                command = (sys.executable, "-c", script)
                contract = TaskContract(
                    task_id=task_id,
                    repository=str(repository),
                    base_sha=base_sha,
                    objective="verify the declared input",
                    allowed_scope=("checked.txt",),
                    acceptance_commands=(shlex.join(command),),
                    required_artifacts=("test-log", "verdict"),
                    verifier_model="fixture",
                    verification_tier=verification_tier,
                )
                node = NodeSpec(
                    "verify",
                    task_id,
                    "verify",
                    "deterministic",
                    "fixture",
                    command=command,
                    read_scopes=("checked.txt",),
                    verifier=True,
                )
                store.create_task(contract, [node], f"create-{task_id}")
                store.queue_task(task_id)
                return self.run_until_terminal(store, state, task_id)

            self.assertEqual(create_and_run("cache-1")["state"], "accepted")
            (repository / "unrelated.txt").write_text("two\n")
            subprocess.run(["git", "add", "unrelated.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "unrelated"], cwd=repository, check=True, capture_output=True)
            self.assertEqual(create_and_run("cache-2")["state"], "accepted")
            self.assertEqual(counter.read_text().splitlines(), ["run", "run"])
            reused = store.read_events(task_id="cache-2")
            self.assertNotIn("node.evidence_reused", {event["event_type"] for event in reused})

            self.assertEqual(create_and_run("cache-tier-change", "L2")["state"], "accepted")
            self.assertEqual(counter.read_text().splitlines(), ["run", "run", "run"])

            (repository / "checked.txt").write_text("changed\n")
            subprocess.run(["git", "add", "checked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "checked"], cwd=repository, check=True, capture_output=True)
            self.assertEqual(create_and_run("cache-3")["state"], "accepted")
            self.assertEqual(counter.read_text().splitlines(), ["run", "run", "run", "run"])


if __name__ == "__main__":
    unittest.main()
