from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.executors import (
    CodexExecutor,
    ExecutionRequest,
    codex_subscription_environment,
)
from codex_workbench.model import NodeSpec, QuotaSnapshot, TaskContract
from codex_workbench.planner import PLAN_SCHEMA, CodexPlanner


class ModelTests(unittest.TestCase):
    def test_artifact_refs_reject_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            with self.assertRaises(ValueError):
                store.path_for("sha256:" + "a" * 64 + ":../secret")

    def test_runtime_steering_is_injected_without_changing_scope(self) -> None:
        request = ExecutionRequest(
            task_id="steered",
            node_id="work",
            attempt=1,
            contract={
                "objective": "update parser",
                "allowed_scope": ["src/parser"],
                "forbidden_scope": ["src/private"],
                "acceptance_commands": ["python -m unittest"],
                "timeout_seconds": 30,
            },
            spec={"title": "parser", "prompt": "implement", "verifier": False},
            worktree=None,
            steering=("保留公开接口",),
        )
        prompt = CodexExecutor._prompt(request)
        self.assertIn("Runtime steering: [\"保留公开接口\"]", prompt)
        self.assertIn('Allowed scope: ["src/parser"]', prompt)

    def test_codex_qualification_requires_companion_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "codex"
            binary.write_text("#!/bin/sh\necho 'Logged in using ChatGPT'\n")
            binary.chmod(0o755)
            executor = CodexExecutor(ArtifactStore(root / "artifacts"), str(binary))

            qualified, reason = executor.qualification()

            self.assertFalse(qualified)
            self.assertIn("codex-code-mode-host", reason)

    def test_codex_worker_enables_code_mode_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            executor = CodexExecutor(ArtifactStore(root / "artifacts"), "/tmp/codex")
            request = ExecutionRequest(
                task_id="task-1",
                node_id="worker",
                attempt=1,
                contract={
                    "objective": "write a bounded file",
                    "allowed_scope": ["result.txt"],
                    "forbidden_scope": [],
                    "acceptance_commands": [],
                    "timeout_seconds": 30,
                },
                spec={
                    "title": "write result",
                    "prompt": "write result.txt",
                    "model": "gpt-5.6-luna",
                    "verifier": False,
                },
                worktree=worktree,
            )

            def fake_run(command: list[str], **_: object):
                enabled = [
                    command[index + 1]
                    for index, argument in enumerate(command[:-1])
                    if argument == "--enable"
                ]
                disabled = [
                    command[index + 1]
                    for index, argument in enumerate(command[:-1])
                    if argument == "--disable"
                ]
                self.assertIn("code_mode_host", enabled)
                self.assertNotIn("code_mode_host", disabled)
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    '{"status":"succeeded","summary":"ok","changed_paths":[],"checks":[]}'
                )
                return subprocess.CompletedProcess(command, 0, "", ""), {}

            with (
                patch.object(executor, "qualification", return_value=(True, "native-subscription")),
                patch.object(executor, "_run", side_effect=fake_run),
            ):
                executor.execute(request)

    def test_planner_schema_requires_every_declared_property(self) -> None:
        item = PLAN_SCHEMA["properties"]["nodes"]["items"]
        self.assertEqual(set(item["required"]), set(item["properties"]))

    def test_planner_receives_only_quota_admitted_claude_models(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
        )
        prompt = CodexPlanner._prompt(
            contract,
            claude_models_available=("sonnet",),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
        )
        self.assertIn('["sonnet"]', prompt)
        self.assertIn("only when its exact model family appears", prompt)

    def test_codex_environment_isolates_home_and_removes_api_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/Users/example",
                "CODEX_WORKBENCH_PROCESS_HOME": "/private/workbench-home",
                "OPENAI_API_KEY": "must-not-forward",
                "ANTHROPIC_API_KEY": "must-not-forward",
            },
            clear=False,
        ):
            environment = codex_subscription_environment()
        self.assertEqual(environment["HOME"], "/private/workbench-home")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_contract_hash_is_deterministic(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="/tmp/example",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
        )
        self.assertEqual(contract.digest, TaskContract.from_dict(contract.to_dict()).digest)

    def test_contract_rejects_relative_repository(self) -> None:
        contract = TaskContract(
            task_id="task-1",
            repository="relative",
            base_sha="abc123",
            objective="bounded work",
            allowed_scope=("src",),
        )
        with self.assertRaisesRegex(ValueError, "absolute"):
            contract.validate()

    def test_claude_quota_fails_closed(self) -> None:
        unknown = QuotaSnapshot(
            observed_at="2026-08-24T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=None,
            weekly_all_remaining=90,
            weekly_sonnet_remaining=90,
            source="fixture",
        )
        self.assertEqual(unknown.permits("sonnet"), (False, "Claude quota is unknown"))

        protected = QuotaSnapshot(
            observed_at="2026-08-24T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=25,
            weekly_all_remaining=90,
            weekly_sonnet_remaining=90,
            source="fixture",
        )
        allowed, reason = protected.permits("sonnet")
        self.assertFalse(allowed)
        self.assertIn("protection active", reason)

        healthy = QuotaSnapshot(
            observed_at="2026-08-24T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=60,
            weekly_all_remaining=70,
            weekly_sonnet_remaining=80,
            source="fixture",
        )
        self.assertTrue(healthy.permits("sonnet")[0])

    def test_claude_quota_zones_enforce_model_and_concurrency_policy(self) -> None:
        def snapshot(remaining: float) -> QuotaSnapshot:
            return QuotaSnapshot(
                observed_at="2026-08-26T00:00:00+00:00",
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=remaining,
                weekly_all_remaining=remaining,
                weekly_sonnet_remaining=remaining,
                weekly_fable_remaining=remaining,
                source="settings-usage",
            )

        red = snapshot(27)
        self.assertEqual(red.dispatch_decision("sonnet").action, "codex")
        self.assertEqual(red.dispatch_decision("sonnet").zone, "red")

        yellow = snapshot(35)
        self.assertEqual(yellow.dispatch_decision("opus").action, "codex")
        self.assertEqual(yellow.dispatch_decision("sonnet").action, "claude")
        deferred = yellow.dispatch_decision("sonnet", ("sonnet",))
        self.assertEqual(deferred.action, "defer")
        self.assertEqual(deferred.max_concurrency, 1)

        green = snapshot(60)
        self.assertEqual(green.dispatch_decision("sonnet", ("sonnet",)).action, "claude")
        self.assertEqual(
            green.dispatch_decision("sonnet", ("sonnet", "sonnet")).action,
            "defer",
        )
        self.assertEqual(green.dispatch_decision("opus", ("opus",)).action, "defer")
        self.assertEqual(green.dispatch_decision("fable", ("opus",)).action, "defer")

        mixed = QuotaSnapshot(
            observed_at="2026-08-26T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=60,
            weekly_all_remaining=60,
            weekly_sonnet_remaining=60,
            weekly_fable_remaining=None,
            source="settings-usage",
        )
        self.assertEqual(mixed.policy_summary()["zone"], "mixed")
        self.assertEqual(mixed.policy_summary()["zones"]["fable"], "unknown")

    def test_claude_quota_zone_boundaries_are_exact(self) -> None:
        def zone(remaining: float) -> str:
            snapshot = QuotaSnapshot(
                observed_at="2026-08-26T00:00:00+00:00",
                auth_ok=True,
                auth_method="native-subscription",
                five_hour_remaining=remaining,
                weekly_all_remaining=remaining,
                weekly_sonnet_remaining=remaining,
                source="settings-usage",
            )
            return snapshot.quota_zone("sonnet")[0]

        self.assertEqual(zone(25), "protected")
        self.assertEqual(zone(25.1), "red")
        self.assertEqual(zone(29.9), "red")
        self.assertEqual(zone(30), "yellow")
        self.assertEqual(zone(40), "yellow")
        self.assertEqual(zone(40.1), "green")

    def test_quota_snapshot_rejects_invalid_percentages(self) -> None:
        snapshot = QuotaSnapshot(
            observed_at="2026-08-26T00:00:00+00:00",
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=101,
            weekly_all_remaining=50,
            weekly_sonnet_remaining=50,
            source="settings-usage",
        )
        with self.assertRaisesRegex(ValueError, "five_hour_remaining"):
            snapshot.validate()


if __name__ == "__main__":
    unittest.main()
