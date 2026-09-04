from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.model import QuotaSnapshot
from codex_workbench.performance import (
    PerformanceRegistry,
    build_performance_snapshot,
    load_benchmark_baseline,
    read_all_events,
)


def event(
    cursor: int,
    event_type: str,
    *,
    task_id: str | None = None,
    node_id: str | None = None,
    payload: dict[str, object] | None = None,
    created_at: str = "2026-09-03T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "cursor": cursor,
        "event_type": event_type,
        "task_id": task_id,
        "node_id": node_id,
        "payload": payload or {},
        "created_at": created_at,
    }


def result(
    status: str,
    model: str = "gpt-5.6-luna",
    *,
    agent_version: str = "0.153.0",
    provider: str = "codex",
) -> dict[str, object]:
    return {
        "status": status,
        "summary": status,
        "actual_model": model,
        "exit_code": 0,
        "provider": provider,
        "agent_name": provider,
        "agent_version": agent_version,
        "result_kind": "worker",
    }


def task(
    task_id: str,
    *,
    state: str = "accepted",
    executor: str = "codex",
    model: str = "gpt-5.6-luna",
    verifier: bool = False,
    task_type: str = "implementation",
    complexity: str = "standard",
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    node: dict[str, object] = {
        "node_id": "work",
        "executor": executor,
        "model": model,
        "verifier": verifier,
        "task_type": task_type,
        "complexity": complexity,
    }
    if reasoning_effort is not None:
        node["model_reasoning_effort"] = reasoning_effort
    return {
        "task_id": task_id,
        "state": state,
        "contract": {"task_type": task_type, "complexity": complexity},
        "nodes": [node],
    }


def catalog() -> dict[str, object]:
    return {
        "catalog_id": "catalog-performance-test",
        "digest": "a" * 64,
        "agents": {
            "codex": {"cli_version": "0.153.0"},
            "claude": {"cli_version": "2.1.42"},
        },
        "models": [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-luna",
                "model_family": "luna",
                "agent_cli_version": "0.153.0",
                "routable": True,
            },
            {
                "provider": "codex",
                "model_id": "gpt-5.3-codex-spark",
                "model_family": "spark",
                "agent_cli_version": "0.153.0",
                "routable": True,
            },
        ],
    }


def compatible_quota(
    *,
    remaining: float = 80,
    five_hour_window_id: str = "five-a",
    weekly_window_id: str = "week-a",
    observed_at: str = "2026-09-03T00:00:00+00:00",
) -> QuotaSnapshot:
    return QuotaSnapshot(
        observed_at=observed_at,
        auth_ok=True,
        auth_method="native-subscription",
        five_hour_remaining=remaining,
        weekly_all_remaining=70,
        weekly_sonnet_remaining=70,
        source=COMPATIBLE_SOURCE,
        five_hour_window_id=five_hour_window_id,
        weekly_window_id=weekly_window_id,
        producer=PRODUCER,
        producer_schema_version=PRODUCER_SCHEMA_VERSION,
        claude_version=SUPPORTED_USAGE_VERSION,
    )


@dataclass
class FakeQuota:
    auth_ok: bool = True
    auth_method: str = "native-subscription"
    five_hour_remaining: float = 99
    weekly_all_remaining: float = 99
    weekly_sonnet_remaining: float = 99
    weekly_fable_remaining: float | None = None
    observed_at: str = "2026-09-03T00:00:00+00:00"
    source: str = "not-a-compatible-producer"
    producer: str = "fixture"
    claude_version: str = "fixture"

    def has_compatible_subscription_provenance(self) -> bool:
        return False


class FakeStore:
    def __init__(
        self,
        events: list[dict[str, object]],
        tasks: list[dict[str, object]],
        *,
        quota: object | None = None,
        list_tasks: bool = True,
    ) -> None:
        self.events = events
        self.tasks = {str(item["task_id"]): item for item in tasks}
        self.quota = quota
        self.list_tasks_enabled = list_tasks
        self.read_calls: list[tuple[int, int]] = []

    def read_events(self, *, after: int = 0, limit: int = 500) -> list[dict[str, object]]:
        self.read_calls.append((after, limit))
        return [item for item in self.events if int(item["cursor"]) > after][:limit]

    def list_tasks(self, limit: int = 100) -> list[dict[str, object]]:
        values = list(self.tasks.values()) if self.list_tasks_enabled else []
        return values[:limit]

    def get_task(self, task_id: str) -> dict[str, object]:
        return self.tasks[task_id]

    def latest_quota(self) -> object | None:
        return self.quota


class PerformanceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cold_start_uses_domain_specific_priors_and_spark_never_gets_fake_quality_or_quota(self) -> None:
        store = FakeStore([], [], quota=FakeQuota())
        registry = PerformanceRegistry(self.root)

        refreshed = registry.refresh(store, catalog())
        coding = registry.calibrate(catalog(), "implementation", "standard")
        reasoning = registry.calibrate(catalog(), "architecture", "high")

        self.assertTrue(refreshed["ok"])
        snapshot = refreshed["snapshot"]
        self.assertEqual(snapshot["pools"]["codex"]["remaining"], None)
        self.assertEqual(snapshot["pools"]["spark"]["remaining"], None)
        self.assertEqual(snapshot["pools"]["spark"]["remaining_display"], "N/A")
        self.assertEqual(snapshot["pools"]["claude"]["remaining"], None)

        luna_coding = next(item for item in coding["candidates"] if item["model_id"] == "gpt-5.6-luna")
        self.assertEqual(luna_coding["quality"]["prior"]["kind"], "benchmark-backed-weak-prior")
        self.assertEqual(
            {item["benchmark"] for item in luna_coding["quality"]["prior"]["evidence"]},
            {"SWE-Bench Pro", "Terminal-Bench"},
        )
        luna_reasoning = next(item for item in reasoning["candidates"] if item["model_id"] == "gpt-5.6-luna")
        self.assertEqual(
            {item["benchmark"] for item in luna_reasoning["quality"]["prior"]["evidence"]},
            {"Agents' Last Exam"},
        )
        spark = next(item for item in coding["candidates"] if item["model_id"] == "gpt-5.3-codex-spark")
        self.assertEqual(spark["quality"]["prior"]["kind"], "declarative-conservative-prior")
        self.assertEqual(spark["quality"]["prior"]["evidence_status"], "unavailable")
        self.assertEqual(spark["quality"]["posterior"]["runtime_sample_count"], 0)
        self.assertEqual(spark["runtime"]["attempt_count"], 0)
        self.assertEqual(spark["runtime"]["first_pass"]["rate"], None)
        self.assertEqual(spark["runtime"]["rework_count"], 0)
        self.assertEqual(spark["runtime"]["duration_seconds"], {
            "sample_count": 0,
            "mean": None,
            "p50": None,
        })

    def test_refresh_reads_the_full_paginated_event_ledger_and_recovers_tasks_by_event_id(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(3, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(4, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
            event(5, "system.noise"),
        ]
        store = FakeStore(events, [task("task-a")], list_tasks=False)
        registry = PerformanceRegistry(self.root, event_page_size=2)

        refreshed = registry.refresh(store, catalog())

        self.assertTrue(refreshed["ok"])
        self.assertGreaterEqual(len(store.read_calls), 3)
        self.assertEqual(refreshed["snapshot"]["event_cursor"], 4)
        self.assertEqual(refreshed["snapshot"]["ledger"]["calibration_event_count"], 3)
        self.assertEqual(refreshed["scan_progress"]["scanned_event_cursor"], 5)
        self.assertEqual(refreshed["scan_progress"]["events_read"], 5)
        self.assertEqual(refreshed["scan_progress"]["tasks_read"], 1)
        metric = refreshed["snapshot"]["metrics"][0]
        self.assertEqual(metric["runtime"]["attempt_count"], 1)
        self.assertEqual(metric["runtime"]["final_acceptance"]["rate"], 1)

    def test_rework_is_penalized_and_agent_versions_are_isolated(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}, created_at="2026-09-03T00:00:00+00:00"),
            event(3, "node.failed", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("failed", agent_version="0.153.0")}, created_at="2026-09-03T00:00:10+00:00"),
            event(4, "node.retry_scheduled", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(5, "node.started", task_id="task-a", node_id="work", payload={"attempt": 2}, created_at="2026-09-03T00:00:20+00:00"),
            event(6, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 2, "result": result("succeeded", agent_version="0.153.0")}, created_at="2026-09-03T00:00:40+00:00"),
            event(7, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
            event(8, "task.created", task_id="task-b"),
            event(9, "node.started", task_id="task-b", node_id="work", payload={"attempt": 1}),
            event(10, "node.accepted", task_id="task-b", node_id="work", payload={"attempt": 1, "result": result("succeeded", agent_version="0.154.0")}),
            event(11, "task.state_changed", task_id="task-b", payload={"to": "accepted"}),
        ]
        snapshot = build_performance_snapshot(
            events,
            [task("task-a"), task("task-b")],
            catalog(),
        )
        metrics = {
            item["key"]["agent_version"]: item
            for item in snapshot["metrics"]
        }
        retried = metrics["0.153.0"]
        clean = metrics["0.154.0"]

        self.assertEqual(retried["runtime"]["first_pass"]["rate"], 0)
        self.assertEqual(retried["runtime"]["final_acceptance"]["rate"], 1)
        self.assertEqual(retried["runtime"]["rework_count"], 1)
        self.assertEqual(retried["runtime"]["quality_rework_count"], 1)
        self.assertEqual(retried["runtime"]["retry_scheduled_count"], 1)
        self.assertEqual(retried["runtime"]["quality_calibration"]["failures"], 1)
        self.assertEqual(retried["runtime"]["duration_seconds"]["mean"], 15)
        self.assertEqual(clean["runtime"]["quality_calibration"]["sample_count"], 1)
        self.assertGreater(clean["posterior"]["mean"], retried["posterior"]["mean"])

    def test_runtime_metrics_and_calibration_are_isolated_by_reasoning_effort(self) -> None:
        effort_catalog = catalog()
        effort_catalog["models"][0]["reasoning"] = {
            "preferred_effort": "max",
            "supported_efforts": ["high", "max"],
        }
        events = [
            event(1, "node.started", task_id="max-task", node_id="work", payload={"attempt": 1}),
            event(2, "node.accepted", task_id="max-task", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(3, "task.state_changed", task_id="max-task", payload={"to": "accepted"}),
            event(4, "node.started", task_id="high-task", node_id="work", payload={"attempt": 1}),
            event(5, "node.failed", task_id="high-task", node_id="work", payload={"attempt": 1, "result": result("failed")}),
            event(6, "task.state_changed", task_id="high-task", payload={"to": "needs_fix"}),
        ]
        registry = PerformanceRegistry(self.root)
        refreshed = registry.refresh(
            FakeStore(
                events,
                [
                    task("max-task", reasoning_effort="max"),
                    task("high-task", state="needs_fix", reasoning_effort="high"),
                ],
            ),
            effort_catalog,
        )

        metrics = {
            item["key"]["reasoning_effort"]: item
            for item in refreshed["snapshot"]["metrics"]
        }
        self.assertEqual(set(metrics), {"high", "max"})
        self.assertEqual(metrics["max"]["runtime"]["quality_calibration"]["successes"], 1)
        self.assertEqual(metrics["high"]["runtime"]["quality_calibration"]["failures"], 1)

        calibration = registry.calibrate(effort_catalog, "implementation", "standard")
        luna = next(item for item in calibration["candidates"] if item["model_id"] == "gpt-5.6-luna")
        self.assertEqual(luna["reasoning_effort"], "max")
        self.assertEqual(luna["quality"]["posterior"]["runtime_sample_count"], 1)
        self.assertEqual(luna["quality"]["posterior"]["runtime_successes"], 1)
        self.assertEqual(luna["quality"]["posterior"]["runtime_failures"], 0)

    def test_retry_uses_the_effective_started_effort_instead_of_the_original_node_spec(self) -> None:
        events = [
            event(
                1,
                "node.started",
                task_id="retry-effort",
                node_id="work",
                payload={"attempt": 1, "model_reasoning_effort": "xhigh"},
            ),
            event(
                2,
                "node.failed",
                task_id="retry-effort",
                node_id="work",
                payload={
                    "attempt": 1,
                    "result": result("failed", model="gpt-5.3-codex-spark"),
                },
            ),
            event(3, "node.retry_scheduled", task_id="retry-effort", node_id="work", payload={"attempt": 1}),
            event(
                4,
                "node.started",
                task_id="retry-effort",
                node_id="work",
                payload={"attempt": 2, "model_reasoning_effort": "max"},
            ),
            event(
                5,
                "node.accepted",
                task_id="retry-effort",
                node_id="work",
                payload={"attempt": 2, "result": result("succeeded")},
            ),
            event(6, "task.state_changed", task_id="retry-effort", payload={"to": "accepted"}),
        ]

        snapshot = build_performance_snapshot(
            events,
            [
                task(
                    "retry-effort",
                    model="gpt-5.3-codex-spark",
                    reasoning_effort="xhigh",
                )
            ],
            catalog(),
        )
        identities = {
            (metric["key"]["model_id"], metric["key"]["reasoning_effort"])
            for metric in snapshot["metrics"]
        }

        self.assertEqual(
            identities,
            {
                ("gpt-5.3-codex-spark", "xhigh"),
                ("gpt-5.6-luna", "max"),
            },
        )

    def test_nonzero_process_failure_is_operational_not_model_quality_rework(self) -> None:
        failed = result("failed")
        failed["exit_code"] = 1
        events = [
            event(1, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(2, "node.failed", task_id="task-a", node_id="work", payload={"attempt": 1, "result": failed}),
            event(3, "node.retry_scheduled", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(4, "node.started", task_id="task-a", node_id="work", payload={"attempt": 2}),
            event(5, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 2, "result": result("succeeded")}),
            event(6, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
        ]

        snapshot = build_performance_snapshot(events, [task("task-a")], catalog())
        metric = snapshot["metrics"][0]

        self.assertEqual(metric["runtime"]["attempt_count"], 2)
        self.assertEqual(metric["runtime"]["rework_count"], 1)
        self.assertEqual(metric["runtime"]["quality_rework_count"], 0)
        self.assertEqual(metric["runtime"]["quality_calibration"]["successes"], 1)
        self.assertEqual(metric["runtime"]["quality_calibration"]["failures"], 0)
        self.assertEqual(metric["runtime"]["quality_calibration"]["unresolved"], 1)

    def test_calibrate_exposes_runtime_metrics_and_matrix_keeps_dag_contexts_exact(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}, created_at="2026-09-03T00:00:00+00:00"),
            event(3, "node.failed", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("failed")}, created_at="2026-09-03T00:00:10+00:00"),
            event(4, "node.retry_scheduled", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(5, "node.started", task_id="task-a", node_id="work", payload={"attempt": 2}, created_at="2026-09-03T00:00:20+00:00"),
            event(6, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 2, "result": result("succeeded")}, created_at="2026-09-03T00:00:40+00:00"),
            event(7, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
        ]
        registry = PerformanceRegistry(self.root)
        refreshed = registry.refresh(FakeStore(events, [task("task-a")]), catalog())

        calibration = registry.calibrate(catalog(), "implementation", "standard")
        luna = next(item for item in calibration["candidates"] if item["model_id"] == "gpt-5.6-luna")
        self.assertEqual(luna["runtime"]["first_pass"]["rate"], 0)
        self.assertEqual(luna["runtime"]["final_acceptance"]["rate"], 1)
        self.assertEqual(luna["runtime"]["rework_count"], 1)
        self.assertEqual(luna["runtime"]["retry_scheduled_count"], 1)
        self.assertEqual(luna["runtime"]["duration_seconds"]["mean"], 15)

        matrix = registry.calibrate_matrix(
            catalog(),
            ["implementation", "architecture", "implementation"],
            ["standard", "high", "standard"],
        )
        contexts = matrix["contexts"]
        self.assertEqual(
            [(item["task_type"], item["complexity"]) for item in contexts],
            [
                ("architecture", "high"),
                ("architecture", "standard"),
                ("implementation", "high"),
                ("implementation", "standard"),
            ],
        )
        self.assertEqual(matrix["snapshot_id"], refreshed["active_generation_id"])
        self.assertEqual({item["snapshot_id"] for item in contexts}, {refreshed["active_generation_id"]})
        architecture_standard = next(
            item
            for item in contexts
            if item["task_type"] == "architecture" and item["complexity"] == "standard"
        )
        architecture_luna = next(
            item for item in architecture_standard["candidates"] if item["model_id"] == "gpt-5.6-luna"
        )
        self.assertEqual(architecture_luna["runtime"]["attempt_count"], 0)

    def test_terminal_bench_claude_code_family_priors_cover_sonnet_alias(self) -> None:
        alias_catalog = catalog()
        alias_catalog["models"] = [
            *alias_catalog["models"],
            {
                "provider": "claude",
                "model_id": "sonnet",
                "model_family": "sonnet",
                "agent_cli_version": "2.1.42",
                "routable": True,
            },
            {
                "provider": "claude",
                "model_id": "claude-opus-4-8",
                "model_family": "opus",
                "agent_cli_version": "2.1.42",
                "routable": True,
            },
        ]
        registry = PerformanceRegistry(self.root)

        calibration = registry.calibrate(alias_catalog, "implementation", "standard")
        sonnet = next(item for item in calibration["candidates"] if item["model_id"] == "sonnet")
        opus = next(item for item in calibration["candidates"] if item["model_id"] == "claude-opus-4-8")

        self.assertEqual(sonnet["quality"]["prior"]["kind"], "benchmark-backed-weak-prior")
        sonnet_evidence = sonnet["quality"]["prior"]["evidence"]
        self.assertEqual(
            [item["record_id"] for item in sonnet_evidence],
            ["terminal-bench-claude-code-sonnet-4-6-2-1"],
        )
        self.assertEqual(sonnet_evidence[0]["match_kind"], "family-transfer")
        self.assertEqual(sonnet_evidence[0]["effective_sample_strength"], 0.125)
        self.assertNotIn(
            "terminal-bench-claude-code-opus-4-6-2-1",
            [item["record_id"] for item in opus["quality"]["prior"]["evidence"]],
        )
        self.assertTrue(
            all(item["match_kind"] == "exact-model" for item in opus["quality"]["prior"]["evidence"])
        )

    def test_fixture_deterministic_verifier_reused_and_unattested_samples_are_excluded(self) -> None:
        events = [
            event(1, "node.accepted", task_id="fixture", node_id="work", payload={"attempt": 1, "result": result("succeeded", model="fixture")}),
            event(2, "node.accepted", task_id="deterministic", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(3, "node.accepted", task_id="verifier", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(4, "node.evidence_reused", task_id="reused", node_id="work"),
            event(5, "node.accepted", task_id="reused", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(6, "node.accepted", task_id="unattested", node_id="work", payload={"attempt": 1, "result": {"status": "succeeded"}}),
        ]
        snapshot = build_performance_snapshot(
            events,
            [
                task("fixture", executor="fixture", model="fixture"),
                task("deterministic", executor="deterministic", model="none"),
                task("verifier", verifier=True),
                task("reused"),
                task("unattested"),
            ],
            catalog(),
        )

        self.assertEqual(snapshot["metrics"], [])
        exclusions = snapshot["ledger"]["excluded_terminal_attempts"]
        self.assertEqual(exclusions["fixture_deterministic_or_verifier"], 3)
        self.assertEqual(exclusions["evidence_reused"], 1)
        self.assertEqual(exclusions["missing_actual_model"], 1)

    def test_unattested_agent_version_is_operational_only(self) -> None:
        snapshot = build_performance_snapshot(
            [
                event(
                    1,
                    "node.accepted",
                    task_id="unattested-version",
                    node_id="work",
                    payload={
                        "attempt": 1,
                        "result": result("succeeded", agent_version="unattested"),
                    },
                )
            ],
            [task("unattested-version")],
            catalog(),
        )

        metric = snapshot["metrics"][0]
        self.assertEqual(metric["runtime"]["attempt_count"], 1)
        self.assertEqual(metric["runtime"]["quality_calibration"]["sample_count"], 0)
        self.assertEqual(metric["runtime"]["quality_calibration"]["unresolved"], 1)
        self.assertEqual(metric["posterior"]["runtime_sample_count"], 0)

    def test_refresh_is_atomic_content_addressed_and_reuses_unchanged_generation(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(3, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(4, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
        ]
        registry = PerformanceRegistry(self.root)
        store = FakeStore(events, [task("task-a")])

        first = registry.refresh(store, catalog())
        second = registry.refresh(store, catalog())
        active = registry.active()
        path = self.root / "performance" / "generations" / f"{first['active_generation_id']}.json"

        self.assertTrue(first["activated"])
        self.assertTrue(second["unchanged"])
        self.assertFalse(second["activated"])
        self.assertEqual(active["snapshot_id"], first["active_generation_id"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "performance" / "active.json").stat().st_mode & 0o777, 0o600)

    def test_noise_and_unchanged_quota_heartbeat_do_not_create_a_generation(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(3, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(4, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
        ]
        store = FakeStore(events, [task("task-a")], quota=compatible_quota())
        registry = PerformanceRegistry(self.root)

        first = registry.refresh(store, catalog())
        store.events.extend([
            event(5, "quota.updated", payload={"provider": "claude"}),
            event(6, "system.noise"),
        ])
        store.quota = compatible_quota(observed_at="2026-09-03T00:01:00+00:00")
        second = registry.refresh(store, catalog())

        self.assertTrue(second["unchanged"])
        self.assertFalse(second["activated"])
        self.assertEqual(second["active_generation_id"], first["active_generation_id"])
        self.assertEqual(registry.status()["generation_count"], 1)
        self.assertEqual(second["scan_progress"]["scanned_event_cursor"], 6)
        self.assertEqual(second["scan_progress"]["claude_observed_at"], "2026-09-03T00:01:00+00:00")

    def test_pending_task_without_terminal_evidence_does_not_create_a_generation(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(3, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(4, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
        ]
        store = FakeStore(events, [task("task-a")])
        registry = PerformanceRegistry(self.root)

        first = registry.refresh(store, catalog())
        store.tasks["task-pending"] = task("task-pending", state="queued")
        store.events.append(event(5, "task.created", task_id="task-pending"))
        second = registry.refresh(store, catalog())

        self.assertTrue(second["unchanged"])
        self.assertEqual(second["active_generation_id"], first["active_generation_id"])
        self.assertEqual(registry.status()["generation_count"], 1)
        self.assertEqual(second["scan_progress"]["tasks_read"], 2)
        self.assertEqual(second["scan_progress"]["calibration_cursor"], 4)

    def test_terminal_and_quota_balance_or_window_changes_create_generations(self) -> None:
        events = [
            event(1, "task.created", task_id="task-a"),
            event(2, "node.started", task_id="task-a", node_id="work", payload={"attempt": 1}),
            event(3, "node.accepted", task_id="task-a", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(4, "task.state_changed", task_id="task-a", payload={"to": "accepted"}),
        ]
        store = FakeStore(events, [task("task-a")], quota=compatible_quota())
        registry = PerformanceRegistry(self.root)

        first = registry.refresh(store, catalog())
        store.quota = compatible_quota(remaining=70)
        balance_changed = registry.refresh(store, catalog())
        store.quota = compatible_quota(remaining=70, five_hour_window_id="five-b")
        window_changed = registry.refresh(store, catalog())
        store.tasks["task-b"] = task("task-b")
        store.events.extend([
            event(5, "task.created", task_id="task-b"),
            event(6, "node.started", task_id="task-b", node_id="work", payload={"attempt": 1}),
            event(7, "node.accepted", task_id="task-b", node_id="work", payload={"attempt": 1, "result": result("succeeded")}),
            event(8, "task.state_changed", task_id="task-b", payload={"to": "accepted"}),
        ])
        terminal_changed = registry.refresh(store, catalog())

        self.assertNotEqual(balance_changed["active_generation_id"], first["active_generation_id"])
        self.assertTrue(balance_changed["activated"])
        self.assertFalse(balance_changed["unchanged"])
        self.assertNotEqual(window_changed["active_generation_id"], balance_changed["active_generation_id"])
        self.assertTrue(window_changed["activated"])
        self.assertNotEqual(terminal_changed["active_generation_id"], window_changed["active_generation_id"])
        self.assertTrue(terminal_changed["activated"])
        self.assertEqual(registry.status()["generation_count"], 4)

    def test_baseline_contains_a_non_routable_hle_reasoning_reference(self) -> None:
        baseline = load_benchmark_baseline()
        hle = next(item for item in baseline["records"] if item["benchmark"] == "Humanity's Last Exam")

        self.assertEqual(hle["domain"], "reasoning")
        self.assertFalse(hle["routing_prior_eligible"])
        self.assertEqual(hle["provenance"], "independent")

    def test_compatible_claude_quota_is_reported_without_filling_codex_or_spark(self) -> None:
        quota = compatible_quota()

        snapshot = build_performance_snapshot([], [], catalog(), quota=quota)

        self.assertEqual(snapshot["pools"]["claude"]["status"], "observed")
        self.assertEqual(snapshot["pools"]["claude"]["remaining"]["five_hour"], 80)
        self.assertIsNone(snapshot["pools"]["codex"]["remaining"])
        self.assertIsNone(snapshot["pools"]["spark"]["remaining"])


if __name__ == "__main__":
    unittest.main()
