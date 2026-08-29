from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import zipfile


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

    def verify(self, ref: str) -> Path:
        path = self.path_for(ref)
        if not path.is_file():
            raise ValueError(f"artifact does not exist: {ref}")
        expected = ref.split(":", 2)[1]
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"artifact hash mismatch: {ref}")
        return path


def presentation_format(path: Path) -> str | None:
    """Identify supported presentation artifacts by content, never by extension."""
    data = path.read_bytes()
    if (
        data.startswith(b"%PDF-")
        and b"%%EOF" in data[-1024:]
        and (b"/Type /Catalog" in data or b"xref" in data)
    ):
        return "pdf"
    if (
        len(data) >= 512
        and data.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
        and "PowerPoint Document".encode("utf-16le") in data
    ):
        return "ppt"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return None
        if "[Content_Types].xml" in names and "ppt/presentation.xml" in names:
            return "pptx"
    return None
