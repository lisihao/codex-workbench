"""Versioned, passive capability observations for Codex Workbench.

The Workbench must be able to notice that a local Codex or Claude Code
installation changed without treating an unverified model alias as a routing
permission.  This module deliberately keeps that boundary small:

* probes are CLI metadata/help commands only (never a model prompt or login);
* every observation is an immutable generation;
* the active generation is a separately, atomically-written pointer; and
* a newly discovered model is observed first.  It becomes routable only when
  it matches a known family policy and passes the policy's control-plane gate.

Routing and task persistence intentionally consume this module from their own
owners.  Keeping the registry independent makes it safe to refresh before
those integrations are upgraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Mapping


CAPABILITY_CATALOG_SCHEMA_VERSION = 1
CAPABILITY_CATALOG_PRODUCER = "codex-workbench.capabilities"
CAPABILITY_CATALOG_SOURCE = "local-cli-passive-observation-v1"
_GENERATION_ID = re.compile(r"^catalog-[0-9a-f]{16,64}$")
_SECRET_ENV_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


class CapabilityCatalogError(ValueError):
    """The capability catalog or its active pointer is not trustworthy."""


class CapabilityProbeError(CapabilityCatalogError):
    """A passive local CLI observation could not produce a complete catalog."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def scrubbed_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep normal runtime/proxy settings while never forwarding credentials."""

    candidate = dict(os.environ if environment is None else environment)
    return {
        name: value
        for name, value in candidate.items()
        if not any(part in name.upper() for part in _SECRET_ENV_PARTS)
    }


# These profiles are intentionally policy statements rather than performance
# claims.  They are used only after a local CLI has observed a usable model.
# A provider upgrade can inherit a *worker* family policy, while a future Sol
# identifier remains observed-only until an explicit control-plane policy names
# it.  This avoids silently moving the planner/verifier to an unreviewed model.
KNOWN_FAMILY_POLICIES: dict[str, dict[str, dict[str, Any]]] = {
    "codex": {
        "spark": {
            "roles": ["worker"],
            "task_types": ["implementation", "debugging", "tests", "docs", "exploration"],
            "quality": {"floor": "focused-mechanical", "preference": "short-low-risk"},
            "cost": {"unit_class": "independent-subscription-pool", "relative": "lowest"},
            "latency": {"class": "fastest"},
            "concurrency": {"weight": 1, "class": "high"},
            "reasoning": {"preferred_effort": "xhigh"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"delegated_worker": True, "research": False},
            "control_plane": False,
        },
        "luna": {
            "roles": ["worker"],
            "task_types": ["implementation", "debugging", "tests", "docs", "exploration"],
            "quality": {"floor": "production", "preference": "bounded-independent"},
            "cost": {"unit_class": "subscription", "relative": "efficient"},
            "latency": {"class": "fast"},
            "concurrency": {"weight": 1, "class": "high"},
            "reasoning": {"preferred_effort": "max"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"delegated_worker": True, "research": False},
            "control_plane": False,
        },
        "terra": {
            "roles": ["worker"],
            "task_types": ["implementation", "debugging", "tests", "docs", "exploration"],
            "quality": {"floor": "production", "preference": "larger-isolated-slice"},
            "cost": {"unit_class": "subscription", "relative": "balanced"},
            "latency": {"class": "balanced"},
            "concurrency": {"weight": 2, "class": "medium"},
            "reasoning": {"preferred_effort": "max"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"delegated_worker": True, "research": False},
            "control_plane": False,
        },
        "sol": {
            "roles": ["planner", "verifier", "architecture", "research"],
            "task_types": ["architecture", "review", "exploration"],
            "quality": {"floor": "frontier", "preference": "cross-module-decision"},
            "cost": {"unit_class": "subscription", "relative": "highest"},
            "latency": {"class": "deliberate"},
            "concurrency": {"weight": 3, "class": "control-plane"},
            "reasoning": {"preferred_effort": "max"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"delegated_worker": True, "research": True},
            "control_plane": True,
        },
        "astra": {
            "roles": ["planner", "verifier", "architecture", "research"],
            "task_types": ["architecture", "review", "exploration"],
            "quality": {"floor": "frontier", "preference": "cross-module-decision"},
            "cost": {"unit_class": "subscription", "relative": "highest"},
            "latency": {"class": "deliberate"},
            "concurrency": {"weight": 3, "class": "control-plane"},
            "reasoning": {"preferred_effort": "max"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"delegated_worker": True, "research": True},
            "control_plane": True,
        },
    },
    "claude": {
        "sonnet": {
            "roles": ["worker", "reviewer"],
            "task_types": ["implementation", "debugging", "tests", "docs", "exploration", "review"],
            "quality": {"floor": "production", "preference": "daily-engineering"},
            "cost": {"unit_class": "shared-subscription", "relative": "balanced"},
            "latency": {"class": "fast"},
            "concurrency": {"weight": 1, "class": "high"},
            "reasoning": {"preferred_effort": "high"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"advisor": True, "remote_control": True, "research": False},
            "control_plane": False,
        },
        "opus": {
            "roles": ["architecture_challenge", "reviewer", "research"],
            "task_types": ["architecture", "review", "exploration"],
            "quality": {"floor": "frontier", "preference": "hard-reasoning"},
            "cost": {"unit_class": "shared-subscription", "relative": "high"},
            "latency": {"class": "deliberate"},
            "concurrency": {"weight": 2, "class": "medium"},
            "reasoning": {"preferred_effort": "max"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"advisor": True, "remote_control": True, "research": True},
            "control_plane": False,
        },
        "fable": {
            "roles": ["architecture_challenge", "reviewer", "research", "creative"],
            "task_types": ["architecture", "review", "creative", "exploration"],
            "quality": {"floor": "frontier", "preference": "architecture-and-creative-challenge"},
            "cost": {"unit_class": "shared-subscription", "relative": "high"},
            "latency": {"class": "deliberate"},
            "concurrency": {"weight": 2, "class": "medium"},
            "reasoning": {"preferred_effort": "max"},
            "tools": {"code_mode": True, "structured_output": True},
            "features": {"advisor": True, "remote_control": True, "research": True},
            "control_plane": False,
        },
    },
}

