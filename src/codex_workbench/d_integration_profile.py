"""Closed source-file additions for the blocked DSH template integration repair."""

SCOPE_PROFILE_ID = "dsh-task-template-integration-v1"
PAIRING_WRITE_ID = "dsh-d-pairing-write-v1"
NOTE_WRITE_ID = "dsh-d-agent-note-write-v1"

BASE_PAIRING_ANCHORS = (
    "docs/architecture.md",
    "docs/config-catalog.md",
    "docs/module-graph.md",
    "docs/subsystems/system-prompt.md",
    "packages/bundle/base/README.md",
    "packages/bundle/web-app/README.md",
    "packages/orchestration/orchestration/README.md",
    "packages/physical-operator/physical-operator/README.md",
    "packages/README.md",
    "packages/prompt/README.md",
    "packages/prompt/task-template-context/README.md",
    "packages/prompt/task-template/README.md",
)

TASK_SCOPE_ADDITIONS = (
    "scripts/gen-cordis-catalog.ts",
    "packages/extensions/tool-cordis/src/api-catalog.ts",
    "packages/extensions/cordis-client-runner/src/client/slot-catalog.ts",
    "packages/core/session/src/known-event-types.ts",
)

_SOURCE_README_ANCHORS = (
    "packages/core/system-prompt/README.md",
    "packages/prompt/task-template-context/README.md",
    "packages/prompt/task-template/README.md",
)

NODE_WRITE_ADDITIONS = (
    *TASK_SCOPE_ADDITIONS,
    "examples/acp-agent/tests/acp.snapshot.ts",
    "examples/acp-agent/tests/fixtures/task-template/task-template.cordis.yml",
    "examples/acp-agent/tests/fixtures/task-template/task-template.cordis.snapshot.yml",
    "packages/prompt/task-template-rpc/package.json",
    *(path for anchor in _SOURCE_README_ANCHORS for path in (
        anchor, anchor.removesuffix(".md") + ".zh.md", anchor.removesuffix(".md") + ".i18n.yaml",
    )),
    "packages/orchestration/orchestration/README.i18n.yaml",
    "packages/physical-operator/physical-operator/README.i18n.yaml",
    "packages/prompt/README.i18n.yaml",
)

NODE_READ_ADDITIONS = tuple(dict.fromkeys((
    *NODE_WRITE_ADDITIONS,
    *(path for anchor in BASE_PAIRING_ANCHORS for path in (anchor, anchor.removesuffix(".md") + ".zh.md")),
    "scripts/gen-client-catalog.ts",
    "scripts/gen-persistence-catalog.ts",
    "scripts/verify-translation-pairing.ts",
    "scripts/translation-pairing-git.ts",
    "scripts/translation-pairing.ts",
    "scripts/cordis-core-api.ts",
    "packages/typert/generator/src",
    "packages/prompt/task-template/src/service.ts",
    "packages/prompt/task-template/src/types.ts",
    "packages/client/ui-task-template/src/client/index.ts",
    "packages/client/ui-settings/src/client/contract/slots.ts",
    "packages/physical-operator/tool-physical-operator/src/index.ts",
    "packages/test-support/acp-snapshot/src/suite.ts",
    "packages/test-support/acp-snapshot/src/launcher.ts",
    "examples/acp-agent/cordis.yml",
)))

# The planner requires both node read and write scopes inside the task's
# allowed_scope. Added read-only inputs never become D or E write scopes.
TASK_ACCESS_ADDITIONS = tuple(dict.fromkeys((*TASK_SCOPE_ADDITIONS, *NODE_READ_ADDITIONS)))

REQUIRED_PACKAGE_MARKERS = {
    "packages/prompt/task-template/package.json": "@deepseek-ai/dsh-task-template",
    "packages/client/ui-task-template/package.json": "@deepseek-ai/dsh-client-ui-task-template",
    "packages/prompt/task-template-rpc/package.json": "@deepseek-ai/dsh-task-template-rpc",
}
