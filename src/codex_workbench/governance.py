from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Any, Literal, Mapping

from .archify import (
    ARCHIFY_COMMIT,
    ARCHIFY_LICENSE,
    ARCHIFY_REPOSITORY,
    ARCHIFY_TAG,
    ARCHIFY_VERSION,
    ArchifyContractError,
    default_vendor_root,
    installed_archify_status,
    repository_root,
    verify_skill_projection,
    verify_vendor,
)


CODE_AS_HARNESS_PROFILE = "code-as-harness/v1"
CODE_AS_HARNESS_SKILL_NAME = "code-as-harness"
CODE_AS_HARNESS_ARTIFACT_KIND = "workbench-canonical-compatible-skill"
CODE_AS_HARNESS_SKILL_MARKER = "codex_workbench_managed: true"
CODE_AS_HARNESS_POLICY_START = "<!-- CODEX-WORKBENCH-CODE-AS-HARNESS:START -->"
CODE_AS_HARNESS_POLICY_END = "<!-- CODEX-WORKBENCH-CODE-AS-HARNESS:END -->"
CODE_AS_HARNESS_SKILL_REQUIRED_TEXT = (
    "## Operating contract",
    "Fill all safe independent work slots",
    "A matching L3 fingerprint has one full gate",
    "A later user message continues the active objective",
)
CODE_AS_HARNESS_POLICY_REQUIRED_TEXT = (
    "Maximize useful safe parallelism",
    "Evidence fingerprint",
    "later user message as steering for the active objective",
)
DEFAULT_VERIFICATION_TIER = "L2"
VerificationTier = Literal["L0", "L1", "L2", "L3"]
VERIFICATION_TIERS = ("L0", "L1", "L2", "L3")
CODE_AS_HARNESS_CAPABILITIES = (
    "maximize-safe-parallelism",
    "declared-impact-scope",
    "evidence-fingerprint-reuse",
    "append-without-objective-cancellation",
)

_SKILL_PATHS = {
    "codex": (
        "CODEX_WORKBENCH_CODEX_SKILL_PATH",
        ".codex/skills/code-as-harness/SKILL.md",
    ),
    "claude-code": (
        "CODEX_WORKBENCH_CLAUDE_SKILL_PATH",
        ".claude/skills/code-as-harness/SKILL.md",
    ),
}
_POLICY_PATHS = {
    "codex": ("CODEX_WORKBENCH_CODEX_POLICY_PATH", ".codex/AGENTS.md"),
    "claude-code": ("CODEX_WORKBENCH_CLAUDE_POLICY_PATH", ".claude/CLAUDE.md"),
}
_ARCHIFY_PATHS = {
    "codex": (
        "CODEX_WORKBENCH_CODEX_ARCHIFY_PATH",
        ".codex/skills/archify",
    ),
    "claude-code": (
        "CODEX_WORKBENCH_CLAUDE_ARCHIFY_PATH",
        ".claude/skills/archify",
    ),
}


def governance_identity(contract: Mapping[str, Any]) -> tuple[str, VerificationTier]:
    profile = contract.get("governance_profile", CODE_AS_HARNESS_PROFILE)
    tier = contract.get("verification_tier", DEFAULT_VERIFICATION_TIER)
    if profile != CODE_AS_HARNESS_PROFILE:
        raise ValueError(f"unsupported governance profile: {profile!r}")
    if tier not in VERIFICATION_TIERS:
        raise ValueError(f"unsupported verification tier: {tier!r}")
    return profile, tier  # type: ignore[return-value]


def governance_receipt_fields(contract: Mapping[str, Any]) -> dict[str, str]:
    profile, tier = governance_identity(contract)
    return {"governance_profile": profile, "verification_tier": tier}


