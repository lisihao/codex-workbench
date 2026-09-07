from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_workbench.execution_readiness import (
    ExecutionReadinessRequest,
    PNPM_MINIMUM_11_VERSION,
    PathDependencyRequirement,
    PnpmRequirement,
    SourceResolutionRequirement,
    ToolRequirement,
    assess_execution_readiness,
)


class ExecutionReadinessTests(unittest.TestCase):
    def test_non_node_worktree_is_ready_without_launching_a_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            calls: list[tuple[str, ...]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(tuple(command))
                raise AssertionError("a non-Node readiness check must not launch a probe")

            report = assess_execution_readiness(
                ExecutionReadinessRequest(worktree=worktree),
                runner=runner,
            )

        self.assertTrue(report.ready, report.to_dict())
        self.assertEqual(report.failure_origin, None)
        self.assertEqual(calls, [])
        pnpm = next(check for check in report.checks if check.check_id == "pnpm:manifest")
        self.assertEqual(pnpm.status, "not-applicable")
        self.assertTrue(pnpm.detail["non_node_usable"])

    def test_pnpm_probe_accepts_complete_root_and_package_local_linkers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            self._pnpm_project(worktree)
            (worktree / "node_modules" / ".modules.yaml").parent.mkdir()
            (worktree / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n")
            (worktree / "node_modules" / ".bin").mkdir()
            (worktree / "packages" / "fixture" / "node_modules").mkdir(parents=True)
            calls: list[dict[str, object]] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append({"command": tuple(command), **kwargs})
                return subprocess.CompletedProcess(command, 0, "11.25.0\n", "")

            report = assess_execution_readiness(
                ExecutionReadinessRequest(
                    worktree=worktree,
                    pnpm=PnpmRequirement(binary="pnpm"),
                ),
                runner=runner,
                which=lambda executable: "/managed/pnpm" if executable == "pnpm" else None,
            )

        self.assertTrue(report.ready, report.to_dict())
        self.assertEqual(calls[0]["command"], ("/managed/pnpm", "--version"))
        self.assertEqual(calls[0]["cwd"], worktree.resolve())
        self.assertEqual(calls[0]["timeout"], 10)
        self.assertTrue(all("install" not in command and "login" not in command for command in (item["command"] for item in calls)))
        linker = next(check for check in report.checks if check.check_id == "pnpm:linker")
        self.assertEqual(linker.status, "passed")
        self.assertEqual(linker.detail["package_local_linkers"], ["packages/fixture/node_modules"])

    def test_pnpm_toolchain_mismatch_is_an_actionable_environment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            self._pnpm_project(worktree)
            self._complete_root_linker(worktree)

            report = assess_execution_readiness(
                ExecutionReadinessRequest(worktree=worktree),
                runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                    command, 0, "11.24.9\n", ""
                ),
                which=lambda executable: "/managed/pnpm" if executable == "pnpm" else None,
                environment={},
            )

        self.assertFalse(report.ready)
        mismatch = next(failure for failure in report.failures if failure.check_id == "pnpm:toolchain")
        self.assertEqual(mismatch.origin, "environment")
        self.assertEqual(mismatch.code, "toolchain-mismatch")
        self.assertIn("11.25.0", mismatch.message)
        self.assertNotIn("quality", json.dumps(report.to_dict()))
        self.assertEqual(PNPM_MINIMUM_11_VERSION, (11, 25, 0))

    def test_pnpm_prefers_the_configured_managed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            self._pnpm_project(worktree)
            self._complete_root_linker(worktree)
            calls: list[tuple[str, ...]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(tuple(command))
                return subprocess.CompletedProcess(command, 0, "11.25.0\n", "")

            report = assess_execution_readiness(
                ExecutionReadinessRequest(worktree=worktree),
                runner=runner,
                which=lambda executable: "/managed/pnpm" if executable == "managed-pnpm" else None,
                environment={"CODEX_WORKBENCH_PNPM": "managed-pnpm"},
            )

        self.assertTrue(report.ready, report.to_dict())
        self.assertEqual(calls, [("/managed/pnpm", "--version")])
        toolchain = next(check for check in report.checks if check.check_id == "pnpm:toolchain")
        self.assertEqual(toolchain.detail["binary_source"], "CODEX_WORKBENCH_PNPM")

    def test_missing_pnpm_linker_never_attempts_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            self._pnpm_project(worktree)
            commands: list[tuple[str, ...]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(tuple(command))
                return subprocess.CompletedProcess(command, 0, "11.25.0\n", "")

            report = assess_execution_readiness(
                ExecutionReadinessRequest(worktree=worktree),
                runner=runner,
                which=lambda executable: "/managed/pnpm" if executable == "pnpm" else None,
                environment={},
            )

        self.assertFalse(report.ready)
        linker = next(failure for failure in report.failures if failure.check_id == "pnpm:linker")
        self.assertEqual(linker.code, "missing-dependency")
        self.assertIn("readiness never runs pnpm install", linker.remediation)
        self.assertEqual(commands, [("/managed/pnpm", "--version")])

    def test_explicit_dependency_resolving_elsewhere_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            external = root / "external-dependency"
            external.mkdir()
            link = worktree / "linked-dependency"
            link.symlink_to(external, target_is_directory=True)

            report = assess_execution_readiness(
                ExecutionReadinessRequest(
                    worktree=worktree,
                    dependencies=(
                        PathDependencyRequirement("linked", "linked-dependency", "directory"),
                    ),
                    pnpm=None,
                )
            )

        self.assertFalse(report.ready)
        failure = report.failures[0]
        self.assertEqual(failure.origin, "environment")
        self.assertEqual(failure.code, "source-outside-worktree")
        self.assertEqual(failure.check_id, "dependency:linked")

    def test_package_source_resolution_uses_target_roots_and_rejects_external_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "src" / "local_package").mkdir(parents=True)
            (worktree / "src" / "local_package" / "__init__.py").write_text("VALUE = 1\n")
            calls: list[tuple[str, ...]] = []

            def external_origin(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(tuple(command))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "module": "local_package",
                            "origin": "/another-worktree/src/local_package/__init__.py",
                            "locations": ["/another-worktree/src/local_package"],
                        }
                    ),
                    "",
                )

            report = assess_execution_readiness(
                ExecutionReadinessRequest(
                    worktree=worktree,
                    source_resolutions=(
                        SourceResolutionRequirement("local_package", ("src",)),
                    ),
                    pnpm=None,
                ),
                runner=external_origin,
                which=lambda executable: sys.executable if executable == sys.executable else None,
            )

        self.assertFalse(report.ready)
        failure = report.failures[0]
        self.assertEqual(failure.check_id, "source:local_package")
        self.assertEqual(failure.code, "source-outside-worktree")
        self.assertEqual(calls[0][1:3], ("-I", "-S"))
        self.assertIn("PathFinder", calls[0][4])

    def test_package_source_resolution_proves_target_source_without_importing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            package = worktree / "src" / "local_package"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("raise RuntimeError('must not import')\n")

            report = assess_execution_readiness(
                ExecutionReadinessRequest(
                    worktree=worktree,
                    source_resolutions=(
                        SourceResolutionRequirement("local_package", ("src",)),
                    ),
                    pnpm=None,
                )
            )

        self.assertTrue(report.ready, report.to_dict())
        source = next(check for check in report.checks if check.check_id == "source:local_package")
        self.assertEqual(source.status, "passed")
        self.assertEqual(source.detail["origin"], str((package / "__init__.py").resolve()))
        self.assertIn("no target package import", source.detail["probe"])

    def test_tool_probe_timeout_is_bounded_environment_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)

            def timeout(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired(command, 3)

            report = assess_execution_readiness(
                ExecutionReadinessRequest(
                    worktree=worktree,
                    tools=(ToolRequirement("formatter", "formatter"),),
                    pnpm=None,
                ),
                runner=timeout,
                which=lambda executable: "/managed/formatter" if executable == "formatter" else None,
            )

        self.assertFalse(report.ready)
        failure = report.failures[0]
        self.assertEqual(failure.check_id, "tool:formatter")
        self.assertEqual(failure.code, "probe-timeout")
        self.assertEqual(failure.origin, "environment")

    def test_request_rejects_unbounded_probe_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "total_timeout_seconds cannot exceed"):
                ExecutionReadinessRequest(
                    worktree=Path(directory),
                    pnpm=None,
                    total_timeout_seconds=61,
                )
            with self.assertRaisesRegex(ValueError, "version_argument"):
                ToolRequirement("unsafe", "unsafe", version_argument="install")

    @staticmethod
    def _pnpm_project(worktree: Path) -> None:
        (worktree / "package.json").write_text(json.dumps({"packageManager": "pnpm@11.7.0"}))
        (worktree / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")

    @staticmethod
    def _complete_root_linker(worktree: Path) -> None:
        (worktree / "node_modules").mkdir()
        (worktree / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n")
        (worktree / "node_modules" / ".bin").mkdir()


if __name__ == "__main__":
    unittest.main()
