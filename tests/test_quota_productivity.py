from __future__ import annotations

import unittest

from codex_workbench.quota_productivity import compute_quota_productivity


class QuotaProductivityTests(unittest.TestCase):
    def test_counts_only_real_named_windows_and_accepted_claude_tasks(self) -> None:
        snapshots = [
            {
                "observed_at": "2026-08-31T00:00:00+00:00",
                "source": "settings-usage-export",
                "five_hour_window_id": "five-real",
                "weekly_window_id": "week-real",
                "five_hour_remaining": 80,
                "weekly_all_remaining": 75,
            },
            {
                "observed_at": "2026-08-31T02:00:00+00:00",
                "source": "settings-usage-export",
                "five_hour_window_id": "five-real",
                "weekly_window_id": "week-real",
                "five_hour_remaining": 70,
                "weekly_all_remaining": 70,
            },
            {
                "observed_at": "2026-08-31T00:00:00+00:00",
                "source": "acceptance-fixture-v1",
                "five_hour_window_id": "five-fake",
                "weekly_window_id": "week-fake",
                "five_hour_remaining": 90,
                "weekly_all_remaining": 90,
            },
            {
                "observed_at": "2026-08-31T02:00:00+00:00",
                "source": "acceptance-fixture-v1",
                "five_hour_window_id": "five-fake",
                "weekly_window_id": "week-fake",
                "five_hour_remaining": 1,
                "weekly_all_remaining": 1,
            },
        ]
        tasks = [
            {
                "task_id": "claude-task",
                "state": "accepted",
                "contract": {"task_points": 2},
                "nodes": [{"result": {"actual_model": "claude-sonnet-4-5"}}],
            },
            {
                "task_id": "codex-task",
                "state": "accepted",
                "contract": {"task_points": 10},
                "nodes": [{"result": {"actual_model": "gpt-5.6-luna"}}],
            },
        ]
        events = [
            {
                "event_type": "task.state_changed",
                "task_id": "claude-task",
                "created_at": "2026-08-31T01:00:00+00:00",
                "payload": {"to": "accepted"},
            },
            {
                "event_type": "task.state_changed",
                "task_id": "codex-task",
                "created_at": "2026-08-31T01:30:00+00:00",
                "payload": {"to": "accepted"},
            },
        ]

        report = compute_quota_productivity(snapshots, tasks, events)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["accepted_claude_tasks"], 1)
        self.assertEqual(report["measured_windows"], 2)
        self.assertEqual({item["window_id"] for item in report["windows"]}, {"five-real", "week-real"})
        five = next(item for item in report["windows"] if item["kind"] == "five-hour")
        self.assertEqual(five["consumed_percent"], 10)
        self.assertEqual(five["accepted_points"], 2)
        self.assertEqual(five["accepted_points_per_10_percent"], 2)

    def test_does_not_claim_productivity_from_one_sample_or_window_reset(self) -> None:
        snapshots = [
            {
                "observed_at": "2026-08-31T00:00:00+00:00",
                "source": "settings-usage-export",
                "five_hour_window_id": "one",
                "five_hour_remaining": 50,
            },
            {
                "observed_at": "2026-08-31T00:00:00+00:00",
                "source": "settings-usage-export",
                "weekly_window_id": "reset",
                "weekly_all_remaining": 30,
            },
            {
                "observed_at": "2026-08-31T01:00:00+00:00",
                "source": "settings-usage-export",
                "weekly_window_id": "reset",
                "weekly_all_remaining": 80,
            },
        ]

        report = compute_quota_productivity(snapshots, [], [])

        self.assertEqual(report["status"], "insufficient-evidence")
        self.assertEqual(
            {item["status"] for item in report["windows"]},
            {"insufficient-evidence", "invalid-window"},
        )


if __name__ == "__main__":
    unittest.main()