def governance_directive(contract: Mapping[str, Any]) -> str:
    profile, tier = governance_identity(contract)
    required_evidence = {
        "L0": "Inspect the relevant source or diff; do not run tests by default.",
        "L1": "Run one focused check that exercises the changed behavior.",
        "L2": "Run affected tests plus the relevant type, lint, build, or quick-governance check.",
        "L3": "Run the project-mandated full gate once after the worktree is stable, then collect runtime evidence.",
    }[tier]
    return (
        f"Governance profile: {profile}. Verification tier: {tier}.\n"
        "Define the observable acceptance boundary and affected-path envelope before editing. Stay inside the "
        "declared scope; give every node only the read/write scopes it needs. Maximize useful safe parallelism "
        "up to the coordinator capacity: do not serialize independent nodes, and never overlap conflicting access. "
        "Run a check only when its failure would change the next action. Reuse passing verification evidence only "
        "when its source, configuration, relevant dependency closure, runtime, scopes, steering, governance profile, "
        "and tier have the same Evidence fingerprint. For L3, do not repeat the full gate for that same fingerprint. "
        "A later user message is appended steering for the active objective; preserve its objective and scope unless "
        "an explicit task-control action pauses, cancels, or replaces it. "
        f"Required evidence: {required_evidence} "
        "Worker completion is not acceptance. Report the acceptance claim, checks actually run, covered and "
        "uncovered scope, residual risk, and full-gate status. Partial evidence must not be reported as accepted."
    )


def _executable_status(name: str, environment: Mapping[str, str]) -> dict[str, object]:
    variable = "CODEX_WORKBENCH_CODEX" if name == "codex" else "CODEX_WORKBENCH_CLAUDE"
    configured = environment.get(variable, name)
    expanded = Path(configured).expanduser()
    if expanded.is_absolute() or "/" in configured:
        candidate = expanded
    else:
        discovered = shutil.which(configured)
        candidate = Path(discovered) if discovered else None
    executable = bool(candidate and candidate.is_file() and os.access(candidate, os.X_OK))
    status: dict[str, object] = {
        "executor": name,
        "environment_variable": variable,
        "configured_binary": configured,
        "resolved_binary": str(candidate) if candidate else None,
        "executable": executable,
        "authentication_checked": False,
        "model_called": False,
    }
    if name == "codex":
        companion = candidate.resolve().with_name("codex-code-mode-host") if candidate else None
        companion_executable = bool(
            companion and companion.is_file() and os.access(companion, os.X_OK)
        )
        status.update(
            {
                "companion_required": True,
                "companion_path": str(companion) if companion else None,
                "companion_executable": companion_executable,
                "ready": executable and companion_executable,
            }
        )
    else:
        status["ready"] = executable
    return status


def _configured_path(
    environment: Mapping[str, str],
    variable: str,
    relative_default: str,
) -> Path:
    configured = environment.get(variable)
    if configured:
        return Path(configured).expanduser()
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    return home / relative_default


def _read_text(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None
    if not os.access(path, os.R_OK):
        return None, "not-readable"
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n"), None
    except UnicodeDecodeError:
        return None, "invalid-utf8"
    except OSError as error:
        return None, str(error)


def _frontmatter_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _parse_skill_document(text: str) -> tuple[dict[str, Any] | None, str]:
    match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)\Z", text, re.DOTALL)
    if match is None:
        return None, ""
    frontmatter: dict[str, Any] = {}
    metadata: dict[str, str] | None = None
    for raw_line in match.group("frontmatter").splitlines():
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            return None, match.group("body")
        if indent == 0:
            metadata = {} if key == "metadata" and not value.strip() else None
            if key in frontmatter:
                return None, match.group("body")
            frontmatter[key] = metadata if metadata is not None else _frontmatter_scalar(value)
            continue
        if indent == 2 and metadata is not None and key not in metadata:
            metadata[key] = _frontmatter_scalar(value)
            continue
        return None, match.group("body")
    return frontmatter, match.group("body")


def _visible_markdown(text: str) -> str:
    if text.count("<!--") != text.count("-->"):
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _has_exact_line(text: str, expected: str) -> bool:
    return any(line.strip() == expected for line in text.splitlines())