# Exact control-plane IDs are deliberately named. A later Sol or Astra
# identifier is observed first and never inherits planner/verifier authority.
_CONTROL_PLANE_FAMILIES = frozenset({"sol", "astra"})
_CONTROL_PLANE_EXACT_IDS = {"gpt-5.6-sol", "gpt-6-astra"}


def model_family(provider: str, model_id: str) -> str | None:
    """Return a known family from an exact CLI selection identifier."""

    normalized_provider = provider.strip().lower()
    normalized_model = model_id.strip().lower()
    if normalized_provider == "codex":
        for family in ("spark", "luna", "terra", "sol", "astra"):
            if re.search(rf"(?:^|[-_.]){re.escape(family)}(?:$|[-_.])", normalized_model):
                return family
    elif normalized_provider == "claude":
        for family in ("sonnet", "opus", "fable"):
            if normalized_model == family or re.search(
                rf"(?:^|[-_.]){re.escape(family)}(?:$|[-_.])", normalized_model
            ):
                return family
    return None


def family_policy(provider: str, family: str | None) -> dict[str, Any] | None:
    """Copy the safe, static policy for a known provider family."""

    if family is None:
        return None
    policy = KNOWN_FAMILY_POLICIES.get(provider.strip().lower(), {}).get(family)
    if policy is None:
        return None
    return json.loads(json.dumps(policy))


def is_routable_model(record: Mapping[str, Any], *, role: str | None = None) -> bool:
    """Return whether a catalog model is admissible, optionally for one role."""

    if record.get("status") != "available" or record.get("routable") is not True:
        return False
    roles = record.get("roles")
    return role is None or (isinstance(roles, list) and role in roles)


def routable_models(
    catalog: Mapping[str, Any], *, provider: str | None = None, role: str | None = None
) -> list[dict[str, Any]]:
    """Return copied, safe-to-route records from a validated catalog."""

    validate_catalog(catalog)
    selected: list[dict[str, Any]] = []
    for record in catalog["models"]:
        if provider is not None and record["provider"] != provider:
            continue
        if is_routable_model(record, role=role):
            selected.append(json.loads(json.dumps(record)))
    return selected


def _status_from_codex_model(raw: Mapping[str, Any]) -> str:
    visibility = str(raw.get("visibility", "")).strip().lower()
    if raw.get("deprecated") is True or raw.get("retired") is True or raw.get("upgrade"):
        return "deprecated"
    if visibility in {"deprecated", "retired"}:
        return "deprecated"
    if visibility in {"list", "visible", "available", ""}:
        return "available"
    return "observed"


