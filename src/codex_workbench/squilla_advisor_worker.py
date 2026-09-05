"""Headless runner for the upstream OpenSquilla V4 classifier.

This file intentionally imports no Codex Workbench control-plane code.  It is
started directly with the deployment-provided Python runtime, applies a
Python-level socket guard around upstream code, and talks only via one JSON
object on stdin/stdout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
import inspect
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Iterator


SCHEMA_VERSION = 1
SOURCE_LABEL = "v4_phase3"
VALID_TIERS = {"c0", "c1", "c2", "c3"}
VALID_ROUTE_CLASSES = {"R0", "R1", "R2", "R3"}
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
MAX_HINT_CHARS = 2_000


def main() -> int:
    started = time.monotonic()
    payload = _read_payload()
    if payload is None:
        _write({"schema_version": SCHEMA_VERSION, "results": [], "diagnostic": "worker_input_invalid"})
        return 2
    requests = payload.get("requests")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("operation") != "advise_batch" or not isinstance(requests, list):
        _write({"schema_version": SCHEMA_VERSION, "results": [], "diagnostic": "worker_input_invalid"})
        return 2
    if sys.version_info < (3, 12):
        _write(
            _unavailable_all(
                requests,
                "runtime_python_unsupported",
                started,
                source_identity=_unchecked_source_identity(payload.get("expected_source_revision")),
            )
        )
        return 0

    source_root = _path_value(payload.get("source_root"))
    bundle_dir = _path_value(payload.get("bundle_dir"))
    expected_source_revision = payload.get("expected_source_revision")
    diagnostic, bundle_metadata = _preflight(source_root, bundle_dir)
    if diagnostic is not None:
        _write(
            _unavailable_all(
                requests,
                diagnostic,
                started,
                bundle_metadata,
                _unchecked_source_identity(expected_source_revision),
            )
        )
        return 0

    source_diagnostic, source_identity = _verify_source_identity(
        source_root, expected_source_revision
    )
    if source_diagnostic is not None:
        _write(
            _unavailable_all(
                requests, source_diagnostic, started, bundle_metadata, source_identity
            )
        )
        return 0

    try:
        results = asyncio.run(_classify_batch(source_root, bundle_dir, requests))
    except ImportError:
        _write(
            _unavailable_all(
                requests, "upstream_dependency_missing", started, bundle_metadata, source_identity
            )
        )
        return 0
    except Exception:
        _write(
            _unavailable_all(
                requests, "strategy_load_failed", started, bundle_metadata, source_identity
            )
        )
        return 0

    worker_elapsed_ms = _elapsed_ms(started)
    for result in results:
        result["worker_elapsed_ms"] = worker_elapsed_ms
    _write(
        {
            "schema_version": SCHEMA_VERSION,
            "results": results,
            "bundle_metadata": bundle_metadata,
            "source_identity": source_identity,
            "worker_elapsed_ms": worker_elapsed_ms,
        }
    )
    return 0


async def _classify_batch(
    source_root: Path, bundle_dir: Path, requests: list[object]
) -> list[dict[str, object]]:
    with _network_disabled():
        sys.path.insert(0, str(source_root / "src"))
        try:
            from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

            strategy = V4Phase3Strategy(bundle_dir=bundle_dir, require_router_runtime=True)
            results: list[dict[str, object]] = []
            for item in requests:
                if not isinstance(item, Mapping):
                    results.append(_unavailable_item("invalid_request"))
                    continue
                results.append(await _classify_one(strategy, item))
            return results
        finally:
            source_entry = str(source_root / "src")
            if sys.path and sys.path[0] == source_entry:
                sys.path.pop(0)


async def _classify_one(strategy: Any, request: Mapping[str, object]) -> dict[str, object]:
    request_id = request.get("request_id")
    if not isinstance(request_id, str):
        return _unavailable_item("invalid_request")
    prompt = request.get("prompt")
    valid_tiers = request.get("valid_tiers")
    if not isinstance(prompt, str) or not isinstance(valid_tiers, list) or not _valid_tiers(valid_tiers):
        return _unavailable_item("invalid_request", request_id)

    try:
        state_flags = request.get("state_flags")
        flags_text_override = (
            json.dumps(state_flags, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if state_flags is not None
            else None
        )
        result = strategy.classify(
            prompt,
            valid_tiers,
            routing_history=request.get("routing_history") or [],
            prev_assistant_text=request.get("previous_public_summary"),
            prev_assistant_usage=request.get("previous_public_usage"),
            history_user_texts=request.get("history_user_texts") or [],
            flags_text_override=flags_text_override,
        )
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        return _unavailable_item("strategy_classify_failed", request_id)
    return _map_result(result, request_id, prompt, valid_tiers)


def _map_result(
    result: object, request_id: str, prompt: str, valid_tiers: list[object]
) -> dict[str, object]:
    if not isinstance(result, tuple) or len(result) != 4:
        return _unavailable_item("invalid_strategy_result", request_id)
    tier, confidence, source, extra = result
    if source == "v4_unavailable":
        return _unavailable_item("v4_unavailable", request_id)
    if (
        not isinstance(tier, str)
        or tier not in VALID_TIERS
        or tier not in valid_tiers
        or not _valid_confidence(confidence)
        or source != SOURCE_LABEL
        or not isinstance(extra, Mapping)
    ):
        return _unavailable_item("invalid_strategy_result", request_id)

    return {
        "request_id": request_id,
        "status": "available",
        "demand_tier": tier,
        "confidence": float(confidence),
        "source": SOURCE_LABEL,
        "extra": {
            "route_class": _route_class(extra.get("route_class")),
            "thinking_mode": _safe_hint(extra.get("thinking_mode"), prompt),
            "prompt_policy": _safe_hint(extra.get("prompt_policy"), prompt),
            "prompt_hint": _safe_hint(extra.get("prompt_hint"), prompt),
        },
        "worker_elapsed_ms": 0,
    }


def _preflight(source_root: Path | None, bundle_dir: Path | None) -> tuple[str | None, dict[str, object]]:
    metadata = _bundle_metadata(bundle_dir)
    if source_root is None or not source_root.is_absolute() or not source_root.is_dir():
        return "source_root_unavailable", metadata
    if bundle_dir is None or not bundle_dir.is_absolute() or not bundle_dir.is_dir():
        return "bundle_unavailable", metadata
    strategy_path = source_root / "src" / "opensquilla" / "squilla_router" / "v4_phase3.py"
    if not strategy_path.is_file():
        return "strategy_source_missing", metadata
    if _is_lfs_pointer(strategy_path):
        return "strategy_source_lfs_pointer", metadata
    if not (bundle_dir / "runtime_src").is_dir():
        return "bundle_runtime_src_missing", metadata
    if not (bundle_dir / "router.runtime.yaml").is_file():
        return "bundle_runtime_config_missing", metadata

    manifest = bundle_dir / "artifact_manifest.json"
    if not manifest.is_file():
        return "bundle_manifest_missing", metadata
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        files = value.get("files", [])
        if value.get("schema_version") != 1 or not isinstance(files, list) or not files:
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        return "bundle_manifest_invalid", metadata
    for entry in files:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            return "bundle_manifest_invalid", metadata
        relative = Path(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            return "bundle_manifest_invalid", metadata
        asset = bundle_dir / relative
        if not asset.is_file():
            return "bundle_asset_missing", metadata
        if _is_lfs_pointer(asset):
            return "bundle_asset_lfs_pointer", metadata
    return None, metadata


def _bundle_metadata(bundle_dir: Path | None) -> dict[str, object]:
    metadata: dict[str, object] = {
        "bundle_version": "unknown",
        "source_model_version": "unknown",
        "feature_dim": None,
        "feature_schema_version": None,
    }
    if bundle_dir is None:
        return metadata
    for filename in ("version.json", "inference_manifest.json"):
        path = bundle_dir / filename
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping):
            continue
        if filename == "version.json" and isinstance(value.get("version"), str):
            metadata["source_model_version"] = value["version"]
        if filename == "inference_manifest.json":
            for key in ("bundle_version", "source_model_version", "feature_dim"):
                if key in value:
                    metadata[key] = value[key]
            feature_meta = value.get("feature_meta")
            if isinstance(feature_meta, Mapping) and "schema_version" in feature_meta:
                metadata["feature_schema_version"] = feature_meta["schema_version"]
    return metadata


def _verify_source_identity(
    source_root: Path | None, expected_source_revision: object
) -> tuple[str | None, dict[str, object]]:
    """Compare the adapter's fixed expected pin with one local Git HEAD read."""

    identity = _unchecked_source_identity(expected_source_revision)
    if source_root is None or not isinstance(expected_source_revision, str):
        return "source_identity_unverified", identity
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return "source_identity_unverified", identity
    observed = completed.stdout.strip().lower()
    if completed.returncode != 0 or not _is_commit_id(observed):
        return "source_identity_unverified", identity
    identity["observed_source_revision"] = observed
    identity["verification_method"] = "git_rev_parse_head"
    if observed != expected_source_revision:
        return "source_revision_mismatch", identity
    return None, identity


