"""Durable idempotency receipts for authenticated Authority MCP requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import threading
from typing import Any

from .model import canonical_hash, canonical_json, now_iso
from .store import CommandConflictError, StateConflictError, WorkbenchStore


# This is additive to the existing v13 schema. The Authority owner inserts it
# into ``WorkbenchStore.initialize()`` so production databases receive it
# through the established schema transaction.
AUTHORITY_REQUEST_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS authority_requests (
    request_id TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    tool TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    actor TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('executing', 'completed', 'unknown')),
    instance_id TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    CHECK(
        (state = 'completed' AND result_json IS NOT NULL AND settled_at IS NOT NULL)
        OR (state = 'executing' AND result_json IS NULL AND settled_at IS NULL)
        OR (state = 'unknown' AND result_json IS NULL AND settled_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS authority_requests_state_updated_idx
    ON authority_requests(state, updated_at);
"""


# These names deliberately mirror the current ``mcp.TOOLS`` catalog. The
# adapter never accepts a callable tool name from a client without this fence.
MCP_TOOL_NAMES = frozenset({
    "workbench_handoff_lockfile",
    "workbench_restore_accepted_source",
    "workbench_read_session_notifications",
    "workbench_ack_session_notification",
    "workbench_configure_node_recovery",
    "workbench_get_node_recovery",
    "workbench_amend_task_acceptance",
    "workbench_validate_blocked_node",
    "workbench_acceptance_report",
    "workbench_continue_session",
    "workbench_control_task",
    "workbench_create_delivery_objective",
    "workbench_decide_approval",
    "workbench_deliver_github",
    "workbench_get_delivery_objective",
    "workbench_get_request",
    "workbench_get_session",
    "workbench_harness_health",
    "workbench_inspect_task",
    "workbench_list_approvals",
    "workbench_list_tasks",
    "workbench_read_artifact",
    "workbench_read_events",
    "workbench_reclaim_worktrees",
    "workbench_request",
    "workbench_restore_worktree",
    "workbench_sync_github",
    "workbench_worktree_status",
})


READ_ONLY_TOOL_NAMES = frozenset({
    "workbench_read_session_notifications",
    "workbench_get_node_recovery",
    "workbench_acceptance_report",
    "workbench_get_delivery_objective",
    "workbench_get_request",
    "workbench_get_session",
    "workbench_harness_health",
    "workbench_inspect_task",
    "workbench_list_approvals",
    "workbench_list_tasks",
    "workbench_read_artifact",
    "workbench_read_events",
    "workbench_worktree_status",
})


REQUEST_ID_MAX_LENGTH = 200


def is_read_only_tool(name: object, arguments: object) -> bool:
    """Return whether one already-validated MCP operation needs no receipt."""

    if not isinstance(name, str) or not isinstance(arguments, dict):
        return False
    if name in READ_ONLY_TOOL_NAMES:
        return True
    if name == "workbench_handoff_lockfile":
        return arguments.get("op") in {"preview", "status"}
    if name == "workbench_restore_accepted_source":
        operation = arguments.get("op")
        return isinstance(operation, str) and operation in {"preview", "status"}
    return name in {"workbench_control_task", "workbench_validate_blocked_node", "workbench_amend_task_acceptance"} and arguments.get("dry_run") is True


