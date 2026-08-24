from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.executors import codex_subscription_environment
from codex_workbench.model import NodeSpec, QuotaSnapshot, TaskContract
from codex_workbench.planner import PLAN_SCHEMA


class ModelTests(unittest.TestCase):
    def test_planner_schema_requires_every_declared_property(self) -> None:
        item = PLAN_SCHEMA["properties"]["nodes"]["items"]
        self.assertEqual(set(item["required"]), set(item["properties"]))

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


if __name__ == "__main__":
    unittest.main()
