#!/usr/bin/env python3
"""Install the pinned, offline OpenSquilla V4 Phase 3 advisor for Workbench.

This installer intentionally copies only the local advisor subset.  It never
installs the OpenSquilla gateway, starts a service, downloads a wheel, or uses
provider credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from codex_workbench.squilla_advisor import (  # noqa: E402
    UPSTREAM_REVISION,
    SquillaAdvisor,
    SquillaAdvisorRequest,
)


UPSTREAM_BUNDLE_RELATIVE = Path(
    "src/opensquilla/squilla_router/models/v4.2_phase3_inference"
)
EXPECTED_MANIFEST_FILE_COUNT = 18
MINIMAL_DEPENDENCIES = (
    "numpy",
    "lightgbm",
    "joblib",
    "scikit-learn",
    "onnxruntime",
    "tokenizers",
    "structlog",
    "PyYAML",
)
REQUIREMENTS_FILENAME = "requirements-native-pinned.txt"
_VALID_SHA256 = re.compile(r"[0-9a-f]{64}")
_PINNED_REQUIREMENT = re.compile(r"([A-Za-z0-9_.-]+)==([^ ;]+)")


class SquillaInstallerError(RuntimeError):
    """The local advisor installation could not reach its activation boundary."""


class InstallPlan:
    """Validated, no-write inputs for one OpenSquilla advisor installation."""

    def __init__(
        self,
        *,
        home: Path,
        source_root: Path,
        bundle_dir: Path,
        wheelhouse: Path,
        python: Path,
        pinned_manifest: bytes,
    ) -> None:
        self.home = home
        self.source_root = source_root
        self.bundle_dir = bundle_dir
        self.wheelhouse = wheelhouse
        self.python = python
        self.pinned_manifest = pinned_manifest

    @property
    def install_root(self) -> Path:
        return self.home / "advisors" / "opensquilla"

    @property
    def config_file(self) -> Path:
        return self.home / "config.json"

    @property
    def requirements_file(self) -> Path:
        return self.wheelhouse / REQUIREMENTS_FILENAME


def _absolute_existing_directory(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise SquillaInstallerError(f"{label} is unavailable: {path}") from error
    if not path.is_dir():
        raise SquillaInstallerError(f"{label} must be an existing directory: {path}")
    return path


def _absolute_existing_file(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise SquillaInstallerError(f"{label} is unavailable: {path}") from error
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SquillaInstallerError(f"{label} must be an executable file: {path}")
    return path


def _run_command(command: Sequence[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise SquillaInstallerError(f"{label} could not start: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or f"exit {completed.returncode}"
        raise SquillaInstallerError(f"{label} failed: {detail}")
    return completed


def _validate_python(python: Path) -> None:
    completed = _run_command(
        (
            str(python),
            "-c",
            "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
        ),
        label="Python version check",
    )
    match = re.fullmatch(r"([0-9]+)[.]([0-9]+)", completed.stdout.strip())
    if match is None or (int(match.group(1)), int(match.group(2))) < (3, 12):
        observed = completed.stdout.strip() or "unknown"
        raise SquillaInstallerError(
            f"--python must be Python 3.12 or newer; observed {observed!r}"
        )


def _git_head(source_root: Path, *, label: str) -> str:
    completed = _run_command(
        ("git", "-C", str(source_root), "rev-parse", "HEAD"),
        label=f"{label} Git HEAD check",
    )
    observed = completed.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", observed):
        raise SquillaInstallerError(f"{label} Git HEAD is not a full commit id")
    return observed


def _read_manifest(path: Path, *, label: str) -> tuple[bytes, list[Mapping[str, object]]]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SquillaInstallerError(f"{label} manifest is unavailable: {path}") from error
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SquillaInstallerError(f"{label} manifest is invalid JSON: {path}") from error
    if not isinstance(decoded, Mapping):
        raise SquillaInstallerError(f"{label} manifest must be a JSON object: {path}")
    files = decoded.get("files")
    if (
        decoded.get("schema_version") != 1
        or decoded.get("bundle") != UPSTREAM_BUNDLE_RELATIVE.as_posix()
        or not isinstance(files, list)
        or len(files) != EXPECTED_MANIFEST_FILE_COUNT
    ):
        raise SquillaInstallerError(
            f"{label} manifest must be the pinned {EXPECTED_MANIFEST_FILE_COUNT}-file V4 bundle"
        )
    entries: list[Mapping[str, object]] = []
    seen_paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, Mapping):
            raise SquillaInstallerError(f"{label} manifest has a non-object file entry")
        relative = entry.get("path")
        size = entry.get("size_bytes")
        sha256 = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen_paths
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or _VALID_SHA256.fullmatch(sha256) is None
        ):
            raise SquillaInstallerError(f"{label} manifest has an invalid file entry")
        seen_paths.add(relative)
        entries.append(entry)
    return raw, entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bundle(
    bundle_dir: Path,
    pinned_manifest: bytes,
    *,
    verify_assets: bool,
) -> None:
    manifest = bundle_dir / "artifact_manifest.json"
    raw, entries = _read_manifest(manifest, label="OpenSquilla bundle")
    if raw != pinned_manifest:
        raise SquillaInstallerError(
            "OpenSquilla bundle manifest does not exactly match the pinned source manifest"
        )
    if not (bundle_dir / "runtime_src").is_dir():
        raise SquillaInstallerError("OpenSquilla bundle is missing runtime_src")
    if not verify_assets:
        return
    for entry in entries:
        relative = Path(str(entry["path"]))
        asset = bundle_dir / relative
        if not asset.is_file():
            raise SquillaInstallerError(f"OpenSquilla bundle asset is missing: {relative}")
        if asset.stat().st_size != entry["size_bytes"]:
            raise SquillaInstallerError(f"OpenSquilla bundle asset size mismatch: {relative}")
        if _sha256(asset) != entry["sha256"]:
            raise SquillaInstallerError(f"OpenSquilla bundle asset SHA-256 mismatch: {relative}")


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_wheelhouse(wheelhouse: Path) -> None:
    requirements_file = wheelhouse / REQUIREMENTS_FILENAME
    if not requirements_file.is_file():
        raise SquillaInstallerError(
            f"wheelhouse is missing {REQUIREMENTS_FILENAME}: {requirements_file}"
        )
    try:
        raw_requirements = [
            line.strip()
            for line in requirements_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except (OSError, UnicodeError) as error:
        raise SquillaInstallerError(
            f"wheelhouse requirements are unreadable: {requirements_file}"
        ) from error
    matches = [_PINNED_REQUIREMENT.fullmatch(line) for line in raw_requirements]
    if any(match is None for match in matches):
        raise SquillaInstallerError(
            "wheelhouse requirements must contain only pinned local package==version entries"
        )
    requirement_names = {
        _normalized_distribution(match.group(1))
        for match in matches
        if match is not None
    }
    wheel_names = {
        _normalized_distribution(path.name.split("-", 1)[0])
        for path in wheelhouse.iterdir()
        if path.is_file() and path.suffix == ".whl" and "-" in path.name
    }
    missing = [
        dependency
        for dependency in MINIMAL_DEPENDENCIES
        if _normalized_distribution(dependency) not in requirement_names
        or _normalized_distribution(dependency) not in wheel_names
    ]
    if missing:
        raise SquillaInstallerError(
            "wheelhouse requirements or local wheels are missing: " + ", ".join(missing)
        )
    unresolved = sorted(requirement_names - wheel_names)
    if unresolved:
        raise SquillaInstallerError(
            "wheelhouse is missing local wheels required by its pinned requirements: "
            + ", ".join(unresolved)
        )


def _read_config(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if not path.is_file():
        raise SquillaInstallerError(f"Workbench config is not a file: {path}")
    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SquillaInstallerError(f"Workbench config is invalid: {path}") from error
    if not isinstance(raw, dict):
        raise SquillaInstallerError(f"Workbench config must be a JSON object: {path}")
    existing = raw.get("squilla_advisor")
    if existing is not None and not isinstance(existing, dict):
        raise SquillaInstallerError("squilla_advisor config must be a JSON object")
    return raw, raw_bytes


def _merged_config(raw: Mapping[str, Any], install_root: Path) -> dict[str, Any]:
    merged = dict(raw)
    existing = raw.get("squilla_advisor")
    if existing is not None and not isinstance(existing, Mapping):
        raise SquillaInstallerError("squilla_advisor config must be a JSON object")
    advisor = dict(existing or {})
    advisor.update(
        {
            "enabled": True,
            "runtime_python": str(install_root / "venv" / "bin" / "python"),
            "source_root": str(install_root / "source"),
            "bundle_dir": str(install_root / "bundle"),
            "timeout_seconds": 45.0,
        }
    )
    merged["squilla_advisor"] = advisor
    return merged


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.write-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _write_config(path: Path, raw: Mapping[str, Any]) -> None:
    content = (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, content)
    path.chmod(0o600)


def _restore_config(path: Path, original: bytes | None) -> None:
    current = path.read_bytes() if path.is_file() else None
    if current == original:
        return
    if original is None:
        if path.exists():
            path.unlink()
        return
    _atomic_write_bytes(path, original)
    path.chmod(0o600)


def _remove_install_directory(path: Path) -> None:
    if path.is_symlink():
        raise SquillaInstallerError(f"installer-owned directory became a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise SquillaInstallerError(f"installer-owned path is not a directory: {path}")
        shutil.rmtree(path)


def _build_venv(plan: InstallPlan, runtime_python: Path) -> None:
    _run_command(
        (str(plan.python), "-m", "venv", str(runtime_python.parent.parent)),
        label="OpenSquilla virtual environment creation",
    )
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        raise SquillaInstallerError(
            f"OpenSquilla virtual environment did not create its Python runtime: {runtime_python}"
        )


def _install_dependencies(plan: InstallPlan, runtime_python: Path) -> None:
    _run_command(
        (
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(plan.wheelhouse),
            "-r",
            str(plan.requirements_file),
        ),
        label="offline OpenSquilla dependency installation",
    )


def _run_smoke(install_root: Path) -> dict[str, object]:
    """Exercise one keyless local batch before its config becomes active."""

    advisor = SquillaAdvisor(
        runtime_python=install_root / "venv" / "bin" / "python",
        source_root=install_root / "source",
        bundle_dir=install_root / "bundle",
        timeout_seconds=45.0,
    )
    results = advisor.advise_batch(
        [
            SquillaAdvisorRequest(
                request_id="opensquilla-install-smoke",
                prompt="Classify this local advisory install smoke request.",
            )
        ]
    )
    if len(results) != 1 or results[0].status != "available":
        diagnostic = results[0].diagnostic if len(results) == 1 else "invalid_batch_result"
        raise SquillaInstallerError(f"OpenSquilla advisor smoke failed: {diagnostic}")
    if results[0].demand_tier not in {"c0", "c1", "c2", "c3"}:
        raise SquillaInstallerError("OpenSquilla advisor smoke returned an invalid demand tier")
    if results[0].source.get("observed_source_revision") != UPSTREAM_REVISION:
        raise SquillaInstallerError("OpenSquilla advisor smoke did not verify the pinned source HEAD")
    receipt = results[0].to_receipt()
    if not isinstance(receipt, Mapping):
        raise SquillaInstallerError("OpenSquilla advisor smoke did not return a receipt object")
    return dict(receipt)


def _write_installation_receipt(install_root: Path, receipt: Mapping[str, object]) -> None:
    """Persist the prompt-free native result before enabling it in config."""

    content = (json.dumps(dict(receipt), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    receipt_path = install_root / "installation-receipt.json"
    _atomic_write_bytes(receipt_path, content)
    receipt_path.chmod(0o600)


def _sibling_backup(path: Path) -> Path:
    backup = path.with_name(f".{path.name}.previous-{uuid.uuid4().hex}")
    if backup.exists() or backup.is_symlink():
        raise SquillaInstallerError(f"OpenSquilla backup path is occupied: {backup}")
    return backup


def preflight_install(
    *,
    home: str | Path,
    source_root: str | Path,
    bundle_dir: str | Path,
    wheelhouse: str | Path,
    python: str | Path,
    verify_bundle_assets: bool,
) -> InstallPlan:
    """Validate all explicit inputs without creating any installation paths."""

    resolved_home = _absolute_existing_directory(home, label="--home")
    if not os.access(resolved_home, os.W_OK | os.X_OK):
        raise SquillaInstallerError(f"--home is not writable: {resolved_home}")
    resolved_source = _absolute_existing_directory(source_root, label="--source-root")
    resolved_bundle = _absolute_existing_directory(bundle_dir, label="--bundle-dir")
    resolved_wheelhouse = _absolute_existing_directory(wheelhouse, label="--wheelhouse")
    resolved_python = _absolute_existing_file(python, label="--python")
    _validate_python(resolved_python)

    observed = _git_head(resolved_source, label="OpenSquilla source")
    if observed != UPSTREAM_REVISION:
        raise SquillaInstallerError(
            "OpenSquilla source HEAD does not match the required pin: "
            f"expected {UPSTREAM_REVISION}, observed {observed}"
        )
    strategy = resolved_source / "src/opensquilla/squilla_router/v4_phase3.py"
    if not strategy.is_file():
        raise SquillaInstallerError(f"OpenSquilla source strategy is missing: {strategy}")
    source_manifest = resolved_source / UPSTREAM_BUNDLE_RELATIVE / "artifact_manifest.json"
    pinned_manifest, _ = _read_manifest(source_manifest, label="pinned OpenSquilla source")
    _validate_bundle(
        resolved_bundle,
        pinned_manifest,
        verify_assets=verify_bundle_assets,
    )
    _validate_wheelhouse(resolved_wheelhouse)
    return InstallPlan(
        home=resolved_home,
        source_root=resolved_source,
        bundle_dir=resolved_bundle,
        wheelhouse=resolved_wheelhouse,
        python=resolved_python,
        pinned_manifest=pinned_manifest,
    )


def _rollback_install(
    *,
    install_root: Path,
    previous: Path | None,
    config_file: Path,
    original_config: bytes | None,
) -> list[str]:
    """Restore the prior directory and exact config bytes after a failed stage."""

    errors: list[str] = []
    try:
        _restore_config(config_file, original_config)
    except Exception as error:  # rollback is the explicitly required recovery boundary
        errors.append(f"config: {error}")
    try:
        _remove_install_directory(install_root)
    except Exception as error:  # rollback is the explicitly required recovery boundary
        errors.append(f"new install: {error}")
    if previous is not None and previous.exists():
        try:
            previous.rename(install_root)
        except Exception as error:  # rollback is the explicitly required recovery boundary
            errors.append(f"previous install: {error}")
    return errors


def install(plan: InstallPlan) -> dict[str, object]:
    """Build at the durable target, smoke it, then atomically activate config."""

    raw_config, original_config = _read_config(plan.config_file)
    install_root = plan.install_root
    if install_root.exists() and not install_root.is_dir():
        raise SquillaInstallerError(f"OpenSquilla installation root is not a directory: {install_root}")
    install_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous: Path | None = None
    try:
        if install_root.exists():
            previous = _sibling_backup(install_root)
            install_root.rename(previous)
        install_root.mkdir(mode=0o700)

        installed_source = install_root / "source"
        _run_command(
            ("git", "clone", "--no-hardlinks", str(plan.source_root), str(installed_source)),
            label="local OpenSquilla source clone",
        )
        if not (installed_source / ".git").exists():
            raise SquillaInstallerError("local OpenSquilla clone did not retain Git metadata")
        observed = _git_head(installed_source, label="installed OpenSquilla source")
        if observed != UPSTREAM_REVISION:
            raise SquillaInstallerError(
                "installed OpenSquilla source HEAD does not match the required pin: "
                f"expected {UPSTREAM_REVISION}, observed {observed}"
            )

        installed_bundle = install_root / "bundle"
        shutil.copytree(plan.bundle_dir, installed_bundle)
        # Hash only the copied final bundle.  This is the one installation-time
        # pass that proves every pinned size and SHA before activation.
        _validate_bundle(installed_bundle, plan.pinned_manifest, verify_assets=True)

        runtime_python = install_root / "venv" / "bin" / "python"
        # A Python venv embeds absolute paths, so it is intentionally created
        # at its final durable location rather than moved from a staging tree.
        _build_venv(plan, runtime_python)
        _install_dependencies(plan, runtime_python)
        native_receipt = _run_smoke(install_root)
        _write_installation_receipt(install_root, native_receipt)
        _write_config(plan.config_file, _merged_config(raw_config, install_root))
    except Exception as error:  # required transaction recovery for stage/smoke/config failure
        rollback_errors = _rollback_install(
            install_root=install_root,
            previous=previous,
            config_file=plan.config_file,
            original_config=original_config,
        )
        detail = str(error)
        if rollback_errors:
            detail += "; rollback incomplete: " + "; ".join(rollback_errors)
        raise SquillaInstallerError(f"OpenSquilla advisor installation failed: {detail}") from error

    return _receipt(
        "installed",
        plan,
        previous_backup=str(previous) if previous is not None else None,
        native_receipt=native_receipt,
    )


def _receipt(
    status: str,
    plan: InstallPlan,
    *,
    previous_backup: str | None = None,
    native_receipt: Mapping[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": status,
        "install_root": str(plan.install_root),
        "source_revision": UPSTREAM_REVISION,
        "manifest_validation": "passed",
        "restart_required": {
            "long_running_mcp_or_service": True,
            "one_shot_cli": "next_invocation",
        },
    }
    if previous_backup is not None:
        result["previous_install_backup"] = previous_backup
    if native_receipt is not None:
        result["native_receipt"] = dict(native_receipt)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the pinned offline OpenSquilla V4 Phase 3 advisor."
    )
    parser.add_argument("--home", required=True, help="existing Codex Workbench state root")
    parser.add_argument("--source-root", required=True, help="local Git checkout at the required pin")
    parser.add_argument("--bundle-dir", required=True, help="local V4 Phase 3 artifact bundle")
    parser.add_argument("--wheelhouse", required=True, help="local wheels for the isolated venv")
    parser.add_argument("--python", required=True, help="Python 3.12+ used to create the venv")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate all inputs and the supplied bundle without writing, installing, or smoking",
    )
    args = parser.parse_args(argv)
    try:
        plan = preflight_install(
            home=args.home,
            source_root=args.source_root,
            bundle_dir=args.bundle_dir,
            wheelhouse=args.wheelhouse,
            python=args.python,
            verify_bundle_assets=args.dry_run,
        )
        receipt = _receipt("dry_run", plan) if args.dry_run else install(plan)
    except SquillaInstallerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