def _record(
    *,
    provider: str,
    model_id: str,
    status: str,
    agent_cli_version: str | None,
    observed_at: str,
    source: Mapping[str, Any],
    provenance: Mapping[str, Any],
    runtime: Mapping[str, Any] | None = None,
    identity_kind: str = "exact-cli-id",
) -> dict[str, Any]:
    family = model_family(provider, model_id)
    policy = family_policy(provider, family)
    policy_origin = "observed-only"
    control_plane_eligible = False
    roles: list[str] = []
    task_types: list[str] = []
    quality: dict[str, Any] = {"floor": "unknown"}
    cost: dict[str, Any] = {"unit_class": "unknown", "relative": "unknown"}
    latency: dict[str, Any] = {"class": "unknown"}
    concurrency: dict[str, Any] = {"weight": None, "class": "unknown"}
    reasoning: dict[str, Any] = {"preferred_effort": None, "supported_efforts": []}
    tools: dict[str, Any] = {}
    features: dict[str, Any] = {}
    if policy is not None:
        policy_origin = "exact-policy" if model_id in _CONTROL_PLANE_EXACT_IDS or provider == "claude" else "family-inherited"
        roles = list(policy["roles"])
        task_types = list(policy["task_types"])
        quality = dict(policy["quality"])
        cost = dict(policy["cost"])
        latency = dict(policy["latency"])
        concurrency = dict(policy["concurrency"])
        reasoning = dict(policy["reasoning"])
        tools = dict(policy["tools"])
        features = dict(policy["features"])
        if provider == "codex" and family in _CONTROL_PLANE_FAMILIES and model_id not in _CONTROL_PLANE_EXACT_IDS:
            # A discovered next-generation control-plane model is useful
            # evidence, not an automatic transfer of control-plane authority.
            roles = [role for role in roles if role not in {"planner", "verifier"}]
            policy_origin = "family-inherited-control-plane-pending"
        else:
            control_plane_eligible = bool(policy.get("control_plane"))

    runtime_data = dict(runtime or {})
    if isinstance(runtime_data.get("supported_efforts"), list):
        reasoning["supported_efforts"] = list(runtime_data["supported_efforts"])
    if runtime_data.get("default_effort") is not None:
        reasoning["default_effort"] = runtime_data["default_effort"]
    elif "default_effort" not in reasoning:
        reasoning["default_effort"] = reasoning.get("preferred_effort")
    tools = {**tools, **dict(runtime_data.get("tools", {}))}
    features = {**features, **dict(runtime_data.get("features", {}))}
    routable = (
        status == "available"
        and policy is not None
        and not (provider == "codex" and family in _CONTROL_PLANE_FAMILIES and model_id not in _CONTROL_PLANE_EXACT_IDS)
    )
    return {
        "provider": provider,
        "model_id": model_id,
        "model_family": family or "unknown",
        "identity": {
            "selection_id": model_id,
            "kind": identity_kind,
            "canonical_model_id": model_id if identity_kind == "exact-cli-id" else None,
        },
        "status": status,
        "routable": routable,
        "control_plane_eligible": control_plane_eligible and routable,
        "policy_origin": policy_origin,
        "roles": roles,
        "task_types": task_types,
        "quality": quality,
        "cost": cost,
        "latency": latency,
        "concurrency": concurrency,
        "reasoning": reasoning,
        "tools": tools,
        "features": features,
        "source": dict(source),
        "provenance": dict(provenance),
        "agent_cli_version": agent_cli_version,
        "observed_at": observed_at,
    }


def _codex_record(raw: Mapping[str, Any], *, cli_version: str, observed_at: str, channel: str) -> dict[str, Any]:
    model_id = raw.get("slug")
    if not isinstance(model_id, str) or not model_id.strip():
        raise CapabilityProbeError("codex debug models item is missing slug")
    supported = raw.get("supported_reasoning_levels", [])
    if not isinstance(supported, list):
        raise CapabilityProbeError(f"codex model {model_id!r} has invalid reasoning levels")
    efforts = [item.get("effort") for item in supported if isinstance(item, Mapping) and isinstance(item.get("effort"), str)]
    raw_subset = {
        key: raw.get(key)
        for key in (
            "slug", "display_name", "description", "visibility", "supported_in_api",
            "default_reasoning_level", "supported_reasoning_levels", "input_modalities",
            "shell_type", "tool_mode", "supports_search_tool", "experimental_supported_tools",
            "multi_agent_version", "context_window", "max_context_window", "upgrade",
        )
        if key in raw
    }
    runtime = {
        "supported_efforts": efforts,
        "default_effort": raw.get("default_reasoning_level"),
        "tools": {
            "shell_type": raw.get("shell_type"),
            "tool_mode": raw.get("tool_mode"),
            "experimental_supported_tools": list(raw.get("experimental_supported_tools", []))
            if isinstance(raw.get("experimental_supported_tools"), list)
            else [],
        },
        "features": {
            "input_modalities": list(raw.get("input_modalities", []))
            if isinstance(raw.get("input_modalities"), list)
            else [],
            "supports_search_tool": raw.get("supports_search_tool") is True,
            "multi_agent_version": raw.get("multi_agent_version"),
            "context_window": raw.get("context_window"),
            "max_context_window": raw.get("max_context_window"),
            "supported_in_api": raw.get("supported_in_api") is True,
        },
    }
    return _record(
        provider="codex",
        model_id=model_id.strip(),
        status=_status_from_codex_model(raw),
        agent_cli_version=cli_version,
        observed_at=observed_at,
        source={"kind": "codex-debug-models", "channel": channel},
        provenance={"cli_version": cli_version, "raw_model_digest": canonical_hash(raw_subset)},
        runtime=runtime,
    )


def _claude_record(model_id: str, *, cli_version: str, observed_at: str) -> dict[str, Any]:
    return _record(
        provider="claude",
        model_id=model_id,
        status="available",
        agent_cli_version=cli_version,
        observed_at=observed_at,
        source={"kind": "claude-cli-help", "channel": "local"},
        provenance={"cli_version": cli_version, "selection_token": model_id},
        runtime={"features": {"model_selector": True}},
        identity_kind="cli-alias",
    )