def _unchecked_source_identity(expected_source_revision: object) -> dict[str, object]:
    return {
        "expected_source_revision": expected_source_revision
        if isinstance(expected_source_revision, str)
        else None,
        "observed_source_revision": None,
        "verification_method": "not_performed",
    }


@contextmanager
def _network_disabled() -> Iterator[None]:
    """Block Python socket APIs; this is not an OS-level network sandbox."""

    original_socket = socket.socket
    original_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("network_disabled")

    socket.socket = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]
    socket.getaddrinfo = blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = original_socket
        socket.create_connection = original_connection
        socket.getaddrinfo = original_getaddrinfo


def _unavailable_all(
    requests: list[object],
    diagnostic: str,
    started: float,
    bundle_metadata: Mapping[str, object] | None = None,
    source_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    worker_elapsed_ms = _elapsed_ms(started)
    results = [
        _unavailable_item(
            diagnostic,
            item.get("request_id") if isinstance(item, Mapping) else None,
        )
        for item in requests
    ]
    for item in results:
        item["worker_elapsed_ms"] = worker_elapsed_ms
    return {
        "schema_version": SCHEMA_VERSION,
        "results": results,
        "bundle_metadata": dict(bundle_metadata or {}),
        "source_identity": dict(source_identity or {}),
        "worker_elapsed_ms": worker_elapsed_ms,
    }


def _unavailable_item(diagnostic: str, request_id: object | None = None) -> dict[str, object]:
    return {
        "request_id": request_id if isinstance(request_id, str) else "invalid-request",
        "status": "unavailable",
        "diagnostic": diagnostic,
        "worker_elapsed_ms": 0,
    }


def _read_payload() -> Mapping[str, object] | None:
    try:
        value = json.loads(sys.stdin.read())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _write(value: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    sys.stdout.write("\n")


def _path_value(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) else None


def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def _is_commit_id(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _valid_tiers(values: list[object]) -> bool:
    return bool(values) and all(isinstance(value, str) and value in VALID_TIERS for value in values)


def _valid_confidence(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _route_class(value: object) -> str | None:
    return value if isinstance(value, str) and value in VALID_ROUTE_CLASSES else None


def _safe_hint(value: object, prompt: str) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_HINT_CHARS:
        return None
    return None if prompt and prompt in value else value


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
