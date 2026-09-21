"""MCP is a protocol adapter; the running Authority service owns operations."""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .authority_service import is_read_only_tool
from .service_client import AuthorityHTTPClient, IndeterminateServiceRequest, ServiceTransportError


def _text(value: Any, *, error: bool = False) -> dict[str, Any]:
    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
    if error:
        result["isError"] = True
    return result


class AuthorityMCPAdapter:
    """Translate one host request without opening or mutating the task database."""

    def __init__(self, client: AuthorityHTTPClient):
        self.client = client
        self._catalog: dict[str, dict[str, Any]] | None = None

    def _tools(self) -> list[dict[str, Any]]:
        tools = self.client.tools().get("tools")
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in tools
        ):
            raise ServiceTransportError("Authority returned an invalid tool catalog")
        self._catalog = {tool["name"]: tool for tool in tools}
        return tools

    @staticmethod
    def _rejected_result(receipt: dict[str, Any]) -> dict[str, Any] | None:
        """Return one confirmed no-effect rejection as an MCP error result."""

        if receipt.get("state") != "rejected":
            return None
        rejection = receipt.get("rejection")
        invalid_fields = rejection.get("invalid_fields") if isinstance(rejection, dict) else None
        allowed_fields = rejection.get("allowed_fields") if isinstance(rejection, dict) else None
        if (
            not isinstance(receipt.get("request_id"), str)
            or receipt.get("effects") != "none"
            or receipt.get("enqueued") is not False
            or not isinstance(rejection, dict)
            or not isinstance(invalid_fields, list)
            or any(not isinstance(field, str) for field in invalid_fields)
            or not isinstance(allowed_fields, list)
            or any(not isinstance(field, str) for field in allowed_fields)
        ):
            raise ServiceTransportError("Authority returned an invalid rejected request receipt")
        return _text(receipt, error=True)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        if request_id is None:
            return None
        method = message.get("method")
        try:
            if method == "initialize":
                status = self.client.status()
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "codex-workbench", "version": status["version"]},
                }
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "ping":
                self.client.status()
                result = {}
            elif method == "tools/call":
                params = message.get("params")
                if not isinstance(params, dict):
                    raise ValueError("tools/call requires object params")
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ValueError("tool name and arguments are invalid")
                if self._catalog is None:
                    self._tools()
                assert self._catalog is not None
                tool = self._catalog.get(name)
                if tool is None:
                    raise ValueError("tool is not in the Authority catalog; refresh tools/list")
                if name == "workbench_get_service_request":
                    receipt = self.client.get_request(arguments.get("request_id"))
                    result = self._rejected_result(receipt) or _text(receipt)
                else:
                    copied = dict(arguments)
                    read_only = (
                        tool.get("annotations", {}).get("readOnlyHint") is True
                        or is_read_only_tool(name, copied)
                    )
                    if name in {
                        "workbench_handoff_lockfile",
                        "workbench_restore_accepted_source",
                        "workbench_repair_blocked_source",
                    }:
                        read_only = is_read_only_tool(name, copied)
                        service_request_id = copied.get(
                            "operation_id"
                            if name == "workbench_handoff_lockfile"
                            and copied.get("op") in {"cancel", "reconcile"}
                            else "request_id"
                        )
                    else:
                        service_request_id = copied.pop("request_id", None)
                    if not read_only and (not isinstance(service_request_id, str) or not service_request_id):
                        raise ValueError("mutation requires a stable request_id; refresh tools/list if absent")
                    envelope = {"tool": name, "arguments": copied}
                    if service_request_id is not None:
                        envelope["request_id"] = service_request_id
                    if "task_id" in copied:
                        envelope["task_id"] = copied["task_id"]
                    if "source_thread_id" in copied:
                        envelope["session_id"] = copied["source_thread_id"]
                    receipt = self.client.dispatch(envelope, read_only=read_only)
                    rejected = self._rejected_result(receipt)
                    if rejected is not None:
                        result = rejected
                    elif receipt.get("state") != "completed" or not isinstance(receipt.get("result"), dict):
                        raise ServiceTransportError("Authority did not return a completed request receipt")
                    else:
                        result = receipt["result"]
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": "unsupported MCP method"}}
        except IndeterminateServiceRequest as error:
            result = _text({"state": "indeterminate", "request_id": error.request_id,
                            "component": "authority_http", "retry_mutation": False,
                            "next_action": "query the same request_id; do not resend the mutation"}, error=True)
        except (ServiceTransportError, OSError, ValueError, KeyError) as error:
            if method != "tools/call":
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32000, "message": "Authority service unavailable or invalid response"}}
            result = _text({"component": "authority_http", "error": str(error)}, error=True)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve_authority_stdio(
    client: AuthorityHTTPClient, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout,
) -> None:
    adapter = AuthorityMCPAdapter(client)
    for line in input_stream:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("MCP request must be an object")
            response = adapter.handle(request)
        except (ValueError, json.JSONDecodeError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "invalid MCP JSON request"}}
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()
