"""Local, advisory-only boundary for OpenSquilla V4 Phase 3 classification.

The module deliberately owns no provider selection, quota decision, permission,
task dispatch, or durable state.  It starts a short-lived local Python worker
once per ``advise_batch`` call, so a DAG batch shares one loaded upstream
``V4Phase3Strategy`` instance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any


ADVISOR_SCHEMA_VERSION = 1
UPSTREAM_REVISION = "94ac35eb99a564e15fa651abf8300c89f21efa0f"
UPSTREAM_SOURCE_LABEL = "v4_phase3"
VALID_DEMAND_TIERS = ("c0", "c1", "c2", "c3")
DEFAULT_VALID_TIERS = VALID_DEMAND_TIERS
CLASSIFICATION_SEMANTICS = "demand_classification_not_task_success_or_model_success_ranking"
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
_MAX_PROMPT_CHARS = 16_000
_MAX_HISTORY_TURNS = 8
_MAX_HISTORY_TEXT_CHARS = 4_000
_MAX_PUBLIC_SUMMARY_CHARS = 8_000
_MAX_HINT_CHARS = 2_000
_ROUTING_HISTORY_KEYS = (
    "route_class",
    "final_route_class",
    "difficulty",
    "difficulty_score",
    "margin",
)


@dataclass(frozen=True)
class SquillaAdvisorRequest:
    """Only public, bounded context that may cross into the local worker."""

    request_id: str
    prompt: str
    valid_tiers: Sequence[str] = DEFAULT_VALID_TIERS
    history_user_texts: Sequence[str] = ()
    routing_history: Sequence[Mapping[str, object]] = ()
    previous_public_summary: str | None = None
    previous_public_usage: Mapping[str, object] | None = None
    state_flags: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SquillaAdvice:
    """A non-authoritative demand-classification suggestion.

    ``to_receipt`` intentionally omits every prompt, history entry, public
    summary, usage payload, and state flag supplied to the advisor.
    """

    request_id: str
    status: str
    demand_tier: str | None
    confidence: float | None
    source: dict[str, object]
    runtime: dict[str, object]
    diagnostic: str | None = None
    route_class: str | None = None
    thinking_hint: str | None = None
    prompt_hint: str | None = None
    prompt_policy: str | None = None
    classification_semantics: str = CLASSIFICATION_SEMANTICS

    def to_receipt(self) -> dict[str, object]:
        """Return stable, prompt-free data for the consuming control plane."""

        return {
            "schema_version": ADVISOR_SCHEMA_VERSION,
            "request_id": self.request_id,
            "status": self.status,
            "demand_tier": self.demand_tier,
            "confidence": self.confidence,
            "classification_semantics": self.classification_semantics,
            "route_class": self.route_class,
            "thinking_hint": self.thinking_hint,
            "prompt_hint": self.prompt_hint,
            "prompt_policy": self.prompt_policy,
            "source": self.source,
            "runtime": self.runtime,
            "diagnostic": self.diagnostic,
        }


class SquillaAdvisor:
    """Run the fixed upstream classifier in a Python-socket-restricted subprocess."""

    def __init__(
        self,
        *,
        runtime_python: str | Path,
        source_root: str | Path,
        bundle_dir: str | Path | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.runtime_python = Path(runtime_python)
        self.source_root = Path(source_root)
        self.bundle_dir = (
            Path(bundle_dir)
            if bundle_dir is not None
            else self.source_root
            / "src"
            / "opensquilla"
            / "squilla_router"
            / "models"
            / "v4.2_phase3_inference"
        )
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def source_metadata() -> dict[str, object]:
        """Load immutable provenance, taxonomy, and non-claim metadata."""

        metadata_file = resources.files("codex_workbench.data").joinpath(
            "opensquilla-router-source.json"
        )
        return json.loads(metadata_file.read_text(encoding="utf-8"))

    def advise(self, request: SquillaAdvisorRequest) -> SquillaAdvice:
        """Return one advisory result while retaining batch semantics internally."""

        return self.advise_batch([request])[0]

    def advise_batch(self, requests: Sequence[SquillaAdvisorRequest]) -> list[SquillaAdvice]:
        """Classify a batch through one local worker and one upstream instance."""

        request_list = list(requests)
        if not request_list:
            return []

        started = time.monotonic()
        answers: list[SquillaAdvice | None] = [None] * len(request_list)
        prepared: list[tuple[int, dict[str, object]]] = []
        request_ids: set[str] = set()

        for index, request in enumerate(request_list):
            try:
                wire = _request_to_wire(request)
                request_id = str(wire["request_id"])
                if request_id in request_ids:
                    raise ValueError("duplicate_request_id")
                request_ids.add(request_id)
                prepared.append((index, wire))
            except (TypeError, ValueError) as exc:
                answers[index] = self._unavailable(
                    _request_id_for_receipt(request),
                    _safe_diagnostic(exc),
                    elapsed_ms=_elapsed_ms(started),
                    asset_status="not_checked",
                )

        if not prepared:
            return [_required_answer(answer) for answer in answers]

        preflight = _preflight(self.runtime_python, self.source_root, self.bundle_dir)
        if preflight["diagnostic"] is not None:
            for index, wire in prepared:
                answers[index] = self._unavailable(
                    str(wire["request_id"]),
                    str(preflight["diagnostic"]),
                    elapsed_ms=_elapsed_ms(started),
                    asset_status=str(preflight["asset_status"]),
                    bundle_metadata=preflight["bundle_metadata"],
                )
            return [_required_answer(answer) for answer in answers]

        payload = {
            "schema_version": ADVISOR_SCHEMA_VERSION,
            "operation": "advise_batch",
            "source_root": str(self.source_root),
            "bundle_dir": str(self.bundle_dir),
            # Internal worker protocol only; the public constructor has no revision override.
            "expected_source_revision": UPSTREAM_REVISION,
            "requests": [wire for _, wire in prepared],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            completed = subprocess.run(
                [
                    str(self.runtime_python),
                    "-I",
                    "-u",
                    str(Path(__file__).with_name("squilla_advisor_worker.py")),
                ],
                input=encoded,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
                env=_worker_environment(),
            )
        except subprocess.TimeoutExpired:
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_timeout",
                started,
                preflight["bundle_metadata"],
            )
        except OSError:
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_start_failed",
                started,
                preflight["bundle_metadata"],
            )

        if completed.returncode != 0:
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_exit_nonzero",
                started,
                preflight["bundle_metadata"],
            )

        try:
            worker_payload = json.loads(completed.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_protocol_invalid",
                started,
                preflight["bundle_metadata"],
            )

        if not isinstance(worker_payload, Mapping):
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_protocol_invalid",
                started,
                preflight["bundle_metadata"],
            )

        worker_results = worker_payload.get("results")
        if (
            worker_payload.get("schema_version") != ADVISOR_SCHEMA_VERSION
            or not isinstance(worker_results, list)
        ):
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_protocol_invalid",
                started,
                preflight["bundle_metadata"],
            )

        source_identity = _source_identity_from_worker(worker_payload.get("source_identity"))
        if source_identity is None:
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_protocol_invalid",
                started,
                preflight["bundle_metadata"],
            )

        by_request_id: dict[str, Mapping[str, object]] = {}
        for item in worker_results:
            if not isinstance(item, Mapping) or not isinstance(item.get("request_id"), str):
                return self._fill_worker_failure(
                    answers,
                    prepared,
                    "worker_protocol_invalid",
                    started,
                    preflight["bundle_metadata"],
                )
            request_id = str(item["request_id"])
            if request_id in by_request_id:
                return self._fill_worker_failure(
                    answers,
                    prepared,
                    "worker_protocol_invalid",
                    started,
                    preflight["bundle_metadata"],
                )
            by_request_id[request_id] = item

        expected_ids = {str(wire["request_id"]) for _, wire in prepared}
        if set(by_request_id) != expected_ids:
            return self._fill_worker_failure(
                answers,
                prepared,
                "worker_protocol_invalid",
                started,
                preflight["bundle_metadata"],
            )

        worker_bundle = worker_payload.get("bundle_metadata")
        bundle_metadata = (
            dict(worker_bundle)
            if isinstance(worker_bundle, Mapping)
            else dict(preflight["bundle_metadata"])
        )
        for index, wire in prepared:
            answers[index] = self._decode_worker_advice(
                str(wire["request_id"]),
                wire,
                by_request_id[str(wire["request_id"])],
                started,
                bundle_metadata,
                source_identity,
            )
        return [_required_answer(answer) for answer in answers]

    def _decode_worker_advice(
        self,
        request_id: str,
        request_wire: Mapping[str, object],
        item: Mapping[str, object],
        started: float,
        bundle_metadata: Mapping[str, object],
        source_identity: Mapping[str, object],
    ) -> SquillaAdvice:
        status = item.get("status")
        runtime = _runtime_receipt(started, item.get("worker_elapsed_ms"))
        if status != "available":
            diagnostic = item.get("diagnostic")
            return self._unavailable(
                request_id,
                diagnostic if isinstance(diagnostic, str) else "worker_result_invalid",
                elapsed_ms=_elapsed_ms(started),
                asset_status="unavailable",
                bundle_metadata=bundle_metadata,
                runtime=runtime,
                source_identity=source_identity,
            )

        tier = item.get("demand_tier")
        confidence = item.get("confidence")
        source = item.get("source")
        if (
            not isinstance(tier, str)
            or tier not in VALID_DEMAND_TIERS
            or tier not in request_wire["valid_tiers"]
            or not _valid_confidence(confidence)
            or source != UPSTREAM_SOURCE_LABEL
        ):
            return self._unavailable(
                request_id,
                "worker_result_invalid",
                elapsed_ms=_elapsed_ms(started),
                asset_status="unavailable",
                bundle_metadata=bundle_metadata,
                runtime=runtime,
                source_identity=source_identity,
            )

        extra = item.get("extra")
        if not isinstance(extra, Mapping):
            return self._unavailable(
                request_id,
                "worker_result_invalid",
                elapsed_ms=_elapsed_ms(started),
                asset_status="unavailable",
                bundle_metadata=bundle_metadata,
                runtime=runtime,
                source_identity=source_identity,
            )
        prompt = str(request_wire["prompt"])
        return SquillaAdvice(
            request_id=request_id,
            status="available",
            demand_tier=tier,
            confidence=float(confidence),
            source=_source_with_runtime_bundle(bundle_metadata, "available", source_identity),
            runtime=runtime,
            route_class=_route_class(extra.get("route_class")),
            thinking_hint=_safe_hint(extra.get("thinking_mode"), prompt),
            prompt_hint=_safe_hint(extra.get("prompt_hint"), prompt),
            prompt_policy=_safe_hint(extra.get("prompt_policy"), prompt),
        )

    def _fill_worker_failure(
        self,
        answers: list[SquillaAdvice | None],
        prepared: Sequence[tuple[int, Mapping[str, object]]],
        diagnostic: str,
        started: float,
        bundle_metadata: Mapping[str, object],
    ) -> list[SquillaAdvice]:
        for index, wire in prepared:
            answers[index] = self._unavailable(
                str(wire["request_id"]),
                diagnostic,
                elapsed_ms=_elapsed_ms(started),
                asset_status="unavailable",
                bundle_metadata=bundle_metadata,
            )
        return [_required_answer(answer) for answer in answers]

    def _unavailable(
        self,
        request_id: str,
        diagnostic: str,
        *,
        elapsed_ms: int,
        asset_status: str,
        bundle_metadata: Mapping[str, object] | None = None,
        runtime: Mapping[str, object] | None = None,
        source_identity: Mapping[str, object] | None = None,
    ) -> SquillaAdvice:
        return SquillaAdvice(
            request_id=request_id,
            status="unavailable",
            demand_tier=None,
            confidence=None,
            source=_source_with_runtime_bundle(
                bundle_metadata or {}, asset_status, source_identity
            ),
            runtime=dict(runtime) if runtime is not None else _runtime_receipt(elapsed_ms=elapsed_ms),
            diagnostic=diagnostic,
        )


def _request_to_wire(request: SquillaAdvisorRequest) -> dict[str, object]:
    if not isinstance(request, SquillaAdvisorRequest):
        raise TypeError("invalid_request_type")
    if not isinstance(request.request_id, str) or not request.request_id or len(request.request_id) > 256:
        raise ValueError("invalid_request_id")
    if not isinstance(request.prompt, str) or len(request.prompt) > _MAX_PROMPT_CHARS:
        raise ValueError("invalid_prompt")

    tiers = tuple(request.valid_tiers)
    if not tiers or any(not isinstance(tier, str) or tier not in VALID_DEMAND_TIERS for tier in tiers):
        raise ValueError("invalid_valid_tiers")
    if len(set(tiers)) != len(tiers):
        raise ValueError("duplicate_valid_tier")

    history_user_texts = tuple(request.history_user_texts)
    if len(history_user_texts) > _MAX_HISTORY_TURNS or any(
        not isinstance(text, str) or len(text) > _MAX_HISTORY_TEXT_CHARS
        for text in history_user_texts
    ):
        raise ValueError("invalid_history_user_texts")
    if len(request.routing_history) > _MAX_HISTORY_TURNS:
        raise ValueError("invalid_routing_history")

    previous_summary = request.previous_public_summary
    if previous_summary is not None and (
        not isinstance(previous_summary, str) or len(previous_summary) > _MAX_PUBLIC_SUMMARY_CHARS
    ):
        raise ValueError("invalid_previous_public_summary")

    return {
        "request_id": request.request_id,
        "prompt": request.prompt,
        "valid_tiers": list(tiers),
        "history_user_texts": list(history_user_texts),
        "routing_history": _sanitize_routing_history(request.routing_history),
        "previous_public_summary": previous_summary,
        "previous_public_usage": _json_value(request.previous_public_usage)
        if request.previous_public_usage is not None
        else None,
        "state_flags": _json_value(request.state_flags) if request.state_flags is not None else None,
    }


def _sanitize_routing_history(history: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    sanitized: list[dict[str, object]] = []
    for entry in history:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid_routing_history")
        clean: dict[str, object] = {}
        for key in _ROUTING_HISTORY_KEYS:
            value = entry.get(key)
            if value is None:
                continue
            if key in {"route_class", "final_route_class"}:
                if not isinstance(value, str) or value not in {"R0", "R1", "R2", "R3"}:
                    raise ValueError("invalid_routing_history")
                clean[key] = value
            elif _valid_number(value):
                clean[key] = float(value)
            else:
                raise ValueError("invalid_routing_history")
        sanitized.append(clean)
    return sanitized


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("invalid_json_value")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError("invalid_json_value")


def _preflight(runtime_python: Path, source_root: Path, bundle_dir: Path) -> dict[str, object]:
    bundle_metadata = _bundle_metadata(bundle_dir)
    if not runtime_python.is_absolute() or not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        return _preflight_failure("runtime_python_unavailable", bundle_metadata)
    if not source_root.is_absolute() or not source_root.is_dir():
        return _preflight_failure("source_root_unavailable", bundle_metadata)
    if not bundle_dir.is_absolute() or not bundle_dir.is_dir():
        return _preflight_failure("bundle_unavailable", bundle_metadata)

    strategy_path = source_root / "src" / "opensquilla" / "squilla_router" / "v4_phase3.py"
    if not strategy_path.is_file():
        return _preflight_failure("strategy_source_missing", bundle_metadata)
    if _is_lfs_pointer(strategy_path):
        return _preflight_failure("strategy_source_lfs_pointer", bundle_metadata)
    if not (bundle_dir / "runtime_src").is_dir():
        return _preflight_failure("bundle_runtime_src_missing", bundle_metadata)
    if not (bundle_dir / "router.runtime.yaml").is_file():
        return _preflight_failure("bundle_runtime_config_missing", bundle_metadata)

    manifest = bundle_dir / "artifact_manifest.json"
    if not manifest.is_file():
        return _preflight_failure("bundle_manifest_missing", bundle_metadata)
    try:
        content = json.loads(manifest.read_text(encoding="utf-8"))
        files = content.get("files", [])
        if content.get("schema_version") != 1 or not isinstance(files, list) or not files:
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        return _preflight_failure("bundle_manifest_invalid", bundle_metadata)
    for entry in files:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            return _preflight_failure("bundle_manifest_invalid", bundle_metadata)
        relative = Path(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            return _preflight_failure("bundle_manifest_invalid", bundle_metadata)
        asset = bundle_dir / relative
        if not asset.is_file():
            return _preflight_failure("bundle_asset_missing", bundle_metadata)
        if _is_lfs_pointer(asset):
            return _preflight_failure("bundle_asset_lfs_pointer", bundle_metadata)
    return {
        "diagnostic": None,
        "asset_status": "preflight_passed_not_native_inference_verified",
        "bundle_metadata": bundle_metadata,
    }


def _preflight_failure(diagnostic: str, bundle_metadata: Mapping[str, object]) -> dict[str, object]:
    return {
        "diagnostic": diagnostic,
        "asset_status": "unavailable",
        "bundle_metadata": bundle_metadata,
    }


def _bundle_metadata(bundle_dir: Path) -> dict[str, object]:
    metadata: dict[str, object] = {
        "path": str(bundle_dir),
        "bundle_version": "unknown",
        "source_model_version": "unknown",
        "feature_dim": None,
        "feature_schema_version": None,
    }
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


def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_LFS_POINTER_PREFIX)) == _LFS_POINTER_PREFIX
    except OSError:
        return False


def _source_with_runtime_bundle(
    bundle_metadata: Mapping[str, object],
    asset_status: str,
    source_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    source = SquillaAdvisor.source_metadata()
    identity = dict(source_identity or _unchecked_source_identity())
    source["expected_source_revision"] = identity["expected_source_revision"]
    source["observed_source_revision"] = identity["observed_source_revision"]
    source["verification_method"] = identity["verification_method"]
    source["runtime_bundle"] = {"asset_status": asset_status, **dict(bundle_metadata)}
    return source


def _unchecked_source_identity() -> dict[str, object]:
    return {
        "expected_source_revision": UPSTREAM_REVISION,
        "observed_source_revision": None,
        "verification_method": "not_performed",
    }


def _source_identity_from_worker(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    expected = value.get("expected_source_revision")
    observed = value.get("observed_source_revision")
    method = value.get("verification_method")
    if expected != UPSTREAM_REVISION or not isinstance(method, str):
        return None
    if observed is not None and not isinstance(observed, str):
        return None
    return {
        "expected_source_revision": expected,
        "observed_source_revision": observed,
        "verification_method": method,
    }


def _runtime_receipt(
    started: float | None = None,
    worker_elapsed_ms: object | None = None,
    *,
    elapsed_ms: int | None = None,
) -> dict[str, object]:
    if elapsed_ms is None:
        elapsed_ms = _elapsed_ms(started) if started is not None else 0
    receipt: dict[str, object] = {
        "mode": "local_headless_subprocess",
        "elapsed_ms": elapsed_ms,
    }
    if _valid_number(worker_elapsed_ms):
        receipt["worker_elapsed_ms"] = int(float(worker_elapsed_ms))
    return receipt


def _worker_environment() -> dict[str, str]:
    """Do not pass provider credentials or proxy configuration to the worker."""

    return {
        "NO_PROXY": "*",
        "no_proxy": "*",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
    }


def _valid_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _valid_confidence(value: object) -> bool:
    return _valid_number(value) and 0.0 <= float(value) <= 1.0


def _route_class(value: object) -> str | None:
    return value if isinstance(value, str) and value in {"R0", "R1", "R2", "R3"} else None


def _safe_hint(value: object, prompt: str) -> str | None:
    if not isinstance(value, str) or len(value) > _MAX_HINT_CHARS:
        return None
    return None if prompt and prompt in value else value


def _safe_diagnostic(exc: BaseException) -> str:
    value = str(exc)
    return value if value in {
        "invalid_request_type",
        "invalid_request_id",
        "invalid_prompt",
        "invalid_valid_tiers",
        "duplicate_valid_tier",
        "invalid_history_user_texts",
        "invalid_routing_history",
        "invalid_previous_public_summary",
        "invalid_json_value",
        "duplicate_request_id",
    } else "invalid_request"


def _request_id_for_receipt(request: object) -> str:
    request_id = getattr(request, "request_id", None)
    return request_id if isinstance(request_id, str) and request_id else "invalid-request"


def _required_answer(value: SquillaAdvice | None) -> SquillaAdvice:
    if value is None:
        raise RuntimeError("advisor response was not populated")
    return value


def _elapsed_ms(started: float | None) -> int:
    return int((time.monotonic() - started) * 1000) if started is not None else 0
