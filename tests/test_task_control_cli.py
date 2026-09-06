from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, call, patch

from codex_workbench.cli import build_parser, command_task


class TaskControlCLITests(unittest.TestCase):
    @staticmethod
    def _run(args: object, store: Mock) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with patch("codex_workbench.cli._config", return_value=object()), patch(
            "codex_workbench.cli._store", return_value=store
        ), redirect_stdout(output):
            code = command_task(args)
        return code, json.loads(output.getvalue())

    def test_lifecycle_controls_require_expected_revision(self) -> None:
        for action in ("queue", "resume", "pause", "cancel"):
            with self.subTest(action=action), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["task", action, "task-1"])

    def test_queue_resume_pause_cancel_and_steer_forward_revision_and_receipts(self) -> None:
        store = Mock()
        store.queue_task.return_value = 4
        store.queue_task_with_instruction.return_value = {
            "task_id": "task-1",
            "state": "queued",
            "revision": 6,
            "steering": {"steering_id": "steering-1", "instruction": "keep scope"},
        }
        store.transition_task.side_effect = [7, 8]
        store.append_task_steering_receipt.return_value = {
            "task_id": "task-1",
            "steering_id": "steering-2",
            "revision": 9,
            "delivery": {"mode": "future_attempts_only"},
        }

        parser = build_parser()
        code, queued = self._run(
            parser.parse_args(["task", "queue", "task-1", "--expected-revision", "3"]),
            store,
        )
        self.assertEqual(code, 0)
        self.assertEqual(queued["revision"], 4)
        store.queue_task.assert_called_once_with("task-1", expected_revision=3)

        code, resumed = self._run(
            parser.parse_args(
                [
                    "task",
                    "resume",
                    "task-1",
                    "--expected-revision",
                    "4",
                    "--instruction",
                    "keep scope",
                ]
            ),
            store,
        )
        self.assertEqual(code, 0)
        self.assertEqual(resumed["steering"]["instruction"], "keep scope")
        store.queue_task_with_instruction.assert_called_once_with(
            "task-1", "keep scope", expected_revision=4
        )

        code, paused = self._run(
            parser.parse_args(["task", "pause", "task-1", "--expected-revision", "6"]),
            store,
        )
        self.assertEqual(code, 0)
        self.assertEqual(paused["revision"], 7)
        code, cancelled = self._run(
            parser.parse_args(["task", "cancel", "task-1", "--expected-revision", "7"]),
            store,
        )
        self.assertEqual(code, 0)
        self.assertEqual(cancelled["revision"], 8)
        self.assertEqual(
            store.transition_task.call_args_list,
            [
                call("task-1", "paused", expected_revision=6),
                call("task-1", "cancelled", expected_revision=7),
            ],
        )

        code, steered = self._run(
            parser.parse_args(
                ["task", "steer", "task-1", "follow up", "--expected-revision", "8"]
            ),
            store,
        )
        self.assertEqual(code, 0)
        self.assertEqual(steered["steering_id"], "steering-2")
        store.append_task_steering_receipt.assert_called_once_with(
            "task-1", "follow up", expected_revision=8
        )
        store.get_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
