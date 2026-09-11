"""Preview and atomically record the fixed DSH Host-build acceptance prerequisite.

The Authority request journal owns mutation idempotency.  This module only
amends an already blocked task's immutable acceptance contract; it never
queues a task, changes an attempt, or executes the added commands.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
from typing import Any, Mapping

from .config import WorkbenchConfig
from .controlled_validation import ControlledValidationError, resolve_runtime
from .model import TaskContract, acceptance_command_argv, canonical_hash, canonical_json, now_iso
from .store import StateConflictError, WorkbenchStore


TOOL_NAME = "workbench_amend_task_acceptance"
PROFILE_ID = "dsh-host-types-before-client-v1"
_HOST_BUILD_SCRIPT = "tsc -b tsconfig.host.json && tsdown --env.DSH_BUILD_FACE host"
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_CLIENT_PROJECT_TARGETS = (
    "packages/prompt/task-template-rpc",
    "packages/client/ui-task-template",
    "packages/orchestration/orchestration-local",
    "packages/orchestration/tool-debate",
    "packages/orchestration/tool-orchestration",
    "packages/physical-operator/tool-physical-operator",
)

ACCEPTANCE_AMENDMENT_TOOL = {
    "name": TOOL_NAME,
    "description": "Preview or atomically add the fixed DSH Host type/build prerequisite before one blocked task's existing Client tsc acceptance command. It never runs commands, queues work, changes a node attempt, scope, models, quota, or historical results.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "task_id",
            "node_id",
            "expected_revision",
            "expected_attempt",
            "expected_contract_hash",
            "profile_id",
            "reason",
            "dry_run",
        ],
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "node_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
            "expected_attempt": {"type": "integer", "minimum": 1},
            "expected_contract_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "profile_id": {"const": PROFILE_ID},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "dry_run": {"type": "boolean"},
            "expected_fingerprint": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "Run only: fingerprint returned by a fresh dry-run preview.",
            },
        },
    },
}

# The generic MCP integration imports the descriptive name above.  Keep this
# alias for narrow callers that conventionally import ``TOOL`` from tool modules.
TOOL = ACCEPTANCE_AMENDMENT_TOOL


class AcceptanceAmendmentError(ValueError):
    """The fixed amendment cannot be safely constructed from current inputs."""


def _arguments(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the explicit preview/CAS request fields without defaulting them."""

    if not isinstance(raw, Mapping):
        raise ValueError("acceptance amendment arguments must be an object")
    schema = ACCEPTANCE_AMENDMENT_TOOL["inputSchema"]
    allowed = set(schema["properties"])
    required = set(schema["required"])
    supplied = set(raw)
    if supplied - allowed or required - supplied:
        raise ValueError("acceptance amendment accepts only its explicit identity and preview fields")
    values = dict(raw)
    for key in ("task_id", "node_id", "reason"):
        value = values[key]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{key} must be a non-empty string without outer whitespace")
    if len(values["reason"]) > 500:
        raise ValueError("reason must be at most 500 characters")
    for key, minimum in (("expected_revision", 0), ("expected_attempt", 1)):
        if type(values[key]) is not int or values[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    expected_hash = values["expected_contract_hash"]
    if not isinstance(expected_hash, str) or _FINGERPRINT_RE.fullmatch(expected_hash) is None:
        raise ValueError("expected_contract_hash must be a lowercase SHA-256 digest")
    if values["profile_id"] != PROFILE_ID:
        raise ValueError(f"profile_id must be {PROFILE_ID!r}")
    if type(values["dry_run"]) is not bool:
        raise ValueError("dry_run must be a boolean")
    fingerprint = values.get("expected_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or _FINGERPRINT_RE.fullmatch(fingerprint) is None
    ):
        raise ValueError("expected_fingerprint must be a lowercase SHA-256 digest")
    if not values["dry_run"] and fingerprint is None:
        raise ValueError("run requires expected_fingerprint from a fresh preview")
    return values


def _candidate(
    store: WorkbenchStore,
    connection: Any,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Read and fence the current blocked task/node/allocation without file IO."""

    task_id = str(arguments["task_id"])
    node_id = str(arguments["node_id"])
    task = connection.execute(
        """
        SELECT task_id, state, state_revision, contract_json, contract_hash
        FROM tasks WHERE task_id = ?
        """,
        (task_id,),
    ).fetchone()
    node = connection.execute(
        """
        SELECT task_id, node_id, state, attempt, worktree, recovery_json
        FROM nodes WHERE task_id = ? AND node_id = ?
        """,
        (task_id, node_id),
    ).fetchone()
    if task is None or node is None:
        raise KeyError((task_id, node_id))
    if int(task["state_revision"]) != arguments["expected_revision"]:
        raise StateConflictError(
            f"expected task revision {arguments['expected_revision']}, found {task['state_revision']}"
        )
    if task["contract_hash"] != arguments["expected_contract_hash"]:
        raise StateConflictError("expected_contract_hash does not match the current task contract")
    if int(node["attempt"]) != arguments["expected_attempt"]:
        raise StateConflictError(
            f"expected node attempt {arguments['expected_attempt']}, found {node['attempt']}"
        )
    if task["state"] != "blocked":
        raise StateConflictError(
            f"task {task_id} is {task['state']}, expected blocked"
        )
    if node["state"] != "blocked":
        raise StateConflictError(
            f"node {node_id} is {node['state']}, expected blocked"
        )
    active = connection.execute(
        """
        SELECT node_id, state FROM nodes
        WHERE task_id = ? AND state IN ('running', 'indeterminate')
        ORDER BY node_id
        """,
        (task_id,),
    ).fetchall()
    if active:
        details = ", ".join(f"{row['node_id']}:{row['state']}" for row in active)
        raise StateConflictError(
            "acceptance amendment requires every task node to have stopped: " + details
        )
    approval = connection.execute(
        "SELECT approval_id FROM approvals WHERE task_id = ? AND decision IS NULL LIMIT 1",
        (task_id,),
    ).fetchone()
    if approval is not None:
        raise StateConflictError("acceptance amendment is blocked by a pending approval")
    if node["recovery_json"] is not None:
        raise StateConflictError("acceptance amendment is blocked by the node recovery hold")
    store.assert_no_active_validation(connection, task_id)

    allocation = connection.execute(
        """
        SELECT allocation_id, task_id, node_id, attempt, repository, base_sha, branch,
               current_path, state
        FROM worktree_allocations
        WHERE task_id = ? AND node_id = ? AND attempt = ?
        """,
        (task_id, node_id, arguments["expected_attempt"]),
    ).fetchone()
    if allocation is None or allocation["state"] != "active":
        raise StateConflictError("blocked node has no active worktree allocation")
    if not node["worktree"] or allocation["current_path"] != node["worktree"]:
        raise StateConflictError(
            "blocked node worktree does not match its active allocation"
        )
    if str(allocation["allocation_id"]) in store._source_only_recovery_hold_ids(connection):
        raise StateConflictError("acceptance amendment is blocked by a retained recovery source")
    try:
        contract = json.loads(str(task["contract_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise StateConflictError("current task contract is invalid JSON") from error
    if not isinstance(contract, dict):
        raise StateConflictError("current task contract must be an object")
    if (
        allocation["repository"] != contract.get("repository")
        or allocation["base_sha"] != contract.get("base_sha")
    ):
        raise StateConflictError("active allocation no longer matches the current task contract")
    return {
        "task": {
            "task_id": task_id,
            "state": str(task["state"]),
            "revision": int(task["state_revision"]),
            "contract_json": str(task["contract_json"]),
            "contract_hash": str(task["contract_hash"]),
            "contract": contract,
        },
        "node": {
            "node_id": node_id,
            "state": str(node["state"]),
            "attempt": int(node["attempt"]),
            "worktree": str(node["worktree"]),
        },
        "allocation": {
            "allocation_id": str(allocation["allocation_id"]),
            "repository": str(allocation["repository"]),
            "base_sha": str(allocation["base_sha"]),
            "branch": str(allocation["branch"]),
            "current_path": str(allocation["current_path"]),
            "attempt": int(allocation["attempt"]),
        },
    }


def _snapshot(store: WorkbenchStore, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return one current durable amendment binding without holding a write lock."""

    with store.connection() as connection:
        return _candidate(store, connection, arguments)


def _worktree_root(path: str) -> Path:
    """Resolve the already allocated worktree without accepting a missing path."""

    try:
        root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StateConflictError("active worktree is unavailable") from error
    if not root.is_dir():
        raise StateConflictError("active worktree is not a directory")
    return root


def _regular_worktree_file(root: Path, relative: str) -> tuple[Path, str]:
    """Bind a non-symlinked repository configuration file inside a worktree."""

    candidate = root / PurePosixPath(relative)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AcceptanceAmendmentError(f"required worktree file is unavailable: {relative}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AcceptanceAmendmentError(f"required worktree file is not a regular file: {relative}")
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AcceptanceAmendmentError(f"required worktree file escapes its allocation: {relative}") from error
    try:
        return resolved, sha256(resolved.read_bytes()).hexdigest()
    except OSError as error:
        raise AcceptanceAmendmentError(f"required worktree file is unreadable: {relative}") from error


def _installed_entrypoint(root: Path, relative: str) -> tuple[Path, str]:
    """Bind one resolved installed entrypoint, allowing normal pnpm symlinks."""

    try:
        resolved = (root / PurePosixPath(relative)).resolve(strict=True)
        if not resolved.is_file():
            raise AcceptanceAmendmentError(
                f"required installed entrypoint is not a file: {relative}"
            )
        return resolved, sha256(resolved.read_bytes()).hexdigest()
    except (OSError, RuntimeError) as error:
        raise AcceptanceAmendmentError(
            f"required installed entrypoint is unavailable: {relative}"
        ) from error


def _client_tsc_index(commands: tuple[str, ...]) -> int:
    """Locate exactly one existing B Client partial-project build command."""

    client_matches: list[int] = []
    for index, source in enumerate(commands):
        argv = acceptance_command_argv(source)
        executable = PurePosixPath(argv[0])
        is_tsc_build = (
            len(argv) >= 3
            and executable.name == "tsc"
            and executable.parent.name == ".bin"
            and argv[1] == "-b"
        )
        if any(argument == "tsconfig.host.json" for argument in argv):
            raise AcceptanceAmendmentError(
                "acceptance already contains a Host tsc prerequisite"
            )
        if (
            any(argument == "DSH_BUILD_FACE=host" for argument in argv)
            or any(
                argv[position] == "--env.DSH_BUILD_FACE"
                and position + 1 < len(argv)
                and argv[position + 1] == "host"
                for position in range(len(argv))
            )
            or any(argument == "--env.DSH_BUILD_FACE=host" for argument in argv)
        ):
            raise AcceptanceAmendmentError(
                "acceptance already contains a Host tsdown prerequisite"
            )
        if is_tsc_build:
            if tuple(argv[2:]) != _CLIENT_PROJECT_TARGETS:
                raise AcceptanceAmendmentError(
                    "the existing .bin/tsc -b command is not the fixed B Client project list"
                )
            client_matches.append(index)
    if len(client_matches) != 1:
        raise AcceptanceAmendmentError(
            "acceptance must contain exactly one fixed B .bin/tsc -b project list"
        )
    return client_matches[0]


def _runtime_metadata(config: WorkbenchConfig) -> dict[str, Any]:
    """Resolve the installed Authority Node identity used by the fixed recipe."""

    try:
        runtime = resolve_runtime(config)
    except ControlledValidationError as error:
        raise AcceptanceAmendmentError(
            "Authority Node runtime cannot be resolved for acceptance amendment"
        ) from error
    return {
        "node_binary": str(runtime.node_binary),
        "node_identity": runtime.node_identity.to_dict(),
    }


def _plan(config: WorkbenchConfig, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Construct the fixed amended contract and immutable file/runtime metadata."""

    task = binding["task"]
    node = binding["node"]
    if not isinstance(task, Mapping) or not isinstance(node, Mapping):
        raise StateConflictError("acceptance amendment durable binding is invalid")
    root = _worktree_root(str(node["worktree"]))
    package_path, package_digest = _regular_worktree_file(root, "package.json")
    host_config, host_config_digest = _regular_worktree_file(root, "tsconfig.host.json")
    tsdown_config, tsdown_config_digest = _regular_worktree_file(root, "tsdown.config.ts")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceAmendmentError("worktree package.json is invalid") from error
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict) or scripts.get("build:lib:host") != _HOST_BUILD_SCRIPT:
        raise AcceptanceAmendmentError(
            "worktree package.json does not declare the required build:lib:host script"
        )
    raw_contract = task["contract"]
    if not isinstance(raw_contract, dict):
        raise StateConflictError("current task contract is invalid")
    old_contract = TaskContract.from_dict(raw_contract)
    commands = old_contract.acceptance_commands
    client_index = _client_tsc_index(commands)
    node_metadata = _runtime_metadata(config)
    host_tsc, host_tsc_digest = _installed_entrypoint(
        root, "node_modules/typescript/bin/tsc"
    )
    host_tsdown, host_tsdown_digest = _installed_entrypoint(
        root, "node_modules/tsdown/dist/run.mjs"
    )
    host_tsc_argv = (
        node_metadata["node_binary"],
        "node_modules/typescript/bin/tsc",
        "-b",
        "tsconfig.host.json",
    )
    host_tsdown_argv = (
        node_metadata["node_binary"],
        "node_modules/tsdown/dist/run.mjs",
        "--env.DSH_BUILD_FACE",
        "host",
    )
    amended_commands = (
        *commands[:client_index],
        shlex.join(host_tsc_argv),
        shlex.join(host_tsdown_argv),
        *commands[client_index:],
    )
    new_contract = TaskContract.from_dict(
        {**raw_contract, "acceptance_commands": list(amended_commands)}
    )
    old_without_acceptance = {
        key: value for key, value in raw_contract.items() if key != "acceptance_commands"
    }
    new_document = new_contract.to_dict()
    new_without_acceptance = {
        key: value for key, value in new_document.items() if key != "acceptance_commands"
    }
    if canonical_json(old_without_acceptance) != canonical_json(new_without_acceptance):
        raise AcceptanceAmendmentError(
            "acceptance amendment would normalize a non-acceptance contract field"
        )
    metadata = {
        "worktree": str(root),
        "client_tsc_index": client_index,
        "package_sha256": package_digest,
        "config_sha256": {
            "tsconfig.host.json": host_config_digest,
            "tsdown.config.ts": tsdown_config_digest,
        },
        "entrypoint_sha256": {
            str(host_tsc): host_tsc_digest,
            str(host_tsdown): host_tsdown_digest,
        },
        **node_metadata,
    }
    return {
        # Preserve the exact durable document in the audit event.  The new
        # document is deliberately normalized through TaskContract below.
        "old_contract": dict(raw_contract),
        "old_contract_hash": str(task["contract_hash"]),
        "new_contract": new_document,
        "new_contract_hash": new_contract.digest,
        "acceptance_commands": list(amended_commands),
        "exact_commands": [
            list(acceptance_command_argv(command)) for command in amended_commands
        ],
        "metadata": metadata,
    }


def _preview(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a stable amendment preview and verify its durable binding twice."""

    before = _snapshot(store, arguments)
    plan = _plan(config, before)
    after = _snapshot(store, arguments)
    if canonical_json(after) != canonical_json(before):
        raise StateConflictError("acceptance amendment durable binding changed during preview")
    fingerprint = canonical_hash(
        {
            "profile_id": PROFILE_ID,
            "task_id": arguments["task_id"],
            "node_id": arguments["node_id"],
            "expected_revision": arguments["expected_revision"],
            "expected_attempt": arguments["expected_attempt"],
            "expected_contract_hash": arguments["expected_contract_hash"],
            "reason": arguments["reason"],
            "binding": before,
            "new_contract_hash": plan["new_contract_hash"],
            "acceptance_commands": plan["acceptance_commands"],
            "metadata": plan["metadata"],
        }
    )
    return before, {**plan, "fingerprint": fingerprint}


def _response(
    arguments: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    dry_run: bool,
    revision: int,
    event_cursor: int | None = None,
) -> dict[str, Any]:
    """Return the exact immutable recipe without implying it has executed."""

    value = {
        "task_id": arguments["task_id"],
        "node_id": arguments["node_id"],
        "expected_attempt": arguments["expected_attempt"],
        "profile_id": PROFILE_ID,
        "dry_run": dry_run,
        "old_contract_hash": plan["old_contract_hash"],
        "new_contract_hash": plan["new_contract_hash"],
        "old_revision": arguments["expected_revision"],
        "new_revision": revision,
        "fingerprint": plan["fingerprint"],
        "acceptance_commands": list(plan["acceptance_commands"]),
        "exact_commands": [list(command) for command in plan["exact_commands"]],
        "metadata": dict(plan["metadata"]),
        "task_state_changed": False,
        "nodes_changed": False,
        "allocations_changed": False,
        "queued": False,
        "commands_executed": False,
    }
    if event_cursor is not None:
        value["event_cursor"] = event_cursor
    return value


def amend_task_acceptance(
    config: WorkbenchConfig,
    store: WorkbenchStore,
    raw_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Preview or CAS-record the fixed Host prerequisite for one blocked task.

    @param config: Authority configuration that resolves the pinned Node runtime.
    @param store: Durable task-state authority.
    @param raw_arguments: The strict MCP request fields.
    @returns: The preview or committed amendment receipt.
    """

    arguments = _arguments(raw_arguments)
    before, plan = _preview(config, store, arguments)
    if arguments["dry_run"]:
        return _response(
            arguments,
            plan,
            dry_run=True,
            revision=int(arguments["expected_revision"]) + 1,
        )
    if plan["fingerprint"] != arguments["expected_fingerprint"]:
        raise StateConflictError("acceptance amendment fingerprint changed; obtain a fresh preview")

    timestamp = now_iso()
    with store.transaction() as connection:
        current = _candidate(store, connection, arguments)
        if canonical_json(current) != canonical_json(before):
            raise StateConflictError("acceptance amendment durable binding changed before compare-and-set")
        revision = int(arguments["expected_revision"]) + 1
        changed = connection.execute(
            """
            UPDATE tasks
            SET contract_json = ?, contract_hash = ?, state_revision = ?, updated_at = ?
            WHERE task_id = ? AND state = 'blocked' AND state_revision = ?
              AND contract_hash = ? AND contract_json = ?
            """,
            (
                canonical_json(plan["new_contract"]),
                plan["new_contract_hash"],
                revision,
                timestamp,
                arguments["task_id"],
                arguments["expected_revision"],
                plan["old_contract_hash"],
                current["task"]["contract_json"],
            ),
        ).rowcount
        if changed != 1:
            raise StateConflictError("acceptance amendment task compare-and-set failed")
        event_cursor = store._event(
            connection,
            "task.acceptance_amended",
            str(arguments["task_id"]),
            str(arguments["node_id"]),
            {
                "old_contract": plan["old_contract"],
                "old_contract_hash": plan["old_contract_hash"],
                "new_contract": plan["new_contract"],
                "new_contract_hash": plan["new_contract_hash"],
                "profile_id": PROFILE_ID,
                "reason": arguments["reason"],
                "preview_fingerprint": plan["fingerprint"],
                "attempt": arguments["expected_attempt"],
                "revision": revision,
            },
            created_at=timestamp,
        )
    return _response(
        arguments,
        plan,
        dry_run=False,
        revision=revision,
        event_cursor=event_cursor,
    )
