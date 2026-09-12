from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_workbench.lockfile_importers import (
    WorkspaceImporterRepairError,
    apply_workspace_importer_repair,
    plan_workspace_importer_repair,
)


class WorkspaceImporterRepairTests(unittest.TestCase):
    """Exercise the deterministic, byte-preserving pnpm importer repair path."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self._write_text("pnpm-workspace.yaml", "packages:\n  - packages/*\n")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_text(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_json(self, relative: str, payload: dict) -> None:
        self._write_text(relative, json.dumps(payload, indent=2) + "\n")

    @staticmethod
    def _lock(importers: str) -> str:
        return (
            "lockfileVersion: '9.0'\n"
            "\n"
            "settings:\n"
            "  autoInstallPeers: true\n"
            "\n"
            "importers:\n"
            "\n"
            f"{importers}"
            "\n"
            "packages:\n"
            "\n"
            "  third-party@1.0.0:\n"
            "    resolution: {integrity: sha512-unchanged}\n"
            "\n"
            "snapshots:\n"
            "\n"
            "  third-party@1.0.0: {}\n"
        )

    def _write_root_manifest(self, dependencies: dict[str, str] | None = None) -> None:
        self._write_json(
            "package.json",
            {"name": "@fixture/root", "private": True, "dependencies": dependencies or {}},
        )

    def _write_workspace_package(
        self, directory: str, name: str, dependencies: dict[str, str] | None = None
    ) -> None:
        self._write_json(
            f"{directory}/package.json",
            {"name": name, "version": "1.0.0", "dependencies": dependencies or {}},
        )

    def test_adds_three_workspace_links_without_touching_third_party_or_snapshots(self) -> None:
        self._write_root_manifest(
            {
                "third-party": "^1.0.0",
                "@fixture/a": "workspace:*",
                "@fixture/b": "workspace:^",
                "@fixture/c": "workspace:~",
            }
        )
        self._write_workspace_package("packages/a", "@fixture/a")
        self._write_workspace_package("packages/b", "@fixture/b")
        self._write_workspace_package("packages/c", "@fixture/c")
        before = self._lock(
            "  .:\n"
            "    dependencies:\n"
            "      third-party:\n"
            "        specifier: ^1.0.0\n"
            "        version: 1.0.0\n"
        )
        self._write_text("pnpm-lock.yaml", before)

        plan = plan_workspace_importer_repair(self.root)

        self.assertEqual(plan["before_lockfile"], before.encode("utf-8"))
        self.assertEqual(plan["before_sha256"], sha256(before.encode("utf-8")).hexdigest())
        self.assertEqual(plan["after_sha256"], sha256(plan["new_lockfile"].encode("utf-8")).hexdigest())
        self.assertEqual(plan["input_files"]["pnpm-lock.yaml"], plan["before_sha256"])
        self.assertEqual(
            [entry["name"] for entry in plan["changed_importers"][0]["added"]],
            ["@fixture/a", "@fixture/b", "@fixture/c"],
        )
        self.assertEqual(
            [entry["status"] for entry in plan["workspace_entries"]],
            ["missing", "missing", "missing"],
        )
        self.assertIn(
            "      '@fixture/a':\n"
            "        specifier: workspace:*\n"
            "        version: link:packages/a\n",
            plan["new_lockfile"],
        )
        self.assertIn(
            "      '@fixture/b':\n"
            "        specifier: workspace:^\n"
            "        version: link:packages/b\n",
            plan["new_lockfile"],
        )
        self.assertIn(
            "      '@fixture/c':\n"
            "        specifier: workspace:~\n"
            "        version: link:packages/c\n",
            plan["new_lockfile"],
        )
        self.assertIn(
            "      third-party:\n"
            "        specifier: ^1.0.0\n"
            "        version: 1.0.0\n",
            plan["new_lockfile"],
        )
        self.assertEqual(
            before[before.index("packages:\n") :],
            plan["new_lockfile"][plan["new_lockfile"].index("packages:\n") :],
        )

        receipt = apply_workspace_importer_repair(self.root, plan)

        self.assertEqual((self.root / "pnpm-lock.yaml").read_text(encoding="utf-8"), plan["new_lockfile"])
        self.assertEqual(receipt["after_sha256"], plan["after_sha256"])
        with self.assertRaisesRegex(WorkspaceImporterRepairError, "plan"):
            apply_workspace_importer_repair(self.root, plan)

    def test_creates_workspace_only_importer_with_relative_link(self) -> None:
        self._write_root_manifest()
        self._write_workspace_package("packages/target", "@fixture/target")
        self._write_workspace_package(
            "packages/consumer", "@fixture/consumer", {"@fixture/target": "workspace:^"}
        )
        self._write_text("pnpm-lock.yaml", self._lock("  .: {}\n"))

        plan = plan_workspace_importer_repair(self.root)

        self.assertEqual(plan["changed_importers"][0]["importer"], "packages/consumer")
        self.assertTrue(plan["changed_importers"][0]["created"])
        self.assertIn(
            "  packages/consumer:\n"
            "    dependencies:\n"
            "      '@fixture/target':\n"
            "        specifier: workspace:^\n"
            "        version: link:../target\n",
            plan["new_lockfile"],
        )

    def test_apply_rejects_tampering_and_input_drift(self) -> None:
        self._write_root_manifest({"@fixture/target": "workspace:*"})
        self._write_workspace_package("packages/target", "@fixture/target")
        self._write_text("pnpm-lock.yaml", self._lock("  .:\n    dependencies:\n"))
        plan = plan_workspace_importer_repair(self.root)

        tampered = dict(plan)
        tampered["new_lockfile"] = "tampered\n"
        with self.assertRaisesRegex(WorkspaceImporterRepairError, "complete current input"):
            apply_workspace_importer_repair(self.root, tampered)

        self._write_json(
            "package.json",
            {
                "name": "@fixture/root",
                "private": True,
                "description": "drifted without changing dependencies",
                "dependencies": {"@fixture/target": "workspace:*"},
            },
        )
        with self.assertRaisesRegex(WorkspaceImporterRepairError, "complete current input"):
            apply_workspace_importer_repair(self.root, plan)

    def test_rejects_malformed_lockfile_and_preserves_existing_third_party_drift(self) -> None:
        self._write_root_manifest({"@fixture/target": "workspace:*"})
        self._write_workspace_package("packages/target", "@fixture/target")
        self._write_text(
            "pnpm-lock.yaml",
            self._lock("  .:\n    dependencies: {}\n"),
        )
        with self.assertRaisesRegex(WorkspaceImporterRepairError, "section is malformed"):
            plan_workspace_importer_repair(self.root)

        self._write_root_manifest({"third-party": "^2.0.0"})
        self._write_text(
            "pnpm-lock.yaml",
            self._lock(
                "  .:\n"
                "    dependencies:\n"
                "      third-party:\n"
                "        specifier: ^1.0.0\n"
                "        version: 1.0.0\n"
            ),
        )
        plan = plan_workspace_importer_repair(self.root)
        self.assertEqual(plan["changed_importers"], [])
        self.assertEqual(plan["new_lockfile"], (self.root / "pnpm-lock.yaml").read_text())

    def test_rejects_workspace_escape_and_symbolic_link(self) -> None:
        self._write_root_manifest()
        self._write_text("pnpm-workspace.yaml", "packages:\n  - ../outside/*\n")
        self._write_text("pnpm-lock.yaml", self._lock("  .: {}\n"))
        with self.assertRaisesRegex(WorkspaceImporterRepairError, "escapes"):
            plan_workspace_importer_repair(self.root)

        self._write_text("pnpm-workspace.yaml", "packages:\n  - packages/*\n")
        outside = self.root / "outside"
        outside.mkdir()
        self._write_json("outside/package.json", {"name": "@fixture/outside"})
        packages = self.root / "packages"
        packages.mkdir(exist_ok=True)
        os.symlink(outside, packages / "linked")
        with self.assertRaisesRegex(WorkspaceImporterRepairError, "symbolic links"):
            plan_workspace_importer_repair(self.root)

    def test_rejects_new_importer_that_would_omit_a_third_party_entry(self) -> None:
        self._write_root_manifest()
        self._write_workspace_package("packages/target", "@fixture/target")
        self._write_workspace_package(
            "packages/consumer",
            "@fixture/consumer",
            {"@fixture/target": "workspace:*", "third-party": "^1.0.0"},
        )
        self._write_text("pnpm-lock.yaml", self._lock("  .: {}\n"))

        with self.assertRaisesRegex(WorkspaceImporterRepairError, "third-party"):
            plan_workspace_importer_repair(self.root)

    def test_accepts_workspace_comments_and_deduplicated_dependency_sections(self) -> None:
        self._write_text(
            "pnpm-workspace.yaml",
            "packages:\n"
            "  # pnpm permits comments in its package list.\n"
            "  - packages/*\n",
        )
        self._write_root_manifest()
        (self.root / "packages" / "notes").mkdir(parents=True)
        self._write_workspace_package("packages/target", "@fixture/target")
        self._write_json(
            "packages/consumer/package.json",
            {
                "name": "@fixture/consumer",
                "dependencies": {"@fixture/target": "workspace:^"},
                "devDependencies": {"@fixture/target": "workspace:^"},
            },
        )
        self._write_text(
            "pnpm-lock.yaml",
            self._lock(
                "  .: {}\n"
                "\n"
                "  packages/consumer:\n"
                "    dependencies:\n"
                "      '@fixture/target':\n"
                "        specifier: workspace:^\n"
                "        version: link:../target\n"
            ),
        )

        plan = plan_workspace_importer_repair(self.root)

        self.assertEqual(plan["changed_importers"], [])
        self.assertEqual(plan["new_lockfile"], plan["before_lockfile"].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
