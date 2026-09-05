from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


class SquillaInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "workbench-home"
        self.home.mkdir()
        self.source = self.root / "opensquilla-source"
        self.bundle = self.root / "opensquilla-bundle"
        self.wheelhouse = self.root / "wheelhouse"
        self.python = self.root / "python3.12"
        self.python.write_text("fixture runtime\n", encoding="utf-8")
        self.python.chmod(0o755)
        self._write_source_and_bundle()
        self.revision = self._initialize_git()
        self._write_wheelhouse()
        self.module = self._installer_module()
        self.pin_patch = mock.patch.object(self.module, "UPSTREAM_REVISION", self.revision)
        self.pin_patch.start()

    def tearDown(self) -> None:
        self.pin_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-squilla-advisor.py"
        spec = importlib.util.spec_from_file_location("codex_workbench_squilla_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _write_source_and_bundle(self) -> None:
        source_bundle = (
            self.source
            / "src"
            / "opensquilla"
            / "squilla_router"
            / "models"
            / "v4.2_phase3_inference"
        )
        strategy = self.source / "src" / "opensquilla" / "squilla_router" / "v4_phase3.py"
        strategy.parent.mkdir(parents=True)
        strategy.write_text("class V4Phase3Strategy: pass\n", encoding="utf-8")
        self._write_bundle(source_bundle)
        self._write_bundle(self.bundle)

    def _write_bundle(self, bundle: Path) -> None:
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "runtime_src").mkdir()
        (bundle / "runtime_src" / "__init__.py").write_text("", encoding="utf-8")
        asset_paths = [
            "router.runtime.yaml",
            "version.json",
            "inference_manifest.json",
            *(f"assets/asset-{index:02d}.bin" for index in range(15)),
        ]
        entries: list[dict[str, object]] = []
        for index, relative in enumerate(asset_paths):
            content = f"fixture asset {index}\n".encode("utf-8")
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            entries.append(
                {
                    "path": relative,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "kind": "fixture",
                    "source_note": "synthetic installer fixture",
                }
            )
        manifest = {
            "schema_version": 1,
            "bundle": "src/opensquilla/squilla_router/models/v4.2_phase3_inference",
            "description": "synthetic pinned bundle",
            "files": entries,
        }
        (bundle / "artifact_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    def _initialize_git(self) -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.source, check=True)
        subprocess.run(["git", "add", "src"], cwd=self.source, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "synthetic OpenSquilla source",
            ],
            cwd=self.source,
            check=True,
        )
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.source, text=True
        ).strip()

    def _write_wheelhouse(self) -> None:
        self.wheelhouse.mkdir()
        names = (
            "numpy",
            "lightgbm",
            "joblib",
            "scikit_learn",
            "onnxruntime",
            "tokenizers",
            "structlog",
            "PyYAML",
        )
        for name in names:
            (self.wheelhouse / f"{name}-1.0-py3-none-any.whl").write_bytes(b"fixture")
        (self.wheelhouse / "requirements-native-pinned.txt").write_text(
            "\n".join(f"{name.replace('_', '-')}==1.0" for name in names) + "\n",
            encoding="utf-8",
        )

    def _command_runner(self, calls: list[tuple[str, ...]]):
        def run(command, *, label: str):
            values = tuple(str(value) for value in command)
            calls.append(values)
            if values[0] == str(self.python) and values[1] == "-c":
                return subprocess.CompletedProcess(values, 0, stdout="3.12\n", stderr="")
            if values[:2] == ("git", "-C") and values[-2:] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(values, 0, stdout=self.revision + "\n", stderr="")
            if values[:3] == ("git", "clone", "--no-hardlinks"):
                source, destination = Path(values[-2]), Path(values[-1])
                self.assertEqual(source, self.source)
                shutil.copytree(source, destination)
                return subprocess.CompletedProcess(values, 0, stdout="", stderr="")
            if values[0] == str(self.python) and values[1:3] == ("-m", "venv"):
                runtime = Path(values[-1]) / "bin" / "python"
                runtime.parent.mkdir(parents=True)
                runtime.write_text("fixture venv python\n", encoding="utf-8")
                runtime.chmod(0o755)
                return subprocess.CompletedProcess(values, 0, stdout="", stderr="")
            if values[1:4] == ("-m", "pip", "install"):
                self.assertIn("--no-index", values)
                self.assertIn("--find-links", values)
                self.assertEqual(
                    values[-2:],
                    ("-r", str(self.wheelhouse / self.module.REQUIREMENTS_FILENAME)),
                )
                return subprocess.CompletedProcess(values, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected command ({label}): {values}")

        return run

    def _plan(self, *, verify_bundle_assets: bool = False):
        return self.module.preflight_install(
            home=self.home,
            source_root=self.source,
            bundle_dir=self.bundle,
            wheelhouse=self.wheelhouse,
            python=self.python,
            verify_bundle_assets=verify_bundle_assets,
        )

    def test_dry_run_hashes_the_supplied_bundle_without_writing(self) -> None:
        calls: list[tuple[str, ...]] = []
        before = (self.home / "config.json").exists()
        with mock.patch.object(self.module, "_run_command", side_effect=self._command_runner(calls)), mock.patch.object(
            self.module, "_run_smoke"
        ) as smoke:
            plan = self._plan(verify_bundle_assets=True)
            receipt = self.module._receipt("dry_run", plan)

        self.assertEqual(receipt["status"], "dry_run")
        self.assertFalse((self.home / "advisors" / "opensquilla").exists())
        self.assertEqual((self.home / "config.json").exists(), before)
        self.assertFalse(smoke.called)
        self.assertFalse(any(command[:2] == ("git", "clone") for command in calls))
        self.assertFalse(any("pip" in command for command in calls))
        self.assertNotIn("native_receipt", receipt)

    def test_successful_install_preserves_unknown_config_fields_and_uses_final_venv(self) -> None:
        original = {
            "operator_setting": {"keep": ["all", "unknown", "keys"]},
            "squilla_advisor": {
                "operator_note": "preserve",
                "nested": {"keep": True},
                "enabled": False,
            },
        }
        (self.home / "config.json").write_text(json.dumps(original) + "\n", encoding="utf-8")
        old_install = self.home / "advisors" / "opensquilla"
        old_install.mkdir(parents=True)
        (old_install / "previous.txt").write_text("preserve successful backup\n", encoding="utf-8")
        calls: list[tuple[str, ...]] = []
        smoke_paths: list[Path] = []
        native_receipt = {
            "schema_version": 1,
            "request_id": "opensquilla-install-smoke",
            "status": "available",
            "demand_tier": "c2",
            "source": {"observed_source_revision": self.revision},
        }
        with mock.patch.object(self.module, "_run_command", side_effect=self._command_runner(calls)), mock.patch.object(
            self.module,
            "_run_smoke",
            side_effect=lambda root: smoke_paths.append(root) or native_receipt,
        ):
            receipt = self.module.install(self._plan())

        install_root = self.home / "advisors" / "opensquilla"
        raw = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "installed")
        self.assertEqual(smoke_paths, [install_root])
        self.assertTrue((install_root / "source" / ".git").exists())
        self.assertTrue((install_root / "venv" / "bin" / "python").is_file())
        self.assertEqual(
            json.loads((install_root / "installation-receipt.json").read_text(encoding="utf-8")),
            native_receipt,
        )
        self.assertEqual(receipt["native_receipt"], native_receipt)
        backup = Path(receipt["previous_install_backup"])
        self.assertTrue(backup.is_dir())
        self.assertEqual(
            (backup / "previous.txt").read_text(encoding="utf-8"),
            "preserve successful backup\n",
        )
        self.assertEqual(raw["operator_setting"], original["operator_setting"])
        self.assertEqual(raw["squilla_advisor"]["operator_note"], "preserve")
        self.assertEqual(raw["squilla_advisor"]["nested"], {"keep": True})
        self.assertEqual(raw["squilla_advisor"]["enabled"], True)
        self.assertEqual(
            raw["squilla_advisor"]["runtime_python"], str(install_root / "venv" / "bin" / "python")
        )
        self.assertEqual(raw["squilla_advisor"]["source_root"], str(install_root / "source"))
        self.assertEqual(raw["squilla_advisor"]["bundle_dir"], str(install_root / "bundle"))
        self.assertEqual(raw["squilla_advisor"]["timeout_seconds"], 45.0)
        self.assertEqual(
            receipt["restart_required"],
            {"long_running_mcp_or_service": True, "one_shot_cli": "next_invocation"},
        )

    def test_smoke_failure_restores_existing_install_and_exact_config(self) -> None:
        old_install = self.home / "advisors" / "opensquilla"
        old_install.mkdir(parents=True)
        (old_install / "previous.txt").write_text("preserve me\n", encoding="utf-8")
        config = self.home / "config.json"
        original_config = b'{"keep":"exact bytes", "squilla_advisor":{"operator_note":"old"}}\n'
        config.write_bytes(original_config)
        calls: list[tuple[str, ...]] = []
        with mock.patch.object(self.module, "_run_command", side_effect=self._command_runner(calls)), mock.patch.object(
            self.module,
            "_run_smoke",
            side_effect=self.module.SquillaInstallerError("synthetic smoke failure"),
        ):
            with self.assertRaisesRegex(self.module.SquillaInstallerError, "synthetic smoke failure"):
                self.module.install(self._plan())

        self.assertEqual((old_install / "previous.txt").read_text(encoding="utf-8"), "preserve me\n")
        self.assertEqual(config.read_bytes(), original_config)
        self.assertFalse(list((old_install.parent).glob(".opensquilla.previous-*")))

    def test_receipt_persistence_failure_restores_existing_install_and_config(self) -> None:
        old_install = self.home / "advisors" / "opensquilla"
        old_install.mkdir(parents=True)
        (old_install / "previous.txt").write_text("restore after receipt failure\n", encoding="utf-8")
        config = self.home / "config.json"
        original_config = b'{"keep":"receipt rollback"}\n'
        config.write_bytes(original_config)
        calls: list[tuple[str, ...]] = []
        native_receipt = {"status": "available", "request_id": "opensquilla-install-smoke"}
        with mock.patch.object(self.module, "_run_command", side_effect=self._command_runner(calls)), mock.patch.object(
            self.module, "_run_smoke", return_value=native_receipt
        ), mock.patch.object(
            self.module,
            "_write_installation_receipt",
            side_effect=OSError("synthetic receipt persistence failure"),
        ):
            with self.assertRaisesRegex(self.module.SquillaInstallerError, "receipt persistence failure"):
                self.module.install(self._plan())

        self.assertEqual(
            (old_install / "previous.txt").read_text(encoding="utf-8"),
            "restore after receipt failure\n",
        )
        self.assertEqual(config.read_bytes(), original_config)

    def test_smoke_uses_one_keyless_batch_and_requires_available_result(self) -> None:
        calls: list[tuple[dict[str, object], list[object]]] = []

        class AvailableAdvice:
            status = "available"
            demand_tier = "c2"
            source = {"observed_source_revision": self.revision}

            def to_receipt(self):
                return {
                    "request_id": "opensquilla-install-smoke",
                    "status": "available",
                    "demand_tier": "c2",
                }

        class AvailableAdvisor:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def advise_batch(self, requests: list[object]):
                calls.append((self.kwargs, requests))
                return [AvailableAdvice()]

        install_root = self.home / "advisors" / "opensquilla"
        with mock.patch.object(self.module, "SquillaAdvisor", AvailableAdvisor):
            receipt = self.module._run_smoke(install_root)

        self.assertEqual(len(calls), 1)
        kwargs, requests = calls[0]
        self.assertEqual(kwargs["runtime_python"], install_root / "venv" / "bin" / "python")
        self.assertEqual(kwargs["timeout_seconds"], 45.0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].request_id, "opensquilla-install-smoke")
        self.assertFalse(requests[0].history_user_texts)
        self.assertIsNone(requests[0].previous_public_summary)
        self.assertEqual(receipt["status"], "available")


if __name__ == "__main__":
    unittest.main()
