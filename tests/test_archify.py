from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_workbench.archify import (
    ARCHIFY_COMMIT,
    ARCHIFY_TAG,
    ARCHIFY_VERSION,
    CONTENT_MANIFEST_FILENAME,
    ROLE_CONTRACTS,
    SKILL_NAME,
    ArchifyContractError,
    role_contract,
    validate_receipt,
    verify_skill_projection,
    verify_vendor,
)


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "archify"
CLI = VENDOR / "bin" / "archify.mjs"
NODE = shutil.which("node")
PHYSICAL_TMP = Path(tempfile.gettempdir()).resolve()


class ArchifyTests(unittest.TestCase):
    @staticmethod
    def _installer_module():
        path = ROOT / "scripts" / "install-archify.py"
        spec = importlib.util.spec_from_file_location("codex_workbench_archify_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if NODE is None:
            self.skipTest("Archify acceptance requires Node.js >=18")
        result = subprocess.run(
            [NODE, str(CLI), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"Archify CLI failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def _json_cli(self, *args: str) -> dict:
        result = self._run_cli(*args)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"Archify CLI did not emit JSON: {error}\n{result.stdout}")
        self.assertIsInstance(value, dict)
        return value

    def test_pinned_vendor_has_full_stable_identity_and_projection(self) -> None:
        identity = verify_vendor(VENDOR)
        self.assertTrue(identity["ok"])
        self.assertEqual(identity["tag"], ARCHIFY_TAG)
        self.assertEqual(identity["commit"], ARCHIFY_COMMIT)
        self.assertEqual(identity["version"], ARCHIFY_VERSION)
        self.assertEqual(identity["license"], "MIT")
        self.assertGreaterEqual(identity["required_file_count"], 20)
        projection = verify_skill_projection(VENDOR, ROOT / "skills" / SKILL_NAME / "SKILL.md")
        self.assertTrue(projection["ok"])
        package = json.loads((VENDOR / "package.json").read_text(encoding="utf-8"))
        self.assertNotEqual(package["version"], "2.14.0")
        self.assertIn("MIT", (VENDOR / "LICENSE").read_text(encoding="utf-8"))

    def test_content_manifest_is_source_lock_bound_and_detects_core_tampering(self) -> None:
        identity = verify_vendor(VENDOR)
        manifest = identity["content_manifest"]
        self.assertEqual(manifest["path"], CONTENT_MANIFEST_FILENAME)
        self.assertGreater(manifest["file_count"], len(ROLE_CONTRACTS))

        with tempfile.TemporaryDirectory(prefix="archify-manifest-", dir=PHYSICAL_TMP) as directory:
            copied = Path(directory) / "archify"
            shutil.copytree(VENDOR, copied)
            target = copied / "bin" / "archify.mjs"
            target.write_text(target.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ArchifyContractError, "content manifest"):
                verify_vendor(copied)

    def test_role_contracts_route_four_roles_and_forbid_overclaiming(self) -> None:
        self.assertEqual(set(ROLE_CONTRACTS), {"architecture", "design", "review", "requirements"})
        for role, contract in ROLE_CONTRACTS.items():
            value = role_contract(role)
            self.assertEqual(value["role"], role)
            self.assertTrue(value["commands"])
            self.assertIn("renderer_pass_is_semantic_correctness", value["forbidden_claims"])
            self.assertIn("authored_reachability_is_runtime_impact_or_blast_radius", value["forbidden_claims"])
            self.assertEqual(tuple(value["diagram_types"]), contract.diagram_types)
        with self.assertRaises(ArchifyContractError):
            role_contract("operator")

    def test_doctor_and_five_typed_fixtures_pass_showcase_validation(self) -> None:
        doctor = self._run_cli("doctor")
        self.assertIn("Archify is ready.", doctor.stdout)
        fixtures = (
            ("architecture", VENDOR / "examples" / "web-app.architecture.json"),
            ("workflow", VENDOR / "examples" / "release-delivery.workflow.json"),
            ("sequence", VENDOR / "examples" / "async-job-roundtrip.sequence.json"),
            ("dataflow", VENDOR / "examples" / "event-stream.dataflow.json"),
            ("lifecycle", VENDOR / "examples" / "agent-run.lifecycle.json"),
        )
        for diagram_type, fixture in fixtures:
            receipt = self._json_cli(
                "validate",
                diagram_type,
                str(fixture),
                "--quality",
                "showcase",
                "--json",
            )
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(len(receipt["checks"]), 9, receipt)
            self.assertEqual(receipt["composition"]["status"], "pass", receipt)
            self.assertEqual(receipt["composition"]["summary"], {"errors": 0, "warnings": 0})
            verdict = validate_receipt(receipt)
            self.assertTrue(verdict["ok"], verdict)
            self.assertTrue(verdict["renderer_pass"], verdict)
            self.assertIsNone(verdict["semantic_pass"])

    def test_deliver_compare_migrate_and_visual_receipts_are_truthful(self) -> None:
        with tempfile.TemporaryDirectory(prefix="archify-receipts-") as directory:
            root = Path(directory)
            delivered = root / "delivered.html"
            deliver = self._json_cli(
                "deliver",
                "architecture",
                str(VENDOR / "examples" / "web-app.architecture.json"),
                str(delivered),
                "--quality",
                "showcase",
                "--json",
            )
            self.assertTrue(validate_receipt(deliver)["ok"], deliver)
            self.assertTrue(delivered.is_file())
            role_verdict = validate_receipt(deliver, role="design")
            self.assertFalse(role_verdict["ok"])
            self.assertTrue(role_verdict["renderer_pass"])
            self.assertFalse(role_verdict["semantic_pass"] is True)

            semantic_deliver = dict(deliver)
            semantic_deliver["semantic"] = {"ok": True, "source": "requirements-fixture-v1"}
            self.assertTrue(validate_receipt(semantic_deliver, role="design")["ok"])

            compared = self._json_cli(
                "compare",
                "architecture",
                str(VENDOR / "examples" / "checkout-platform.base.architecture.json"),
                str(VENDOR / "examples" / "checkout-platform.head.architecture.json"),
                str(root / "delta.html"),
                "--quality",
                "showcase",
                "--json",
            )
            compare_verdict = validate_receipt(compared)
            self.assertTrue(compare_verdict["ok"], compare_verdict)
            self.assertTrue(compare_verdict["renderer_pass"])

            migrated = root / "migrated.workflow.json"
            migration = self._json_cli(
                "migrate",
                "workflow",
                str(VENDOR / "test" / "fixtures" / "v1-workflow-explicit-coordinates.workflow.json"),
                str(migrated),
                "--to-schema",
                "2",
                "--json",
            )
            migration_verdict = validate_receipt(migration)
            self.assertTrue(migration_verdict["ok"], migration_verdict)
            self.assertEqual(json.loads(migrated.read_text(encoding="utf-8"))["schema_version"], 2)

            visual_result = self._run_cli("visual-check", str(delivered), "--json", check=False)
            self.assertIn(visual_result.returncode, {0, 2}, visual_result.stderr)
            visual = json.loads(visual_result.stdout)
            self.assertIn(visual["status"], {"pass", "skipped"})
            visual_verdict = validate_receipt(visual)
            if visual["status"] == "pass":
                self.assertTrue(visual_verdict["ok"], visual_verdict)
                self.assertFalse(visual_verdict["visual_pass"])
                reviewed = validate_receipt(visual, require_visual_review=True)
                self.assertFalse(reviewed["ok"])
            else:
                self.assertFalse(visual_verdict["ok"])

    def test_receipt_validator_rejects_renderer_only_and_bad_visual_claims(self) -> None:
        valid = self._json_cli(
            "validate",
            "architecture",
            str(VENDOR / "examples" / "web-app.architecture.json"),
            "--quality",
            "showcase",
            "--json",
        )
        invalid = json.loads(json.dumps(valid))
        invalid["checks"] = invalid["checks"][:-1]
        verdict = validate_receipt(invalid)
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["renderer_pass"])
        self.assertIn("9 passing artifact checks", " ".join(verdict["reasons"]))

        invalid_identity = json.loads(json.dumps(valid))
        invalid_identity["schemaVersion"] = 999
        identity_verdict = validate_receipt(invalid_identity)
        self.assertFalse(identity_verdict["ok"])
        self.assertFalse(identity_verdict["renderer_pass"])

        visual = {
            "schemaVersion": 1,
            "ok": True,
            "command": "visual-check",
            "type": "architecture",
            "status": "pass",
            "visualReview": "pending",
            "artifact": {"path": "/tmp/example.html", "sha256": "a" * 64, "bytes": 10},
            "containment": {"status": "pass"},
            "captures": {"status": "pass"},
        }
        self.assertTrue(validate_receipt(visual)["ok"])
        self.assertFalse(validate_receipt(visual, require_visual_review=True)["ok"])

    def test_installer_preflights_both_endpoints_and_refuses_unmanaged_skill(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(prefix="archify-install-", dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            codex_root = root / "codex"
            claude_root = root / "claude"
            unmanaged = claude_root / ".claude" / "skills" / "archify"
            unmanaged.mkdir(parents=True)
            sentinel = unmanaged / "SKILL.md"
            sentinel.write_text("user-owned\n", encoding="utf-8")
            with self.assertRaises(installer.ArchifyInstallError):
                installer.install_archify(
                    ROOT,
                    codex_root=codex_root,
                    claude_root=claude_root,
                )
            self.assertFalse((codex_root / ".codex" / "skills" / "archify").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user-owned\n")

    def test_installer_is_idempotent_and_copies_full_core_to_both_temp_endpoints(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(prefix="archify-install-", dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            codex_root = root / "codex"
            claude_root = root / "claude"
            first = installer.install_archify(
                ROOT,
                codex_root=codex_root,
                claude_root=claude_root,
            )
            second = installer.install_archify(
                ROOT,
                codex_root=codex_root,
                claude_root=claude_root,
            )
            self.assertEqual(first, second)
            for agent, relative in installer.TARGETS.items():
                base = (codex_root if agent == "codex" else claude_root) / relative
                self.assertEqual((base / "SKILL.md").read_bytes(), (VENDOR / "SKILL.md").read_bytes())
                self.assertEqual((base / "package.json").read_bytes(), (VENDOR / "package.json").read_bytes())
                self.assertEqual(
                    (base / CONTENT_MANIFEST_FILENAME).read_bytes(),
                    (VENDOR / CONTENT_MANIFEST_FILENAME).read_bytes(),
                )
                self.assertTrue(verify_vendor(base)["ok"])
                marker = json.loads((base / installer.MANAGED_MARKER_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(marker["managed_by"], installer.MANAGED_BY)
                self.assertEqual(marker["commit"], ARCHIFY_COMMIT)

    def test_installer_rejects_live_and_broken_symlink_ancestors_before_writes(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(prefix="archify-symlink-", dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            destination = root / "destination"
            destination.mkdir()
            for name, link_target in (
                ("live-link", destination),
                ("broken-link", root / "missing-destination"),
            ):
                with self.subTest(name=name):
                    linked_root = root / name
                    linked_root.symlink_to(link_target, target_is_directory=True)
                    with self.assertRaisesRegex(installer.ArchifyInstallError, "symlink ancestor"):
                        installer.preflight_install(
                            ROOT,
                            codex_root=linked_root,
                            claude_root=root / "claude",
                        )
                    self.assertFalse((destination / ".codex" / "skills" / "archify").exists())

    def test_installer_rolls_back_both_endpoints_and_cleans_staging_after_second_swap_fails(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(prefix="archify-rollback-", dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            codex_root = root / "codex"
            claude_root = root / "claude"
            installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)
            targets = {
                "codex": codex_root / installer.TARGETS["codex"],
                "claude": claude_root / installer.TARGETS["claude"],
            }
            sentinels = {
                agent: target / "local-before-rollback.txt"
                for agent, target in targets.items()
            }
            for agent, sentinel in sentinels.items():
                sentinel.write_text(f"{agent}-old\n", encoding="utf-8")

            real_replace = installer.os.replace
            claude_target = targets["claude"]

            def fail_second_stage(source: object, destination: object) -> object:
                if (
                    Path(source).name.startswith(".archify.stage-")
                    and Path(destination) == claude_target
                ):
                    raise OSError("injected second endpoint failure")
                return real_replace(source, destination)

            with mock.patch.object(installer.os, "replace", side_effect=fail_second_stage):
                with self.assertRaisesRegex(installer.ArchifyInstallError, "atomic install failed"):
                    installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)

            for agent, sentinel in sentinels.items():
                self.assertEqual(sentinel.read_text(encoding="utf-8"), f"{agent}-old\n")
            leftovers = [
                path
                for path in root.rglob("*")
                if ".archify.stage-" in path.name or ".archify.backup-" in path.name
            ]
            self.assertEqual(leftovers, [])

    def test_installer_recovers_a_persistent_transaction_after_interrupt(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(prefix="archify-interrupt-", dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            codex_root = root / "codex"
            claude_root = root / "claude"
            installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)
            targets = {
                "codex": codex_root / installer.TARGETS["codex"],
                "claude": claude_root / installer.TARGETS["claude"],
            }
            for agent, target in targets.items():
                (target / "before-interrupt.txt").write_text(agent, encoding="utf-8")

            real_replace = installer.os.replace

            def interrupt_after_first_endpoint(source: object, destination: object) -> object:
                if (
                    Path(source).name.startswith(".archify.stage-")
                    and Path(destination) == targets["claude"]
                ):
                    raise SystemExit("simulated SIGKILL boundary")
                return real_replace(source, destination)

            with mock.patch.object(installer.os, "replace", side_effect=interrupt_after_first_endpoint):
                with self.assertRaises(SystemExit):
                    installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)

            recovered = installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)
            self.assertTrue(recovered["recovered_transaction"])
            for target in targets.values():
                self.assertEqual((target / "SKILL.md").read_bytes(), (VENDOR / "SKILL.md").read_bytes())
                self.assertFalse((target / "before-interrupt.txt").exists())

    def test_installer_upgrade_replaces_entire_owned_tree_without_stale_files(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(prefix="archify-upgrade-", dir=PHYSICAL_TMP) as directory:
            root = Path(directory)
            codex_root = root / "codex"
            claude_root = root / "claude"
            installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)
            for agent, relative in installer.TARGETS.items():
                target = (codex_root if agent == "codex" else claude_root) / relative
                (target / "stale-from-prior-version.txt").write_text("old\n", encoding="utf-8")
            installer.install_archify(ROOT, codex_root=codex_root, claude_root=claude_root)
            for agent, relative in installer.TARGETS.items():
                target = (codex_root if agent == "codex" else claude_root) / relative
                self.assertFalse((target / "stale-from-prior-version.txt").exists())
            leftovers = [
                path
                for path in root.rglob("*")
                if ".archify.stage-" in path.name or ".archify.backup-" in path.name
            ]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
