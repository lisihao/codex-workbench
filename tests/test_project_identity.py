from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from codex_workbench.governance import governance_directive
from codex_workbench.project_identity import WORKBENCH_PROJECT_IDENTITY


class ProjectIdentityTests(unittest.TestCase):
    @staticmethod
    def _installer_module():
        path = Path(__file__).resolve().parents[1] / "scripts" / "install-code-as-harness.py"
        spec = importlib.util.spec_from_file_location("project_identity_harness_installer", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_canonical_text_states_goals_without_claiming_unimplemented_capabilities(self) -> None:
        self.assertIn("intended goals", WORKBENCH_PROJECT_IDENTITY)
        self.assertIn("not a claim that every capability is implemented", WORKBENCH_PROJECT_IDENTITY)
        self.assertIn("DSH is independent", WORKBENCH_PROJECT_IDENTITY)
        self.assertIn("not integration", WORKBENCH_PROJECT_IDENTITY)
        self.assertIn("preserving accepted ancestors", WORKBENCH_PROJECT_IDENTITY)

    def test_governance_and_installer_policy_use_identical_canonical_text(self) -> None:
        installer = self._installer_module()
        contract = {"governance_profile": "code-as-harness/v1", "verification_tier": "L2"}

        runtime = governance_directive(contract)
        codex = installer.policy_block("codex")
        claude = installer.policy_block("claude-code")

        self.assertIn(WORKBENCH_PROJECT_IDENTITY, runtime)
        self.assertIn(WORKBENCH_PROJECT_IDENTITY, codex)
        self.assertIn(WORKBENCH_PROJECT_IDENTITY, claude)
        self.assertEqual(codex.count(WORKBENCH_PROJECT_IDENTITY), 1)
        self.assertEqual(claude.count(WORKBENCH_PROJECT_IDENTITY), 1)

    def test_policy_update_preserves_user_text_and_replaces_one_managed_block(self) -> None:
        installer = self._installer_module()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            policy = Path(directory) / "AGENTS.md"
            prefix = "# User-owned policy\n\n"
            suffix = "\n\n# User-owned footer\n"
            policy.write_text(prefix + installer.policy_block("codex") + suffix, encoding="utf-8")

            first = installer.updated_policy(policy, installer.policy_block("codex"))
            policy.write_text(first, encoding="utf-8")
            second = installer.updated_policy(policy, installer.policy_block("codex"))

            self.assertTrue(second.startswith(prefix))
            self.assertTrue(second.endswith(suffix))
            self.assertEqual(second.count(installer.POLICY_START), 1)
            self.assertEqual(second.count(installer.POLICY_END), 1)
            self.assertEqual(second.count(WORKBENCH_PROJECT_IDENTITY), 1)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
