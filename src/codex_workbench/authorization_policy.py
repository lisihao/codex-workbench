"""Canonical, dependency-free wording for Workbench continuous authorization.

Installers load this module directly from a checked-out source tree, so keep it
free of Workbench imports and make each rule a single display-safe sentence.
The rules guide planning and operating behavior; durable controllers still
decide whether a particular action can execute.
"""

WORKBENCH_CONTINUOUS_AUTHORIZATION_RULES = (
    "Within an already-authorized objective, continue implementation, verification, fixes, and "
    "delivery already authorized by that objective when the action stays in scope, low risk, "
    "reversible, and free of severe adverse side effects.",
    "Do not pause for routine confirmation or status reports.",
    "A new version, attempt, or subtask within that same bounded objective is not an "
    "independent reason to request approval.",
    "Reassess authorization when risk or scope changes; a backup does not by itself make "
    "an action low risk or reversible.",
    "Pause for a high-risk, irreversible, out-of-scope action, severe adverse side effects, "
    "a real missing permission, or a material decision that is still missing.",
    "Do not bypass platform review or fabricate approval.",
    "When an external effect is unknown, reconcile the original effect or receipt before "
    "retrying or continuing that effect; do not replay it.",
    "An explicit pause or cancellation takes precedence.",
)


__all__ = ["WORKBENCH_CONTINUOUS_AUTHORIZATION_RULES"]
