from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets


@dataclass(frozen=True)
class WorkbenchConfig:
    state_root: Path
    host: str = "127.0.0.1"
    port: int = 8766
    max_workers: int = 4

    @property
    def database(self) -> Path:
        return self.state_root / "state.sqlite"

    @property
    def token_file(self) -> Path:
        return self.state_root / "control.token"

    @property
    def config_file(self) -> Path:
        return self.state_root / "config.json"

    def initialize(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root.chmod(0o700)
        if not self.token_file.exists():
            self.token_file.write_text(secrets.token_urlsafe(32) + "\n")
            self.token_file.chmod(0o600)
        if not self.config_file.exists():
            self.config_file.write_text(
                json.dumps(
                    {"host": self.host, "port": self.port, "max_workers": self.max_workers},
                    indent=2,
                )
                + "\n"
            )
            self.config_file.chmod(0o600)

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
            return cls(
                state_root=root,
                host=raw.get("host", "127.0.0.1"),
                port=int(raw.get("port", 8766)),
                max_workers=int(raw.get("max_workers", 4)),
            )
        return cls(state_root=root)

