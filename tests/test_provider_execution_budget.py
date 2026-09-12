from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.executors import ClaudeExecutor, CodexExecutor, ExecutionRequest
from codex_workbench.model import ClaudeDispatchDecision, NodeResult, NodeSpec, TaskContract
from codex_workbench.service import Coordinator, _ClaimRoute
from codex_workbench.store import WorkbenchStore


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ProviderExecutionBudgetTests(unittest.TestCase):
    def _claim(
        self,
        root: Path,
        *,
        task_id: str,
        executor: str = "claude",
        timeout_seconds: int = 10,
    ) -> tuple[WorkbenchStore, Coordinator, dict]:
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
        (repository / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

        state = root / "state"
        store = WorkbenchStore(state / "state.sqlite")
        store.initialize()
        epoch = store.activate_coordinator(f"{task_id}-coordinator", "fixture-machine")
        contract = TaskContract(
            task_id=task_id,
            repository=str(repository),
            base_sha=base_sha,
            objective="exercise one provider execution budget",
            allowed_scope=("README.md",),
            required_artifacts=(),
            timeout_seconds=timeout_seconds,
            retry_limit=0,
        )
        worker = NodeSpec(
            "work",
            task_id,
            "provider fixture",
            executor,  # type: ignore[arg-type]
            "sonnet" if executor == "claude" else "gpt-5.6-luna",
            "run the provider fixture",
            read_scopes=("README.md",),
        )
        verifier = NodeSpec(
            "verify",
            task_id,
            "verify provider fixture",
            "fixture",
            "fixture",
            "verify after worker",
            depends_on=("work",),
            verifier=True,
        )
        store.create_task(contract, [worker, verifier], f"{task_id}-create")
        store.queue_task(task_id)
        claimed = store.claim_ready_node(f"{task_id}-worker", epoch)
        assert claimed is not None
        return store, Coordinator(store, state, coordinator_epoch=epoch, max_workers=1), claimed

    @staticmethod
    def _request(
        root: Path,
        *,
        provider: str = "codex",
        timeout_seconds: int = 10,
        deadline: float | None = None,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            task_id="provider-budget",
            node_id="work",
            attempt=1,
            contract={
                "objective": "exercise one provider execution budget",
                "allowed_scope": ["README.md"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": timeout_seconds,
            },
            spec={
                "title": "provider fixture",
                "prompt": "run the fixture subprocess",
                "executor": provider,
                "model": "sonnet" if provider == "claude" else "gpt-5.6-luna",
                "verifier": False,
                "read_scopes": (),
                "write_scopes": (),
            },
            worktree=root,
            provider_deadline_monotonic=deadline,
        )

    def test_failed_or_blocked_first_provider_exhausts_budget_without_codex_spawn(self) -> None:
        for status in ("failed", "blocked"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task_id = f"budget-{status}"
                store, coordinator, claimed = self._claim(root, task_id=task_id)
                clock = _Clock(100.0)
                first_requests: list[ExecutionRequest] = []
                evidence_refs: list[str] = []
                codex_calls: list[ExecutionRequest] = []

                class FirstProvider:
                    def execute(self, request: ExecutionRequest) -> NodeResult:
                        first_requests.append(request)
                        fixture = subprocess.run(
                            [sys.executable, "-c", "print('first provider fixture')"],
                            check=True,
                            text=True,
                            capture_output=True,
                        )
                        evidence = coordinator.artifacts.put_text(fixture.stdout, "first-provider.txt")
                        evidence_refs.append(evidence)
                        clock.advance(10.0)
                        return NodeResult(
                            status=status,  # type: ignore[arg-type]
                            summary=f"first provider returned a known {status}",
                            artifacts={"first-provider-evidence": evidence},
                            actual_model="sonnet",
                            provider="claude",
                            result_kind="worker",
                            checks=("fixture-first-provider",),
                        )

                class FallbackProvider:
                    def execute(self, request: ExecutionRequest) -> NodeResult:
                        codex_calls.append(request)
                        raise AssertionError("expired fallback must not spawn Codex")

                try:
                    with (
                        patch("codex_workbench.service.time.monotonic", new=clock.monotonic),
                        patch.object(
                            coordinator,
                            "_executor",
                            side_effect=lambda provider: FirstProvider() if provider == "claude" else FallbackProvider(),
                        ),
                    ):
                        coordinator._execute_claimed(claimed, _ClaimRoute(None, (), None))
                finally:
                    coordinator._pool.shutdown(wait=True)

                task = store.get_task(task_id)
                work = next(node for node in task["nodes"] if node["node_id"] == "work")
                self.assertEqual(first_requests[0].provider_deadline_monotonic, 110.0)
                self.assertEqual(codex_calls, [])
                self.assertEqual(work["result"]["status"], status)
                self.assertEqual(work["result"]["actual_model"], "sonnet")
                self.assertEqual(work["result"]["provider"], "claude")
                self.assertEqual(work["result"]["artifacts"]["first-provider-evidence"], evidence_refs[0])
                self.assertIn("provider execution budget exhausted before Codex fallback", work["result"]["summary"])
                self.assertEqual(
                    [event for event in store.read_events(task_id=task_id) if event["event_type"] == "node.routed"],
                    [],
                )

    def test_fallback_retains_the_remaining_shared_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _store, coordinator, claimed = self._claim(root, task_id="budget-remaining")
            clock = _Clock(20.0)
            fallback_requests: list[ExecutionRequest] = []

            class FirstProvider:
                def execute(self, _request: ExecutionRequest) -> NodeResult:
                    subprocess.run(
                        [sys.executable, "-c", "print('first provider fixture')"],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    clock.advance(7.0)
                    return NodeResult("failed", "first provider fixture failed", result_kind="worker")

            class FallbackProvider:
                def execute(self, request: ExecutionRequest) -> NodeResult:
                    fallback_requests.append(request)
                    subprocess.run(
                        [sys.executable, "-c", "print('fallback provider fixture')"],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    return NodeResult("failed", "fallback provider fixture failed", result_kind="worker")

            try:
                with (
                    patch("codex_workbench.service.time.monotonic", new=clock.monotonic),
                    patch.object(
                        coordinator,
                        "_executor",
                        side_effect=lambda provider: FirstProvider() if provider == "claude" else FallbackProvider(),
                    ),
                ):
                    coordinator._execute_claimed(claimed, _ClaimRoute(None, (), None))
            finally:
                coordinator._pool.shutdown(wait=True)

            self.assertEqual(len(fallback_requests), 1)
            self.assertEqual(fallback_requests[0].provider_deadline_monotonic, 30.0)
            assert fallback_requests[0].provider_deadline_monotonic is not None
            self.assertEqual(fallback_requests[0].provider_deadline_monotonic - clock.monotonic(), 3.0)

    def test_single_provider_execution_keeps_its_own_budget_without_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, coordinator, claimed = self._claim(
                root,
                task_id="budget-single-provider",
                executor="codex",
            )
            clock = _Clock(50.0)
            requests: list[ExecutionRequest] = []

            class SingleProvider:
                def execute(self, request: ExecutionRequest) -> NodeResult:
                    requests.append(request)
                    subprocess.run(
                        [sys.executable, "-c", "print('single provider fixture')"],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    return NodeResult("failed", "single provider fixture failed", result_kind="worker")

            try:
                with (
                    patch("codex_workbench.service.time.monotonic", new=clock.monotonic),
                    patch.object(coordinator, "_executor", return_value=SingleProvider()),
                ):
                    coordinator._execute_claimed(claimed, _ClaimRoute(None, (), None))
            finally:
                coordinator._pool.shutdown(wait=True)

            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].provider_deadline_monotonic, 60.0)
            self.assertEqual(
                [event for event in store.read_events(task_id="budget-single-provider") if event["event_type"] == "node.routed"],
                [],
            )

    def test_direct_codex_executor_uses_remaining_budget_and_keeps_legacy_timeout_without_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock(40.0)

            class FixtureCodex(CodexExecutor):
                def __init__(self) -> None:
                    super().__init__(ArtifactStore(root / "artifacts"), binary=sys.executable)
                    self.qualification_timeouts: list[float] = []
                    self.spawn_timeouts: list[int | float] = []

                def qualification(self, *, timeout_seconds: float = 15) -> tuple[bool, str]:
                    self.qualification_timeouts.append(timeout_seconds)
                    return True, "fixture"

                def _command(
                    self,
                    _binary: str,
                    _request: ExecutionRequest,
                    _schema_path: Path,
                    _output_path: Path,
                ) -> list[str]:
                    return [sys.executable, "-c", "print('codex fixture subprocess')"]

                def _run(self, command: list[str], **kwargs: object):  # type: ignore[no-untyped-def]
                    self.spawn_timeouts.append(kwargs["timeout"])
                    return super()._run(command, **kwargs)  # type: ignore[arg-type]

            executor = FixtureCodex()
            with patch("codex_workbench.executors.time.monotonic", new=clock.monotonic):
                executor.execute(self._request(root, deadline=43.5))
                executor.execute(self._request(root, timeout_seconds=8))

            self.assertEqual(executor.qualification_timeouts, [3.5, 15])
            self.assertEqual(executor.spawn_timeouts, [3.5, 8])

    def test_qualification_cannot_overrun_the_remaining_budget_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock(70.0)

            class QualificationConsumesBudget(ClaudeExecutor):
                def __init__(self) -> None:
                    super().__init__(ArtifactStore(root / "artifacts"), quota=None)
                    self.qualification_timeouts: list[float | None] = []
                    self.spawns = 0

                def qualification(
                    self,
                    _model: str,
                    *,
                    timeout_seconds: float | None = None,
                ) -> tuple[bool, str]:
                    self.qualification_timeouts.append(timeout_seconds)
                    assert timeout_seconds is not None
                    clock.advance(timeout_seconds)
                    return True, "fixture"

                def _run(self, *_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
                    self.spawns += 1
                    raise AssertionError("budget exhausted during qualification must prevent spawn")

            executor = QualificationConsumesBudget()
            with patch("codex_workbench.executors.time.monotonic", new=clock.monotonic):
                result = executor.execute(self._request(root, provider="claude", deadline=75.0))

            self.assertEqual(executor.qualification_timeouts, [5.0])
            self.assertEqual(executor.spawns, 0)
            self.assertEqual(result.status, "failed")
            self.assertIn("budget exhausted before spawn", result.summary)

    def test_initial_zero_budget_skips_qualification_and_spawn_for_both_executors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = ClaudeExecutor(ArtifactStore(root / "claude-artifacts"), quota=None)
            codex = CodexExecutor(ArtifactStore(root / "codex-artifacts"), binary=sys.executable)
            with (
                patch("codex_workbench.executors.time.monotonic", return_value=100.0),
                patch.object(claude, "qualification", side_effect=AssertionError("must not qualify Claude")) as claude_qualification,
                patch.object(claude, "_run") as claude_run,
                patch.object(codex, "qualification", side_effect=AssertionError("must not qualify Codex")) as codex_qualification,
                patch.object(codex, "_run") as codex_run,
            ):
                claude_result = claude.execute(self._request(root, provider="claude", deadline=100.0))
                codex_result = codex.execute(self._request(root, deadline=100.0))

            self.assertEqual((claude_result.status, codex_result.status), ("failed", "failed"))
            self.assertIn("budget exhausted before spawn", claude_result.summary)
            self.assertIn("budget exhausted before spawn", codex_result.summary)
            claude_qualification.assert_not_called()
            codex_qualification.assert_not_called()
            claude_run.assert_not_called()
            codex_run.assert_not_called()

    def test_native_qualification_and_auth_probes_receive_the_remaining_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_binary = root / "fixture-codex"
            codex_host = root / "codex-code-mode-host"
            claude_binary = root / "fixture-claude"
            for binary in (codex_binary, codex_host, claude_binary):
                binary.write_text("#!/bin/sh\n", encoding="utf-8")
                binary.chmod(0o755)

            class AllowedQuota:
                def dispatch_decision(self, *_args: object, **_kwargs: object) -> ClaudeDispatchDecision:
                    return ClaudeDispatchDecision("claude", "green", "fixture allowed", 1)

            codex = CodexExecutor(ArtifactStore(root / "codex-artifacts"), binary=str(codex_binary))
            claude = ClaudeExecutor(
                ArtifactStore(root / "claude-artifacts"),
                quota=AllowedQuota(),  # type: ignore[arg-type]
                binary=str(claude_binary),
            )
            probes: list[tuple[tuple[str, ...], float]] = []

            def probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                timeout = kwargs["timeout"]
                assert isinstance(timeout, float)
                probes.append((tuple(command[1:]), timeout))
                if command[1:] == ["login", "status"]:
                    return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
                self.assertEqual(command[1:], ["auth", "status", "--json"])
                return subprocess.CompletedProcess(
                    command,
                    0,
                    '{"loggedIn":true,"authMethod":"subscription","apiProvider":"firstParty"}',
                    "",
                )

            failed_process = (subprocess.CompletedProcess([], 1, "", ""), {})
            with (
                patch("codex_workbench.executors.time.monotonic", return_value=100.0),
                patch("codex_workbench.executors.subprocess.run", side_effect=probe),
                patch.object(codex, "_run", return_value=failed_process),
                patch.object(claude, "_run", return_value=failed_process),
            ):
                codex.execute(self._request(root, deadline=104.0))
                claude.execute(self._request(root, provider="claude", deadline=104.0))

            self.assertEqual(
                probes,
                [
                    (("login", "status"), 4.0),
                    (("auth", "status", "--json"), 4.0),
                ],
            )

    def test_provider_spawn_timeout_stays_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = ClaudeExecutor(ArtifactStore(root / "claude-artifacts"), quota=None)
            codex = CodexExecutor(ArtifactStore(root / "codex-artifacts"), binary=sys.executable)
            claude_request = self._request(root, provider="claude", deadline=105.0)
            codex_request = self._request(root, deadline=105.0)
            with (
                patch("codex_workbench.executors.time.monotonic", return_value=100.0),
                patch.object(claude, "qualification", return_value=(True, "fixture")),
                patch.object(claude, "_run", side_effect=subprocess.TimeoutExpired("claude", 5.0)) as claude_run,
                patch.object(codex, "qualification", return_value=(True, "fixture")),
                patch.object(codex, "_run", side_effect=subprocess.TimeoutExpired("codex", 5.0)) as codex_run,
            ):
                claude_result = claude.execute(claude_request)
                codex_result = codex.execute(codex_request)

            self.assertEqual((claude_result.status, codex_result.status), ("indeterminate", "indeterminate"))
            self.assertEqual(claude_run.call_args.kwargs["timeout"], 5.0)
            self.assertEqual(codex_run.call_args.kwargs["timeout"], 5.0)

    def test_auth_and_quota_rejections_remain_before_provider_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = self._request(root, provider="claude", deadline=105.0)
            claude = ClaudeExecutor(ArtifactStore(root / "claude-artifacts"), quota=None)
            codex = CodexExecutor(ArtifactStore(root / "codex-artifacts"), binary=sys.executable)
            with (
                patch("codex_workbench.executors.time.monotonic", return_value=100.0),
                patch.object(claude, "_run") as claude_run,
                patch.object(codex, "qualification", return_value=(False, "Codex authentication rejected")),
                patch.object(codex, "_run") as codex_run,
            ):
                quota_result = claude.execute(request)
                auth_result = codex.execute(self._request(root, deadline=105.0))

            self.assertEqual((quota_result.status, quota_result.summary), ("blocked", "Claude quota is unknown"))
            self.assertEqual((auth_result.status, auth_result.summary), ("blocked", "Codex authentication rejected"))
            claude_run.assert_not_called()
            codex_run.assert_not_called()