def _skill_artifact_status(agent: str, environment: Mapping[str, str]) -> dict[str, object]:
    variable, relative_default = _SKILL_PATHS[agent]
    path = _configured_path(environment, variable, relative_default)
    text, error = _read_text(path)
    exists = path.is_file()
    readable = text is not None
    frontmatter, body = _parse_skill_document(text) if text is not None else (None, "")
    metadata = frontmatter.get("metadata") if frontmatter is not None else None
    visible_body = _visible_markdown(body)
    declared_name = bool(
        frontmatter is not None
        and frontmatter.get("name") == CODE_AS_HARNESS_SKILL_NAME
    )
    managed = bool(
        isinstance(metadata, dict)
        and metadata.get("codex_workbench_managed") == "true"
    )
    profile_declared = bool(
        isinstance(metadata, dict)
        and metadata.get("profile") == CODE_AS_HARNESS_PROFILE
    )
    artifact_kind_declared = bool(
        isinstance(metadata, dict)
        and metadata.get("artifact_kind") == CODE_AS_HARNESS_ARTIFACT_KIND
    )
    required_content_present = bool(
        visible_body
        and _has_exact_line(visible_body, "## Operating contract")
        and all(clause in visible_body for clause in CODE_AS_HARNESS_SKILL_REQUIRED_TEXT[1:])
    )
    return {
        "agent": agent,
        "environment_variable": variable,
        "expected_path": str(path),
        "exists": exists,
        "readable": readable,
        "frontmatter_valid": frontmatter is not None,
        "declared_name_matches": declared_name,
        "managed_marker_present": managed,
        "profile_declared": profile_declared,
        "artifact_kind_declared": artifact_kind_declared,
        "required_content_present": required_content_present,
        "expected_artifact_kind": CODE_AS_HARNESS_ARTIFACT_KIND,
        "installed": (
            readable
            and declared_name
            and managed
            and profile_declared
            and artifact_kind_declared
            and required_content_present
        ),
        "read_error": error,
    }


def _policy_status(agent: str, environment: Mapping[str, str]) -> dict[str, object]:
    variable, relative_default = _POLICY_PATHS[agent]
    path = _configured_path(environment, variable, relative_default)
    text, error = _read_text(path)
    start_matches = (
        list(re.finditer(rf"(?m)^{re.escape(CODE_AS_HARNESS_POLICY_START)}[ \t]*$", text))
        if text is not None
        else []
    )
    end_matches = (
        list(re.finditer(rf"(?m)^{re.escape(CODE_AS_HARNESS_POLICY_END)}[ \t]*$", text))
        if text is not None
        else []
    )
    start_count = len(start_matches)
    end_count = len(end_matches)
    block = ""
    block_present = False
    if start_count == end_count == 1 and start_matches[0].start() < end_matches[0].start():
        block = text[start_matches[0].end():end_matches[0].start()].strip("\n")
        block_present = True
    visible_block = _visible_markdown(block)
    profile_declared = bool(
        block_present
        and (
            _has_exact_line(
                visible_block,
                f"Profile: `{CODE_AS_HARNESS_PROFILE}`. Canonical skill: `{CODE_AS_HARNESS_SKILL_NAME}`.",
            )
            or _has_exact_line(visible_block, f"Profile: {CODE_AS_HARNESS_PROFILE}")
        )
    )
    target_agent_declared = bool(
        block_present
        and any(
            re.fullmatch(rf"(?:-\s*)?Target agent: `{re.escape(agent)}`\.", line.strip())
            for line in visible_block.splitlines()
        )
    )
    required_content_present = bool(
        block_present
        and visible_block
        and all(clause in visible_block for clause in CODE_AS_HARNESS_POLICY_REQUIRED_TEXT)
    )
    return {
        "agent": agent,
        "environment_variable": variable,
        "expected_path": str(path),
        "exists": path.is_file(),
        "readable": text is not None,
        "start_marker_count": start_count,
        "end_marker_count": end_count,
        "managed_block_present": block_present,
        "profile_declared": profile_declared,
        "target_agent_declared": target_agent_declared,
        "required_content_present": required_content_present,
        "installed": (
            block_present
            and profile_declared
            and target_agent_declared
            and required_content_present
        ),
        "read_error": error,
    }


