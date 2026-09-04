"""Portable, credential-free Codex Radar snapshot provider.

The package has no dependency on Codex Workbench or DSH. SQLite is the
authoritative local store; consumers may use :class:`RadarRegistry` directly or
read its portable JSON compatibility projections.
"""

from .provider import (
    ATTRIBUTION,
    RadarProviderError,
    RadarRegistry,
    validate_radar_snapshot,
    write_personal_use_consent,
)

__all__ = [
    "ATTRIBUTION",
    "RadarProviderError",
    "RadarRegistry",
    "validate_radar_snapshot",
    "write_personal_use_consent",
]
