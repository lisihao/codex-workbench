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
    deployment_role: str = "client"
    authority_host: str | None = None
    authority_machine_id: str | None = None
    quota_snapshot_file: Path | None = None
    quota_refresh_seconds: int = 60

    @property
    def effective_quota_snapshot_file(self) -> Path:
        return self.quota_snapshot_file or self.state_root / "claude-quota.json"

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
        desired = {
            **raw,
            "host": self.host,
            "port": self.port,
            "max_workers": self.max_workers,
            "deployment_role": self.deployment_role,
            "authority_host": self.authority_host,
            "authority_machine_id": self.authority_machine_id,
            "quota_snapshot_file": str(self.effective_quota_snapshot_file),
            "quota_refresh_seconds": self.quota_refresh_seconds,
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
                deployment_role=str(role),
                authority_host=authority_host,
                authority_machine_id=raw.get("authority_machine_id"),
                quota_snapshot_file=Path(raw["quota_snapshot_file"]).expanduser()
                if raw.get("quota_snapshot_file")
                else root / "claude-quota.json",
                quota_refresh_seconds=int(raw.get("quota_refresh_seconds", 60)),
            )
        inferred_authority = bool(os.environ.get("CODEX_WORKBENCH_PROCESS_HOME"))
        return cls(
            state_root=root,
            deployment_role="authority" if inferred_authority else "client",
            authority_host=socket.gethostname() if inferred_authority else None,
            authority_machine_id=None,
            quota_snapshot_file=root / "claude-quota.json",
        )
