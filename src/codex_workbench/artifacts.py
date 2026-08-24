from __future__ import annotations

from hashlib import sha256
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root

    def put_bytes(self, data: bytes, suffix: str = "bin") -> str:
        digest = sha256(data).hexdigest()
        directory = self.root / "sha256" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / f"{digest}.{suffix}"
        if not target.exists():
            target.write_bytes(data)
            target.chmod(0o600)
        return f"sha256:{digest}:{suffix}"

    def put_text(self, text: str, suffix: str = "txt") -> str:
        return self.put_bytes(text.encode(), suffix)

    def path_for(self, ref: str) -> Path:
        algorithm, digest, suffix = ref.split(":", 2)
        if algorithm != "sha256" or len(digest) != 64:
            raise ValueError("unsupported artifact ref")
        return self.root / "sha256" / digest[:2] / f"{digest}.{suffix}"