def _catalog_body(
    *,
    observed_at: str,
    agents: Mapping[str, Any],
    models: Iterable[Mapping[str, Any]],
    probe_errors: Iterable[str],
) -> dict[str, Any]:
    return {
        "schema_version": CAPABILITY_CATALOG_SCHEMA_VERSION,
        "producer": CAPABILITY_CATALOG_PRODUCER,
        "source": CAPABILITY_CATALOG_SOURCE,
        "observed_at": observed_at,
        "agents": dict(agents),
        "models": [dict(item) for item in models],
        "probe_errors": list(probe_errors),
    }


def build_catalog(
    *,
    observed_at: str,
    agents: Mapping[str, Any],
    models: Iterable[Mapping[str, Any]],
    probe_errors: Iterable[str] = (),
) -> dict[str, Any]:
    """Build and validate one immutable catalog generation from observations."""

    body = _catalog_body(
        observed_at=observed_at,
        agents=agents,
        models=models,
        probe_errors=probe_errors,
    )
    digest = canonical_hash(body)
    catalog = {**body, "catalog_id": f"catalog-{digest[:16]}", "digest": digest}
    return validate_catalog(catalog)


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityCatalogError(f"catalog {field} must be a non-empty string")
    return value


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityCatalogError(f"catalog {field} must be an object")
    return value


def _validate_model(record: Mapping[str, Any]) -> dict[str, Any]:
    provider = _require_string(record.get("provider"), "model provider")
    model_id = _require_string(record.get("model_id"), "model_id")
    status = _require_string(record.get("status"), "model status")
    if status not in {"available", "observed", "deprecated", "unavailable"}:
        raise CapabilityCatalogError(f"catalog model {model_id!r} has unsupported status {status!r}")
    if not isinstance(record.get("routable"), bool):
        raise CapabilityCatalogError(f"catalog model {model_id!r} routable must be boolean")
    if status != "available" and record["routable"]:
        raise CapabilityCatalogError(f"catalog model {model_id!r} is not available but is routable")
    family = _require_string(record.get("model_family"), "model_family")
    if family == "unknown" and record["routable"]:
        raise CapabilityCatalogError(f"unknown catalog model {model_id!r} cannot be routable")
    policy_origin = _require_string(record.get("policy_origin"), "policy_origin")
    if policy_origin == "observed-only" and record["routable"]:
        raise CapabilityCatalogError(f"observed-only catalog model {model_id!r} cannot be routable")
    if record.get("control_plane_eligible") is True:
        roles = record.get("roles")
        if not record["routable"] or not isinstance(roles, list) or not {"planner", "verifier"}.issubset(roles):
            raise CapabilityCatalogError(f"catalog model {model_id!r} has invalid control-plane eligibility")
        if provider != "codex" or model_id not in _CONTROL_PLANE_EXACT_IDS:
            raise CapabilityCatalogError(f"catalog model {model_id!r} is not an allowed control-plane ID")
    for field in (
        "identity", "quality", "cost", "latency", "concurrency", "reasoning", "tools",
        "features", "source", "provenance",
    ):
        _require_mapping(record.get(field), f"model {model_id} {field}")
    for field in ("roles", "task_types"):
        if not isinstance(record.get(field), list) or not all(isinstance(item, str) for item in record[field]):
            raise CapabilityCatalogError(f"catalog model {model_id!r} {field} must be string list")
    _require_string(record.get("observed_at"), f"model {model_id} observed_at")
    _require_string(record.get("agent_cli_version"), f"model {model_id} agent_cli_version")
    return json.loads(json.dumps(dict(record)))


