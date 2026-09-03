from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_workbench.config import WorkbenchConfig


class WorkbenchConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
