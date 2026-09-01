from __future__ import annotations

from typing import Any, Literal, Mapping


CODE_AS_HARNESS_PROFILE = "code-as-harness/v1"
DEFAULT_VERIFICATION_TIER = "L2"
VerificationTier = Literal["L0", "L1", "L2", "L3"]
VERIFICATION_TIERS = ("L0", "L1", "L2", "L3")


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
        "Define the observable acceptance boundary before editing. Stay inside the declared scope. "
        "Run a check only when its failure would change the next action, and reuse passing evidence while "
        "its source, configuration, dependencies, and relevant environment are unchanged. "
        f"Required evidence: {required_evidence} "
        "Worker completion is not acceptance. Report the acceptance claim, checks actually run, covered and "
        "uncovered scope, residual risk, and full-gate status. Partial evidence must not be reported as accepted."
    )


def governance_status() -> dict[str, object]:
    return {
        "profile": CODE_AS_HARNESS_PROFILE,
        "default_verification_tier": DEFAULT_VERIFICATION_TIER,
        "enforced": True,
        "execution_location": "authority",
    }
