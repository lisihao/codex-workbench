"""Independent literals for the platform-reviewed minimal D integration scope."""

from __future__ import annotations

import unittest

from codex_workbench.d_integration_profile import (
    NODE_READ_ADDITIONS,
    NODE_WRITE_ADDITIONS,
    TASK_ACCESS_ADDITIONS,
)


EXPECTED_WRITES = (
    "scripts/gen-cordis-catalog.ts",
    "packages/extensions/tool-cordis/src/api-catalog.ts",
    "packages/extensions/cordis-client-runner/src/client/slot-catalog.ts",
    "packages/core/session/src/known-event-types.ts",
    "examples/acp-agent/tests/acp.snapshot.ts",
    "examples/acp-agent/tests/fixtures/task-template/task-template.cordis.yml",
    "examples/acp-agent/tests/fixtures/task-template/task-template.cordis.snapshot.yml",
    "packages/prompt/task-template-rpc/package.json",
)

EXPECTED_READ_ONLY_INPUTS = (
    "scripts/gen-client-catalog.ts",
    "scripts/gen-persistence-catalog.ts",
    "packages/prompt/task-template/src/service.ts",
    "packages/prompt/task-template/src/types.ts",
    "packages/prompt/task-template-context/src/index.ts",
    "packages/client/ui-task-template/src/client/index.ts",
    "packages/client/ui-settings/src/client/contract/slots.ts",
    "packages/physical-operator/tool-physical-operator/src/index.ts",
    "packages/test-support/acp-snapshot/src/suite.ts",
    "packages/test-support/acp-snapshot/src/launcher.ts",
    "examples/acp-agent/cordis.yml",
)

REMOVED_SPECULATIVE_WRITES = (
    "packages/core/system-prompt/README.md",
    "packages/core/system-prompt/README.zh.md",
    "packages/core/system-prompt/README.i18n.yaml",
    "packages/prompt/task-template-context/README.md",
    "packages/prompt/task-template-context/README.zh.md",
    "packages/prompt/task-template-context/README.i18n.yaml",
    "packages/prompt/task-template/README.md",
    "packages/prompt/task-template/README.zh.md",
    "packages/prompt/task-template/README.i18n.yaml",
    "packages/orchestration/orchestration/README.i18n.yaml",
    "packages/physical-operator/physical-operator/README.i18n.yaml",
    "packages/prompt/README.i18n.yaml",
)

REMOVED_BROAD_OR_UNNEEDED_READS = (
    "packages/typert/generator/src",
    "scripts/verify-translation-pairing.ts",
    "scripts/translation-pairing-git.ts",
    "scripts/translation-pairing.ts",
    "scripts/cordis-core-api.ts",
)


class DScopeMinimalProfileTests(unittest.TestCase):
    """Keep the fixed profile at the evidence-supported file boundary."""

    def test_write_scope_is_exactly_the_eight_evidenced_files(self) -> None:
        self.assertEqual(NODE_WRITE_ADDITIONS, EXPECTED_WRITES)
        self.assertEqual(len(NODE_WRITE_ADDITIONS), 8)
        self.assertTrue(set(NODE_WRITE_ADDITIONS).isdisjoint(REMOVED_SPECULATIVE_WRITES))

    def test_read_scope_separates_exact_inputs_from_outputs(self) -> None:
        self.assertEqual(
            NODE_READ_ADDITIONS,
            (*EXPECTED_WRITES, *EXPECTED_READ_ONLY_INPUTS),
        )
        self.assertEqual(TASK_ACCESS_ADDITIONS, NODE_READ_ADDITIONS)
        self.assertTrue(set(NODE_READ_ADDITIONS).isdisjoint(REMOVED_BROAD_OR_UNNEEDED_READS))


if __name__ == "__main__":
    unittest.main()
