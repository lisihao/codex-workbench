from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from .governance import governance_receipt_fields
from .model import canonical_hash
from .worktrees import normalize_scope


def reusable_evidence_key(
    contract: dict,
    spec: dict,
    worktree: Path | None,
    steering: tuple[str, ...] = (),
) -> str | None:
    if worktree is None or spec.get("executor") == "fixture":
        return None
    if not spec.get("verifier") and (spec.get("executor") != "deterministic" or spec.get("write_scopes")):
        return None
    scopes = tuple(dict.fromkeys((*spec.get("read_scopes", ()), *spec.get("write_scopes", ()))))
    if not scopes:
        return None
    return evidence_fingerprint(contract, spec, worktree, steering, scopes=scopes)


def evidence_fingerprint(
    contract: dict,
    spec: dict,
    worktree: Path,
    steering: tuple[str, ...] = (),
    *,
    scopes: tuple[str, ...] | None = None,
) -> str:
    """Identify reusable verifier evidence, including the governing full-gate tier."""

    selected_scopes = scopes or tuple(
        dict.fromkeys((*spec.get("read_scopes", ()), *spec.get("write_scopes", ())))
    )
    if not selected_scopes:
        raise ValueError("Evidence fingerprint requires at least one declared scope")
    executor = spec["executor"]
    return canonical_hash(
        {
            "kind": "verified-evidence-v3",
            "contract": {
                "repository": contract["repository"],
                "base_sha": contract["base_sha"],
                "allowed_scope": contract.get("allowed_scope", ()),
                "forbidden_scope": contract.get("forbidden_scope", ()),
                "required_artifacts": contract.get("required_artifacts", ()),
            },
            "objective": contract["objective"],
            "acceptance_commands": contract.get("acceptance_commands", ()),
            "governance": governance_receipt_fields(contract),
            "spec": {
                key: value
                for key, value in spec.items()
                if key not in {"task_id", "node_id", "ordinal", "depends_on"}
            },
            "steering": steering,
            "scope_fingerprint": _scope_fingerprint(worktree, selected_scopes),
            "runtime": _runtime_identity(executor),
        }
    )


def _scope_fingerprint(worktree: Path, scopes: tuple[str, ...]) -> str:
    normalized = [normalize_scope(scope) for scope in scopes]
    paths = ["." if scope == "." else scope for scope in normalized]
    result = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *paths,
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    digest = sha256()
    for raw in sorted(item for item in result.stdout.split(b"\0") if item):
        relative = raw.decode(errors="surrogateescape")
        target = worktree / relative
        digest.update(raw)
        digest.update(b"\0")
        if target.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(target).encode(errors="surrogateescape"))
        elif target.is_file():
            digest.update(target.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_identity(executor: str) -> dict[str, str]:
    identity = {
        "platform": platform.platform(),
        "python": sys.version,
        "executor": executor,
    }
    binary_name = (
        os.environ.get("CODEX_WORKBENCH_CODEX", "codex")
        if executor == "codex"
        else os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude")
        if executor == "claude"
        else None
    )
    if binary_name:
        binary = shutil.which(binary_name) if "/" not in binary_name else binary_name
        if binary:
            result = subprocess.run(
                [binary, "--version"],
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            identity["product"] = (result.stdout or result.stderr).strip()
    return identity
