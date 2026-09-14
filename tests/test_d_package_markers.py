"""Literal DSH package identities required by the closed D repair profile."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench import controlled_validation as validation
from codex_workbench import d_integration_profile as profile
from codex_workbench import d_integration_scope as scope
from codex_workbench.d_integration_metadata import PairingMetadata
from codex_workbench.d_integration_profile import PAIRING_WRITE_ID


LITERAL_PACKAGE_MARKERS = {
    "packages/prompt/task-template/package.json": "@deepseek-ai/dsh-task-template",
    "packages/client/ui-task-template/package.json": "@deepseek-ai/dsh-client-ui-task-template",
    "packages/prompt/task-template-rpc/package.json": "@deepseek-ai/dsh-task-template-rpc",
}
UI_MARKER = "packages/client/ui-task-template/package.json"


class DPackageMarkerTests(unittest.TestCase):
    """Keep the closed profile bound to the deployed DSH package manifests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wb-d-package-markers-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "user.name", "Fixture")
        self._write_literal_markers()
        self._write_validation_inputs()
        self._git("add", ".")
        self._git("commit", "-m", "literal DSH marker fixture")
        self.base_sha = self._git("rev-parse", "HEAD")
        self.runtime = validation.ValidationRuntime(
            self._executable("codex"), self._executable("pnpm"), self._executable("node")
        )

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(self.worktree), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _executable(self, name: str) -> Path:
        target = self.root / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o700)
        return target

    def _write_literal_markers(self) -> None:
        for relative, package_name in LITERAL_PACKAGE_MARKERS.items():
            target = self.worktree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"name": package_name}) + "\n", encoding="utf-8")

    def _write_validation_inputs(self) -> None:
        anchor = self.worktree / "docs/d-package-marker.md"
        anchor.parent.mkdir(parents=True, exist_ok=True)
        anchor.write_text("# D package marker\n", encoding="utf-8")
        anchor.with_name("d-package-marker.zh.md").write_text("# D 包标识\n", encoding="utf-8")
        loader = self.worktree / "node_modules/tsx/dist/esm/index.mjs"
        loader.parent.mkdir(parents=True)
        loader.write_text("export {}\n", encoding="utf-8")
        script = self.worktree / "scripts/verify-translation-pairing.ts"
        script.parent.mkdir(parents=True)
        script.write_text("export {}\n", encoding="utf-8")

    def _scope_binding(self) -> tuple[dict[str, object], dict[str, object]]:
        allocation = {
            "current_path": str(self.worktree),
            "base_sha": self.base_sha,
            "branch": "main",
        }
        return (
            {
                "task": {"contract": {"repository": str(self.worktree)}},
                "d": {"allocation": allocation},
            },
            {"allocation": allocation},
        )

    def _write_ui_marker(self, package_name: str) -> None:
        (self.worktree / UI_MARKER).write_text(
            json.dumps({"name": package_name}) + "\n", encoding="utf-8"
        )

    def test_profile_has_exact_literal_manifest_mapping(self) -> None:
        self.assertEqual(profile.REQUIRED_PACKAGE_MARKERS, LITERAL_PACKAGE_MARKERS)

    def test_scope_source_identity_binds_literal_manifest_names(self) -> None:
        binding, _source_snapshot = self._scope_binding()
        identity = scope._source_identity(binding)
        self.assertEqual(
            {path: marker["name"] for path, marker in identity["package_markers"].items()},
            LITERAL_PACKAGE_MARKERS,
        )

    def test_metadata_plan_binds_literal_manifest_names(self) -> None:
        plan = validation.plan_validation(
            self.worktree,
            PAIRING_WRITE_ID,
            self.runtime,
            metadata=PairingMetadata(("docs/d-package-marker.md",)),
        )
        assert plan.metadata is not None
        self.assertEqual(
            {
                marker.path: marker.expected_package_name
                for marker in plan.metadata.profile_markers
            },
            LITERAL_PACKAGE_MARKERS,
        )

    def test_scope_rejects_wrong_and_arbitrary_ui_manifest_names(self) -> None:
        binding, _source_snapshot = self._scope_binding()
        for package_name in (
            "@deepseek-ai/dsh-ui-task-template",
            "@example.invalid/arbitrary-package",
        ):
            with self.subTest(package_name=package_name):
                self._write_ui_marker(package_name)
                with self.assertRaisesRegex(
                    scope.IntegrationScopeAmendmentError,
                    "required package marker name is invalid",
                ):
                    scope._source_identity(binding)
                self._write_ui_marker(LITERAL_PACKAGE_MARKERS[UI_MARKER])

    def test_metadata_rejects_wrong_and_arbitrary_ui_manifest_names(self) -> None:
        request = PairingMetadata(("docs/d-package-marker.md",))
        for package_name in (
            "@deepseek-ai/dsh-ui-task-template",
            "@example.invalid/arbitrary-package",
        ):
            with self.subTest(package_name=package_name):
                self._write_ui_marker(package_name)
                with self.assertRaisesRegex(
                    validation.ControlledValidationError,
                    "D profile package marker does not match",
                ):
                    validation.plan_validation(
                        self.worktree, PAIRING_WRITE_ID, self.runtime, metadata=request
                    )
                self._write_ui_marker(LITERAL_PACKAGE_MARKERS[UI_MARKER])


if __name__ == "__main__":
    unittest.main()
