"""Read and acknowledge session-routed events through the existing Authority."""
from __future__ import annotations

from .session_notifications import ack_session_notification, list_session_notifications


SESSION_NOTIFICATION_TOOLS = [
    {
        "name": "workbench_read_session_notifications",
        "description": "Read pending durable notifications routed to one originating session. Reading does not acknowledge delivery or wake a model.",
        "inputSchema": {
            "type": "object", "additionalProperties": False, "required": ["source_thread_id"],
            "properties": {
                "source_thread_id": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "after_cursor": {"type": "integer", "minimum": 0},
            },
        },
    },
    {
        "name": "workbench_ack_session_notification",
        "description": "Acknowledge one notification for its exact originating session using the Authority request journal. Does not assert that a host displayed it.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["source_thread_id", "notification_id"],
            "properties": {
                "source_thread_id": {"type": "string", "minLength": 1},
                "notification_id": {"type": "string", "minLength": 1},
            },
        },
    },
]


def session_notification_tool(store, name: str, arguments: dict) -> dict:
    """Dispatch only fixed read/ack operations; callers supply Authority authentication."""
    if name == "workbench_read_session_notifications":
        if "source_thread_id" not in arguments or set(arguments) - {"source_thread_id", "limit", "after_cursor"}:
            raise ValueError("invalid session notification read fields")
        return list_session_notifications(store, **arguments)
    if name == "workbench_ack_session_notification":
        if set(arguments) != {"source_thread_id", "notification_id"}:
            raise ValueError("invalid session notification acknowledgement fields")
        return ack_session_notification(store, **arguments)
    raise ValueError("unsupported session notification operation")
