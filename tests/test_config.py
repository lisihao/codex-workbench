from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from codex_workbench.config import WorkbenchConfig


class WorkbenchConfigTests(unittest.TestCase):
    def test_radar_refresh_defaults_to_one_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = WorkbenchConfig(root)
            configured.initialize()

            loaded = WorkbenchConfig.load(root)
            raw = json.loads(configured.config_file.read_text())

            self.assertEqual(configured.radar_refresh_seconds, 24 * 60 * 60)
            self.assertEqual(loaded.radar_refresh_seconds, 24 * 60 * 60)
            self.assertEqual(raw["radar"]["refresh_interval_seconds"], 24 * 60 * 60)

    def test_spark_workers_defaults_to_a_bounded_lane_and_round_trips_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaulted = WorkbenchConfig(root, max_workers=8)
            self.assertEqual(defaulted.effective_spark_workers, 4)
            configured = WorkbenchConfig(root, max_workers=8, spark_workers=3)
            configured.initialize()

            loaded = WorkbenchConfig.load(root)

            self.assertEqual(loaded.spark_workers, 3)
            self.assertEqual(loaded.effective_spark_workers, 3)

    def test_spark_workers_must_not_exceed_global_executor_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "spark_workers"):
                WorkbenchConfig(Path(directory), max_workers=2, spark_workers=3)

    def test_radar_paths_and_offline_freshness_round_trip_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = WorkbenchConfig(
                root,
                radar_refresh_seconds=1234,
                radar_stale_after_seconds=4321,
                radar_expire_after_seconds=8765,
            )
            configured.initialize()

            loaded = WorkbenchConfig.load(root)
            raw = json.loads(configured.config_file.read_text(encoding="utf-8"))

            self.assertTrue(loaded.radar_enabled)
            self.assertEqual(loaded.effective_radar_state_root, root / "radar")
            self.assertEqual(
                loaded.effective_radar_authorization_file,
                root / "radar" / "authorization.json",
            )
            self.assertEqual(loaded.radar_refresh_seconds, 1234)
            self.assertEqual(loaded.radar_stale_after_seconds, 4321)
            self.assertEqual(loaded.radar_expire_after_seconds, 8765)
            self.assertTrue(raw["radar"]["authority_only"])
            self.assertNotIn("api_key", json.dumps(raw).lower())

    def test_radar_expiry_cannot_precede_stale_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "radar_expire_after_seconds"):
                WorkbenchConfig(
                    Path(directory),
                    radar_stale_after_seconds=10,
                    radar_expire_after_seconds=9,
                )

    def test_ai_frontier_defaults_and_unknown_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = WorkbenchConfig(root)
            configured.initialize()
            raw = json.loads(configured.config_file.read_text(encoding="utf-8"))
            raw["ai_frontier"]["operator_note"] = "keep this"
            configured.config_file.write_text(json.dumps(raw) + "\n", encoding="utf-8")

            loaded = WorkbenchConfig.load(root)
            loaded.initialize()
            rewritten = json.loads(configured.config_file.read_text(encoding="utf-8"))

            self.assertTrue(loaded.ai_frontier_enabled)
            self.assertEqual(loaded.ai_frontier_refresh_seconds, 259200)
            self.assertEqual(loaded.ai_frontier_stale_after_seconds, 7 * 24 * 60 * 60)
            self.assertEqual(loaded.ai_frontier_expire_after_seconds, 31 * 24 * 60 * 60)
            self.assertEqual(loaded.effective_ai_frontier_state_root, root / "ai-frontier")
            self.assertEqual(
                loaded.effective_ai_frontier_authorization_file,
                root / "ai-frontier" / "authorization.json",
            )
            self.assertEqual(rewritten["ai_frontier"]["operator_note"], "keep this")
            self.assertTrue(rewritten["ai_frontier"]["authority_only"])

    def test_ai_frontier_expiry_cannot_precede_stale_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "ai_frontier_expire_after_seconds"):
                WorkbenchConfig(
                    Path(directory),
                    ai_frontier_stale_after_seconds=10,
                    ai_frontier_expire_after_seconds=9,
                )


if __name__ == "__main__":
    unittest.main()
