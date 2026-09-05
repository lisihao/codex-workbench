from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
from typing import Any, Callable, Iterator

from .model import (
    NodeResult,
    NodeSpec,
    QuotaSnapshot,
    TaskContract,
    canonical_hash,
    canonical_json,
    codex_model_profile,
    codex_model_reasoning_effort,
    is_codex_control_plane_model,
    now_iso,
    retry_model,
)
from .artifacts import ArtifactStore, presentation_format
from .dependency_inputs import load_recorded_dependency_input
from .dirty_worktree_recovery import DirtyWorktreeRecovery, DirtyWorktreeRecoveryError
from .governance import governance_identity
from .legacy_evidence import load_manifest, validate_manifest
from .planner import propose_archify_reconciliation
from .scheduler_metrics import (
    EXECUTION_LANES,
    execution_lane_for_spec,
    quota_pool_id_for_spec,
)
from .worktrees import (
    WorktreeManager,
    normalize_scope,
    scope_access_conflicts,
    scope_allows,
    scopes_overlap,
)


SCHEMA_VERSION = 12
_ARCHIFY_RENDER_COMMANDS = frozenset({"deliver", "compare", "visual-check"})
_ARCHIFY_RECEIPT_ONLY_COMMANDS = frozenset({"validate", "migrate"})

_DIRTY_WORKTREE_RECOVERY_KIND = "blocked-worktree-recovery"
_DIRTY_WORKTREE_RECOVERY_PROVIDER = "workbench-dirty-worktree-recovery"

def _repository_identity(repository: str) -> str:
    root = Path(repository).expanduser().resolve()
    try:
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True, capture_output=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return str(root)
    return str(Path(common).resolve())


class CommandConflictError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
    pass


def _normalize_execution_lanes(lanes: tuple[str, ...] | None) -> frozenset[str] | None:
    if lanes is None:
        return None
    normalized = frozenset(lanes)
    invalid = normalized - set(EXECUTION_LANES)
    if invalid:
        raise ValueError(f"unsupported execution lanes: {sorted(invalid)}")
    return normalized


def _normalize_lane_capacities(capacities: dict[str, int] | None) -> dict[str, int]:
    if capacities is None:
        return {}
    normalized: dict[str, int] = {}
    for lane, capacity in capacities.items():
        if lane not in EXECUTION_LANES:
            raise ValueError(f"unsupported execution lane capacity: {lane!r}")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError("lane capacity must be a non-negative integer")
        normalized[lane] = capacity
    return normalized


