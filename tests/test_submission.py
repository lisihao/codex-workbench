from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeSpec, QuotaSnapshot
from codex_workbench.store import WorkbenchStore
from codex_workbench.submission import submit_natural_language_request


class SubmissionTests(unittest.TestCase):
    def test_yellow_zone_exposes_only_authenticated_sonnet_to_planner(self) -> None:
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

            config = WorkbenchConfig(root / "state")
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            store.write_quota(
                QuotaSnapshot(
                    observed_at="2026-08-26T00:00:00+00:00",
                    auth_ok=True,
                    auth_method="native-subscription",
                    five_hour_remaining=35,
                    weekly_all_remaining=60,
                    weekly_sonnet_remaining=60,
                    weekly_fable_remaining=60,
                    source="settings-usage",
                )
            )
            planned = [
                NodeSpec(
                    "verify",
                    "task-yellow",
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    verifier=True,
                )
            ]
            with (
                patch("codex_workbench.submission.ClaudeExecutor.authentication", return_value=(True, "native-subscription")),
                patch("codex_workbench.submission.CodexPlanner.compile", return_value=planned) as compile_plan,
            ):
                result = submit_natural_language_request(
                    config,
                    store,
                    objective="bounded work",
                    repository=str(repository),
                    allowed_scope=("README.md",),
                    task_id="task-yellow",
                    queue=False,
                )

            self.assertEqual(result["claude_models_available"], ("sonnet",))
            self.assertEqual(compile_plan.call_args.kwargs["claude_models_available"], ("sonnet",))


if __name__ == "__main__":
    unittest.main()