class AuthorityService:
    """Journal mutating MCP requests without owning task or scheduler state."""

    def __init__(
        self,
        store: WorkbenchStore,
        invoke_callable: Callable[[str, dict[str, Any]], Any],
        instance_id: str,
    ):
        self.store = store
        self.invoke_callable = invoke_callable
        self.instance_id = self._required_string(instance_id, "instance_id")
        self._drain_condition = threading.Condition()
        self._draining = False
        self._mutations_in_flight = 0

    def dispatch(
        self,
        envelope: Mapping[str, Any],
        *,
        authenticated_actor: str = "authenticated",
    ) -> dict[str, Any]:
        """Invoke one allowlisted MCP tool with durable write idempotency.

        The caller supplies ``authenticated_actor`` from the already-authenticated
        Authority endpoint. ``actor`` is intentionally not an envelope field.
        """

        request = self._normalize_envelope(envelope)
        if is_read_only_tool(request["tool"], request["arguments"]):
            result = self.invoke_callable(request["tool"], request["arguments"])
            response: dict[str, Any] = {"state": "completed", "result": result}
            if request["request_id"] is not None:
                response["request_id"] = request["request_id"]
            return response

        self._begin_mutation()
        try:
            request_id = request["request_id"]
            if request_id is None:
                raise ValueError("mutating Authority requests require request_id")
            actor = self._required_string(authenticated_actor, "authenticated_actor")
            fingerprint = canonical_hash({
                "tool": request["tool"],
                "arguments": request["arguments"],
                "task_id": request["task_id"],
                "session_id": request["session_id"],
                "actor": actor,
            })
            if not self._reserve(request, fingerprint, actor):
                return self.get_request(request_id)

            try:
                result = self.invoke_callable(request["tool"], request["arguments"])
            except StateConflictError as error:
                # MCP presents a compare-and-set rejection as an ordinary tool
                # result. It is complete, never permission escalation or a retry.
                result = self._mcp_error_result(error)
            except Exception:
                return self._settle_unknown(request_id)

            try:
                result_json = canonical_json(result)
            except Exception:
                # The tool may already have changed durable state. Without an
                # exactly recorded result, the only safe receipt is unknown.
                return self._settle_unknown(request_id)
            return self._settle_completed(request_id, result_json)
        finally:
            self._finish_mutation()

    def begin_drain(self) -> None:
        """Reject future mutations while allowing already-admitted work to settle."""

        with self._drain_condition:
            self._draining = True

    def wait_for_idle(self) -> None:
        """Wait until every mutation admitted before draining has settled."""

        with self._drain_condition:
            while self._mutations_in_flight:
                self._drain_condition.wait()

    def _begin_mutation(self) -> None:
        with self._drain_condition:
            if self._draining:
                raise StateConflictError("Authority service is draining; mutation was not started")
            self._mutations_in_flight += 1

    def _finish_mutation(self) -> None:
        with self._drain_condition:
            self._mutations_in_flight -= 1
            if self._mutations_in_flight < 0:
                raise RuntimeError("Authority mutation accounting underflow")
            if self._mutations_in_flight == 0:
                self._drain_condition.notify_all()

    def get_request(self, request_id: str) -> dict[str, Any]:
        """Return the safe public state for one previously journaled request."""

        normalized_request_id = self._request_id(request_id)
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT request_id, state, result_json FROM authority_requests WHERE request_id = ?",
                (normalized_request_id,),
            ).fetchone()
        if row is None:
            raise KeyError(normalized_request_id)
        return self._public_receipt(row)

    def recover_interrupted(self) -> int:
        """Fence pre-startup executing receipts as unknown without replaying them.

        The Authority startup owner must hold its coordinator lease before
        calling this explicit recovery method. Construction never runs it.
        """

        timestamp = now_iso()
        recovered = 0
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT request_id, tool, task_id, session_id
                FROM authority_requests
                WHERE state = 'executing'
                ORDER BY created_at, request_id
                """
            ).fetchall()
            for row in rows:
                changed = connection.execute(
                    """
                    UPDATE authority_requests
                    SET state = 'unknown', updated_at = ?, settled_at = ?
                    WHERE request_id = ? AND state = 'executing'
                    """,
                    (timestamp, timestamp, row["request_id"]),
                ).rowcount
                if changed != 1:
                    raise StateConflictError("authority request recovery compare-and-set failed")
                WorkbenchStore._event(
                    connection,
                    "authority_request.unknown",
                    row["task_id"],
                    None,
                    self._event_payload(row, "unknown"),
                    created_at=timestamp,
                )
                recovered += 1
        return recovered

    def _reserve(
        self,
        request: dict[str, Any],
        fingerprint: str,
        actor: str,
    ) -> bool:
        """Write one executing receipt before invoking an effectful MCP tool."""

        timestamp = now_iso()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM authority_requests WHERE request_id = ?",
                (request["request_id"],),
            ).fetchone()
            if existing is not None:
                self._assert_matching_request(existing, request, fingerprint, actor)
                return False
            if request["task_id"] is not None:
                self.store.assert_no_active_validation(connection, request["task_id"])
            if request["tool"] == "workbench_validate_blocked_node":
                active = connection.execute(
                    "SELECT request_id FROM authority_requests WHERE task_id = ? AND state = 'executing' LIMIT 1",
                    (request["task_id"],),
                ).fetchone()
                if active is not None:
                    raise StateConflictError("another task mutation is in progress; validation was not started")
            connection.execute(
                """
                INSERT INTO authority_requests(
                    request_id, request_fingerprint, tool, task_id, session_id,
                    actor, state, instance_id, result_json, created_at, updated_at,
                    settled_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'executing', ?, NULL, ?, ?, NULL)
                """,
                (
                    request["request_id"],
                    fingerprint,
                    request["tool"],
                    request["task_id"],
                    request["session_id"],
                    actor,
                    self.instance_id,
                    timestamp,
                    timestamp,
                ),
            )
            WorkbenchStore._event(
                connection,
                "authority_request.executing",
                request["task_id"],
                None,
                self._event_payload(request, "executing"),
                created_at=timestamp,
            )
        return True

    def _settle_completed(self, request_id: str, result_json: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM authority_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            changed = connection.execute(
                """
                UPDATE authority_requests
                SET state = 'completed', result_json = ?, updated_at = ?, settled_at = ?
                WHERE request_id = ? AND state = 'executing' AND instance_id = ?
                """,
                (result_json, timestamp, timestamp, request_id, self.instance_id),
            ).rowcount
            if changed == 1:
                WorkbenchStore._event(
                    connection,
                    "authority_request.completed",
                    row["task_id"],
                    None,
                    self._event_payload(row, "completed"),
                    created_at=timestamp,
                )
                row = connection.execute(
                    "SELECT request_id, state, result_json FROM authority_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                assert row is not None
            return self._public_receipt(row)

    def _settle_unknown(self, request_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM authority_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            changed = connection.execute(
                """
                UPDATE authority_requests
                SET state = 'unknown', updated_at = ?, settled_at = ?
                WHERE request_id = ? AND state = 'executing' AND instance_id = ?
                """,
                (timestamp, timestamp, request_id, self.instance_id),
            ).rowcount
            if changed == 1:
                WorkbenchStore._event(
                    connection,
                    "authority_request.unknown",
                    row["task_id"],
                    None,
                    self._event_payload(row, "unknown"),
                    created_at=timestamp,
                )
                row = connection.execute(
                    "SELECT request_id, state, result_json FROM authority_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                assert row is not None
            return self._public_receipt(row)

    @staticmethod
    def _mcp_error_result(error: StateConflictError) -> dict[str, Any]:
        """Match the existing MCP representation for a CAS rejection."""

        return {
            "content": [{"type": "text", "text": str(error)}],
            "isError": True,
        }

    @staticmethod
    def _public_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
        state = str(row["state"])
        receipt: dict[str, Any] = {"request_id": str(row["request_id"]), "state": state}
        if state == "completed":
            result_json = row["result_json"]
            if result_json is None:
                raise RuntimeError("completed authority request has no result")
            receipt["result"] = json.loads(str(result_json))
        return receipt

    @staticmethod
    def _event_payload(row: Mapping[str, Any], state: str) -> dict[str, Any]:
        """Build an audit payload that never carries arguments or tool output."""

        return {
            "request_id": str(row["request_id"]),
            "tool": str(row["tool"]),
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "state": state,
        }

    @staticmethod
    def _assert_matching_request(
        row: Mapping[str, Any],
        request: Mapping[str, Any],
        fingerprint: str,
        actor: str,
    ) -> None:
        if (
            row["request_fingerprint"] != fingerprint
            or row["tool"] != request["tool"]
            or row["task_id"] != request["task_id"]
            or row["session_id"] != request["session_id"]
            or row["actor"] != actor
        ):
            raise CommandConflictError("authority request_id has a different immutable request")

    def _normalize_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(envelope, Mapping):
            raise ValueError("authority request envelope must be an object")
        allowed_keys = {"request_id", "tool", "arguments", "task_id", "session_id"}
        extra_keys = set(envelope) - allowed_keys
        if extra_keys:
            raise ValueError("authority request envelope has unsupported fields")
        tool = self._required_string(envelope.get("tool"), "tool")
        if tool not in MCP_TOOL_NAMES:
            raise ValueError("unknown Authority MCP tool")
        raw_arguments = envelope.get("arguments")
        if not isinstance(raw_arguments, dict):
            raise ValueError("arguments must be an object")
        try:
            arguments = json.loads(canonical_json(raw_arguments))
        except Exception as error:
            raise ValueError("arguments must be JSON serializable") from error

        request_id = (
            self._request_id(envelope["request_id"])
            if "request_id" in envelope and envelope["request_id"] is not None
            else None
        )
        envelope_task_id = self._optional_string(envelope.get("task_id"), "task_id")
        envelope_session_id = self._optional_string(envelope.get("session_id"), "session_id")
        argument_task_id = self._argument_binding(arguments, "task_id")
        argument_session_id = self._session_binding(arguments)
        if (
            envelope_task_id is not None
            and argument_task_id is not None
            and envelope_task_id != argument_task_id
        ):
            raise ValueError("task_id does not match the tool arguments")
        if (
            envelope_session_id is not None
            and argument_session_id is not None
            and envelope_session_id != argument_session_id
        ):
            raise ValueError("session_id does not match the tool arguments")
        request = {
            "request_id": request_id,
            "tool": tool,
            "arguments": dict(arguments),
            "task_id": envelope_task_id or argument_task_id,
            "session_id": envelope_session_id or argument_session_id,
        }
        self._freeze_session_task_binding(
            request,
            has_explicit_task=(envelope_task_id is not None or argument_task_id is not None),
        )
        if tool == "workbench_validate_blocked_node" and arguments.get("dry_run") is not True:
            if request_id is None or arguments.get("validation_id") != request_id or request["task_id"] is None:
                raise ValueError("validation run requires task_id and validation_id equal to request_id")
        if tool == "workbench_handoff_lockfile" and not is_read_only_tool(tool, arguments):
            operation = arguments.get("op")
            bound_id = arguments.get("request_id") if operation == "apply" else arguments.get("operation_id")
            if request_id is None or bound_id != request_id or request["task_id"] is None:
                raise ValueError("lockfile handoff mutation requires a matching journal identity and task_id")
        if tool == "workbench_restore_accepted_source" and not is_read_only_tool(tool, arguments):
            if request_id is None or arguments.get("request_id") != request_id or request["task_id"] is None:
                raise ValueError("historical source mutation requires a matching journal identity and task_id")
        return request

    def _freeze_session_task_binding(
        self,
        request: dict[str, Any],
        *,
        has_explicit_task: bool,
    ) -> None:
        """Bind session-bound requests to the active task visible at reservation."""

        session_id = request["session_id"]
        if session_id is None:
            return
        try:
            binding = self.store.get_session_binding(session_id)
        except KeyError as error:
            raise ValueError("session_id has no durable session binding") from error
        if request["tool"] in {"workbench_read_session_notifications", "workbench_ack_session_notification"}:
            if has_explicit_task:
                raise ValueError("session notification requests use their durable route, not an active task override")
            return
        active_task_id = binding.get("active_task_id")
        if active_task_id is not None:
            active_task_id = self._required_string(active_task_id, "session active_task_id")

        # A planning request uses a session's frozen context to create a new
        # reservation. Its optional task_id is that new identity, not the
        # existing active task, so comparing the two would reject valid work.
        if request["tool"] == "workbench_request":
            return
        if has_explicit_task:
            if request["task_id"] != active_task_id:
                raise ValueError("task_id does not match the session active task")
        elif active_task_id is not None:
            request["task_id"] = active_task_id
        if request["tool"] == "workbench_continue_session":
            self._freeze_continue_session_task(request, active_task_id)

    def _freeze_continue_session_task(
        self,
        request: dict[str, Any],
        active_task_id: str | None,
    ) -> None:
        """Pass the frozen active task to the store's in-transaction CAS check."""

        arguments = request["arguments"]
        if "expected_task_id" in arguments:
            expected_task_id = arguments["expected_task_id"]
            expected_task_id = self._required_string(
                expected_task_id,
                "arguments.expected_task_id",
            )
            if expected_task_id != active_task_id:
                raise ValueError("expected_task_id does not match the session active task")
        if active_task_id is not None:
            arguments["expected_task_id"] = active_task_id

    @classmethod
    def _session_binding(cls, arguments: dict[str, Any]) -> str | None:
        bindings = [
            cls._argument_binding(arguments, key)
            for key in ("source_thread_id", "session_id")
            if key in arguments
        ]
        if len(bindings) == 2 and bindings[0] != bindings[1]:
            raise ValueError("session bindings in the tool arguments do not match")
        return bindings[0] if bindings else None

    @classmethod
    def _argument_binding(cls, arguments: dict[str, Any], name: str) -> str | None:
        if name not in arguments:
            return None
        return cls._required_string(arguments[name], f"arguments.{name}")

    @staticmethod
    def _required_string(value: object, name: str) -> str:
        normalized = AuthorityService._optional_string(value, name)
        if normalized is None:
            raise ValueError(f"{name} is required")
        return normalized

    @staticmethod
    def _request_id(value: object) -> str:
        request_id = AuthorityService._required_string(value, "request_id")
        if len(request_id) > REQUEST_ID_MAX_LENGTH:
            raise ValueError(f"request_id must be at most {REQUEST_ID_MAX_LENGTH} characters")
        if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in request_id):
            raise ValueError("request_id must not contain control characters")
        return request_id

    @staticmethod
    def _optional_string(value: object, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value
