"""Authenticated loopback access shared by MCP and the SSH control fallback."""
from __future__ import annotations

from http.client import HTTPException
import json
import math
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .authority_service import is_read_only_tool


class ServiceTransportError(RuntimeError):
    """The configured Authority HTTP endpoint did not return a valid receipt."""


class ServiceHTTPError(ServiceTransportError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Authority service returned HTTP {status}")


class IndeterminateServiceRequest(ServiceTransportError):
    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(
            f"Authority request {request_id} has no confirmed completion receipt; "
            "query the same request ID, do not resubmit the mutation"
        )


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AuthorityHTTPClient:
    """Use an existing control token without forwarding it to a proxy or redirect.

    Read retries are bounded. A mutation is sent once; uncertain transport
    completion is resolved only through its durable request ID.
    """

    def __init__(
        self, base_url: str, token: str, *, timeout_seconds: float = 30,
        read_attempts: int = 3, backoff_seconds: float = 0.2,
    ):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Authority service requires an explicit loopback HTTP endpoint")
        if not isinstance(token, str) or not token or any(ch in token for ch in "\r\n"):
            raise ValueError("an existing control token is required")
        if type(read_attempts) is not int or not 1 <= read_attempts <= 5:
            raise ValueError("read_attempts must be between 1 and 5")
        if (not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300
                or not math.isfinite(backoff_seconds) or not 0 <= backoff_seconds <= 5):
            raise ValueError("invalid bounded service timeout or backoff")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_seconds = timeout_seconds
        self.read_attempts = read_attempts
        self.backoff_seconds = backoff_seconds
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 *, retry_read: bool = False) -> dict[str, Any]:
        payload = json.dumps(body).encode() if body is not None else None
        attempts = self.read_attempts if retry_read else 1
        for attempt in range(attempts):
            request = Request(self.base_url + path, data=payload, method=method,
                              headers={"Authorization": f"Bearer {self._token}",
                                       "Content-Type": "application/json"})
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    value = json.load(response)
                if not isinstance(value, dict):
                    raise ServiceTransportError("Authority returned a non-object receipt")
                return value
            except HTTPError as error:
                failure = ServiceHTTPError(error.code)
                error.close()
                if error.code not in {502, 503, 504}:
                    raise failure from None
            except (OSError, URLError, HTTPException, UnicodeError, json.JSONDecodeError):
                failure = ServiceTransportError("Authority HTTP connection or receipt failed")
            if attempt + 1 == attempts:
                raise failure from None
            time.sleep(self.backoff_seconds * (2 ** attempt))
        raise AssertionError("bounded request loop did not return")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/service/status", retry_read=True)

    def tools(self) -> dict[str, Any]:
        return self._request("GET", "/api/service/tools", retry_read=True)

    def get_request(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError("request_id must be non-empty and at most 200 characters")
        return self._request("GET", "/api/service/requests/" + quote(request_id, safe=""), retry_read=True)

    def dispatch(self, envelope: dict[str, Any], *, read_only: bool = False) -> dict[str, Any]:
        read_only = read_only is True and is_read_only_tool(
            envelope.get("tool"), envelope.get("arguments", {})
        )
        request_id = envelope.get("request_id")
        if not read_only and (not isinstance(request_id, str) or not request_id):
            raise ValueError("mutation requires a stable request_id")
        try:
            receipt = self._request("POST", "/api/service/requests", envelope, retry_read=read_only)
        except ServiceHTTPError as error:
            if read_only or (error.status < 500 and error.status != 408):
                raise
            receipt = self._resolve_uncertain_write(str(request_id))
        except ServiceTransportError:
            if read_only:
                raise
            receipt = self._resolve_uncertain_write(str(request_id))
        if not read_only and (
            receipt.get("state") != "completed" or receipt.get("request_id") != request_id
        ):
            raise IndeterminateServiceRequest(str(request_id))
        return receipt

    def _resolve_uncertain_write(self, request_id: str) -> dict[str, Any]:
        try:
            return self.get_request(request_id)
        except ServiceTransportError:
            raise IndeterminateServiceRequest(request_id) from None
