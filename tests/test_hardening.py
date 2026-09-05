from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import zipfile

from codex_workbench.acceptance import build_acceptance_report
from codex_workbench.artifacts import ArtifactStore, presentation_format
from codex_workbench.authority import normalize_boot_id
from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.config import WorkbenchConfig
from codex_workbench.executors import ClaudeExecutor, CodexExecutor, ExecutionRequest
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract
from codex_workbench.quota import JsonFileQuotaAdapter, QuotaRefresher
from codex_workbench.store import StateConflictError, WorkbenchStore
from unittest.mock import patch


class WorkbenchHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("coordinator-test", "test-machine")
        self.artifacts = ArtifactStore(self.root / "artifacts")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_darwin_boot_id_ignores_microsecond_format_jitter(self) -> None:
        first = "{ sec = 1787971311, usec = 90423 } Fri Aug 28 23:01:51 2026"
        second = "{ sec = 1787971311, usec = 91777 } Fri Aug 28 23:01:51 2026"
        self.assertEqual(normalize_boot_id(first), "darwin:1787971311")
        self.assertEqual(normalize_boot_id(first), normalize_boot_id(second))
        for instance, boot in (("one", first), ("two", second)):
            self.store.record_system_event(
                "coordinator.started",
                {
                    "instance_id": instance,
                    "pid": 1,
                    "host": "mac-mini",
                    "boot_id": boot,
                    "ledger_cursor_before_start": 1,
                    "ledger_task_count": 1,
                },
            )
        a3 = next(
            check
            for check in build_acceptance_report(self.store)["checks"]
            if check["id"] == "A3"
        )
        self.assertEqual(a3["status"], "pending")
        self.assertIn("1 个 boot ID", a3["evidence"])

    def test_quota_ttl_fails_closed_and_local_adapter_refreshes(self) -> None:
        now = datetime.now(UTC)
        stale = QuotaSnapshot(
            observed_at=(now - timedelta(minutes=16)).isoformat(),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=90,
            weekly_all_remaining=90,
            weekly_sonnet_remaining=90,
            source="settings-usage-export",
        )
        decision = stale.dispatch_decision(
            "sonnet", max_age_seconds=900, current_time=now
        )
        self.assertEqual((decision.action, decision.zone), ("codex", "unknown"))
        self.assertIn("stale", decision.reason)

        export = self.root / "quota.json"
        export.write_text(
            json.dumps(
                {
                    "observed_at": now.isoformat(),
                    "auth_ok": True,
                    "auth_method": "native-subscription",
                    "five_hour_remaining": 70,
                    "weekly_all_remaining": 80,
                    "weekly_sonnet_remaining": 75,
                    "five_hour_window_id": "window-1",
                    "weekly_window_id": "week-1",
                }
            )
        )
        refresher = QuotaRefresher(
            self.store,
            JsonFileQuotaAdapter(export),
            interval_seconds=60,
        )
        self.assertTrue(refresher.refresh_once())
        self.assertFalse(refresher.refresh_once())
        refreshed = self.store.latest_quota()
        assert refreshed is not None
        self.assertEqual(refreshed.source, "settings-usage-export")
        manual_decision = refreshed.dispatch_decision(
            "sonnet", max_age_seconds=900, current_time=now
        )
        self.assertEqual((manual_decision.action, manual_decision.zone), ("codex", "unknown"))
        self.assertIn("provenance", manual_decision.reason)

        export.write_text(
            json.dumps(
                {
                    "producer": PRODUCER,
                    "producer_schema_version": PRODUCER_SCHEMA_VERSION,
                    "source": COMPATIBLE_SOURCE,
                    "claude_version": SUPPORTED_USAGE_VERSION,
                    "observed_at": now.isoformat(),
                    "auth_ok": True,
                    "auth_method": "native-subscription",
                    "pools": {
                        "five_hour": {
                            "displayed_used_percent": 10,
                            "remaining_lower_bound": 89,
                            "window_id": "five_hour:2099-01-01T00:00:00Z",
                            "reset_precision": "precise",
                            "reset_fingerprint": "4 pm (Asia/Singapore)",
                        },
                        "seven_day": {
                            "displayed_used_percent": 20,
                            "remaining_lower_bound": 79,
                            "window_id": "weekly:2099-01-01@Asia/Singapore",
                            "reset_precision": "date-only-compatible",
                            "reset_fingerprint": "Jan 1, 2099 (Asia/Singapore)",
                        },
                        "seven_day_sonnet": {
                            "displayed_used_percent": 25,
                            "remaining_lower_bound": 74,
                            "window_id": "weekly:2099-01-01@Asia/Singapore",
                            "reset_precision": "date-only-compatible",
                            "reset_fingerprint": "Jan 1, 2099 (Asia/Singapore)",
                        },
                    },
                }
            )
        )
        self.assertTrue(refresher.refresh_once())
        compatible = self.store.latest_quota()
        assert compatible is not None
        self.assertEqual(
            compatible.dispatch_decision(
                "sonnet", max_age_seconds=900, current_time=now
            ).action,
            "claude",
        )

    def test_quota_source_is_persisted_and_defaults_inside_authority_root(self) -> None:
        config = WorkbenchConfig(
            self.root / "authority",
            deployment_role="authority",
            authority_host=__import__("socket").gethostname(),
        )
        config.initialize()
        persisted = json.loads(config.config_file.read_text())
        self.assertEqual(
            persisted["quota_snapshot_file"],
            str(config.state_root / "claude-quota.json"),
        )
        loaded = WorkbenchConfig.load(config.state_root)
        self.assertEqual(loaded.effective_quota_snapshot_file, config.state_root / "claude-quota.json")

    def test_schema_v8_invalidates_pre_governance_evidence_cache(self) -> None:
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata SET value = '7' WHERE key = 'schema_version'")
            connection.execute(
                "INSERT INTO evidence_cache(cache_key,result_json,source_task_id,source_node_id,created_at,last_used_at,use_count) "
                "VALUES('v1','{}','old','old','now','now',0)"
            )
        self.store.initialize()
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM evidence_cache"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_subscription_executor_failure_results_keep_structured_contract(self) -> None:
        request = ExecutionRequest(
            task_id="task", node_id="worker", attempt=1,
            contract={"timeout_seconds": 1},
            spec={"model": "gpt-5.6-luna", "verifier": False},
            worktree=self.root,
        )
        codex = CodexExecutor(self.artifacts)
        with patch.object(codex, "qualification", return_value=(False, "not qualified")):
            result = codex.execute(request)
        self.assertEqual((result.status, result.result_kind), ("blocked", "worker"))
        self.assertEqual(result.requested_model, "gpt-5.6-luna")
        self.assertEqual(result.provider, "codex")
        self.assertIsNone(result.agent_name)
        self.assertIsNone(result.capability_snapshot_id)

        verifier_request = ExecutionRequest(
            task_id="task", node_id="verify", attempt=1,
            contract={"timeout_seconds": 1},
            spec={"model": "gpt-5.6-sol", "verifier": True},
            worktree=self.root,
        )
        with patch.object(codex, "qualification", return_value=(False, "not qualified")):
            verifier_result = codex.execute(verifier_request)
        self.assertEqual(
            (verifier_result.status, verifier_result.result_kind, verifier_result.verdict),
            ("blocked", "verifier", "blocked"),
        )
        self.assertEqual(verifier_result.requested_model, "gpt-5.6-sol")
        self.assertEqual(verifier_result.provider, "codex")

        claude = ClaudeExecutor(self.artifacts, quota=None)
        with patch.object(claude, "qualification", return_value=(False, "not qualified")):
            claude_result = claude.execute(
                ExecutionRequest(
                    task_id="task", node_id="claude", attempt=1,
                    contract={"timeout_seconds": 1},
                    spec={"model": "sonnet", "verifier": False},
                    worktree=self.root,
                )
        )
        self.assertEqual((claude_result.status, claude_result.result_kind), ("blocked", "worker"))
        self.assertEqual(claude_result.requested_model, "sonnet")
        self.assertEqual(claude_result.provider, "claude")
        self.assertIsNone(claude_result.agent_name)
        self.assertIsNone(claude_result.capability_snapshot_id)

    def test_codex_success_and_failure_results_include_pinned_capability_metadata(self) -> None:
        executor = CodexExecutor(self.artifacts)
        common = {
            "title": "bounded worker",
            "prompt": "implement the bounded change",
            "model": "gpt-5.6-luna",
            "verifier": False,
            "provider": "codex",
            "agent_name": "codex.exec",
            "agent_version": "0.149.1",
            "capability_snapshot_id": "catalog-20260902-001",
            "model_capability_id": "codex.gpt-5.6-luna",
            "agent_capability_id": "codex.exec",
        }
        request = ExecutionRequest(
            task_id="task",
            node_id="codex",
            attempt=1,
            contract={
                "objective": "implement the bounded change",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 30,
            },
            spec=common,
            worktree=self.root,
        )
        response = {
            "status": "succeeded",
            "summary": "bounded change complete",
            "changed_paths": ["src/worker.py"],
            "checks": ["python -m unittest"],
        }

        def fake_run(command: list[str], **_: object):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(response))
            return subprocess.CompletedProcess(command, 0, "", ""), {}

        with (
            patch.object(executor, "qualification", return_value=(True, "native-subscription")),
            patch.object(executor, "_run", side_effect=fake_run),
        ):
            result = executor.execute(request)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.actual_model, "gpt-5.6-luna")
        self.assertEqual(
            (
                result.requested_model,
                result.provider,
                result.agent_name,
                result.agent_version,
                result.capability_snapshot_id,
                result.model_capability_id,
                result.agent_capability_id,
            ),
            (
                "gpt-5.6-luna",
                "codex",
                "codex.exec",
                "0.149.1",
                "catalog-20260902-001",
                "codex.gpt-5.6-luna",
                "codex.exec",
            ),
        )

        failed_request = ExecutionRequest(
            task_id="task",
            node_id="codex-failed",
            attempt=1,
            contract={
                "objective": "implement the bounded change",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 30,
            },
            spec={**common, "agent_version": "0.149.1"},
            worktree=self.root,
        )
        with (
            patch.object(executor, "qualification", return_value=(True, "native-subscription")),
            patch.object(
                executor,
                "_run",
                return_value=(subprocess.CompletedProcess([], 2, "", "worker failed"), {}),
            ),
        ):
            failed = executor.execute(failed_request)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.provider, "codex")
        self.assertEqual(failed.capability_snapshot_id, "catalog-20260902-001")
        self.assertEqual(failed.agent_version, "0.149.1")

    def test_claude_worker_uses_native_structured_scope_and_model_evidence(self) -> None:
        executor = ClaudeExecutor(self.artifacts, quota=None)
        request = ExecutionRequest(
            task_id="task",
            node_id="claude",
            attempt=1,
            contract={
                "objective": "write the parser",
                "allowed_scope": ["src"],
                "forbidden_scope": ["src/private"],
                "acceptance_commands": ["python -m unittest tests/test_parser.py"],
                "timeout_seconds": 30,
            },
            spec={
                "title": "parser worker",
                "prompt": "implement the parser",
                "model": "sonnet",
                "verifier": False,
                "provider": "claude",
                "agent_name": "claude-code",
                "agent_version": "2.1.239",
                "capability_snapshot_id": "catalog-20260902-001",
                "model_capability_id": "claude.sonnet",
                "agent_capability_id": "claude-code",
                "read_scopes": ["src"],
                "write_scopes": ["src/parser.py"],
            },
            worktree=self.root,
        )
        response = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {
                "status": "succeeded",
                "summary": "parser updated",
                "changed_paths": ["src/parser.py"],
                "checks": ["python -m unittest tests/test_parser.py"],
            },
            "modelUsage": {"claude-sonnet-4-5-20250929": {"outputTokens": 4}},
        }
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object):
            captured["command"] = command
            captured["environment"] = kwargs["environment"]
            return subprocess.CompletedProcess(command, 0, json.dumps(response), ""), {
                "stdout": "sha256:" + "1" * 64 + ":stdout.log",
                "stderr": "sha256:" + "2" * 64 + ":stderr.log",
            }

        with (
            patch.object(executor, "qualification", return_value=(True, "native-subscription")),
            patch.object(executor, "_run", side_effect=fake_run),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "must-not-forward"}, clear=False),
        ):
            result = executor.execute(request)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.summary, "parser updated")
        self.assertEqual(result.actual_model, "claude-sonnet-4-5-20250929")
        self.assertEqual(result.requested_model, "sonnet")
        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.agent_name, "claude-code")
        self.assertEqual(result.agent_version, "2.1.239")
        self.assertEqual(result.capability_snapshot_id, "catalog-20260902-001")
        self.assertEqual(result.model_capability_id, "claude.sonnet")
        self.assertEqual(result.agent_capability_id, "claude-code")
        self.assertEqual(result.changed_paths, ("src/parser.py",))
        self.assertEqual(result.checks, ("python -m unittest tests/test_parser.py",))
        self.assertEqual(result.governance_profile, "code-as-harness/v1")
        self.assertEqual(result.verification_tier, "L2")
        command = captured["command"]
        assert isinstance(command, list)
        self.assertIn("-p", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        governance = command[command.index("--append-system-prompt") + 1]
        self.assertIn("Governance profile: code-as-harness/v1", governance)
        self.assertIn("Verification tier: L2", governance)
        schema = json.loads(command[command.index("--json-schema") + 1])
        self.assertEqual(set(schema["required"]), {"status", "summary", "changed_paths", "checks"})
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        tools = command[command.index("--tools") + 1]
        self.assertIn("Read", tools)
        self.assertIn("Edit", tools)
        self.assertIn("Write", tools)
        self.assertIn("Bash", tools)
        self.assertIn("Bash(python -m unittest tests/test_parser.py)", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--allow-dangerously-skip-permissions", command)
        self.assertNotIn("--fallback-model", command)
        environment = captured["environment"]
        assert isinstance(environment, dict)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_claude_worker_fails_closed_without_structured_output_or_model_evidence(self) -> None:
        executor = ClaudeExecutor(self.artifacts, quota=None)
        request = ExecutionRequest(
            task_id="task",
            node_id="claude",
            attempt=1,
            contract={
                "objective": "inspect",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 30,
            },
            spec={
                "title": "read-only worker",
                "prompt": "inspect the source",
                "model": "sonnet",
                "verifier": False,
                "provider": "claude",
                "agent_name": "claude-code",
                "agent_version": "2.1.239",
                "capability_snapshot_id": "catalog-20260902-001",
                "model_capability_id": "claude.sonnet",
                "agent_capability_id": "claude-code",
                "write_scopes": [],
            },
            worktree=self.root,
        )
        response = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "unstructured text that must not be accepted",
        }
        with (
            patch.object(executor, "qualification", return_value=(True, "native-subscription")),
            patch.object(
                executor,
                "_run",
                return_value=(
                    subprocess.CompletedProcess([], 0, json.dumps(response), ""),
                    {},
                ),
            ),
        ):
            result = executor.execute(request)

        self.assertEqual(result.status, "failed")
        self.assertIn("structured", result.summary.lower())
        self.assertIsNone(result.actual_model)
        self.assertEqual(result.requested_model, "sonnet")
        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.agent_name, "claude-code")
        self.assertEqual(result.agent_version, "2.1.239")
        self.assertEqual(result.capability_snapshot_id, "catalog-20260902-001")
        self.assertEqual(result.model_capability_id, "claude.sonnet")
        self.assertEqual(result.agent_capability_id, "claude-code")
        self.assertEqual(result.changed_paths, ())
        self.assertEqual(result.checks, ())

    def test_claude_worker_rejects_invalid_structured_payload_and_read_only_permissions(self) -> None:
        executor = ClaudeExecutor(self.artifacts, quota=None)
        request = ExecutionRequest(
            task_id="task",
            node_id="claude",
            attempt=1,
            contract={
                "objective": "inspect",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 30,
            },
            spec={
                "title": "read-only worker",
                "prompt": "inspect the source",
                "model": "sonnet",
                "verifier": False,
                "write_scopes": [],
            },
            worktree=self.root,
        )
        response = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {
                "status": "succeeded",
                "summary": "bad",
                "changed_paths": [],
                "checks": [],
                "unexpected": True,
            },
            "modelUsage": {"claude-sonnet-4-5-20250929": {"outputTokens": 4}},
        }
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **_: object):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, json.dumps(response), ""), {}

        with (
            patch.object(executor, "qualification", return_value=(True, "native-subscription")),
            patch.object(executor, "_run", side_effect=fake_run),
        ):
            result = executor.execute(request)

        self.assertEqual(result.status, "failed")
        self.assertIn("structured", result.summary.lower())
        command = captured["command"]
        assert isinstance(command, list)
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        tools = command[command.index("--tools") + 1]
        self.assertIn("Read", tools)
        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)
        self.assertNotIn("Bash", tools)

    def test_claude_timeout_is_indeterminate_without_requested_model_claim(self) -> None:
        executor = ClaudeExecutor(self.artifacts, quota=None)
        request = ExecutionRequest(
            task_id="task",
            node_id="claude",
            attempt=1,
            contract={
                "objective": "inspect",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 1,
            },
            spec={"title": "worker", "prompt": "inspect", "model": "sonnet", "verifier": False},
            worktree=self.root,
        )
        with (
            patch.object(executor, "qualification", return_value=(True, "native-subscription")),
            patch.object(executor, "_run", side_effect=subprocess.TimeoutExpired("claude", 1)),
        ):
            result = executor.execute(request)

        self.assertEqual(result.status, "indeterminate")
        self.assertIsNone(result.actual_model)
        self.assertEqual(result.requested_model, "sonnet")
        self.assertEqual(result.provider, "claude")

    def test_claude_executor_is_worker_only(self) -> None:
        executor = ClaudeExecutor(self.artifacts, quota=None)
        request = ExecutionRequest(
            task_id="task",
            node_id="verify",
            attempt=1,
            contract={
                "objective": "verify",
                "allowed_scope": ["src"],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 30,
            },
            spec={"title": "verifier", "prompt": "verify", "model": "sonnet", "verifier": True},
            worktree=self.root,
        )
        with patch.object(executor, "qualification", side_effect=AssertionError("must not qualify")):
            result = executor.execute(request)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.result_kind, "worker")
        self.assertIn("worker", result.summary.lower())

    def test_sol_rejection_runs_worker_repair_with_retry_escalation(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        contract = TaskContract(
            task_id="repair-loop",
            repository=str(repository),
            base_sha="base",
            objective="repair until verified",
            allowed_scope=("src",),
            retry_limit=2,
        )
        nodes = [
            NodeSpec(
                "worker",
                contract.task_id,
                "implement",
                "codex",
                "gpt-5.6-luna",
                "implement",
                write_scopes=("src",),
            ),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "codex",
                "gpt-5.6-sol",
                "verify",
                depends_on=("worker",),
                read_scopes=("src",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "create-repair")
        self.store.queue_task(contract.task_id)
        worker = self.store.claim_ready_node("worker-1", self.epoch)
        assert worker is not None
        self.store.settle_claimed(worker, self._worker_result("first patch", "gpt-5.6-luna"))
        verifier = self.store.claim_ready_node("sol-1", self.epoch)
        assert verifier is not None
        self.store.settle_claimed(
            verifier,
            self._verifier_result("needs_fix", "tests reveal a defect"),
        )

        task = self.store.get_task(contract.task_id)
        self.assertEqual(task["state"], "queued")
        self.assertIn("Verifier rejected attempt 1", task["steering"][-1]["instruction"])
        repaired = self.store.claim_ready_node("worker-2", self.epoch)
        assert repaired is not None
        self.assertEqual(repaired["node_id"], "worker")
        self.assertEqual(repaired["attempt"], 2)
        self.assertEqual(repaired["spec"]["model"], "gpt-5.6-terra")
        self.store.settle_claimed(repaired, self._worker_result("repaired patch", "gpt-5.6-terra"))
        reverify = self.store.claim_ready_node("sol-2", self.epoch)
        assert reverify is not None
        self.store.settle_claimed(
            reverify,
            self._verifier_result("accepted", "verified"),
        )
        self.assertEqual(self.store.get_task(contract.task_id)["state"], "accepted")
        event_types = {
            event["event_type"]
            for event in self.store.read_events(task_id=contract.task_id)
        }
        self.assertIn("task.repair_scheduled", event_types)
        a5 = next(
            check for check in build_acceptance_report(self.store)["checks"]
            if check["id"] == "A5"
        )
        self.assertEqual(a5["status"], "ok")

    def test_verifier_shape_evidence_and_epoch_are_fenced(self) -> None:
        repository = self.root / "fenced"
        repository.mkdir()
        contract = TaskContract(
            task_id="fenced",
            repository=str(repository),
            base_sha="base",
            objective="fenced settlement",
            allowed_scope=("src",),
        )
        with self.assertRaisesRegex(ValueError, "exact Codex control-plane"):
            self.store.create_task(
                contract,
                [
                    NodeSpec("work", "fenced", "work", "fixture", "fixture", "ok"),
                    NodeSpec(
                        "verify",
                        "fenced",
                        "verify",
                        "codex",
                        "gpt-5.6-terra",
                        "verify",
                        depends_on=("work",),
                        verifier=True,
                    ),
                ],
                "bad-verifier",
            )

        worker = NodeSpec(
            "work", "fenced", "work", "codex", "gpt-5.6-luna", "work",
            write_scopes=("src",),
        )
        verifier = NodeSpec(
            "verify", "fenced", "verify", "codex", "gpt-5.6-sol", "verify",
            depends_on=("work",), read_scopes=("src",), verifier=True,
        )
        self.store.create_task(contract, [worker, verifier], "good-verifier")
        self.store.queue_task("fenced")
        claim = self.store.claim_ready_node("worker", self.epoch)
        assert claim is not None
        with self.assertRaisesRegex(ValueError, "result_kind=worker"):
            self.store.settle_claimed(
                claim,
                NodeResult("succeeded", "unstructured"),
            )
        invalid = NodeResult(
            "succeeded",
            "forged",
            artifacts={"stdout": "sha256:" + "0" * 64 + ":log"},
            actual_model="gpt-5.6-luna",
            result_kind="worker",
            checks=("check",),
        )
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.store.settle_claimed(claim, invalid)

        newer_epoch = self.store.activate_coordinator("coordinator-new", "test-machine")
        self.assertGreater(newer_epoch, self.epoch)
        with self.assertRaisesRegex(StateConflictError, "stale"):
            self.store.settle_claimed(claim, self._worker_result("late result", "gpt-5.6-luna"))

    def test_scope_conflict_is_partitioned_by_repository_identity(self) -> None:
        for name in ("one", "two"):
            repository = self.root / name
            repository.mkdir()
            contract = TaskContract(
                task_id=name,
                repository=str(repository),
                base_sha="base",
                objective=name,
                allowed_scope=("src",),
            )
            self.store.create_task(
                contract,
                [
                    NodeSpec("work", name, "work", "fixture", "fixture", "ok", write_scopes=("src",)),
                    NodeSpec("verify", name, "verify", "fixture", "fixture", "accepted", depends_on=("work",), verifier=True),
                ],
                f"create-{name}",
            )
            self.store.queue_task(name)
        first = self.store.claim_ready_node("one", self.epoch)
        second = self.store.claim_ready_node("two", self.epoch)
        assert first is not None and second is not None
        self.assertNotEqual(first["task_id"], second["task_id"])

    def test_linked_worktrees_share_repository_identity_for_scope_conflicts(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
        (repository / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
        linked = self.root / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked)], cwd=repository, check=True, capture_output=True)
        for task_id, path in (("main-worktree", repository), ("linked-worktree", linked)):
            contract = TaskContract(
                task_id=task_id, repository=str(path), base_sha="base",
                objective=task_id, allowed_scope=("src",),
            )
            nodes = [
                NodeSpec("work", task_id, "work", "fixture", "fixture", "ok", write_scopes=("src",)),
                NodeSpec("verify", task_id, "verify", "fixture", "fixture", "accepted", depends_on=("work",), verifier=True),
            ]
            self.store.create_task(contract, nodes, f"create-{task_id}")
            self.store.queue_task(task_id)
        first = self.store.claim_ready_node("first", self.epoch)
        second = self.store.claim_ready_node("second", self.epoch)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a12_requires_real_provenance_and_content_format(self) -> None:
        fake = self.artifacts.put_bytes(b"not really a presentation", "pptx")
        with self.assertRaisesRegex(ValueError, "content"):
            self.store.record_acceptance_attestation(
                "A12", fake, "fake.pptx", 25, "week-1", "claude-session", "fixture"
            )

        presentation = self.root / "slides.pptx"
        with zipfile.ZipFile(presentation, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/presentation.xml", "<p:presentation/>")
        self.assertEqual(presentation_format(presentation), "pptx")
        data = presentation.read_bytes()
        ref = self.artifacts.put_bytes(data, "pptx")
        self.store.record_acceptance_attestation(
            "A12",
            ref,
            presentation.name,
            len(data),
            "week-1",
            "claude-web-session-123",
            "real reserved-quota journey",
        )
        report = build_acceptance_report(self.store)
        a12 = next(check for check in report["checks"] if check["id"] == "A12")
        self.assertEqual(a12["status"], "pending")

    def test_client_configuration_cannot_start_authority(self) -> None:
        client = WorkbenchConfig(self.root / "client", deployment_role="client")
        with self.assertRaisesRegex(RuntimeError, "cannot start a local writer"):
            client.assert_authority()

    def test_legacy_authority_config_requires_explicit_machine_rebind(self) -> None:
        config = WorkbenchConfig(
            self.root / "legacy-authority",
            deployment_role="authority",
            authority_host=__import__("socket").gethostname(),
        )
        config.initialize()
        loaded = WorkbenchConfig.load(config.state_root)
        with self.assertRaisesRegex(RuntimeError, "machine ID is missing"):
            loaded.assert_authority()

    def test_authority_config_checks_stable_machine_id_not_only_hostname(self) -> None:
        config = WorkbenchConfig(
            self.root / "bound-authority",
            deployment_role="authority",
            authority_host=__import__("socket").gethostname(),
            authority_machine_id="darwin:ioplatformuuid:expected",
        )
        with patch(
            "codex_workbench.config.authority_machine_id",
            return_value="darwin:ioplatformuuid:different",
        ), self.assertRaisesRegex(RuntimeError, "different machine ID"):
            config.assert_authority()

    def test_store_rejects_a_different_authority_machine(self) -> None:
        with self.assertRaisesRegex(StateConflictError, "different machine ID"):
            self.store.activate_coordinator("foreign", "other-machine")

    def _worker_result(self, summary: str, model: str) -> NodeResult:
        return NodeResult(
            "succeeded",
            summary,
            artifacts={
                "patch": self.artifacts.put_text(summary, "patch"),
                "stdout": self.artifacts.put_text("tests passed", "stdout.log"),
            },
            actual_model=model,
            result_kind="worker",
            changed_paths=("src/result.py",),
            checks=("python -m unittest",),
        )

    def _verifier_result(self, verdict: str, summary: str) -> NodeResult:
        status = "succeeded" if verdict == "accepted" else "failed"
        evidence = self.artifacts.put_text(summary, "result.json")
        return NodeResult(
            status,
            summary,
            artifacts={"test-log": evidence, "verdict": evidence},
            actual_model="gpt-5.6-sol",
            result_kind="verifier",
            checks=("python -m unittest",),
            evidence=(evidence,),
            verdict=verdict,
        )


if __name__ == "__main__":
    unittest.main()
