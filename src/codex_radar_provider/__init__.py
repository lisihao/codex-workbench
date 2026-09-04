"""Portable, credential-free Codex Radar snapshot provider.

The package has no dependency on Codex Workbench or DSH.  Consumers only need
the durable JSON snapshot produced by :class:`RadarRegistry`.
"""

from .provider import ATTRIBUTION, RadarProviderError, RadarRegistry, validate_radar_snapshot

__all__ = ["ATTRIBUTION", "RadarProviderError", "RadarRegistry", "validate_radar_snapshot"]
