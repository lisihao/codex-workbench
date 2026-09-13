"""Closed source-file additions for the blocked DSH template integration repair."""

SCOPE_PROFILE_ID = "dsh-task-template-integration-v1"
PAIRING_WRITE_ID = "dsh-d-pairing-write-v1"
NOTE_WRITE_ID = "dsh-d-agent-note-write-v1"

_GENERATOR_WRITES = (
    "scripts/gen-cordis-catalog.ts",
    "packages/extensions/tool-cordis/src/api-catalog.ts",
    "packages/extensions/cordis-client-runner/src/client/slot-catalog.ts",
    "packages/core/session/src/known-event-types.ts",
)

_ASSEMBLED_SNAPSHOT_WRITES = (
    "examples/acp-agent/tests/acp.snapshot.ts",
    "examples/acp-agent/tests/fixtures/task-template/task-template.cordis.yml",
    "examples/acp-agent/tests/fixtures/task-template/task-template.cordis.snapshot.yml",
)

NODE_WRITE_ADDITIONS = (
    *_GENERATOR_WRITES,
    *_ASSEMBLED_SNAPSHOT_WRITES,
    "packages/prompt/task-template-rpc/package.json",
)

NODE_READ_ADDITIONS = tuple(dict.fromkeys((
    *NODE_WRITE_ADDITIONS,
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
)))

# The planner requires both node read and write scopes inside the task's
# allowed_scope. Added read-only inputs never become D or E write scopes.
TASK_ACCESS_ADDITIONS = NODE_READ_ADDITIONS

REQUIRED_PACKAGE_MARKERS = {
    "packages/prompt/task-template/package.json": "@deepseek-ai/dsh-task-template",
    "packages/client/ui-task-template/package.json": "@deepseek-ai/dsh-client-ui-task-template",
    "packages/prompt/task-template-rpc/package.json": "@deepseek-ai/dsh-task-template-rpc",
}