def validate_catalog(raw: Mapping[str, Any] | object) -> dict[str, Any]:
    """Validate a persisted catalog, rejecting malformed or unsafe admissions."""

    catalog = _require_mapping(raw, "catalog")
    if catalog.get("schema_version") != CAPABILITY_CATALOG_SCHEMA_VERSION:
        raise CapabilityCatalogError("unsupported capability catalog schema version")
    if catalog.get("producer") != CAPABILITY_CATALOG_PRODUCER:
        raise CapabilityCatalogError("invalid capability catalog producer")
    if catalog.get("source") != CAPABILITY_CATALOG_SOURCE:
        raise CapabilityCatalogError("invalid capability catalog source")
    _require_string(catalog.get("observed_at"), "observed_at")
    catalog_id = _require_string(catalog.get("catalog_id"), "catalog_id")
    if _GENERATION_ID.fullmatch(catalog_id) is None:
        raise CapabilityCatalogError("invalid capability catalog generation ID")
    digest = _require_string(catalog.get("digest"), "digest")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CapabilityCatalogError("invalid capability catalog digest")
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise CapabilityCatalogError("capability catalog must contain at least one model")
    model_records = [_validate_model(_require_mapping(item, "model")) for item in models]
    identities = {(item["provider"], item["model_id"]) for item in model_records}
    if len(identities) != len(model_records):
        raise CapabilityCatalogError("capability catalog contains duplicate provider/model IDs")
    agents = _require_mapping(catalog.get("agents"), "agents")
    for provider in ("codex", "claude"):
        agent = _require_mapping(agents.get(provider), f"agents.{provider}")
        _require_string(agent.get("status"), f"agents.{provider}.status")
        _require_string(agent.get("cli_version"), f"agents.{provider}.cli_version")
        _require_mapping(agent.get("features"), f"agents.{provider}.features")
        _require_mapping(agent.get("source"), f"agents.{provider}.source")
        _require_mapping(agent.get("provenance"), f"agents.{provider}.provenance")
        _require_string(agent.get("observed_at"), f"agents.{provider}.observed_at")
    if not isinstance(catalog.get("probe_errors"), list) or not all(
        isinstance(item, str) for item in catalog["probe_errors"]
    ):
        raise CapabilityCatalogError("catalog probe_errors must be a string list")
    body = {
        key: catalog[key]
        for key in ("schema_version", "producer", "source", "observed_at", "agents", "models", "probe_errors")
    }
    expected_digest = canonical_hash(body)
    if expected_digest != digest or catalog_id != f"catalog-{digest[:16]}":
        raise CapabilityCatalogError("capability catalog digest does not match its contents")
    return json.loads(json.dumps(dict(catalog)))


def _safe_error(error: BaseException) -> str:
    text = str(error).replace("\n", " ").strip()
    return text[:300] if text else type(error).__name__


