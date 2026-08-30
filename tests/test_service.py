from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from codex_workbench.acceptance import build_acceptance_report
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


class ServiceTests(unittest.TestCase):
    def test_worker_future_exception_is_persisted_and_fails_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / "state.sqlite")
            store.initialize()
            epoch = store.activate_coordinator("future-failure", "test-machine")
            store.record_system_event("coordinator.started", {"instance_id": "future-failure"})
            coordinator = Coordinator(store, root, coordinator_epoch=epoch)
            future: Future[None] = Future()
            future.set_exception(RuntimeError("fixture worker exploded"))
            coordinator._futures[future] = ("task/node", None)
            coordinator._collect()
            coordinator._pool.shutdown(wait=True)

            health = store.health()
            self.assertFalse(health["ok"])
            self.assertIn("fixture worker exploded", health["coordinator_failure"]["error"])
            self.assertIn(
                "coordinator.failed",
                {event["event_type"] for event in store.read_events()},
            )
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
                    source="settings-usage",
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
                    source="settings-usage",
                )
            )
            contract = TaskContract(
                task_id="red-fallback",
                repository=str(repository),
                base_sha=base_sha,
                objective="route before starting Claude",
                allowed_scope=("README.md",),
                required_artifacts=(),
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
                "succeeded", "Codex completed", actual_model="gpt-5.6-luna",
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
            self.assertEqual(routed_node["effective_model"], "gpt-5.6-luna")
            routed = [event for event in store.read_events(task_id=contract.task_id) if event["event_type"] == "node.routed"]
            self.assertEqual(routed[0]["payload"]["zone"], "red")

    def test_yellow_quota_zone_limits_sonnet_but_keeps_codex_parallel(self) -> None:
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
            epoch = store.activate_coordinator("yellow-concurrency", "test-machine")
            store.write_quota(
                QuotaSnapshot(
                    observed_at=now_iso(),
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=35,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    source="settings-usage",
                )
            )
            contract = TaskContract(
                task_id="yellow-concurrency",
                repository=str(repository),
                base_sha=base_sha,
                objective="enforce yellow-zone concurrency",
                allowed_scope=("README.md",),
                required_artifacts=(),
            )
            nodes = [
                NodeSpec("a", contract.task_id, "A", "claude", "sonnet", "A", read_scopes=("README.md",)),
                NodeSpec("b", contract.task_id, "B", "claude", "sonnet", "B", read_scopes=("README.md",)),
                NodeSpec("c", contract.task_id, "C", "codex", "gpt-5.6-luna", "C", read_scopes=("README.md",)),
            ]
            store.create_task(contract, verified(nodes, contract.task_id), "yellow-concurrency-create")
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
            self.assertEqual(claude.max_active, 1)
            self.assertEqual(codex.calls, 1)

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
