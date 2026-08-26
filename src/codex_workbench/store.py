from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterator

from .model import (
    NodeResult,
    NodeSpec,
    QuotaSnapshot,
    TaskContract,
    canonical_hash,
    canonical_json,
    now_iso,
)
from .worktrees import normalize_scope, scope_access_conflicts, scope_allows, scopes_overlap


SCHEMA_VERSION = 4


class CommandConflictError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
    pass


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
                CREATE INDEX IF NOT EXISTS nodes_state_idx ON nodes(state, updated_at);
                CREATE INDEX IF NOT EXISTS events_task_cursor_idx ON events(task_id, cursor);
                CREATE INDEX IF NOT EXISTS tasks_state_updated_idx ON tasks(state, updated_at);
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
            elif int(current["value"]) in {1, 2, 3}:
                node_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
                }
                if "effective_executor" not in node_columns:
                    connection.execute("ALTER TABLE nodes ADD COLUMN effective_executor TEXT")
                if "effective_model" not in node_columns:
                    connection.execute("ALTER TABLE nodes ADD COLUMN effective_model TEXT")
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif int(current["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported schema version {current['value']}; expected {SCHEMA_VERSION}"
                )
        self.path.chmod(0o600)

    def cached_evidence(self, cache_key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            return {
                "cache_key": row["cache_key"],
                "result": json.loads(row["result_json"]),
                "source_task_id": row["source_task_id"],
                "source_node_id": row["source_node_id"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "use_count": row["use_count"],
            }

    def save_evidence(
        self,
        cache_key: str,
        result: NodeResult,
        task_id: str,
        node_id: str,
    ) -> None:
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
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events(event_type, task_id, node_id, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (event_type, task_id, node_id, canonical_json(payload), now_iso()),
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
                                     effective_executor = NULL, effective_model = NULL,
                                     started_at = NULL, settled_at = NULL, updated_at = ?
                    WHERE task_id = ? AND state = 'failed'
                    """,
                    (now_iso(), task_id),
                )
        return self.transition_task(task_id, "queued", expected_revision=task["state_revision"])

    def resolve_indeterminate(
        self,
        task_id: str,
        node_id: str,
        resolution: str,
        *,
        expected_revision: int,
    ) -> int:
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

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, contract_hash, state, state_revision, created_at, updated_at,
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
        return {
            "task_id": row["task_id"],
            "state": row["state"],
            "state_revision": row["state_revision"],
            "contract_hash": row["contract_hash"],
            "contract": json.loads(row["contract_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "blocker": row["blocker"],
            "verdict": row["verdict"],
            "nodes": [
                {
                    **json.loads(node["spec_json"]),
                    "state": node["state"],
                    "attempt": node["attempt"],
                    "worker_id": node["worker_id"],
                    "worktree": node["worktree"],
                    "effective_executor": node["effective_executor"],
                    "effective_model": node["effective_model"],
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

    def record_system_event(self, event_type: str, payload: dict[str, Any]) -> int:
        with self.transaction() as connection:
            return self._event(connection, event_type, None, None, payload)

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
    ) -> int:
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE nodes SET effective_executor = ?, effective_model = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'running'
                """,
                (executor, model, now_iso(), task_id, node_id),
            ).rowcount
            if changed != 1:
                raise StateConflictError(f"node {node_id} is not running")
            return self._event(connection, "node.routed", task_id, node_id, payload)

    def authority_status(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT event_type, payload_json, created_at FROM events
                WHERE event_type IN ('coordinator.started', 'coordinator.stopped')
                ORDER BY cursor DESC LIMIT 1
                """
            ).fetchone()
            if row is None or row["event_type"] != "coordinator.started":
                return None
            return {**json.loads(row["payload_json"]), "active": True, "observed_at": row["created_at"]}

    def claim_ready_node(
        self,
        worker_id: str,
        admissible: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        timestamp = now_iso()
        with self.transaction() as connection:
            candidates = connection.execute(
                """
                SELECT n.*, t.contract_json, t.state AS task_state
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.state = 'pending' AND t.state IN ('queued', 'running', 'verifying', 'needs_fix')
                ORDER BY t.created_at, json_extract(n.spec_json, '$.ordinal'), n.node_id
                """
            ).fetchall()
            running_accesses: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
            for running in connection.execute(
                "SELECT spec_json FROM nodes WHERE state = 'running'"
            ).fetchall():
                running_spec = json.loads(running["spec_json"])
                running_accesses.append(
                    (
                        tuple(running_spec.get("read_scopes", [])),
                        tuple(running_spec.get("write_scopes", [])),
                    )
                )

            selected: sqlite3.Row | None = None
            selected_spec: dict[str, Any] | None = None
            for candidate in candidates:
                spec = json.loads(candidate["spec_json"])
                dependencies = spec.get("depends_on", [])
                if dependencies:
                    placeholders = ",".join("?" for _ in dependencies)
                    states = connection.execute(
                        f"SELECT node_id, state FROM nodes WHERE task_id = ? AND node_id IN ({placeholders})",
                        (candidate["task_id"], *dependencies),
                    ).fetchall()
                    if len(states) != len(dependencies) or any(row["state"] != "accepted" for row in states):
                        continue
                read_scopes = tuple(spec.get("read_scopes", []))
                write_scopes = tuple(spec.get("write_scopes", []))
                if any(
                    scope_access_conflicts(read_scopes, write_scopes, running_reads, running_writes)
                    for running_reads, running_writes in running_accesses
                ):
                    continue
                if admissible is not None and not admissible(spec):
                    continue
                selected = candidate
                selected_spec = spec
                break

            if selected is None or selected_spec is None:
                return None

            attempt = int(selected["attempt"]) + 1
            connection.execute(
                """
                UPDATE nodes SET state = 'running', attempt = ?, worker_id = ?,
                                 effective_executor = ?, effective_model = ?,
                                 started_at = ?, updated_at = ?
                WHERE task_id = ? AND node_id = ? AND state = 'pending'
                """,
                (
                    attempt,
                    worker_id,
                    selected_spec["executor"],
                    selected_spec["model"],
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
                {"attempt": attempt, "worker_id": worker_id, "executor": selected_spec["executor"]},
            )
            return {
                "task_id": selected["task_id"],
                "node_id": selected["node_id"],
                "attempt": attempt,
                "spec": selected_spec,
                "contract": json.loads(selected["contract_json"]),
            }

    def assign_worktree(self, task_id: str, node_id: str, worktree: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET worktree = ?, updated_at = ? WHERE task_id = ? AND node_id = ?",
                (worktree, now_iso(), task_id, node_id),
            )

    def settle_node(self, task_id: str, node_id: str, result: NodeResult) -> None:
        timestamp = now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT n.state, n.attempt, n.spec_json, t.contract_json
                FROM nodes n JOIN tasks t USING(task_id)
                WHERE n.task_id = ? AND n.node_id = ?
                """,
                (task_id, node_id),
            ).fetchone()
            if row is None:
                raise KeyError((task_id, node_id))
            if row["state"] != "running":
                raise StateConflictError(f"node {node_id} is {row['state']}, expected running")
            spec = json.loads(row["spec_json"])
            contract = json.loads(row["contract_json"])
            if result.status == "succeeded" and spec.get("verifier"):
                missing = self._missing_required_artifacts(connection, task_id, contract, result)
                if missing:
                    result = NodeResult(
                        status="failed",
                        summary=f"required acceptance Evidence is missing: {', '.join(missing)}",
                        artifacts=result.artifacts,
                        actual_model=result.actual_model,
                        exit_code=result.exit_code,
                        retryable=False,
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
                UPDATE nodes SET state = ?, settled_at = ?, updated_at = ?, result_json = ?
                WHERE task_id = ? AND node_id = ?
                """,
                (node_state, timestamp, timestamp, canonical_json(result.to_dict()), task_id, node_id),
            )
            self._event(
                connection,
                f"node.{node_state}",
                task_id,
                node_id,
                {"attempt": row["attempt"], "result": result.to_dict()},
            )

            task = connection.execute(
                "SELECT state, state_revision FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert task is not None
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
                if result.retryable and int(row["attempt"]) <= int(contract.get("retry_limit", 0)):
                    connection.execute(
                        """
                        UPDATE nodes SET state = 'pending', worker_id = NULL,
                                         effective_executor = NULL, effective_model = NULL,
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
                revision = int(task["state_revision"]) + 1
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
                "SELECT task_id, node_id, attempt FROM nodes WHERE state = 'running'"
            ).fetchall()
            for row in rows:
                result = NodeResult(
                    status="indeterminate",
                    summary="coordinator restarted while the worker was running; explicit resolution required",
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
                self._event(
                    connection,
                    "node.indeterminate",
                    row["task_id"],
                    row["node_id"],
                    {"attempt": row["attempt"], "reason": "coordinator_restart"},
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
        return {
            "ok": True,
            "schema_version": schema,
            "cursor": cursor,
            "task_counts": counts,
            "active_executors": active_executors,
            "active_models": active_models,
            "authority": self.authority_status(),
        }

    def stale_tasks(self, max_age_seconds: int = 300) -> list[dict[str, str]]:
        cutoff = datetime.now(UTC).timestamp() - max_age_seconds
        active = {"planning", "ready", "queued", "running", "verifying", "needs_fix", "needs_approval"}
        return [
            {"task_id": task["task_id"], "state": task["state"], "updated_at": task["updated_at"]}
            for task in self.list_tasks()
            if task["state"] in active and datetime.fromisoformat(task["updated_at"]).timestamp() < cutoff
        ]
