from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch
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
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract, now_iso
from codex_workbench.service import Coordinator
from codex_workbench.executors import FixtureExecutor
from codex_workbench.store import WorkbenchStore


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
            routed = [event for event in store.read_events(task_id="fallback") if event["event_type"] == "node.routed"]
            self.assertEqual(routed[0]["payload"]["reason"], "Claude native-subscription authentication is unavailable")
            checks = {check["id"]: check for check in build_acceptance_report(store)["checks"]}
            self.assertEqual(checks["A8"]["status"], "pending")
            self.assertEqual(checks["A9"]["status"], "ok")

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

    def test_green_shared_capacity_routes_overflow_to_codex_without_idling_workers(self) -> None:
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
            codex_started = threading.Event()

            class ClaudeStub:
                def __init__(self):
                    self.calls = 0
                    self.active = 0
                    self.max_active = 0
                    self.lock = threading.Lock()

                def execute(self, _request):
                    with self.lock:
                        self.calls += 1
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                    codex_started.wait(timeout=2)
                    time.sleep(0.03)
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
                    codex_started.set()
                    return NodeResult(
                        "succeeded", "Codex completed", actual_model="gpt-5.6-luna",
                        result_kind="worker", checks=("fixture-check",),
                    )

            claude = ClaudeStub()
            codex = CodexStub()
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
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if all(node["state"] == "accepted" for node in store.get_task(contract.task_id)["nodes"]):
                        break
                    time.sleep(0.02)
                coordinator.stop()
                thread.join(timeout=3)

            self.assertEqual(claude.calls, 2)
            self.assertEqual(claude.max_active, 2)
            self.assertEqual(codex.calls, 1)
            routed = [
                event for event in store.read_events(task_id=contract.task_id)
                if event["event_type"] == "node.routed"
            ]
            self.assertEqual(routed[0]["payload"]["fallback_kind"], "claude-capacity-overflow")
            self.assertEqual(routed[0]["payload"]["zone"], "green")
            overflow = next(node for node in store.get_task(contract.task_id)["nodes"] if node["node_id"] == "c")
            self.assertEqual((overflow["effective_executor"], overflow["effective_model"]), ("codex", "gpt-5.6-luna"))

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
            contract = TaskContract(
                task_id="compose",
                repository=str(repository),
                base_sha=base_sha,
                objective="compose parallel changes",
                allowed_scope=("tests",),
                verifier_model="fixture",
            )
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

            def create_and_run(task_id: str) -> dict:
                base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
                contract = TaskContract(
                    task_id=task_id,
                    repository=str(repository),
                    base_sha=base_sha,
                    objective="verify the declared input",
                    allowed_scope=("checked.txt",),
                    required_artifacts=("test-log", "verdict"),
                    verifier_model="fixture",
                )
                script = (
                    "from pathlib import Path; "
                    f"p=Path({str(counter)!r}); p.write_text((p.read_text() if p.exists() else '')+'run\\n'); "
                    "assert Path('checked.txt').read_text()"
                )
                node = NodeSpec(
                    "verify",
                    task_id,
                    "verify",
                    "deterministic",
                    "fixture",
                    command=(sys.executable, "-c", script),
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
            self.assertEqual(counter.read_text().splitlines(), ["run"])
            reused = store.read_events(task_id="cache-2")
            self.assertIn("node.evidence_reused", {event["event_type"] for event in reused})

            (repository / "checked.txt").write_text("changed\n")
            subprocess.run(["git", "add", "checked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "checked"], cwd=repository, check=True, capture_output=True)
            self.assertEqual(create_and_run("cache-3")["state"], "accepted")
            self.assertEqual(counter.read_text().splitlines(), ["run", "run"])


if __name__ == "__main__":
    unittest.main()
