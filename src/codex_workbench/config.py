from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import socket

from .authority import authority_machine_id


@dataclass(frozen=True)
class WorkbenchConfig:
    state_root: Path
    host: str = "127.0.0.1"
    port: int = 8766
    max_workers: int = 4
    # Spark is a logical worker lane inside the one global executor.  ``None``
    # deliberately means "use the portable default", rather than reserving
    # idle threads for Spark.
    spark_workers: int | None = None
    deployment_role: str = "client"
    authority_host: str | None = None
    authority_machine_id: str | None = None
    quota_snapshot_file: Path | None = None
    quota_refresh_seconds: int = 60
    radar_enabled: bool = True
    radar_state_root: Path | None = None
    radar_authorization_file: Path | None = None
    radar_refresh_seconds: int = 6 * 60 * 60
    radar_stale_after_seconds: int = 7 * 24 * 60 * 60
    radar_expire_after_seconds: int = 31 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be positive")
        if self.spark_workers is not None and not (
            0 <= self.spark_workers <= self.max_workers
        ):
            raise ValueError("spark_workers must be between 0 and max_workers")
        if self.radar_refresh_seconds <= 0:
            raise ValueError("radar_refresh_seconds must be positive")
        if self.radar_stale_after_seconds <= 0:
            raise ValueError("radar_stale_after_seconds must be positive")
        if self.radar_expire_after_seconds < self.radar_stale_after_seconds:
            raise ValueError(
                "radar_expire_after_seconds must be at least radar_stale_after_seconds"
            )

    @property
    def effective_spark_workers(self) -> int:
        """Return the dedicated Spark lane cap without reserving executor slots."""

        return min(4, self.max_workers) if self.spark_workers is None else self.spark_workers

    @property
    def effective_quota_snapshot_file(self) -> Path:
        return self.quota_snapshot_file or self.state_root / "claude-quota.json"

    @property
    def effective_radar_state_root(self) -> Path:
        return self.radar_state_root or self.state_root / "radar"

    @property
    def effective_radar_authorization_file(self) -> Path:
        return (
            self.radar_authorization_file
            or self.effective_radar_state_root / "authorization.json"
        )

    @property
    def database(self) -> Path:
        return self.state_root / "state.sqlite"

    @property
    def token_file(self) -> Path:
        return self.state_root / "control.token"

    @property
    def config_file(self) -> Path:
        return self.state_root / "config.json"

    @property
    def install_manifest(self) -> Path:
        return self.state_root / "app" / "install-manifest.json"

    def initialize(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root.chmod(0o700)
        if not self.token_file.exists():
            self.token_file.write_text(secrets.token_urlsafe(32) + "\n")
            self.token_file.chmod(0o600)
        raw = {}
        if self.config_file.exists():
            raw = json.loads(self.config_file.read_text())
        existing_radar = raw.get("radar", {})
        if existing_radar is None:
            existing_radar = {}
        if not isinstance(existing_radar, dict):
            raise ValueError("radar config must be an object")
        desired = {
            **raw,
            "host": self.host,
            "port": self.port,
            "max_workers": self.max_workers,
            "spark_workers": self.spark_workers,
            "deployment_role": self.deployment_role,
            "authority_host": self.authority_host,
            "authority_machine_id": self.authority_machine_id,
            "quota_snapshot_file": str(self.effective_quota_snapshot_file),
            "quota_refresh_seconds": self.quota_refresh_seconds,
            "radar": {
                **existing_radar,
                "enabled": self.radar_enabled,
                "state_root": str(self.effective_radar_state_root),
                "authorization_receipt": str(
                    self.effective_radar_authorization_file
                ),
                "refresh_interval_seconds": self.radar_refresh_seconds,
                "stale_after_seconds": self.radar_stale_after_seconds,
                "expire_after_seconds": self.radar_expire_after_seconds,
                "authority_only": True,
            },
        }
        if desired != raw:
            self.config_file.write_text(json.dumps(desired, indent=2) + "\n")
            self.config_file.chmod(0o600)

    def assert_authority(self) -> None:
        if self.deployment_role != "authority":
            raise RuntimeError("this Workbench installation is a client and cannot start a local writer")
        if self.authority_host != socket.gethostname():
            raise RuntimeError(
                f"authority is pinned to {self.authority_host!r}, not {socket.gethostname()!r}"
            )
        if not self.authority_machine_id:
            raise RuntimeError(
                "authority machine ID is missing; explicitly run init --authority to bind this ledger"
            )
        current_machine_id = authority_machine_id()
        if self.authority_machine_id != current_machine_id:
            raise RuntimeError(
                "authority ledger is bound to a different machine ID: "
                f"{self.authority_machine_id!r} != {current_machine_id!r}"
            )

    def token(self) -> str:
        return self.token_file.read_text().strip()

    @classmethod
    def load(cls, state_root: Path | None = None) -> "WorkbenchConfig":
        root = state_root or Path(
            os.environ.get(
                "CODEX_WORKBENCH_HOME",
                "~/Library/Application Support/Codex Workbench",
            )
        ).expanduser()
        config_file = root / "config.json"
        if config_file.exists():
            raw = json.loads(config_file.read_text())
            radar = raw.get("radar", {})
            if radar is None:
                radar = {}
            if not isinstance(radar, dict):
                raise ValueError("radar config must be an object")
            inferred_authority = bool(os.environ.get("CODEX_WORKBENCH_PROCESS_HOME"))
            role = raw.get("deployment_role") or (
                "authority" if inferred_authority else "client"
            )
            authority_host = raw.get("authority_host")
            if role == "authority" and not authority_host:
                authority_host = socket.gethostname()
            return cls(
                state_root=root,
                host=raw.get("host", "127.0.0.1"),
                port=int(raw.get("port", 8766)),
                max_workers=int(raw.get("max_workers", 4)),
                spark_workers=(
                    int(raw["spark_workers"])
                    if raw.get("spark_workers") is not None
                    else None
                ),
                deployment_role=str(role),
                authority_host=authority_host,
                authority_machine_id=raw.get("authority_machine_id"),
                quota_snapshot_file=Path(raw["quota_snapshot_file"]).expanduser()
                if raw.get("quota_snapshot_file")
                else root / "claude-quota.json",
                quota_refresh_seconds=int(raw.get("quota_refresh_seconds", 60)),
                radar_enabled=bool(radar.get("enabled", True)),
                radar_state_root=Path(radar["state_root"]).expanduser()
                if radar.get("state_root")
                else root / "radar",
                radar_authorization_file=Path(
                    radar["authorization_receipt"]
                ).expanduser()
                if radar.get("authorization_receipt")
                else root / "radar" / "authorization.json",
                radar_refresh_seconds=int(
                    radar.get("refresh_interval_seconds", 6 * 60 * 60)
                ),
                radar_stale_after_seconds=int(
                    radar.get("stale_after_seconds", 7 * 24 * 60 * 60)
                ),
                radar_expire_after_seconds=int(
                    radar.get("expire_after_seconds", 31 * 24 * 60 * 60)
                ),
            )
        inferred_authority = bool(os.environ.get("CODEX_WORKBENCH_PROCESS_HOME"))
        return cls(
            state_root=root,
            deployment_role="authority" if inferred_authority else "client",
            authority_host=socket.gethostname() if inferred_authority else None,
            authority_machine_id=None,
            quota_snapshot_file=root / "claude-quota.json",
        )
