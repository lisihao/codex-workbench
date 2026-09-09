from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
import re
from pathlib import Path
import sqlite3
import subprocess
import threading
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

from .model import (
    DEFAULT_QUOTA_TTL_SECONDS,
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
from .delivery_lifecycle import (
    DeliveryAdmissionBusy,
    DELIVERY_RECEIPT_STATES,
    DELIVERY_STAGES,
    IDENTITY_FIELDS,
    required_completion_identities,
    required_delivery_stages,
    merge_identities,
    normalize_budget,
    normalize_identities,
    normalize_objective_request,
    normalize_stage,
    normalize_timestamp,
    normalize_wait_reason,
    recovery_action_for_failure,
    validate_live_verification_receipt,
)
from .dirty_worktree_recovery import (
    DirtyWorktreeRecovery,
    DirtyWorktreeRecoveryError,
    partition_recovery_paths,
)
from .execution_attribution import ExecutionAttribution
from .governance import governance_identity
from .legacy_evidence import load_manifest, validate_manifest
from .planner import propose_archify_reconciliation
from .scheduler_metrics import (
    EXECUTION_LANES,
    execution_lane_for_spec,
    quota_pool_id_for_spec,
)
from .scope_isolation import scope_entity_alias_conflicts
from .worktrees import (
    WorktreeManager,
    normalize_scope,
    scope_access_conflicts,
    scope_allows,
    scopes_overlap,
)


SCHEMA_VERSION = 13
_ARCHIFY_RENDER_COMMANDS = frozenset({"deliver", "compare", "visual-check"})
_ARCHIFY_RECEIPT_ONLY_COMMANDS = frozenset({"validate", "migrate"})
_DELIVERY_LEASE_SECONDS = 60 * 60

_DIRTY_WORKTREE_RECOVERY_KIND = "blocked-worktree-recovery"
_DIRTY_WORKTREE_RECOVERY_PROVIDER = "workbench-dirty-worktree-recovery"
_FAILED_ATTEMPT_RECOVERY_KIND = "failed-attempt-worktree-recovery"
_FAILED_ATTEMPT_RECOVERY_PROVIDER = "workbench-failed-attempt-recovery"

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

    @contextmanager
    def _schema_migration_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> Iterator[None]:
        """Keep schema DDL and its version marker in one SQLite transaction."""

        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _schema_write(
        connection: sqlite3.Connection,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Cursor:
        """Execute one schema mutation; tests may fault this durable boundary."""

        return connection.execute(statement, parameters)

    @staticmethod
    def _preflight_schema_version(connection: sqlite3.Connection) -> int | None:
        """Read and validate the current schema without issuing DDL."""

        metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
        ).fetchone()
        if metadata is None:
            return None
        current = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if current is None:
            return None
        raw_version = current["value"]
        try:
            schema_version = int(raw_version)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"unsupported schema version {raw_version}; expected {SCHEMA_VERSION}"
            ) from error
        if schema_version not in range(1, SCHEMA_VERSION + 1):
            raise RuntimeError(
                f"unsupported schema version {raw_version}; expected {SCHEMA_VERSION}"
            )
        return schema_version

    def _apply_schema_migration(
        self,
        connection: sqlite3.Connection,
        prior_schema_version: int | None,
    ) -> None:
        """Apply metadata and legacy-column migration after base DDL exists."""

        if prior_schema_version is None:
            self._schema_write(
                connection,
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif prior_schema_version < SCHEMA_VERSION:
            node_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
            }
            if "effective_executor" not in node_columns:
                self._schema_write(connection, "ALTER TABLE nodes ADD COLUMN effective_executor TEXT")
            if "effective_model" not in node_columns:
                self._schema_write(connection, "ALTER TABLE nodes ADD COLUMN effective_model TEXT")
            if "coordinator_epoch" not in node_columns:
                self._schema_write(
                    connection,
                    "ALTER TABLE nodes ADD COLUMN coordinator_epoch INTEGER NOT NULL DEFAULT 0",
                )
            if "lease_epoch" not in node_columns:
                self._schema_write(
                    connection,
                    "ALTER TABLE nodes ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0",
                )
            if "recovery_json" not in node_columns:
                self._schema_write(connection, "ALTER TABLE nodes ADD COLUMN recovery_json TEXT")
            task_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "priority" not in task_columns:
                self._schema_write(
                    connection,
                    "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
                )
            steering_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(task_steering)").fetchall()
            }
            if "sequence" not in steering_columns:
                self._schema_write(
                    connection,
                    "ALTER TABLE task_steering ADD COLUMN sequence INTEGER",
                )
            # v9 had no explicit sequence. Its durable semantics were
            # chronological steering with the stable steering ID as the
            # tie-breaker; rowid reflected insertion/storage order only and
            # can be reversed by import/rebuild paths.
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
                    self._schema_write(
                        connection,
                        "UPDATE task_steering SET sequence = ? WHERE rowid = ?",
                        (stored_sequence, steering_row["rowid"]),
                    )
                next_sequences[task_id] = int(stored_sequence)
            self._schema_write(
                connection,
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            # v8 binds reusable Evidence to the code-as-harness governance
            # receipt; only rows that actually predate v8 lack that proof.
            if prior_schema_version < 8:
                self._schema_write(connection, "DELETE FROM evidence_cache")
        self._schema_write(
            connection,
            "CREATE INDEX IF NOT EXISTS task_steering_task_sequence_idx "
            "ON task_steering(task_id, sequence)",
        )
        # Schema v13 is already the public compatibility fence.  The
        # lifecycle tables above are additive, idempotent DDL in the same
        # transaction; an older v13 binary ignores this marker and its tables,
        # which keeps a rollback read/write compatible instead of requiring a
        # destructive down-migration.
        self._schema_write(
            connection,
            "INSERT INTO metadata(key, value) VALUES('delivery_lifecycle_schema_version', '1') "
            "ON CONFLICT(key) DO NOTHING",
        )

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._init_lock, self.connection() as connection:
            schema_sql = """
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
                CREATE TABLE IF NOT EXISTS planning_requests (
                    command_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    coordinator_epoch INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    started_at TEXT,
                    settled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                -- The lifecycle overlay deliberately does not foreign-key
                -- task_id: a planning reservation owns its task ID before
                -- materialization creates the immutable task contract.
                CREATE TABLE IF NOT EXISTS delivery_objectives (
                    objective_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    stage_attempt INTEGER NOT NULL,
                    state_revision INTEGER NOT NULL,
                    next_action_json TEXT NOT NULL,
                    owner_id TEXT,
                    coordinator_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    due_at TEXT NOT NULL,
                    next_wakeup_at TEXT,
                    last_material_progress_at TEXT NOT NULL,
                    last_progress_json TEXT NOT NULL,
                    wait_reason_json TEXT,
                    budget_json TEXT NOT NULL,
                    evidence_fingerprints_json TEXT NOT NULL,
                    identities_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_stage_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    objective_id TEXT NOT NULL REFERENCES delivery_objectives(objective_id)
                        ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    stage_attempt INTEGER NOT NULL,
                    receipt_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    evidence_fingerprint TEXT,
                    identities_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(objective_id, stage, stage_attempt, receipt_hash)
                );
                -- Intent is durable before an authority-owned adapter can
                -- cause an external effect.  A restart therefore reconciles
                -- or freezes an uncertain dispatch instead of replaying it.
                CREATE TABLE IF NOT EXISTS delivery_stage_dispatches (
                    dispatch_id TEXT PRIMARY KEY,
                    objective_id TEXT NOT NULL REFERENCES delivery_objectives(objective_id)
                        ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    stage_attempt INTEGER NOT NULL,
                    request_hash TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt_id TEXT,
                    rollback_state TEXT,
                    rollback_receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(objective_id, stage, stage_attempt)
                );
                CREATE TABLE IF NOT EXISTS delivery_authorization_receipts (
                    authorization_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    objective_id TEXT REFERENCES delivery_objectives(objective_id),
                    request_hash TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    authority_json TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    granted_by TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_admission_waits (
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    wait_fingerprint TEXT NOT NULL,
                    reason_kind TEXT NOT NULL,
                    reason_json TEXT NOT NULL,
                    next_action_json TEXT NOT NULL,
                    blocking_json TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    next_wakeup_at TEXT NOT NULL,
                    event_cursor INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, node_id),
                    FOREIGN KEY(task_id, node_id) REFERENCES nodes(task_id, node_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS delivery_leases (
                    command_id TEXT PRIMARY KEY REFERENCES delivery_receipts(command_id)
                        ON DELETE CASCADE,
                    fence TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
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
                CREATE UNIQUE INDEX IF NOT EXISTS planning_requests_task_id_unique_idx
                    ON planning_requests(task_id);
                CREATE INDEX IF NOT EXISTS planning_requests_state_idx
                    ON planning_requests(state, created_at, command_id);
                CREATE INDEX IF NOT EXISTS tasks_state_updated_idx ON tasks(state, updated_at);
                CREATE INDEX IF NOT EXISTS task_steering_task_created_idx
                    ON task_steering(task_id, created_at);
                CREATE INDEX IF NOT EXISTS delivery_objectives_wakeup_idx
                    ON delivery_objectives(state, next_wakeup_at, due_at);
                CREATE INDEX IF NOT EXISTS delivery_stage_receipts_objective_idx
                    ON delivery_stage_receipts(objective_id, stage, stage_attempt);
                CREATE INDEX IF NOT EXISTS delivery_stage_dispatches_objective_idx
                    ON delivery_stage_dispatches(objective_id, stage, stage_attempt, state);
                CREATE INDEX IF NOT EXISTS delivery_authorization_task_idx
                    ON delivery_authorization_receipts(task_id, decision, expires_at);
                CREATE INDEX IF NOT EXISTS node_admission_waits_wakeup_idx
                    ON node_admission_waits(next_wakeup_at, due_at);
                CREATE INDEX IF NOT EXISTS context_import_thread_created_idx
                    ON context_import_receipts(source_thread_id, created_at);
                CREATE INDEX IF NOT EXISTS worktree_allocations_state_idx
                    ON worktree_allocations(state, updated_at);
                CREATE INDEX IF NOT EXISTS worktree_archives_state_idx
                    ON worktree_archives(state, updated_at);
                """
            # Read-only version fencing precedes every DDL statement. An
            # unknown newer database must remain untouched by an older binary.
            prior_schema_version = self._preflight_schema_version(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            with self._schema_migration_transaction(connection):
                for statement in schema_sql.split(";"):
                    if statement.strip():
                        self._schema_write(connection, statement)
                self._apply_schema_migration(connection, prior_schema_version)
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
            authorization: dict[str, Any] | None = None
            if not contract.get("external_write_permission", False):
                authorization = self._matching_delivery_authorization(
                    connection,
                    task_id,
                    request,
                    timestamp=timestamp,
                )
                if authorization is None:
                    raise StateConflictError(
                        f"task {task_id} contract does not authorize external GitHub writes"
                    )
            details = {
                "request": request,
                **(
                    {"delivery_authorization_id": authorization["authorization_id"]}
                    if authorization is not None
                    else {}
                ),
            }
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
                {
                    "command_id": command_id,
                    "request_hash": request_hash,
                    **(
                        {"delivery_authorization_id": authorization["authorization_id"]}
                        if authorization is not None
                        else {}
                    ),
                },
            )
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            assert row is not None
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
    def _delivery_stage_receipt_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "receipt_id": str(row["receipt_id"]),
            "objective_id": str(row["objective_id"]),
            "task_id": str(row["task_id"]),
            "stage": str(row["stage"]),
            "attempt": int(row["stage_attempt"]),
            "request_hash": str(row["receipt_hash"]),
            "state": str(row["state"]),
            "receipt": json.loads(str(row["receipt_json"])),
            "evidence_fingerprint": row["evidence_fingerprint"],
            "identities": json.loads(str(row["identities_json"])),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _delivery_stage_dispatch_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "dispatch_id": str(row["dispatch_id"]),
            "objective_id": str(row["objective_id"]),
            "task_id": str(row["task_id"]),
            "stage": str(row["stage"]),
            "attempt": int(row["stage_attempt"]),
            "request_hash": str(row["request_hash"]),
            "adapter_name": str(row["adapter_name"]),
            "state": str(row["state"]),
            "receipt_id": row["receipt_id"],
            "rollback": (
                {
                    "state": str(row["rollback_state"]),
                    "receipt": (
                        json.loads(str(row["rollback_receipt_json"]))
                        if row["rollback_receipt_json"] is not None
                        else None
                    ),
                }
                if row["rollback_state"] is not None
                else None
            ),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _delivery_authorization_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "authorization_id": str(row["authorization_id"]),
            "task_id": str(row["task_id"]),
            "objective_id": row["objective_id"],
            "request_hash": str(row["request_hash"]),
            "scope": json.loads(str(row["scope_json"])),
            "authority": json.loads(str(row["authority_json"])),
            "decision": str(row["decision"]),
            "granted_by": str(row["granted_by"]),
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _delivery_objective_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_receipts: bool = True,
        include_authorizations: bool = True,
    ) -> dict[str, Any]:
        request = json.loads(str(row["request_json"]))
        result: dict[str, Any] = {
            "objective_id": str(row["objective_id"]),
            "command_id": str(row["command_id"]),
            "task_id": str(row["task_id"]),
            "request_hash": str(row["request_hash"]),
            "requested_endpoints": request["requested_endpoints"],
            "scope": request["scope"],
            "authority": request["authority"],
            "request_metadata": request.get("metadata", {}),
            "required_stages": list(required_delivery_stages(request)),
            "required_identities": list(required_completion_identities(request)),
            "state": str(row["state"]),
            "stage": str(row["stage"]),
            "stage_attempt": int(row["stage_attempt"]),
            "state_revision": int(row["state_revision"]),
            "next_action": json.loads(str(row["next_action_json"])),
            "lease": {
                "owner_id": row["owner_id"],
                "coordinator_epoch": int(row["coordinator_epoch"]),
                "lease_epoch": int(row["lease_epoch"]),
                "expires_at": row["lease_expires_at"],
            },
            "due_at": str(row["due_at"]),
            "next_wakeup_at": row["next_wakeup_at"],
            "last_material_progress_at": str(row["last_material_progress_at"]),
            "last_progress": json.loads(str(row["last_progress_json"])),
            "wait_reason": (
                json.loads(str(row["wait_reason_json"]))
                if row["wait_reason_json"] is not None
                else None
            ),
            "budget": json.loads(str(row["budget_json"])),
            "evidence_fingerprints": json.loads(str(row["evidence_fingerprints_json"])),
            "identities": json.loads(str(row["identities_json"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        if include_receipts:
            receipts = connection.execute(
                """
                SELECT * FROM delivery_stage_receipts
                WHERE objective_id = ?
                ORDER BY created_at, receipt_id
                """,
                (row["objective_id"],),
            ).fetchall()
            result["stage_receipts"] = [
                WorkbenchStore._delivery_stage_receipt_row(receipt) for receipt in receipts
            ]
            dispatches = connection.execute(
                """
                SELECT * FROM delivery_stage_dispatches
                WHERE objective_id = ?
                ORDER BY created_at, dispatch_id
                """,
                (row["objective_id"],),
            ).fetchall()
            result["stage_dispatches"] = [
                WorkbenchStore._delivery_stage_dispatch_row(dispatch) for dispatch in dispatches
            ]
        if include_authorizations:
            authorizations = connection.execute(
                """
                SELECT * FROM delivery_authorization_receipts
                WHERE task_id = ?
                ORDER BY created_at, authorization_id
                """,
                (row["task_id"],),
            ).fetchall()
            result["delivery_authorizations"] = [
                WorkbenchStore._delivery_authorization_row(authorization)
                for authorization in authorizations
            ]
        return result

    @staticmethod
    def _node_admission_wait_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reason_kind": str(row["reason_kind"]),
            "reason": json.loads(str(row["reason_json"])),
            "next_action": json.loads(str(row["next_action_json"])),
            "blocking": json.loads(str(row["blocking_json"])),
            "due_at": str(row["due_at"]),
            "next_wakeup_at": str(row["next_wakeup_at"]),
            "event_cursor": int(row["event_cursor"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _next_delivery_lease_epoch(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'delivery_objective_lease_epoch'"
        ).fetchone()
        epoch = (int(row["value"]) if row else 0) + 1
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('delivery_objective_lease_epoch', ?)",
            (str(epoch),),
        )
        return epoch

    @staticmethod
    def _timestamp_after(timestamp: str, seconds: int) -> str:
        return (datetime.fromisoformat(timestamp) + timedelta(seconds=seconds)).isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _timestamp_is_due(value: str | None, timestamp: str) -> bool:
        return value is None or datetime.fromisoformat(value) <= datetime.fromisoformat(timestamp)

    def _assert_delivery_task_or_reservation(
        self,
        connection: sqlite3.Connection,
        task_id: str,
    ) -> None:
        task = connection.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task is not None:
            return
        reservation = connection.execute(
            "SELECT 1 FROM planning_requests WHERE task_id = ?", (task_id,)
        ).fetchone()
        if reservation is None:
            raise KeyError(task_id)

    def create_delivery_objective(
        self,
        task_id: str,
        command_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable, end-to-end objective beside its task ID.

        The task may still be a planning reservation.  This makes the plan
        stage restart-safe without creating a shadow implementation task.
        """

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("delivery objective task_id is required")
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("delivery objective command_id is required")
        normalized = normalize_objective_request(request)
        request_hash = canonical_hash({"task_id": task_id, "request": normalized})
        timestamp = now_iso()
        due_at = normalized["due_at"] or self._timestamp_after(
            timestamp, int(normalized["budget"]["time_budget_seconds"])
        )
        next_wakeup_at = normalized["next_wakeup_at"] or timestamp
        objective_id = "delivery-objective-" + canonical_hash(
            {"task_id": task_id, "request_hash": request_hash}
        )[:24]
        progress = {
            "kind": "delivery-objective-created",
            "stage": "plan",
            "next_action": normalized["next_action"],
        }
        with self.transaction() as connection:
            self._assert_delivery_task_or_reservation(connection, task_id)
            existing_command = connection.execute(
                "SELECT * FROM delivery_objectives WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing_command is not None:
                if (
                    existing_command["task_id"] != task_id
                    or existing_command["request_hash"] != request_hash
                ):
                    raise CommandConflictError(
                        f"delivery objective command {command_id!r} was already used with a different request"
                    )
                return self._delivery_objective_row(connection, existing_command)
            existing = connection.execute(
                "SELECT * FROM delivery_objectives WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise StateConflictError(
                        f"task {task_id!r} already has an immutable delivery objective"
                    )
                return self._delivery_objective_row(connection, existing)
            unavailable = connection.execute(
                """
                SELECT a.allocation_id FROM worktree_allocations a
                JOIN nodes n ON n.task_id = a.task_id AND n.node_id = a.node_id
                    AND n.attempt = a.attempt
                WHERE a.task_id = ? AND json_extract(n.spec_json, '$.verifier') = 1
                    AND a.state != 'active'
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if unavailable is not None:
                raise StateConflictError("restore the verifier worktree before creating a delivery objective")
            connection.execute(
                """
                INSERT INTO delivery_objectives(
                    objective_id, command_id, task_id, request_hash, request_json,
                    state, stage, stage_attempt, state_revision, next_action_json,
                    due_at, next_wakeup_at, last_material_progress_at,
                    last_progress_json, budget_json, evidence_fingerprints_json,
                    identities_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'active', 'plan', 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    objective_id,
                    command_id,
                    task_id,
                    request_hash,
                    canonical_json(normalized),
                    canonical_json(normalized["next_action"]),
                    due_at,
                    next_wakeup_at,
                    timestamp,
                    canonical_json(progress),
                    canonical_json(normalized["budget"]),
                    canonical_json(normalized["evidence_fingerprints"]),
                    canonical_json(normalized["identities"]),
                    timestamp,
                    timestamp,
                ),
            )
            cursor = self._event(
                connection,
                "delivery_objective.created",
                task_id,
                None,
                {
                    "objective_id": objective_id,
                    "command_id": command_id,
                    "request_hash": request_hash,
                    "stage": "plan",
                    "stage_attempt": 1,
                    "state_revision": 1,
                    "next_wakeup_at": next_wakeup_at,
                },
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE delivery_objectives SET last_progress_json = ?
                WHERE objective_id = ?
                """,
                (canonical_json({**progress, "event_cursor": cursor}), objective_id),
            )
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            assert row is not None
            return self._delivery_objective_row(connection, row)

    def get_delivery_objective(self, objective_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if row is None:
                raise KeyError(objective_id)
            return self._delivery_objective_row(connection, row)

    def get_delivery_objective_for_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._delivery_objective_row(connection, row) if row is not None else None

    def list_due_delivery_objectives(
        self,
        *,
        now: str | None = None,
        limit: int = 100,
        owner_id: str | None = None,
        coordinator_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("delivery objective limit must be positive")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ValueError("delivery objective owner_id must be a non-empty string or null")
        if coordinator_epoch is not None and (
            isinstance(coordinator_epoch, bool) or not isinstance(coordinator_epoch, int) or coordinator_epoch <= 0
        ):
            raise ValueError("delivery objective coordinator_epoch must be positive or null")
        if (owner_id is None) != (coordinator_epoch is None):
            raise ValueError("delivery objective owner_id and coordinator_epoch must be supplied together")
        timestamp = normalize_timestamp(now, "now") if now is not None else now_iso()
        assert timestamp is not None
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_objectives
                WHERE state IN ('active', 'waiting')
                  AND next_wakeup_at IS NOT NULL AND next_wakeup_at <= ?
                  AND (
                      state != 'active' OR owner_id IS NULL OR lease_expires_at IS NULL
                      OR lease_expires_at <= ?
                      OR (owner_id = ? AND coordinator_epoch = ?)
                  )
                ORDER BY next_wakeup_at, due_at, objective_id
                LIMIT ?
                """,
                (timestamp, timestamp, owner_id, coordinator_epoch, limit),
            ).fetchall()
            return [
                self._delivery_objective_row(connection, row, include_receipts=False)
                for row in rows
            ]

    def claim_delivery_objective(
        self,
        objective_id: str,
        owner_id: str,
        coordinator_epoch: int,
        *,
        expected_revision: int,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("delivery objective owner_id is required")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("delivery objective expected_revision must be positive")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("delivery objective lease_seconds must be between 1 and 3600")
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            self._reconcile_delivery_safe_point_waits(connection, timestamp=timestamp)
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if row is None:
                raise KeyError(objective_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected delivery objective revision {expected_revision}, found {row['state_revision']}"
                )
            if row["state"] in {"complete", "cancelled", "needs_decision"}:
                return None
            if row["state"] == "waiting" and row["wait_reason_json"] is not None:
                try:
                    wait_reason = json.loads(str(row["wait_reason_json"]))
                except json.JSONDecodeError as error:
                    raise StateConflictError("delivery objective wait reason is invalid") from error
                if (
                    isinstance(wait_reason, dict)
                    and wait_reason.get("kind") == "active-workers-draining"
                    and wait_reason.get("safe_point_ready") is not True
                ):
                    # The bounded wake is a reconciliation cadence, not
                    # permission to interrupt a still-running worker.
                    return None
            if not self._timestamp_is_due(row["next_wakeup_at"], timestamp):
                return None
            if (
                row["state"] == "active"
                and row["owner_id"] is not None
                and row["lease_expires_at"] is not None
                and not self._timestamp_is_due(row["lease_expires_at"], timestamp)
            ):
                if (
                    row["owner_id"] == owner_id
                    and int(row["coordinator_epoch"]) == coordinator_epoch
                ):
                    return self._delivery_objective_row(connection, row)
                raise StateConflictError("delivery objective is leased by another owner")
            lease_epoch = self._next_delivery_lease_epoch(connection)
            revision = int(row["state_revision"]) + 1
            expires_at = self._timestamp_after(timestamp, lease_seconds)
            changed = connection.execute(
                """
                UPDATE delivery_objectives
                SET state = 'active', state_revision = ?, owner_id = ?, coordinator_epoch = ?,
                    lease_epoch = ?, lease_expires_at = ?, next_wakeup_at = ?,
                    wait_reason_json = NULL, updated_at = ?
                WHERE objective_id = ? AND state_revision = ?
                """,
                (
                    revision,
                    owner_id,
                    coordinator_epoch,
                    lease_epoch,
                    expires_at,
                    timestamp,
                    timestamp,
                    objective_id,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("delivery objective claim compare-and-set failed")
            self._event(
                connection,
                "delivery_objective.claimed",
                str(row["task_id"]),
                None,
                {
                    "objective_id": objective_id,
                    "owner_id": owner_id,
                    "coordinator_epoch": coordinator_epoch,
                    "lease_epoch": lease_epoch,
                    "state_revision": revision,
                    "stage": row["stage"],
                    "stage_attempt": int(row["stage_attempt"]),
                    "lease_expires_at": expires_at,
                },
                created_at=timestamp,
            )
            claimed = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            assert claimed is not None
            return self._delivery_objective_row(connection, claimed)

    @staticmethod
    def _assert_delivery_objective_lease(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        coordinator_epoch: int,
        lease_epoch: int,
        timestamp: str,
    ) -> None:
        WorkbenchStore._assert_active_coordinator(connection, coordinator_epoch)
        if (
            row["state"] != "active"
            or int(row["coordinator_epoch"]) != coordinator_epoch
            or int(row["lease_epoch"]) != lease_epoch
            or row["owner_id"] is None
            or row["lease_expires_at"] is None
            or WorkbenchStore._timestamp_is_due(str(row["lease_expires_at"]), timestamp)
        ):
            raise StateConflictError("delivery objective lease is stale")

    def renew_delivery_objective_lease(
        self,
        objective_id: str,
        *,
        coordinator_epoch: int,
        lease_epoch: int,
        expected_revision: int,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("delivery objective expected_revision must be positive")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("delivery objective lease_seconds must be between 1 and 3600")
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if row is None:
                raise KeyError(objective_id)
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected delivery objective revision {expected_revision}, found {row['state_revision']}"
                )
            self._assert_delivery_objective_lease(
                connection,
                row,
                coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch,
                timestamp=timestamp,
            )
            expires_at = self._timestamp_after(timestamp, lease_seconds)
            revision = expected_revision + 1
            changed = connection.execute(
                """
                UPDATE delivery_objectives
                SET state_revision = ?, lease_expires_at = ?, updated_at = ?
                WHERE objective_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (revision, expires_at, timestamp, objective_id, expected_revision, lease_epoch),
            ).rowcount
            if changed != 1:
                raise StateConflictError("delivery objective renewal compare-and-set failed")
            self._event(
                connection,
                "delivery_objective.lease_renewed",
                str(row["task_id"]),
                None,
                {
                    "objective_id": objective_id,
                    "coordinator_epoch": coordinator_epoch,
                    "lease_epoch": lease_epoch,
                    "state_revision": revision,
                    "lease_expires_at": expires_at,
                },
                created_at=timestamp,
            )
            renewed = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            assert renewed is not None
            return self._delivery_objective_row(connection, renewed)

    def get_delivery_stage_dispatch(self, objective_id: str, stage: str, attempt: int) -> dict[str, Any] | None:
        """Read prior intent without creating a newly authorized side effect."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_stage_dispatches WHERE objective_id = ? AND stage = ? AND stage_attempt = ?",
                (objective_id, stage, attempt),
            ).fetchone()
            return self._delivery_stage_dispatch_row(row) if row is not None else None

    def begin_delivery_stage_dispatch(
        self,
        objective_id: str,
        *,
        stage: str,
        attempt: int,
        expected_revision: int,
        coordinator_epoch: int,
        lease_epoch: int,
        adapter_name: str,
    ) -> dict[str, Any]:
        """Persist external-stage intent before invoking an adapter.

        The deterministic dispatch ID is the adapter's idempotency key.  If a
        process dies after intent but before a receipt, callers receive the
        old dispatch and must reconcile it; they never receive permission to
        invoke the effect again merely because a lease expired.
        """

        normalized_stage = normalize_stage(stage)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("delivery stage dispatch attempt must be positive")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("delivery stage dispatch expected_revision must be positive")
        if not isinstance(adapter_name, str) or not adapter_name.strip():
            raise ValueError("delivery stage dispatch adapter_name is required")
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if row is None:
                raise KeyError(objective_id)
            self._assert_delivery_objective_lease(
                connection,
                row,
                coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch,
                timestamp=timestamp,
            )
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected delivery objective revision {expected_revision}, found {row['state_revision']}"
                )
            request = json.loads(str(row["request_json"]))
            required_stages = required_delivery_stages(request)
            if normalized_stage not in required_stages:
                raise StateConflictError("delivery stage was not explicitly requested")
            if row["stage"] != normalized_stage or int(row["stage_attempt"]) != attempt:
                raise StateConflictError("delivery stage dispatch is stale for the current stage attempt")
            if normalized_stage == "deploy":
                gate = self._deployment_admission_gate(connection)
                if ((gate is not None and gate["objective_id"] != objective_id)
                        or self._delivery_deployment_blockers_for_objective(connection, row)):
                    raise DeliveryAdmissionBusy("authority work is still active")
            request_hash = canonical_hash(
                {
                    "objective_id": objective_id,
                    "objective_request_hash": row["request_hash"],
                    "stage": normalized_stage,
                    "attempt": attempt,
                }
            )
            dispatch_id = "delivery-dispatch-" + request_hash[:24]
            existing = connection.execute(
                """
                SELECT * FROM delivery_stage_dispatches
                WHERE objective_id = ? AND stage = ? AND stage_attempt = ?
                """,
                (objective_id, normalized_stage, attempt),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash or existing["dispatch_id"] != dispatch_id:
                    raise StateConflictError("delivery stage dispatch identity conflicts with prior intent")
                return {**self._delivery_stage_dispatch_row(existing), "new": False}
            connection.execute(
                """
                INSERT INTO delivery_stage_dispatches(
                    dispatch_id, objective_id, task_id, stage, stage_attempt,
                    request_hash, adapter_name, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'started', ?, ?)
                """,
                (
                    dispatch_id,
                    objective_id,
                    row["task_id"],
                    normalized_stage,
                    attempt,
                    request_hash,
                    adapter_name.strip(),
                    timestamp,
                    timestamp,
                ),
            )
            cursor = self._event(
                connection,
                "delivery_objective.stage_dispatched",
                str(row["task_id"]),
                None,
                {
                    "objective_id": objective_id,
                    "dispatch_id": dispatch_id,
                    "stage": normalized_stage,
                    "attempt": attempt,
                    "adapter_name": adapter_name.strip(),
                },
                created_at=timestamp,
            )
            connection.execute(
                """
                UPDATE delivery_objectives
                SET last_material_progress_at = ?, last_progress_json = ?, updated_at = ?
                WHERE objective_id = ?
                """,
                (
                    timestamp,
                    canonical_json(
                        {
                            "kind": "delivery_objective.stage_dispatched",
                            "event_cursor": cursor,
                            "dispatch_id": dispatch_id,
                            "stage": normalized_stage,
                            "attempt": attempt,
                        }
                    ),
                    timestamp,
                    objective_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM delivery_stage_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            assert stored is not None
            return {**self._delivery_stage_dispatch_row(stored), "new": True}

    def begin_delivery_stage_rollback(
        self,
        dispatch_id: str,
        *,
        coordinator_epoch: int,
        lease_epoch: int,
    ) -> dict[str, Any]:
        """Fence one preauthorized reversible rollback beneath its deploy intent."""

        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise ValueError("delivery rollback dispatch_id is required")
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT d.*, o.state AS objective_state, o.coordinator_epoch AS objective_epoch,
                       o.lease_epoch AS objective_lease_epoch, o.owner_id, o.lease_expires_at
                FROM delivery_stage_dispatches d
                JOIN delivery_objectives o USING(objective_id)
                WHERE d.dispatch_id = ?
                """,
                (dispatch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(dispatch_id)
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (row["objective_id"],)
            ).fetchone()
            assert objective is not None
            self._assert_delivery_objective_lease(
                connection,
                objective,
                coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch,
                timestamp=timestamp,
            )
            if row["stage"] != "deploy":
                raise StateConflictError("only a deployment dispatch can roll back")
            if row["rollback_state"] is None:
                connection.execute(
                    """
                    UPDATE delivery_stage_dispatches
                    SET rollback_state = 'started', updated_at = ?
                    WHERE dispatch_id = ? AND rollback_state IS NULL
                    """,
                    (timestamp, dispatch_id),
                )
                self._event(
                    connection,
                    "delivery_objective.rollback_dispatched",
                    str(row["task_id"]),
                    None,
                    {"objective_id": row["objective_id"], "dispatch_id": dispatch_id},
                    created_at=timestamp,
                )
                current = connection.execute(
                    "SELECT * FROM delivery_stage_dispatches WHERE dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                assert current is not None
                return {**self._delivery_stage_dispatch_row(current), "new": True}
            return {**self._delivery_stage_dispatch_row(row), "new": False}

    def settle_delivery_stage_rollback(
        self,
        dispatch_id: str,
        *,
        coordinator_epoch: int,
        lease_epoch: int,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist only an actually verified rollback receipt."""

        if not isinstance(receipt, dict) or receipt.get("verified") is not True:
            raise ValueError("delivery rollback receipt must explicitly verify rollback")
        try:
            json.dumps(receipt, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("delivery rollback receipt must be JSON-safe") from error
        timestamp = now_iso()
        with self.transaction() as connection:
            dispatch = connection.execute(
                "SELECT * FROM delivery_stage_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if dispatch is None:
                raise KeyError(dispatch_id)
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (dispatch["objective_id"],)
            ).fetchone()
            assert objective is not None
            self._assert_delivery_objective_lease(
                connection,
                objective,
                coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch,
                timestamp=timestamp,
            )
            if dispatch["rollback_state"] == "settled":
                existing = json.loads(str(dispatch["rollback_receipt_json"]))
                if existing != receipt:
                    raise StateConflictError("delivery rollback receipt conflicts with prior verified rollback")
                return self._delivery_stage_dispatch_row(dispatch)
            if dispatch["rollback_state"] != "started":
                raise StateConflictError("delivery rollback was not durably dispatched")
            changed = connection.execute(
                """
                UPDATE delivery_stage_dispatches
                SET rollback_state = 'settled', rollback_receipt_json = ?, updated_at = ?
                WHERE dispatch_id = ? AND rollback_state = 'started'
                """,
                (canonical_json(receipt), timestamp, dispatch_id),
            ).rowcount
            if changed != 1:
                raise StateConflictError("delivery rollback receipt compare-and-set failed")
            self._event(
                connection,
                "delivery_objective.rollback_verified",
                str(dispatch["task_id"]),
                None,
                {
                    "objective_id": dispatch["objective_id"],
                    "dispatch_id": dispatch_id,
                    "receipt": receipt,
                },
                created_at=timestamp,
            )
            stored = connection.execute(
                "SELECT * FROM delivery_stage_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            assert stored is not None
            return self._delivery_stage_dispatch_row(stored)

    def record_delivery_stage_receipt(
        self,
        objective_id: str,
        receipt_id: str,
        *,
        stage: str,
        attempt: int,
        expected_revision: int,
        coordinator_epoch: int,
        lease_epoch: int,
        state: str | None = None,
        status: str | None = None,
        receipt: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        evidence_fingerprint: str | None = None,
        identities: dict[str, Any] | None = None,
        failure: dict[str, Any] | str | None = None,
        retry_eligible: bool = False,
        cost_delta: float = 0.0,
        next_wakeup_at: str | None = None,
        dispatch_id: str | None = None,
    ) -> dict[str, Any]:
        """Fence one stage receipt and advance only the current stage attempt.

        A late webhook cannot pass the stage/attempt/revision/lease fence.  An
        exact duplicate returns its original receipt without moving a wakeup or
        appending another event.
        """

        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValueError("delivery stage receipt_id is required")
        if dispatch_id is not None and (not isinstance(dispatch_id, str) or not dispatch_id.strip()):
            raise ValueError("delivery stage dispatch_id must be a non-empty string or null")
        normalized_stage = normalize_stage(stage)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("delivery stage attempt must be positive")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("delivery stage expected_revision must be positive")
        if isinstance(retry_eligible, bool) is False:
            raise ValueError("delivery stage retry_eligible must be a boolean")
        if (
            isinstance(cost_delta, bool)
            or not isinstance(cost_delta, (int, float))
            or not math.isfinite(float(cost_delta))
            or cost_delta < 0
        ):
            raise ValueError("delivery stage cost_delta must be a non-negative number")
        if state is not None and status is not None and state != status:
            raise ValueError("delivery stage state and status disagree")
        receipt_state = str(status if status is not None else state if state is not None else "succeeded")
        if receipt_state not in DELIVERY_RECEIPT_STATES:
            raise ValueError(f"unsupported delivery receipt state {receipt_state!r}")
        if receipt is not None and payload is not None and receipt != payload:
            raise ValueError("delivery stage receipt and payload disagree")
        receipt_body = receipt if receipt is not None else payload if payload is not None else {}
        if not isinstance(receipt_body, dict):
            raise ValueError("delivery stage receipt must be an object")
        try:
            json.dumps(receipt_body, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("delivery stage receipt must be JSON-safe") from error
        if evidence_fingerprint is not None and (
            not isinstance(evidence_fingerprint, str) or not evidence_fingerprint.strip()
        ):
            raise ValueError("delivery evidence_fingerprint must be a non-empty string")
        normalized_identities = normalize_identities(identities)
        if receipt_state == "succeeded":
            normalized_failure = None
        else:
            if failure is None:
                raise ValueError("a non-succeeded delivery receipt requires a precise failure reason")
            normalized_failure = normalize_wait_reason(
                failure, default_kind="verification-failure"
            )
        requested_wakeup = normalize_timestamp(next_wakeup_at, "next_wakeup_at")
        receipt_hash = canonical_hash(
            {
                "objective_id": objective_id,
                "stage": normalized_stage,
                "attempt": attempt,
                "state": receipt_state,
                "receipt": receipt_body,
                "evidence_fingerprint": evidence_fingerprint,
                "identities": normalized_identities,
                "failure": normalized_failure,
                "retry_eligible": retry_eligible,
                "cost_delta": float(cost_delta),
                "dispatch_id": dispatch_id,
            }
        )
        timestamp = now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM delivery_stage_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if existing is not None:
                if existing["receipt_hash"] != receipt_hash:
                    raise CommandConflictError(
                        f"delivery stage receipt {receipt_id!r} was already used with different content"
                    )
                objective = connection.execute(
                    "SELECT * FROM delivery_objectives WHERE objective_id = ?",
                    (existing["objective_id"],),
                ).fetchone()
                if objective is None:
                    raise StateConflictError("delivery stage receipt has no objective")
                return {
                    "receipt": self._delivery_stage_receipt_row(existing),
                    "objective": self._delivery_objective_row(connection, objective),
                    "idempotent": True,
                }
            duplicate = connection.execute(
                """
                SELECT * FROM delivery_stage_receipts
                WHERE objective_id = ? AND stage = ? AND stage_attempt = ? AND receipt_hash = ?
                """,
                (objective_id, normalized_stage, attempt, receipt_hash),
            ).fetchone()
            if duplicate is not None:
                objective = connection.execute(
                    "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
                ).fetchone()
                if objective is None:
                    raise StateConflictError("delivery stage receipt has no objective")
                return {
                    "receipt": self._delivery_stage_receipt_row(duplicate),
                    "objective": self._delivery_objective_row(connection, objective),
                    "idempotent": True,
                }
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if row is None:
                raise KeyError(objective_id)
            self._assert_delivery_objective_lease(
                connection,
                row,
                coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch,
                timestamp=timestamp,
            )
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected delivery objective revision {expected_revision}, found {row['state_revision']}"
                )
            if row["stage"] != normalized_stage or int(row["stage_attempt"]) != attempt:
                raise StateConflictError(
                    "delivery stage receipt is stale for the current stage attempt"
                )
            if receipt_state == "succeeded" and evidence_fingerprint is None:
                raise StateConflictError("a successful delivery stage requires an Evidence fingerprint")
            request = json.loads(str(row["request_json"]))
            required_stages = required_delivery_stages(request)
            if normalized_stage not in required_stages:
                raise StateConflictError("delivery stage was not explicitly requested")
            if dispatch_id is not None:
                dispatch = connection.execute(
                    "SELECT * FROM delivery_stage_dispatches WHERE dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if (
                    dispatch is None
                    or dispatch["objective_id"] != objective_id
                    or dispatch["stage"] != normalized_stage
                    or int(dispatch["stage_attempt"]) != attempt
                    or dispatch["state"] != "started"
                ):
                    raise StateConflictError("delivery stage receipt does not match an active dispatch intent")
            if receipt_state == "succeeded" and normalized_stage == "live-verify":
                try:
                    validate_live_verification_receipt(request, receipt_body)
                except ValueError as error:
                    raise StateConflictError(str(error)) from error
            try:
                merged_identities = merge_identities(
                    json.loads(str(row["identities_json"])), normalized_identities
                )
            except ValueError as error:
                raise StateConflictError(str(error)) from error
            budget = normalize_budget(json.loads(str(row["budget_json"])))
            elapsed = max(
                0.0,
                (datetime.fromisoformat(timestamp) - datetime.fromisoformat(str(row["created_at"]))).total_seconds(),
            )
            budget["elapsed_seconds"] = max(float(budget["elapsed_seconds"]), elapsed)
            budget["cost_used"] = float(budget["cost_used"]) + float(cost_delta)
            time_exhausted = budget["elapsed_seconds"] > float(budget["time_budget_seconds"])
            cost_exhausted = budget["cost_used"] > float(budget["cost_budget"])
            evidence = json.loads(str(row["evidence_fingerprints_json"]))
            if evidence_fingerprint is not None:
                evidence[normalized_stage] = {
                    "attempt": attempt,
                    "fingerprint": evidence_fingerprint,
                }
            receipt_document: dict[str, Any] = dict(receipt_body)
            if normalized_failure is not None:
                receipt_document["failure"] = normalized_failure
            next_state: str
            next_stage = normalized_stage
            next_attempt = attempt
            wait_reason: dict[str, Any] | None = None
            if receipt_state == "succeeded":
                if normalized_stage == required_stages[-1]:
                    succeeded_stages = {
                        str(item["stage"])
                        for item in connection.execute(
                            """
                            SELECT DISTINCT stage FROM delivery_stage_receipts
                            WHERE objective_id = ? AND state = 'succeeded'
                            """,
                            (objective_id,),
                        ).fetchall()
                    }
                    succeeded_stages.add(normalized_stage)
                    if set(required_stages) - succeeded_stages:
                        raise StateConflictError(
                            "delivery cannot complete before every requested stage has a success receipt"
                        )
                    missing_identities = [
                        name for name in required_completion_identities(request) if name not in merged_identities
                    ]
                    if missing_identities:
                        raise StateConflictError(
                            "delivery completion requires exact "
                            + ", ".join(missing_identities)
                            + " identities"
                        )
                    next_state = "complete"
                    next_action = {"action": "complete", "stage": normalized_stage}
                    wakeup = None
                else:
                    stage_index = required_stages.index(normalized_stage)
                    next_stage = required_stages[stage_index + 1]
                    next_attempt = 1
                    next_state = "active"
                    next_action = {"action": "execute_stage", "stage": next_stage}
                    wakeup = timestamp
            else:
                assert normalized_failure is not None
                recovery_action = recovery_action_for_failure(normalized_failure)
                budget["attempts_used"] = int(budget["attempts_used"]) + 1
                retry_allowed = (
                    retry_eligible
                    and recovery_action["automatic"]
                    and budget["attempts_used"] < int(budget["attempt_limit"])
                    and not time_exhausted
                    and not cost_exhausted
                )
                if retry_allowed:
                    next_state = "waiting"
                    next_attempt = attempt + 1
                    exponent = min(30, max(0, int(budget["attempts_used"]) - 1))
                    backoff = min(
                        int(budget["max_backoff_seconds"]),
                        int(budget["base_backoff_seconds"]) * (2**exponent),
                    )
                    wakeup = requested_wakeup or self._timestamp_after(timestamp, backoff)
                    wait_reason = {
                        **normalized_failure,
                        "retry_eligible": True,
                        "next_attempt": next_attempt,
                        "backoff_seconds": backoff,
                        "budget": budget,
                        "recovery": recovery_action,
                    }
                    next_action = {
                        "action": recovery_action["action"],
                        "stage": normalized_stage,
                        "attempt": next_attempt,
                        "retry_after": wakeup,
                    }
                else:
                    next_state = "needs_decision"
                    wakeup = None
                    resolution = (
                        "reconcile_authoritatively"
                        if normalized_failure["kind"] == "unknown-effects"
                        else "grant_scope_limited_authorization"
                        if normalized_failure["kind"] == "permission-denied"
                        else "provide_essential_user_choice"
                        if normalized_failure["kind"] == "missing-essential-user-choice"
                        else "choose_recovery_or_stop"
                    )
                    wait_reason = {
                        **normalized_failure,
                        "retry_eligible": False,
                        "resolution": resolution,
                        "budget": budget,
                        "recovery": recovery_action,
                    }
                    next_action = {
                        "action": "request_human_decision",
                        "stage": normalized_stage,
                        "resolution": resolution,
                        "recommended_recovery": recovery_action["action"],
                    }
            revision = expected_revision + 1
            event_type = (
                "delivery_objective.stage_succeeded"
                if receipt_state == "succeeded"
                else "delivery_objective.retry_scheduled"
                if next_state == "waiting"
                else "delivery_objective.decision_required"
            )
            connection.execute(
                """
                INSERT INTO delivery_stage_receipts(
                    receipt_id, objective_id, task_id, stage, stage_attempt, receipt_hash,
                    state, receipt_json, evidence_fingerprint, identities_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    objective_id,
                    row["task_id"],
                    normalized_stage,
                    attempt,
                    receipt_hash,
                    receipt_state,
                    canonical_json(receipt_document),
                    evidence_fingerprint,
                    canonical_json(normalized_identities),
                    timestamp,
                ),
            )
            event_payload = {
                "objective_id": objective_id,
                "receipt_id": receipt_id,
                "stage": normalized_stage,
                "attempt": attempt,
                "receipt_state": receipt_state,
                "next_state": next_state,
                "next_stage": next_stage,
                "next_attempt": next_attempt,
                "state_revision": revision,
                "next_wakeup_at": wakeup,
                "evidence_fingerprint": evidence_fingerprint,
            }
            cursor = self._event(
                connection,
                event_type,
                str(row["task_id"]),
                None,
                event_payload,
                created_at=timestamp,
            )
            progress = {
                "kind": event_type,
                "event_cursor": cursor,
                "receipt_id": receipt_id,
                "stage": normalized_stage,
                "attempt": attempt,
                "receipt_state": receipt_state,
            }
            retains_lease = next_state == "active"
            next_owner = row["owner_id"] if retains_lease else None
            next_coordinator_epoch = int(row["coordinator_epoch"]) if retains_lease else 0
            next_lease_epoch = lease_epoch if retains_lease else 0
            next_lease_expires_at = row["lease_expires_at"] if retains_lease else None
            changed = connection.execute(
                """
                UPDATE delivery_objectives
                SET state = ?, stage = ?, stage_attempt = ?, state_revision = ?,
                    next_action_json = ?, owner_id = ?, coordinator_epoch = ?, lease_epoch = ?,
                    lease_expires_at = ?, next_wakeup_at = ?, last_material_progress_at = ?,
                    last_progress_json = ?, wait_reason_json = ?, budget_json = ?,
                    evidence_fingerprints_json = ?, identities_json = ?, updated_at = ?
                WHERE objective_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (
                    next_state,
                    next_stage,
                    next_attempt,
                    revision,
                    canonical_json(next_action),
                    next_owner,
                    next_coordinator_epoch,
                    next_lease_epoch,
                    next_lease_expires_at,
                    wakeup,
                    timestamp,
                    canonical_json(progress),
                    canonical_json(wait_reason) if wait_reason is not None else None,
                    canonical_json(budget),
                    canonical_json(evidence),
                    canonical_json(merged_identities),
                    timestamp,
                    objective_id,
                    expected_revision,
                    lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("delivery stage receipt compare-and-set failed")
            if dispatch_id is not None:
                settled_dispatch = connection.execute(
                    """
                    UPDATE delivery_stage_dispatches
                    SET state = 'settled', receipt_id = ?, updated_at = ?
                    WHERE dispatch_id = ? AND state = 'started'
                    """,
                    (receipt_id, timestamp, dispatch_id),
                ).rowcount
                if settled_dispatch != 1:
                    raise StateConflictError("delivery stage dispatch settlement compare-and-set failed")
            stored_receipt = connection.execute(
                "SELECT * FROM delivery_stage_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            assert stored_receipt is not None and objective is not None
            return {
                "receipt": self._delivery_stage_receipt_row(stored_receipt),
                "objective": self._delivery_objective_row(connection, objective),
                "idempotent": False,
            }

    @staticmethod
    def _deployment_admission_gate(connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT o.objective_id, o.task_id, o.stage, o.state FROM delivery_objectives o
            WHERE o.state NOT IN ('complete', 'cancelled') AND (
              (o.stage = 'deploy' AND (
                json_extract(o.last_progress_json, '$.kind') IN
                  ('delivery_objective.deployment_safe_point_waiting', 'delivery_objective.deployment_safe_point_ready')
                OR EXISTS (SELECT 1 FROM delivery_stage_dispatches d WHERE d.objective_id = o.objective_id
                           AND d.stage = 'deploy' AND d.state = 'started')
                OR (json_extract(o.wait_reason_json, '$.kind') = 'unknown-effects'
                    AND EXISTS (SELECT 1 FROM delivery_stage_dispatches d WHERE d.objective_id = o.objective_id AND d.stage = 'deploy'))
              )) OR (o.stage = 'live-verify' AND EXISTS (
                SELECT 1 FROM delivery_stage_receipts r WHERE r.objective_id = o.objective_id
                AND r.stage = 'deploy' AND r.state = 'succeeded'))
            ) ORDER BY o.created_at, o.objective_id LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def delivery_admission_gate(self) -> dict[str, Any] | None:
        """Expose the authority rollout currently holding new worker admission."""
        with self.connection() as connection:
            return self._deployment_admission_gate(connection)

    @staticmethod
    def _delivery_deployment_blockers_for_objective(
        connection: sqlite3.Connection,
        objective: sqlite3.Row,
    ) -> list[dict[str, Any]]:
        """Return all authority work that must drain before its service restarts."""

        task = connection.execute(
            "SELECT contract_json FROM tasks WHERE task_id = ?", (objective["task_id"],)
        ).fetchone()
        if task is None:
            # A planning reservation has no worker worktree to drain yet.
            return []
        contract = json.loads(str(task["contract_json"]))
        repository = contract.get("repository")
        if not isinstance(repository, str) or not repository:
            raise StateConflictError("delivery objective task has no repository identity")
        rows = connection.execute(
            """
            SELECT n.task_id, n.node_id, n.attempt, n.worker_id, n.worktree,
                   n.started_at, t.contract_json
            FROM nodes n JOIN tasks t USING(task_id)
            WHERE n.state = 'running'
            ORDER BY n.started_at, n.task_id, n.node_id
            """
        ).fetchall()
        blockers: list[dict[str, Any]] = []
        for row in rows:
            blockers.append(
                {
                    "task_id": str(row["task_id"]),
                    "node_id": str(row["node_id"]),
                    "attempt": int(row["attempt"]),
                    "worker_id": row["worker_id"],
                    "worktree": row["worktree"],
                    "started_at": row["started_at"],
                }
            )
        for planning in connection.execute("SELECT task_id, command_id, attempt, started_at FROM planning_requests WHERE state = 'running'").fetchall():
            blockers.append({"task_id": planning["task_id"], "node_id": None, "attempt": planning["attempt"],
                             "worker_id": None, "worktree": None, "started_at": planning["started_at"],
                             "kind": "planning", "command_id": planning["command_id"]})
        return blockers

    def delivery_deployment_blockers(self, objective_id: str) -> list[dict[str, Any]]:
        """Read the concrete worker leases that must drain before deployment."""

        with self.connection() as connection:
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if objective is None:
                raise KeyError(objective_id)
            return self._delivery_deployment_blockers_for_objective(connection, objective)

    def defer_delivery_observation(
        self,
        objective_id: str,
        *,
        expected_revision: int,
        coordinator_epoch: int,
        lease_epoch: int,
        reason: dict[str, Any],
        next_wakeup_at: str | None,
        dispatch_id: str | None,
    ) -> dict[str, Any]:
        """Release a stage lease for a pending observation without retrying its effect.

        The dispatch remains unsettled. A later owner must reconcile that same
        intent, while stage attempts and failure budgets remain unchanged.
        """
        wait = normalize_wait_reason(reason)
        timestamp = now_iso()
        wakeup = normalize_timestamp(next_wakeup_at, "next_wakeup_at")
        if wakeup is None or self._timestamp_is_due(wakeup, timestamp):
            wakeup = self._timestamp_after(timestamp, 5)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if row is None:
                raise KeyError(objective_id)
            self._assert_delivery_objective_lease(
                connection, row, coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch, timestamp=timestamp,
            )
            if int(row["state_revision"]) != expected_revision:
                raise StateConflictError("delivery observation revision is stale")
            dispatch = connection.execute(
                "SELECT * FROM delivery_stage_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if dispatch_id is None:
                if row["stage"] != "deploy" or connection.execute(
                    "SELECT 1 FROM delivery_stage_dispatches WHERE objective_id = ? AND stage = 'deploy' AND stage_attempt = ?",
                    (objective_id, row["stage_attempt"]),
                ).fetchone():
                    raise StateConflictError("only an undispatched rollout may wait without intent")
            elif (dispatch is None or dispatch["objective_id"] != objective_id
                    or dispatch["stage"] != row["stage"]
                    or dispatch["stage_attempt"] != row["stage_attempt"]
                    or dispatch["state"] != "started"):
                raise StateConflictError("delivery observation has no current unsettled dispatch")
            previous = json.loads(str(row["last_progress_json"]))
            observation = {
                "kind": "delivery_objective.observation_waiting",
                "stage": row["stage"], "dispatch_id": dispatch_id, "reason": wait,
            }
            changed_observation = any(previous.get(key) != value for key, value in observation.items())
            if changed_observation:
                cursor = self._event(
                    connection, "delivery_objective.observation_waiting", str(row["task_id"]), None,
                    {"objective_id": objective_id, **observation, "next_wakeup_at": wakeup},
                    created_at=timestamp,
                )
                observation["event_cursor"] = cursor
            else:
                observation = previous
            next_action = {
                "action": "reconcile_pending_observation", "stage": row["stage"],
                "dispatch_id": dispatch_id, "retry_after": wakeup,
            }
            changed = connection.execute(
                """
                UPDATE delivery_objectives
                SET state = 'waiting', state_revision = state_revision + 1,
                    owner_id = NULL, coordinator_epoch = 0, lease_epoch = 0,
                    lease_expires_at = NULL, next_wakeup_at = ?, next_action_json = ?,
                    wait_reason_json = ?, last_progress_json = ?,
                    last_material_progress_at = ?, updated_at = ?
                WHERE objective_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (wakeup, canonical_json(next_action), canonical_json(wait), canonical_json(observation),
                 timestamp if changed_observation else row["last_material_progress_at"], timestamp,
                 objective_id, expected_revision, lease_epoch),
            ).rowcount
            if changed != 1:
                raise StateConflictError("delivery observation compare-and-set failed")
            current = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            assert current is not None
            return self._delivery_objective_row(connection, current)

    def defer_delivery_deployment_safe_point(
        self,
        objective_id: str,
        *,
        expected_revision: int,
        coordinator_epoch: int,
        lease_epoch: int,
        blockers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Durably wait for active workers rather than interrupting a rollout."""

        if not isinstance(blockers, list) or not blockers:
            raise ValueError("deployment safe-point wait requires active worker blockers")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("delivery safe-point expected_revision must be positive")
        timestamp = now_iso()
        with self.transaction() as connection:
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if objective is None:
                raise KeyError(objective_id)
            self._assert_delivery_objective_lease(
                connection,
                objective,
                coordinator_epoch=coordinator_epoch,
                lease_epoch=lease_epoch,
                timestamp=timestamp,
            )
            if int(objective["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected delivery objective revision {expected_revision}, found {objective['state_revision']}"
                )
            if objective["stage"] != "deploy":
                raise StateConflictError("only deployment may wait for a rollout safe point")
            actual_blockers = self._delivery_deployment_blockers_for_objective(connection, objective)
            if not actual_blockers:
                raise StateConflictError("deployment workers already drained; claim the stage instead")
            due_at = str(objective["due_at"])
            next_wakeup_at = self._timestamp_after(timestamp, 5)
            next_action = {
                "action": "wait_for_active_workers_to_drain",
                "stage": "deploy",
                "trigger": "worker_settlement_or_bounded_reconciliation",
                "retry_after": next_wakeup_at,
            }
            wait_reason = {
                "kind": "active-workers-draining",
                "detail": "deployment waits for all authority work to reach a safe point",
                "blockers": actual_blockers,
                "due_at": due_at,
            }
            revision = expected_revision + 1
            cursor = self._event(
                connection,
                "delivery_objective.deployment_safe_point_waiting",
                str(objective["task_id"]),
                None,
                {
                    "objective_id": objective_id,
                    "stage": "deploy",
                    "attempt": int(objective["stage_attempt"]),
                    "state_revision": revision,
                    "blockers": actual_blockers,
                    "next_wakeup_at": next_wakeup_at,
                },
                created_at=timestamp,
            )
            changed = connection.execute(
                """
                UPDATE delivery_objectives
                SET state = 'waiting', state_revision = ?, next_action_json = ?,
                    owner_id = NULL, coordinator_epoch = 0, lease_epoch = 0,
                    lease_expires_at = NULL, next_wakeup_at = ?,
                    last_material_progress_at = ?, last_progress_json = ?,
                    wait_reason_json = ?, updated_at = ?
                WHERE objective_id = ? AND state_revision = ? AND lease_epoch = ?
                """,
                (
                    revision,
                    canonical_json(next_action),
                    next_wakeup_at,
                    timestamp,
                    canonical_json(
                        {
                            "kind": "delivery_objective.deployment_safe_point_waiting",
                            "event_cursor": cursor,
                            "stage": "deploy",
                            "attempt": int(objective["stage_attempt"]),
                            "blocker_count": len(actual_blockers),
                        }
                    ),
                    canonical_json(wait_reason),
                    timestamp,
                    objective_id,
                    expected_revision,
                    lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("delivery deployment safe-point compare-and-set failed")
            current = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            assert current is not None
            return self._delivery_objective_row(connection, current)

    def _reconcile_delivery_safe_point_waits(
        self,
        connection: sqlite3.Connection,
        *,
        timestamp: str,
    ) -> int:
        """Wake each recorded safe point once its exact blocking leases drain."""

        rows = connection.execute(
            """
            SELECT * FROM delivery_objectives
            WHERE state = 'waiting' AND stage = 'deploy' AND wait_reason_json IS NOT NULL
            """
        ).fetchall()
        resumed = 0
        for objective in rows:
            try:
                wait_reason = json.loads(str(objective["wait_reason_json"]))
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise StateConflictError("deployment safe-point wait is invalid") from error
            if wait_reason.get("kind") != "active-workers-draining":
                continue
            recorded = wait_reason.get("blockers")
            if not isinstance(recorded, list):
                raise StateConflictError("deployment safe-point blockers are invalid")
            if wait_reason.get("safe_point_ready") is True:
                continue
            still_running = bool(self._delivery_deployment_blockers_for_objective(connection, objective))
            for blocker in recorded:
                if not isinstance(blocker, dict):
                    raise StateConflictError("deployment safe-point blocker is invalid")
                row = connection.execute(
                    "SELECT state, attempt FROM nodes WHERE task_id = ? AND node_id = ?",
                    (blocker.get("task_id"), blocker.get("node_id")),
                ).fetchone()
                if (
                    row is not None
                    and row["state"] == "running"
                    and int(row["attempt"]) == blocker.get("attempt")
                ):
                    still_running = True
                    break
            if still_running:
                continue
            # A newly running worker is checked again immediately before the
            # deploy adapter call.  This wake only says the recorded lease
            # set has drained; it never claims the rollout was successful.
            revision = int(objective["state_revision"])
            cursor = self._event(
                connection,
                "delivery_objective.deployment_safe_point_ready",
                str(objective["task_id"]),
                None,
                {
                    "objective_id": objective["objective_id"],
                    "stage": "deploy",
                    "attempt": int(objective["stage_attempt"]),
                    "state_revision": revision,
                    "next_wakeup_at": timestamp,
                },
                created_at=timestamp,
            )
            changed = connection.execute(
                """
                UPDATE delivery_objectives
                SET next_wakeup_at = ?, last_material_progress_at = ?,
                    last_progress_json = ?, wait_reason_json = ?, updated_at = ?
                WHERE objective_id = ? AND state_revision = ? AND state = 'waiting'
                """,
                (
                    timestamp,
                    timestamp,
                    canonical_json(
                        {
                            "kind": "delivery_objective.deployment_safe_point_ready",
                            "event_cursor": cursor,
                            "stage": "deploy",
                            "attempt": int(objective["stage_attempt"]),
                        }
                    ),
                    canonical_json({**wait_reason, "safe_point_ready": True}),
                    timestamp,
                    objective["objective_id"],
                    objective["state_revision"],
                ),
            ).rowcount
            if changed == 1:
                resumed += 1
        return resumed

    def reconcile_delivery_safe_point_waits(self) -> int:
        """Event-driven callers and periodic coordinator ticks share this wakeup."""

        timestamp = now_iso()
        with self.transaction() as connection:
            return self._reconcile_delivery_safe_point_waits(connection, timestamp=timestamp)

    @staticmethod
    def _normalize_delivery_authorization_scope(scope: object) -> dict[str, Any]:
        """Require one exact GitHub or deployment subset, never a blanket grant."""

        if not isinstance(scope, dict) or not scope:
            raise ValueError("delivery authorization scope must be a non-empty object")
        if "deployment" in scope:
            if set(scope) != {"deployment"}:
                raise ValueError("deployment authorization may bind only one deployment endpoint")
            deployment = scope["deployment"]
            if not isinstance(deployment, dict):
                raise ValueError("deployment authorization scope.deployment must be an object")
            unknown = set(deployment) - {"target"}
            target = deployment.get("target")
            if unknown or not isinstance(target, str) or not target.strip():
                raise ValueError("deployment authorization scope must bind exactly one target")
            return {
                "deployment": {"target": target.strip()},
                "declared_scope": dict(scope),
            }
        raw_delivery = scope.get("delivery", scope)
        if not isinstance(raw_delivery, dict):
            raise ValueError("delivery authorization scope.delivery must be an object")
        required = {"remote", "base_branch", "merge"}
        unknown = set(raw_delivery) - {"remote", "base_branch", "merge", "release_tag"}
        if unknown or not required.issubset(raw_delivery):
            raise ValueError(
                "delivery authorization scope must bind remote, base_branch, merge, and optional release_tag"
            )
        remote = raw_delivery["remote"]
        base_branch = raw_delivery["base_branch"]
        merge = raw_delivery["merge"]
        release_tag = raw_delivery.get("release_tag")
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError("delivery authorization remote is required")
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise ValueError("delivery authorization base_branch is required")
        if not isinstance(merge, bool):
            raise ValueError("delivery authorization merge must be a boolean")
        if release_tag is not None and (not isinstance(release_tag, str) or not release_tag.strip()):
            raise ValueError("delivery authorization release_tag must be a non-empty string or null")
        return {
            "delivery": {
                "remote": remote.strip(),
                "base_branch": base_branch.strip(),
                "merge": merge,
                "release_tag": release_tag.strip() if isinstance(release_tag, str) else None,
            },
            "declared_scope": dict(scope),
        }

    @staticmethod
    def _normalize_delivery_authority(authority: object) -> dict[str, Any]:
        if not isinstance(authority, dict) or not authority:
            raise ValueError("delivery authorization authority must be a non-empty object")
        try:
            json.dumps(authority, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("delivery authorization authority must be JSON-safe") from error
        return dict(authority)

    @staticmethod
    def _authorization_matches_request(scope: dict[str, Any], request: dict[str, Any]) -> bool:
        delivery = scope.get("delivery")
        if not isinstance(delivery, dict):
            return False
        for key in ("remote", "base_branch", "merge", "release_tag"):
            if delivery.get(key) != request.get(key):
                return False
        return True

    def _matching_delivery_authorization(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        request: dict[str, Any],
        *,
        timestamp: str,
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            """
            SELECT * FROM delivery_authorization_receipts
            WHERE task_id = ? AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC, rowid DESC
            """,
            (task_id, timestamp),
        ).fetchall()
        for row in rows:
            candidate = self._delivery_authorization_row(row)
            if self._authorization_matches_request(candidate["scope"], request):
                return candidate if candidate["decision"] == "granted" else None
        return None

    @staticmethod
    def _deployment_authorization_matches_request(scope: dict[str, Any], target: str) -> bool:
        deployment = scope.get("deployment")
        return isinstance(deployment, dict) and deployment.get("target") == target

    def _matching_deployment_authorization(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        target: str,
        *,
        timestamp: str,
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            """
            SELECT * FROM delivery_authorization_receipts
            WHERE task_id = ? AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC, rowid DESC
            """,
            (task_id, timestamp),
        ).fetchall()
        for row in rows:
            candidate = self._delivery_authorization_row(row)
            if self._deployment_authorization_matches_request(candidate["scope"], target):
                return candidate if candidate["decision"] == "granted" else None
        return None

    def delivery_stage_authorization(self, objective_id: str, stage: str) -> dict[str, Any]:
        """Resolve the least authority needed for one external lifecycle stage.

        The task's old immutable external-write bit continues to cover its
        exact GitHub route for compatibility.  A scope-limited receipt is
        otherwise required, and deployment always requires its own endpoint
        receipt.  No worker executor setting is consulted here.
        """

        normalized_stage = normalize_stage(stage)
        with self.connection() as connection:
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)
            ).fetchone()
            if objective is None:
                raise KeyError(objective_id)
            request = json.loads(str(objective["request_json"]))
            if normalized_stage not in required_delivery_stages(request):
                return {
                    "authorized": False,
                    "reason": "delivery stage was not explicitly requested",
                    "authorization": None,
                }
            if normalized_stage not in {"integrate", "ci", "publish", "deploy", "live-verify"}:
                return {"authorized": True, "reason": "no external endpoint", "authorization": None}
            task = connection.execute(
                "SELECT state, contract_json FROM tasks WHERE task_id = ?", (objective["task_id"],)
            ).fetchone()
            if task is None or task["state"] != "accepted":
                return {
                    "authorized": False,
                    "reason": "verifier acceptance is required before coordinator delivery",
                    "authorization": None,
                }
            endpoints = request["requested_endpoints"]
            timestamp = now_iso()
            if normalized_stage in {"integrate", "ci", "publish"}:
                github = endpoints.get("github") if isinstance(endpoints, dict) else None
                if not isinstance(github, dict):
                    return {
                        "authorized": False,
                        "reason": "GitHub endpoint was not explicitly requested",
                        "authorization": None,
                    }
                delivery_request = {
                    "task_id": str(objective["task_id"]),
                    "remote": github.get("remote", "origin"),
                    "base_branch": github.get("base_branch"),
                    "merge": github.get("merge", False),
                    "release_tag": github.get("release_tag"),
                }
                if (
                    not isinstance(delivery_request["remote"], str)
                    or not isinstance(delivery_request["base_branch"], str)
                    or not isinstance(delivery_request["merge"], bool)
                    or (
                        delivery_request["release_tag"] is not None
                        and not isinstance(delivery_request["release_tag"], str)
                    )
                ):
                    return {
                        "authorized": False,
                        "reason": "requested GitHub endpoint has an invalid exact delivery scope",
                        "authorization": None,
                    }
                contract = json.loads(str(task["contract_json"]))
                if contract.get("external_write_permission") is True:
                    return {
                        "authorized": True,
                        "reason": "immutable task delivery authority",
                        "authorization": {"source": "immutable-task-contract"},
                    }
                authorization = self._matching_delivery_authorization(
                    connection,
                    str(objective["task_id"]),
                    delivery_request,
                    timestamp=timestamp,
                )
                return {
                    "authorized": authorization is not None,
                    "reason": (
                        "scope-limited GitHub delivery authorization is required"
                        if authorization is None
                        else "scope-limited GitHub delivery authorization"
                    ),
                    "authorization": authorization,
                }
            deployment = endpoints.get("deployment") if isinstance(endpoints, dict) else None
            if not isinstance(deployment, dict) or not isinstance(deployment.get("target"), str):
                return {
                    "authorized": False,
                    "reason": "deployment endpoint was not explicitly requested",
                    "authorization": None,
                }
            authorization = self._matching_deployment_authorization(
                connection,
                str(objective["task_id"]),
                deployment["target"],
                timestamp=timestamp,
            )
            return {
                "authorized": authorization is not None,
                "reason": (
                    "scope-limited deployment authorization is required"
                    if authorization is None
                    else "scope-limited deployment authorization"
                ),
                "authorization": authorization,
            }

    def record_delivery_authorization(
        self,
        task_id: str,
        authorization_id: str,
        *,
        scope: dict[str, Any],
        authority: dict[str, Any],
        decision: str = "granted",
        granted_by: str = "coordinator",
        objective_id: str | None = None,
        expires_at: str | None = None,
        expected_task_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append an explicit delivery overlay without mutating the contract."""

        if not isinstance(authorization_id, str) or not authorization_id.strip():
            raise ValueError("delivery authorization_id is required")
        if decision not in {"granted", "denied"}:
            raise ValueError("delivery authorization decision must be granted or denied")
        if not isinstance(granted_by, str) or not granted_by.strip():
            raise ValueError("delivery authorization granted_by is required")
        normalized_scope = self._normalize_delivery_authorization_scope(scope)
        normalized_authority = self._normalize_delivery_authority(authority)
        normalized_expiry = normalize_timestamp(expires_at, "expires_at")
        request_hash = canonical_hash(
            {
                "task_id": task_id,
                "scope": normalized_scope,
                "authority": normalized_authority,
                "decision": decision,
                "granted_by": granted_by.strip(),
                "objective_id": objective_id,
                "expires_at": normalized_expiry,
            }
        )
        timestamp = now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM delivery_authorization_receipts WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise CommandConflictError(
                        f"delivery authorization {authorization_id!r} was already used with a different request"
                    )
                objective = connection.execute(
                    "SELECT * FROM delivery_objectives WHERE task_id = ?", (task_id,)
                ).fetchone()
                return {
                    "authorization": self._delivery_authorization_row(existing),
                    "objective": (
                        self._delivery_objective_row(connection, objective)
                        if objective is not None
                        else None
                    ),
                    "idempotent": True,
                }
            task = connection.execute(
                "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["state"] != "accepted":
                raise StateConflictError(
                    f"task {task_id} is {task['state']}, expected accepted for delivery authorization"
                )
            if expected_task_revision is not None and int(task["state_revision"]) != expected_task_revision:
                raise StateConflictError(
                    f"expected task revision {expected_task_revision}, found {task['state_revision']}"
                )
            objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE task_id = ?", (task_id,)
            ).fetchone()
            if objective_id is not None:
                if objective is None or objective["objective_id"] != objective_id:
                    raise StateConflictError("delivery authorization objective does not belong to the task")
            linked_objective_id = str(objective["objective_id"]) if objective is not None else None
            connection.execute(
                """
                INSERT INTO delivery_authorization_receipts(
                    authorization_id, task_id, objective_id, request_hash, scope_json,
                    authority_json, decision, granted_by, expires_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization_id,
                    task_id,
                    linked_objective_id,
                    request_hash,
                    canonical_json(normalized_scope),
                    canonical_json(normalized_authority),
                    decision,
                    granted_by.strip(),
                    normalized_expiry,
                    timestamp,
                ),
            )
            cursor = self._event(
                connection,
                f"delivery.authorization_{decision}",
                task_id,
                None,
                {
                    "authorization_id": authorization_id,
                    "objective_id": linked_objective_id,
                    "request_hash": request_hash,
                    "scope": normalized_scope.get("delivery", normalized_scope.get("deployment")),
                    "expires_at": normalized_expiry,
                },
                created_at=timestamp,
            )
            if decision == "denied" and objective is not None and objective["state"] in {"waiting", "needs_decision"}:
                revision = int(objective["state_revision"]) + 1
                wait_reason = {
                    "kind": "permission-denied",
                    "detail": "explicit coordinator delivery authorization was denied",
                    "resolution": "grant_scope_limited_authorization",
                    "authorization_id": authorization_id,
                }
                next_action = {
                    "action": "request_human_decision",
                    "stage": str(objective["stage"]),
                    "resolution": "grant_scope_limited_authorization",
                }
                connection.execute(
                    """
                    UPDATE delivery_objectives
                    SET state = 'needs_decision', state_revision = ?, next_action_json = ?,
                        owner_id = NULL, coordinator_epoch = 0, lease_epoch = 0,
                        lease_expires_at = NULL, next_wakeup_at = NULL,
                        last_material_progress_at = ?, last_progress_json = ?,
                        wait_reason_json = ?, updated_at = ?
                    WHERE objective_id = ? AND state_revision = ?
                    """,
                    (
                        revision,
                        canonical_json(next_action),
                        timestamp,
                        canonical_json(
                            {
                                "kind": "delivery-authorization-denied",
                                "event_cursor": cursor,
                                "authorization_id": authorization_id,
                                "stage": objective["stage"],
                                "attempt": int(objective["stage_attempt"]),
                            }
                        ),
                        canonical_json(wait_reason),
                        timestamp,
                        objective["objective_id"],
                        objective["state_revision"],
                    ),
                )
                self._event(
                    connection,
                    "delivery_objective.authorization_denied",
                    task_id,
                    None,
                    {
                        "objective_id": objective["objective_id"],
                        "authorization_id": authorization_id,
                        "stage": objective["stage"],
                        "attempt": int(objective["stage_attempt"]),
                        "state_revision": revision,
                    },
                    created_at=timestamp,
                )
            elif decision == "granted" and objective is not None:
                wait_reason = (
                    json.loads(str(objective["wait_reason_json"]))
                    if objective["wait_reason_json"] is not None
                    else None
                )
                if (
                    objective["state"] in {"waiting", "needs_decision"}
                    and isinstance(wait_reason, dict)
                    and wait_reason.get("kind") == "permission-denied"
                ):
                    revision = int(objective["state_revision"]) + 1
                    progress = {
                        "kind": "delivery-authorization-granted",
                        "event_cursor": cursor,
                        "authorization_id": authorization_id,
                        "stage": objective["stage"],
                        "attempt": int(objective["stage_attempt"]),
                    }
                    next_action = {
                        "action": "execute_stage",
                        "stage": str(objective["stage"]),
                    }
                    connection.execute(
                        """
                        UPDATE delivery_objectives
                        SET state = 'active', state_revision = ?, next_action_json = ?,
                            next_wakeup_at = ?, last_material_progress_at = ?,
                            last_progress_json = ?, wait_reason_json = NULL, updated_at = ?
                        WHERE objective_id = ? AND state_revision = ?
                        """,
                        (
                            revision,
                            canonical_json(next_action),
                            timestamp,
                            timestamp,
                            canonical_json(progress),
                            timestamp,
                            objective["objective_id"],
                            objective["state_revision"],
                        ),
                    )
                    self._event(
                        connection,
                        "delivery_objective.authorization_resumed",
                        task_id,
                        None,
                        {
                            "objective_id": objective["objective_id"],
                            "authorization_id": authorization_id,
                            "stage": objective["stage"],
                            "attempt": int(objective["stage_attempt"]),
                            "state_revision": revision,
                            "next_wakeup_at": timestamp,
                        },
                        created_at=timestamp,
                    )
            authorization = connection.execute(
                "SELECT * FROM delivery_authorization_receipts WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            current_objective = connection.execute(
                "SELECT * FROM delivery_objectives WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert authorization is not None
            return {
                "authorization": self._delivery_authorization_row(authorization),
                "objective": (
                    self._delivery_objective_row(connection, current_objective)
                    if current_objective is not None
                    else None
                ),
                "idempotent": False,
            }

    def grant_delivery_authorization(
        self,
        task_id: str,
        authorization_id: str,
        *,
        scope: dict[str, Any],
        authority: dict[str, Any],
        granted_by: str = "coordinator",
        objective_id: str | None = None,
        expires_at: str | None = None,
        expected_task_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.record_delivery_authorization(
            task_id,
            authorization_id,
            scope=scope,
            authority=authority,
            decision="granted",
            granted_by=granted_by,
            objective_id=objective_id,
            expires_at=expires_at,
            expected_task_revision=expected_task_revision,
        )

    def check_delivery_command(self, command_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        """Return an existing matching delivery receipt without creating one.

        Delivery validates local Git inputs before creating a durable receipt.  A
        preflight lookup still has to fence a reused command ID first: otherwise
        a changed request could fail at an unrelated local preflight step rather
        than being rejected as a command conflict.
        """

        request_hash = canonical_hash(request)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise CommandConflictError(
                    f"delivery command {command_id!r} was already used with a different request"
                )
            return self._delivery_row(row)

    def acquire_delivery_lease(
        self,
        command_id: str,
        *,
        lease_seconds: int = _DELIVERY_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Acquire the durable fence for one delivery command.

        The external Git and GitHub calls cannot run in a SQLite transaction.
        This short transaction therefore gives the caller an opaque fence that
        every later receipt advance must present.  An expired lease may be
        recovered after a process loss, while an unexpired lease rejects a
        concurrent resume instead of permitting a second external write.
        """

        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("delivery lease_seconds must be a positive integer")
        observed = datetime.now(UTC)
        timestamp = observed.isoformat(timespec="seconds")
        expires_at = (observed + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        fence = uuid4().hex
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if receipt is None:
                raise KeyError(command_id)
            existing = connection.execute(
                "SELECT fence, expires_at FROM delivery_leases WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing is not None and str(existing["expires_at"]) > timestamp:
                raise StateConflictError(
                    f"delivery command {command_id!r} is already being resumed"
                )
            connection.execute(
                """
                INSERT INTO delivery_leases(command_id, fence, acquired_at, expires_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                    fence = excluded.fence,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (command_id, fence, timestamp, expires_at, timestamp),
            )
            self._event(
                connection,
                "delivery.lease_acquired",
                receipt["task_id"],
                None,
                {
                    "command_id": command_id,
                    "recovered_expired_lease": existing is not None,
                },
            )
            return {"receipt": self._delivery_row(receipt), "fence": fence, "expires_at": expires_at}

    def renew_delivery_lease(
        self,
        command_id: str,
        fence: str,
        *,
        lease_seconds: int = _DELIVERY_LEASE_SECONDS,
    ) -> None:
        """Extend a current delivery fence before a bounded external command."""

        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("delivery lease_seconds must be a positive integer")
        observed = datetime.now(UTC)
        timestamp = observed.isoformat(timespec="seconds")
        expires_at = (observed + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self.transaction() as connection:
            self._require_delivery_fence(connection, command_id, fence, timestamp)
            changed = connection.execute(
                """
                UPDATE delivery_leases
                SET expires_at = ?, updated_at = ?
                WHERE command_id = ? AND fence = ?
                """,
                (expires_at, timestamp, command_id, fence),
            ).rowcount
            if changed != 1:
                raise StateConflictError(f"delivery command {command_id!r} lease is stale")

    def release_delivery_lease(self, command_id: str, fence: str) -> bool:
        """Release a fence if it still belongs to this invocation.

        A stale caller must not be able to release a newer owner's fence.  This
        method deliberately returns false rather than raising for that normal
        finally-path race.
        """

        timestamp = now_iso()
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT task_id FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if receipt is None:
                raise KeyError(command_id)
            changed = connection.execute(
                "DELETE FROM delivery_leases WHERE command_id = ? AND fence = ?",
                (command_id, fence),
            ).rowcount
            if changed:
                self._event(
                    connection,
                    "delivery.lease_released",
                    receipt["task_id"],
                    None,
                    {"command_id": command_id, "released_at": timestamp},
                )
            return bool(changed)

    def update_delivery(
        self,
        command_id: str,
        state: str,
        details: dict[str, Any],
        *,
        expected_states: tuple[str, ...] | None = None,
        fence: str | None = None,
    ) -> dict[str, Any]:
        """Advance one delivery receipt, optionally under its durable fence.

        Existing callers without a fence retain compatibility, but delivery
        execution itself always supplies both ``expected_states`` and ``fence``.
        The pair prevents an old or concurrent invocation from overwriting a
        newer recovery result.
        """

        if not isinstance(details, dict):
            raise ValueError("delivery details must be an object")
        if expected_states is not None:
            if not expected_states or any(not isinstance(value, str) for value in expected_states):
                raise ValueError("delivery expected_states must be non-empty strings")
        if fence is not None and expected_states is None:
            raise ValueError("fenced delivery updates require expected_states")
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            if expected_states is not None and row["state"] not in expected_states:
                expected = ", ".join(sorted(expected_states))
                raise StateConflictError(
                    f"delivery command {command_id!r} is {row['state']}, expected one of {expected}"
                )
            if fence is not None:
                self._require_delivery_fence(connection, command_id, fence, timestamp)
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

    @staticmethod
    def _require_delivery_fence(
        connection: sqlite3.Connection,
        command_id: str,
        fence: str,
        timestamp: str,
    ) -> None:
        lease = connection.execute(
            "SELECT fence, expires_at FROM delivery_leases WHERE command_id = ?", (command_id,)
        ).fetchone()
        if (
            lease is None
            or lease["fence"] != fence
            or str(lease["expires_at"]) <= timestamp
        ):
            raise StateConflictError(f"delivery command {command_id!r} lease is stale")

    def get_delivery(self, command_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            return self._delivery_row(row)
    def deny_delivery_authorization(
        self,
        task_id: str,
        authorization_id: str,
        *,
        scope: dict[str, Any],
        authority: dict[str, Any],
        granted_by: str = "coordinator",
        objective_id: str | None = None,
        expires_at: str | None = None,
        expected_task_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.record_delivery_authorization(
            task_id,
            authorization_id,
            scope=scope,
            authority=authority,
            decision="denied",
            granted_by=granted_by,
            objective_id=objective_id,
            expires_at=expires_at,
            expected_task_revision=expected_task_revision,
        )

    def get_planning_request_for_task(self, task_id: str) -> dict[str, Any] | None:
        """Read the existing reservation without allocating a new task identity."""
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM planning_requests WHERE task_id = ?", (task_id,)).fetchone()
            return self._planning_request_row(row) if row is not None else None

    def retry_planning_request(self, command_id: str, *, expected_attempt: int, max_attempts: int,
                               reason: str, coordinator_epoch: int, objective_id: str | None = None,
                               objective_lease_epoch: int | None = None) -> dict[str, Any]:
        """Requeue a failed, unmaterialized reservation under its original hash."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("planning retry requires a reason")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("planning retry maximum must be positive")
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            row = connection.execute("SELECT * FROM planning_requests WHERE command_id = ?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(command_id)
            if (objective_id is None) != (objective_lease_epoch is None):
                raise ValueError("planning retry objective and lease must be supplied together")
            if objective_id is not None:
                objective = connection.execute("SELECT * FROM delivery_objectives WHERE objective_id = ?", (objective_id,)).fetchone()
                if objective is None or objective["task_id"] != row["task_id"] or objective["stage"] != "plan":
                    raise StateConflictError("planning retry objective does not own this reservation")
                self._assert_delivery_objective_lease(connection, objective, coordinator_epoch=coordinator_epoch,
                                                      lease_epoch=objective_lease_epoch, timestamp=now_iso())
                max_attempts = min(max_attempts, json.loads(objective["budget_json"])["attempt_limit"])
            if int(row["attempt"]) != expected_attempt:
                raise StateConflictError("planning retry attempt is stale")
            if connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (row["task_id"],)).fetchone():
                raise StateConflictError("planning already materialized a task")
            if row["state"] == "pending":
                return self._planning_request_row(row)
            if row["state"] != "failed":
                raise StateConflictError("only a failed planning reservation may retry")
            request = json.loads(row["request_json"])
            request_limit = request.get("retry_limit", 3)
            if (isinstance(request_limit, bool) or not isinstance(request_limit, int)
                    or expected_attempt >= min(max_attempts, request_limit)):
                raise StateConflictError("planning retry budget exhausted")
            timestamp = now_iso()
            connection.execute("UPDATE planning_requests SET state = 'pending', coordinator_epoch = 0, updated_at = ? WHERE command_id = ?",
                               (timestamp, command_id))
            self._event(connection, "planning_request.retry_queued", row["task_id"], None,
                        {"command_id": command_id, "attempt": expected_attempt, "request_hash": row["request_hash"],
                         "reason": reason, "previous_error": row["error"]}, created_at=timestamp)
            retried = connection.execute("SELECT * FROM planning_requests WHERE command_id = ?", (command_id,)).fetchone()
            return self._planning_request_row(retried)

    def enqueue_planning_request(
        self,
        command_id: str,
        task_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Durably enqueue one planning request with command-id idempotency."""

        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("planning command_id is required")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("planning task_id is required")
        if not isinstance(request, dict):
            raise ValueError("planning request must be a JSON object")

        # Session history is already durably content-addressed in
        # ``context_import_receipts``. The planning ledger keeps only the
        # reference, never a second plaintext copy.
        frozen_request = dict(request)
        frozen_request.pop("context_excerpt", None)
        request_json = canonical_json(frozen_request)
        request_hash = canonical_hash({"task_id": task_id, "request": frozen_request})
        timestamp = now_iso()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise CommandConflictError(
                        f"planning command {command_id!r} was already used with a different request"
                    )
                return self._planning_request_row(existing)

            task = connection.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is not None:
                raise StateConflictError(f"planning task {task_id!r} already exists")

            receipt = connection.execute(
                "SELECT task_id FROM command_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if receipt is not None:
                raise CommandConflictError(
                    f"planning command {command_id!r} already belongs to task {receipt['task_id']!r}"
                )

            reservation = connection.execute(
                "SELECT command_id FROM planning_requests WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if reservation is not None:
                raise CommandConflictError(
                    f"planning task {task_id!r} is already reserved by command "
                    f"{reservation['command_id']!r}"
                )

            connection.execute(
                """
                INSERT INTO planning_requests(
                    command_id, request_hash, task_id, request_json, state,
                    attempt, coordinator_epoch, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                """,
                (command_id, request_hash, task_id, request_json, timestamp, timestamp),
            )
            self._event(
                connection,
                "planning_request.enqueued",
                task_id,
                None,
                {
                    "command_id": command_id,
                    "request_hash": request_hash,
                    "state": "pending",
                    "attempt": 0,
                },
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            assert row is not None
            return self._planning_request_row(row)

    def get_planning_request(self, command_id: str) -> dict[str, Any]:
        """Return one durable planning request or raise ``KeyError``."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            return self._planning_request_row(row)

    def claim_planning_request(self, coordinator_epoch: int) -> dict[str, Any] | None:
        """Claim the oldest pending planning request under the active epoch."""

        if isinstance(coordinator_epoch, bool) or not isinstance(coordinator_epoch, int):
            raise ValueError("planning coordinator_epoch must be an integer")
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            if self._deployment_admission_gate(connection) is not None:
                return None
            row = connection.execute(
                """
                SELECT * FROM planning_requests
                WHERE state = 'pending'
                ORDER BY created_at, command_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            attempt = int(row["attempt"]) + 1
            changed = connection.execute(
                """
                UPDATE planning_requests
                SET state = 'running', attempt = ?, coordinator_epoch = ?,
                    started_at = ?, settled_at = NULL, result_json = NULL,
                    error = NULL, updated_at = ?
                WHERE command_id = ? AND state = 'pending' AND attempt = ?
                """,
                (
                    attempt,
                    coordinator_epoch,
                    timestamp,
                    timestamp,
                    row["command_id"],
                    row["attempt"],
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("planning request claim compare-and-set failed")
            self._event(
                connection,
                "planning_request.claimed",
                row["task_id"],
                None,
                {
                    "command_id": row["command_id"],
                    "request_hash": row["request_hash"],
                    "state": "running",
                    "attempt": attempt,
                    "coordinator_epoch": coordinator_epoch,
                },
                created_at=timestamp,
            )
            claimed = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (row["command_id"],),
            ).fetchone()
            assert claimed is not None
            return {**self._planning_request_row(claimed), "previous_error": row["error"]}

    def complete_planning_request(
        self,
        command_id: str,
        attempt: int,
        coordinator_epoch: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Settle a running planning request successfully with fencing."""

        if not isinstance(result, dict):
            raise ValueError("planning result must be a JSON object")
        result_json = canonical_json(result)
        return self._settle_planning_request(
            command_id,
            attempt,
            coordinator_epoch,
            state="succeeded",
            result_json=result_json,
            error=None,
        )

    def fail_planning_request(
        self,
        command_id: str,
        attempt: int,
        coordinator_epoch: int,
        error: str,
    ) -> dict[str, Any]:
        """Settle a running planning request as failed with fencing."""

        if not isinstance(error, str) or not error.strip():
            raise ValueError("planning error is required")
        return self._settle_planning_request(
            command_id,
            attempt,
            coordinator_epoch,
            state="failed",
            result_json=None,
            error=error,
        )

    def _settle_planning_request(
        self,
        command_id: str,
        attempt: int,
        coordinator_epoch: int,
        *,
        state: str,
        result_json: str | None,
        error: str | None,
    ) -> dict[str, Any]:
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("planning attempt must be a positive integer")
        if isinstance(coordinator_epoch, bool) or not isinstance(coordinator_epoch, int):
            raise ValueError("planning coordinator_epoch must be an integer")
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            row = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            if int(row["attempt"]) != attempt:
                raise StateConflictError(
                    f"planning request {command_id!r} has attempt {row['attempt']}, expected {attempt}"
                )
            if int(row["coordinator_epoch"]) != coordinator_epoch:
                raise StateConflictError(
                    f"planning request {command_id!r} has coordinator epoch "
                    f"{row['coordinator_epoch']}, expected {coordinator_epoch}"
                )
            if row["state"] != "running":
                if (
                    row["state"] == state
                    and row["result_json"] == result_json
                    and row["error"] == error
                ):
                    return self._planning_request_row(row)
                raise StateConflictError(
                    f"planning request {command_id!r} is {row['state']}, expected running"
                )
            changed = connection.execute(
                """
                UPDATE planning_requests
                SET state = ?, result_json = ?, error = ?, settled_at = ?, updated_at = ?
                WHERE command_id = ? AND state = 'running'
                  AND attempt = ? AND coordinator_epoch = ?
                """,
                (
                    state,
                    result_json,
                    error,
                    timestamp,
                    timestamp,
                    command_id,
                    attempt,
                    coordinator_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("planning request settlement compare-and-set failed")
            self._event(
                connection,
                f"planning_request.{state}",
                row["task_id"],
                None,
                {
                    "command_id": command_id,
                    "request_hash": row["request_hash"],
                    "state": state,
                    "attempt": attempt,
                    "coordinator_epoch": coordinator_epoch,
                },
                created_at=timestamp,
            )
            settled = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            assert settled is not None
            return self._planning_request_row(settled)

    def recover_interrupted_planning_requests(self) -> int:
        """Fence all running planning requests as indeterminate after interruption."""

        timestamp = now_iso()
        error = "planning request interrupted before settlement; explicit resolution required"
        recovered = 0
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM planning_requests WHERE state = 'running' ORDER BY created_at, command_id"
            ).fetchall()
            for row in rows:
                changed = connection.execute(
                    """
                    UPDATE planning_requests
                    SET state = 'indeterminate', error = ?, settled_at = ?, updated_at = ?
                    WHERE command_id = ? AND state = 'running'
                    """,
                    (error, timestamp, timestamp, row["command_id"]),
                ).rowcount
                if changed != 1:
                    raise StateConflictError(
                        f"planning request {row['command_id']!r} recovery compare-and-set failed"
                    )
                self._event(
                    connection,
                    "planning_request.interrupted",
                    row["task_id"],
                    None,
                    {
                        "command_id": row["command_id"],
                        "request_hash": row["request_hash"],
                        "state": "indeterminate",
                        "attempt": int(row["attempt"]),
                        "coordinator_epoch": int(row["coordinator_epoch"]),
                        "reason": "coordinator_restart",
                    },
                    created_at=timestamp,
                )
                recovered += 1
        return recovered

    @staticmethod
    def _planning_request_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "command_id": row["command_id"],
            "request_hash": row["request_hash"],
            "task_id": row["task_id"],
            "request": json.loads(row["request_json"]),
            "state": row["state"],
            "attempt": int(row["attempt"]),
            "coordinator_epoch": int(row["coordinator_epoch"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "started_at": row["started_at"],
            "settled_at": row["settled_at"],
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

    def _prepare_task_materialization(
        self,
        contract: TaskContract,
        nodes: list[NodeSpec],
    ) -> dict[str, Any]:
        """Validate one task graph and freeze all durable values before locking.

        Planner-controlled validation and hashing intentionally finish before a
        writer transaction begins. Persistence then contains only SQLite work,
        so model, filesystem, and other slow operations never hold the writer
        lock.
        """

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

        contract_document = contract.to_dict()
        node_documents = [node.to_dict() for node in nodes]
        return {
            "contract_json": canonical_json(contract_document),
            "contract_hash": contract.digest,
            "node_rows": tuple(
                (
                    node.node_id,
                    node.ordinal,
                    canonical_json(document),
                )
                for node, document in zip(nodes, node_documents, strict=True)
            ),
            "request_hash": canonical_hash(
                {"contract": contract_document, "nodes": node_documents}
            ),
        }

    def create_task(
        self,
        contract: TaskContract,
        nodes: list[NodeSpec],
        command_id: str,
    ) -> str:
        prepared = self._prepare_task_materialization(contract, nodes)
        timestamp = now_iso()
        with self.transaction() as connection:
            planning_reservation = connection.execute(
                """
                SELECT command_id, task_id, state FROM planning_requests
                WHERE (task_id = ? OR command_id = ?)
                  AND state IN ('pending', 'running', 'succeeded', 'failed', 'indeterminate')
                ORDER BY command_id
                LIMIT 1
                """,
                (contract.task_id, command_id),
            ).fetchone()
            if planning_reservation is not None:
                raise CommandConflictError(
                    f"planning request {planning_reservation['command_id']!r} "
                    f"in state {planning_reservation['state']!r} reserves task "
                    f"{planning_reservation['task_id']!r}"
                )
            receipt = connection.execute(
                "SELECT request_hash, task_id FROM command_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != prepared["request_hash"]:
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
                    prepared["contract_json"],
                    prepared["contract_hash"],
                    timestamp,
                    timestamp,
                ),
            )
            for node_id, ordinal, spec_json in sorted(
                prepared["node_rows"], key=lambda item: (item[1], item[0])
            ):
                connection.execute(
                    """
                    INSERT INTO nodes(task_id, node_id, spec_json, state, updated_at)
                    VALUES(?, ?, ?, 'pending', ?)
                    """,
                    (contract.task_id, node_id, spec_json, timestamp),
                )
            connection.execute(
                """
                INSERT INTO command_receipts(command_id, request_hash, task_id, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (command_id, prepared["request_hash"], contract.task_id, timestamp),
            )
            self._event(
                connection,
                "task.created",
                contract.task_id,
                None,
                {"contract_hash": prepared["contract_hash"], "node_count": len(nodes)},
                created_at=timestamp,
            )
        return contract.task_id

    def materialize_planning_request(
        self,
        command_id: str,
        *,
        attempt: int,
        coordinator_epoch: int,
        contract: TaskContract,
        nodes: list[NodeSpec],
        result: dict,
        queue: bool,
        source_thread_id: str | None,
    ) -> dict:
        """Atomically persist one fenced planning result and its executable task.

        A successful return proves the planning receipt, task graph, optional
        queue transition, and optional session binding committed together. A
        failure leaves the claimed request running, so recovery marks it
        indeterminate rather than replaying a potentially materialized plan.
        """

        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("planning command_id is required")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("planning attempt must be a positive integer")
        if isinstance(coordinator_epoch, bool) or not isinstance(coordinator_epoch, int):
            raise ValueError("planning coordinator_epoch must be an integer")
        if not isinstance(result, dict):
            raise ValueError("planning result must be a JSON object")
        if not isinstance(queue, bool):
            raise ValueError("planning queue must be a boolean")
        if source_thread_id is not None and (
            not isinstance(source_thread_id, str)
            or not source_thread_id.strip()
            or any(character.isspace() for character in source_thread_id)
        ):
            raise ValueError("planning source_thread_id must be non-empty and contain no whitespace")
        if source_thread_id != contract.source_thread_id:
            raise ValueError("planning source_thread_id must match the task contract")

        # All planner-controlled validation and hash calculation happens
        # before the durable fencing transaction begins.
        prepared = self._prepare_task_materialization(contract, nodes)
        result_json = canonical_json(result)
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            planning = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if planning is None:
                raise KeyError(command_id)
            if planning["task_id"] != contract.task_id:
                raise StateConflictError(
                    f"planning request {command_id!r} belongs to task "
                    f"{planning['task_id']!r}, not {contract.task_id!r}"
                )
            if int(planning["attempt"]) != attempt:
                raise StateConflictError(
                    f"planning request {command_id!r} has attempt {planning['attempt']}, "
                    f"expected {attempt}"
                )
            if int(planning["coordinator_epoch"]) != coordinator_epoch:
                raise StateConflictError(
                    f"planning request {command_id!r} has coordinator epoch "
                    f"{planning['coordinator_epoch']}, expected {coordinator_epoch}"
                )

            if planning["state"] == "succeeded":
                if planning["result_json"] != result_json:
                    raise CommandConflictError(
                        f"planning request {command_id!r} already succeeded with a different result"
                    )
                task = connection.execute(
                    "SELECT state, state_revision, contract_hash FROM tasks WHERE task_id = ?",
                    (contract.task_id,),
                ).fetchone()
                receipt = connection.execute(
                    "SELECT request_hash, task_id FROM command_receipts WHERE command_id = ?",
                    (command_id,),
                ).fetchone()
                if (
                    task is None
                    or task["contract_hash"] != prepared["contract_hash"]
                    or receipt is None
                    or receipt["task_id"] != contract.task_id
                    or receipt["request_hash"] != prepared["request_hash"]
                ):
                    raise StateConflictError(
                        "succeeded planning request has incomplete or inconsistent materialization"
                    )
                return {
                    **self._planning_request_row(planning),
                    "task_state": task["state"],
                    "task_revision": int(task["state_revision"]),
                }
            if planning["state"] != "running":
                raise StateConflictError(
                    f"planning request {command_id!r} is {planning['state']}, expected running"
                )

            existing_task = connection.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?",
                (contract.task_id,),
            ).fetchone()
            if existing_task is not None:
                raise StateConflictError(
                    f"planning materialization task {contract.task_id!r} already exists"
                )
            existing_receipt = connection.execute(
                "SELECT task_id FROM command_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing_receipt is not None:
                raise CommandConflictError(
                    f"planning materialization command {command_id!r} already belongs to task "
                    f"{existing_receipt['task_id']!r}"
                )

            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, contract_json, contract_hash, state,
                    state_revision, created_at, updated_at
                ) VALUES(?, ?, ?, 'inbox', 1, ?, ?)
                """,
                (
                    contract.task_id,
                    prepared["contract_json"],
                    prepared["contract_hash"],
                    timestamp,
                    timestamp,
                ),
            )
            for node_id, ordinal, spec_json in sorted(
                prepared["node_rows"], key=lambda item: (item[1], item[0])
            ):
                connection.execute(
                    """
                    INSERT INTO nodes(task_id, node_id, spec_json, state, updated_at)
                    VALUES(?, ?, ?, 'pending', ?)
                    """,
                    (contract.task_id, node_id, spec_json, timestamp),
                )
            connection.execute(
                """
                INSERT INTO command_receipts(command_id, request_hash, task_id, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (command_id, prepared["request_hash"], contract.task_id, timestamp),
            )
            self._event(
                connection,
                "task.created",
                contract.task_id,
                None,
                {"contract_hash": prepared["contract_hash"], "node_count": len(nodes)},
                created_at=timestamp,
            )

            task_state = "inbox"
            task_revision = 1
            if queue:
                task_state = "queued"
                task_revision = 2
                changed = connection.execute(
                    """
                    UPDATE tasks
                    SET state = 'queued', state_revision = ?, updated_at = ?, blocker = NULL, verdict = NULL
                    WHERE task_id = ? AND state = 'inbox' AND state_revision = 1
                    """,
                    (task_revision, timestamp, contract.task_id),
                ).rowcount
                if changed != 1:
                    raise StateConflictError("planning task queue compare-and-set failed")
                self._event(
                    connection,
                    "task.state_changed",
                    contract.task_id,
                    None,
                    {"from": "inbox", "to": "queued", "revision": task_revision, "blocker": None},
                    created_at=timestamp,
                )

            if source_thread_id is not None:
                binding = connection.execute(
                    "SELECT context_ref FROM session_bindings WHERE source_thread_id = ?",
                    (source_thread_id,),
                ).fetchone()
                if binding is None:
                    raise StateConflictError(
                        f"planning session binding {source_thread_id!r} does not exist"
                    )
                if binding["context_ref"] != contract.context_bundle_ref:
                    raise StateConflictError(
                        "planning session binding context_ref does not match the task contract"
                    )
                context_receipt = connection.execute(
                    """
                    SELECT command_id FROM context_import_receipts
                    WHERE source_thread_id = ? AND context_ref = ?
                    ORDER BY created_at DESC, command_id DESC
                    LIMIT 1
                    """,
                    (source_thread_id, contract.context_bundle_ref),
                ).fetchone()
                if context_receipt is None:
                    raise StateConflictError(
                        "planning session binding points to a missing frozen context receipt"
                    )
                changed = connection.execute(
                    """
                    UPDATE session_bindings
                    SET active_task_id = ?, updated_at = ?
                    WHERE source_thread_id = ? AND context_ref = ?
                    """,
                    (contract.task_id, timestamp, source_thread_id, contract.context_bundle_ref),
                ).rowcount
                if changed != 1:
                    raise StateConflictError("planning session binding compare-and-set failed")
                self._event(
                    connection,
                    "context.task_bound",
                    contract.task_id,
                    None,
                    {
                        "source_thread_id": source_thread_id,
                        "context_ref": contract.context_bundle_ref,
                    },
                    created_at=timestamp,
                )

            changed = connection.execute(
                """
                UPDATE planning_requests
                SET state = 'succeeded', result_json = ?, error = NULL,
                    settled_at = ?, updated_at = ?
                WHERE command_id = ? AND state = 'running'
                  AND attempt = ? AND coordinator_epoch = ?
                """,
                (
                    result_json,
                    timestamp,
                    timestamp,
                    command_id,
                    attempt,
                    coordinator_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("planning request materialization compare-and-set failed")
            self._event(
                connection,
                "planning_request.succeeded",
                contract.task_id,
                None,
                {
                    "command_id": command_id,
                    "request_hash": planning["request_hash"],
                    "state": "succeeded",
                    "attempt": attempt,
                    "coordinator_epoch": coordinator_epoch,
                    "contract_hash": prepared["contract_hash"],
                    "task_state": task_state,
                    "task_revision": task_revision,
                },
                created_at=timestamp,
            )
            settled = connection.execute(
                "SELECT * FROM planning_requests WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            assert settled is not None
            return {
                **self._planning_request_row(settled),
                "task_state": task_state,
                "task_revision": task_revision,
            }

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

    def queue_task(
        self,
        task_id: str,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """Atomically reset retryable failed nodes and queue a task.

        A failed worker can retain a dirty physical worktree even though its
        logical node is reset to ``pending``.  The reset therefore records a
        sealed, DB-only recovery binding for every physical failed attempt;
        the coordinator captures and restores that binding outside SQLite
        before it can dispatch the retry.  No filesystem inspection belongs in
        this transaction.
        """

        return int(
            self._queue_task(
                task_id,
                expected_revision=expected_revision,
                instruction=None,
            )["revision"]
        )

    def queue_task_with_instruction(
        self,
        task_id: str,
        instruction: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Validate, persist steering, and queue in one durable transition.

        MCP control uses this instead of separately appending instruction and
        queueing.  Invalid instruction text or a stale supplied revision leaves
        the task and every node untouched, so it cannot accidentally launch a
        reset retry.
        """

        normalized = self._normalize_steering_instruction(instruction)
        return self._queue_task(
            task_id,
            expected_revision=expected_revision,
            instruction=normalized,
        )

    def _queue_task(
        self,
        task_id: str,
        *,
        expected_revision: int | None,
        instruction: str | None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if expected_revision is not None and int(task["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected task revision {expected_revision}, found {task['state_revision']}"
                )
            if task["state"] not in {"inbox", "planning", "ready", "needs_fix", "paused"}:
                raise StateConflictError(f"cannot queue task from {task['state']}")

            steering_receipt: dict[str, Any] | None = None
            if instruction is not None:
                steering_receipt = self._append_task_steering(
                    connection,
                    task_id,
                    instruction,
                    expected_revision=expected_revision,
                    scheduled_for="next_attempt",
                )
                task = connection.execute(
                    "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                assert task is not None

            current_revision = int(task["state_revision"])
            recovery_by_node: dict[str, str] = {}
            if task["state"] == "needs_fix":
                failed_nodes = connection.execute(
                    "SELECT * FROM nodes WHERE task_id = ? AND state = 'failed' ORDER BY node_id",
                    (task_id,),
                ).fetchall()
                next_revision = current_revision + 1
                for node in failed_nodes:
                    authorization = self._failed_attempt_recovery_authorization(
                        connection,
                        task_id,
                        node,
                        authorization_revision=next_revision,
                    )
                    if authorization is not None:
                        recovery_by_node[str(node["node_id"])] = canonical_json(authorization)

                for node in failed_nodes:
                    node_id = str(node["node_id"])
                    if node_id in recovery_by_node:
                        connection.execute(
                            """
                            UPDATE nodes
                            SET state = 'pending', worker_id = NULL, worktree = NULL,
                                started_at = NULL, settled_at = NULL, result_json = NULL,
                                coordinator_epoch = 0, lease_epoch = 0, recovery_json = ?, updated_at = ?
                            WHERE task_id = ? AND node_id = ? AND state = 'failed'
                            """,
                            (recovery_by_node[node_id], timestamp, task_id, node_id),
                        )
                    else:
                        # Preserve legacy retry provenance when there is no
                        # owned physical source to capture.
                        connection.execute(
                            """
                            UPDATE nodes
                            SET state = 'pending', worker_id = NULL,
                                started_at = NULL, settled_at = NULL, updated_at = ?
                            WHERE task_id = ? AND node_id = ? AND state = 'failed'
                            """,
                            (timestamp, task_id, node_id),
                        )

            revision = current_revision + 1
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = 'queued', state_revision = ?, updated_at = ?, blocker = NULL, verdict = NULL
                WHERE task_id = ? AND state_revision = ?
                """,
                (revision, timestamp, task_id, current_revision),
            ).rowcount
            if changed != 1:
                raise StateConflictError("task queue compare-and-set failed")
            self._event(
                connection,
                "task.state_changed",
                task_id,
                None,
                {
                    "from": task["state"],
                    "to": "queued",
                    "revision": revision,
                    "blocker": None,
                },
                created_at=timestamp,
            )
            for node_id, raw in recovery_by_node.items():
                authorization = json.loads(raw)
                self._event(
                    connection,
                    "node.failed_attempt_recovery_queued",
                    task_id,
                    node_id,
                    {
                        "source_attempt": authorization["source"]["attempt"],
                        "next_attempt": authorization["source"]["attempt"] + 1,
                        "source_allocation_id": authorization["source_allocation_id"],
                        "expected_changed_paths": authorization["source"]["changed_paths"],
                        "task_revision": revision,
                    },
                    created_at=timestamp,
                )
            return {
                "task_id": task_id,
                "state": "queued",
                "revision": revision,
                **({"steering": steering_receipt} if steering_receipt is not None else {}),
            }

    @staticmethod
    def _failed_attempt_recovery_authorization(
        connection: sqlite3.Connection,
        task_id: str,
        node: sqlite3.Row,
        *,
        authorization_revision: int,
        source_result_json: str | None = None,
    ) -> dict[str, Any] | None:
        """Build a filesystem-free retry binding for one failed allocation.

        A failed node without a physical allocation cannot have left an owned
        dirty worktree.  A node with one is always inspected by the service on
        retry, even when its result reported no changed paths, so an omitted
        change list cannot silently discard a source modification.
        """

        try:
            source_result_json = source_result_json or str(node["result_json"])
            result = json.loads(source_result_json)
            spec = json.loads(str(node["spec_json"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise StateConflictError("failed node recovery receipt is invalid JSON") from error
        if not isinstance(result, dict) or result.get("status") not in {"failed", "blocked"}:
            raise StateConflictError("retryable node lacks a failed or blocked result receipt")
        if not isinstance(spec, dict):
            raise StateConflictError("failed node specification is invalid")
        changed_paths = result.get("changed_paths")
        if (
            not isinstance(changed_paths, list)
            or not all(isinstance(path, str) and path for path in changed_paths)
            or tuple(changed_paths) != tuple(sorted(set(changed_paths)))
        ):
            raise StateConflictError("failed node changed_paths are not a canonical explicit list")
        recoverable_paths, generated_residue_paths = partition_recovery_paths(
            tuple(changed_paths)
        )

        allocation = connection.execute(
            """
            SELECT * FROM worktree_allocations
            WHERE task_id = ? AND node_id = ? AND attempt = ?
            """,
            (task_id, node["node_id"], node["attempt"]),
        ).fetchone()
        if allocation is None:
            if node["worktree"] is not None:
                raise StateConflictError(
                    "failed node worktree has no physical allocation to recover"
                )
            if changed_paths:
                raise StateConflictError(
                    "failed node reports changes but has no physical worktree allocation to recover"
                )
            return None
        if allocation["state"] != "active":
            raise StateConflictError("failed node recovery source allocation is not active")
        if not isinstance(node["worktree"], str) or allocation["current_path"] != node["worktree"]:
            raise StateConflictError("failed node recovery worktree does not match its allocation")
        expected_branch = WorktreeManager.branch_name(task_id, str(node["node_id"]), int(node["attempt"]))
        if allocation["branch"] != expected_branch:
            raise StateConflictError("failed node recovery allocation branch does not match its attempt")

        task = connection.execute(
            "SELECT contract_json FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise KeyError(task_id)
        try:
            contract = json.loads(str(task["contract_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("failed node task contract is invalid JSON") from error
        if not isinstance(contract, dict):
            raise StateConflictError("failed node task contract is invalid")
        if spec.get("verifier"):
            if changed_paths:
                raise StateConflictError("failed verifier worktree changes cannot be resumed as worker input")
            return None
        allowed_scope = contract.get("allowed_scope")
        forbidden_scope = contract.get("forbidden_scope")
        write_scopes = spec.get("write_scopes")
        if not (
            isinstance(allowed_scope, list)
            and all(isinstance(scope, str) for scope in allowed_scope)
            and isinstance(forbidden_scope, list)
            and all(isinstance(scope, str) for scope in forbidden_scope)
            and isinstance(write_scopes, list)
            and all(isinstance(scope, str) for scope in write_scopes)
        ):
            raise StateConflictError("failed node recovery scopes are invalid")
        for relative_path in recoverable_paths:
            try:
                task_allowed = scope_allows(relative_path, allowed_scope, forbidden_scope)
                node_allowed = scope_allows(relative_path, write_scopes, [])
            except ValueError as error:
                raise StateConflictError(
                    f"failed node changed path is invalid for recovery: {relative_path!r}"
                ) from error
            if not task_allowed:
                raise StateConflictError(
                    f"failed node recovery path is outside task scope: {relative_path}"
                )
            if not node_allowed:
                raise StateConflictError(
                    f"failed node recovery path is outside node write scope: {relative_path}"
                )
        base_sha = contract.get("base_sha")
        if not isinstance(base_sha, str) or not base_sha or allocation["base_sha"] != base_sha:
            raise StateConflictError("failed node recovery allocation base does not match its contract")
        return {
            "schema_version": 1,
            "kind": _FAILED_ATTEMPT_RECOVERY_KIND,
            "state": "capture_pending",
            "authorization_revision": authorization_revision,
            "source_allocation_id": str(allocation["allocation_id"]),
            "source_result_json": source_result_json,
            "source": {
                "attempt": int(node["attempt"]),
                "worktree": str(allocation["current_path"]),
                "branch": str(allocation["branch"]),
                "base_sha": base_sha,
                "changed_paths": list(recoverable_paths),
                "generated_residue_paths": list(generated_residue_paths),
            },
        }

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
        recoverable_paths, generated_residue_paths = partition_recovery_paths(
            tuple(sorted(set(changed_paths)))
        )
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
                "changed_paths": recoverable_paths,
                "generated_residue_paths": generated_residue_paths,
                "allocation_id": str(allocation["allocation_id"]),
                "repository": str(contract["repository"]),
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

    def capture_and_resume_blocked_worktree(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
        reason: str,
        preserve_untracked: bool = False,
        expected_checkpoint_sha: str | None = None,
    ) -> dict[str, Any]:
        """Capture a blocked attempt outside SQLite, then authorize its retry."""

        reason = reason.strip()
        if not reason:
            raise ValueError("dirty-worktree recovery reason must be non-empty")
        candidate = self.blocked_worktree_recovery_candidate(
            task_id,
            node_id,
            expected_revision=expected_revision,
            expected_attempt=expected_attempt,
        )
        source = candidate["source"]
        task = self.get_task(task_id)
        contract = task.get("contract")
        if not isinstance(contract, dict) or not isinstance(contract.get("repository"), str):
            raise StateConflictError("blocked-worktree recovery task contract is invalid")
        base_sha = str(source["base_sha"])
        nodes = task.get("nodes")
        if not isinstance(nodes, list):
            raise StateConflictError("blocked-worktree recovery task nodes are invalid")
        node = next(
            (
                item
                for item in nodes
                if isinstance(item, dict) and item.get("node_id") == node_id
            ),
            None,
        )
        if not isinstance(node, dict) or not isinstance(node.get("result"), dict):
            raise StateConflictError("blocked-worktree recovery source result is invalid")
        raw_artifacts = node["result"].get("artifacts")
        if not isinstance(raw_artifacts, dict):
            raise StateConflictError("blocked-worktree recovery source artifacts are invalid")

        capture_kwargs: dict[str, object] = {}
        dependency_input_ref = raw_artifacts.get("dependency-input")
        if dependency_input_ref is not None:
            if not isinstance(dependency_input_ref, str) or not dependency_input_ref:
                raise StateConflictError(
                    "blocked-worktree recovery dependency input ref is invalid"
                )
            dependency_input = load_recorded_dependency_input(
                self.artifacts,
                dependency_input_ref,
                task_id=task_id,
                node_id=node_id,
                base_sha=base_sha,
            )
            capture_kwargs = {
                "task_id": task_id,
                "node_id": node_id,
                "input_tree_sha": dependency_input.input_tree_sha,
                "dependency_input_ref": dependency_input_ref,
            }

        if preserve_untracked:
            if dependency_input_ref is None:
                raise StateConflictError(
                    "explicit untracked preservation requires a dependent blocked worker"
                )
            source_path = Path(str(source["worktree"])).expanduser().resolve(strict=True)
            untracked_paths = DirtyWorktreeRecovery.untracked_paths(source_path)
            if not untracked_paths:
                raise StateConflictError(
                    "explicit untracked preservation requested but source has no untracked files"
                )
            allowed_scope = contract.get("allowed_scope")
            forbidden_scope = contract.get("forbidden_scope")
            write_scopes = node.get("write_scopes")
            if not (
                isinstance(allowed_scope, list)
                and all(isinstance(scope, str) for scope in allowed_scope)
                and isinstance(forbidden_scope, list)
                and all(isinstance(scope, str) for scope in forbidden_scope)
                and isinstance(write_scopes, list)
                and all(isinstance(scope, str) for scope in write_scopes)
            ):
                raise StateConflictError("blocked-worktree recovery scopes are invalid")
            for relative_path in untracked_paths:
                if not scope_allows(relative_path, allowed_scope, forbidden_scope):
                    raise StateConflictError(
                        "untracked recovery path is outside the task contract scope: "
                        + relative_path
                    )
                if not scope_allows(relative_path, write_scopes, []):
                    raise StateConflictError(
                        "untracked recovery path is outside the blocked node write scope: "
                        + relative_path
                    )
            capture_kwargs["preserve_untracked_paths"] = untracked_paths

        recovery = DirtyWorktreeRecovery(
            self.artifacts,
            WorktreeManager(self.path.parent / "worktrees"),
        ).capture(
            repository=contract["repository"],
            base_sha=base_sha,
            worktree=str(source["worktree"]),
            branch=str(source["branch"]),
            attempt=expected_attempt,
            expected_changed_paths=tuple(source["changed_paths"]),
            expected_checkpoint_sha=expected_checkpoint_sha,
            expected_generated_residue_paths=tuple(
                source.get("generated_residue_paths", ())
            ),
            **capture_kwargs,
        )
        return {
            **self.resume_blocked_worktree(
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
                reason=reason,
                recovery=recovery,
            ),
            "recovery": recovery,
        }

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
        *,
        verify_filesystem: bool = True,
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
        if "source_checkpoint_sha" in recovery:
            checkpoint = recovery["source_checkpoint_sha"]
            if not isinstance(checkpoint, str) or not re.fullmatch(r"[0-9a-f]{40}", checkpoint):
                raise StateConflictError("checkpoint requires an exact full commit SHA")
            common.add("source_checkpoint_sha")
        if schema_version == 1:
            required = common
        elif schema_version in {2, 3, 5, 6}:
            required = common | {
                "source_task_id",
                "source_node_id",
                "input_tree_sha",
                "dependency_input_ref",
            }
            if schema_version in {3, 6}:
                required.add("untracked_paths")
            if schema_version in {5, 6}:
                required |= {"generated_residue_paths", "generated_residue_ref"}
        elif schema_version == 4:
            required = common | {"generated_residue_paths", "generated_residue_ref"}
        else:
            raise StateConflictError("dirty-worktree recovery receipt schema is unsupported")
        if set(recovery) != required:
            raise StateConflictError("dirty-worktree recovery receipt has an invalid shape")
        source = candidate["source"]
        for relative_path in source["changed_paths"] if "source_checkpoint_sha" in recovery else ():
            if not scope_allows(relative_path, candidate["task"]["allowed_scope"],
                                candidate["task"]["forbidden_scope"]):
                raise StateConflictError("recovery path is outside the task contract scope: " + relative_path)
            if not scope_allows(relative_path, candidate["node"]["write_scopes"], []):
                raise StateConflictError("recovery path is outside the blocked node write scope: " + relative_path)
        if (
            recovery["source_attempt"] != expected_attempt
            or recovery["source_branch"] != source["branch"]
            or recovery["base_sha"] != source["base_sha"]
            or tuple(sorted(recovery["changed_paths"])) != source["changed_paths"]
        ):
            raise StateConflictError(
                "dirty-worktree recovery receipt does not match the blocked allocation"
            )
        source_residue = source.get("generated_residue_paths", ())
        if source_residue:
            if (
                schema_version not in {4, 5, 6}
                or tuple(recovery.get("generated_residue_paths", ())) != source_residue
                or not isinstance(recovery.get("generated_residue_ref"), str)
                or not recovery["generated_residue_ref"]
            ):
                raise StateConflictError(
                    "dirty-worktree recovery receipt does not bind generated residue"
                )
        elif schema_version in {4, 5, 6}:
            raise StateConflictError(
                "dirty-worktree recovery receipt has unexpected generated residue"
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
        if schema_version in {2, 3, 5, 6}:
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
        if schema_version in {3, 6}:
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
        if not verify_filesystem:
            return dict(recovery)
        try:
            source_path = Path(str(source["worktree"])).expanduser().resolve(strict=True)
            receipt_source = Path(str(recovery["source_worktree"])).expanduser().resolve(strict=True)
            artifacts = ArtifactStore(self.path.parent / "artifacts")
            patch = artifacts.verify(str(recovery["patch_ref"])).read_bytes()
            if schema_version in {4, 5, 6}:
                artifacts.verify(str(recovery["generated_residue_ref"]))
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
        checkpoint = recovery.get("source_checkpoint_sha")
        if checkpoint is not None:
            DirtyWorktreeRecovery(self.artifacts, WorktreeManager(self.path.parent / "worktrees")).validate_retry_source(
                repository=str(source["repository"]), base_sha=base_sha,
                worktree=str(source_path), branch=str(source["branch"]),
                expected_checkpoint_sha=checkpoint,
            )
        if self._recovery_git_bytes(source_path, "rev-parse", "HEAD").decode().strip() != (checkpoint or base_sha):
            raise StateConflictError("dirty-worktree recovery source no longer matches contract base")
        if self._recovery_git_bytes(source_path, "branch", "--show-current").decode().strip() != source["branch"]:
            raise StateConflictError("dirty-worktree recovery source no longer matches allocated branch")
        if actual_untracked != expected_untracked:
            raise StateConflictError("dirty-worktree recovery source untracked paths drifted")
        comparison_tree = base_sha
        if schema_version in {1, 4}:
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

        # Freeze and inspect the source/artifact before acquiring SQLite's
        # write lock. The following transaction repeats only durable shape and
        # revision checks, then CASes the authorization into place.
        with self.connection() as connection:
            preflight_candidate = self._blocked_worktree_recovery_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )
            preflight_capture = self._validate_blocked_worktree_recovery_capture(
                preflight_candidate,
                expected_attempt,
                recovery,
            )
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
                verify_filesystem=False,
            )
            if canonical_json(capture) != canonical_json(preflight_capture):
                raise StateConflictError("dirty-worktree recovery capture changed before authorization")
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
        return int(
            self.append_task_steering_receipt(
                task_id,
                instruction,
                expected_revision=expected_revision,
            )["revision"]
        )

    def append_task_steering_receipt(
        self,
        task_id: str,
        instruction: str,
        *,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """Persist steering and report only what can truthfully receive it."""

        normalized = self._normalize_steering_instruction(instruction)
        with self.transaction() as connection:
            return self._append_task_steering(
                connection,
                task_id,
                normalized,
                expected_revision=expected_revision,
            )

    @staticmethod
    def _normalize_steering_instruction(instruction: str) -> str:
        if not isinstance(instruction, str):
            raise ValueError("instruction must be a string")
        normalized = instruction.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("instruction must contain 1 to 500 characters")
        return normalized

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
        scheduled_for: str | None = None,
    ) -> dict[str, Any]:
        instruction = self._normalize_steering_instruction(instruction)
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
        running_attempts = [
            {"node_id": str(row["node_id"]), "attempt": int(row["attempt"])}
            for row in connection.execute(
                """
                SELECT node_id, attempt FROM nodes
                WHERE task_id = ? AND state = 'running'
                ORDER BY node_id
                """,
                (task_id,),
            ).fetchall()
        ]
        if scheduled_for is None:
            if running_attempts:
                scheduled_for = "future_attempt"
            elif task["state"] in {"queued", "running", "verifying"}:
                scheduled_for = "next_attempt"
            elif task["state"] in {"paused", "blocked", "needs_fix"}:
                scheduled_for = "after_resume"
            else:
                scheduled_for = "after_queue"
        return {
            "steering_id": steering_id,
            "revision": revision,
            "state": task["state"],
            "objective_preserved": True,
            # Executors receive their steering snapshot when claimed. A
            # message appended after a node is already running cannot be
            # truthfully claimed as delivered to that process.
            "delivery": {
                "status": "not_delivered",
                "mode": "future_attempts_only",
                "current_attempt_received": False,
                "current_running_attempt_delivered": False,
                "scheduled_for": scheduled_for,
                "running_attempts": running_attempts,
            },
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
                "SELECT state, worktree, recovery_json FROM nodes WHERE task_id = ? AND node_id = ?",
                (task_id, node_id),
            ).fetchone()
            if task is None or node is None:
                raise KeyError((task_id, node_id))
            if int(task["state_revision"]) != expected_revision:
                raise StateConflictError(
                    f"expected task revision {expected_revision}, found {task['state_revision']}"
                )
            if node["state"] != "indeterminate":
                raise StateConflictError(f"node {node_id} is {node['state']}, expected indeterminate")
            if resolution == "retry":
                self._assert_indeterminate_retry_is_safe(node)
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
                "SELECT state, worktree, recovery_json FROM nodes WHERE task_id = ? AND node_id = ?",
                (task_id, node_id),
            ).fetchone()
            if node is None:
                raise KeyError((task_id, node_id))
            if node["state"] != "indeterminate":
                raise StateConflictError(
                    f"node {node_id} is {node['state']}, expected indeterminate"
                )
            if decision == "retry":
                self._assert_indeterminate_retry_is_safe(node)
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
    def _assert_indeterminate_retry_is_safe(node: sqlite3.Row) -> None:
        """Reject retry when an indeterminate attempt owns recovery state."""

        if node["worktree"] is not None or node["recovery_json"] is not None:
            raise StateConflictError(
                "indeterminate retry requires explicit recovery because the attempt owns a worktree or recovery receipt"
            )

    def indeterminate_local_recovery_candidate(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
    ) -> dict[str, Any]:
        """Read-only shape for one owned-worktree indeterminate node."""

        with self.connection() as connection:
            return self._indeterminate_local_recovery_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )

    @staticmethod
    def _indeterminate_local_recovery_candidate(
        connection: sqlite3.Connection,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
    ) -> dict[str, Any]:
        """Read-only shape for one owned-worktree indeterminate node.

        Only a worker node left ``indeterminate`` with no pending
        ``recovery_json`` binding and an owned, physically active worktree
        allocation is eligible. A node whose target was never assigned (a
        ``capture_pending`` binding still on the row) already has a safe path
        through ``queue_task``.
        """

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
        if task["state"] != "needs_approval":
            raise StateConflictError(
                f"task {task_id} is {task['state']}, expected needs_approval"
            )
        if node["state"] != "indeterminate":
            raise StateConflictError(f"node {node_id} is {node['state']}, expected indeterminate")
        if node["recovery_json"] is not None:
            raise StateConflictError(
                "indeterminate node still has a pending recovery binding; resolve it through queue_task"
            )
        if not isinstance(node["worktree"], str) or not node["worktree"]:
            raise StateConflictError(
                "indeterminate node has no owned worktree to recover locally"
            )
        try:
            spec = json.loads(str(node["spec_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("indeterminate node specification is invalid JSON") from error
        if not isinstance(spec, dict):
            raise StateConflictError("indeterminate node specification is invalid")
        if spec.get("verifier"):
            raise StateConflictError(
                "local indeterminate recovery is only supported for worker nodes"
            )
        try:
            contract = json.loads(str(task["contract_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("indeterminate node task contract is invalid JSON") from error
        allowed_scope = contract.get("allowed_scope") if isinstance(contract, dict) else None
        forbidden_scope = contract.get("forbidden_scope") if isinstance(contract, dict) else None
        write_scopes = spec.get("write_scopes")
        depends_on = spec.get("depends_on", [])
        repository = contract.get("repository") if isinstance(contract, dict) else None
        base_sha = contract.get("base_sha") if isinstance(contract, dict) else None
        if not (
            isinstance(repository, str)
            and repository
            and isinstance(base_sha, str)
            and base_sha
            and isinstance(allowed_scope, list)
            and all(isinstance(scope, str) for scope in allowed_scope)
            and isinstance(forbidden_scope, list)
            and all(isinstance(scope, str) for scope in forbidden_scope)
            and isinstance(write_scopes, list)
            and all(isinstance(scope, str) for scope in write_scopes)
            and isinstance(depends_on, list)
            and all(isinstance(dependency, str) and dependency for dependency in depends_on)
        ):
            raise StateConflictError("local indeterminate recovery task contract or scopes are invalid")
        # This route is deliberately local-only: a crashed executor with
        # permission to mutate an external system can leave effects that a
        # worktree inspection cannot prove absent.  Do not turn the operator
        # assertion into a blind retry in that case.
        if (
            contract.get("external_write_permission") is not False
            or contract.get("destructive_action_permission") is not False
        ):
            raise StateConflictError(
                "local indeterminate recovery requires a contract with no external or destructive permissions"
            )
        allocation = connection.execute(
            """
            SELECT * FROM worktree_allocations
            WHERE task_id = ? AND node_id = ? AND attempt = ?
            """,
            (task_id, node_id, expected_attempt),
        ).fetchone()
        if allocation is None or allocation["state"] != "active":
            raise StateConflictError(
                "indeterminate node has no active physical worktree allocation to recover"
            )
        if allocation["current_path"] != node["worktree"]:
            raise StateConflictError(
                "indeterminate node worktree does not match its physical allocation"
            )
        expected_branch = WorktreeManager.branch_name(task_id, node_id, expected_attempt)
        if allocation["branch"] != expected_branch:
            raise StateConflictError(
                "indeterminate node allocation branch does not match its attempt"
            )
        if allocation["repository"] != repository or allocation["base_sha"] != base_sha:
            raise StateConflictError(
                "indeterminate node allocation does not match its task contract"
            )
        return {
            "task": {
                "task_id": task_id,
                "state": str(task["state"]),
                "revision": int(task["state_revision"]),
                "repository": repository,
                "base_sha": base_sha,
                "allowed_scope": tuple(allowed_scope),
                "forbidden_scope": tuple(forbidden_scope),
            },
            "node": {
                "node_id": node_id,
                "state": str(node["state"]),
                "attempt": int(node["attempt"]),
                "worktree": str(node["worktree"]),
                "branch": expected_branch,
                "write_scopes": tuple(write_scopes),
                "depends_on": tuple(depends_on),
            },
            "allocation_id": str(allocation["allocation_id"]),
        }

    def queue_indeterminate_local_recovery(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_revision: int,
        expected_attempt: int,
        reason: str,
        confirm_old_executor_ended: bool,
        confirm_effects_restricted_to_owned_files: bool,
        observed_changed_paths: tuple[str, ...],
        observed_generated_residue_paths: tuple[str, ...] = (),
        dependency_input_ref: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Authorize one indeterminate node's own worktree for capture-and-retry.

        The caller must already have proven, outside SQLite, that the
        previous executor process ended and that ``observed_changed_paths``
        is the exact and complete tracked/untracked change set of the owned
        worktree before asserting the two confirmation flags; this method
        performs no filesystem or Git inspection itself. Authorization
        reuses the identical ``failed-attempt-worktree-recovery`` binding,
        capture, and pre-dispatch restoration path already used for
        retryable failed workers: the coordinator captures this worktree's
        patch, prepares a fresh target worktree, restores the patch there,
        and only then assigns and dispatches -- the indeterminate source
        worktree itself is never reused as a dispatch target and is
        superseded once the new attempt is assigned.

        A node with ``depends_on`` cannot reproduce accepted-ancestor
        lineage from a fabricated crash receipt, so its own recorded
        ``dependency-input`` artifact ref (written before dispatch, still
        durable in ArtifactStore even though the crash never reached
        settlement) must be supplied explicitly to preserve those already
        accepted dependencies rather than recomputing them from a
        potentially changed task snapshot.
        """

        reason = reason.strip()
        if not reason:
            raise ValueError("local indeterminate recovery reason must be non-empty")
        if confirm_old_executor_ended is not True:
            raise ValueError(
                "local indeterminate recovery requires an explicit assertion that the old executor ended"
            )
        if confirm_effects_restricted_to_owned_files is not True:
            raise ValueError(
                "local indeterminate recovery requires an explicit assertion that effects are "
                "restricted to owned files"
            )
        try:
            raw_changed_paths = tuple(observed_changed_paths)
            raw_generated_residue_paths = tuple(observed_generated_residue_paths)
        except TypeError as error:
            raise ValueError("observed recovery paths must be explicit path sequences") from error
        if not all(isinstance(path, str) and path for path in raw_changed_paths):
            raise ValueError("observed_changed_paths must contain explicit path strings")
        if not all(isinstance(path, str) and path for path in raw_generated_residue_paths):
            raise ValueError("observed_generated_residue_paths must contain explicit path strings")
        changed_paths = tuple(sorted(set(raw_changed_paths)))
        generated_residue_paths = tuple(sorted(set(raw_generated_residue_paths)))
        if partition_recovery_paths(generated_residue_paths)[1] != generated_residue_paths:
            raise ValueError("observed generated residue paths are not recognized recovery residue")
        if dependency_input_ref is not None and (
            not isinstance(dependency_input_ref, str) or not dependency_input_ref
        ):
            raise ValueError("dependency_input_ref must be a non-empty artifact reference")

        # ``changed_paths`` excludes ignored generated residue by design, but
        # the shared failed-attempt authorization derives its separate residue
        # list by partitioning the synthetic result's complete observation.
        # Include both sets here so that the eventual capture can verify and
        # discard only the known generated files without losing provenance.
        observed_result_paths = tuple(sorted(set((*changed_paths, *generated_residue_paths))))

        synthetic_result = NodeResult(
            status="blocked",
            summary=f"local indeterminate recovery: {reason}",
            result_kind="worker",
            changed_paths=observed_result_paths,
            verdict=None,
            artifacts=(
                {"dependency-input": dependency_input_ref}
                if dependency_input_ref is not None
                else {}
            ),
        ).to_dict()
        source_result_json = canonical_json(synthetic_result)

        if dry_run:
            with self.connection() as connection:
                candidate = self._indeterminate_local_recovery_candidate(
                    connection,
                    task_id,
                    node_id,
                    expected_revision=expected_revision,
                    expected_attempt=expected_attempt,
                )
                self._require_indeterminate_local_recovery_dependency_input(
                    candidate, dependency_input_ref
                )
                node = connection.execute(
                    "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?", (task_id, node_id)
                ).fetchone()
                assert node is not None
                authorization = self._failed_attempt_recovery_authorization(
                    connection,
                    task_id,
                    node,
                    authorization_revision=expected_revision + 1,
                    source_result_json=source_result_json,
                )
            if authorization is None or authorization["source"]["generated_residue_paths"] != list(
                generated_residue_paths
            ):
                raise StateConflictError(
                    "local indeterminate recovery generated residue paths do not match the observed worktree"
                )
            return {
                "task_id": task_id,
                "node_id": node_id,
                "dry_run": True,
                "task": candidate["task"],
                "node": candidate["node"],
                "would_authorize": authorization,
                "operator_asserted": True,
                "automatically_verified": False,
            }

        # Preflight outside the write transaction, mirroring
        # resume_blocked_worktree: the transaction below repeats only the
        # same durable, already-recorded checks against a fresh read inside
        # the lock, so no additional filesystem or subprocess IO ever runs
        # while SQLite's write lock is held.
        with self.connection() as connection:
            candidate = self._indeterminate_local_recovery_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )
            self._require_indeterminate_local_recovery_dependency_input(
                candidate, dependency_input_ref
            )
            preflight_node = connection.execute(
                "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?", (task_id, node_id)
            ).fetchone()
            assert preflight_node is not None
            preflight_authorization = self._failed_attempt_recovery_authorization(
                connection,
                task_id,
                preflight_node,
                authorization_revision=expected_revision + 1,
                source_result_json=source_result_json,
            )
        if preflight_authorization is None:
            raise StateConflictError(
                "local indeterminate recovery could not build a recovery authorization"
            )

        timestamp = now_iso()
        with self.transaction() as connection:
            candidate = self._indeterminate_local_recovery_candidate(
                connection,
                task_id,
                node_id,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
            )
            self._require_indeterminate_local_recovery_dependency_input(
                candidate, dependency_input_ref
            )
            node = connection.execute(
                "SELECT * FROM nodes WHERE task_id = ? AND node_id = ?", (task_id, node_id)
            ).fetchone()
            assert node is not None
            revision = expected_revision + 1
            authorization = self._failed_attempt_recovery_authorization(
                connection,
                task_id,
                node,
                authorization_revision=revision,
                source_result_json=source_result_json,
            )
            if authorization is None:
                raise StateConflictError(
                    "local indeterminate recovery could not build a recovery authorization"
                )
            if authorization["source"]["generated_residue_paths"] != list(generated_residue_paths):
                raise StateConflictError(
                    "local indeterminate recovery generated residue paths do not match the observed worktree"
                )
            if canonical_json(authorization) != canonical_json(preflight_authorization):
                raise StateConflictError(
                    "local indeterminate recovery authorization changed before commit"
                )
            changed = connection.execute(
                """
                UPDATE nodes
                SET state = 'pending', worker_id = NULL, worktree = NULL,
                    effective_executor = NULL, effective_model = NULL,
                    started_at = NULL, settled_at = NULL, result_json = NULL,
                    coordinator_epoch = 0, lease_epoch = 0, recovery_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'indeterminate' AND attempt = ?
                  AND worktree = ? AND recovery_json IS NULL
                """,
                (
                    canonical_json(authorization),
                    timestamp,
                    task_id,
                    node_id,
                    expected_attempt,
                    candidate["node"]["worktree"],
                ),
            ).rowcount
            if changed != 1:
                raise StateConflictError("local indeterminate recovery node compare-and-set failed")

            approval = connection.execute(
                """
                SELECT approval_id, request_json FROM approvals
                WHERE task_id = ? AND kind = 'indeterminate_resolution'
                  AND decision IS NULL
                  AND json_extract(request_json, '$.node_id') = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (task_id, node_id),
            ).fetchone()
            if approval is not None:
                request = json.loads(approval["request_json"])
                request["decision_revision"] = revision
                connection.execute(
                    """
                    UPDATE approvals SET decision = 'retry', decided_at = ?, request_json = ?
                    WHERE approval_id = ?
                    """,
                    (timestamp, canonical_json(request), approval["approval_id"]),
                )
                self._event(
                    connection,
                    "approval.decided",
                    task_id,
                    node_id,
                    {
                        "approval_id": approval["approval_id"],
                        "decision": "retry",
                        "task_revision": revision,
                    },
                    created_at=timestamp,
                )
            remaining_indeterminate = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM nodes WHERE task_id = ? AND state = 'indeterminate'",
                    (task_id,),
                ).fetchone()["count"]
            )
            if remaining_indeterminate:
                next_task_state = "needs_approval"
                blocker = f"{remaining_indeterminate} indeterminate node(s) still require approval"
            else:
                next_task_state = "queued"
                blocker = None
            changed_task = connection.execute(
                """
                UPDATE tasks
                SET state = ?, state_revision = ?, updated_at = ?, blocker = ?, verdict = NULL
                WHERE task_id = ? AND state_revision = ?
                """,
                (next_task_state, revision, timestamp, blocker, task_id, expected_revision),
            ).rowcount
            if changed_task != 1:
                raise StateConflictError("local indeterminate recovery task compare-and-set failed")
            authorization_cursor = self._event(
                connection,
                "node.indeterminate_local_recovery_queued",
                task_id,
                node_id,
                {
                    "attempt": expected_attempt,
                    "next_attempt": expected_attempt + 1,
                    "worktree": candidate["node"]["worktree"],
                    "allocation_id": candidate["allocation_id"],
                    "reason": reason,
                    "expected_changed_paths": list(changed_paths),
                    "operator_assertion": {
                        "confirm_old_executor_ended": True,
                        "confirm_effects_restricted_to_owned_files": True,
                        "assertion": "operator_asserted",
                        "automatically_verified": False,
                    },
                    "task_revision": revision,
                },
                created_at=timestamp,
            )
            self._event(
                connection,
                "node.indeterminate_resolved",
                task_id,
                node_id,
                {"resolution": "retry", "task_revision": revision},
                created_at=timestamp,
            )
            self._event(
                connection,
                "task.state_changed",
                task_id,
                None,
                {
                    "from": "needs_approval",
                    "to": next_task_state,
                    "revision": revision,
                    "blocker": blocker,
                },
                created_at=timestamp,
            )
            return {
                "task_id": task_id,
                "node_id": node_id,
                "dry_run": False,
                "revision": revision,
                "task": {"task_id": task_id, "state": next_task_state, "revision": revision},
                "node": {
                    "node_id": node_id,
                    "state": "pending",
                    "attempt": expected_attempt,
                    "next_attempt": expected_attempt + 1,
                },
                "operator_asserted": True,
                "automatically_verified": False,
                "authorization_event_cursor": authorization_cursor,
            }

    @staticmethod
    def _require_indeterminate_local_recovery_dependency_input(
        candidate: Mapping[str, Any], dependency_input_ref: str | None
    ) -> None:
        """Fail before queuing when accepted-ancestor lineage is unavailable."""

        node = candidate.get("node")
        depends_on = node.get("depends_on") if isinstance(node, Mapping) else None
        if depends_on and dependency_input_ref is None:
            raise StateConflictError(
                "local indeterminate recovery requires the recorded dependency-input artifact"
            )

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
        admission_wait_rows = connection.execute(
            "SELECT * FROM node_admission_waits WHERE task_id = ?",
            (row["task_id"],),
        ).fetchall()
        admission_waits = {
            str(wait["node_id"]): WorkbenchStore._node_admission_wait_row(wait)
            for wait in admission_wait_rows
        }
        delivery_objective = connection.execute(
            "SELECT * FROM delivery_objectives WHERE task_id = ?", (row["task_id"],)
        ).fetchone()
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
            "delivery_objective": (
                WorkbenchStore._delivery_objective_row(
                    connection,
                    delivery_objective,
                    include_receipts=False,
                )
                if delivery_objective is not None
                else None
            ),
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
                    "admission_wait": admission_waits.get(str(node["node_id"])),
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

    def read_event_projection(
        self,
        *,
        task_ids: tuple[str, ...],
        event_types: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read a complete event subset and expose its ledger cursor coverage."""

        selected_tasks = tuple(sorted(set(task_ids)))
        selected_types = tuple(sorted(set(event_types)))
        if len(selected_tasks) + len(selected_types) > 900:
            raise ValueError("event projection exceeds the bounded SQLite parameter set")
        clauses: list[str] = []
        parameters: list[str] = []
        if selected_tasks:
            clauses.append("task_id IN (" + ",".join("?" for _ in selected_tasks) + ")")
            parameters.extend(selected_tasks)
        if selected_types:
            clauses.append("event_type IN (" + ",".join("?" for _ in selected_types) + ")")
            parameters.extend(selected_types)
        where = " OR ".join(clauses) if clauses else "0"
        with self.connection() as connection:
            connection.execute("BEGIN")
            ledger = connection.execute(
                "SELECT MIN(cursor) AS first_cursor, MAX(cursor) AS last_cursor FROM events"
            ).fetchone()
            rows = connection.execute(
                f"SELECT * FROM events WHERE {where} ORDER BY cursor",
                tuple(parameters),
            ).fetchall()
        events = [
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
        return events, {
            "mode": "complete-task-and-event-type-projection",
            "ledger_first_cursor": int(ledger["first_cursor"] or 0),
            "ledger_last_cursor": int(ledger["last_cursor"] or 0),
            "selected_first_cursor": int(events[0]["cursor"]) if events else 0,
            "selected_last_cursor": int(events[-1]["cursor"]) if events else 0,
            "selected_event_count": len(events),
            "task_count": len(selected_tasks),
            "global_event_types": list(selected_types),
            "truncated": False,
        }

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
            "node.admission_waiting",
            "node.routed",
            "coordinator.started",
            "coordinator.stopped",
            "coordinator.failed",
            "quota.refresh_failed",
            "quota.refresh_unavailable",
            "worktree.recovery_failed",
            "worktree.purge_failed",
            "delivery_objective.decision_required",
            "delivery_objective.authorization_denied",
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

    def get_session_context(self, source_thread_id: str, context_ref: str) -> dict[str, Any]:
        """Read one frozen context receipt without consulting active bindings."""

        if not isinstance(source_thread_id, str) or not source_thread_id:
            raise ValueError("source_thread_id is required")
        if not isinstance(context_ref, str) or not context_ref:
            raise ValueError("context_ref is required")
        with self.connection() as connection:
            receipt = connection.execute(
                """
                SELECT * FROM context_import_receipts
                WHERE source_thread_id = ? AND context_ref = ?
                ORDER BY created_at DESC, command_id DESC
                LIMIT 1
                """,
                (source_thread_id, context_ref),
            ).fetchone()
            if receipt is None:
                raise KeyError((source_thread_id, context_ref))
            return self._context_receipt(receipt)

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
            pending_delivery = {
                str(row["task_id"])
                for row in connection.execute(
                    "SELECT task_id FROM delivery_objectives WHERE state NOT IN ('complete', 'cancelled')"
                ).fetchall()
            }
            delivered = {
                str(row["task_id"])
                for row in connection.execute(
                    "SELECT DISTINCT task_id FROM delivery_receipts WHERE state IN ('merged', 'released')"
                ).fetchall()
            }
        result: list[dict[str, Any]] = []
        for allocation in candidates:
            if allocation["task_id"] in pending_delivery:
                continue
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
            pending_delivery = connection.execute(
                "SELECT 1 FROM delivery_objectives WHERE task_id = ? "
                "AND state NOT IN ('complete', 'cancelled')",
                (row["task_id"],),
            ).fetchone()
            if pending_delivery is not None:
                raise StateConflictError("worktree is retained by an unfinished delivery objective")
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

    def record_node_admission_deferred(
        self,
        task_id: str,
        node_id: str,
        *,
        model: str,
        reason_kind: str,
        quota_snapshot_id: int,
        reason: str,
        zone: str,
        capacity_units: int,
        active_units: int,
        requested_units: int,
        available_units: int,
        resume_condition: str,
    ) -> int | None:
        """Persist one admission wait per quota snapshot while the node is pending."""

        if reason_kind not in {"temporary-capacity", "quota-refresh-required"}:
            raise ValueError("unsupported admission wait reason kind")
        if type(quota_snapshot_id) is not int or quota_snapshot_id < 1:
            raise ValueError("admission wait quota_snapshot_id must be positive")
        if not model.strip() or not reason.strip() or not zone.strip():
            raise ValueError("admission wait model, reason, and zone are required")
        if not resume_condition.strip():
            raise ValueError("admission wait resume condition is required")
        units = (capacity_units, active_units, requested_units, available_units)
        if any(type(value) is not int or value < 0 for value in units):
            raise ValueError("admission wait capacity units must be non-negative integers")
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT state, attempt, spec_json FROM nodes
                WHERE task_id = ? AND node_id = ?
                """,
                (task_id, node_id),
            ).fetchone()
            if row is None:
                raise KeyError((task_id, node_id))
            if row["state"] != "pending":
                return None
            spec = json.loads(row["spec_json"])
            if spec.get("executor") != "claude" or str(spec.get("model")) != model:
                raise StateConflictError(
                    "admission wait does not match the pending Claude route"
                )
            payload = {
                "provider": "claude",
                "model": model,
                "action": "defer",
                "reason_kind": reason_kind,
                "reason": reason,
                "zone": zone,
                "capacity_units": capacity_units,
                "active_units": active_units,
                "requested_units": requested_units,
                "available_units": available_units,
                "next_attempt": int(row["attempt"]) + 1,
                "resume_condition": resume_condition,
                "quota_snapshot_id": quota_snapshot_id,
            }
            previous = connection.execute(
                """
                SELECT cursor, payload_json FROM events
                WHERE task_id = ? AND node_id = ?
                  AND event_type = 'node.admission_deferred'
                ORDER BY cursor DESC LIMIT 1
                """,
                (task_id, node_id),
            ).fetchone()
            if previous is not None:
                previous_payload = json.loads(previous["payload_json"])
                if previous_payload == payload:
                    return int(previous["cursor"])
            quota = connection.execute(
                """
                SELECT 1 FROM quota_snapshots
                WHERE provider = 'claude' AND id = ?
                """,
                (quota_snapshot_id,),
            ).fetchone()
            if quota is None:
                raise StateConflictError(
                    "admission wait quota snapshot reference does not exist"
                )
            return self._event(
                connection,
                "node.admission_deferred",
                task_id,
                node_id,
                payload,
            )

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
        quota_snapshot_id: int | None = None,
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
            # caller-supplied copies before attaching the selected durable
            # row.  Legacy callers retain newest-row attachment when they do
            # not have an effective-observation reference.
            for key in (
                "quota_snapshot",
                "quota_provenance",
                "quota_snapshot_id",
                "quota_source",
            ):
                event_payload.pop(key, None)
            if quota_snapshot_id is not None:
                if type(quota_snapshot_id) is not int or quota_snapshot_id < 1:
                    raise ValueError("route quota_snapshot_id must be a positive integer")
                quota_row = connection.execute(
                    """
                    SELECT id, snapshot_json FROM quota_snapshots
                    WHERE provider = 'claude' AND id = ?
                    """,
                    (quota_snapshot_id,),
                ).fetchone()
                if quota_row is None:
                    raise StateConflictError("route quota snapshot reference does not exist")
            else:
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
        if "source_checkpoint_sha" in recovery:
            checkpoint = recovery["source_checkpoint_sha"]
            if not isinstance(checkpoint, str) or not re.fullmatch(r"[0-9a-f]{40}", checkpoint):
                raise StateConflictError("checkpoint requires an exact full commit SHA")
            common_fields.add("source_checkpoint_sha")
        schema_version = recovery.get("schema_version")
        if schema_version == 1:
            receipt_fields = common_fields
        elif schema_version in {2, 3, 5, 6}:
            receipt_fields = common_fields | {
                "source_task_id",
                "source_node_id",
                "input_tree_sha",
                "dependency_input_ref",
            }
            if schema_version in {3, 6}:
                receipt_fields.add("untracked_paths")
            if schema_version in {5, 6}:
                receipt_fields |= {"generated_residue_paths", "generated_residue_ref"}
        elif schema_version == 4:
            receipt_fields = common_fields | {
                "generated_residue_paths",
                "generated_residue_ref",
            }
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
        if schema_version in {2, 3, 5, 6}:
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
        if schema_version in {4, 5, 6}:
            generated_paths = recovery.get("generated_residue_paths")
            generated_ref = recovery.get("generated_residue_ref")
            if (
                not isinstance(generated_paths, list)
                or not generated_paths
                or not all(isinstance(path, str) for path in generated_paths)
                or partition_recovery_paths(tuple(generated_paths))[1] != tuple(generated_paths)
                or tuple(generated_paths) != tuple(sorted(set(generated_paths)))
                or not isinstance(generated_ref, str)
                or not generated_ref
            ):
                raise StateConflictError(
                    "dirty-worktree recovery generated residue evidence is invalid"
                )
        if schema_version in {3, 6}:
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

    @staticmethod
    def _failed_attempt_recovery_binding(
        raw: object,
        *,
        next_attempt: int,
        allow_assigned: bool = False,
    ) -> dict[str, Any] | None:
        """Decode the one-shot pre-dispatch recovery binding for a failed node.

        This is deliberately a structural parser only.  The service validates
        Git state, artifact bytes, scope, and target contents before calling
        the DB-only assignment CAS.
        """

        if raw is None:
            return None
        if not isinstance(raw, str):
            raise StateConflictError("failed-attempt recovery authorization is invalid")
        try:
            authorization = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StateConflictError("failed-attempt recovery authorization is invalid JSON") from error
        if not isinstance(authorization, dict):
            raise StateConflictError("failed-attempt recovery authorization is invalid")
        if authorization.get("kind") != _FAILED_ATTEMPT_RECOVERY_KIND:
            return None
        base_fields = {
            "schema_version",
            "kind",
            "state",
            "authorization_revision",
            "source_allocation_id",
            "source_result_json",
            "source",
        }
        state = authorization.get("state")
        assigned_fields = base_fields | {
            "recovery",
            "recovery_ref",
            "target_attempt",
            "target_worktree",
            "target_branch",
        }
        if state == "capture_pending":
            if set(authorization) != base_fields:
                raise StateConflictError("failed-attempt recovery authorization has an invalid shape")
        elif state == "assigned" and allow_assigned:
            if set(authorization) != assigned_fields:
                raise StateConflictError("failed-attempt recovery assignment has an invalid shape")
        else:
            raise StateConflictError("failed-attempt recovery authorization is not claimable")
        if (
            authorization.get("schema_version") != 1
            or isinstance(authorization.get("authorization_revision"), bool)
            or not isinstance(authorization.get("authorization_revision"), int)
            or int(authorization["authorization_revision"]) <= 0
            or not isinstance(authorization.get("source_allocation_id"), str)
            or not authorization["source_allocation_id"]
            or not isinstance(authorization.get("source_result_json"), str)
        ):
            raise StateConflictError("failed-attempt recovery authorization fields are invalid")
        try:
            source_result = json.loads(authorization["source_result_json"])
        except json.JSONDecodeError as error:
            raise StateConflictError("failed-attempt recovery source result is invalid JSON") from error
        if not isinstance(source_result, dict) or source_result.get("status") not in {
            "failed",
            "blocked",
        }:
            raise StateConflictError(
                "failed-attempt recovery source result is not failed or blocked"
            )
        source = authorization["source"]
        source_fields = {
            "attempt",
            "worktree",
            "branch",
            "base_sha",
            "changed_paths",
        }
        if not isinstance(source, dict) or frozenset(source) not in {
            frozenset(source_fields),
            frozenset(source_fields | {"generated_residue_paths"}),
        }:
            raise StateConflictError("failed-attempt recovery source is invalid")
        source_attempt = source["attempt"]
        changed_paths = source["changed_paths"]
        if (
            isinstance(source_attempt, bool)
            or not isinstance(source_attempt, int)
            or source_attempt < 1
            or source_attempt + 1 != next_attempt
            or not all(isinstance(source.get(field), str) and source[field] for field in (
                "worktree",
                "branch",
                "base_sha",
            ))
            or not isinstance(changed_paths, list)
            or not all(isinstance(path, str) and path for path in changed_paths)
            or tuple(changed_paths) != tuple(sorted(set(changed_paths)))
        ):
            raise StateConflictError("failed-attempt recovery source does not match the next attempt")
        generated_residue_paths = source.get("generated_residue_paths", [])
        if (
            not isinstance(generated_residue_paths, list)
            or not all(isinstance(path, str) for path in generated_residue_paths)
            or tuple(generated_residue_paths) != tuple(sorted(set(generated_residue_paths)))
            or partition_recovery_paths(tuple(generated_residue_paths))[1]
            != tuple(generated_residue_paths)
        ):
            raise StateConflictError("failed-attempt recovery generated residue paths are invalid")
        binding: dict[str, Any] = {
            "state": state,
            "authorization_revision": int(authorization["authorization_revision"]),
            "source_allocation_id": authorization["source_allocation_id"],
            "source_result_json": authorization["source_result_json"],
            "source": dict(source),
        }
        if state == "assigned":
            recovery = authorization["recovery"]
            if (
                not isinstance(authorization["recovery_ref"], str)
                or not authorization["recovery_ref"]
                or isinstance(authorization["target_attempt"], bool)
                or not isinstance(authorization["target_attempt"], int)
                or authorization["target_attempt"] != next_attempt
                or not isinstance(authorization["target_worktree"], str)
                or not authorization["target_worktree"]
                or not isinstance(authorization["target_branch"], str)
                or not authorization["target_branch"]
                or not isinstance(recovery, dict)
            ):
                raise StateConflictError("failed-attempt recovery assignment fields are invalid")
            if (
                recovery.get("source_attempt") != source_attempt
                or recovery.get("source_worktree") != source["worktree"]
                or recovery.get("source_branch") != source["branch"]
                or recovery.get("base_sha") != source["base_sha"]
                or recovery.get("changed_paths") != changed_paths
                or (
                    generated_residue_paths
                    and recovery.get("generated_residue_paths") != generated_residue_paths
                )
            ):
                raise StateConflictError("failed-attempt recovery assignment does not match its source")
            binding.update(
                {
                    "recovery": dict(recovery),
                    "recovery_ref": authorization["recovery_ref"],
                    "target_attempt": authorization["target_attempt"],
                    "target_worktree": authorization["target_worktree"],
                    "target_branch": authorization["target_branch"],
                }
            )
        return binding

    @staticmethod
    def _recorded_retry_fallback_route(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        node_id: str,
        source_attempt: int,
        original_claude_model: str | None = None,
    ) -> tuple[str, str] | None:
        """Reuse a durable fallback route without treating it as identity evidence.

        Retry recovery intentionally clears transient ``effective_*`` columns
        before a new lease.  A Claude-to-Codex fallback is nevertheless a
        durable route decision for the failed source attempt. Provider CLI
        response failures allow a new attempt to reconsider its frozen Claude
        candidate; current contract, authentication and quota gates still run.
        Other fallback reasons keep their existing durable Codex route.
        """

        rows = connection.execute(
            """
            SELECT payload_json FROM events
            WHERE event_type = 'node.routed' AND task_id = ? AND node_id = ?
            ORDER BY cursor DESC
            """,
            (task_id, node_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            model = payload.get("model")
            if (
                payload.get("attempt") == source_attempt
                and payload.get("from") == "claude"
                and payload.get("to") == "codex"
                and isinstance(model, str)
                and model
            ):
                reason = payload.get("reason")
                if (
                    original_claude_model
                    and payload.get("fallback_kind") == "claude-executor-failed"
                    and isinstance(reason, str)
                    and reason.startswith("Claude structured result rejected: CLI ")
                ):
                    return "claude", original_claude_model
                return "codex", model
        return None

    @staticmethod
    def _scope_access_pairs(
        candidate_reads: tuple[str, ...],
        candidate_writes: tuple[str, ...],
        running_reads: tuple[str, ...],
        running_writes: tuple[str, ...],
    ) -> list[dict[str, str]]:
        """Return the exact non-read/read paths behind an access conflict."""

        if not scope_access_conflicts(
            candidate_reads, candidate_writes, running_reads, running_writes
        ):
            return []
        pairs: list[dict[str, str]] = []
        for candidate_kind, candidate_scopes, running_kind, running_scopes in (
            ("write", candidate_writes, "write", running_writes),
            ("write", candidate_writes, "read", running_reads),
            ("read", candidate_reads, "write", running_writes),
        ):
            for candidate_scope in candidate_scopes:
                for running_scope in running_scopes:
                    if not scopes_overlap(candidate_scope, running_scope):
                        continue
                    left = normalize_scope(candidate_scope)
                    right = normalize_scope(running_scope)
                    pairs.append(
                        {
                            "candidate_access": candidate_kind,
                            "candidate_scope": left,
                            "running_access": running_kind,
                            "running_scope": right,
                            # The intersection of two nested repository
                            # scopes is the narrower path.  Persist that
                            # exact path so an admission wait explains what
                            # must be released rather than merely the
                            # candidate's broader read envelope.
                            "overlap": (
                                left
                                if right == "." or (left != "." and len(left) >= len(right))
                                else right
                            ),
                        }
                    )
        return pairs

    def _private_worktree_isolation(
        self,
        connection: sqlite3.Connection,
        *,
        candidate: sqlite3.Row,
        candidate_contract: dict[str, Any],
        candidate_executor: str,
        candidate_attempt: int,
        running: dict[str, Any],
    ) -> dict[str, Any]:
        """Report missing evidence for a proposed reader/writer exception.

        This is intentionally stricter than "different task": both worktrees
        must have deterministic private paths/branches at the exact same base,
        neither side may be a fixture/shared-resource route, and the running
        allocation must still be active.  Any missing proof remains a gate.
        """

        if candidate_executor == "fixture" or running["executor"] == "fixture":
            return {"status": "unproven", "reason": "fixture_or_shared_execution"}
        if candidate["recovery_json"] is not None:
            return {"status": "unproven", "reason": "candidate_recovery_input_is_not_fixed_base"}
        if candidate_contract.get("base_sha") != running["contract"].get("base_sha"):
            return {"status": "unproven", "reason": "base_sha_mismatch"}
        if candidate_contract.get("repository") != running["contract"].get("repository"):
            return {"status": "unproven", "reason": "repository_contract_mismatch"}
        candidate_spec = json.loads(str(candidate["spec_json"]))
        if candidate_spec.get("shared_resources") or running["spec"].get("shared_resources"):
            return {"status": "unproven", "reason": "shared_resource_declared"}
        manager = WorktreeManager(self.path.parent / "worktrees")
        try:
            candidate_path = str(
                manager.worktree_path(
                    str(candidate["task_id"]),
                    str(candidate["node_id"]),
                    candidate_attempt,
                )
            )
            candidate_branch = manager.branch_name(
                str(candidate["task_id"]), str(candidate["node_id"]), candidate_attempt
            )
            running_path = str(
                manager.worktree_path(
                    str(running["task_id"]), str(running["node_id"]), int(running["attempt"])
                )
            )
            running_branch = manager.branch_name(
                str(running["task_id"]), str(running["node_id"]), int(running["attempt"])
            )
        except (OSError, ValueError):
            return {"status": "unproven", "reason": "worktree_identity_unavailable"}
        candidate_allocation = connection.execute(
            """
            SELECT 1 FROM worktree_allocations
            WHERE task_id = ? AND node_id = ? AND attempt = ? AND state = 'active'
            """,
            (candidate["task_id"], candidate["node_id"], candidate_attempt),
        ).fetchone()
        if candidate_allocation is not None:
            return {"status": "unproven", "reason": "candidate_attempt_already_has_active_allocation"}
        if (
            running["allocation_state"] != "active"
            or running["worktree"] != running["allocation_path"]
            or running["allocation_path"] != running_path
            or running["allocation_branch"] != running_branch
            or running["allocation_base_sha"] != candidate_contract.get("base_sha")
            or candidate_path == running_path
            or candidate_branch == running_branch
        ):
            return {"status": "unproven", "reason": "active_private_worktree_receipt_missing"}
        return {
            "status": "unproven",
            "reason": "read_scope_enforcement_unavailable",
            "candidate_worktree": candidate_path,
            "candidate_branch": candidate_branch,
            "running_worktree": running_path,
            "running_branch": running_branch,
            "base_sha": candidate_contract["base_sha"],
        }

    def _record_scope_access_wait(
        self,
        connection: sqlite3.Connection,
        *,
        candidate: sqlite3.Row,
        next_attempt: int,
        blockers: list[dict[str, Any]],
        timestamp: str,
    ) -> int:
        task_id = str(candidate["task_id"])
        node_id = str(candidate["node_id"])
        blocking = {
            "nodes": sorted(
                blockers,
                key=lambda item: (
                    item["task_id"], item["node_id"], item["attempt"], str(item["worker_id"] or "")
                ),
            )
        }
        reason = {
            "kind": "scope_access_conflict",
            "detail": "node waits for a conflicting repository scope to be released",
            "next_attempt": next_attempt,
            "conflicts": blocking["nodes"],
        }
        wait_fingerprint = canonical_hash(
            {
                "task_id": task_id,
                "node_id": node_id,
                "next_attempt": next_attempt,
                "reason": reason,
                "blocking": blocking,
            }
        )
        previous = connection.execute(
            "SELECT * FROM node_admission_waits WHERE task_id = ? AND node_id = ?",
            (task_id, node_id),
        ).fetchone()
        if previous is not None and previous["wait_fingerprint"] == wait_fingerprint:
            return int(previous["event_cursor"])
        due_at = self._timestamp_after(timestamp, 300)
        next_wakeup_at = self._timestamp_after(timestamp, 5)
        next_action = {
            "action": "claim_when_scope_released",
            "stage": "admission",
            "target_attempt": next_attempt,
            "trigger": "running_node_settlement_or_bounded_reconciliation",
        }
        cursor = self._event(
            connection,
            "node.admission_waiting",
            task_id,
            node_id,
            {
                "wait_fingerprint": wait_fingerprint,
                "reason_kind": "scope_access_conflict",
                "next_attempt": next_attempt,
                "blocking": blocking,
                "next_action": next_action,
                "due_at": due_at,
                "next_wakeup_at": next_wakeup_at,
            },
            created_at=timestamp,
        )
        connection.execute(
            """
            INSERT INTO node_admission_waits(
                task_id, node_id, wait_fingerprint, reason_kind, reason_json,
                next_action_json, blocking_json, due_at, next_wakeup_at,
                event_cursor, created_at, updated_at
            ) VALUES(?, ?, ?, 'scope_access_conflict', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, node_id) DO UPDATE SET
                wait_fingerprint = excluded.wait_fingerprint,
                reason_kind = excluded.reason_kind,
                reason_json = excluded.reason_json,
                next_action_json = excluded.next_action_json,
                blocking_json = excluded.blocking_json,
                due_at = excluded.due_at,
                next_wakeup_at = excluded.next_wakeup_at,
                event_cursor = excluded.event_cursor,
                updated_at = excluded.updated_at
            """,
            (
                task_id,
                node_id,
                wait_fingerprint,
                canonical_json(reason),
                canonical_json(next_action),
                canonical_json(blocking),
                due_at,
                next_wakeup_at,
                cursor,
                timestamp,
                timestamp,
            ),
        )
        return cursor

    def _resolve_scope_access_wait(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        node_id: str,
        timestamp: str,
        reason: str,
    ) -> int | None:
        wait = connection.execute(
            "SELECT * FROM node_admission_waits WHERE task_id = ? AND node_id = ?",
            (task_id, node_id),
        ).fetchone()
        if wait is None:
            return None
        connection.execute(
            "DELETE FROM node_admission_waits WHERE task_id = ? AND node_id = ?",
            (task_id, node_id),
        )
        return self._event(
            connection,
            "node.admission_resumed",
            task_id,
            node_id,
            {
                "reason": reason,
                "admission_wait_event_cursor": int(wait["event_cursor"]),
                "wait_fingerprint": wait["wait_fingerprint"],
            },
            created_at=timestamp,
        )

    def _reconcile_scope_access_waits(
        self,
        connection: sqlite3.Connection,
        *,
        timestamp: str,
    ) -> None:
        """Resolve waits only after every recorded blocking lease has released."""

        waits = connection.execute("SELECT * FROM node_admission_waits").fetchall()
        for wait in waits:
            node = connection.execute(
                "SELECT state FROM nodes WHERE task_id = ? AND node_id = ?",
                (wait["task_id"], wait["node_id"]),
            ).fetchone()
            if node is None or node["state"] != "pending":
                connection.execute(
                    "DELETE FROM node_admission_waits WHERE task_id = ? AND node_id = ?",
                    (wait["task_id"], wait["node_id"]),
                )
                continue
            try:
                blocking = json.loads(str(wait["blocking_json"]))
                blockers = blocking["nodes"]
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise StateConflictError("scope admission wait is invalid") from error
            still_running = False
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    raise StateConflictError("scope admission wait blocker is invalid")
                row = connection.execute(
                    """
                    SELECT state, attempt FROM nodes WHERE task_id = ? AND node_id = ?
                    """,
                    (blocker.get("task_id"), blocker.get("node_id")),
                ).fetchone()
                if (
                    row is not None
                    and row["state"] == "running"
                    and int(row["attempt"]) == blocker.get("attempt")
                ):
                    still_running = True
                    break
            if not still_running:
                self._resolve_scope_access_wait(
                    connection,
                    task_id=str(wait["task_id"]),
                    node_id=str(wait["node_id"]),
                    timestamp=timestamp,
                    reason="recorded_scope_released",
                )

    def pending_node_admission_waits(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("admission wait limit must be positive")
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM node_admission_waits
                ORDER BY next_wakeup_at, task_id, node_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "task_id": str(row["task_id"]),
                    "node_id": str(row["node_id"]),
                    **self._node_admission_wait_row(row),
                }
                for row in rows
            ]

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
        if self.delivery_admission_gate() is not None:
            return None
        repository_identities = self._claim_repository_identities()
        timestamp = now_iso()
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            if self._deployment_admission_gate(connection) is not None:
                return None
            candidates = connection.execute(
                """
                SELECT n.*, t.contract_json, t.state AS task_state, t.priority AS task_priority
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.state = 'pending' AND t.state IN ('queued', 'running', 'verifying', 'needs_fix')
                ORDER BY t.priority DESC, t.created_at,
                         json_extract(n.spec_json, '$.ordinal'), n.node_id
                """
            ).fetchall()
            running_accesses: list[dict[str, Any]] = []
            running_task_parallelism: dict[str, bool] = {}
            running_lane_active = {lane: 0 for lane in EXECUTION_LANES}
            for running in connection.execute(
                """
                SELECT n.task_id, n.node_id, n.attempt, n.worker_id, n.worktree, n.spec_json,
                       n.effective_executor, n.effective_model, t.contract_json,
                       a.state AS allocation_state, a.current_path AS allocation_path,
                       a.base_sha AS allocation_base_sha, a.branch AS allocation_branch
                FROM nodes n JOIN tasks t USING(task_id)
                LEFT JOIN worktree_allocations a
                  ON a.task_id = n.task_id AND a.node_id = n.node_id
                 AND a.attempt = n.attempt
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
                running_repository = repository_identities.get(
                    str(running_contract["repository"])
                )
                if running_repository is None:
                    # A task created after the preflight snapshot will be
                    # considered on the next poll. Never inspect Git while
                    # holding SQLite's write lock.
                    return None
                running_task_parallelism[task_id] = running_task_parallelism.get(task_id, False) or (
                    running_spec.get("parallelizable") is False
                )
                running_accesses.append(
                    {
                        "repository": running_repository,
                        "task_id": task_id,
                        "node_id": str(running["node_id"]),
                        "attempt": int(running["attempt"]),
                        "worker_id": running["worker_id"],
                        "read_scopes": tuple(running_spec.get("read_scopes", [])),
                        "write_scopes": tuple(running_spec.get("write_scopes", [])),
                        "executor": str(running_spec["executor"]),
                        "spec": running_spec,
                        "contract": running_contract,
                        "worktree": running["worktree"],
                        "allocation_state": running["allocation_state"],
                        "allocation_path": running["allocation_path"],
                        "allocation_base_sha": running["allocation_base_sha"],
                        "allocation_branch": running["allocation_branch"],
                    }
                )
                running_lane_active[execution_lane_for_spec(running_spec)] += 1

            self._reconcile_scope_access_waits(connection, timestamp=timestamp)

            selected: sqlite3.Row | None = None
            selected_spec: dict[str, Any] | None = None
            selected_effective_executor: str | None = None
            selected_effective_model: str | None = None
            selected_lane: str | None = None
            selected_pool: str | None = None
            selected_blocked_retry_authorization_cursor: int | None = None
            selected_dirty_worktree_recovery: dict[str, Any] | None = None
            selected_failed_attempt_recovery: dict[str, Any] | None = None
            selected_provider_readmission: dict[str, Any] | None = None
            for candidate in candidates:
                provider_readmission = None
                spec = json.loads(candidate["spec_json"])
                candidate_attempt = int(candidate["attempt"]) + 1
                authorization = self._blocked_retry_authorization(
                    connection,
                    str(candidate["task_id"]),
                    str(candidate["node_id"]),
                    candidate_attempt,
                )
                failed_attempt_recovery = self._failed_attempt_recovery_binding(
                    candidate["recovery_json"], next_attempt=candidate_attempt
                )
                dirty_worktree_recovery = (
                    None
                    if failed_attempt_recovery is not None
                    else self._current_dirty_worktree_recovery_binding(
                        candidate["recovery_json"],
                        next_attempt=candidate_attempt,
                    )
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
                    source_attempt = (
                        int(failed_attempt_recovery["source"]["attempt"])
                        if failed_attempt_recovery is not None
                        else int(candidate["attempt"])
                    )
                    retry_fallback = (
                        self._recorded_retry_fallback_route(
                            connection,
                            task_id=str(candidate["task_id"]),
                            node_id=str(candidate["node_id"]),
                            source_attempt=source_attempt,
                            original_claude_model=(
                                str(spec["model"]) if spec["executor"] == "claude" else None
                            ),
                        )
                        if source_attempt > 0
                        else None
                    )
                    if retry_fallback is not None and retry_fallback[0] == "claude":
                        provider_readmission = {
                            "source_attempt": source_attempt,
                            "reason": "prior-provider-cli-response-failure",
                            "candidate_executor": "claude",
                            "candidate_model": retry_fallback[1],
                            "requires_current_admission": True,
                        }
                    effective_executor, selected_model = retry_fallback or (
                        str(candidate["effective_executor"] or spec["executor"]),
                        str(candidate["effective_model"] or spec["model"]),
                    )
                    effective_model = retry_model(
                        selected_model,
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
                repository = repository_identities.get(
                    str(candidate_contract["repository"])
                )
                if repository is None:
                    continue
                blocking_conflicts: list[dict[str, Any]] = []
                for running in running_accesses:
                    if repository != running["repository"]:
                        continue
                    pairs = self._scope_access_pairs(
                        read_scopes,
                        write_scopes,
                        running["read_scopes"],
                        running["write_scopes"],
                    )
                    candidate_root = WorktreeManager(self.path.parent / "worktrees").worktree_path(
                        str(candidate["task_id"]), str(candidate["node_id"]), candidate_attempt
                    )
                    aliases = scope_entity_alias_conflicts(
                        candidate_worktree=candidate_root,
                        candidate_reads=read_scopes,
                        candidate_writes=write_scopes,
                        running_worktree=running["worktree"] or candidate_contract["repository"],
                        running_reads=running["read_scopes"],
                        running_writes=running["write_scopes"],
                    )
                    if aliases["status"] == "conflict":
                        pairs.extend({**pair, "overlap": pair["entity"]} for pair in aliases["conflicts"])
                    if not pairs:
                        continue
                    isolation = self._private_worktree_isolation(
                        connection,
                        candidate=candidate,
                        candidate_contract=candidate_contract,
                        candidate_executor=effective_executor,
                        candidate_attempt=candidate_attempt,
                        running=running,
                    )
                    # Allocations describe paths, not an execution-time write
                    # barrier. Both logical and observed alias conflicts wait.
                    unsafe_pairs = pairs
                    if unsafe_pairs:
                        blocking_conflicts.append(
                            {
                                "task_id": running["task_id"],
                                "node_id": running["node_id"],
                                "attempt": running["attempt"],
                                "worker_id": running["worker_id"],
                                "conflict_paths": unsafe_pairs,
                                "isolation": isolation,
                            }
                        )
                if blocking_conflicts:
                    self._record_scope_access_wait(
                        connection,
                        candidate=candidate,
                        next_attempt=candidate_attempt,
                        blockers=blocking_conflicts,
                        timestamp=timestamp,
                    )
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
                selected_failed_attempt_recovery = failed_attempt_recovery
                selected_provider_readmission = provider_readmission
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
            scope_wait_resume_cursor = self._resolve_scope_access_wait(
                connection,
                task_id=str(selected["task_id"]),
                node_id=str(selected["node_id"]),
                timestamp=timestamp,
                reason="node_claimed_after_scope_gate",
            )
            lease_epoch = self._next_lease_epoch(connection)
            effective_executor = selected_effective_executor
            effective_model = selected_effective_model
            admission_wait = connection.execute(
                """
                SELECT cursor, payload_json FROM events
                WHERE task_id = ? AND node_id = ?
                  AND event_type = 'node.admission_deferred'
                ORDER BY cursor DESC LIMIT 1
                """,
                (selected["task_id"], selected["node_id"]),
            ).fetchone()
            admission_wait_cursor: int | None = None
            if admission_wait is not None:
                admission_wait_payload = json.loads(admission_wait["payload_json"])
                if admission_wait_payload.get("next_attempt") == attempt:
                    admission_wait_cursor = int(admission_wait["cursor"])
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
            started_event_cursor = self._event(
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
                    **({"provider_readmission": selected_provider_readmission}
                       if selected_provider_readmission is not None else {}),
                    **({
                        "admission_deferred_event_cursor": admission_wait_cursor,
                    } if admission_wait_cursor is not None else {}),
                    **({
                        "scope_admission_wait_resumed_event_cursor": scope_wait_resume_cursor,
                    } if scope_wait_resume_cursor is not None else {}),
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
                    **({
                        "failed_attempt_recovery": {
                            "source_attempt": selected_failed_attempt_recovery["source"]["attempt"],
                            "source_allocation_id": selected_failed_attempt_recovery["source_allocation_id"],
                            "expected_changed_paths": selected_failed_attempt_recovery["source"]["changed_paths"],
                        },
                    } if selected_failed_attempt_recovery is not None else {}),
                },
                created_at=timestamp,
            )
            steering_rows = connection.execute(
                """
                SELECT steering_id, instruction, sequence FROM task_steering
                WHERE task_id = ? ORDER BY sequence
                """,
                (selected["task_id"],),
            ).fetchall()
            steering = tuple(str(row["instruction"]) for row in steering_rows)
            steering_deliveries = [
                {
                    "steering_id": str(row["steering_id"]),
                    "node_id": str(selected["node_id"]),
                    "attempt": attempt,
                    "event_cursor": self._event(
                        connection,
                        "task.steering_delivered",
                        selected["task_id"],
                        selected["node_id"],
                        {
                            "steering_id": str(row["steering_id"]),
                            "instruction": str(row["instruction"]),
                            "sequence": int(row["sequence"]),
                            "node_id": str(selected["node_id"]),
                            "attempt": attempt,
                        },
                        created_at=timestamp,
                    ),
                }
                for row in steering_rows
            ]
            return {
                "task_id": selected["task_id"],
                "node_id": selected["node_id"],
                "attempt": attempt,
                "started_at": timestamp,
                "started_event_cursor": started_event_cursor,
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
                "steering_deliveries": steering_deliveries,
                **({
                    "blocked_worktree_recovery": selected_dirty_worktree_recovery,
                } if selected_dirty_worktree_recovery is not None else {}),
                **({
                    "failed_attempt_recovery": selected_failed_attempt_recovery,
                } if selected_failed_attempt_recovery is not None else {}),
            }

    def _claim_repository_identities(self) -> dict[str, str]:
        """Resolve repositories for a claim without holding a SQLite write lock."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT t.contract_json
                FROM tasks t JOIN nodes n USING(task_id)
                WHERE n.state IN ('pending', 'running')
                  AND t.state IN ('queued', 'running', 'verifying', 'needs_fix')
                """
            ).fetchall()
        repositories = {
            str(json.loads(row["contract_json"])["repository"])
            for row in rows
        }
        return {
            repository: _repository_identity(repository)
            for repository in repositories
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
        if recovery.get("schema_version") in {2, 3, 5, 6}:
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
        elif recovery.get("schema_version") not in {1, 4}:
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

    def prevalidate_dirty_worktree_recovery_target(
        self,
        task_id: str,
        node_id: str,
        worktree: str,
        *,
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
    ) -> dict[str, Any]:
        """Perform recovery filesystem validation before the assignment CAS.

        SQLite remains uninvolved while Git and artifact content are read. The
        following assignment rechecks the immutable receipt and lease only.
        """

        with self.connection() as connection:
            task = connection.execute(
                "SELECT contract_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            node = connection.execute(
                """
                SELECT state, attempt, coordinator_epoch, lease_epoch, worktree, recovery_json
                FROM nodes WHERE task_id = ? AND node_id = ?
                """,
                (task_id, node_id),
            ).fetchone()
        if task is None or node is None:
            raise KeyError((task_id, node_id))
        if (
            node["state"] != "running"
            or int(node["attempt"]) != attempt
            or int(node["coordinator_epoch"]) != coordinator_epoch
            or int(node["lease_epoch"]) != lease_epoch
            or node["worktree"] is not None
        ):
            raise StateConflictError(f"node {node_id} lease is stale")
        try:
            contract = json.loads(str(task["contract_json"]))
        except json.JSONDecodeError as error:
            raise StateConflictError("dirty-worktree recovery task contract is invalid JSON") from error
        recovery_binding = self._current_dirty_worktree_recovery_binding(
            node["recovery_json"], next_attempt=attempt
        )
        target, patch = self._validate_dirty_worktree_recovery_target(
            contract=contract,
            task_id=task_id,
            node_id=node_id,
            attempt=attempt,
            worktree=worktree,
            recovery_binding=recovery_binding,
        )
        return {
            "schema_version": 1,
            "kind": _DIRTY_WORKTREE_RECOVERY_KIND,
            "authorization_revision": recovery_binding["authorization_revision"],
            "source_allocation_id": recovery_binding["source_allocation_id"],
            "target_worktree": str(target),
            "target_branch": WorktreeManager.branch_name(task_id, node_id, attempt),
            "patch_sha256": sha256(patch).hexdigest(),
        }

    def assign_worktree(
        self,
        task_id: str,
        node_id: str,
        worktree: str,
        *,
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
        recovery_preflight: dict[str, Any] | None = None,
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
                expected_preflight = {
                    "schema_version": 1,
                    "kind": _DIRTY_WORKTREE_RECOVERY_KIND,
                    "authorization_revision": recovery_binding["authorization_revision"],
                    "source_allocation_id": recovery_binding["source_allocation_id"],
                    "target_worktree": worktree,
                    "target_branch": branch,
                    "patch_sha256": recovery_binding["recovery"]["patch_sha256"],
                }
                if not isinstance(recovery_preflight, dict) or any(
                    recovery_preflight.get(key) != value
                    for key, value in expected_preflight.items()
                ):
                    raise StateConflictError(
                        "dirty-worktree recovery target must be prevalidated before assignment"
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
                        "target_worktree": worktree,
                        "target_branch": branch,
                    }
                )
                assigned_worktree = worktree
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

    def assign_failed_attempt_recovery_worktree(
        self,
        task_id: str,
        node_id: str,
        worktree: str,
        *,
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
        binding: dict[str, Any],
        recovery: dict[str, Any] | None,
        recovery_ref: str | None,
        prepared_recovery: dict[str, Any] | None,
    ) -> None:
        """Commit an already-validated failed-attempt target under its lease.

        The coordinator must perform all capture, artifact, and Git checks
        before this call. This method intentionally performs only durable
        shape, lineage, and CAS checks, so no slow filesystem operation holds
        the SQLite write transaction.
        """

        expected_branch = WorktreeManager.branch_name(task_id, node_id, attempt)
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            task = connection.execute(
                "SELECT contract_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            node = connection.execute(
                """
                SELECT state, attempt, coordinator_epoch, lease_epoch, worktree, recovery_json
                FROM nodes WHERE task_id = ? AND node_id = ?
                """,
                (task_id, node_id),
            ).fetchone()
            if task is None or node is None:
                raise KeyError((task_id, node_id))
            if (
                node["state"] != "running"
                or int(node["attempt"]) != attempt
                or int(node["coordinator_epoch"]) != coordinator_epoch
                or int(node["lease_epoch"]) != lease_epoch
                or node["worktree"] is not None
            ):
                raise StateConflictError(f"node {node_id} lease is stale")
            current = self._failed_attempt_recovery_binding(
                node["recovery_json"], next_attempt=attempt
            )
            if current is None:
                raise StateConflictError("failed-attempt recovery binding is missing")
            for field in (
                "authorization_revision",
                "source_allocation_id",
                "source_result_json",
                "source",
            ):
                if binding.get(field) != current.get(field):
                    raise StateConflictError("failed-attempt recovery binding changed before assignment")
            try:
                contract = json.loads(str(task["contract_json"]))
            except json.JSONDecodeError as error:
                raise StateConflictError("failed-attempt recovery task contract is invalid JSON") from error
            source = current["source"]
            if source["base_sha"] != contract.get("base_sha"):
                raise StateConflictError("failed-attempt recovery base does not match task contract")
            allocation = connection.execute(
                "SELECT state, current_path, branch, base_sha FROM worktree_allocations WHERE allocation_id = ?",
                (current["source_allocation_id"],),
            ).fetchone()
            if (
                allocation is None
                or allocation["state"] != "active"
                or allocation["current_path"] != source["worktree"]
                or allocation["branch"] != source["branch"]
                or allocation["base_sha"] != source["base_sha"]
            ):
                raise StateConflictError("failed-attempt recovery source allocation changed before assignment")
            existing = connection.execute(
                "SELECT allocation_id FROM worktree_allocations WHERE task_id = ? AND node_id = ? AND attempt = ?",
                (task_id, node_id, attempt),
            ).fetchone()
            if existing is not None:
                raise StateConflictError("failed-attempt recovery target allocation already exists")

            stored_recovery: str | None = None
            if recovery is not None:
                if (
                    not isinstance(recovery_ref, str)
                    or not recovery_ref
                    or not isinstance(prepared_recovery, dict)
                    or recovery.get("source_attempt") != source["attempt"]
                    or recovery.get("source_worktree") != source["worktree"]
                    or recovery.get("source_branch") != source["branch"]
                    or recovery.get("base_sha") != source["base_sha"]
                    or recovery.get("changed_paths") != source["changed_paths"]
                    or (
                        source.get("generated_residue_paths")
                        and recovery.get("generated_residue_paths")
                        != source["generated_residue_paths"]
                    )
                    or not isinstance(recovery.get("patch_ref"), str)
                    or not recovery["patch_ref"]
                    or not isinstance(recovery.get("patch_sha256"), str)
                    or not recovery["patch_sha256"]
                ):
                    raise StateConflictError("failed-attempt recovery capture does not match its source")
                expected_prepared = {
                    "target_attempt": attempt,
                    "target_worktree": worktree,
                    "target_branch": expected_branch,
                    "target_patch_sha256": recovery["patch_sha256"],
                }
                if any(
                    prepared_recovery.get(key) != value
                    for key, value in expected_prepared.items()
                ):
                    raise StateConflictError(
                        "failed-attempt recovery target does not match its prepared patch"
                    )
                stored_recovery = canonical_json(
                    {
                        "schema_version": 1,
                        "kind": _FAILED_ATTEMPT_RECOVERY_KIND,
                        "state": "assigned",
                        "authorization_revision": current["authorization_revision"],
                        "source_allocation_id": current["source_allocation_id"],
                        "source_result_json": current["source_result_json"],
                        "source": source,
                        "recovery": recovery,
                        "recovery_ref": recovery_ref,
                        "target_attempt": attempt,
                        "target_worktree": worktree,
                        "target_branch": expected_branch,
                    }
                )
            elif recovery_ref is not None or prepared_recovery is not None:
                raise StateConflictError("clean failed-attempt recovery cannot carry a recovery artifact")

            timestamp = now_iso()
            changed = connection.execute(
                """
                UPDATE nodes SET worktree = ?, recovery_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'running'
                  AND attempt = ? AND coordinator_epoch = ? AND lease_epoch = ?
                """,
                (
                    worktree,
                    stored_recovery,
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
            superseded = connection.execute(
                """
                UPDATE worktree_allocations SET state = 'superseded', updated_at = ?
                WHERE allocation_id = ? AND state = 'active'
                """,
                (timestamp, current["source_allocation_id"]),
            ).rowcount
            if superseded != 1:
                raise StateConflictError("failed-attempt recovery source allocation is not active")
            allocation_id = "wta-" + canonical_hash(
                {"task_id": task_id, "node_id": node_id, "attempt": attempt}
            )[:24]
            connection.execute(
                """
                INSERT INTO worktree_allocations(
                    allocation_id, task_id, node_id, attempt, repository,
                    base_sha, branch, current_path, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    allocation_id,
                    task_id,
                    node_id,
                    attempt,
                    contract["repository"],
                    contract["base_sha"],
                    expected_branch,
                    worktree,
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
                    "path": worktree,
                    "branch": expected_branch,
                },
                created_at=timestamp,
            )
            self._event(
                connection,
                "node.failed_attempt_recovery_prepared",
                task_id,
                node_id,
                {
                    "source_attempt": source["attempt"],
                    "source_allocation_id": current["source_allocation_id"],
                    "target_attempt": attempt,
                    "target_worktree": worktree,
                    "target_branch": expected_branch,
                    "recovery_ref": recovery_ref,
                    "recovered_changes": recovery is not None,
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
        # Failed-attempt recovery has its own pre-dispatch lifecycle.  Its
        # structural/lease validation happens in ``settle_node`` before this
        # blocked-recovery-specific parser is reached.
        if stored.get("kind") == _FAILED_ATTEMPT_RECOVERY_KIND:
            return None
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

    def _rollback_failed_attempt_recovery(
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
        """Restore failed a1 state if its retry cannot be captured safely."""

        if (
            result.provider != _FAILED_ATTEMPT_RECOVERY_PROVIDER
            or result.status not in {"failed", "blocked"}
            or result.result_kind != "worker"
        ):
            raise StateConflictError(
                "failed-attempt recovery must be rejected before a normal worker can settle"
            )
        source = recovery["source"]
        allocation = connection.execute(
            """
            SELECT state, current_path, branch, base_sha FROM worktree_allocations
            WHERE allocation_id = ?
            """,
            (recovery["source_allocation_id"],),
        ).fetchone()
        if (
            allocation is None
            or allocation["state"] != "active"
            or allocation["current_path"] != source["worktree"]
            or allocation["branch"] != source["branch"]
            or allocation["base_sha"] != source["base_sha"]
        ):
            raise StateConflictError(
                "failed-attempt recovery source allocation is no longer active"
            )
        changed = connection.execute(
            """
            UPDATE nodes
            SET state = 'failed', attempt = ?, worker_id = NULL, worktree = ?,
                effective_executor = NULL, effective_model = NULL,
                started_at = NULL, settled_at = ?, result_json = ?, recovery_json = NULL,
                coordinator_epoch = 0, lease_epoch = 0, updated_at = ?
            WHERE task_id = ? AND node_id = ? AND state = 'running'
              AND attempt = ? AND coordinator_epoch = ? AND lease_epoch = ?
            """,
            (
                source["attempt"],
                source["worktree"],
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
            raise StateConflictError("failed-attempt recovery rollback lease is stale")
        task = connection.execute(
            "SELECT state, state_revision, blocker, verdict FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert task is not None
        blocker = f"failed-attempt recovery rejected: {result.summary}"
        control_state_preserved = task["state"] in {"paused", "cancelled"}
        if control_state_preserved:
            revision = int(task["state_revision"])
        else:
            revision = int(task["state_revision"]) + 1
            connection.execute(
                """
                UPDATE tasks
                SET state = 'needs_fix', state_revision = ?, updated_at = ?,
                    blocker = ?, verdict = NULL
                WHERE task_id = ?
                """,
                (revision, timestamp, blocker, task_id),
            )
        self._event(
            connection,
            "node.failed_attempt_recovery_rejected",
            task_id,
            node_id,
            {
                "attempt": int(row["attempt"]),
                "source_attempt": source["attempt"],
                "source_allocation_id": recovery["source_allocation_id"],
                "source": source,
                "rejection": result.to_dict(),
                "task_revision": revision,
                "control_state_preserved": control_state_preserved,
            },
            created_at=timestamp,
        )
        if control_state_preserved:
            self._event(
                connection,
                "task.control_state_preserved",
                task_id,
                node_id,
                {
                    "state": task["state"],
                    "revision": revision,
                    "during": "failed_attempt_recovery_rollback",
                },
                created_at=timestamp,
            )
        else:
            self._event(
                connection,
                "task.state_changed",
                task_id,
                None,
                {
                    "from": task["state"],
                    "to": "needs_fix",
                    "revision": revision,
                    "blocker": blocker,
                    "recovery_rolled_back": True,
                },
                created_at=timestamp,
            )

    def _prevalidate_settlement(
        self,
        task_id: str,
        node_id: str,
        result: NodeResult,
        *,
        attempt: int,
        coordinator_epoch: int,
        lease_epoch: int,
    ) -> dict[str, Any]:
        """Validate a settlement's leased artifact inputs without a write lock."""

        with self.connection() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            row = connection.execute(
                """
                SELECT n.state, n.attempt, n.coordinator_epoch, n.lease_epoch, n.worktree,
                       n.effective_executor, n.effective_model, n.spec_json, n.recovery_json,
                       t.contract_json
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
            signature = {
                field: row[field]
                for field in (
                    "state",
                    "attempt",
                    "coordinator_epoch",
                    "lease_epoch",
                    "worktree",
                    "effective_executor",
                    "effective_model",
                    "spec_json",
                    "recovery_json",
                    "contract_json",
                )
            }
            failed_attempt_recovery = self._failed_attempt_recovery_binding(
                row["recovery_json"],
                next_attempt=attempt,
                allow_assigned=True,
            )
            if (
                failed_attempt_recovery is not None
                and failed_attempt_recovery["state"] == "capture_pending"
            ):
                return signature
            spec = json.loads(row["spec_json"])
            self._validate_result_contract(
                connection,
                task_id,
                node_id,
                spec,
                row,
                result,
            )
            self._verify_artifact_refs(result.artifacts)
            if spec.get("verifier") and spec.get("executor") != "fixture":
                for ref in result.evidence:
                    self.artifacts.verify(ref)
            return signature

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
        preflight_signature = self._prevalidate_settlement(
            task_id,
            node_id,
            result,
            attempt=attempt,
            coordinator_epoch=coordinator_epoch,
            lease_epoch=lease_epoch,
        )
        with self.transaction() as connection:
            self._assert_active_coordinator(connection, coordinator_epoch)
            row = connection.execute(
                """
                SELECT n.node_id, n.state, n.attempt, n.coordinator_epoch, n.lease_epoch, n.worktree,
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
            if any(row[field] != value for field, value in preflight_signature.items()):
                raise StateConflictError("node settlement inputs changed before commit")
            failed_attempt_recovery = self._failed_attempt_recovery_binding(
                row["recovery_json"],
                next_attempt=attempt,
                allow_assigned=True,
            )
            if (
                failed_attempt_recovery is not None
                and failed_attempt_recovery["state"] == "capture_pending"
            ):
                self._rollback_failed_attempt_recovery(
                    connection,
                    task_id=task_id,
                    node_id=node_id,
                    row=row,
                    recovery=failed_attempt_recovery,
                    result=result,
                    timestamp=timestamp,
                )
                return
            spec = json.loads(row["spec_json"])
            contract = json.loads(row["contract_json"])
            recovery = self._dirty_worktree_recovery_for_settlement(
                row["recovery_json"],
                attempt=attempt,
            )
            self._validate_result_contract(
                connection,
                task_id,
                node_id,
                spec,
                row,
                result,
                verify_artifacts=False,
            )
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
            result_json = canonical_json(result.to_dict())
            task = connection.execute(
                "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert task is not None
            task_revision = int(task["state_revision"])
            retry_recovery: dict[str, Any] | None = None
            retry_recovery_error: str | None = None
            if (
                node_state in {"failed", "blocked"}
                and not spec.get("verifier")
                and result.retryable
                and int(row["attempt"]) <= int(contract.get("retry_limit", 0))
            ):
                try:
                    retry_recovery = self._failed_attempt_recovery_authorization(
                        connection,
                        task_id,
                        row,
                        authorization_revision=task_revision + 1,
                        source_result_json=result_json,
                    )
                except StateConflictError as error:
                    retry_recovery_error = str(error)
            if retry_recovery is None:
                connection.execute(
                    """
                    UPDATE nodes
                    SET state = ?, settled_at = ?, updated_at = ?, result_json = ?, recovery_json = NULL
                    WHERE task_id = ? AND node_id = ?
                    """,
                    (node_state, timestamp, timestamp, result_json, task_id, node_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE nodes
                    SET state = 'pending', worker_id = NULL, worktree = NULL,
                        effective_executor = NULL, effective_model = NULL,
                        started_at = NULL, settled_at = NULL, result_json = NULL,
                        recovery_json = ?, coordinator_epoch = 0, lease_epoch = 0, updated_at = ?
                    WHERE task_id = ? AND node_id = ?
                    """,
                    (canonical_json(retry_recovery), timestamp, task_id, node_id),
                )
            connection.execute(
                """
                UPDATE worktree_allocations
                SET node_result_json = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND attempt = ?
                """,
                (result_json, timestamp, task_id, node_id, attempt),
            )
            node_event = {"attempt": row["attempt"], "result": result.to_dict()}
            if recovery is not None:
                node_event["blocked_worktree_recovery"] = {
                    "source_attempt": recovery["recovery"]["source_attempt"],
                    "source_allocation_id": recovery["source_allocation_id"],
                    "recovery": recovery["recovery"],
                }
            if failed_attempt_recovery is not None:
                node_event["failed_attempt_recovery"] = {
                    "source_attempt": failed_attempt_recovery["source"]["attempt"],
                    "source_allocation_id": failed_attempt_recovery["source_allocation_id"],
                    "recovery_ref": failed_attempt_recovery.get("recovery_ref"),
                }
            if retry_recovery is not None:
                node_event["failed_attempt_recovery"] = {
                    "source_attempt": retry_recovery["source"]["attempt"],
                    "source_allocation_id": retry_recovery["source_allocation_id"],
                    "state": "capture_pending",
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

            next_state = task["state"]
            blocker: str | None = None
            verdict: str | None = None
            # A control transition that committed after settlement preflight
            # owns the task state. The leased node result may still be recorded,
            # but it must not undo an operator pause or cancellation.
            if task["state"] in {"paused", "cancelled"}:
                pass
            elif node_state == "accepted" and spec.get("verifier"):
                next_state = "accepted"
                verdict = result.summary
            elif node_state == "indeterminate":
                next_state = "needs_approval"
                blocker = f"node {node_id} has an indeterminate result"
            elif retry_recovery_error is not None:
                next_state = "needs_fix"
                blocker = f"retryable attempt requires manual recovery: {retry_recovery_error}"
                self._event(
                    connection,
                    "node.retry_recovery_rejected",
                    task_id,
                    node_id,
                    {"attempt": int(row["attempt"]), "reason": retry_recovery_error},
                )
            elif retry_recovery is not None:
                next_state = "queued"
                self._event(
                    connection,
                    "node.retry_scheduled",
                    task_id,
                    node_id,
                    {
                        "attempt": int(row["attempt"]),
                        "next_attempt": int(row["attempt"]) + 1,
                        "failed_attempt_recovery": "capture_pending",
                        "source_allocation_id": retry_recovery["source_allocation_id"],
                    },
                )
            elif (
                node_state == "blocked"
                and result.retryable
                and int(row["attempt"]) <= int(contract.get("retry_limit", 0))
            ):
                connection.execute(
                    """
                    UPDATE nodes SET state = 'pending', worker_id = NULL,
                                     started_at = NULL, settled_at = NULL, updated_at = ?
                    WHERE task_id = ? AND node_id = ?
                    """,
                    (timestamp, task_id, node_id),
                )
                next_state = "queued"
                self._event(
                    connection,
                    "node.retry_scheduled",
                    task_id,
                    node_id,
                    {"attempt": row["attempt"], "typed_blocked_result": True},
                )
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

    def _validate_execution_attribution(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        node_id: str,
        attempt: int,
        spec: dict[str, Any],
        contract: dict[str, Any],
        result: NodeResult,
        verify_artifacts: bool = True,
    ) -> ExecutionAttribution | None:
        """Validate bounded attribution against this durable node attempt."""

        raw = result.execution_attribution
        if raw is None:
            return None
        try:
            attribution = ExecutionAttribution.from_dict(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"execution attribution is invalid: {error}") from error
        state = attribution.state
        if (state.task_id, state.node_id, state.attempt) != (task_id, node_id, attempt):
            raise ValueError("execution attribution state does not match the settled node attempt")

        artifact_refs = set(result.artifacts.values())
        expected_snapshot_ids = {
            value
            for value in (
                spec.get("capability_snapshot_id"),
                contract.get("capability_snapshot_id"),
            )
            if isinstance(value, str) and value
        }
        references = (
            *attribution.references,
            *attribution.requested_model.provenance.references,
            *attribution.observed_model.provenance.references,
            *attribution.physical_call.provenance.references,
            *attribution.failure.references,
            *(reference for candidate in attribution.candidate_decisions for reference in candidate.references),
            *(
                reference
                for condition in (
                    attribution.conditions.dependency,
                    attribution.conditions.scope,
                    attribution.conditions.provider_quota,
                    attribution.conditions.environment_readiness,
                    attribution.conditions.cpu_wait,
                    attribution.conditions.memory_wait,
                    attribution.conditions.io_wait,
                )
                for reference in condition.references
            ),
        )
        for reference in references:
            if reference.kind in {"artifact", "readiness_report", "dependency_report"}:
                if reference.ref not in artifact_refs:
                    raise ValueError(
                        "execution attribution artifact reference is not present in this result"
                    )
                if verify_artifacts:
                    self.artifacts.verify(reference.ref)
            elif reference.kind == "event":
                if reference.cursor is None:
                    raise ValueError("execution attribution event reference requires a cursor")
                event = connection.execute(
                    "SELECT event_type, task_id, node_id, payload_json FROM events WHERE cursor = ?",
                    (reference.cursor,),
                ).fetchone()
                try:
                    event_payload = json.loads(event["payload_json"]) if event is not None else None
                except (TypeError, json.JSONDecodeError):
                    event_payload = None
                if (
                    event is None
                    or event["event_type"] != reference.ref
                    or event["task_id"] != task_id
                    or event["node_id"] != node_id
                    or not isinstance(event_payload, dict)
                    or event_payload.get("attempt") != attempt
                ):
                    raise ValueError("execution attribution event reference is not this node attempt")
            elif reference.kind == "quota_snapshot":
                prefix, separator, raw_id = reference.ref.partition(":")
                if prefix != "claude" or not separator or not raw_id.isdigit():
                    raise ValueError("execution attribution quota reference is invalid")
                snapshot = connection.execute(
                    "SELECT id FROM quota_snapshots WHERE provider = 'claude' AND id = ?",
                    (int(raw_id),),
                ).fetchone()
                if snapshot is None:
                    raise ValueError("execution attribution quota reference is unavailable")
            elif reference.kind == "scope_contract":
                if reference.ref != f"task:{task_id}":
                    raise ValueError("execution attribution scope reference does not match this task")
            elif reference.kind == "snapshot" and reference.ref not in expected_snapshot_ids:
                raise ValueError("execution attribution capability snapshot is not pinned by this node")
            elif reference.kind != "snapshot":
                raise ValueError("execution attribution reference kind is not persisted by this runtime")

        if state.event_cursor is not None:
            started = connection.execute(
                "SELECT event_type, task_id, node_id, payload_json FROM events WHERE cursor = ?",
                (state.event_cursor,),
            ).fetchone()
            try:
                started_payload = json.loads(started["payload_json"]) if started is not None else None
            except (TypeError, json.JSONDecodeError):
                started_payload = None
            if (
                started is None
                or started["event_type"] != "node.started"
                or started["task_id"] != task_id
                or started["node_id"] != node_id
                or not isinstance(started_payload, dict)
                or started_payload.get("attempt") != attempt
            ):
                raise ValueError("execution attribution state cursor is not this node.started event")

        observed = attribution.observed_model
        if observed.status == "attested" and result.actual_model != observed.model_id:
            raise ValueError(
                "legacy actual_model must match an attested observed model when attribution is present"
            )
        return attribution

    def _validate_result_contract(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        node_id: str,
        spec: dict[str, Any],
        row: sqlite3.Row,
        result: NodeResult,
        *,
        verify_artifacts: bool = True,
    ) -> None:
        recovery = self._dirty_worktree_recovery_for_settlement(
            row["recovery_json"],
            attempt=int(row["attempt"]),
        )
        contract = json.loads(row["contract_json"])
        self._validate_execution_attribution(
            connection,
            task_id=task_id,
            node_id=node_id,
            attempt=int(row["attempt"]),
            spec=spec,
            contract=contract,
            result=result,
            verify_artifacts=verify_artifacts,
        )
        if spec.get("executor") == "fixture" or spec.get("model") == "fixture":
            if recovery is None:
                return
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
            and verify_artifacts
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
            # New coordinator results carry typed attribution.  Codex's
            # selected CLI argument is not a native observation, so an absent
            # legacy actual_model is valid only when that attribution records
            # the identity as unknown/unattested. Legacy direct-store callers
            # keep the historic stricter requirement below.
            and not (
                result.actual_model is None and result.execution_attribution is not None
            )
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
        recovered, _ = self.recover_interrupted_with_orphans()
        return recovered

    def recover_interrupted_with_orphans(
        self,
    ) -> tuple[int, tuple[dict[str, Any], ...]]:
        """Fence interrupted nodes and return unassigned retry targets for cleanup."""

        timestamp = now_iso()
        orphaned_targets: list[dict[str, Any]] = []
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT n.*, t.contract_json
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.state = 'running'
                """
            ).fetchall()
            for row in rows:
                spec = json.loads(row["spec_json"])
                failed_recovery = self._failed_attempt_recovery_binding(
                    row["recovery_json"],
                    next_attempt=int(row["attempt"]),
                    allow_assigned=True,
                )
                if (
                    failed_recovery is not None
                    and failed_recovery["state"] == "capture_pending"
                ):
                    source = failed_recovery["source"]
                    try:
                        contract = json.loads(str(row["contract_json"]))
                    except json.JSONDecodeError as error:
                        raise StateConflictError(
                            "failed-attempt recovery task contract is invalid JSON"
                        ) from error
                    repository = contract.get("repository") if isinstance(contract, dict) else None
                    if not isinstance(repository, str) or not repository:
                        raise StateConflictError(
                            "failed-attempt recovery task repository is invalid"
                        )
                    orphan = {
                        "cleanup_id": "orphan-" + canonical_hash(
                            {
                                "task_id": row["task_id"],
                                "node_id": row["node_id"],
                                "attempt": row["attempt"],
                                "authorization_revision": failed_recovery[
                                    "authorization_revision"
                                ],
                            }
                        )[:24],
                        "task_id": str(row["task_id"]),
                        "node_id": str(row["node_id"]),
                        "attempt": int(row["attempt"]),
                        "repository": repository,
                        "branch": WorktreeManager.branch_name(
                            str(row["task_id"]),
                            str(row["node_id"]),
                            int(row["attempt"]),
                        ),
                    }
                    orphaned_targets.append(orphan)
                    result = NodeResult(
                        status="failed",
                        summary=(
                            "coordinator restarted before failed-attempt recovery assignment; "
                            "the original failed attempt was restored"
                        ),
                        result_kind="worker",
                        changed_paths=tuple(source["changed_paths"]),
                        checks=("RECOVERED: pre-assignment retry was rolled back to its source",),
                        provider=_FAILED_ATTEMPT_RECOVERY_PROVIDER,
                        actual_model=None,
                    )
                    self._rollback_failed_attempt_recovery(
                        connection,
                        task_id=str(row["task_id"]),
                        node_id=str(row["node_id"]),
                        row=row,
                        recovery=failed_recovery,
                        result=result,
                        timestamp=timestamp,
                    )
                    self._event(
                        connection,
                        "failed_attempt_recovery.orphan_cleanup_pending",
                        str(row["task_id"]),
                        str(row["node_id"]),
                        orphan,
                        created_at=timestamp,
                    )
                    continue
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
            return len(rows), tuple(orphaned_targets)

    def pending_failed_attempt_recovery_orphans(self) -> tuple[dict[str, Any], ...]:
        """Return durable orphan cleanups without a terminal resolution event."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT pending.payload_json
                FROM events pending
                WHERE pending.event_type =
                      'failed_attempt_recovery.orphan_cleanup_pending'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM events resolved
                      WHERE resolved.event_type =
                            'failed_attempt_recovery.orphan_resolved'
                        AND json_extract(resolved.payload_json, '$.cleanup_id') =
                            json_extract(pending.payload_json, '$.cleanup_id')
                  )
                ORDER BY pending.cursor
                """
            ).fetchall()
        pending: list[dict[str, Any]] = []
        required = {
            "cleanup_id",
            "task_id",
            "node_id",
            "attempt",
            "repository",
            "branch",
        }
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as error:
                raise StateConflictError(
                    "failed-attempt orphan cleanup receipt is invalid JSON"
                ) from error
            if (
                not isinstance(payload, dict)
                or set(payload) != required
                or any(
                    not isinstance(payload[field], str) or not payload[field]
                    for field in required - {"attempt"}
                )
                or isinstance(payload["attempt"], bool)
                or not isinstance(payload["attempt"], int)
                or payload["attempt"] < 1
            ):
                raise StateConflictError(
                    "failed-attempt orphan cleanup receipt is invalid"
                )
            pending.append(payload)
        return tuple(pending)

    def write_quota(self, snapshot: QuotaSnapshot) -> None:
        snapshot.validate()
        raw_snapshot = snapshot.raw_payload()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO quota_snapshots(provider, snapshot_json, observed_at)
                VALUES('claude', ?, ?)
                """,
                (canonical_json(raw_snapshot), snapshot.observed_at),
            )
            self._event(
                connection,
                "quota.updated",
                None,
                None,
                {"provider": "claude", "snapshot": raw_snapshot},
            )

    @staticmethod
    def _quota_snapshot_from_row(row: sqlite3.Row) -> QuotaSnapshot:
        """Hydrate immutable collector evidence and bind its ledger row ID."""

        payload = json.loads(str(row["snapshot_json"]))
        if not isinstance(payload, dict):
            raise StateConflictError("quota snapshot ledger row is not an object")
        # These are read-time projection fields; a raw row may never smuggle
        # them back into the ledger selection result.
        payload.pop("ledger_id", None)
        payload.pop("effective_observation", None)
        payload.pop("effective_admission_blocked", None)
        return replace(QuotaSnapshot(**payload), ledger_id=int(row["id"]))

    @staticmethod
    def _quota_receipt(
        raw: QuotaSnapshot,
        effective: QuotaSnapshot,
        *,
        selection: str,
        recovery_reason: str,
    ) -> dict[str, Any]:
        """Describe a ledger projection without adding a quota authority."""

        def provenance(snapshot: QuotaSnapshot) -> dict[str, Any]:
            return {
                "provider": "claude",
                "source": snapshot.source,
                "producer": snapshot.producer,
                "producer_schema_version": snapshot.producer_schema_version,
                "claude_version": snapshot.claude_version,
                "five_hour_window_id": snapshot.five_hour_window_id,
                "weekly_window_id": snapshot.weekly_window_id,
            }

        return {
            "selection": selection,
            "raw_snapshot_id": raw.ledger_id,
            "effective_snapshot_id": effective.ledger_id,
            "raw_observed_at": raw.observed_at,
            "effective_observed_at": effective.observed_at,
            "authentication": (
                "native-subscription-authenticated"
                if raw.auth_ok and raw.auth_method == "native-subscription"
                else "unavailable"
            ),
            "quota_collection": raw.quota_collection_status(),
            "raw_observation_status": raw.observation_status(),
            "recovery_reason": recovery_reason,
            "admission_blocked": effective.effective_admission_blocked,
            "raw_provenance": provenance(raw),
            "effective_provenance": provenance(effective),
        }

    @staticmethod
    def _empty_reset_binding_reason(
        empty: QuotaSnapshot,
        complete: QuotaSnapshot,
        *,
        current_time: datetime | None,
    ) -> str | None:
        """Return why an empty row cannot borrow a complete ledger row.

        A producer-confirmed collection failure has no parsed pool/reset
        payload.  It may borrow only before the selected complete row's known
        reset deadlines.  An otherwise empty row with unknown bindings remains
        uncertain and therefore fails closed.
        """

        empty_windows = (empty.five_hour_window_id, empty.weekly_window_id)
        complete_windows = (complete.five_hour_window_id, complete.weekly_window_id)
        if all(isinstance(value, str) and value for value in empty_windows):
            if empty_windows != complete_windows:
                return "reset-window-mismatch"
            if not empty.has_current_confident_reset_windows(current_time=current_time):
                return "empty-reset-binding-unknown-or-expired"
            return None
        if (
            empty.five_hour_window_id is None
            and empty.weekly_window_id is None
            and empty.collection_state == "failed"
        ):
            observed = empty.observed_datetime()
            deadlines = complete.reset_window_deadlines()
            if (
                observed is None
                or deadlines is None
                or any(observed >= deadline for deadline in deadlines)
            ):
                return "empty-reset-binding-unknown-or-expired"
            return None
        return "empty-reset-binding-unknown-or-expired"

    @staticmethod
    def _same_raw_observation(snapshots: list[QuotaSnapshot]) -> bool:
        """Whether a same-timestamp group is genuinely one raw observation."""

        return len({canonical_json(snapshot.raw_payload()) for snapshot in snapshots}) <= 1

    @staticmethod
    def _safety_preference(snapshot: QuotaSnapshot) -> tuple[int, float, int]:
        """Choose the least permissive actual row for an ambiguous tie."""

        status = snapshot.observation_status()
        identifier = snapshot.ledger_id or 0
        if status == "authentication-unavailable":
            return 0, 0.0, -identifier
        if not snapshot.has_compatible_subscription_provenance():
            return 1, 0.0, -identifier
        if status == "authenticated-partial":
            values = [
                value
                for value in (
                    snapshot.five_hour_remaining,
                    snapshot.weekly_all_remaining,
                    snapshot.weekly_sonnet_remaining,
                    snapshot.weekly_fable_remaining,
                )
                if value is not None
            ]
            return 2, min(values) if values else 0.0, -identifier
        if status == "authenticated-empty":
            return 3, 0.0, -identifier
        values = [
            value
            for value in (
                snapshot.five_hour_remaining,
                snapshot.weekly_all_remaining,
                snapshot.weekly_sonnet_remaining,
                snapshot.weekly_fable_remaining,
            )
            if value is not None
        ]
        return 4, min(values) if values else 0.0, -identifier

    @staticmethod
    def _ordered_quota_observations(
        snapshots: list[QuotaSnapshot],
    ) -> tuple[QuotaSnapshot, list[QuotaSnapshot], frozenset[datetime], tuple[int, ...], str | None]:
        """Pick current raw evidence by observed time, not append race order.

        A later append carrying an older observation cannot replace newer auth
        loss or low-pool evidence.  Invalid timestamps written after the newest
        orderable row are also an admission barrier because their placement is
        unknowable.
        """

        valid: list[tuple[datetime, QuotaSnapshot]] = []
        invalid: list[QuotaSnapshot] = []
        for snapshot in snapshots:
            observed = snapshot.observed_datetime()
            if observed is None:
                invalid.append(snapshot)
            else:
                valid.append((observed, snapshot))
        if not valid:
            raw = max(snapshots, key=lambda snapshot: snapshot.ledger_id or 0)
            return (
                replace(raw, effective_admission_blocked=True),
                [],
                frozenset(),
                tuple(snapshot.ledger_id or 0 for snapshot in invalid),
                "invalid-observation-time",
            )

        newest_time = max(observed for observed, _snapshot in valid)
        newest_group = [
            snapshot for observed, snapshot in valid if observed == newest_time
        ]
        newest_insert_id = max(snapshot.ledger_id or 0 for snapshot in newest_group)
        invalid_ids = tuple(snapshot.ledger_id or 0 for snapshot in invalid)
        if invalid and max(invalid_ids) > newest_insert_id:
            raw = max(invalid, key=lambda snapshot: snapshot.ledger_id or 0)
            return (
                replace(raw, effective_admission_blocked=True),
                [],
                frozenset(),
                invalid_ids,
                "invalid-observation-time",
            )

        grouped: dict[datetime, list[QuotaSnapshot]] = {}
        for observed, snapshot in valid:
            grouped.setdefault(observed, []).append(snapshot)
        ambiguous_times = frozenset(
            observed
            for observed, group in grouped.items()
            if not WorkbenchStore._same_raw_observation(group)
        )
        raw = min(newest_group, key=WorkbenchStore._safety_preference)
        ordered = [
            snapshot
            for _observed, snapshot in sorted(
                valid,
                key=lambda item: (item[0], item[1].ledger_id or 0),
                reverse=True,
            )
        ]
        if newest_time in ambiguous_times:
            return (
                replace(raw, effective_admission_blocked=True),
                ordered,
                ambiguous_times,
                invalid_ids,
                "ambiguous-observation-order",
            )
        return raw, ordered, ambiguous_times, invalid_ids, None

    @staticmethod
    def _recovered_quota(
        snapshots: list[QuotaSnapshot],
        *,
        max_age_seconds: int | None,
        current_time: datetime | None,
        ambiguous_times: frozenset[datetime],
        invalid_observation_ids: tuple[int, ...],
    ) -> tuple[QuotaSnapshot, str, str]:
        """Select a bounded effective row from ordered raw ledger evidence.

        Only a current authenticated-empty row may borrow an older complete
        row.  Its original timestamp, reset windows, and durable ID stay
        intact.  The append order must agree with observed chronology along
        the recovery run, so a concurrent delayed write cannot mask a newer
        empty/auth-loss/low-pool observation.
        """

        newest = snapshots[0]
        status = newest.observation_status()
        if status == "complete":
            return newest, "latest", "latest-complete-observation"
        if status == "authentication-unavailable":
            return newest, "latest", "authentication-unavailable"
        if status == "authenticated-partial":
            return newest, "latest", "partial-observation-authoritative"
        assert status == "authenticated-empty"
        if max_age_seconds is None:
            return newest, "latest", "caller-freshness-limit-unavailable"
        if not newest.has_compatible_subscription_provenance():
            return newest, "latest", "incompatible-provenance"
        if not newest.is_fresh(
            max_age_seconds=max_age_seconds,
            current_time=current_time,
        ):
            return newest, "latest", "empty-observation-stale"
        if newest.observed_datetime() is None:
            return newest, "latest", "empty-observation-time-invalid"

        empty_run = [newest]
        previous_empty = newest
        for candidate in snapshots[1:]:
            candidate_observed = candidate.observed_datetime()
            previous_observed = previous_empty.observed_datetime()
            candidate_id = candidate.ledger_id or 0
            previous_id = previous_empty.ledger_id or 0
            if candidate_observed in ambiguous_times:
                return newest, "latest", "ambiguous-observation-order"
            if (
                candidate_observed is None
                or previous_observed is None
                or candidate_observed >= previous_observed
                or candidate_id >= previous_id
            ):
                return newest, "latest", "out-of-order-observation"
            if any(candidate_id < invalid_id < previous_id for invalid_id in invalid_observation_ids):
                return newest, "latest", "invalid-observation-time"

            candidate_status = candidate.observation_status()
            if candidate_status == "authentication-unavailable":
                return newest, "latest", "intervening-authentication-unavailable"
            if candidate_status == "authenticated-partial":
                return newest, "latest", "intervening-partial-observation-authoritative"
            if candidate_status == "authenticated-empty":
                if not candidate.has_compatible_subscription_provenance():
                    return newest, "latest", "incompatible-provenance"
                if candidate.recovery_identity() != newest.recovery_identity():
                    return newest, "latest", "source-identity-mismatch"
                if not candidate.is_fresh(
                    max_age_seconds=max_age_seconds,
                    current_time=current_time,
                ):
                    return newest, "latest", "empty-observation-stale"
                empty_run.append(candidate)
                previous_empty = candidate
                continue

            assert candidate_status == "complete"
            if not candidate.has_compatible_subscription_provenance():
                return newest, "latest", "incompatible-provenance"
            if candidate.recovery_identity() != newest.recovery_identity():
                return newest, "latest", "source-identity-mismatch"
            if not candidate.is_fresh(
                max_age_seconds=max_age_seconds,
                current_time=current_time,
            ):
                return newest, "latest", "complete-observation-stale"
            if not candidate.has_current_confident_reset_windows(current_time=current_time):
                return newest, "latest", "complete-reset-binding-unknown-or-expired"
            for empty in empty_run:
                binding_reason = WorkbenchStore._empty_reset_binding_reason(
                    empty,
                    candidate,
                    current_time=current_time,
                )
                if binding_reason is not None:
                    return newest, "latest", binding_reason
            return candidate, "last-known-good", "authenticated-empty-recovered"
        return newest, "latest", "no-compatible-complete-observation"

    def latest_quota(
        self,
        *,
        max_age_seconds: int | None = DEFAULT_QUOTA_TTL_SECONDS,
        current_time: datetime | None = None,
    ) -> QuotaSnapshot | None:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, snapshot_json FROM quota_snapshots
                WHERE provider = 'claude' ORDER BY id DESC
                """
            ).fetchall()
        if not rows:
            return None
        snapshots = [self._quota_snapshot_from_row(row) for row in rows]
        raw, ordered, ambiguous_times, invalid_ids, raw_reason = (
            self._ordered_quota_observations(snapshots)
        )
        if raw_reason is not None:
            effective, selection, recovery_reason = raw, "latest", raw_reason
        else:
            effective, selection, recovery_reason = self._recovered_quota(
                ordered,
                max_age_seconds=max_age_seconds,
                current_time=current_time,
                ambiguous_times=ambiguous_times,
                invalid_observation_ids=invalid_ids,
            )
        return replace(
            effective,
            effective_observation=self._quota_receipt(
                raw,
                effective,
                selection=selection,
                recovery_reason=recovery_reason,
            ),
        )

    def quota_snapshot_reference(self, snapshot: QuotaSnapshot) -> int | None:
        """Return the existing immutable snapshot row for attribution.

        The coordinator already received this snapshot through the durable
        quota ledger.  Returning its row identity lets an attribution record
        point to that source without embedding a second snapshot copy.
        """

        snapshot.validate()
        raw_payload = snapshot.raw_payload()
        with self.connection() as connection:
            if snapshot.ledger_id is not None:
                row = connection.execute(
                    """
                    SELECT id, snapshot_json FROM quota_snapshots
                    WHERE provider = 'claude' AND id = ?
                    """,
                    (snapshot.ledger_id,),
                ).fetchone()
                if row is None:
                    return None
                # Normalize legacy omitted defaults before comparing, while
                # still requiring the exact immutable ledger row.
                persisted = self._quota_snapshot_from_row(row)
                if persisted != snapshot:
                    return None
                return int(row["id"])
            row = connection.execute(
                """
                SELECT id FROM quota_snapshots
                WHERE provider = 'claude' AND snapshot_json = ?
                ORDER BY id DESC LIMIT 1
                """,
                (canonical_json(raw_payload),),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            # A bounded compatibility scan resolves a pre-feature row that
            # omitted optional defaults, without ever attaching a different
            # newer row to the selected effective observation.
            rows = connection.execute(
                """
                SELECT id, snapshot_json FROM quota_snapshots
                WHERE provider = 'claude' ORDER BY id DESC
                """
            ).fetchall()
            for candidate in rows:
                if self._quota_snapshot_from_row(candidate) == snapshot:
                    return int(candidate["id"])
            return None

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
            "deployment_admission_gate": self.delivery_admission_gate(),
        }

    def stale_tasks(self, max_age_seconds: int = 300) -> list[dict[str, str]]:
        cutoff = datetime.now(UTC).timestamp() - max_age_seconds
        active = {"planning", "ready", "queued", "running", "verifying", "needs_fix", "needs_approval"}
        return [
            {"task_id": task["task_id"], "state": task["state"], "updated_at": task["updated_at"]}
            for task in self.list_tasks()
            if task["state"] in active and datetime.fromisoformat(task["updated_at"]).timestamp() < cutoff
        ]
