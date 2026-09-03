from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from codex_workbench.scheduler_metrics import (
    build_scheduler_metrics,
    compute_scheduler_metrics,
    execution_lane_for_spec,
)


class SchedulerMetricsTests(unittest.TestCase):
    def test_store_reader_paginates_so_recent_window_events_are_not_truncated(self) -> None:
        now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        events = [
            _event(
                cursor,
                "system.noise",
                "task",
                "spark",
                now - timedelta(hours=2),
                1,
                execution_lane="spark",
            )
            for cursor in range(1, 5_001)
        ]
        events.extend(
            (
                _event(
                    5_001,
                    "node.started",
                    "task",
                    "spark",
                    now - timedelta(minutes=10),
                    1,
                    execution_lane="spark",
                ),
                _event(
                    5_002,
                    "node.accepted",
                    "task",
                    "spark",
                    now - timedelta(minutes=5),
                    1,
                    execution_lane="spark",
                ),
            )
        )

        class Store:
            @staticmethod
            def list_tasks(*, limit: int) -> list[dict[str, object]]:
                del limit
                return [
                    {
                        "task_id": "task",
                        "nodes": [
                            {
                                "node_id": "spark",
                                "executor": "codex",
                                "model": "gpt-5.3-codex-spark",
                                "state": "accepted",
                            }
                        ],
                    }
                ]

            @staticmethod
            def read_events(*, after: int = 0, limit: int = 500) -> list[dict[str, object]]:
                return [event for event in events if int(event["cursor"]) > after][:limit]

        metrics = build_scheduler_metrics(
            Store(),  # type: ignore[arg-type]
            now=now,
            max_workers=4,
            spark_workers=2,
        )

        self.assertEqual(metrics["lanes"]["spark"]["started"], 1)
        self.assertEqual(metrics["lanes"]["spark"]["accepted"], 1)
        self.assertEqual(metrics["lanes"]["spark"]["busy_seconds"], 300.0)

    def test_replay_deduplicates_attempt_events_and_reports_utilization(self) -> None:
        now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        base = now - timedelta(hours=1)
        tasks = [
            {
                "task_id": "task",
                "state": "running",
                "nodes": [
                    {
                        "node_id": "spark",
                        "executor": "codex",
                        "model": "gpt-5.3-codex-spark",
                        "state": "accepted",
                    },
                    {"node_id": "general", "executor": "codex", "model": "gpt-5.6-luna", "state": "running"},
                    {"node_id": "queued", "executor": "codex", "model": "gpt-5.6-luna", "state": "pending"},
                    {
                        "node_id": "dependency-blocked",
                        "executor": "codex",
                        "model": "gpt-5.6-luna",
                        "state": "pending",
                        "depends_on": ["not-accepted"],
                    },
                    {"node_id": "verify", "executor": "codex", "model": "gpt-5.6-sol", "verifier": True, "state": "accepted"},
                ],
            }
        ]
        events = [
            _event(1, "node.started", "task", "spark", base, 1, execution_lane="spark"),
            # A duplicated persisted event never converts one attempt into two starts.
            _event(2, "node.started", "task", "spark", base, 1, execution_lane="spark"),
            _event(3, "node.accepted", "task", "spark", base + timedelta(minutes=30), 1, execution_lane="spark"),
            _event(4, "node.accepted", "task", "spark", base + timedelta(minutes=30), 1, execution_lane="spark"),
            _event(5, "node.started", "task", "general", base + timedelta(minutes=30), 1, execution_lane="general"),
            _event(6, "node.failed", "task", "general", base + timedelta(minutes=45), 1, execution_lane="general"),
            _event(7, "node.retry_scheduled", "task", "general", base + timedelta(minutes=45), 1, execution_lane="general"),
            _event(8, "node.retry_scheduled", "task", "general", base + timedelta(minutes=45), 1, execution_lane="general"),
            _event(9, "node.started", "task", "general", base + timedelta(minutes=50), 2, execution_lane="general"),
            _event(10, "node.started", "task", "verify", base + timedelta(minutes=40), 1, execution_lane="control"),
            _event(11, "node.accepted", "task", "verify", base + timedelta(minutes=50), 1, execution_lane="control"),
            _event(
                12,
                "task.repair_scheduled",
                "task",
                "verify",
                base + timedelta(minutes=55),
                1,
                execution_lane="control",
                attempt_field="verifier_attempt",
            ),
            _event(
                13,
                "task.repair_scheduled",
                "task",
                "verify",
                base + timedelta(minutes=55),
                1,
                execution_lane="control",
                attempt_field="verifier_attempt",
            ),
        ]

        metrics = compute_scheduler_metrics(
            tasks,
            events,
            now=now,
            window_seconds=3600,
            max_workers=4,
            spark_workers=2,
        )

        spark = metrics["lanes"]["spark"]
        self.assertEqual((spark["started"], spark["accepted"]), (1, 1))
        self.assertEqual(spark["busy_seconds"], 1800.0)
        self.assertEqual(spark["utilization"], 0.25)
        self.assertEqual(spark["accepted_per_hour"], 1.0)
        self.assertEqual(spark["quota_pool_ids"], ["codex-spark"])

        general = metrics["lanes"]["general"]
        self.assertEqual((general["queue_depth"], general["inflight"]), (1, 1))
        self.assertEqual(general["dependency_blocked"], 1)
        self.assertEqual((general["started"], general["failed"], general["retry"]), (2, 1, 1))
        self.assertEqual(general["busy_seconds"], 1500.0)
        self.assertEqual(general["utilization"], 0.104167)

        control = metrics["lanes"]["control"]
        self.assertEqual((control["accepted"], control["rework"]), (1, 1))
        self.assertEqual(control["busy_seconds"], 600.0)
        self.assertEqual(metrics["global"]["busy_seconds"], 3900.0)
        self.assertEqual(metrics["global"]["utilization"], 0.270833)
        self.assertEqual(metrics["quota_pools"]["codex-spark"]["status"], "N/A")
        self.assertIsNone(metrics["quota_pools"]["codex-spark"]["remaining"])

    def test_busy_time_uses_only_the_observable_window(self) -> None:
        now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        events = [
            _event(
                1,
                "node.started",
                "task",
                "worker",
                now - timedelta(hours=2),
                1,
                execution_lane="general",
            )
        ]
        metrics = compute_scheduler_metrics(
            [{"task_id": "task", "nodes": [{"node_id": "worker", "model": "gpt-5.6-luna", "state": "running"}]}],
            events,
            now=now,
            window_seconds=3600,
            max_workers=1,
            spark_workers=0,
        )

        self.assertEqual(metrics["lanes"]["general"]["busy_seconds"], 3600.0)
        self.assertEqual(metrics["lanes"]["general"]["utilization"], 1.0)
        self.assertEqual(metrics["lanes"]["spark"]["utilization"], None)

    def test_non_spark_model_cannot_enter_spark_lane_from_a_persisted_label(self) -> None:
        self.assertEqual(
            execution_lane_for_spec(
                {"execution_lane": "spark", "model": "gpt-5.6-luna", "model_profile": "luna_worker"}
            ),
            "general",
        )

    def test_event_payload_cannot_spoof_lane_or_quota_pool_when_spec_exists(self) -> None:
        now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        tasks = [
            {
                "task_id": "task",
                "nodes": [
                    {
                        "node_id": "luna",
                        "executor": "codex",
                        "model": "gpt-5.6-luna",
                        "state": "accepted",
                    }
                ],
            }
        ]
        started = _event(1, "node.started", "task", "luna", now, 1, execution_lane="spark")
        accepted = _event(2, "node.accepted", "task", "luna", now, 1, execution_lane="spark")
        started["payload"]["quota_pool_id"] = "codex-spark"  # type: ignore[index]
        accepted["payload"]["quota_pool_id"] = "codex-spark"  # type: ignore[index]

        metrics = compute_scheduler_metrics(
            tasks,
            [started, accepted],
            now=now,
            max_workers=4,
            spark_workers=2,
        )

        self.assertEqual(metrics["lanes"]["spark"]["started"], 0)
        self.assertEqual(metrics["lanes"]["general"]["started"], 1)
        self.assertEqual(metrics["lanes"]["general"]["accepted"], 1)
        self.assertEqual(metrics["lanes"]["general"]["quota_pool_ids"], ["codex-general"])

    def test_planning_task_is_not_counted_as_claimable_queue_depth(self) -> None:
        metrics = compute_scheduler_metrics(
            [
                {
                    "task_id": "planning",
                    "state": "planning",
                    "nodes": [
                        {
                            "node_id": "worker",
                            "executor": "codex",
                            "model": "gpt-5.6-luna",
                            "state": "pending",
                        }
                    ],
                }
            ],
            [],
            now=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
            max_workers=4,
            spark_workers=2,
        )

        self.assertEqual(metrics["lanes"]["general"]["queue_depth"], 0)
        self.assertEqual(metrics["lanes"]["general"]["dependency_blocked"], 0)


def _event(
    cursor: int,
    event_type: str,
    task_id: str,
    node_id: str,
    at: datetime,
    attempt: int,
    *,
    execution_lane: str,
    attempt_field: str = "attempt",
) -> dict[str, object]:
    return {
        "cursor": cursor,
        "event_type": event_type,
        "task_id": task_id,
        "node_id": node_id,
        "created_at": at.isoformat(),
        "payload": {attempt_field: attempt, "execution_lane": execution_lane},
    }


if __name__ == "__main__":
    unittest.main()
