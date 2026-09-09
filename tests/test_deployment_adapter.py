from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from codex_workbench.authority_delivery import build_authority_delivery_lifecycle
from codex_workbench.config import WorkbenchConfig
from codex_workbench.deployment_adapter import (
    HttpObservation,
    LocalDeploymentAuthority,
    LocalDeploymentStageAdapter,
    build_authority_deployment_stage_adapter,
)
from codex_workbench.deployment_helper import (
    SourceIdentity,
    deployment_paths,
    read_deployment_json,
    run_deployment_helper,
)
from codex_workbench.delivery_lifecycle import DeliveryStageContext
from codex_workbench.store import WorkbenchStore


_COMMIT = "a" * 40


class _FixtureStagingProvider:
    def prepare(self, source_root: Path, _commit: str, _dispatch_id: str) -> Path:
        return source_root.resolve()


class LocalDeploymentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        (self.source_root / "scripts").mkdir(parents=True)
        (self.source_root / "src" / "codex_workbench").mkdir(parents=True)
        (self.source_root / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\nversion = '9.9.9'\n",
            encoding="utf-8",
        )
        (self.source_root / "src" / "codex_workbench" / "__init__.py").write_text(
            '__version__ = "9.9.9"\n', encoding="utf-8"
        )
        (self.source_root / "src" / "codex_workbench" / "deployment_helper.py").write_text(
            "# fixture helper\n", encoding="utf-8"
        )
        (self.source_root / "scripts" / "install-macos.py").write_text(
            "# fixture installer\n", encoding="utf-8"
        )
        self.source = SourceIdentity(
            commit=_COMMIT,
            version="9.9.9",
            manifest={
                "algorithm": "sha256",
                "files": {
                    name: sha256(name.encode("utf-8")).hexdigest()
                    for name in (
                        "pyproject.toml",
                        "src/codex_workbench/__init__.py",
                        "src/codex_workbench/deployment_helper.py",
                        "scripts/install-macos.py",
                    )
                },
            },
        )
        self.launches: list[tuple[list[str], Path]] = []
        self.helper_pid = 4343
        self.authority = LocalDeploymentAuthority(
            target="macos-fixture",
            source_root=self.source_root,
            state_root=self.state_root,
            installer_python=Path(sys.executable),
            installer_arguments=("--codex-binary", "/configured/codex"),
            installer_timeout_seconds=30,
            observation_timeout_seconds=2,
            rollback_probe_timeout_seconds=2,
            rollback_probe_interval_seconds=1,
            health_check_name="health",
            health_url="http://127.0.0.1:8766/health",
            functional_check_name="snapshot",
            functional_url="http://127.0.0.1:8766/api/snapshot",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(
        self,
        *,
        http_reader: object | None = None,
        pid_alive: object | None = None,
    ) -> LocalDeploymentStageAdapter:
        def launcher(argv: list[str], source_root: Path) -> int:
            self.launches.append((argv, source_root))
            return self.helper_pid

        return LocalDeploymentStageAdapter(
            self.authority,
            source_identity_reader=lambda _source_root: self.source,
            staging_provider=_FixtureStagingProvider(),
            source_commit_reader=lambda _source_root: self.source.commit,
            helper_launcher=launcher,
            http_reader=(http_reader if http_reader is not None else self._unexpected_http),
            pid_alive=(pid_alive if pid_alive is not None else lambda _pid: False),
        )

    @staticmethod
    def _unexpected_http(_url: str, _timeout_seconds: int) -> HttpObservation:
        raise AssertionError("test did not authorize a real or fake HTTP observation")

    def _objective(self, *, identities: dict | None = None) -> dict:
        return {
            "objective_id": "objective-fixture",
            "requested_endpoints": {
                "deployment": {
                    "target": "macos-fixture",
                    "health_checks": ["health"],
                    "functional_checks": ["snapshot"],
                }
            },
            "identities": dict(identities or {}),
            "lease": {"coordinator_epoch": 7},
        }

    def _context(
        self,
        *,
        stage: str,
        dispatch_id: str = "delivery-dispatch-fixture",
        identities: dict | None = None,
    ) -> DeliveryStageContext:
        return DeliveryStageContext(
            objective=self._objective(identities=identities),
            task=None,
            stage=stage,
            attempt=1,
            dispatch_id=dispatch_id,
            authorization={"scope": {"deployment": {"target": "macos-fixture"}}},
        )

    def _complete_helper(self, dispatch_id: str, *, returncode: int = 0) -> list[tuple[str, ...]]:
        paths = deployment_paths(self.authority.state_root, dispatch_id)
        calls: list[tuple[str, ...]] = []

        def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, returncode, "installer stdout", "installer stderr")

        result = run_deployment_helper(
            paths.request,
            runner=runner,
            identity_reader=lambda _source_root: self.source,
        )
        self.assertEqual(result["status"], "succeeded" if returncode == 0 else "failed")
        return calls

    def _installed_manifest(self) -> None:
        path = self.state_root / "app" / "install-manifest.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps({"commit": self.source.commit, "version": self.source.version}),
            encoding="utf-8",
        )

    def _successful_deployment(self) -> tuple[LocalDeploymentStageAdapter, DeliveryStageContext, object]:
        adapter = self._adapter()
        deploy = self._context(stage="deploy")
        dispatched = adapter.execute_stage(deploy)
        self.assertEqual(dispatched.status, "deferred")
        self._complete_helper(deploy.dispatch_id)
        settled = adapter.reconcile_stage(deploy)
        self.assertEqual(settled.status, "succeeded")
        return adapter, deploy, settled

    def test_deploy_records_exact_request_and_reconciles_the_same_helper_result(self) -> None:
        adapter = self._adapter()
        context = self._context(stage="deploy")

        outcome = adapter.execute_stage(context)

        self.assertEqual(outcome.status, "deferred")
        paths = deployment_paths(self.authority.state_root, context.dispatch_id)
        request = read_deployment_json(paths.request)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["dispatch_id"], context.dispatch_id)
        self.assertEqual(request["source"], self.source.to_dict())
        self.assertEqual(request["target"], "macos-fixture")
        self.assertEqual(
            self.launches,
            [
                (
                    [
                        str(self.authority.installer_python),
                        str(self.authority.helper_script),
                        "--request",
                        str(paths.request),
                    ],
                    self.source_root.resolve(),
                )
            ],
        )

        calls = self._complete_helper(context.dispatch_id)
        settled = adapter.reconcile_stage(context)

        self.assertEqual(settled.status, "succeeded")
        self.assertEqual(
            calls,
            [
                (
                    str(self.authority.installer_python),
                    str(self.authority.installer_script),
                    "--source",
                    str(self.source_root.resolve()),
                    "--state-root",
                    str(self.state_root.resolve()),
                    "--codex-binary",
                    "/configured/codex",
                )
            ],
        )
        self.assertEqual(settled.identities["deploy"]["commit"], self.source.commit)
        self.assertEqual(len(self.launches), 1)

    def test_running_helper_is_deferred_but_missing_result_after_exit_is_indeterminate(self) -> None:
        adapter = self._adapter(pid_alive=lambda pid: pid == self.helper_pid)
        context = self._context(stage="deploy")
        self.assertEqual(adapter.execute_stage(context).status, "deferred")
        paths = deployment_paths(self.authority.state_root, context.dispatch_id)
        paths.running.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dispatch_id": context.dispatch_id,
                    "target": "macos-fixture",
                    "pid": self.helper_pid,
                }
            ),
            encoding="utf-8",
        )

        repeated = adapter.execute_stage(context)
        self.assertEqual(repeated.status, "deferred")
        self.assertEqual(len(self.launches), 1)
        running = adapter.reconcile_stage(context)

        self.assertEqual(running.status, "deferred")
        self.assertTrue(running.retry_eligible)
        stopped_adapter = self._adapter(pid_alive=lambda _pid: False)
        disappeared = stopped_adapter.reconcile_stage(context)
        self.assertEqual(disappeared.status, "indeterminate")
        self.assertFalse(disappeared.retry_eligible)

    def test_helper_refuses_source_drift_before_running_the_installer(self) -> None:
        adapter = self._adapter()
        context = self._context(stage="deploy")
        self.assertEqual(adapter.execute_stage(context).status, "deferred")
        paths = deployment_paths(self.authority.state_root, context.dispatch_id)
        observed = SourceIdentity(
            commit="b" * 40,
            version=self.source.version,
            manifest=self.source.manifest,
        )
        calls: list[tuple[str, ...]] = []

        def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, "unexpected", "")

        result = run_deployment_helper(
            paths.request,
            runner=runner,
            identity_reader=lambda _source_root: observed,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["installer"]["status"], "not-started")
        self.assertEqual(calls, [])
        self.assertEqual(adapter.reconcile_stage(context).status, "failed")

    def test_helper_nonzero_result_does_not_claim_rollback_verification(self) -> None:
        adapter = self._adapter()
        context = self._context(stage="deploy")
        self.assertEqual(adapter.execute_stage(context).status, "deferred")

        calls = self._complete_helper(context.dispatch_id, returncode=7)
        outcome = adapter.reconcile_stage(context)
        rollback = adapter.rollback_deployment(context, outcome)

        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.retry_eligible)
        self.assertFalse(rollback["verified"])
        self.assertIn("not independently verified", rollback["reason"])

    def test_helper_persists_verified_rollback_only_after_old_runtime_is_observed_again(self) -> None:
        old_commit = "c" * 40
        old_version = "9.9.8"
        manifest = self.state_root / "app" / "install-manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(
            json.dumps({"commit": old_commit, "version": old_version}),
            encoding="utf-8",
        )
        baseline_authority = {"active": True, "coordinator_epoch": 7, "instance_id": "before"}
        restored_authority = {"active": True, "coordinator_epoch": 8, "instance_id": "after"}

        def payload(authority: dict[str, object]) -> dict[str, object]:
            return {
                "version": old_version,
                "build": {"commit": old_commit, "version": old_version},
                "authority": authority,
            }

        def baseline_reader(url: str, _timeout_seconds: int) -> HttpObservation:
            if url == self.authority.health_url:
                return HttpObservation(200, {"ok": True, **payload(baseline_authority)})
            if url == self.authority.functional_url:
                return HttpObservation(
                    200,
                    {
                        "version": old_version,
                        "build": {"commit": old_commit, "version": old_version},
                        "health": {"authority": baseline_authority},
                    },
                )
            raise AssertionError(f"unexpected URL {url}")

        adapter = self._adapter(http_reader=baseline_reader)
        context = self._context(stage="deploy", dispatch_id="delivery-dispatch-rollback")
        self.assertEqual(adapter.execute_stage(context).status, "deferred")
        paths = deployment_paths(self.authority.state_root, context.dispatch_id)

        def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 7, "", "installer failure")

        def rollback_reader(url: str, _timeout_seconds: int):
            if url == self.authority.health_url:
                return 200, {"ok": True, **payload(restored_authority)}
            if url == self.authority.functional_url:
                return 200, {
                    "version": old_version,
                    "build": {"commit": old_commit, "version": old_version},
                    "health": {"authority": restored_authority},
                }
            raise AssertionError(f"unexpected URL {url}")

        result = run_deployment_helper(
            paths.request,
            runner=runner,
            identity_reader=lambda _source_root: self.source,
            rollback_http_reader=rollback_reader,
        )
        outcome = adapter.reconcile_stage(context)
        rollback = adapter.rollback_deployment(context, outcome)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["rollback_receipt"]["verified"])
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(rollback["verified"])
        self.assertEqual(rollback["observed_manifest"], {"commit": old_commit, "version": old_version})
        self.assertEqual(rollback["checks"]["health"]["coordinator_epoch"], 8)

    def test_live_verify_requires_matching_manifest_and_two_http_200_identity_responses(self) -> None:
        adapter, deploy, deployment = self._successful_deployment()
        self._installed_manifest()
        payload = {
            "version": self.source.version,
            "build": {"commit": self.source.commit, "version": self.source.version},
        }
        authority = {"active": True, "coordinator_epoch": 8, "instance_id": "fresh-fixture"}

        def http_reader(url: str, _timeout_seconds: int) -> HttpObservation:
            if url == self.authority.health_url:
                return HttpObservation(200, {"ok": True, "authority": authority, **payload})
            if url == self.authority.functional_url:
                return HttpObservation(200, {"health": {"authority": authority}, **payload})
            raise AssertionError(f"unexpected URL {url}")

        verifier = self._adapter(http_reader=http_reader)
        live = self._context(
            stage="live-verify",
            dispatch_id="delivery-dispatch-live",
            identities=dict(deployment.identities),
        )

        outcome = verifier.execute_stage(live)

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(
            outcome.receipt["checks"]["health"]["health"]["http_status"], 200
        )
        self.assertEqual(
            outcome.receipt["checks"]["functional"]["snapshot"]["status"], "passed"
        )
        self.assertEqual(outcome.identities["runtime"]["commit"], self.source.commit)
        self.assertEqual(deploy.stage, "deploy")

    def test_live_verify_rejects_mismatched_manifest_without_treating_a_process_as_success(self) -> None:
        _adapter, _deploy, deployment = self._successful_deployment()
        manifest = self.state_root / "app" / "install-manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(
            json.dumps({"commit": "b" * 40, "version": self.source.version}),
            encoding="utf-8",
        )
        verifier = self._adapter(http_reader=self._unexpected_http)
        live = self._context(
            stage="live-verify",
            dispatch_id="delivery-dispatch-live-mismatch",
            identities=dict(deployment.identities),
        )

        outcome = verifier.reconcile_stage(live)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.failure["kind"], "verification-failure")
        self.assertIn("manifest", outcome.failure["detail"])

    def test_live_verify_rejects_an_unchanged_coordinator_epoch(self) -> None:
        _adapter, _deploy, deployment = self._successful_deployment()
        self._installed_manifest()
        payload = {
            "version": self.source.version,
            "build": {"commit": self.source.commit, "version": self.source.version},
        }
        authority = {"active": True, "coordinator_epoch": 7, "instance_id": "stale-fixture"}

        def http_reader(url: str, _timeout_seconds: int) -> HttpObservation:
            if url == self.authority.health_url:
                return HttpObservation(200, {"ok": True, "authority": authority, **payload})
            if url == self.authority.functional_url:
                return HttpObservation(200, {"health": {"authority": authority}, **payload})
            raise AssertionError(f"unexpected URL {url}")

        verifier = self._adapter(http_reader=http_reader)
        live = self._context(
            stage="live-verify",
            dispatch_id="delivery-dispatch-live-stale-epoch",
            identities=dict(deployment.identities),
        )

        outcome = verifier.execute_stage(live)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.receipt["checks"]["health"]["health"]["coordinator_epoch"], 7)
        self.assertEqual(outcome.receipt["checks"]["health"]["health"]["status"], "failed")

    def test_commit_pinned_staging_deploys_integration_commit_without_touching_dirty_primary(self) -> None:
        primary = self.root / "Projects" / "primary"
        state_root = self.root / "state-staging"
        (primary / "scripts").mkdir(parents=True)
        (primary / "src" / "codex_workbench").mkdir(parents=True)
        state_root.mkdir()

        def write_source(version: str) -> None:
            (primary / "pyproject.toml").write_text(
                f"[project]\nname = \"fixture\"\nversion = \"{version}\"\n",
                encoding="utf-8",
            )
            (primary / "src" / "codex_workbench" / "__init__.py").write_text(
                f'__version__ = "{version}"\n', encoding="utf-8"
            )
            (primary / "src" / "codex_workbench" / "deployment_helper.py").write_text(
                f"# helper {version}\n", encoding="utf-8"
            )
            (primary / "scripts" / "install-macos.py").write_text(
                f"# installer {version}\n", encoding="utf-8"
            )

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(primary), *arguments],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        subprocess.run(["git", "init", str(primary)], check=True, capture_output=True, text=True)
        write_source("1.0.0")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "integration")
        integration_commit = git("rev-parse", "HEAD")
        write_source("1.0.1")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "primary-head")
        primary_head = git("rev-parse", "HEAD")
        (primary / "dirty-primary.txt").write_text("keep primary dirty\n", encoding="utf-8")
        dirty_before = git("status", "--porcelain")

        authority = LocalDeploymentAuthority(
            target="macos-fixture",
            source_root=primary,
            state_root=state_root,
            installer_python=Path(sys.executable),
            installer_arguments=("--codex-binary", "/configured/codex"),
            installer_timeout_seconds=30,
            observation_timeout_seconds=2,
            rollback_probe_timeout_seconds=2,
            rollback_probe_interval_seconds=1,
            health_check_name="health",
            health_url="http://127.0.0.1:8766/health",
            functional_check_name="snapshot",
            functional_url="http://127.0.0.1:8766/api/snapshot",
        )
        launched: list[tuple[list[str], Path]] = []

        def launcher(argv: list[str], source_root: Path) -> int:
            launched.append((argv, source_root))
            return 9191

        adapter = LocalDeploymentStageAdapter(
            authority,
            helper_launcher=launcher,
            http_reader=self._unexpected_http,
        )
        context = DeliveryStageContext(
            objective={
                "objective_id": "staging-objective",
                "requested_endpoints": {
                    "deployment": {
                        "target": "macos-fixture",
                        "health_checks": ["health"],
                        "functional_checks": ["snapshot"],
                    }
                },
                "identities": {"build": {"integration_commit": integration_commit}},
                "lease": {"coordinator_epoch": 7},
            },
            task=None,
            stage="deploy",
            attempt=1,
            dispatch_id="delivery-dispatch-staged-commit",
            authorization={"scope": {"deployment": {"target": "macos-fixture"}}},
        )

        self.assertEqual(adapter.execute_stage(context).status, "deferred")
        paths = deployment_paths(state_root, context.dispatch_id)
        request = read_deployment_json(paths.request)
        assert request is not None
        staged_root = Path(request["source_root"])
        self.assertEqual(request["trusted_source_root"], str(primary.resolve()))
        self.assertEqual(request["source"]["commit"], integration_commit)
        self.assertEqual(
            staged_root.parent,
            primary.parent.resolve() / ".codex-workbench-deployment-staging",
        )
        self.assertNotEqual(staged_root, primary.resolve())
        self.assertEqual(launched[0][1], staged_root)
        self.assertEqual(git("rev-parse", "HEAD"), primary_head)
        self.assertEqual(git("status", "--porcelain"), dirty_before)
        installer_calls: list[tuple[str, ...]] = []

        def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            installer_calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, "staged installer", "")

        result = run_deployment_helper(paths.request, runner=runner)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(git("rev-parse", "HEAD"), primary_head)
        self.assertEqual(git("status", "--porcelain"), dirty_before)
        self.assertIn(str(staged_root), installer_calls[0])
        self.assertNotIn(str(primary.resolve()), installer_calls[0])
        self.assertEqual(adapter.reconcile_stage(context).status, "succeeded")
    def test_factory_requires_explicit_local_authority_configuration(self) -> None:
        config = WorkbenchConfig(
            self.state_root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id="fixture-machine",
        )
        self.assertIsNone(build_authority_deployment_stage_adapter(Mock(spec=WorkbenchStore), config))
        config.config_file.write_text(
            json.dumps(
                {
                    "local_deployment": {
                        "schema_version": 1,
                        "target": "macos-fixture",
                        "source_root": str(self.source_root),
                        "installer_python": sys.executable,
                        "installer_arguments": ["--codex-binary", "/configured/codex"],
                        "installer_timeout_seconds": 30,
                        "observation_timeout_seconds": 2,
                        "rollback_probe_timeout_seconds": 2,
                        "rollback_probe_interval_seconds": 1,
                        "health": {"name": "health", "url": "http://127.0.0.1:8766/health"},
                        "functional": {
                            "name": "snapshot",
                            "url": "http://127.0.0.1:8766/api/snapshot",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        adapter = build_authority_deployment_stage_adapter(Mock(spec=WorkbenchStore), config)

        self.assertIsInstance(adapter, LocalDeploymentStageAdapter)
        lifecycle = build_authority_delivery_lifecycle(
            Mock(spec=WorkbenchStore),
            config,
            coordinator_epoch=1,
        )
        composite = lifecycle.adapter
        self.assertIs(composite.adapters["deploy"], composite.adapters["live-verify"])
        self.assertIsInstance(composite.adapters["deploy"], LocalDeploymentStageAdapter)


if __name__ == "__main__":
    unittest.main()