def _archify_health(environment: Mapping[str, str]) -> dict[str, object]:
    """Inspect pinned Archify source/projection/install state without a CLI.

    This deliberately stays a filesystem-only health path: it neither logs in
    nor invokes Codex, Claude, Node, or an Archify renderer.
    """

    expected_identity = {
        "repository": ARCHIFY_REPOSITORY,
        "tag": ARCHIFY_TAG,
        "commit": ARCHIFY_COMMIT,
        "version": ARCHIFY_VERSION,
        "license": ARCHIFY_LICENSE,
    }
    vendor_root = default_vendor_root()
    vendor: dict[str, object] = {
        "ok": False,
        "path": str(vendor_root),
        "pinned_identity": expected_identity,
        "error": None,
    }
    try:
        vendor.update(verify_vendor(vendor_root))
    except ArchifyContractError as error:
        vendor["error"] = str(error)

    projection_path = repository_root() / "skills" / "archify" / "SKILL.md"
    projection: dict[str, object] = {
        "ok": False,
        "path": str(projection_path),
        "error": None,
    }
    try:
        projection.update(verify_skill_projection(vendor_root, projection_path))
    except ArchifyContractError as error:
        projection["error"] = str(error)

    installations = {
        agent: installed_archify_status(
            _configured_path(environment, variable, relative_default),
            agent,
        )
        for agent, (variable, relative_default) in _ARCHIFY_PATHS.items()
    }
    installations_ready = all(bool(entry.get("ok")) for entry in installations.values())
    return {
        "ok": bool(vendor["ok"]) and bool(projection["ok"]) and installations_ready,
        "vendor": vendor,
        "projection": projection,
        "installations": installations,
        "pinned_identity": expected_identity,
        "health_probe": "filesystem-only",
        "authentication_checked": False,
        "model_called": False,
    }


def code_as_harness_health(
    config: Any,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Report filesystem artifacts and static Workbench wiring without using either CLI."""

    selected_environment = os.environ if environment is None else environment
    deployment_role = str(getattr(config, "deployment_role", "client"))
    device = "macmini" if deployment_role == "authority" else "macbook"
    executors = {
        name: _executable_status(name, selected_environment)
        for name in ("codex", "claude")
    }
    skill_artifacts = {
        agent: _skill_artifact_status(agent, selected_environment)
        for agent in _SKILL_PATHS
    }
    policies = {
        agent: _policy_status(agent, selected_environment)
        for agent in _POLICY_PATHS
    }
    archify = _archify_health(selected_environment)
    from .executors import managed_harness_static_wiring

    managed_injection = managed_harness_static_wiring()
    binaries_ready = all(bool(entry["ready"]) for entry in executors.values())
    skills_ready = all(bool(entry["installed"]) for entry in skill_artifacts.values())
    policies_ready = all(bool(entry["installed"]) for entry in policies.values())
    wiring_ready = all(
        bool(entry["static_wiring_verified"])
        for entry in managed_injection.values()
    )
    archify_ready = bool(archify["ok"])
    return {
        "ok": binaries_ready and skills_ready and policies_ready and wiring_ready and archify_ready,
        "profile": CODE_AS_HARNESS_PROFILE,
        "device": device,
        "deployment_role": deployment_role,
        "execution_path": "local-authority" if deployment_role == "authority" else "mcp-to-authority",
        "executors": executors,
        "skill_artifacts": skill_artifacts,
        "global_policies": policies,
        "workbench_managed_injection": {
            "status": "compatible-managed-capability" if wiring_ready else "wiring-not-ready",
            "runtime_execution_observed": False,
            "executors": managed_injection,
        },
        "archify": archify,
        "readiness": {
            "executor_binaries": binaries_ready,
            "canonical_skill_artifacts": skills_ready,
            "managed_global_policies": policies_ready,
            "workbench_static_injection": wiring_ready,
            "archify_pinned_vendor_projection_and_installations": archify_ready,
        },
        "capabilities": list(CODE_AS_HARNESS_CAPABILITIES),
        "max_safe_parallelism": int(getattr(config, "max_workers", 0)),
        "health_probe": "filesystem-and-static-wiring",
        "authentication_checked": False,
        "model_called": False,
    }


def governance_status() -> dict[str, object]:
    from .research import RESEARCH_POLICY_VERSION, RESEARCH_SKILL_NAME

    return {
        "profile": CODE_AS_HARNESS_PROFILE,
        "default_verification_tier": DEFAULT_VERIFICATION_TIER,
        "enforced": True,
        "execution_location": "authority",
        "capabilities": list(CODE_AS_HARNESS_CAPABILITIES),
        "research_policy": RESEARCH_POLICY_VERSION,
        "research_skill": RESEARCH_SKILL_NAME,
    }