class WorkbenchStore:
    def __init__(self, path: Path):
        self.path = path
        self._init_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._init_lock, self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    contract_json TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    state_revision INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    blocker TEXT,
                    verdict TEXT
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    worktree TEXT,
                    effective_executor TEXT,
                    effective_model TEXT,
                    started_at TEXT,
                    settled_at TEXT,
                    result_json TEXT,
                    recovery_json TEXT,
                    coordinator_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    node_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_receipts (
                    command_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    decision TEXT,
                    decided_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_steering (
                    steering_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    instruction TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_receipts (
                    command_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    state TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_cache (
                    cache_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    source_task_id TEXT NOT NULL,
                    source_node_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS context_import_receipts (
                    command_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    source_thread_id TEXT NOT NULL,
                    context_ref TEXT NOT NULL,
                    archive_ref TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    allowed_scopes_json TEXT NOT NULL,
                    context_excerpt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_bindings (
                    source_thread_id TEXT PRIMARY KEY,
                    context_ref TEXT NOT NULL,
                    active_task_id TEXT REFERENCES tasks(task_id),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worktree_allocations (
                    allocation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    repository TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    node_result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, node_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS worktree_archives (
                    archive_id TEXT PRIMARY KEY,
                    allocation_id TEXT,
                    source_host TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    archive_path TEXT,
                    archive_sha256 TEXT,
                    size_bytes INTEGER,
                    state TEXT NOT NULL,
                    manifest_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    verified_at TEXT,
                    purged_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS home_presence_leases (
                    client_id TEXT PRIMARY KEY,
                    route TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS nodes_state_idx ON nodes(state, updated_at);
                CREATE INDEX IF NOT EXISTS events_task_cursor_idx ON events(task_id, cursor);
                CREATE INDEX IF NOT EXISTS tasks_state_updated_idx ON tasks(state, updated_at);
                CREATE INDEX IF NOT EXISTS task_steering_task_created_idx
                    ON task_steering(task_id, created_at);
                CREATE INDEX IF NOT EXISTS context_import_thread_created_idx
                    ON context_import_receipts(source_thread_id, created_at);
                CREATE INDEX IF NOT EXISTS worktree_allocations_state_idx
                    ON worktree_allocations(state, updated_at);
                CREATE INDEX IF NOT EXISTS worktree_archives_state_idx
                    ON worktree_archives(state, updated_at);
                """
            )
            current = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(current["value"]) in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}:
                node_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
                }
                if "effective_executor" not in node_columns:
                    connection.execute("ALTER TABLE nodes ADD COLUMN effective_executor TEXT")
                if "effective_model" not in node_columns:
                    connection.execute("ALTER TABLE nodes ADD COLUMN effective_model TEXT")
                if "coordinator_epoch" not in node_columns:
                    connection.execute(
                        "ALTER TABLE nodes ADD COLUMN coordinator_epoch INTEGER NOT NULL DEFAULT 0"
                    )
                if "lease_epoch" not in node_columns:
                    connection.execute(
                        "ALTER TABLE nodes ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0"
                    )
                if "recovery_json" not in node_columns:
                    connection.execute("ALTER TABLE nodes ADD COLUMN recovery_json TEXT")
                task_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
                if "priority" not in task_columns:
                    connection.execute(
                        "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
                    )
                steering_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(task_steering)"
                    ).fetchall()
                }
                if "sequence" not in steering_columns:
                    connection.execute(
                        "ALTER TABLE task_steering ADD COLUMN sequence INTEGER"
                    )
                # v9 had no explicit sequence.  Its durable semantics were
                # chronological steering with the stable steering ID as the
                # tie-breaker; rowid reflected insertion/storage order only
                # and can be reversed by import/rebuild paths.
                steering_rows = connection.execute(
                    """
                    SELECT rowid, task_id, steering_id, created_at, sequence
                    FROM task_steering
                    ORDER BY task_id, created_at, steering_id, rowid
                    """
                ).fetchall()
                next_sequences: dict[str, int] = {}
                for steering_row in steering_rows:
                    task_id = str(steering_row["task_id"])
                    current_sequence = next_sequences.get(task_id, 0)
                    stored_sequence = steering_row["sequence"]
                    if stored_sequence is None or int(stored_sequence) <= current_sequence:
                        stored_sequence = current_sequence + 1
                        connection.execute(
                            "UPDATE task_steering SET sequence = ? WHERE rowid = ?",
                            (stored_sequence, steering_row["rowid"]),
                        )
                    next_sequences[task_id] = int(stored_sequence)
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                # v8 binds reusable Evidence to the code-as-harness governance
                # receipt; older rows cannot prove which profile governed them.
                connection.execute("DELETE FROM evidence_cache")
            elif int(current["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported schema version {current['value']}; expected {SCHEMA_VERSION}"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_steering_task_sequence_idx "
                "ON task_steering(task_id, sequence)"
            )
        self.path.chmod(0o600)

    @property
    def artifacts(self) -> ArtifactStore:
        return ArtifactStore(self.path.parent / "artifacts")

    def activate_coordinator(self, instance_id: str, authority_machine_id: str) -> int:
        if not instance_id.strip():
            raise ValueError("coordinator instance_id is required")
        if not authority_machine_id.strip():
            raise ValueError("authority_machine_id is required")
        with self.transaction() as connection:
            owner = connection.execute(
                "SELECT value FROM metadata WHERE key = 'authority_machine_id'"
            ).fetchone()
            if owner is not None and owner["value"] != authority_machine_id:
                raise StateConflictError(
                    "authority ledger belongs to a different machine ID"
                )
            if owner is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('authority_machine_id', ?)",
                    (authority_machine_id,),
                )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'coordinator_epoch'"
            ).fetchone()
            epoch = (int(row["value"]) if row else 0) + 1
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('coordinator_epoch', ?)",
                (str(epoch),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('coordinator_instance_id', ?)",
                (instance_id,),
            )
            return epoch

    @staticmethod
    def _next_lease_epoch(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'node_lease_epoch'"
        ).fetchone()
        epoch = (int(row["value"]) if row else 0) + 1
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('node_lease_epoch', ?)",
            (str(epoch),),
        )
        return epoch

    @staticmethod
    def _assert_active_coordinator(
        connection: sqlite3.Connection,
        coordinator_epoch: int,
    ) -> None:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'coordinator_epoch'"
        ).fetchone()
        if row is None or int(row["value"]) != coordinator_epoch:
            raise StateConflictError("coordinator lease epoch is stale")

    def cached_evidence(self, cache_key: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            result = json.loads(row["result_json"])
            try:
                self._verify_artifact_refs(result.get("artifacts", {}))
            except ValueError:
                connection.execute(
                    "DELETE FROM evidence_cache WHERE cache_key = ?", (cache_key,)
                )
                return None
            return {
                "cache_key": row["cache_key"],
                "result": result,
                "source_task_id": row["source_task_id"],
                "source_node_id": row["source_node_id"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "use_count": row["use_count"],
            }

    def _verify_artifact_refs(self, artifacts: dict[str, str]) -> None:
        for name, ref in artifacts.items():
            if not isinstance(ref, str):
                raise ValueError(f"artifact {name!r} must be a content-addressed ref")
            self.artifacts.verify(ref)

    def save_evidence(
        self,
        cache_key: str,
        result: NodeResult,
        task_id: str,
        node_id: str,
    ) -> None:
        self._verify_artifact_refs(result.artifacts)
        timestamp = now_iso()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO evidence_cache(
                    cache_key, result_json, source_task_id, source_node_id,
                    created_at, last_used_at, use_count
                ) VALUES(?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO NOTHING
                """,
                (cache_key, canonical_json(result.to_dict()), task_id, node_id, timestamp, timestamp),
            )

    def record_evidence_reuse(
        self,
        cache_key: str,
        task_id: str,
        node_id: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                raise KeyError(cache_key)
            connection.execute(
                """
                UPDATE evidence_cache SET last_used_at = ?, use_count = use_count + 1
                WHERE cache_key = ?
                """,
                (timestamp, cache_key),
            )
            source = {
                "cache_key": cache_key,
                "source_task_id": row["source_task_id"],
                "source_node_id": row["source_node_id"],
            }
            self._event(connection, "node.evidence_reused", task_id, node_id, source)
            return source

    def begin_delivery(self, task_id: str, command_id: str, request: dict[str, Any]) -> dict[str, Any]:
        request_hash = canonical_hash(request)
        timestamp = now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise CommandConflictError(
                        f"delivery command {command_id!r} was already used with a different request"
                    )
                return self._delivery_row(existing)
            task = connection.execute(
                "SELECT state, contract_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["state"] != "accepted":
                raise StateConflictError(f"task {task_id} is {task['state']}, expected accepted")
            contract = json.loads(task["contract_json"])
            if not contract.get("external_write_permission", False):
                raise StateConflictError(
                    f"task {task_id} contract does not authorize external GitHub writes"
                )
            details = {"request": request}
            connection.execute(
                """
                INSERT INTO delivery_receipts(
                    command_id, request_hash, task_id, state, details_json, created_at, updated_at
                ) VALUES(?, ?, ?, 'accepted', ?, ?, ?)
                """,
                (command_id, request_hash, task_id, canonical_json(details), timestamp, timestamp),
            )
            self._event(
                connection,
                "delivery.accepted",
                task_id,
                None,
                {"command_id": command_id, "request_hash": request_hash},
            )
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            assert row is not None
            return self._delivery_row(row)

    def update_delivery(
        self,
        command_id: str,
        state: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            merged = {**json.loads(row["details_json"]), **details}
            connection.execute(
                """
                UPDATE delivery_receipts SET state = ?, details_json = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (state, canonical_json(merged), timestamp, command_id),
            )
            self._event(
                connection,
                f"delivery.{state}",
                row["task_id"],
                None,
                {"command_id": command_id, **details},
            )
            updated = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            assert updated is not None
            return self._delivery_row(updated)

    def get_delivery(self, command_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            return self._delivery_row(row)

    @staticmethod
    def _delivery_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "command_id": row["command_id"],
            "request_hash": row["request_hash"],
            "task_id": row["task_id"],
            "state": row["state"],
            "details": json.loads(row["details_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        task_id: str | None,
        node_id: str | None,
        payload: dict[str, Any],
        *,
        created_at: str | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (event_type, task_id, node_id, canonical_json(payload), created_at or now_iso()),
        ).lastrowid
        assert cursor is not None
        return int(cursor)

    def create_task(
        self,
        contract: TaskContract,
        nodes: list[NodeSpec],
        command_id: str,
    ) -> str:
        contract.validate()
        if not nodes:
            raise ValueError("task requires at least one node")
        if any(node.task_id != contract.task_id for node in nodes):
            raise ValueError("all nodes must belong to the contract task_id")
        node_ids = {node.node_id for node in nodes}
        if len(node_ids) != len(nodes):
            raise ValueError("node_id must be unique within a task")
        for node in nodes:
            node.validate()
            for scope in (*node.read_scopes, *node.write_scopes):
                normalize_scope(scope)
                if not scope_allows(scope, list(contract.allowed_scope), []):
                    raise ValueError(f"node {node.node_id} scope {scope!r} exceeds the task contract")
                if any(scopes_overlap(scope, forbidden) for forbidden in contract.forbidden_scope):
                    raise ValueError(
                        f"node {node.node_id} scope {scope!r} overlaps forbidden scope"
                    )
            missing = set(node.depends_on) - node_ids
            if missing:
                raise ValueError(f"node {node.node_id} has missing dependencies: {sorted(missing)}")
        self._assert_verifier_contract(contract, nodes)
        self._assert_acyclic(nodes)

        request = {"contract": contract.to_dict(), "nodes": [node.to_dict() for node in nodes]}
        request_hash = canonical_hash(request)
        timestamp = now_iso()
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT request_hash, task_id FROM command_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise CommandConflictError(
                        f"command {command_id!r} was already used with a different request"
                    )
                return str(receipt["task_id"])

            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, contract_json, contract_hash, state,
                    state_revision, created_at, updated_at
                ) VALUES(?, ?, ?, 'inbox', 1, ?, ?)
                """,
                (
                    contract.task_id,
                    canonical_json(contract.to_dict()),
                    contract.digest,
                    timestamp,
                    timestamp,
                ),
            )
            for node in sorted(nodes, key=lambda item: (item.ordinal, item.node_id)):
                connection.execute(
                    """
                    INSERT INTO nodes(task_id, node_id, spec_json, state, updated_at)
                    VALUES(?, ?, ?, 'pending', ?)
                    """,
                    (contract.task_id, node.node_id, canonical_json(node.to_dict()), timestamp),
                )
            connection.execute(
                """
                INSERT INTO command_receipts(command_id, request_hash, task_id, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (command_id, request_hash, contract.task_id, timestamp),
            )
            self._event(
                connection,
                "task.created",
                contract.task_id,
                None,
                {"contract_hash": contract.digest, "node_count": len(nodes)},
            )
        return contract.task_id

    @staticmethod
    def _assert_acyclic(nodes: list[NodeSpec]) -> None:
        dependencies = {node.node_id: set(node.depends_on) for node in nodes}
        remaining = set(dependencies)
        while remaining:
            ready = {node_id for node_id in remaining if not (dependencies[node_id] & remaining)}
            if not ready:
                raise ValueError("task graph contains a cycle")
            remaining -= ready

    @staticmethod
    def _assert_verifier_contract(contract: TaskContract, nodes: list[NodeSpec]) -> None:
        verifiers = [node for node in nodes if node.verifier]
        if not verifiers:
            raise ValueError("task graph must contain exactly one verifier")
        if len(verifiers) != 1:
            raise ValueError("task graph must contain exactly one verifier")
        verifier = verifiers[0]
        workers = {node.node_id for node in nodes if not node.verifier}
        if set(verifier.depends_on) != workers:
            raise ValueError("verifier must depend on every required worker")
        if verifier.executor == "fixture":
            return
        if contract.verifier_model == "fixture" and verifier.model == "fixture":
            return
        if (
            verifier.executor != "codex"
            or not is_codex_control_plane_model(verifier.model)
        ):
            raise ValueError("verifier must be an exact Codex control-plane node")
        if (
            verifier.model != contract.verifier_model
            or not is_codex_control_plane_model(contract.verifier_model)
        ):
            raise ValueError(
                "verifier must match the exact Codex control-plane verifier_model in the contract"
            )

    def transition_task(
        self,
        task_id: str,
        state: str,
        *,
        expected_revision: int | None = None,
        blocker: str | None = None,
        verdict: str | None = None,
    ) -> int:
        timestamp = now_iso()
        allowed = {
            "inbox": {"queued", "cancelled"},
            "planning": {"queued", "paused", "cancelled"},
            "ready": {"queued", "paused", "cancelled"},
            "queued": {"paused", "cancelled"},
            "running": {"paused", "cancelled"},
            "verifying": {"paused", "cancelled"},
            "needs_fix": {"queued", "cancelled"},
            "needs_approval": {"queued", "cancelled"},
            "paused": {"queued", "cancelled"},
        }
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if expected_revision is not None and task["state_revision"] != expected_revision:
                raise StateConflictError(
                    f"expected task revision {expected_revision}, found {task['state_revision']}"
                )
            if state == "accepted":
                raise StateConflictError("accepted is verifier-owned and cannot be set by task control")
            if state not in allowed.get(str(task["state"]), set()):
                raise StateConflictError(f"cannot transition task from {task['state']} to {state}")
            revision = int(task["state_revision"]) + 1
            connection.execute(
                """
                UPDATE tasks
                SET state = ?, state_revision = ?, updated_at = ?, blocker = ?, verdict = ?
                WHERE task_id = ?
                """,
                (state, revision, timestamp, blocker, verdict, task_id),
            )
            self._event(
                connection,
                "task.state_changed",
                task_id,
                None,
                {"from": task["state"], "to": state, "revision": revision, "blocker": blocker},
            )
            return revision

    def queue_task(self, task_id: str) -> int:
        task = self.get_task(task_id)
        if task["state"] not in {"inbox", "planning", "ready", "needs_fix", "paused"}:
            raise StateConflictError(f"cannot queue task from {task['state']}")
        if task["state"] == "needs_fix":
            with self.transaction() as connection:
                connection.execute(
                    """
                    UPDATE nodes SET state = 'pending', worker_id = NULL,
                                     started_at = NULL, settled_at = NULL, updated_at = ?
                    WHERE task_id = ? AND state = 'failed'
                    """,
                    (now_iso(), task_id),
                )
        return self.transition_task(task_id, "queued", expected_revision=task["state_revision"])

    @staticmethod
    def _blocked_retry_candidate(
        connection: sqlite3.Connection,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
    ) -> dict[str, Any]:
        task = connection.execute(
            "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        node = connection.execute(
            "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?", (task_id, node_id)
        ).fetchone()
        if task is None or node is None:
            raise KeyError((task_id, node_id))
        if int(task["state_revision"]) != expected_revision:
            raise StateConflictError(
                f"expected task revision {expected_revision}, found {task['state_revision']}"
            )
        if int(node["attempt"]) != expected_attempt:
            raise StateConflictError(
                f"expected node attempt {expected_attempt}, found {node['attempt']}"
            )
        if task["state"] != "blocked":
            raise StateConflictError(f"task {task_id} is {task['state']}, expected blocked")
        if node["state"] != "blocked":
            raise StateConflictError(f"node {node_id} is {node['state']}, expected blocked")

        active = connection.execute(
            """
            SELECT node_id, state FROM nodes
            WHERE task_id = ? AND state IN ('running', 'indeterminate')
            ORDER BY node_id
            """,
            (task_id,),
        ).fetchall()
        if active:
            states = ", ".join(f"{row['node_id']}:{row['state']}" for row in active)
            raise StateConflictError(
                f"cannot retry blocked node while task has running or indeterminate nodes: {states}"
            )

        if not node["result_json"]:
            raise StateConflictError("blocked node has no latest result receipt")
        try:
            result = json.loads(node["result_json"])
        except json.JSONDecodeError as error:
            raise StateConflictError("blocked node result receipt is invalid JSON") from error
        if not isinstance(result, dict) or result.get("status") != "blocked":
            raise StateConflictError("latest node result must explicitly be blocked")
        if "changed_paths" not in result or not isinstance(result["changed_paths"], list):
            raise StateConflictError("blocked node result must contain changed_paths as an explicit list")
        if result["changed_paths"]:
            raise StateConflictError("blocked node result changed_paths must be an explicit empty list")
        if result.get("verdict") not in {None, "blocked"}:
            raise StateConflictError("blocked node result has a contradictory verdict")

        try:
            spec = json.loads(node["spec_json"])
        except json.JSONDecodeError as error:
            raise StateConflictError("blocked node specification is invalid JSON") from error
        if not isinstance(spec, dict):
            raise StateConflictError("blocked node specification must be an object")
        requested_executor = spec.get("executor")
        requested_model = spec.get("model")
        effective_executor = node["effective_executor"]
        effective_model = node["effective_model"]
        if not isinstance(requested_executor, str) or not requested_executor:
            raise StateConflictError("blocked node has no requested executor")
        if not isinstance(requested_model, str) or not requested_model:
            raise StateConflictError("blocked node has no requested model")
        if not isinstance(effective_executor, str) or not effective_executor:
            raise StateConflictError("blocked node has no effective executor to preserve")
        if not isinstance(effective_model, str) or not effective_model:
            raise StateConflictError("blocked node has no effective model to preserve")

        def route(executor: str, model: str) -> dict[str, Any]:
            return {
                "executor": executor,
                "model": model,
                "model_profile": codex_model_profile(model),
                "model_reasoning_effort": codex_model_reasoning_effort(model),
            }

        return {
            "task": {
                "task_id": task_id,
                "state": str(task["state"]),
                "revision": int(task["state_revision"]),
            },
            "node": {
                "node_id": node_id,
                "state": str(node["state"]),
                "attempt": int(node["attempt"]),
            },
            "requested_route": route(requested_executor, requested_model),
            "effective_route": route(effective_executor, effective_model),
            "would_retry": {
                "attempt": int(node["attempt"]) + 1,
                **route(effective_executor, effective_model),
            },
        }

    @staticmethod
    def _blocked_retry_authorization(
        connection: sqlite3.Connection,
        task_id: str,
        node_id: str,
        next_attempt: int,
    ) -> dict[str, Any] | None:
        if next_attempt <= 1:
            return None
        rows = connection.execute(
            """
            SELECT cursor, payload_json FROM events
            WHERE task_id = ? AND node_id = ? AND event_type = 'node.blocked_retry_authorized'
            ORDER BY cursor DESC
            """,
            (task_id, node_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as error:
                raise StateConflictError("blocked retry authorization receipt is invalid JSON") from error
            if not isinstance(payload, dict) or payload.get("next_attempt") != next_attempt:
                continue
            if payload.get("original_attempt") != next_attempt - 1:
                raise StateConflictError("blocked retry authorization attempt is inconsistent")
            assertion = payload.get("operator_assertion")
            route = payload.get("effective_route")
            if (
                not isinstance(assertion, dict)
                or assertion.get("confirm_no_side_effects") is not True
                or assertion.get("automatically_verified") is not False
                or not isinstance(route, dict)
                or not isinstance(route.get("executor"), str)
                or not route["executor"]
                or not isinstance(route.get("model"), str)
                or not route["model"]
            ):
                raise StateConflictError("blocked retry authorization receipt is incomplete")
            return {
                "event_cursor": int(row["cursor"]),
                "executor": route["executor"],
                "model": route["model"],
            }
        return None

    def retry_blocked_node(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
        reason: str,
        confirm_no_side_effects: bool,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        reason = reason.strip()
        if not reason:
            raise ValueError("blocked retry reason must be non-empty")
        if not confirm_no_side_effects:
            raise ValueError("blocked retry requires an explicit no-side-effects operator assertion")

        if dry_run:
            with self.connection() as connection:
                candidate = self._blocked_retry_candidate(
                    connection,
                    task_id,
                    node_id,
                    expected_revision=expected_revision,
                    expected_attempt=expected_attempt,
                )
            return {
                "task_id": task_id,
                "node_id": node_id,
                "dry_run": True,
                "task": candidate["task"],
                "node": candidate["node"],
                "would_retry": candidate["would_retry"],
                "operator_asserted": True,
                "automatically_verified": False,
            }

        timestamp = now_iso()
        with self.transaction() as connection:
            candidate = self._blocked_retry_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )
            changed = connection.execute(
                """
                UPDATE nodes
                SET state = 'pending', worker_id = NULL, worktree = NULL,
                    started_at = NULL, settled_at = NULL,
                    coordinator_epoch = 0, lease_epoch = 0, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'blocked' AND attempt = ?
                """,
                (timestamp, task_id, node_id, expected_attempt),
            ).rowcount
            if changed != 1:
                raise StateConflictError("blocked retry node compare-and-set failed")
            revision = expected_revision + 1
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = 'queued', state_revision = ?, updated_at = ?, blocker = NULL, verdict = NULL
                WHERE task_id = ? AND state = 'blocked' AND state_revision = ?
                """,
                (revision, timestamp, task_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise StateConflictError("blocked retry task compare-and-set failed")

            authorization_cursor = self._event(
                connection,
                "node.blocked_retry_authorized",
                task_id,
                node_id,
                {
                    "original_attempt": expected_attempt,
                    "next_attempt": candidate["would_retry"]["attempt"],
                    "reason": reason,
                    "operator_assertion": {
                        "confirm_no_side_effects": True,
                        "assertion": "operator_asserted",
                        "automatically_verified": False,
                    },
                    "requested_route": candidate["requested_route"],
                    "effective_route": candidate["effective_route"],
                    "task_revision": revision,
                },
                created_at=timestamp,
            )
            self._event(
                connection,
                "task.state_changed",
                task_id,
                None,
                {
                    "from": "blocked",
                    "to": "queued",
                    "revision": revision,
                    "blocker": None,
                },
                created_at=timestamp,
            )
            return {
                "task_id": task_id,
                "node_id": node_id,
                "dry_run": False,
                "revision": revision,
                "task": {"task_id": task_id, "state": "queued", "revision": revision},
                "node": {
                    "node_id": node_id,
                    "state": "pending",
                    "attempt": expected_attempt,
                },
                "next_attempt": candidate["would_retry"]["attempt"],
                "authorized_route": candidate["effective_route"],
                "operator_asserted": True,
                "automatically_verified": False,
                "authorization_event_cursor": authorization_cursor,
            }


    @staticmethod
    def _blocked_worktree_recovery_candidate(
        connection: sqlite3.Connection,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
    ) -> dict[str, Any]:
        task = connection.execute(
            "SELECT state, state_revision, contract_json FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        node = connection.execute(
            "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?", (task_id, node_id)
        ).fetchone()
        if task is None or node is None:
            raise KeyError((task_id, node_id))
        if int(task["state_revision"]) != expected_revision:
            raise StateConflictError(
                f"expected task revision {expected_revision}, found {task['state_revision']}"
            )
        if int(node["attempt"]) != expected_attempt:
            raise StateConflictError(
                f"expected node attempt {expected_attempt}, found {node['attempt']}"
            )
        if task["state"] not in {"blocked", "queued", "running"}:
            raise StateConflictError(
                f"task {task_id} is {task['state']}, expected blocked, queued, or running"
            )
        if node["state"] != "blocked":
            raise StateConflictError(f"node {node_id} is {node['state']}, expected blocked")
        if node["recovery_json"] is not None:
            raise StateConflictError("blocked node already has a pending dirty-worktree recovery")
        if not node["result_json"]:
            raise StateConflictError("blocked node has no latest result receipt")
        try:
            result = json.loads(node["result_json"])
        except json.JSONDecodeError as error:
            raise StateConflictError("blocked node result receipt is invalid JSON") from error
        changed_paths = result.get("changed_paths") if isinstance(result, dict) else None
        if result.get("status") != "blocked" or not isinstance(changed_paths, list) or not changed_paths:
            raise StateConflictError(
                "dirty-worktree recovery requires a blocked result with non-empty changed_paths"
            )
        if not all(isinstance(path, str) and path for path in changed_paths):
            raise StateConflictError("blocked node changed_paths are invalid")
        allocation = connection.execute(
            """
            SELECT * FROM worktree_allocations
            WHERE task_id = ? AND node_id = ? AND attempt = ?
            """,
            (task_id, node_id, expected_attempt),
        ).fetchone()
        if allocation is None or allocation["state"] != "active":
            raise StateConflictError("blocked node has no active physical worktree allocation")
        if not node["worktree"] or allocation["current_path"] != node["worktree"]:
            raise StateConflictError("blocked node worktree does not match its physical allocation")
        contract = json.loads(task["contract_json"])
        try:
            spec = json.loads(node["spec_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise StateConflictError("blocked node spec is invalid JSON") from error
        allowed_scope = contract.get("allowed_scope") if isinstance(contract, dict) else None
        forbidden_scope = contract.get("forbidden_scope") if isinstance(contract, dict) else None
        write_scopes = spec.get("write_scopes") if isinstance(spec, dict) else None
        if not (
            isinstance(allowed_scope, list)
            and all(isinstance(scope, str) for scope in allowed_scope)
            and isinstance(forbidden_scope, list)
            and all(isinstance(scope, str) for scope in forbidden_scope)
            and isinstance(write_scopes, list)
            and all(isinstance(scope, str) for scope in write_scopes)
        ):
            raise StateConflictError("blocked-worktree recovery scopes are invalid")
        return {
            "task": {
                "task_id": task_id,
                "state": str(task["state"]),
                "revision": int(task["state_revision"]),
                "allowed_scope": tuple(allowed_scope),
                "forbidden_scope": tuple(forbidden_scope),
            },
            "node": {
                "node_id": node_id,
                "state": str(node["state"]),
                "attempt": int(node["attempt"]),
                "write_scopes": tuple(write_scopes),
            },
            "source": {
                "worktree": str(allocation["current_path"]),
                "branch": str(allocation["branch"]),
                "base_sha": str(contract["base_sha"]),
                "changed_paths": tuple(sorted(changed_paths)),
                "allocation_id": str(allocation["allocation_id"]),
            },
            "source_result_json": str(node["result_json"]),
        }

    def blocked_worktree_recovery_candidate(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            return self._blocked_worktree_recovery_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )

    @staticmethod
    def _recovery_git_bytes(worktree: Path, *arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(worktree), *arguments],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except OSError as error:
            raise StateConflictError(
                f"cannot inspect blocked-worktree recovery worktree: {error}"
            ) from error
        if completed.returncode:
            raise StateConflictError(
                completed.stderr.decode(errors="replace").strip()
                or completed.stdout.decode(errors="replace").strip()
                or "cannot inspect blocked-worktree recovery worktree"
            )
        return bytes(completed.stdout)

    def _validate_blocked_worktree_recovery_capture(
        self,
        candidate: dict[str, Any],
        expected_attempt: int,
        recovery: dict[str, Any],
    ) -> dict[str, Any]:
        common = {
            "schema_version",
            "source_attempt",
            "source_worktree",
            "source_branch",
            "base_sha",
            "changed_paths",
            "patch_ref",
            "patch_sha256",
        }
        schema_version = recovery.get("schema_version")
        if schema_version == 1:
            required = common
        elif schema_version in {2, 3}:
            required = common | {
                "source_task_id",
                "source_node_id",
                "input_tree_sha",
                "dependency_input_ref",
            }
            if schema_version == 3:
                required.add("untracked_paths")
        else:
            raise StateConflictError("dirty-worktree recovery receipt schema is unsupported")
        if set(recovery) != required:
            raise StateConflictError("dirty-worktree recovery receipt has an invalid shape")
        source = candidate["source"]
        if (
            recovery["source_attempt"] != expected_attempt
            or recovery["source_branch"] != source["branch"]
            or recovery["base_sha"] != source["base_sha"]
            or tuple(sorted(recovery["changed_paths"])) != source["changed_paths"]
        ):
            raise StateConflictError(
                "dirty-worktree recovery receipt does not match the blocked allocation"
            )
        for field in (
            "source_worktree",
            "source_branch",
            "base_sha",
            "patch_ref",
            "patch_sha256",
        ):
            if not isinstance(recovery[field], str) or not recovery[field]:
                raise StateConflictError(
                    f"dirty-worktree recovery receipt field {field!r} is invalid"
                )
        if schema_version in {2, 3}:
            for field in (
                "source_task_id",
                "source_node_id",
                "input_tree_sha",
                "dependency_input_ref",
            ):
                if not isinstance(recovery[field], str) or not recovery[field]:
                    raise StateConflictError(
                        f"dirty-worktree recovery receipt field {field!r} is invalid"
                    )
        expected_untracked: tuple[str, ...] = ()
        if schema_version == 3:
            raw_untracked = recovery.get("untracked_paths")
            if (
                not isinstance(raw_untracked, list)
                or not raw_untracked
                or not all(isinstance(path, str) and path for path in raw_untracked)
                or tuple(raw_untracked) != tuple(sorted(set(raw_untracked)))
                or not set(raw_untracked).issubset(source["changed_paths"])
            ):
                raise StateConflictError("dirty-worktree recovery receipt untracked_paths are invalid")
            expected_untracked = tuple(raw_untracked)
            allowed_scope = list(candidate["task"]["allowed_scope"])
            forbidden_scope = list(candidate["task"]["forbidden_scope"])
            write_scopes = list(candidate["node"]["write_scopes"])
            for relative_path in expected_untracked:
                if not scope_allows(relative_path, allowed_scope, forbidden_scope):
                    raise StateConflictError(
                        f"untracked recovery path is outside the task contract scope: {relative_path}"
                    )
                if not scope_allows(relative_path, write_scopes, []):
                    raise StateConflictError(
                        f"untracked recovery path is outside the blocked node write scope: {relative_path}"
                    )
        try:
            source_path = Path(str(source["worktree"])).expanduser().resolve(strict=True)
            receipt_source = Path(str(recovery["source_worktree"])).expanduser().resolve(strict=True)
            artifacts = ArtifactStore(self.path.parent / "artifacts")
            patch = artifacts.verify(str(recovery["patch_ref"])).read_bytes()
            source_result = json.loads(candidate["source_result_json"])
            actual_untracked = DirtyWorktreeRecovery.untracked_paths(source_path)
        except (OSError, ValueError, DirtyWorktreeRecoveryError, json.JSONDecodeError) as error:
            raise StateConflictError(
                f"dirty-worktree recovery source or artifact is unavailable: {error}"
            ) from error
        if not isinstance(source_result, dict):
            raise StateConflictError("dirty-worktree recovery source result is invalid")
        source_artifacts = source_result.get("artifacts")
        if not isinstance(source_artifacts, dict):
            raise StateConflictError("dirty-worktree recovery source artifacts are invalid")
        recorded_dependency_ref = source_artifacts.get("dependency-input")
        if receipt_source != source_path:
            raise StateConflictError(
                "dirty-worktree recovery receipt source does not match the active allocation"
            )
        patch_hash = sha256(patch).hexdigest()
        if patch_hash != recovery["patch_sha256"]:
            raise StateConflictError(
                "dirty-worktree recovery patch artifact does not match the captured receipt"
            )
        base_sha = str(source["base_sha"])
        if self._recovery_git_bytes(source_path, "rev-parse", "HEAD").decode().strip() != base_sha:
            raise StateConflictError("dirty-worktree recovery source no longer matches contract base")
        if self._recovery_git_bytes(source_path, "branch", "--show-current").decode().strip() != source["branch"]:
            raise StateConflictError("dirty-worktree recovery source no longer matches allocated branch")
        if actual_untracked != expected_untracked:
            raise StateConflictError("dirty-worktree recovery source untracked paths drifted")
        comparison_tree = base_sha
        if schema_version == 1:
            if recorded_dependency_ref is not None:
                raise StateConflictError(
                    "legacy dirty-worktree recovery cannot reproduce recorded dependency input"
                )
        else:
            if recovery["source_task_id"] != candidate["task"]["task_id"]:
                raise StateConflictError("dependency recovery receipt has another task")
            if recovery["source_node_id"] != candidate["node"]["node_id"]:
                raise StateConflictError("dependency recovery receipt has another node")
            if recorded_dependency_ref != recovery["dependency_input_ref"]:
                raise StateConflictError(
                    "dependency recovery receipt does not match the blocked worker input"
                )
            try:
                dependency_input = load_recorded_dependency_input(
                    artifacts,
                    str(recovery["dependency_input_ref"]),
                    task_id=str(candidate["task"]["task_id"]),
                    node_id=str(candidate["node"]["node_id"]),
                    base_sha=base_sha,
                )
            except Exception as error:
                raise StateConflictError(
                    f"dependency recovery input is unavailable or invalid: {error}"
                ) from error
            if dependency_input.input_tree_sha != recovery["input_tree_sha"]:
                raise StateConflictError(
                    "dependency recovery input tree does not match the recovery receipt"
                )
            comparison_tree = self._recovery_git_bytes(
                source_path,
                "rev-parse",
                "--verify",
                f"{dependency_input.input_tree_sha}^{{tree}}",
            ).decode().strip()
        tracked_paths = tuple(
            sorted(
                line
                for line in self._recovery_git_bytes(
                    source_path,
                    "diff",
                    "--name-only",
                    "--no-renames",
                    comparison_tree,
                    "--",
                )
                .decode(errors="surrogateescape")
                .splitlines()
                if line
            )
        )
        source_paths = tuple(sorted((*tracked_paths, *actual_untracked)))
        if source_paths != source["changed_paths"]:
            raise StateConflictError("dirty-worktree recovery source changed paths drifted")
        try:
            current_patch = DirtyWorktreeRecovery.captured_patch(
                source_path,
                comparison_tree,
                actual_untracked,
            )
        except DirtyWorktreeRecoveryError as error:
            raise StateConflictError(f"dirty-worktree recovery source patch is unavailable: {error}") from error
        if current_patch != patch:
            raise StateConflictError("dirty-worktree recovery source patch drifted")
        return dict(recovery)

    def resume_blocked_worktree(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
        reason: str,
        recovery: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        reason = reason.strip()
        if not reason:
            raise ValueError("dirty-worktree recovery reason must be non-empty")

        if dry_run:
            with self.connection() as connection:
                candidate = self._blocked_worktree_recovery_candidate(
                    connection,
                    task_id,
                    node_id,
                    expected_revision=expected_revision,
                    expected_attempt=expected_attempt,
                )
                capture = self._validate_blocked_worktree_recovery_capture(
                    candidate,
                    expected_attempt,
                    recovery,
                )
            return {
                "task_id": task_id,
                "node_id": node_id,
                "dry_run": True,
                "task": candidate["task"],
                "node": candidate["node"],
                "source": candidate["source"],
                "next_attempt": expected_attempt + 1,
                "recovery": capture,
            }

        timestamp = now_iso()
        with self.transaction() as connection:
            candidate = self._blocked_worktree_recovery_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )
            capture = self._validate_blocked_worktree_recovery_capture(
                candidate,
                expected_attempt,
                recovery,
            )
            revision = expected_revision + 1
            authorization = {
                "schema_version": 1,
                "kind": _DIRTY_WORKTREE_RECOVERY_KIND,
                "state": "authorized",
                "authorization_revision": revision,
                "source_allocation_id": candidate["source"]["allocation_id"],
                "source_result_json": candidate["source_result_json"],
                "recovery": capture,
            }
            changed = connection.execute(
                """
                UPDATE nodes
                SET state = 'pending', worker_id = NULL, worktree = NULL,
                    effective_executor = NULL, effective_model = NULL,
                    started_at = NULL, settled_at = NULL, result_json = NULL,
                    coordinator_epoch = 0, lease_epoch = 0, recovery_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'blocked' AND attempt = ?
                """,
                (
                    canonical_json(authorization),
                    timestamp,
                    task_id,
                    node_id,
                    expected_attempt,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("dirty-worktree recovery node compare-and-set failed")
            next_state = (
                "queued"
                if candidate["task"]["state"] == "blocked"
                else candidate["task"]["state"]
            )
            task_changed = connection.execute(
                """
                UPDATE tasks
                SET state = ?, state_revision = ?, updated_at = ?, blocker = NULL, verdict = NULL
                WHERE task_id = ? AND state_revision = ?
                """,
                (next_state, revision, timestamp, task_id, expected_revision),
            ).rowcount
            if task_changed != 1:
                raise StateConflictError(
                    "dirty-worktree recovery task revision compare-and-set failed"
                )
            self._event(
                connection,
                "node.blocked_worktree_recovery_authorized",
                task_id,
                node_id,
                {
                    "source_attempt": expected_attempt,
                    "next_attempt": expected_attempt + 1,
                    "reason": reason,
                    "recovery": capture,
                    "source_allocation_id": candidate["source"]["allocation_id"],
                    "task_revision": revision,
                },
                created_at=timestamp,
            )
            if next_state != candidate["task"]["state"]:
                self._event(
                    connection,
                    "task.state_changed",
                    task_id,
                    None,
                    {
                        "from": candidate["task"]["state"],
                        "to": next_state,
                        "revision": revision,
                        "blocker": None,
                    },
                    created_at=timestamp,
                )
            return {
                "task_id": task_id,
                "node_id": node_id,
                "dry_run": False,
                "revision": revision,
                "task": {"task_id": task_id, "state": next_state, "revision": revision},
                "node": {
                    "node_id": node_id,
                    "state": "pending",
                    "attempt": expected_attempt,
                },
                "source": candidate["source"],
                "next_attempt": expected_attempt + 1,
            }

    def _archify_reconciliation_candidate(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        task = connection.execute(
            """
            SELECT state, state_revision, contract_json, contract_hash
            FROM tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if task is None:
            raise KeyError(task_id)
        if int(task["state_revision"]) != expected_revision:
            raise StateConflictError(
                f"expected task revision {expected_revision}, found {task['state_revision']}"
            )
        if task["state"] not in {"blocked", "paused"}:
            raise StateConflictError(
                f"task {task_id} is {task['state']}, expected blocked or paused"
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
            states = ", ".join(f"{row['node_id']}:{row['state']}" for row in active)
            raise StateConflictError(
                f"cannot reconcile Archify metadata while task has running or indeterminate nodes: {states}"
            )
        try:
            contract = json.loads(task["contract_json"])
        except json.JSONDecodeError as error:
            raise StateConflictError("task frozen contract is invalid JSON") from error
        if not isinstance(contract, dict):
            raise StateConflictError("task frozen contract must be an object")

        rows = connection.execute(
            """
            SELECT * FROM nodes WHERE task_id = ?
            ORDER BY json_extract(spec_json, '$.ordinal'), node_id
            """,
            (task_id,),
        ).fetchall()
        nodes: list[dict[str, Any]] = []
        stored: dict[str, tuple[sqlite3.Row, dict[str, Any]]] = {}
        for row in rows:
            try:
                spec = json.loads(row["spec_json"])
                result = json.loads(row["result_json"]) if row["result_json"] is not None else None
            except json.JSONDecodeError as error:
                raise StateConflictError(f"node {row['node_id']} has invalid durable JSON") from error
            if not isinstance(spec, dict):
                raise StateConflictError(f"node {row['node_id']} specification must be an object")
            node_id = str(row["node_id"])
            nodes.append({
                **spec,
                "state": row["state"],
                "attempt": int(row["attempt"]),
                "result": result,
                "worktree": row["worktree"],
            })
            stored[node_id] = (row, spec)

        proposals = propose_archify_reconciliation(contract, nodes)
        changes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != {"node_id", "before", "after"}:
                raise StateConflictError("Archify reconciliation proposal is malformed")
            node_id = proposal["node_id"]
            before = proposal["before"]
            after = proposal["after"]
            if not isinstance(node_id, str) or node_id in seen or node_id not in stored:
                raise StateConflictError("Archify reconciliation proposal has an invalid node_id")
            seen.add(node_id)
            if (
                not isinstance(before, dict)
                or not isinstance(after, dict)
                or set(before) != {"archify", "prompt"}
                or set(after) != {"archify", "prompt"}
                or not isinstance(after["prompt"], str)
                or (after["archify"] is not None and not isinstance(after["archify"], dict))
            ):
                raise StateConflictError("Archify reconciliation proposal has invalid derived fields")
            row, spec = stored[node_id]
            current = {"archify": spec.get("archify"), "prompt": spec.get("prompt")}
            if before != current:
                raise StateConflictError(
                    f"Archify reconciliation proposal for {node_id} does not match durable metadata"
                )
            if (
                row["state"] != "pending"
                or int(row["attempt"]) != 0
                or row["result_json"] is not None
                or row["worktree"] is not None
            ):
                raise StateConflictError(
                    f"Archify reconciliation may update only pending attempt-zero node {node_id}"
                )
            if before == after:
                continue
            updated_spec = {**spec, "archify": after["archify"], "prompt": after["prompt"]}
            changes.append({
                "node_id": node_id,
                "before": before,
                "after": after,
                "before_spec_json": row["spec_json"],
                "after_spec_json": canonical_json(updated_spec),
            })

        return {
            "task": {
                "task_id": task_id,
                "state": str(task["state"]),
                "revision": int(task["state_revision"]),
                "contract_json": task["contract_json"],
                "contract_hash": task["contract_hash"],
            },
            "changes": changes,
        }

    def reconcile_archify_metadata(
        self,
        task_id: str,
        *,
        expected_revision: int,
        reason: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        reason = reason.strip()
        if not reason:
            raise ValueError("Archify reconciliation reason must be non-empty")

        if dry_run:
            with self.connection() as connection:
                candidate = WorkbenchStore._archify_reconciliation_candidate(
                    connection, task_id, expected_revision=expected_revision
                )
            return {
                "task_id": task_id,
                "dry_run": True,
                "status": "would-reconcile" if candidate["changes"] else "unchanged",
                "task": {
                    key: candidate["task"][key]
                    for key in ("task_id", "state", "revision")
                },
                "changes": [
                    {key: change[key] for key in ("node_id", "before", "after")}
                    for change in candidate["changes"]
                ],
            }

        timestamp = now_iso()
        with self.transaction() as connection:
            candidate = WorkbenchStore._archify_reconciliation_candidate(
                connection, task_id, expected_revision=expected_revision
            )
            if not candidate["changes"]:
                return {
                    "task_id": task_id,
                    "dry_run": False,
                    "status": "unchanged",
                    "revision": expected_revision,
                    "task": {
                        key: candidate["task"][key]
                        for key in ("task_id", "state", "revision")
                    },
                    "changes": [],
                }
            for change in candidate["changes"]:
                changed = connection.execute(
                    """
                    UPDATE nodes SET spec_json = ?, updated_at = ?
                    WHERE task_id = ? AND node_id = ? AND state = 'pending' AND attempt = 0
                      AND result_json IS NULL AND worktree IS NULL AND spec_json = ?
                    """,
                    (
                        change["after_spec_json"],
                        timestamp,
                        task_id,
                        change["node_id"],
                        change["before_spec_json"],
                    ),
                ).rowcount
                if changed != 1:
                    raise StateConflictError(
                        f"Archify reconciliation compare-and-set failed for {change['node_id']}"
                    )
            revision = expected_revision + 1
            changed = connection.execute(
                """
                UPDATE tasks SET state_revision = ?, updated_at = ?
                WHERE task_id = ? AND state_revision = ? AND contract_hash = ? AND contract_json = ?
                """,
                (
                    revision,
                    timestamp,
                    task_id,
                    expected_revision,
                    candidate["task"]["contract_hash"],
                    candidate["task"]["contract_json"],
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("Archify reconciliation task compare-and-set failed")
            public_changes = [
                {key: change[key] for key in ("node_id", "before", "after")}
                for change in candidate["changes"]
            ]
            event_cursor = self._event(
                connection,
                "task.archify_reconciled",
                task_id,
                None,
                {
                    "reason": reason,
                    "revision": revision,
                    "state": candidate["task"]["state"],
                    "changes": public_changes,
                },
                created_at=timestamp,
            )
            return {
                "task_id": task_id,
                "dry_run": False,
                "status": "reconciled",
                "revision": revision,
                "task": {
                    "task_id": task_id,
                    "state": candidate["task"]["state"],
                    "revision": revision,
                },
                "changes": public_changes,
                "event_cursor": event_cursor,
            }

    def set_task_priority(
        self,
        task_id: str,
        priority: int,
        *,
        expected_revision: int,
    ) -> int:
        if not -10 <= priority <= 10:
            raise ValueError("priority must be between -10 and 10")
        timestamp = now_iso()
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT state, state_revision, priority FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if int(task["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected task revision {expected_revision}, found {task['state_revision']}"
                )
            if task["state"] in {"accepted", "cancelled"}:
                raise StateConflictError(f"cannot reprioritize task in {task['state']}")
            if int(task["priority"]) == priority:
                return int(task["state_revision"])
            revision = int(task["state_revision"]) + 1
            connection.execute(
                """
                UPDATE tasks SET priority = ?, state_revision = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (priority, revision, timestamp, task_id),
            )
            self._event(
                connection,
                "task.priority_changed",
                task_id,
                None,
                {
                    "from": int(task["priority"]),
                    "to": priority,
                    "revision": revision,
                },
            )
            return revision

    def append_task_steering(
        self,
        task_id: str,
        instruction: str,
        *,
        expected_revision: int,
    ) -> int:
        with self.transaction() as connection:
            receipt = self._append_task_steering(
                connection,
                task_id,
                instruction,
                expected_revision=expected_revision,
            )
            return int(receipt["revision"])

    @staticmethod
    def _next_steering_sequence(
        connection: sqlite3.Connection,
        task_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
            "FROM task_steering WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        return int(row["next_sequence"])

    def append_active_session_steering(
        self,
        source_thread_id: str,
        instruction: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append a user message to its bound task without changing that task's objective or state."""

        if not source_thread_id or any(character.isspace() for character in source_thread_id):
            raise ValueError("source_thread_id must be non-empty and contain no whitespace")
        with self.transaction() as connection:
            binding = connection.execute(
                "SELECT active_task_id FROM session_bindings WHERE source_thread_id = ?",
                (source_thread_id,),
            ).fetchone()
            if binding is None:
                raise KeyError(source_thread_id)
            task_id = binding["active_task_id"]
            if task_id is None:
                raise StateConflictError("session has no active task to continue")
            task = connection.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateConflictError("session active task is missing")
            if task["state"] in {"accepted", "blocked", "cancelled"}:
                raise StateConflictError(
                    f"session active task is terminal: {task['state']}"
                )
            receipt = self._append_task_steering(
                connection,
                task_id,
                instruction,
                expected_revision=expected_revision,
            )
            self._event(
                connection,
                "session.active_task_message_appended",
                task_id,
                None,
                {
                    "source_thread_id": source_thread_id,
                    "steering_id": receipt["steering_id"],
                    "revision": receipt["revision"],
                    "state": receipt["state"],
                },
            )
            return {"task_id": task_id, **receipt}

    def _append_task_steering(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        instruction: str,
        *,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        instruction = instruction.strip()
        if not instruction or len(instruction) > 500:
            raise ValueError("instruction must contain 1 to 500 characters")
        timestamp = now_iso()
        task = connection.execute(
            "SELECT state, state_revision FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise KeyError(task_id)
        if expected_revision is not None and int(task["state_revision"]) != expected_revision:
            raise StateConflictError(
                f"expected task revision {expected_revision}, found {task['state_revision']}"
            )
        if task["state"] in {"accepted", "cancelled"}:
            raise StateConflictError(f"cannot steer task in {task['state']}")
        revision = int(task["state_revision"]) + 1
        sequence = self._next_steering_sequence(connection, task_id)
        steering_id = "steering-" + canonical_hash(
            {
                "task_id": task_id,
                "revision": revision,
                "instruction": instruction,
            }
        )[:24]
        connection.execute(
            """
            INSERT INTO task_steering(steering_id, task_id, instruction, created_at, sequence)
            VALUES(?, ?, ?, ?, ?)
            """,
            (steering_id, task_id, instruction, timestamp, sequence),
        )
        connection.execute(
            """
            UPDATE tasks SET state_revision = ?, updated_at = ? WHERE task_id = ?
            """,
            (revision, timestamp, task_id),
        )
        self._event(
            connection,
            "task.steering_added",
            task_id,
            None,
            {"steering_id": steering_id, "revision": revision},
        )
        return {
            "steering_id": steering_id,
            "revision": revision,
            "state": task["state"],
            "objective_preserved": True,
        }

    def resolve_indeterminate(
        self,
        task_id: str,
        node_id: str,
        resolution: str,
        *,
        expected_revision: int,
    ) -> int:
        with self.connection() as connection:
            approval = connection.execute(
                """
                SELECT approval_id FROM approvals
                WHERE task_id = ? AND kind = 'indeterminate_resolution'
                  AND decision IS NULL
                  AND json_extract(request_json, '$.node_id') = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (task_id, node_id),
            ).fetchone()
        if approval is not None:
            return self.decide_approval(
                str(approval["approval_id"]),
                resolution,
                expected_revision=expected_revision,
            )
        if resolution not in {"retry", "fail", "cancel"}:
            raise ValueError("resolution must be retry, fail, or cancel")
        timestamp = now_iso()
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT state_revision, state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            node = connection.execute(
                "SELECT state FROM nodes WHERE task_id = ? AND node_id = ?", (task_id, node_id)
            ).fetchone()
            if task is None or node is None:
                raise KeyError((task_id, node_id))
            if int(task["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected task revision {expected_revision}, found {task['state_revision']}"
                )
            if node["state"] != "indeterminate":
                raise StateConflictError(f"node {node_id} is {node['state']}, expected indeterminate")
            node_state = "pending" if resolution == "retry" else "failed" if resolution == "fail" else "cancelled"
            task_state = "queued" if resolution == "retry" else "needs_fix" if resolution == "fail" else "cancelled"
            connection.execute(
                """
                UPDATE nodes SET state = ?, worker_id = NULL,
                                 effective_executor = NULL, effective_model = NULL,
                                 started_at = NULL, settled_at = NULL, updated_at = ?
                WHERE task_id = ? AND node_id = ?
                """,
                (node_state, timestamp, task_id, node_id),
            )
            revision = int(task["state_revision"]) + 1
            connection.execute(
                """
                UPDATE tasks SET state = ?, state_revision = ?, updated_at = ?, blocker = NULL
                WHERE task_id = ?
                """,
                (task_state, revision, timestamp, task_id),
            )
            self._event(
                connection,
                "node.indeterminate_resolved",
                task_id,
                node_id,
                {"resolution": resolution, "task_revision": revision},
            )
            return revision

    def list_approvals(
        self,
        *,
        pending_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE a.decision IS NULL" if pending_only else ""
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, t.state_revision AS task_revision
                FROM approvals a JOIN tasks t USING(task_id)
                {where}
                ORDER BY CASE WHEN a.decision IS NULL THEN 0 ELSE 1 END,
                         a.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._approval_row(row) for row in rows]

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        expected_revision: int,
    ) -> int:
        if decision not in {"retry", "fail", "cancel"}:
            raise ValueError("decision must be retry, fail, or cancel")
        timestamp = now_iso()
        with self.transaction() as connection:
            approval = connection.execute(
                """
                SELECT a.*, t.state_revision AS task_revision
                FROM approvals a JOIN tasks t USING(task_id)
                WHERE a.approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
            if approval is None:
                raise KeyError(approval_id)
            if approval["kind"] != "indeterminate_resolution":
                raise StateConflictError(f"unsupported approval kind {approval['kind']}")
            request = json.loads(approval["request_json"])
            if approval["decision"] is not None:
                if approval["decision"] == decision:
                    return int(request["decision_revision"])
                raise StateConflictError(
                    f"approval {approval_id} was already decided as {approval['decision']}"
                )
            if int(approval["task_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected task revision {expected_revision}, found {approval['task_revision']}"
                )
            task_id = str(approval["task_id"])
            node_id = str(request["node_id"])
            node = connection.execute(
                "SELECT state FROM nodes WHERE task_id = ? AND node_id = ?",
                (task_id, node_id),
            ).fetchone()
            if node is None:
                raise KeyError((task_id, node_id))
            if node["state"] != "indeterminate":
                raise StateConflictError(
                    f"node {node_id} is {node['state']}, expected indeterminate"
                )
            node_state = (
                "pending" if decision == "retry" else "failed" if decision == "fail" else "cancelled"
            )
            connection.execute(
                """
                UPDATE nodes SET state = ?, worker_id = NULL,
                                 effective_executor = NULL, effective_model = NULL,
                                 started_at = NULL, settled_at = NULL, updated_at = ?
                WHERE task_id = ? AND node_id = ?
                """,
                (node_state, timestamp, task_id, node_id),
            )
            remaining_indeterminate = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM nodes WHERE task_id = ? AND state = 'indeterminate'",
                    (task_id,),
                ).fetchone()["count"]
            )
            if decision == "cancel":
                task_state = "cancelled"
                blocker = None
            elif remaining_indeterminate:
                task_state = "needs_approval"
                blocker = f"{remaining_indeterminate} indeterminate node(s) still require approval"
            else:
                task_state = "queued" if decision == "retry" else "needs_fix"
                blocker = None
            revision = int(approval["task_revision"]) + 1
            connection.execute(
                """
                UPDATE tasks SET state = ?, state_revision = ?, updated_at = ?, blocker = ?
                WHERE task_id = ?
                """,
                (task_state, revision, timestamp, blocker, task_id),
            )
            request["decision_revision"] = revision
            connection.execute(
                """
                UPDATE approvals SET decision = ?, decided_at = ?, request_json = ?
                WHERE approval_id = ?
                """,
                (decision, timestamp, canonical_json(request), approval_id),
            )
            self._event(
                connection,
                "approval.decided",
                task_id,
                node_id,
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "task_revision": revision,
                },
            )
            self._event(
                connection,
                "node.indeterminate_resolved",
                task_id,
                node_id,
                {
                    "approval_id": approval_id,
                    "resolution": decision,
                    "task_revision": revision,
                },
            )
            return revision

    @staticmethod
    def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
        request = json.loads(row["request_json"])
        return {
            "approval_id": row["approval_id"],
            "task_id": row["task_id"],
            "kind": row["kind"],
            "request": request,
            "decision": row["decision"],
            "decided_at": row["decided_at"],
            "created_at": row["created_at"],
            "task_revision": int(row["task_revision"]),
            "decision_revision": request.get("decision_revision"),
        }

    @staticmethod
    def _create_indeterminate_approval(
        connection: sqlite3.Connection,
        task_id: str,
        node_id: str,
        attempt: int,
        task_revision: int,
        reason: str,
    ) -> str:
        approval_id = "approval-" + canonical_hash(
            {
                "kind": "indeterminate_resolution",
                "task_id": task_id,
                "node_id": node_id,
                "attempt": attempt,
            }
        )[:24]
        request = {
            "node_id": node_id,
            "attempt": attempt,
            "task_revision_at_request": task_revision,
            "reason": reason,
            "allowed_decisions": ["retry", "fail", "cancel"],
        }
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO approvals(
                approval_id, task_id, kind, request_json, created_at
            ) VALUES(?, ?, 'indeterminate_resolution', ?, ?)
            """,
            (approval_id, task_id, canonical_json(request), now_iso()),
        ).rowcount
        if inserted:
            WorkbenchStore._event(
                connection,
                "approval.requested",
                task_id,
                node_id,
                {
                    "approval_id": approval_id,
                    "kind": "indeterminate_resolution",
                    "attempt": attempt,
                    "task_revision": task_revision,
                },
            )
        return approval_id

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, contract_hash, state, state_revision, priority, created_at, updated_at,
                       blocker, verdict, contract_json
                FROM tasks ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._task_row(connection, row) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return self._task_row(connection, row)

    @staticmethod
    def _task_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        node_rows = connection.execute(
            "SELECT * FROM nodes WHERE task_id = ? ORDER BY json_extract(spec_json, '$.ordinal'), node_id",
            (row["task_id"],),
        ).fetchall()
        steering_rows = connection.execute(
            """
            SELECT steering_id, instruction, created_at, sequence FROM task_steering
            WHERE task_id = ? ORDER BY sequence
            """,
            (row["task_id"],),
        ).fetchall()
        return {
            "task_id": row["task_id"],
            "state": row["state"],
            "state_revision": row["state_revision"],
            "priority": row["priority"],
            "contract_hash": row["contract_hash"],
            "contract": json.loads(row["contract_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "blocker": row["blocker"],
            "verdict": row["verdict"],
            "steering": [dict(item) for item in steering_rows],
            "nodes": [
                {
                    **json.loads(node["spec_json"]),
                    "state": node["state"],
                    "attempt": node["attempt"],
                    "worker_id": node["worker_id"],
                    "worktree": node["worktree"],
                    "effective_executor": node["effective_executor"],
                    "effective_model": node["effective_model"],
                    "coordinator_epoch": node["coordinator_epoch"],
                    "lease_epoch": node["lease_epoch"],
                    "started_at": node["started_at"],
                    "settled_at": node["settled_at"],
                    "updated_at": node["updated_at"],
                    "result": json.loads(node["result_json"]) if node["result_json"] else None,
                }
                for node in node_rows
            ],
        }

    def read_events(
        self, after: int = 0, limit: int = 500, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if task_id:
                rows = connection.execute(
                    """
                    SELECT * FROM events WHERE cursor > ? AND task_id = ?
                    ORDER BY cursor LIMIT ?
                    """,
                    (after, task_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?",
                    (after, limit),
                ).fetchall()
            return [
                {
                    "cursor": row["cursor"],
                    "event_type": row["event_type"],
                    "task_id": row["task_id"],
                    "node_id": row["node_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def _events_for_task(self, task_id: str, *, event_type: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if event_type is None:
                rows = connection.execute(
                    "SELECT * FROM events WHERE task_id = ? ORDER BY cursor", (task_id,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM events WHERE task_id = ? AND event_type = ? ORDER BY cursor",
                    (task_id, event_type),
                ).fetchall()
        return [
            {
                "cursor": row["cursor"], "event_type": row["event_type"], "task_id": row["task_id"],
                "node_id": row["node_id"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"],
            }
            for row in rows
        ]

    def remediate_legacy_evidence(self, command_id: str, manifest_ref: str) -> dict[str, Any]:
        """Append one validated legacy-Evidence overlay without changing source rows."""
        if not command_id.strip():
            raise ValueError("legacy remediation command_id is required")
        manifest, manifest_hash = load_manifest(self.artifacts, manifest_ref)
        source = manifest.get("source")
        task_id = source.get("task_id") if isinstance(source, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("legacy remediation manifest source task_id is required")
        try:
            task = self.get_task(task_id)
        except KeyError as error:
            raise ValueError("legacy remediation source task does not exist") from error
        review_task, review_events = self._legacy_review_source(manifest, task_id)
        validated = validate_manifest(
            manifest, task, self._events_for_task(task_id), self.artifacts, review_task, review_events
        )
        request_hash = canonical_hash(
            {
                "kind": "legacy-evidence-remediation-v1",
                "manifest_hash": manifest_hash,
            }
        )
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT request_hash, task_id FROM command_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise CommandConflictError(
                        f"command {command_id!r} was already used with a different request"
                    )
                row = connection.execute(
                    """
                    SELECT cursor, payload_json FROM events
                    WHERE event_type = 'acceptance.evidence_remediated'
                      AND task_id = ?
                      AND json_extract(payload_json, '$.command_id') = ?
                      AND json_extract(payload_json, '$.request_hash') = ?
                    ORDER BY cursor DESC LIMIT 1
                    """,
                    (task_id, command_id, request_hash),
                ).fetchone()
                if row is None:
                    raise StateConflictError("legacy remediation receipt has no matching event")
                stored = json.loads(row["payload_json"])
                return {
                    "task_id": task_id,
                    "event_cursor": int(row["cursor"]),
                    "manifest_ref": stored["manifest_ref"],
                    "manifest_hash": stored["manifest_hash"],
                    "idempotent": True,
                }
            payload = {
                "kind": "legacy-evidence-remediation-v1",
                "command_id": command_id,
                "request_hash": request_hash,
                "manifest_ref": manifest_ref,
                "manifest_hash": manifest_hash,
                "task_id": validated["task_id"],
                "contract_hash": validated["contract_hash"],
                "base_sha": validated["base_sha"],
                "source_event_first": validated["event_first"],
                "source_event_last": validated["event_last"],
            }
            cursor = self._event(connection, "acceptance.evidence_remediated", task_id, None, payload)
            connection.execute(
                """
                INSERT INTO command_receipts(command_id, request_hash, task_id, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (command_id, request_hash, task_id, now_iso()),
            )
        return {
            "task_id": task_id,
            "event_cursor": cursor,
            "manifest_ref": manifest_ref,
            "manifest_hash": manifest_hash,
            "idempotent": False,
        }

    def legacy_evidence_remediations(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return only overlays revalidated from ArtifactStore and receipt ledger."""
        if task_id is None:
            with self.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM events WHERE event_type = 'acceptance.evidence_remediated' ORDER BY cursor"
                ).fetchall()
            candidates = [
                {
                    "cursor": row["cursor"], "event_type": row["event_type"], "task_id": row["task_id"],
                    "node_id": row["node_id"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"],
                }
                for row in rows
            ]
        else:
            candidates = self._events_for_task(task_id, event_type="acceptance.evidence_remediated")
        remediations: list[dict[str, Any]] = []
        for event in candidates:
            payload = event["payload"]
            if payload.get("kind") != "legacy-evidence-remediation-v1":
                continue
            command_id = payload.get("command_id")
            request_hash = payload.get("request_hash")
            manifest_ref = payload.get("manifest_ref")
            manifest_hash = payload.get("manifest_hash")
            source_task_id = payload.get("task_id")
            if not all(isinstance(value, str) and value for value in (command_id, request_hash, manifest_ref, manifest_hash, source_task_id)):
                continue
            try:
                with self.connection() as connection:
                    receipt = connection.execute(
                        "SELECT request_hash, task_id FROM command_receipts WHERE command_id = ?", (command_id,)
                    ).fetchone()
                if receipt is None or receipt["request_hash"] != request_hash or receipt["task_id"] != source_task_id:
                    continue
                manifest, actual_manifest_hash = load_manifest(self.artifacts, manifest_ref)
                if actual_manifest_hash != manifest_hash:
                    continue
                expected_hash = canonical_hash(
                    {
                        "kind": "legacy-evidence-remediation-v1",
                        "manifest_hash": manifest_hash,
                    }
                )
                if expected_hash != request_hash:
                    continue
                task = self.get_task(source_task_id)
                review_task, review_events = self._legacy_review_source(manifest, source_task_id)
                validated = validate_manifest(
                    manifest,
                    task,
                    self._events_for_task(source_task_id),
                    self.artifacts,
                    review_task,
                    review_events,
                )
                if any(
                    payload.get(key) != validated[source_key]
                    for key, source_key in (
                        ("contract_hash", "contract_hash"),
                        ("base_sha", "base_sha"),
                        ("source_event_first", "event_first"),
                        ("source_event_last", "event_last"),
                    )
                ):
                    continue
            except (KeyError, ValueError, OSError, json.JSONDecodeError):
                continue
            remediations.append({
                "event_cursor": event["cursor"], "manifest_ref": manifest_ref,
                "manifest_hash": manifest_hash, "command_id": command_id, **validated,
            })
        return remediations

    def _legacy_review_source(
        self,
        manifest: dict[str, Any],
        source_task_id: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        overlay = manifest.get("overlay")
        supplemental = overlay.get("supplemental_sol_review") if isinstance(overlay, dict) else None
        if supplemental is None:
            return None, None
        review_source = supplemental.get("review_source") if isinstance(supplemental, dict) else None
        review_task_id = review_source.get("task_id") if isinstance(review_source, dict) else None
        if not isinstance(review_task_id, str) or not review_task_id or review_task_id == source_task_id:
            raise ValueError("legacy remediation review_source task_id is required")
        try:
            return self.get_task(review_task_id), self._events_for_task(review_task_id)
        except KeyError as error:
            raise ValueError("legacy remediation review source task does not exist") from error

    def list_alerts(self, limit: int = 30) -> list[dict[str, Any]]:
        important = {
            "approval.requested",
            "node.blocked",
            "node.indeterminate",
            "node.routed",
            "coordinator.started",
            "coordinator.stopped",
            "coordinator.failed",
            "quota.refresh_failed",
            "quota.refresh_unavailable",
            "worktree.recovery_failed",
            "worktree.purge_failed",
        }
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY cursor DESC LIMIT 1000"
            ).fetchall()
        alerts: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            is_task_alert = (
                row["event_type"] == "task.state_changed"
                and payload.get("to")
                in {"accepted", "blocked", "needs_fix", "needs_approval", "cancelled"}
            )
            if row["event_type"] not in important and not is_task_alert:
                continue
            alerts.append(
                {
                    "cursor": row["cursor"],
                    "event_type": row["event_type"],
                    "task_id": row["task_id"],
                    "node_id": row["node_id"],
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
            if len(alerts) == limit:
                break
        return list(reversed(alerts))

    def record_system_event(self, event_type: str, payload: dict[str, Any]) -> int:
        with self.transaction() as connection:
            return self._event(connection, event_type, None, None, payload)

    def record_session_context(
        self,
        *,
        command_id: str,
        request_hash: str,
        source_thread_id: str,
        context_ref: str,
        archive_ref: str,
        manifest: dict[str, Any],
        repository: str,
        base_sha: str,
        allowed_scopes: tuple[str, ...],
        context_excerpt: str,
    ) -> dict[str, Any]:
        if not command_id or not source_thread_id:
            raise ValueError("command_id and source_thread_id are required")
        timestamp = now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM context_import_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise CommandConflictError(
                        f"command {command_id!r} was already used with a different context bundle"
                    )
                return self._context_receipt(existing)
            connection.execute(
                """
                INSERT INTO context_import_receipts(
                    command_id, request_hash, source_thread_id, context_ref,
                    archive_ref, manifest_json, repository, base_sha,
                    allowed_scopes_json, context_excerpt, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    request_hash,
                    source_thread_id,
                    context_ref,
                    archive_ref,
                    canonical_json(manifest),
                    repository,
                    base_sha,
                    canonical_json(allowed_scopes),
                    context_excerpt,
                    timestamp,
                ),
            )
            previous_binding = connection.execute(
                """
                SELECT active_task_id, context_ref FROM session_bindings
                WHERE source_thread_id = ?
                """,
                (source_thread_id,),
            ).fetchone()
            previous_task_id = (
                previous_binding["active_task_id"] if previous_binding is not None else None
            )
            context_changed = (
                previous_binding is not None
                and previous_binding["context_ref"] != context_ref
            )
            connection.execute(
                """
                INSERT INTO session_bindings(
                    source_thread_id, context_ref, active_task_id, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(source_thread_id) DO UPDATE SET
                    context_ref = excluded.context_ref,
                    active_task_id = excluded.active_task_id,
                    updated_at = excluded.updated_at
                """,
                (
                    source_thread_id,
                    context_ref,
                    None if context_changed else previous_task_id,
                    timestamp,
                ),
            )
            if context_changed and previous_task_id is not None:
                self._event(
                    connection,
                    "context.active_task_invalidated",
                    previous_task_id,
                    None,
                    {
                        "source_thread_id": source_thread_id,
                        "previous_context_ref": previous_binding["context_ref"],
                        "new_context_ref": context_ref,
                        "reason": "context_bundle_replaced",
                    },
                )
            self._event(
                connection,
                "context.imported",
                None,
                None,
                {
                    "source_thread_id": source_thread_id,
                    "context_ref": context_ref,
                    "archive_ref": archive_ref,
                    "repository": repository,
                    "base_sha": base_sha,
                },
            )
            row = connection.execute(
                "SELECT * FROM context_import_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            assert row is not None
            return self._context_receipt(row)

    @staticmethod
    def _context_receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "command_id": row["command_id"],
            "source_thread_id": row["source_thread_id"],
            "context_ref": row["context_ref"],
            "archive_ref": row["archive_ref"],
            "manifest": json.loads(row["manifest_json"]),
            "repository": row["repository"],
            "base_sha": row["base_sha"],
            "allowed_scopes": json.loads(row["allowed_scopes_json"]),
            "context_excerpt": row["context_excerpt"],
            "created_at": row["created_at"],
        }

    def get_session_binding(self, source_thread_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            binding = connection.execute(
                "SELECT * FROM session_bindings WHERE source_thread_id = ?",
                (source_thread_id,),
            ).fetchone()
            if binding is None:
                raise KeyError(source_thread_id)
            receipt = connection.execute(
                """
                SELECT * FROM context_import_receipts
                WHERE source_thread_id = ? AND context_ref = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_thread_id, binding["context_ref"]),
            ).fetchone()
            if receipt is None:
                raise StateConflictError("session binding points to a missing context receipt")
            return {
                **self._context_receipt(receipt),
                "active_task_id": binding["active_task_id"],
                "updated_at": binding["updated_at"],
            }

    def bind_task_to_session(self, source_thread_id: str, task_id: str) -> None:
        timestamp = now_iso()
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE session_bindings
                SET active_task_id = ?, updated_at = ?
                WHERE source_thread_id = ?
                """,
                (task_id, timestamp, source_thread_id),
            ).rowcount
            if changed != 1:
                raise KeyError(source_thread_id)
            self._event(
                connection,
                "context.task_bound",
                task_id,
                None,
                {"source_thread_id": source_thread_id},
            )

    @staticmethod
    def _parse_observed_at(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from error
        if parsed.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return parsed.astimezone(UTC)

    def record_client_heartbeat(
        self,
        client_id: str,
        client_kind: str,
        *,
        route: str | None = None,
        reason: str | None = None,
        observed_at: str | None = None,
        presence_ttl_seconds: int = 600,
    ) -> int:
        if not client_id or len(client_id) > 128 or any(character.isspace() for character in client_id):
            raise ValueError("client_id must be non-empty, contain no whitespace, and be at most 128 characters")
        if client_kind not in {"macbook", "phone"}:
            raise ValueError("client_kind must be macbook or phone")
        if presence_ttl_seconds < 60 or presence_ttl_seconds > 3600:
            raise ValueError("presence_ttl_seconds must be between 60 and 3600")
        location_values = (route, reason, observed_at)
        if any(value is not None for value in location_values) and not all(
            isinstance(value, str) and value for value in location_values
        ):
            raise ValueError("route, reason, and observed_at must be supplied together")
        timestamp = now_iso()
        payload: dict[str, Any] = {"client_id": client_id, "client_kind": client_kind}
        with self.transaction() as connection:
            if route is not None:
                assert reason is not None and observed_at is not None
                if route not in {"lan", "tailscale"}:
                    raise ValueError("route must be lan or tailscale")
                observed = self._parse_observed_at(observed_at)
                now = datetime.now(UTC)
                age = (now - observed).total_seconds()
                if age < -30 or age > 120:
                    raise ValueError("location observation is outside the trusted freshness window")
                payload.update(
                    {"route": route, "reason": reason, "observed_at": observed.isoformat()}
                )
                if (
                    client_kind == "macbook"
                    and route == "lan"
                    and reason == "home_network_lan_probe_ok"
                ):
                    expires_at = (now + timedelta(seconds=presence_ttl_seconds)).isoformat()
                    connection.execute(
                        """
                        INSERT INTO home_presence_leases(
                            client_id, route, reason, observed_at, expires_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        ON CONFLICT(client_id) DO UPDATE SET
                            route = excluded.route,
                            reason = excluded.reason,
                            observed_at = excluded.observed_at,
                            expires_at = excluded.expires_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            client_id,
                            route,
                            reason,
                            observed.isoformat(),
                            expires_at,
                            timestamp,
                        ),
                    )
                    payload["home_presence_expires_at"] = expires_at
                else:
                    connection.execute(
                        "DELETE FROM home_presence_leases WHERE client_id = ?",
                        (client_id,),
                    )
            return self._event(connection, "client.heartbeat", None, None, payload)

    def active_home_presence(self, *, at: datetime | None = None) -> dict[str, Any] | None:
        now = (at or datetime.now(UTC)).astimezone(UTC)
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM home_presence_leases WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            row = connection.execute(
                """
                SELECT * FROM home_presence_leases
                WHERE expires_at > ? ORDER BY expires_at DESC LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def _allocation_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        if "contract_json" in value:
            value["contract"] = json.loads(value.pop("contract_json"))
        if "spec_json" in value:
            value["spec"] = json.loads(value.pop("spec_json"))
        if "node_result_json" in value:
            raw_result = value.pop("node_result_json")
            value["node_result"] = json.loads(raw_result) if raw_result else None
        return value

    def list_worktree_allocations(self, *, states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT a.*, t.state AS task_state, t.contract_json, n.spec_json,
                   n.attempt AS current_attempt, n.worktree AS node_worktree
            FROM worktree_allocations a
            JOIN tasks t USING(task_id)
            JOIN nodes n ON n.task_id = a.task_id AND n.node_id = a.node_id
        """
        parameters: tuple[Any, ...] = ()
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" WHERE a.state IN ({placeholders})"
            parameters = states
        query += " ORDER BY a.created_at, a.allocation_id"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._allocation_row(row) for row in rows]

    def get_worktree_allocation(self, allocation_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT a.*, t.state AS task_state, t.contract_json, n.spec_json,
                       n.attempt AS current_attempt, n.worktree AS node_worktree
                FROM worktree_allocations a
                JOIN tasks t USING(task_id)
                JOIN nodes n ON n.task_id = a.task_id AND n.node_id = a.node_id
                WHERE allocation_id = ?
                """,
                (allocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(allocation_id)
            return self._allocation_row(row)

    def reclaimable_worktree_allocations(self) -> list[dict[str, Any]]:
        candidates = self.list_worktree_allocations(
            states=("active", "quarantine_pending", "quarantined", "archive_failed", "archived_verified", "purge_failed")
        )
        with self.connection() as connection:
            delivered = {
                str(row["task_id"])
                for row in connection.execute(
                    "SELECT DISTINCT task_id FROM delivery_receipts WHERE state IN ('merged', 'released')"
                ).fetchall()
            }
        result: list[dict[str, Any]] = []
        for allocation in candidates:
            if allocation["task_state"] not in {"accepted", "cancelled"}:
                continue
            verifier = bool(allocation["spec"].get("verifier"))
            external = bool(allocation["contract"].get("external_write_permission"))
            allocation["purge_allowed"] = (
                allocation["task_state"] == "cancelled"
                or not verifier
                or not external
                or allocation["task_id"] in delivered
            )
            if allocation["purge_allowed"]:
                result.append(allocation)
        return result

    def begin_worktree_quarantine(self, allocation_id: str, destination: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(allocation_id)
            if row["state"] not in {"active", "quarantine_pending"}:
                return dict(row)
            connection.execute(
                "UPDATE worktree_allocations SET state = 'quarantine_pending', updated_at = ? WHERE allocation_id = ?",
                (timestamp, allocation_id),
            )
            self._event(
                connection,
                "worktree.quarantine_pending",
                row["task_id"],
                row["node_id"],
                {"allocation_id": allocation_id, "from": row["current_path"], "to": destination},
            )
        return self.get_worktree_allocation(allocation_id)

    def finish_worktree_quarantine(self, allocation_id: str, destination: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(allocation_id)
            previous = str(row["current_path"])
            connection.execute(
                """
                UPDATE worktree_allocations
                SET current_path = ?, state = 'quarantined', updated_at = ?
                WHERE allocation_id = ?
                """,
                (destination, timestamp, allocation_id),
            )
            connection.execute(
                """
                UPDATE nodes SET worktree = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND attempt = ? AND worktree = ?
                """,
                (
                    destination,
                    timestamp,
                    row["task_id"],
                    row["node_id"],
                    row["attempt"],
                    previous,
                ),
            )
            self._event(
                connection,
                "worktree.quarantined",
                row["task_id"],
                row["node_id"],
                {"allocation_id": allocation_id, "path": destination},
            )
        return self.get_worktree_allocation(allocation_id)

    def begin_worktree_archive(
        self,
        archive_id: str,
        allocation_id: str | None,
        *,
        source_host: str,
        source_path: str,
        transport: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM worktree_archives WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO worktree_archives(
                        archive_id, allocation_id, source_host, source_path,
                        transport, state, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        archive_id,
                        allocation_id,
                        source_host,
                        source_path,
                        transport,
                        timestamp,
                        timestamp,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM worktree_archives WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

    def finish_worktree_archive(
        self,
        archive_id: str,
        *,
        archive_path: str,
        archive_sha256: str,
        size_bytes: int,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_archives WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
            if row is None:
                raise KeyError(archive_id)
            connection.execute(
                """
                UPDATE worktree_archives
                SET archive_path = ?, archive_sha256 = ?, size_bytes = ?,
                    state = 'verified', manifest_json = ?, error = NULL,
                    verified_at = ?, updated_at = ?
                WHERE archive_id = ?
                """,
                (
                    archive_path,
                    archive_sha256,
                    size_bytes,
                    canonical_json(manifest),
                    timestamp,
                    timestamp,
                    archive_id,
                ),
            )
            if row["allocation_id"]:
                connection.execute(
                    """
                    UPDATE worktree_allocations
                    SET state = 'archived_verified', updated_at = ?
                    WHERE allocation_id = ?
                    """,
                    (timestamp, row["allocation_id"]),
                )
            self._event(
                connection,
                "worktree.archive_verified",
                manifest.get("task_id"),
                manifest.get("node_id"),
                {
                    "archive_id": archive_id,
                    "allocation_id": row["allocation_id"],
                    "path": archive_path,
                    "sha256": archive_sha256,
                    "size_bytes": size_bytes,
                    "transport": row["transport"],
                },
            )
        return self.get_worktree_archive(archive_id)

    def fail_worktree_archive(self, archive_id: str, error: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_archives WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
            if row is None:
                raise KeyError(archive_id)
            connection.execute(
                "UPDATE worktree_archives SET state = 'failed', error = ?, updated_at = ? WHERE archive_id = ?",
                (error[:2000], timestamp, archive_id),
            )
            if row["allocation_id"]:
                connection.execute(
                    "UPDATE worktree_allocations SET state = 'archive_failed', updated_at = ? WHERE allocation_id = ?",
                    (timestamp, row["allocation_id"]),
                )
            return dict(
                connection.execute(
                    "SELECT * FROM worktree_archives WHERE archive_id = ?",
                    (archive_id,),
                ).fetchone()
            )

    def get_worktree_archive(self, archive_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_archives WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
            if row is None:
                raise KeyError(archive_id)
            result = dict(row)
            result["manifest"] = json.loads(result.pop("manifest_json")) if result.get("manifest_json") else None
            return result

    def list_worktree_archives(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT archive_id FROM worktree_archives ORDER BY created_at DESC"
            ).fetchall()
        return [self.get_worktree_archive(str(row["archive_id"])) for row in rows]

    def mark_worktree_purged(self, allocation_id: str, archive_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            allocation = connection.execute(
                "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            archive = connection.execute(
                "SELECT * FROM worktree_archives WHERE archive_id = ? AND allocation_id = ?",
                (archive_id, allocation_id),
            ).fetchone()
            if allocation is None or archive is None:
                raise KeyError((allocation_id, archive_id))
            if archive["state"] != "verified":
                raise StateConflictError("local worktree purge requires a verified archive receipt")
            connection.execute(
                "UPDATE worktree_allocations SET state = 'purged', updated_at = ? WHERE allocation_id = ?",
                (timestamp, allocation_id),
            )
            connection.execute(
                "UPDATE worktree_archives SET purged_at = ?, updated_at = ? WHERE archive_id = ?",
                (timestamp, timestamp, archive_id),
            )
            self._event(
                connection,
                "worktree.purged",
                allocation["task_id"],
                allocation["node_id"],
                {"allocation_id": allocation_id, "archive_id": archive_id},
            )
        return self.get_worktree_allocation(allocation_id)

    def mark_worktree_purge_failed(self, allocation_id: str, error: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(allocation_id)
            connection.execute(
                "UPDATE worktree_allocations SET state = 'purge_failed', updated_at = ? WHERE allocation_id = ?",
                (timestamp, allocation_id),
            )
            self._event(
                connection,
                "worktree.purge_failed",
                row["task_id"],
                row["node_id"],
                {"allocation_id": allocation_id, "error": error[:2000]},
            )
        return self.get_worktree_allocation(allocation_id)

    def record_client_observation(
        self,
        client_id: str,
        device_class: str,
        snapshot_cursor: int,
        current_cursor: int,
        user_agent: str,
    ) -> int:
        if not client_id or len(client_id) > 128 or any(character.isspace() for character in client_id):
            raise ValueError("client_id must be non-empty, contain no whitespace, and be at most 128 characters")
        if device_class not in {"phone", "desktop"}:
            raise ValueError("device_class must be phone or desktop")
        if snapshot_cursor < 0 or snapshot_cursor > current_cursor:
            raise ValueError("snapshot_cursor is outside the current event ledger")
        if current_cursor - snapshot_cursor > 100:
            raise ValueError("rendered snapshot is too stale to attest")
        return self.record_system_event(
            "client.observed",
            {
                "client_id": client_id,
                "device_class": device_class,
                "snapshot_cursor": snapshot_cursor,
                "current_cursor": current_cursor,
                "authenticated": True,
                "rendered": True,
                "user_agent": user_agent[:512],
            },
        )

    def _resolve_export_receipt(
        self,
        export_receipt: str | Path | dict[str, Any] | None,
        export_receipt_ref: str | None,
        *,
        artifact_ref: str,
        quota_window_id: str,
        source_session_id: str,
    ) -> str | None:
        if export_receipt is not None and export_receipt_ref is not None:
            raise ValueError("provide an export receipt or export_receipt_ref, not both")
        value: str | Path | dict[str, Any] | None = (
            export_receipt_ref if export_receipt_ref is not None else export_receipt
        )
        if value is None:
            return None

        if isinstance(value, dict):
            receipt_ref = self.artifacts.put_text(canonical_json(value), "json")
        elif isinstance(value, Path):
            if not value.is_file():
                raise ValueError("A12 export receipt file does not exist")
            try:
                receipt_ref = self.artifacts.put_bytes(value.read_bytes(), "json")
            except OSError as error:
                raise ValueError("A12 export receipt file cannot be read") from error
        elif isinstance(value, str) and value.startswith("sha256:"):
            receipt_ref = value
        elif isinstance(value, str):
            candidate = Path(value).expanduser()
            if candidate.is_file():
                try:
                    receipt_ref = self.artifacts.put_bytes(candidate.read_bytes(), "json")
                except OSError as error:
                    raise ValueError("A12 export receipt file cannot be read") from error
            else:
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError) as error:
                    raise ValueError("A12 export receipt must be JSON or a file path") from error
                if not isinstance(parsed, dict):
                    raise ValueError("A12 export receipt must be a JSON object")
                receipt_ref = self.artifacts.put_text(canonical_json(parsed), "json")
        else:
            raise ValueError("A12 export receipt must be JSON, a file path, or an artifact ref")

        try:
            receipt_path = self.artifacts.verify(receipt_ref)
            receipt = json.loads(receipt_path.read_text())
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("A12 export receipt is not a valid content-addressed JSON artifact") from error
        if not isinstance(receipt, dict):
            raise ValueError("A12 export receipt must be a JSON object")
        if receipt.get("provider") != "claude-web":
            raise ValueError("A12 export receipt must identify claude-web")
        if str(receipt.get("status", "")).lower() not in {
            "completed", "exported", "succeeded", "success", "ok"
        }:
            raise ValueError("A12 export receipt is not completed")
        if receipt.get("source_session_id", receipt.get("session_id")) != source_session_id:
            raise ValueError("A12 export receipt session does not match the attestation")
        if receipt.get("quota_window_id", receipt.get("window_id")) != quota_window_id:
            raise ValueError("A12 export receipt quota window does not match the attestation")

        receipt_artifact_ref = receipt.get("artifact_ref", receipt.get("output_artifact_ref"))
        receipt_digest = receipt.get("artifact_sha256", receipt.get("artifact_hash"))
        if receipt_artifact_ref is None and receipt_digest is None:
            raise ValueError("A12 export receipt must identify the exported artifact")
        if receipt_artifact_ref is not None and receipt_artifact_ref != artifact_ref:
            raise ValueError("A12 export receipt artifact does not match the attestation")
        if receipt_digest is not None:
            normalized_digest = str(receipt_digest)
            if normalized_digest.startswith("sha256:"):
                normalized_digest = normalized_digest.split(":", 1)[1]
            expected_digest = artifact_ref.split(":", 2)[1]
            if normalized_digest != expected_digest:
                raise ValueError("A12 export receipt artifact hash does not match the attestation")
        return receipt_ref

    def record_acceptance_attestation(
        self,
        check_id: str,
        artifact_ref: str,
        artifact_name: str,
        artifact_size: int,
        quota_window_id: str,
        source_session_id: str,
        note: str,
        export_receipt: str | Path | dict[str, Any] | None = None,
        *,
        export_receipt_ref: str | None = None,
    ) -> int:
        if check_id != "A12":
            raise ValueError("only A12 accepts a local administrator attestation")
        if not artifact_ref.startswith("sha256:") or artifact_size <= 0:
            raise ValueError("a non-empty content-addressed artifact is required")
        if not quota_window_id.strip() or not source_session_id.strip() or not note.strip():
            raise ValueError("quota_window_id, source_session_id, and note are required")
        path = self.artifacts.verify(artifact_ref)
        detected_format = presentation_format(path)
        if detected_format is None or path.stat().st_size != artifact_size:
            raise ValueError("A12 artifact content or size is invalid")
        receipt_ref = self._resolve_export_receipt(
            export_receipt,
            export_receipt_ref,
            artifact_ref=artifact_ref,
            quota_window_id=quota_window_id,
            source_session_id=source_session_id,
        )
        payload = {
            "check_id": check_id,
            "artifact_ref": artifact_ref,
            "artifact_name": artifact_name,
            "artifact_size": artifact_size,
            "quota_window_id": quota_window_id,
            "source_session_id": source_session_id,
            "provider": "claude-web",
            "provenance_kind": "real-user-journey",
            "detected_format": detected_format,
            "note": note,
            "source": "local-admin-cli",
        }
        if receipt_ref is not None:
            payload["export_receipt_ref"] = receipt_ref
        return self.record_system_event(
            "acceptance.attested",
            payload,
        )

    def record_node_event(
        self,
        event_type: str,
        task_id: str,
        node_id: str,
        payload: dict[str, Any],
    ) -> int:
        with self.transaction() as connection:
            return self._event(connection, event_type, task_id, node_id, payload)

    def record_node_route(
        self,
        task_id: str,
        node_id: str,
        *,
        executor: str,
        model: str,
        payload: dict[str, Any],
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
    ) -> int:
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            changed = connection.execute(
                """
                UPDATE nodes SET effective_executor = ?, effective_model = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'running'
                  AND attempt = ? AND coordinator_epoch = ? AND lease_epoch = ?
                """,
                (
                    executor,
                    model,
                    now_iso(),
                    task_id,
                    node_id,
                    attempt,
                    coordinator_epoch,
                    lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError(f"node {node_id} is not running")
            event_payload = dict(payload)
            # Route callers may describe why a handoff happened, but quota
            # provenance must come from the durable snapshot ledger. Strip
            # caller-supplied copies before attaching the latest persisted row.
            for key in (
                "quota_snapshot",
                "quota_provenance",
                "quota_snapshot_id",
                "quota_source",
            ):
                event_payload.pop(key, None)
            quota_row = connection.execute(
                """
                SELECT id, snapshot_json FROM quota_snapshots
                WHERE provider = 'claude' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if quota_row is not None:
                try:
                    event_payload["quota_snapshot_id"] = int(quota_row["id"])
                    event_payload["quota_snapshot"] = json.loads(quota_row["snapshot_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    event_payload.pop("quota_snapshot_id", None)
                    event_payload.pop("quota_snapshot", None)
            return self._event(connection, "node.routed", task_id, node_id, event_payload)

    def authority_status(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute(
                    """
                    SELECT key, value FROM metadata
                    WHERE key IN (
                        'authority_machine_id',
                        'coordinator_instance_id',
                        'coordinator_epoch'
                    )
                    """
                ).fetchall()
            }
            rows = connection.execute(
                """
                SELECT cursor, event_type, payload_json, created_at FROM events
                WHERE event_type IN ('coordinator.started', 'coordinator.stopped', 'coordinator.failed')
                ORDER BY cursor
                """
            ).fetchall()
        if not rows:
            return None
        instance_id = str(metadata.get("coordinator_instance_id", "")).strip()
        machine_id = str(metadata.get("authority_machine_id", "")).strip()
        try:
            coordinator_epoch = int(metadata.get("coordinator_epoch", "0"))
        except (TypeError, ValueError):
            return None
        if not instance_id or not machine_id or coordinator_epoch <= 0:
            return None

        decoded_rows: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(payload, dict):
                return None
            decoded_rows.append((row, payload))

        latest_row, latest = decoded_rows[-1]
        # A newly activated coordinator owns a strictly greater durable epoch.
        # That epoch fences an older process even when a crash prevented it
        # from recording coordinator.stopped.  Only the latest lifecycle event
        # can prove that the metadata owner is currently live.
        if latest_row["event_type"] != "coordinator.started":
            return None
        if latest.get("instance_id") != instance_id:
            return None
        latest_machine = latest.get("machine_id", latest.get("authority_machine_id"))
        if latest_machine != machine_id:
            return None
        try:
            latest_epoch = int(latest.get("coordinator_epoch", "0"))
            pid = int(latest.get("pid", "0"))
        except (TypeError, ValueError):
            return None
        if latest_epoch != coordinator_epoch or pid <= 0 or not str(latest.get("host", "")).strip():
            return None
        return {
            **latest,
            "active": True,
            "observed_at": latest_row["created_at"],
            "authority_epoch": coordinator_epoch,
        }

    @staticmethod
    def _current_dirty_worktree_recovery_binding(
        raw: object,
        *,
        next_attempt: int,
    ) -> dict[str, Any] | None:
        """Decode only the currently-authorized one-shot recovery receipt.

        This intentionally reads ``nodes.recovery_json`` rather than replaying
        historical events: a previous authorization can never route a later
        attempt.  The receipt remains internal durable state until settlement.
        """

        if raw is None:
            return None
        if not isinstance(raw, str):
            raise StateConflictError("dirty-worktree recovery authorization is invalid")
        try:
            authorization = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StateConflictError(
                "dirty-worktree recovery authorization is invalid JSON"
            ) from error
        required = {
            "schema_version",
            "kind",
            "state",
            "authorization_revision",
            "source_allocation_id",
            "source_result_json",
            "recovery",
        }
        if not isinstance(authorization, dict) or set(authorization) != required:
            raise StateConflictError("dirty-worktree recovery authorization has an invalid shape")
        if (
            authorization["schema_version"] != 1
            or authorization["kind"] != _DIRTY_WORKTREE_RECOVERY_KIND
            or authorization["state"] != "authorized"
        ):
            raise StateConflictError("dirty-worktree recovery authorization is not claimable")
        if (
            isinstance(authorization["authorization_revision"], bool)
            or not isinstance(authorization["authorization_revision"], int)
            or authorization["authorization_revision"] <= 0
            or not isinstance(authorization["source_allocation_id"], str)
            or not authorization["source_allocation_id"]
            or not isinstance(authorization["source_result_json"], str)
        ):
            raise StateConflictError("dirty-worktree recovery authorization fields are invalid")
        try:
            source_result = json.loads(authorization["source_result_json"])
        except json.JSONDecodeError as error:
            raise StateConflictError(
                "dirty-worktree recovery source result is invalid JSON"
            ) from error
        if not isinstance(source_result, dict) or source_result.get("status") != "blocked":
            raise StateConflictError("dirty-worktree recovery source result is not blocked")
        recovery = authorization["recovery"]
        common_fields = {
            "schema_version",
            "source_attempt",
            "source_worktree",
            "source_branch",
            "base_sha",
            "changed_paths",
            "patch_ref",
            "patch_sha256",
        }
        if not isinstance(recovery, dict):
            raise StateConflictError("dirty-worktree recovery receipt has an invalid shape")
        schema_version = recovery.get("schema_version")
        if schema_version == 1:
            receipt_fields = common_fields
        elif schema_version in {2, 3}:
            receipt_fields = common_fields | {
                "source_task_id",
                "source_node_id",
                "input_tree_sha",
                "dependency_input_ref",
            }
            if schema_version == 3:
                receipt_fields.add("untracked_paths")
        else:
            raise StateConflictError("dirty-worktree recovery receipt schema is unsupported")
        if set(recovery) != receipt_fields:
            raise StateConflictError("dirty-worktree recovery receipt has an invalid shape")
        source_attempt = recovery["source_attempt"]
        if (
            isinstance(source_attempt, bool)
            or not isinstance(source_attempt, int)
            or source_attempt + 1 != next_attempt
            or not isinstance(recovery["changed_paths"], list)
            or not recovery["changed_paths"]
            or not all(isinstance(path, str) and path for path in recovery["changed_paths"])
        ):
            raise StateConflictError("dirty-worktree recovery receipt does not match the next attempt")
        fields = [
            "source_worktree",
            "source_branch",
            "base_sha",
            "patch_ref",
            "patch_sha256",
        ]
        if schema_version in {2, 3}:
            fields.extend(
                [
                    "source_task_id",
                    "source_node_id",
                    "input_tree_sha",
                    "dependency_input_ref",
                ]
            )
        for field in fields:
            if not isinstance(recovery[field], str) or not recovery[field]:
                raise StateConflictError(
                    f"dirty-worktree recovery receipt field {field!r} is invalid"
                )
        if schema_version == 3:
            untracked_paths = recovery.get("untracked_paths")
            if (
                not isinstance(untracked_paths, list)
                or not untracked_paths
                or not all(isinstance(path, str) and path for path in untracked_paths)
                or tuple(untracked_paths) != tuple(sorted(set(untracked_paths)))
                or not set(untracked_paths).issubset(recovery["changed_paths"])
            ):
                raise StateConflictError("dirty-worktree recovery receipt untracked_paths are invalid")
        return {
            "authorization_revision": authorization["authorization_revision"],
            "source_allocation_id": authorization["source_allocation_id"],
            "source_result_json": authorization["source_result_json"],
            "recovery": dict(recovery),
        }

    def claim_ready_node(
        self,
        worker_id: str,
        coordinator_epoch: int,
        admissible: Callable[[dict[str, Any]], bool] | None = None,
        *,
        execution_lanes: tuple[str, ...] | None = None,
        lane_capacities: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        if coordinator_epoch <= 0:
            raise ValueError("coordinator_epoch must be positive")
        allowed_lanes = _normalize_execution_lanes(execution_lanes)
        capacities = _normalize_lane_capacities(lane_capacities)
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            candidates = connection.execute(
                """
                SELECT n.*, t.contract_json, t.state AS task_state, t.priority AS task_priority
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.state = 'pending' AND t.state IN ('queued', 'running', 'verifying', 'needs_fix')
                ORDER BY t.priority DESC, t.created_at,
                         json_extract(n.spec_json, '$.ordinal'), n.node_id
                """
            ).fetchall()
            running_accesses: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
            running_task_parallelism: dict[str, bool] = {}
            running_lane_active = {lane: 0 for lane in EXECUTION_LANES}
            for running in connection.execute(
                """
                SELECT n.task_id, n.spec_json, n.effective_executor, n.effective_model,
                       t.contract_json
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.state = 'running'
                """
            ).fetchall():
                persisted_spec = json.loads(running["spec_json"])
                running_spec = {
                    **persisted_spec,
                    "executor": running["effective_executor"] or persisted_spec["executor"],
                    "model": running["effective_model"] or persisted_spec["model"],
                }
                running_contract = json.loads(running["contract_json"])
                task_id = str(running["task_id"])
                running_task_parallelism[task_id] = running_task_parallelism.get(task_id, False) or (
                    running_spec.get("parallelizable") is False
                )
                running_accesses.append(
                    (
                        _repository_identity(running_contract["repository"]),
                        tuple(running_spec.get("read_scopes", [])),
                        tuple(running_spec.get("write_scopes", [])),
                    )
                )
                running_lane_active[execution_lane_for_spec(running_spec)] += 1

            selected: sqlite3.Row | None = None
            selected_spec: dict[str, Any] | None = None
            selected_effective_executor: str | None = None
            selected_effective_model: str | None = None
            selected_lane: str | None = None
            selected_pool: str | None = None
            selected_blocked_retry_authorization_cursor: int | None = None
            selected_dirty_worktree_recovery: dict[str, Any] | None = None
            for candidate in candidates:
                spec = json.loads(candidate["spec_json"])
                candidate_attempt = int(candidate["attempt"]) + 1
                authorization = self._blocked_retry_authorization(
                    connection,
                    str(candidate["task_id"]),
                    str(candidate["node_id"]),
                    candidate_attempt,
                )
                dirty_worktree_recovery = self._current_dirty_worktree_recovery_binding(
                    candidate["recovery_json"],
                    next_attempt=candidate_attempt,
                )
                if dirty_worktree_recovery is not None:
                    # Recovery preparation runs locally against the sealed
                    # receipt; it must never consume a subscription route.
                    effective_executor = "deterministic"
                    effective_model = "blocked-worktree-recovery"
                elif authorization is not None:
                    effective_executor = str(authorization["executor"])
                    effective_model = str(authorization["model"])
                else:
                    effective_executor = str(candidate["effective_executor"] or spec["executor"])
                    effective_model = retry_model(
                        str(candidate["effective_model"] or spec["model"]),
                        candidate_attempt,
                        verifier=bool(spec.get("verifier")),
                        routing_policy_version=spec.get("routing_policy_version"),
                    )
                effective_spec = {
                    **spec,
                    "executor": effective_executor,
                    "model": effective_model,
                    "model_profile": codex_model_profile(effective_model),
                    "model_reasoning_effort": codex_model_reasoning_effort(effective_model),
                }
                lane = execution_lane_for_spec(effective_spec)
                if allowed_lanes is not None and lane not in allowed_lanes:
                    continue
                if lane in capacities and running_lane_active[lane] >= capacities[lane]:
                    continue
                dependencies = spec.get("depends_on", [])
                if dependencies:
                    placeholders = ",".join("?" for _ in dependencies)
                    states = connection.execute(
                        f"SELECT node_id, state FROM nodes WHERE task_id = ? AND node_id IN ({placeholders})",
                        (candidate["task_id"], *dependencies),
                    ).fetchall()
                    if len(states) != len(dependencies) or any(row["state"] != "accepted" for row in states):
                        continue
                candidate_task_id = str(candidate["task_id"])
                candidate_parallelizable = spec.get("parallelizable") is not False
                if candidate_task_id in running_task_parallelism and (
                    not candidate_parallelizable or running_task_parallelism[candidate_task_id]
                ):
                    continue
                read_scopes = tuple(spec.get("read_scopes", []))
                write_scopes = tuple(spec.get("write_scopes", []))
                candidate_contract = json.loads(candidate["contract_json"])
                repository = _repository_identity(candidate_contract["repository"])
                if any(
                    repository == running_repository
                    and scope_access_conflicts(read_scopes, write_scopes, running_reads, running_writes)
                    for running_repository, running_reads, running_writes in running_accesses
                ):
                    continue
                if admissible is not None and not admissible(effective_spec):
                    continue
                selected = candidate
                selected_spec = spec
                selected_effective_executor = effective_executor
                selected_effective_model = effective_model
                selected_lane = lane
                selected_pool = quota_pool_id_for_spec(effective_spec)
                selected_blocked_retry_authorization_cursor = authorization["event_cursor"] if authorization is not None else None
                selected_dirty_worktree_recovery = dirty_worktree_recovery
                break

            if (
                selected is None
                or selected_spec is None
                or selected_effective_executor is None
                or selected_effective_model is None
                or selected_lane is None
                or selected_pool is None
            ):
                return None

            attempt = int(selected["attempt"]) + 1
            lease_epoch = self._next_lease_epoch(connection)
            effective_executor = selected_effective_executor
            effective_model = selected_effective_model
            connection.execute(
                """
                UPDATE nodes SET state = 'running', attempt = ?, worker_id = ?,
                                 effective_executor = ?, effective_model = ?,
                                 coordinator_epoch = ?, lease_epoch = ?,
                                 started_at = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'pending'
                """,
                (
                    attempt,
                    worker_id,
                    effective_executor,
                    effective_model,
                    coordinator_epoch,
                    lease_epoch,
                    timestamp,
                    timestamp,
                    selected["task_id"],
                    selected["node_id"],
                ),
            )
            if selected["task_state"] in {"queued", "needs_fix"}:
                connection.execute(
                    """
                    UPDATE tasks SET state = 'running', state_revision = state_revision + 1,
                                     updated_at = ?, blocker = NULL
                    WHERE task_id = ?
                    """,
                    (timestamp, selected["task_id"]),
                )
                self._event(
                    connection,
                    "task.state_changed",
                    selected["task_id"],
                    None,
                    {"from": selected["task_state"], "to": "running"},
                )
            self._event(
                connection,
                "node.started",
                selected["task_id"],
                selected["node_id"],
                {
                    "attempt": attempt,
                    "worker_id": worker_id,
                    "executor": effective_executor,
                    "model": effective_model,
                    "model_profile": codex_model_profile(effective_model),
                    "model_reasoning_effort": codex_model_reasoning_effort(effective_model),
                    "coordinator_epoch": coordinator_epoch,
                    "lease_epoch": lease_epoch,
                    "execution_lane": selected_lane,
                    "quota_pool_id": selected_pool,
                    "lane_capacity": capacities.get(selected_lane),
                    "lane_active_units": running_lane_active[selected_lane] + 1,
                    "claimed_at": timestamp,
                    **({
                        "blocked_retry_authorization_event_cursor": selected_blocked_retry_authorization_cursor,
                    } if selected_blocked_retry_authorization_cursor is not None else {}),
                    **({
                        "blocked_worktree_recovery": {
                            "source_attempt": selected_dirty_worktree_recovery["recovery"]["source_attempt"],
                            "source_allocation_id": selected_dirty_worktree_recovery["source_allocation_id"],
                            "patch_ref": selected_dirty_worktree_recovery["recovery"]["patch_ref"],
                        },
                    } if selected_dirty_worktree_recovery is not None else {}),
                },
                created_at=timestamp,
            )
            steering = tuple(
                row["instruction"]
                for row in connection.execute(
                    """
                    SELECT instruction FROM task_steering
                    WHERE task_id = ? ORDER BY sequence
                    """,
                    (selected["task_id"],),
                ).fetchall()
            )
            return {
                "task_id": selected["task_id"],
                "node_id": selected["node_id"],
                "attempt": attempt,
                "coordinator_epoch": coordinator_epoch,
                "lease_epoch": lease_epoch,
                "spec": {
                    **selected_spec,
                    "executor": effective_executor,
                    "model": effective_model,
                    "model_profile": codex_model_profile(effective_model),
                    "model_reasoning_effort": codex_model_reasoning_effort(effective_model),
                    "execution_lane": selected_lane,
                    "quota_pool_id": selected_pool,
                },
                "contract": json.loads(selected["contract_json"]),
                "steering": steering,
                **({
                    "blocked_worktree_recovery": selected_dirty_worktree_recovery,
                } if selected_dirty_worktree_recovery is not None else {}),
            }

    def _validate_dirty_worktree_recovery_target(
        self,
        *,
        contract: dict[str, Any],
        task_id: str,
        node_id: str,
        attempt: int,
        worktree: str,
        recovery_binding: dict[str, Any],
    ) -> tuple[Path, bytes]:
        """Verify that an a2 target is the exact prepared a1 worker patch."""

        recovery = recovery_binding["recovery"]
        if recovery["base_sha"] != contract["base_sha"]:
            raise StateConflictError("dirty-worktree recovery receipt base does not match task contract")
        try:
            target = Path(worktree).expanduser().resolve(strict=True)
            artifacts = ArtifactStore(self.path.parent / "artifacts")
            patch = artifacts.verify(str(recovery["patch_ref"])).read_bytes()
        except (OSError, ValueError) as error:
            raise StateConflictError(
                f"dirty-worktree recovery target or patch artifact is unavailable: {error}"
            ) from error
        if sha256(patch).hexdigest() != recovery["patch_sha256"]:
            raise StateConflictError(
                "dirty-worktree recovery patch artifact does not match the captured receipt"
            )
        comparison_tree = recovery["base_sha"]
        if recovery.get("schema_version") in {2, 3}:
            if recovery.get("source_task_id") != task_id or recovery.get("source_node_id") != node_id:
                raise StateConflictError("dependency recovery receipt belongs to another node")
            try:
                dependency_input = load_recorded_dependency_input(
                    artifacts,
                    str(recovery["dependency_input_ref"]),
                    task_id=task_id,
                    node_id=node_id,
                    base_sha=str(contract["base_sha"]),
                )
            except Exception as error:
                raise StateConflictError(
                    f"dependency recovery input is unavailable or invalid: {error}"
                ) from error
            if dependency_input.input_tree_sha != recovery.get("input_tree_sha"):
                raise StateConflictError(
                    "dependency recovery input tree does not match the recovery receipt"
                )
            comparison_tree = dependency_input.input_tree_sha
        elif recovery.get("schema_version") != 1:
            raise StateConflictError("dirty-worktree recovery receipt schema is unsupported")
        expected_branch = WorktreeManager.branch_name(task_id, node_id, attempt)
        if self._recovery_git_bytes(target, "rev-parse", "HEAD").decode().strip() != recovery["base_sha"]:
            raise StateConflictError("dirty-worktree recovery target no longer matches contract base")
        if self._recovery_git_bytes(target, "branch", "--show-current").decode().strip() != expected_branch:
            raise StateConflictError("dirty-worktree recovery target branch is invalid")
        if self._recovery_git_bytes(target, "ls-files", "--others", "--exclude-standard", "-z"):
            raise StateConflictError("dirty-worktree recovery target has unexpected untracked files")
        if self._recovery_git_bytes(target, "diff", "--binary", comparison_tree) != patch:
            raise StateConflictError(
                "dirty-worktree recovery target does not match the captured source patch"
            )
        return target, patch

    def assign_worktree(
        self,
        task_id: str,
        node_id: str,
        worktree: str,
        *,
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
    ) -> None:
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            task = connection.execute(
                "SELECT contract_json FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            node = connection.execute(
                """
                SELECT state, attempt, coordinator_epoch, lease_epoch, worktree, recovery_json
                FROM nodes WHERE task_id = ? AND node_id = ?
                """,
                (task_id, node_id),
            ).fetchone()
            if node is None:
                raise KeyError((task_id, node_id))
            if (
                node["state"] != "running"
                or int(node["attempt"]) != attempt
                or int(node["coordinator_epoch"]) != coordinator_epoch
                or int(node["lease_epoch"]) != lease_epoch
            ):
                raise StateConflictError(f"node {node_id} lease is stale")
            contract = json.loads(task["contract_json"])
            branch = WorktreeManager.branch_name(task_id, node_id, attempt)
            allocation_id = "wta-" + canonical_hash(
                {"task_id": task_id, "node_id": node_id, "attempt": attempt}
            )[:24]
            timestamp = now_iso()
            recovery_binding = None
            consumed_recovery_json = None
            assigned_worktree = worktree
            if node["recovery_json"] is not None:
                if node["worktree"] is not None:
                    raise StateConflictError("dirty-worktree recovery target was already assigned")
                recovery_binding = self._current_dirty_worktree_recovery_binding(
                    node["recovery_json"],
                    next_attempt=attempt,
                )
                target, _ = self._validate_dirty_worktree_recovery_target(
                    contract=contract,
                    task_id=task_id,
                    node_id=node_id,
                    attempt=attempt,
                    worktree=worktree,
                    recovery_binding=recovery_binding,
                )
                try:
                    authorization = json.loads(node["recovery_json"])
                except json.JSONDecodeError as error:
                    raise StateConflictError(
                        "dirty-worktree recovery authorization is invalid JSON"
                    ) from error
                consumed_recovery_json = canonical_json(
                    {
                        **authorization,
                        "state": "consumed",
                        "consumed_at": timestamp,
                        "target_attempt": attempt,
                        "target_worktree": str(target),
                        "target_branch": branch,
                    }
                )
                assigned_worktree = str(target)
            changed = connection.execute(
                """
                UPDATE nodes SET worktree = ?, recovery_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'running'
                  AND attempt = ? AND coordinator_epoch = ? AND lease_epoch = ?
                """,
                (
                    assigned_worktree,
                    consumed_recovery_json if recovery_binding is not None else node["recovery_json"],
                    timestamp,
                    task_id,
                    node_id,
                    attempt,
                    coordinator_epoch,
                    lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError(f"node {node_id} lease is stale")
            if recovery_binding is not None:
                superseded = connection.execute(
                    """
                    UPDATE worktree_allocations
                    SET state = 'superseded', updated_at = ?
                    WHERE allocation_id = ? AND state = 'active'
                    """,
                    (timestamp, recovery_binding["source_allocation_id"]),
                ).rowcount
                if superseded != 1:
                    raise StateConflictError(
                        "dirty-worktree recovery source allocation is not active"
                    )
            connection.execute(
                """
                INSERT INTO worktree_allocations(
                    allocation_id, task_id, node_id, attempt, repository,
                    base_sha, branch, current_path, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(task_id, node_id, attempt) DO UPDATE SET
                    current_path = excluded.current_path,
                    updated_at = excluded.updated_at
                """,
                (
                    allocation_id,
                    task_id,
                    node_id,
                    attempt,
                    contract["repository"],
                    contract["base_sha"],
                    branch,
                    assigned_worktree,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(
                connection,
                "worktree.allocated",
                task_id,
                node_id,
                {
                    "allocation_id": allocation_id,
                    "attempt": attempt,
                    "path": assigned_worktree,
                    "branch": branch,
                },
            )
            if recovery_binding is not None:
                self._event(
                    connection,
                    "node.blocked_worktree_recovery_consumed",
                    task_id,
                    node_id,
                    {
                        "attempt": attempt,
                        "source_attempt": recovery_binding["recovery"]["source_attempt"],
                        "source_allocation_id": recovery_binding["source_allocation_id"],
                        "recovery": recovery_binding["recovery"],
                        "target_worktree": assigned_worktree,
                        "target_branch": branch,
                    },
                    created_at=timestamp,
                )

    def _dirty_worktree_recovery_for_settlement(
        self,
        raw: object,
        *,
        attempt: int,
    ) -> dict[str, Any] | None:
        """Return a current receipt only for its authorized or consumed a2."""

        if raw is None:
            return None
        if not isinstance(raw, str):
            raise StateConflictError("dirty-worktree recovery receipt is invalid")
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StateConflictError("dirty-worktree recovery receipt is invalid JSON") from error
        if not isinstance(stored, dict):
            raise StateConflictError("dirty-worktree recovery receipt is invalid")
        state = stored.get("state")
        base_fields = {
            "schema_version",
            "kind",
            "state",
            "authorization_revision",
            "source_allocation_id",
            "source_result_json",
            "recovery",
        }
        if state == "authorized":
            binding = self._current_dirty_worktree_recovery_binding(raw, next_attempt=attempt)
            return {"state": state, **binding}
        consumed_fields = base_fields | {
            "consumed_at",
            "target_attempt",
            "target_worktree",
            "target_branch",
        }
        if state != "consumed" or set(stored) != consumed_fields:
            raise StateConflictError("dirty-worktree recovery receipt is not current")
        if (
            stored.get("target_attempt") != attempt
            or not isinstance(stored.get("consumed_at"), str)
            or not isinstance(stored.get("target_worktree"), str)
            or not stored["target_worktree"]
            or not isinstance(stored.get("target_branch"), str)
            or not stored["target_branch"]
        ):
            raise StateConflictError("dirty-worktree recovery consumed receipt is invalid")
        authorized = {key: stored[key] for key in base_fields}
        authorized["state"] = "authorized"
        binding = self._current_dirty_worktree_recovery_binding(
            canonical_json(authorized),
            next_attempt=attempt,
        )
        return {
            "state": state,
            "target_worktree": stored["target_worktree"],
            "target_branch": stored["target_branch"],
            "consumed_at": stored["consumed_at"],
            **binding,
        }

    def _rollback_authorized_dirty_worktree_recovery(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        node_id: str,
        row: sqlite3.Row,
        recovery: dict[str, Any],
        result: NodeResult,
        timestamp: str,
    ) -> None:
        """Restore the blocked a1 receipt when a2 was never allocated."""

        if result.status not in {"blocked", "failed"}:
            raise ValueError("unprepared dirty-worktree recovery may only block or fail")
        source = recovery["recovery"]
        allocation = connection.execute(
            """
            SELECT state, current_path, branch FROM worktree_allocations
            WHERE allocation_id = ?
            """,
            (recovery["source_allocation_id"],),
        ).fetchone()
        if (
            allocation is None
            or allocation["state"] != "active"
            or allocation["current_path"] != source["source_worktree"]
            or allocation["branch"] != source["source_branch"]
        ):
            raise StateConflictError(
                "dirty-worktree recovery source allocation is no longer active"
            )
        changed = connection.execute(
            """
            UPDATE nodes
            SET state = 'blocked', attempt = ?, worker_id = NULL, worktree = ?,
                effective_executor = NULL, effective_model = NULL,
                started_at = NULL, settled_at = ?, result_json = ?, recovery_json = NULL,
                coordinator_epoch = 0, lease_epoch = 0, updated_at = ?
            WHERE task_id = ? AND node_id = ? AND state = 'running'
              AND attempt = ? AND coordinator_epoch = ? AND lease_epoch = ?
            """,
            (
                source["source_attempt"],
                source["source_worktree"],
                timestamp,
                recovery["source_result_json"],
                timestamp,
                task_id,
                node_id,
                row["attempt"],
                row["coordinator_epoch"],
                row["lease_epoch"],
            ),
        ).rowcount
        if changed != 1:
            raise StateConflictError("dirty-worktree recovery rollback lease is stale")
        task = connection.execute(
            "SELECT state, state_revision FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert task is not None
        revision = int(task["state_revision"]) + 1
        connection.execute(
            """
            UPDATE tasks
            SET state = 'blocked', state_revision = ?, updated_at = ?, blocker = ?, verdict = NULL
            WHERE task_id = ?
            """,
            (revision, timestamp, result.summary, task_id),
        )
        self._event(
            connection,
            "node.blocked_worktree_recovery_rolled_back",
            task_id,
            node_id,
            {
                "attempt": int(row["attempt"]),
                "source_attempt": source["source_attempt"],
                "source_allocation_id": recovery["source_allocation_id"],
                "recovery": source,
                "preparation_result": result.to_dict(),
                "task_revision": revision,
            },
            created_at=timestamp,
        )
        self._event(
            connection,
            "task.state_changed",
            task_id,
            None,
            {
                "from": task["state"],
                "to": "blocked",
                "revision": revision,
                "blocker": result.summary,
                "recovery_rolled_back": True,
            },
            created_at=timestamp,
        )

    def settle_node(
        self,
        task_id: str,
        node_id: str,
        result: NodeResult,
        *,
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
    ) -> None:
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            row = connection.execute(
                """
                SELECT n.state, n.attempt, n.coordinator_epoch, n.lease_epoch,
                       n.effective_executor, n.effective_model, n.spec_json, n.recovery_json, t.contract_json
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.task_id = ? AND n.node_id = ?
                """,
                (task_id, node_id),
            ).fetchone()
            if row is None:
                raise KeyError((task_id, node_id))
            if row["state"] != "running":
                raise StateConflictError(f"node {node_id} is {row['state']}, expected running")
            if (
                int(row["attempt"]) != attempt
                or int(row["coordinator_epoch"]) != coordinator_epoch
                or int(row["lease_epoch"]) != lease_epoch
            ):
                raise StateConflictError(f"node {node_id} lease is stale")
            spec = json.loads(row["spec_json"])
            contract = json.loads(row["contract_json"])
            recovery = self._dirty_worktree_recovery_for_settlement(
                row["recovery_json"],
                attempt=attempt,
            )
            self._validate_result_contract(spec, row, result)
            self._verify_artifact_refs(result.artifacts)
            if recovery is not None and recovery["state"] == "authorized":
                self._rollback_authorized_dirty_worktree_recovery(
                    connection,
                    task_id=task_id,
                    node_id=node_id,
                    row=row,
                    recovery=recovery,
                    result=result,
                    timestamp=timestamp,
                )
                return
            if spec.get("verifier") and spec.get("executor") != "fixture":
                for ref in result.evidence:
                    self.artifacts.verify(ref)
            if result.status == "succeeded" and spec.get("verifier"):
                missing = self._missing_required_artifacts(connection, task_id, contract, result)
                if missing:
                    result = replace(
                        result,
                        status="failed",
                        summary=f"required acceptance Evidence is missing: {', '.join(missing)}",
                        retryable=False,
                        verdict="needs_fix",
                    )
            if result.status == "succeeded":
                node_state = "accepted"
            elif result.status == "indeterminate":
                node_state = "indeterminate"
            elif result.status == "blocked":
                node_state = "blocked"
            else:
                node_state = "failed"
            connection.execute(
                """
                UPDATE nodes
                SET state = ?, settled_at = ?, updated_at = ?, result_json = ?, recovery_json = NULL
                WHERE task_id = ? AND node_id = ?
                """,
                (node_state, timestamp, timestamp, canonical_json(result.to_dict()), task_id, node_id),
            )
            connection.execute(
                """
                UPDATE worktree_allocations
                SET node_result_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND attempt = ?
                """,
                (canonical_json(result.to_dict()), timestamp, task_id, node_id, attempt),
            )
            node_event = {"attempt": row["attempt"], "result": result.to_dict()}
            if recovery is not None:
                node_event["blocked_worktree_recovery"] = {
                    "source_attempt": recovery["recovery"]["source_attempt"],
                    "source_allocation_id": recovery["source_allocation_id"],
                    "recovery": recovery["recovery"],
                }
            self._event(
                connection,
                f"node.{node_state}",
                task_id,
                node_id,
                node_event,
            )
            if spec.get("verifier") and result.evidence:
                for ref in result.evidence:
                    self._event(
                        connection,
                        "verifier.evidence_claimed",
                        task_id,
                        node_id,
                        {
                            "attempt": int(row["attempt"]),
                            "artifact_ref": ref,
                            "checks": list(result.checks),
                            "verdict": result.verdict,
                        },
                    )

            task = connection.execute(
                "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert task is not None
            task_revision = int(task["state_revision"])
            next_state = task["state"]
            blocker: str | None = None
            verdict: str | None = None
            if node_state == "accepted" and spec.get("verifier"):
                next_state = "accepted"
                verdict = result.summary
            elif node_state == "indeterminate":
                next_state = "needs_approval"
                blocker = f"node {node_id} has an indeterminate result"
            elif node_state == "blocked":
                next_state = "blocked"
                blocker = result.summary
            elif node_state == "failed":
                if spec.get("verifier") and int(row["attempt"]) <= int(
                    contract.get("retry_limit", 0)
                ):
                    feedback = f"Verifier rejected attempt {row['attempt']}: {result.summary}"[:500]
                    steering_id = "steering-" + canonical_hash(
                        {
                            "task_id": task_id,
                            "verifier_node": node_id,
                            "attempt": int(row["attempt"]),
                            "feedback": feedback,
                        }
                    )[:24]
                    sequence = self._next_steering_sequence(connection, task_id)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO task_steering(
                            steering_id, task_id, instruction, created_at, sequence
                        ) VALUES(?, ?, ?, ?, ?)
                        """,
                        (steering_id, task_id, feedback, timestamp, sequence),
                    )
                    connection.execute(
                        """
                        UPDATE nodes
                        SET state = 'pending', worker_id = NULL, worktree = NULL,
                            effective_executor = NULL, effective_model = NULL,
                            started_at = NULL, settled_at = NULL,
                            result_json = NULL,
                            coordinator_epoch = 0, lease_epoch = 0, updated_at = ?
                        WHERE task_id = ?
                          AND (node_id = ? OR json_extract(spec_json, '$.verifier') = 0)
                        """,
                        (timestamp, task_id, node_id),
                    )
                    next_state = "queued"
                    self._event(
                        connection,
                        "task.repair_scheduled",
                        task_id,
                        node_id,
                        {
                            "verifier_attempt": int(row["attempt"]),
                            "feedback_steering_id": steering_id,
                        },
                    )
                elif result.retryable and int(row["attempt"]) <= int(contract.get("retry_limit", 0)):
                    connection.execute(
                        """
                        UPDATE nodes SET state = 'pending', worker_id = NULL,
                                         started_at = NULL, settled_at = NULL, updated_at = ?
                        WHERE task_id = ? AND node_id = ?
                        """,
                        (timestamp, task_id, node_id),
                    )
                    self._event(
                        connection,
                        "node.retry_scheduled",
                        task_id,
                        node_id,
                        {"attempt": row["attempt"]},
                    )
                else:
                    next_state = "needs_fix"
                    blocker = result.summary
            elif node_state == "accepted":
                pending_non_verifiers = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM nodes
                    WHERE task_id = ?
                      AND json_extract(spec_json, '$.verifier') = 0
                      AND state != 'accepted'
                    """,
                    (task_id,),
                ).fetchone()["count"]
                if pending_non_verifiers == 0:
                    next_state = "verifying"

            if next_state != task["state"] or blocker or verdict:
                revision = task_revision + 1
                connection.execute(
                    """
                    UPDATE tasks SET state = ?, state_revision = ?, updated_at = ?,
                                     blocker = ?, verdict = ? WHERE task_id = ?
                    """,
                    (next_state, revision, timestamp, blocker, verdict, task_id),
                )
                self._event(
                    connection,
                    "task.state_changed",
                    task_id,
                    None,
                    {"from": task["state"], "to": next_state, "revision": revision, "blocker": blocker},
                )
                task_revision = revision
            if node_state == "indeterminate":
                self._create_indeterminate_approval(
                    connection,
                    task_id,
                    node_id,
                    int(row["attempt"]),
                    task_revision,
                    result.summary,
                )

    def settle_claimed(self, claimed: dict[str, Any], result: NodeResult) -> None:
        self.settle_node(
            str(claimed["task_id"]),
            str(claimed["node_id"]),
            result,
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )

    def _validate_result_contract(
        self,
        spec: dict[str, Any],
        row: sqlite3.Row,
        result: NodeResult,
    ) -> None:
        recovery = self._dirty_worktree_recovery_for_settlement(
            row["recovery_json"],
            attempt=int(row["attempt"]),
        )
        if spec.get("executor") == "fixture" or spec.get("model") == "fixture":
            if recovery is None:
                return
        contract = json.loads(row["contract_json"])
        expected_profile, expected_tier = governance_identity(contract)
        if result.governance_profile != expected_profile:
            raise ValueError(
                f"result governance profile {result.governance_profile!r} does not match "
                f"contract profile {expected_profile!r}"
            )
        if result.verification_tier != expected_tier:
            raise ValueError(
                f"result verification tier {result.verification_tier!r} does not match "
                f"contract tier {expected_tier!r}"
            )
        if recovery is not None:
            if result.provider != _DIRTY_WORKTREE_RECOVERY_PROVIDER:
                raise ValueError(
                    "dirty-worktree recovery result must use its exact recovery provider"
                )
            if result.actual_model is not None:
                raise ValueError("dirty-worktree recovery result must not attest a native model")
            if result.result_kind != "worker":
                raise ValueError("dirty-worktree recovery result must declare result_kind=worker")
            if not result.checks or not result.artifacts:
                raise ValueError(
                    "dirty-worktree recovery result must contain structured checks and artifacts"
                )
            return
        if result.provider == _DIRTY_WORKTREE_RECOVERY_PROVIDER:
            raise ValueError("dirty-worktree recovery provider has no persisted receipt")
        if spec.get("verifier"):
            if result.result_kind != "verifier":
                raise ValueError("verifier result must declare result_kind=verifier")
            WorkbenchStore._validate_actual_model(row, result)
            if not is_codex_control_plane_model(row["effective_model"] or ""):
                raise ValueError(
                    "only an exact Codex control-plane verifier may settle the verifier node"
                )
            expected = {
                "succeeded": "accepted",
                "failed": "needs_fix",
                "blocked": "blocked",
            }.get(result.status)
            if expected is not None and result.verdict != expected:
                raise ValueError(
                    f"verifier result status {result.status} requires verdict {expected}"
                )
            if result.status in {"blocked", "indeterminate"}:
                return
            if not result.checks:
                raise ValueError("verifier result must contain structured checks")
            if not result.evidence:
                raise ValueError("verifier result must contain structured evidence")
            return
        if result.result_kind != "worker":
            raise ValueError("worker result must declare result_kind=worker")
        WorkbenchStore._validate_actual_model(row, result)
        if result.status == "succeeded" and not result.checks:
            raise ValueError("successful worker result must contain structured checks")
        directive = spec.get("archify")
        if (
            result.status == "succeeded"
            and isinstance(directive, dict)
            and directive.get("schema_version") == 1
            and directive.get("artifact_required") is True
        ):
            self._validate_archify_worker_evidence(result)

    def _validate_archify_worker_evidence(self, result: NodeResult) -> None:
        """Validate command-appropriate Archify evidence before persistence.

        Renderer-owning commands use the existing pinned render-validation
        envelope.  ``validate`` and ``migrate`` do not own a graphic output,
        but their command execution must still be replayed by the host and
        bound to the frozen inputs.  A receipt-only model claim is not
        persistence-worthy evidence.
        """

        receipt_ref = result.artifacts.get("archify-receipt")
        if not isinstance(receipt_ref, str):
            raise ValueError(
                "successful Archify worker result must contain independent receipt evidence: "
                "archify-receipt"
            )
        try:
            receipt_path = self.artifacts.verify(receipt_ref)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"Archify receipt evidence is unreadable: {error}") from error
        if not isinstance(receipt, dict):
            raise ValueError("Archify receipt evidence must decode to an object")
        command = receipt.get("command")
        if command not in _ARCHIFY_RENDER_COMMANDS | _ARCHIFY_RECEIPT_ONLY_COMMANDS:
            raise ValueError(f"Archify receipt command is unsupported: {command!r}")

        execution_ref = result.artifacts.get("archify-execution")
        if not isinstance(execution_ref, str):
            raise ValueError(
                "successful Archify worker result must contain command execution evidence: "
                "archify-execution"
            )
        try:
            execution_path = self.artifacts.verify(execution_ref)
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"Archify execution evidence is unreadable: {error}") from error
        if not isinstance(execution, dict):
            raise ValueError("Archify execution evidence must decode to an object")
        if (
            execution.get("schema_version") != 1
            or execution.get("receipt_ref") != receipt_ref
            or execution.get("receipt_command") != command
        ):
            raise ValueError("Archify execution evidence is not bound to its command receipt")
        if command in _ARCHIFY_RECEIPT_ONLY_COMMANDS:
            expected_fields = {
                "schema_version",
                "kind",
                "receipt_ref",
                "receipt_command",
                "frozen_input",
                "frozen_source",
                "frozen_destination",
                "proof",
                "argv",
                "stdout",
                "stderr",
                "exit_code",
                "provenance",
                "stdout_ref",
                "stderr_ref",
                "cli_receipt",
            }
            if set(execution) != expected_fields or execution.get("kind") != "archify-executor-command-validation":
                raise ValueError(
                    "validate/migrate Archify execution evidence must use the host command-validation envelope"
                )
            expected_mode = (
                "pinned-validate-and-frozen-input-binding"
                if command == "validate"
                else "pinned-migrate-and-frozen-source-destination-binding"
            )
            if execution.get("proof") != {
                "mode": expected_mode,
                "renderer_check": "not-applicable",
            }:
                raise ValueError(
                    "validate/migrate Archify execution evidence must preserve host input binding"
                )

            def valid_binding(value: object, label: str) -> dict[str, Any]:
                if not isinstance(value, dict) or set(value) != {"path", "sha256", "bytes"}:
                    raise ValueError(f"Archify {command} execution evidence has invalid {label} binding")
                path = value.get("path")
                digest = value.get("sha256")
                byte_count = value.get("bytes")
                if (
                    not isinstance(path, str)
                    or not path
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or not isinstance(byte_count, int)
                    or isinstance(byte_count, bool)
                    or byte_count < 0
                ):
                    raise ValueError(f"Archify {command} execution evidence has invalid {label} binding")
                return value

            if not isinstance(execution.get("argv"), list) or any(
                not isinstance(value, str) or not value for value in execution["argv"]
            ):
                raise ValueError("Archify command execution evidence argv is invalid")
            if not isinstance(execution.get("stdout"), str) or not isinstance(execution.get("stderr"), str):
                raise ValueError("Archify command execution evidence logs are invalid")
            if execution.get("exit_code") != 0:
                raise ValueError("successful Archify command execution evidence must have exit_code 0")
            if not isinstance(execution.get("provenance"), dict) or execution["provenance"].get("ok") is not True:
                raise ValueError("Archify command execution evidence provenance is invalid")
            if not isinstance(execution.get("cli_receipt"), dict):
                raise ValueError("Archify command execution evidence CLI receipt is invalid")

            stdout_ref = execution.get("stdout_ref")
            stderr_ref = execution.get("stderr_ref")
            if not isinstance(stdout_ref, str) or not isinstance(stderr_ref, str):
                raise ValueError("Archify command execution evidence log references are invalid")
            try:
                stdout = self.artifacts.verify(stdout_ref).read_text(encoding="utf-8")
                stderr = self.artifacts.verify(stderr_ref).read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as error:
                raise ValueError(f"Archify command execution evidence logs are unreadable: {error}") from error
            if stdout != execution["stdout"] or stderr != execution["stderr"]:
                raise ValueError("Archify command execution evidence logs do not match their artifacts")

            if command == "validate":
                frozen_input = valid_binding(execution.get("frozen_input"), "frozen_input")
                if execution.get("frozen_source") is not None or execution.get("frozen_destination") is not None:
                    raise ValueError("validate Archify execution evidence must not contain migration bindings")
                receipt_input = receipt.get("input")
                if not isinstance(receipt_input, str) or not receipt_input:
                    raise ValueError("validate Archify receipt is missing its input binding")
                if Path(receipt_input).expanduser().is_absolute():
                    try:
                        if Path(receipt_input).expanduser().resolve() != Path(frozen_input["path"]).expanduser().resolve():
                            raise ValueError("validate Archify execution evidence is not bound to frozen_input")
                    except OSError as error:
                        raise ValueError(f"validate Archify input binding cannot be resolved: {error}") from error
            else:
                if execution.get("frozen_input") is not None:
                    raise ValueError("migrate Archify execution evidence must not contain a validate binding")
                frozen_source = valid_binding(execution.get("frozen_source"), "frozen_source")
                frozen_destination = valid_binding(execution.get("frozen_destination"), "frozen_destination")
                for label, frozen in (("source", frozen_source), ("destination", frozen_destination)):
                    receipt_binding = receipt.get(label)
                    if not isinstance(receipt_binding, dict) or any(
                        receipt_binding.get(key) != frozen[key] for key in ("path", "sha256", "bytes")
                    ):
                        raise ValueError(f"migrate Archify execution evidence is not bound to frozen_{label}")
        elif execution.get("kind") != "archify-executor-render-validation":
            raise ValueError(
                "deliver/compare/visual-check Archify execution evidence must use render validation"
            )

    @staticmethod
    def _validate_actual_model(row: sqlite3.Row, result: NodeResult) -> None:
        executor = str(row["effective_executor"] or "")
        leased_model = str(row["effective_model"] or "")
        if (
            result.status in {"succeeded", "failed"}
            and executor in {"codex", "claude"}
            and not WorkbenchStore._actual_model_matches_lease(
                executor,
                leased_model,
                result.actual_model,
            )
        ):
            raise ValueError(
                f"result actual_model {result.actual_model!r} does not match leased model "
                f"{row['effective_model']!r}"
            )

    @staticmethod
    def _actual_model_matches_lease(
        executor: str,
        leased_model: str,
        actual_model: str | None,
    ) -> bool:
        if actual_model is None:
            return False
        leased = leased_model.strip().lower()
        actual = actual_model.strip().lower()
        if executor != "claude":
            return actual == leased
        for family in ("opus", "sonnet", "fable"):
            if leased == family:
                return family in actual
        return actual == leased or actual.startswith(f"{leased}-")

    @staticmethod
    def _missing_required_artifacts(
        connection: sqlite3.Connection,
        task_id: str,
        contract: dict[str, Any],
        verifier_result: NodeResult,
    ) -> list[str]:
        rows = connection.execute(
            "SELECT spec_json, result_json FROM nodes WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        specs = [json.loads(row["spec_json"]) for row in rows]
        if specs and all(spec["executor"] == "fixture" for spec in specs):
            return []
        artifact_keys = set(verifier_result.artifacts)
        for row in rows:
            if row["result_json"]:
                artifact_keys.update(json.loads(row["result_json"]).get("artifacts", {}))
        missing: list[str] = []
        for required in contract.get("required_artifacts", []):
            if required == "diff":
                present = "patch" in artifact_keys
            elif required == "test-log":
                present = bool({"test-log", "stdout", "stderr"} & artifact_keys)
            elif required == "verdict":
                present = bool(verifier_result.summary.strip())
            else:
                present = required in artifact_keys
            if not present:
                missing.append(required)
        return missing

    def recover_interrupted(self) -> int:
        timestamp = now_iso()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT task_id, node_id, attempt, spec_json FROM nodes WHERE state = 'running'"
            ).fetchall()
            for row in rows:
                spec = json.loads(row["spec_json"])
                result = NodeResult(
                    status="indeterminate",
                    summary="coordinator restarted while the worker was running; explicit resolution required",
                    result_kind="verifier" if spec.get("verifier") else "worker",
                )
                connection.execute(
                    """
                    UPDATE nodes SET state = 'indeterminate', settled_at = ?, updated_at = ?,
                                     result_json = ?
                    WHERE task_id = ? AND node_id = ?
                    """,
                    (timestamp, timestamp, canonical_json(result.to_dict()), row["task_id"], row["node_id"]),
                )
                connection.execute(
                    """
                    UPDATE tasks SET state = 'needs_approval', state_revision = state_revision + 1,
                                     updated_at = ?, blocker = ? WHERE task_id = ?
                    """,
                    (timestamp, f"node {row['node_id']} is indeterminate after restart", row["task_id"]),
                )
                task_revision = int(
                    connection.execute(
                        "SELECT state_revision FROM tasks WHERE task_id = ?",
                        (row["task_id"],),
                    ).fetchone()["state_revision"]
                )
                self._event(
                    connection,
                    "node.indeterminate",
                    row["task_id"],
                    row["node_id"],
                    {"attempt": row["attempt"], "reason": "coordinator_restart"},
                )
                self._create_indeterminate_approval(
                    connection,
                    row["task_id"],
                    row["node_id"],
                    int(row["attempt"]),
                    task_revision,
                    result.summary,
                )
            return len(rows)

    def write_quota(self, snapshot: QuotaSnapshot) -> None:
        snapshot.validate()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO quota_snapshots(provider, snapshot_json, observed_at)
                VALUES('claude', ?, ?)
                """,
                (canonical_json(asdict(snapshot)), snapshot.observed_at),
            )
            self._event(
                connection,
                "quota.updated",
                None,
                None,
                {"provider": "claude", "snapshot": asdict(snapshot)},
            )

    def latest_quota(self) -> QuotaSnapshot | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT snapshot_json FROM quota_snapshots
                WHERE provider = 'claude' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            return QuotaSnapshot(**json.loads(row["snapshot_json"])) if row else None

    def list_quota_snapshots(self, limit: int = 5000) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json FROM quota_snapshots
                WHERE provider = 'claude' ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [json.loads(row["snapshot_json"]) for row in rows]

    def health(self) -> dict[str, Any]:
        with self.connection() as connection:
            schema = int(
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()["value"]
            )
            cursor = int(
                connection.execute("SELECT COALESCE(MAX(cursor), 0) AS cursor FROM events").fetchone()[
                    "cursor"
                ]
            )
            counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state"
                ).fetchall()
            }
            active_executors = {
                row["executor"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT effective_executor AS executor, COUNT(*) AS count
                    FROM nodes WHERE state = 'running'
                    GROUP BY effective_executor
                    """
                ).fetchall()
                if row["executor"]
            }
            active_models = {
                row["model"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT effective_model AS model, COUNT(*) AS count
                    FROM nodes WHERE state = 'running'
                    GROUP BY effective_model
                    """
                ).fetchall()
                if row["model"]
            }
            worktree_counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM worktree_allocations GROUP BY state"
                ).fetchall()
            }
            archive_counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM worktree_archives GROUP BY state"
                ).fetchall()
            }
        authority = self.authority_status()
        with self.connection() as connection:
            lifecycle = connection.execute(
                "SELECT event_type, payload_json, created_at FROM events "
                "WHERE event_type IN ('coordinator.started','coordinator.stopped','coordinator.failed') "
                "ORDER BY cursor DESC LIMIT 1"
            ).fetchone()
        coordinator_failure = (
            {**json.loads(lifecycle["payload_json"]), "observed_at": lifecycle["created_at"]}
            if lifecycle is not None and lifecycle["event_type"] == "coordinator.failed"
            else None
        )
        return {
            "ok": coordinator_failure is None,
            "schema_version": schema,
            "cursor": cursor,
            "task_counts": counts,
            "active_executors": active_executors,
            "active_models": active_models,
            "worktree_counts": worktree_counts,
            "worktree_archive_counts": archive_counts,
            "home_presence": self.active_home_presence(),
            "authority": authority,
            "coordinator_failure": coordinator_failure,
        }

    def stale_tasks(self, max_age_seconds: int = 300) -> list[dict[str, str]]:
        cutoff = datetime.now(UTC).timestamp() - max_age_seconds
        active = {"planning", "ready", "queued", "running", "verifying", "needs_fix", "needs_approval"}
        return [
            {"task_id": task["task_id"], "state": task["state"], "updated_at": task["updated_at"]}
            for task in self.list_tasks()
            if task["state"] in active and datetime.fromisoformat(task["updated_at"]).timestamp() < cutoff
        ]