@dataclass(frozen=True)
class CapabilityRegistry:
    """Persistent capability catalog rooted beneath one Workbench state root."""

    state_root: Path
    codex_binary: str | Path = "codex"
    claude_binary: str | Path = "claude"
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    timeout_seconds: float = 20

    @property
    def root(self) -> Path:
        return self.state_root / "capabilities"

    @property
    def generations_dir(self) -> Path:
        return self.root / "generations"

    @property
    def active_path(self) -> Path:
        return self.root / "active.json"

    @property
    def refresh_path(self) -> Path:
        return self.root / "last-refresh.json"

    def refresh(self, *, activate_safe: bool = False, bundled: bool = False) -> dict[str, Any]:
        """Passively observe local CLIs and optionally activate a safe generation.

        A failed core Codex observation never replaces the known-good active
        pointer.  The failure is recorded independently so the immutable active
        catalog stays immutable and route decisions remain reproducible.
        """

        try:
            current = self.active()
        except CapabilityCatalogError as error:
            return self._refresh_failure(error, active=None)
        try:
            catalog = self._probe(bundled=bundled)
            unchanged = current is not None and self._functional_catalog(
                current
            ) == self._functional_catalog(catalog)
            effective_catalog = current if unchanged else catalog
            if not unchanged:
                self._write_generation(catalog)
            activation: dict[str, Any] | None = None
            if current is None or (activate_safe and not unchanged):
                activation = self.activate(effective_catalog["catalog_id"], safe=True)
            outcome = {
                "ok": True,
                "catalog": effective_catalog,
                "active_generation_id": (
                    activation["active_generation_id"] if activation is not None else self._active_generation_id()
                ),
                "activated": activation is not None,
                "unchanged": unchanged,
                "probe_errors": list(catalog["probe_errors"]),
            }
            self._write_refresh_status({
                "ok": True,
                "observed_at": now_iso(),
                "catalog_id": effective_catalog["catalog_id"],
                "unchanged": unchanged,
                "probe_errors": list(catalog["probe_errors"]),
            })
            return outcome
        except CapabilityCatalogError as error:
            return self._refresh_failure(error, active=current)
        except (OSError, subprocess.SubprocessError) as error:
            return self._refresh_failure(CapabilityProbeError(_safe_error(error)), active=current)

    def status(self) -> dict[str, Any]:
        """Return active catalog, immutable generation count, and last refresh."""

        active: dict[str, Any] | None = None
        error: str | None = None
        try:
            active = self.active()
        except CapabilityCatalogError as exc:
            error = str(exc)
        refresh: dict[str, Any] | None = None
        if self.refresh_path.exists():
            try:
                raw = json.loads(self.refresh_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    refresh = raw
                else:
                    error = error or "last refresh receipt is malformed"
            except (OSError, json.JSONDecodeError):
                error = error or "last refresh receipt is unreadable"
        generations = self._generation_ids()
        return {
            "ok": active is not None and error is None,
            "active_generation_id": active["catalog_id"] if active is not None else None,
            "active": active,
            "generation_count": len(generations),
            "generations": generations,
            "last_refresh": refresh,
            "error": error,
        }

    def active(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        pointer = self._read_pointer()
        return self.load_generation(pointer["active_generation_id"])

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        self._validate_generation_id(generation_id)
        path = self.generations_dir / f"{generation_id}.json"
        if not path.exists():
            raise CapabilityCatalogError(f"capability catalog generation {generation_id!r} is missing")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CapabilityCatalogError(f"capability catalog generation {generation_id!r} is unreadable") from error
        catalog = validate_catalog(raw)
        if catalog["catalog_id"] != generation_id:
            raise CapabilityCatalogError("capability catalog generation filename does not match content")
        return catalog

    def activate(self, generation_id: str, *, safe: bool = True) -> dict[str, Any]:
        """Atomically point new tasks at an existing immutable generation."""

        target = self.load_generation(generation_id)
        if safe:
            errors = self.safe_activation_errors(target)
            if errors:
                raise CapabilityCatalogError("catalog is not safe to activate: " + "; ".join(errors))
        current = self._read_pointer(optional=True)
        history = list(current["history"]) if current is not None else []
        if not history or history[-1] != generation_id:
            history.append(generation_id)
        pointer = {
            "schema_version": CAPABILITY_CATALOG_SCHEMA_VERSION,
            "producer": CAPABILITY_CATALOG_PRODUCER,
            "active_generation_id": generation_id,
            "history": history,
            "updated_at": now_iso(),
        }
        self._atomic_write_json(self.active_path, pointer)
        return {
            "ok": True,
            "active_generation_id": generation_id,
            "history": history,
            "safe": safe,
        }

    def rollback(self) -> dict[str, Any]:
        """Move the active pointer back one activated generation without deleting it."""

        pointer = self._read_pointer()
        history = list(pointer["history"])
        if len(history) < 2:
            raise CapabilityCatalogError("no previous active capability catalog is available for rollback")
        abandoned = history.pop()
        previous = history[-1]
        self.load_generation(previous)
        replacement = {
            "schema_version": CAPABILITY_CATALOG_SCHEMA_VERSION,
            "producer": CAPABILITY_CATALOG_PRODUCER,
            "active_generation_id": previous,
            "history": history,
            "updated_at": now_iso(),
            "rolled_back_from": abandoned,
        }
        self._atomic_write_json(self.active_path, replacement)
        return {
            "ok": True,
            "active_generation_id": previous,
            "rolled_back_from": abandoned,
            "history": history,
        }

    def diff(
        self, from_generation_id: str | None = None, to_generation_id: str | None = None
    ) -> dict[str, Any]:
        """Diff functional capability fields, ignoring per-refresh timestamps."""

        pointer = self._read_pointer(optional=True)
        if to_generation_id is None:
            if pointer is None:
                raise CapabilityCatalogError("there is no active capability catalog to diff")
            to_generation_id = pointer["active_generation_id"]
        if from_generation_id is None:
            if pointer is not None and pointer["active_generation_id"] == to_generation_id and len(pointer["history"]) >= 2:
                from_generation_id = pointer["history"][-2]
            else:
                candidates = [item for item in self._generation_ids() if item != to_generation_id]
                from_generation_id = candidates[-1] if candidates else None
        target = self.load_generation(to_generation_id)
        source = self.load_generation(from_generation_id) if from_generation_id is not None else None
        source_models = self._model_index(source["models"]) if source is not None else {}
        target_models = self._model_index(target["models"])
        added = [self._identity_text(key) for key in sorted(target_models.keys() - source_models.keys())]
        removed = [self._identity_text(key) for key in sorted(source_models.keys() - target_models.keys())]
        changed: list[dict[str, Any]] = []
        for key in sorted(target_models.keys() & source_models.keys()):
            before = self._functional_model(source_models[key])
            after = self._functional_model(target_models[key])
            fields = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
            if fields:
                changed.append({"provider": key[0], "model_id": key[1], "fields": fields})
        agent_changes: list[dict[str, Any]] = []
        source_agents = source.get("agents", {}) if source is not None else {}
        target_agents = target.get("agents", {})
        for provider in sorted(set(source_agents) | set(target_agents)):
            before = self._functional_agent(source_agents.get(provider))
            after = self._functional_agent(target_agents.get(provider))
            fields = sorted(
                name
                for name in set(before) | set(after)
                if before.get(name) != after.get(name)
            )
            if fields:
                agent_changes.append({"provider": provider, "fields": fields})
        return {
            "ok": True,
            "from_generation_id": source["catalog_id"] if source is not None else None,
            "to_generation_id": target["catalog_id"],
            "added": added,
            "removed": removed,
            "changed": changed,
            "agents_changed": agent_changes,
            "probe_errors_changed": (
                list(source.get("probe_errors", ())) if source is not None else []
            )
            != list(target.get("probe_errors", ())),
        }

    @staticmethod
    def safe_activation_errors(catalog: Mapping[str, Any]) -> list[str]:
        """Return reasons a catalog cannot replace the current safe default."""

        validated = validate_catalog(catalog)
        codex_agent = validated["agents"]["codex"]
        if codex_agent["status"] != "available":
            return ["Codex CLI is not available"]
        models = validated["models"]
        control = [
            item
            for item in models
            if item["provider"] == "codex"
            and item["model_id"] == "gpt-5.6-sol"
            and is_routable_model(item, role="planner")
            and is_routable_model(item, role="verifier")
            and item["control_plane_eligible"] is True
        ]
        workers = [
            item
            for item in models
            if item["provider"] == "codex" and is_routable_model(item, role="worker")
        ]
        errors: list[str] = []
        if not control:
            errors.append("no exact-policy Codex Sol planner/verifier is available")
        if not workers:
            errors.append("no routable Codex worker is available")
        return errors

    def _probe(self, *, bundled: bool) -> dict[str, Any]:
        observed_at = now_iso()
        codex_version = self._version(self.codex_binary, "codex")
        command = ["debug", "models"] + (["--bundled"] if bundled else [])
        model_payload = self._json_command(self.codex_binary, command, "codex debug models")
        raw_models = model_payload.get("models") if isinstance(model_payload, Mapping) else None
        if not isinstance(raw_models, list) or not raw_models:
            raise CapabilityProbeError("codex debug models did not return a non-empty models list")
        channel = "bundled" if bundled else "live"
        models = [
            _codex_record(_require_mapping(raw, "codex debug model"), cli_version=codex_version, observed_at=observed_at, channel=channel)
            for raw in raw_models
        ]
        codex_features, codex_errors = self._help_features(
            self.codex_binary, ("agents", "remote-control", "app-server")
        )
        agents: dict[str, Any] = {
            "codex": {
                "status": "available",
                "cli_version": codex_version,
                "features": codex_features,
                "source": {"kind": "codex-cli-version-and-help", "channel": channel},
                "provenance": {"cli_version": codex_version},
                "observed_at": observed_at,
            }
        }
        probe_errors = list(codex_errors)

        claude_agent, claude_models, claude_errors = self._probe_claude(observed_at)
        agents["claude"] = claude_agent
        models.extend(claude_models)
        probe_errors.extend(claude_errors)
        return build_catalog(
            observed_at=observed_at,
            agents=agents,
            models=models,
            probe_errors=probe_errors,
        )

    def _probe_claude(self, observed_at: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        try:
            version = self._version(self.claude_binary, "claude")
            help_result = self._command(self.claude_binary, ["--help"], "claude --help")
            if help_result.returncode != 0:
                raise CapabilityProbeError("claude --help exited non-zero")
            features, feature_errors = self._help_features(
                self.claude_binary, ("agents", "remote-control")
            )
            help_text = self._stdout(help_result)
            aliases = [
                name for name in ("sonnet", "opus", "fable")
                if re.search(rf"\b{re.escape(name)}\b", help_text, re.IGNORECASE)
            ]
            models = [_claude_record(name, cli_version=version, observed_at=observed_at) for name in aliases]
            agent = {
                "status": "available",
                "cli_version": version,
                "features": {"model_aliases": aliases, **features},
                "source": {"kind": "claude-cli-version-and-help", "channel": "local"},
                "provenance": {"cli_version": version},
                "observed_at": observed_at,
            }
            return agent, models, feature_errors
        except CapabilityCatalogError as error:
            agent = {
                "status": "unavailable",
                "cli_version": "unavailable",
                "features": {"model_aliases": []},
                "source": {"kind": "claude-cli-passive-observation", "channel": "local"},
                "provenance": {"error": _safe_error(error)},
                "observed_at": observed_at,
            }
            return agent, [], [f"Claude passive capability probe failed: {_safe_error(error)}"]

    def _version(self, binary: str | Path, label: str) -> str:
        result = self._command(binary, ["--version"], f"{label} --version")
        if result.returncode != 0:
            raise CapabilityProbeError(f"{label} --version exited non-zero")
        output = self._stdout(result).strip()
        if not output:
            raise CapabilityProbeError(f"{label} --version returned no output")
        match = re.search(r"\b\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?\b", output)
        return match.group(0) if match is not None else output[:120]

    def _help_features(
        self, binary: str | Path, subcommands: Iterable[str]
    ) -> tuple[dict[str, Any], list[str]]:
        features: dict[str, Any] = {}
        errors: list[str] = []
        for subcommand in subcommands:
            try:
                result = self._command(binary, [subcommand, "--help"], f"{Path(str(binary)).name} {subcommand} --help")
                available = result.returncode == 0
                features[subcommand.replace("-", "_")] = available
                if not available:
                    errors.append(f"{Path(str(binary)).name} {subcommand} --help exited non-zero")
            except CapabilityCatalogError as error:
                features[subcommand.replace("-", "_")] = False
                errors.append(_safe_error(error))
        return features, errors

    def _json_command(self, binary: str | Path, arguments: list[str], label: str) -> Mapping[str, Any]:
        result = self._command(binary, arguments, label)
        if result.returncode != 0:
            raise CapabilityProbeError(f"{label} exited non-zero")
        try:
            payload = json.loads(self._stdout(result))
        except json.JSONDecodeError as error:
            raise CapabilityProbeError(f"{label} did not return JSON") from error
        return _require_mapping(payload, f"{label} payload")

    def _command(
        self, binary: str | Path, arguments: list[str], label: str
    ) -> subprocess.CompletedProcess[str]:
        command = [str(binary), *arguments]
        try:
            return self.runner(
                command,
                text=True,
                capture_output=True,
                check=False,
                env=scrubbed_environment(),
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise CapabilityProbeError(f"{label} timed out after {self.timeout_seconds:g}s") from error
        except OSError as error:
            raise CapabilityProbeError(f"{label} could not start") from error

    @staticmethod
    def _stdout(result: subprocess.CompletedProcess[str]) -> str:
        output = result.stdout
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        return output if isinstance(output, str) else ""

    def _write_generation(self, catalog: Mapping[str, Any]) -> Path:
        valid = validate_catalog(catalog)
        path = self.generations_dir / f"{valid['catalog_id']}.json"
        if path.exists():
            existing = self.load_generation(valid["catalog_id"])
            if existing["digest"] != valid["digest"]:
                raise CapabilityCatalogError("capability catalog generation ID collision")
            return path
        self._atomic_write_json(path, valid)
        return path

    def _refresh_failure(self, error: CapabilityCatalogError, *, active: dict[str, Any] | None) -> dict[str, Any]:
        message = _safe_error(error)
        self._write_refresh_status({
            "ok": False,
            "observed_at": now_iso(),
            "error": message,
            "reused_active_generation_id": active["catalog_id"] if active is not None else None,
        })
        return {
            "ok": False,
            "error": message,
            "catalog": active,
            "active_generation_id": active["catalog_id"] if active is not None else None,
            "reused_active": active is not None,
        }

    def _write_refresh_status(self, value: Mapping[str, Any]) -> None:
        self._atomic_write_json(self.refresh_path, dict(value))

    def _read_pointer(self, *, optional: bool = False) -> dict[str, Any] | None:
        if not self.active_path.exists():
            if optional:
                return None
            raise CapabilityCatalogError("no active capability catalog")
        try:
            raw = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CapabilityCatalogError("active capability catalog pointer is unreadable") from error
        pointer = _require_mapping(raw, "active capability catalog pointer")
        if pointer.get("schema_version") != CAPABILITY_CATALOG_SCHEMA_VERSION:
            raise CapabilityCatalogError("active capability catalog pointer has unsupported schema")
        if pointer.get("producer") != CAPABILITY_CATALOG_PRODUCER:
            raise CapabilityCatalogError("active capability catalog pointer has invalid producer")
        generation_id = _require_string(pointer.get("active_generation_id"), "active_generation_id")
        self._validate_generation_id(generation_id)
        history = pointer.get("history")
        if not isinstance(history, list) or not history or not all(isinstance(item, str) for item in history):
            raise CapabilityCatalogError("active capability catalog pointer history is invalid")
        for item in history:
            self._validate_generation_id(item)
        if history[-1] != generation_id:
            raise CapabilityCatalogError("active capability catalog pointer history does not end at active generation")
        _require_string(pointer.get("updated_at"), "active pointer updated_at")
        return json.loads(json.dumps(dict(pointer)))

    def _active_generation_id(self) -> str | None:
        pointer = self._read_pointer(optional=True)
        return pointer["active_generation_id"] if pointer is not None else None

    def _generation_ids(self) -> list[str]:
        if not self.generations_dir.exists():
            return []
        return sorted(
            path.stem
            for path in self.generations_dir.glob("catalog-*.json")
            if _GENERATION_ID.fullmatch(path.stem) is not None
        )

    @staticmethod
    def _validate_generation_id(generation_id: str) -> None:
        if not isinstance(generation_id, str) or _GENERATION_ID.fullmatch(generation_id) is None:
            raise CapabilityCatalogError("invalid capability catalog generation ID")

    def _atomic_write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(canonical_json(dict(payload)) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _model_index(models: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
        return {(str(item["provider"]), str(item["model_id"])): item for item in models}

    @staticmethod
    def _identity_text(identity: tuple[str, str]) -> str:
        return f"{identity[0]}:{identity[1]}"

    @staticmethod
    def _functional_model(record: Mapping[str, Any]) -> dict[str, Any]:
        ignored = {"observed_at", "source"}
        return {key: value for key, value in record.items() if key not in ignored}

    @staticmethod
    def _functional_agent(record: object) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            return {}
        ignored = {"observed_at", "source"}
        return {key: value for key, value in record.items() if key not in ignored}

    @classmethod
    def _functional_catalog(cls, catalog: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "agents": {
                provider: cls._functional_agent(agent)
                for provider, agent in sorted(catalog["agents"].items())
            },
            "models": sorted(
                (cls._functional_model(model) for model in catalog["models"]),
                key=lambda item: (
                    str(item.get("provider")),
                    str(item.get("model_id")),
                ),
            ),
            "probe_errors": list(catalog.get("probe_errors", ())),
        }
