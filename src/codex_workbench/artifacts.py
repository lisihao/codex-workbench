from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re


_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


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
        try:
            algorithm, digest, suffix = ref.split(":", 2)
        except ValueError as error:
            raise ValueError("unsupported artifact ref") from error
        if (
            algorithm != "sha256"
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or _SUFFIX.fullmatch(suffix) is None
        ):
            raise ValueError("unsupported artifact ref")
        return self.root / "sha256" / digest[:2] / f"{digest}.{suffix}"
