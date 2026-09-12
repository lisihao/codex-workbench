"""Canonical Workbench/DSH project-boundary wording.

This module intentionally has no Workbench package imports so installers can
load the same text from the checked-in source tree before installation.
"""

WORKBENCH_PROJECT_IDENTITY = (
    "Workbench is a Codex-fronted general distributed development-agent tool "
    "spanning a MacBook cockpit and Mac mini authority. Its intended goals are "
    "bounded planning and decomposition, evidence-governed model and reasoning "
    "selection using Radar, AI Frontier, OpenSquilla, and local evidence, "
    "effective parallelism, verification-led delivery, and deterministic recovery. "
    "These are target capabilities, not a claim that every capability is implemented.\n"
    "DSH is independent: it is one of Workbench's development workloads and deliverables, not a "
    "Workbench subsystem. Repository, worktree, PR, version, release, deployment, "
    "ledger, and acceptance ownership remain separate by project. A Workbench fix "
    "does not count as DSH feature completion. Report Workbench outcomes, DSH outcomes, "
    "and their dependencies separately. After Workbench recovery is accepted, "
    "resume the original DSH node while preserving accepted ancestors; do not recreate "
    "or rerun those accepted ancestors. Using Workbench to develop DSH is not integration; "
    "a port or integration is a separate scoped project."
)

__all__ = ["WORKBENCH_PROJECT_IDENTITY"]
