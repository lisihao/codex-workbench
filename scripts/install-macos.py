#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import uuid

LABEL = "com.lisihao.codex-workbench"
QUOTA_LABEL = "com.lisihao.codex-workbench-quota"
DEFAULT_TAILSCALE_HTTPS_PORT = 10443
DEFAULT_TAILSCALE_NATIVE_SSH_PORT = 10022
DEFAULT_AUTHORITY_MAX_WORKERS = 8
RESEARCH_SKILL_REQUIRED_FILES = (
    "SKILL.md",
    "UrlVerificationProtocol.md",
    "Workflows/StandardResearch.md",
    "Workflows/DeepInvestigation.md",
)


def relaunch_with_supported_runtime() -> None:
    if sys.version_info >= (3, 11):
        return
    selector = Path(__file__).resolve().with_name("python-runtime")
    if not selector.is_file():
        raise SystemExit(
            "Codex Workbench requires Python 3.11 or newer; "
            f"runtime selector is missing: {selector}"
        )
    print(
        "Current Python is incompatible; relaunching with the Workbench Python runtime selector.",
        file=sys.stderr,
    )
    os.execv(str(selector), [str(selector), str(Path(__file__).resolve()), *sys.argv[1:]])


relaunch_with_supported_runtime()


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def macos_machine_id() -> str:
    result = run(
        "/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice",
        check=False,
    )
    import re
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', result.stdout)
    if result.returncode != 0 or match is None:
        raise SystemExit("macOS IOPlatformUUID is unavailable; refusing to bind authority")
    return "darwin:ioplatformuuid:" + match.group(1).lower()


def network_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def absolute_path(path: Path) -> Path:
    """Make a path absolute without resolving away symlink evidence."""

    path = path.expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    """Reject direct, broken, and ancestor symlinks before any write."""

    path = absolute_path(path)
    current = path
    while True:
        if current.is_symlink():
            raise SystemExit(f"{label} has a symlink ancestor: {current}")
        if current.parent == current:
            return
        current = current.parent


def assert_directory_target(path: Path, label: str) -> None:
    path = absolute_path(path)
    assert_no_symlink_ancestors(path, label=label)
    if path.exists() and not path.is_dir():
        raise SystemExit(f"{label} is not a directory: {path}")
    ancestor = path
    while not ancestor.exists():
        # is_symlink() catches a broken link which exists() deliberately does not.
        if ancestor.is_symlink():
            raise SystemExit(f"{label} has a symlink ancestor: {ancestor}")
        ancestor = ancestor.parent
    assert_no_symlink_ancestors(ancestor, label=label)
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise SystemExit(f"{label} parent is not writable: {ancestor}")
    if path.exists() and not os.access(path, os.W_OK | os.X_OK):
        raise SystemExit(f"{label} is not writable: {path}")


def assert_file_target(path: Path, label: str) -> None:
    path = absolute_path(path)
    assert_no_symlink_ancestors(path, label=label)
    if path.exists() and not path.is_file():
        raise SystemExit(f"{label} is not a file: {path}")
    assert_directory_target(path.parent, f"{label} parent")
    if path.exists() and not os.access(path, os.W_OK):
        raise SystemExit(f"{label} is not writable: {path}")


def remove_path(path: Path) -> None:
    """Remove only the named path; never follow a symlink during rollback."""

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        if not path.is_dir():
            raise SystemExit(f"rollback target is unsupported: {path}")
        shutil.rmtree(path)


class InstallTransaction:
    """Small reversible transaction for local files touched by the authority installer."""

    def __init__(self, parent: Path) -> None:
        parent = absolute_path(parent)
        assert_no_symlink_ancestors(parent, label="installer transaction parent")
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            raise SystemExit(f"installer transaction parent is not a directory: {parent}")
        self.root = Path(tempfile.mkdtemp(prefix=".codex-workbench-install-", dir=parent))
        self.entries: list[tuple[Path, Path, bool, str | None, bool]] = []
        self.created_directories: list[Path] = []
        self.preserved_app: Path | None = None
        self.preserved_app_root: Path | None = None
        self.application_root: Path | None = None

    def snapshot(self, path: Path, label: str, *, allow_symlink: bool = False) -> None:
        path = absolute_path(path)
        if allow_symlink and path.is_symlink():
            assert_no_symlink_ancestors(path.parent, label=label)
        else:
            assert_no_symlink_ancestors(path, label=label)
        existed = path.exists() or path.is_symlink()
        backup = self.root / f"entry-{len(self.entries)}"
        link_target: str | None = None
        if existed:
            if path.is_symlink():
                link_target = os.readlink(path)
            elif path.is_dir():
                shutil.copytree(path, backup, symlinks=True)
            elif path.is_file():
                shutil.copy2(path, backup)
            else:
                raise SystemExit(f"{label} is not a supported filesystem target: {path}")
        self.entries.append((path, backup, existed, link_target, allow_symlink))

    def track_created_directory(self, path: Path) -> None:
        self.created_directories.append(absolute_path(path))

    def preserve_existing_app(self, app_root: Path, state_root: Path) -> None:
        app_root = absolute_path(app_root)
        state_root = absolute_path(state_root)
        self.application_root = app_root
        if not app_root.exists():
            return
        backup = state_root / f"previous-app-{uuid.uuid4().hex}"
        if backup.exists() or backup.is_symlink():
            raise SystemExit(f"application backup path is unexpectedly occupied: {backup}")
        app_root.rename(backup)
        self.preserved_app = backup
        self.preserved_app_root = app_root

    def rollback(self) -> None:
        errors: list[str] = []
        for path, backup, existed, link_target, allow_symlink in reversed(self.entries):
            try:
                if allow_symlink and path.is_symlink():
                    assert_no_symlink_ancestors(path.parent, label="rollback target")
                else:
                    assert_no_symlink_ancestors(path, label="rollback target")
                if path.exists() or path.is_symlink():
                    remove_path(path)
                if not existed:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if link_target is not None:
                    path.symlink_to(link_target)
                elif backup.is_dir():
                    shutil.copytree(backup, path, symlinks=True)
                else:
                    shutil.copy2(backup, path)
            except Exception as error:  # pragma: no cover - catastrophic filesystem fault
                errors.append(f"{path}: {error}")
        if self.preserved_app is not None and self.preserved_app.exists():
            try:
                app_root = self.preserved_app_root
                if app_root is None:
                    raise SystemExit("preserved application root is missing")
                if app_root.exists() or app_root.is_symlink():
                    remove_path(app_root)
                self.preserved_app.rename(app_root)
            except Exception as error:  # pragma: no cover - catastrophic filesystem fault
                errors.append(f"{self.preserved_app}: {error}")
        elif self.application_root is not None:
            try:
                if self.application_root.exists() or self.application_root.is_symlink():
                    remove_path(self.application_root)
            except Exception as error:  # pragma: no cover - catastrophic filesystem fault
                errors.append(f"{self.application_root}: {error}")
        for path in sorted(self.created_directories, key=lambda value: len(value.parts), reverse=True):
            try:
                if not path.exists() or not path.is_dir():
                    continue
                for child in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                    if child.is_dir() and not child.is_symlink() and not any(child.iterdir()):
                        child.rmdir()
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass
        self.cleanup()
        if errors:
            raise SystemExit("installer rollback failed: " + "; ".join(errors))

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def commit(self) -> None:
        self.cleanup()


def configure_tailscale_serve(
    tailscale: str,
    socket_path: str,
    *,
    https_port: int,
    native_ssh_port: int,
) -> None:
    socket_argument = f"--socket={socket_path}"
    run(
        tailscale,
        socket_argument,
        "serve",
        "--yes",
        "--bg",
        f"--https={https_port}",
        "http://127.0.0.1:8766",
    )
    run(
        tailscale,
        socket_argument,
        "serve",
        "--yes",
        "--bg",
        f"--tcp={native_ssh_port}",
        "tcp://127.0.0.1:22",
    )


def validate_research_skill_source(source: Path) -> Path:
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise SystemExit(f"Research skill source is not a directory: {source}")
    missing = [name for name in RESEARCH_SKILL_REQUIRED_FILES if not (source / name).is_file()]
    if missing:
        raise SystemExit(
            "Research skill is incomplete; missing required files: " + ", ".join(missing)
        )
    return source


def install_research_skill(source: Path, process_home: Path) -> Path:
    source = validate_research_skill_source(source)
    destination = process_home / ".agents" / "skills" / "research"
    assert_no_symlink_ancestors(destination, label="Research skill destination")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise SystemExit(f"Research skill destination must not be a symlink: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise SystemExit(f"Research skill destination is not a directory: {destination}")
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def install_code_as_harness(source: Path) -> None:
    installer = source / "scripts" / "install-code-as-harness.py"
    if not installer.is_file():
        raise SystemExit(f"Code-as-Harness installer is missing: {installer}")
    result = run(
        sys.executable,
        str(installer),
        "--source",
        str(source),
        "--adopt-compatible",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown installer failure"
        raise SystemExit(f"Code-as-Harness installation failed: {detail}")


def install_archify(source: Path) -> None:
    installer = source / "scripts" / "install-archify.py"
    if not installer.is_file():
        raise SystemExit(f"Archify installer is missing: {installer}")
    result = run(sys.executable, str(installer), "--source", str(source), check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown installer failure"
        raise SystemExit(f"Archify installation failed: {detail}")


def preflight_managed_agent_skills(source: Path) -> None:
    """Preflight both global agent projections before either installer writes."""

    harness_installer = source / "scripts" / "install-code-as-harness.py"
    archify_installer = source / "scripts" / "install-archify.py"
    if not harness_installer.is_file():
        raise SystemExit(f"Code-as-Harness installer is missing: {harness_installer}")
    if not archify_installer.is_file():
        raise SystemExit(f"Archify installer is missing: {archify_installer}")
    checks = (
        (harness_installer, ("--check", "--adopt-compatible"), "Code-as-Harness"),
        (archify_installer, ("--dry-run",), "Archify"),
    )
    for installer, check_flags, label in checks:
        result = run(
            sys.executable,
            str(installer),
            "--source",
            str(source),
            *check_flags,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown preflight failure"
            raise SystemExit(f"{label} preflight failed: {detail}")


def preflight_global_agent_targets(home: Path) -> None:
    """Check every global target before either managed Skill installer writes."""

    home = absolute_path(home)
    targets = (
        (home / ".codex" / "skills" / "code-as-harness" / "SKILL.md", "Codex Code-as-Harness skill"),
        (home / ".codex" / "AGENTS.md", "Codex policy"),
        (home / ".codex" / "skills" / "archify", "Codex Archify skill"),
        (home / ".claude" / "skills" / "code-as-harness" / "SKILL.md", "Claude Code-as-Harness skill"),
        (home / ".claude" / "CLAUDE.md", "Claude policy"),
        (home / ".claude" / "skills" / "archify", "Claude Archify skill"),
    )
    for path, label in targets:
        if path.name == "SKILL.md":
            assert_file_target(path, label)
        elif path.name.endswith(".md"):
            assert_file_target(path, label)
        else:
            assert_directory_target(path, label)


def preflight_authority_plists(
    source: Path,
    app_root: Path,
    state_root: Path,
    codex_binary: Path,
    codex_home: Path,
    process_home: Path,
    quota_snapshot_file: Path,
    claude_binary: Path | None,
) -> None:
    template = (source / "launchd" / f"{LABEL}.plist.in").read_text()
    rendered = (
        template.replace("__APP_ROOT__", str(app_root))
        .replace("__STATE_ROOT__", str(state_root))
        .replace("__CODEX_BINARY__", str(codex_binary))
        .replace("__CODEX_HOME__", str(codex_home))
        .replace("__PROCESS_HOME__", str(process_home))
        .replace("__QUOTA_SNAPSHOT_FILE__", str(quota_snapshot_file))
        .replace("__CLAUDE_BINARY__", str(claude_binary) if claude_binary else "")
    )
    plistlib.loads(rendered.encode())
    if claude_binary is not None:
        quota_template = (source / "launchd" / f"{QUOTA_LABEL}.plist.in").read_text()
        quota_rendered = (
            quota_template.replace("__APP_ROOT__", str(app_root))
            .replace("__STATE_ROOT__", str(state_root))
            .replace("__USER_HOME__", str(Path.home()))
            .replace("__CLAUDE_BINARY__", str(claude_binary))
            .replace("__QUOTA_SNAPSHOT_FILE__", str(quota_snapshot_file))
        )
        plistlib.loads(quota_rendered.encode())


def authority_max_workers(config: dict[str, object]) -> int:
    return max(DEFAULT_AUTHORITY_MAX_WORKERS, int(config.get("max_workers", 4)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--state-root", default="~/Library/Application Support/Codex Workbench")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run all local preflight checks and print the plan without writing or activating anything",
    )
    parser.add_argument("--codex-binary", default="~/.codex/packages/standalone/current/codex")
    parser.add_argument(
        "--claude-binary",
        help="absolute or resolvable Claude CLI to use for the passive quota producer",
    )
    parser.add_argument("--quota-snapshot-file")
    parser.add_argument(
        "--research-skill-source",
        default="~/.agents/skills/research",
        help="complete Research skill copied into the isolated Sol planner home",
    )
    parser.add_argument(
        "--tailscale-socket",
        help="active userspace tailscaled LocalAPI socket; when set, configure tailnet-only cockpit and native SSH Serve",
    )
    parser.add_argument(
        "--tailscale-https-port",
        type=network_port,
        default=DEFAULT_TAILSCALE_HTTPS_PORT,
    )
    parser.add_argument(
        "--tailscale-native-ssh-port",
        type=network_port,
        default=DEFAULT_TAILSCALE_NATIVE_SSH_PORT,
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"Workbench source is not a directory: {source}")
    state_root = absolute_path(Path(args.state_root))
    app_root = state_root / "app"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = launch_agents / f"{LABEL}.plist"
    quota_plist_path = launch_agents / f"{QUOTA_LABEL}.plist"
    logs = state_root / "logs"
    runtime_root = state_root / "runtime"
    codex_home = state_root / "codex-home"
    process_home = state_root / "codex-process-home"
    quota_snapshot_file = (
        absolute_path(Path(args.quota_snapshot_file))
        if args.quota_snapshot_file
        else state_root / "claude-quota.json"
    )
    assert_directory_target(state_root, "state root")
    assert_directory_target(logs, "log root")
    assert_directory_target(runtime_root, "runtime root")
    assert_directory_target(codex_home, "Codex home")
    assert_directory_target(process_home, "process home")
    assert_directory_target(launch_agents, "LaunchAgents root")
    assert_file_target(config_file := state_root / "config.json", "config file")
    assert_file_target(plist_path, "service LaunchAgent")
    assert_file_target(quota_plist_path, "quota LaunchAgent")
    assert_file_target(quota_snapshot_file, "quota snapshot")
    backup_root = state_root / "previous-app"
    if app_root.is_symlink():
        raise SystemExit(f"application root must not be a symlink: {app_root}")
    if backup_root.is_symlink():
        raise SystemExit(f"previous application backup must not be a symlink: {backup_root}")
    assert_no_symlink_ancestors(app_root, label="application root")
    assert_no_symlink_ancestors(backup_root, label="previous application backup")
    if app_root.exists():
        assert_directory_target(app_root, "application root")
    if backup_root.exists():
        assert_directory_target(backup_root, "previous application backup")
    auth_link = codex_home / "auth.json"
    assert_no_symlink_ancestors(auth_link.parent, label="Codex auth link")
    if auth_link.is_symlink():
        try:
            auth_destination = (auth_link.parent / os.readlink(auth_link)).resolve(strict=True)
        except (OSError, RuntimeError):
            raise SystemExit(f"Codex auth link is broken; refusing to replace: {auth_link}")
        if auth_destination != (Path.home() / ".codex" / "auth.json").resolve(strict=True):
            raise SystemExit(f"Codex auth link points outside the subscription auth file: {auth_link}")
    elif auth_link.exists():
        raise SystemExit(f"refusing to replace non-symlink auth file: {auth_link}")
    config_raw = json.loads(config_file.read_text()) if config_file.exists() else {}
    if not isinstance(config_raw, dict):
        raise SystemExit(f"config file must contain a JSON object: {config_file}")
    authority_machine_id = macos_machine_id()
    research_source = validate_research_skill_source(Path(args.research_skill_source))
    research_destination = process_home / ".agents" / "skills" / "research"
    assert_no_symlink_ancestors(research_destination, label="Research skill destination")
    assert_directory_target(research_destination.parent, "Research skill parent")
    if research_destination.exists():
        assert_directory_target(research_destination, "Research skill destination")
    codex_source = Path(args.codex_binary).expanduser().resolve(strict=True)
    if not codex_source.is_file() or not os.access(codex_source, os.X_OK):
        raise SystemExit(f"Codex CLI is not executable: {codex_source}")
    codex_host_source = codex_source.with_name("codex-code-mode-host")
    if not codex_host_source.is_file() or not os.access(codex_host_source, os.X_OK):
        raise SystemExit(
            f"Codex workspace tool host is missing or not executable: {codex_host_source}"
        )
    auth_source = Path.home() / ".codex" / "auth.json"
    if not auth_source.is_file():
        raise SystemExit("Codex subscription auth is missing at ~/.codex/auth.json")
    authority_max_workers(config_raw)
    int(config_raw.get("quota_refresh_seconds", 60))
    selected_claude = args.claude_binary if args.claude_binary is not None else shutil.which("claude")
    if selected_claude and "/" not in selected_claude:
        selected_claude = shutil.which(selected_claude)
        if args.claude_binary is not None and selected_claude is None:
            raise SystemExit(f"Claude CLI is not resolvable: {args.claude_binary}")
    claude_binary = None
    if selected_claude:
        candidate = Path(selected_claude).expanduser().resolve(strict=False)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            if args.claude_binary is not None:
                raise SystemExit(f"Claude CLI is not executable: {candidate}")
        else:
            claude_binary = candidate
    elif args.claude_binary is not None:
        raise SystemExit(f"Claude CLI is not resolvable: {args.claude_binary}")

    tailscale_socket = None
    tailscale = None
    if args.tailscale_socket:
        tailscale_path = Path(args.tailscale_socket).expanduser().resolve(strict=True)
        if not tailscale_path.is_socket():
            raise SystemExit(f"--tailscale-socket is not a Unix socket: {tailscale_path}")
        tailscale_socket = str(tailscale_path)
        tailscale = shutil.which("tailscale")
        if not tailscale:
            raise SystemExit("--tailscale-socket requires the tailscale CLI")
    runtime_selector = source / "scripts" / "python-runtime"
    if not runtime_selector.is_file() or not os.access(runtime_selector, os.X_OK):
        raise SystemExit(f"Workbench Python runtime selector is missing or not executable: {runtime_selector}")
    commit = run("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
    if not commit:
        raise SystemExit(f"Workbench source has no commit identity: {source}")
    tag_result = run("git", "-C", str(source), "describe", "--tags", "--exact-match", check=False)
    tag = tag_result.stdout.strip() if tag_result.returncode == 0 else None
    version_line = next(
        line
        for line in (source / "src" / "codex_workbench" / "__init__.py").read_text().splitlines()
        if line.startswith("__version__")
    )
    version = version_line.split("=", 1)[1].strip().strip('"')
    runtime_binary = runtime_root / "codex"
    assert_file_target(runtime_binary, "runtime Codex executable")
    assert_file_target(runtime_root / "codex-code-mode-host", "runtime Codex workspace tool host")
    preflight_authority_plists(
        source,
        app_root,
        state_root,
        runtime_binary,
        codex_home,
        process_home,
        quota_snapshot_file,
        claude_binary,
    )
    preflight_global_agent_targets(Path.home())
    preflight_managed_agent_skills(source)
    archify_lock = json.loads(
        (source / "vendor" / "archify" / "SOURCE-LOCK.json").read_text(encoding="utf-8")
    )
    if args.dry_run:
        print("Codex Workbench authority dry-run: no filesystem writes, launchctl, SSH, or MCP changes")
        print(f"plan: source={source}")
        print(f"plan: state_root={state_root}")
        print(f"plan: application={app_root}")
        print(f"plan: Codex runtime={runtime_root}")
        print(f"plan: Research skill={research_destination}")
        print(f"plan: service={plist_path}")
        print(f"plan: quota={quota_plist_path} ({'enabled' if claude_binary else 'skipped: Claude CLI unavailable'})")
        print("plan: managed Code-as-Harness and Archify projections for Codex and Claude Code")
        return 0

    domain = f"gui/{run('id', '-u').stdout.strip()}"
    service_labels = (LABEL, QUOTA_LABEL)
    service_was_loaded = {
        label: run("launchctl", "print", f"{domain}/{label}", check=False).returncode == 0
        for label in service_labels
    }
    transaction = InstallTransaction(state_root)
    for directory in (state_root, logs, runtime_root, codex_home, process_home, launch_agents):
        if not directory.exists():
            transaction.track_created_directory(directory)
    for path, label in (
        (config_file, "config file"),
        (runtime_root, "runtime root"),
        (process_home / ".agents" / "skills" / "research", "Research skill"),
        (auth_link, "Codex auth link"),
        (plist_path, "service LaunchAgent"),
        (quota_plist_path, "quota LaunchAgent"),
        (quota_snapshot_file, "quota snapshot"),
        (Path.home() / ".codex" / "skills" / "code-as-harness", "Codex Code-as-Harness skill"),
        (Path.home() / ".codex" / "AGENTS.md", "Codex policy"),
        (Path.home() / ".codex" / "skills" / "archify", "Codex Archify skill"),
        (Path.home() / ".claude" / "skills" / "code-as-harness", "Claude Code-as-Harness skill"),
        (Path.home() / ".claude" / "CLAUDE.md", "Claude policy"),
        (Path.home() / ".claude" / "skills" / "archify", "Claude Archify skill"),
    ):
        transaction.snapshot(path, label, allow_symlink=path == auth_link)
    services_touched = False
    try:
        install_code_as_harness(source)
        install_archify(source)

        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        process_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        research_skill = install_research_skill(research_source, process_home)
        config_raw.update(
            {
                "deployment_role": "authority",
                "authority_host": __import__("socket").gethostname(),
                "authority_machine_id": authority_machine_id,
                "max_workers": authority_max_workers(config_raw),
                "quota_snapshot_file": str(quota_snapshot_file),
                "quota_refresh_seconds": min(
                    int(config_raw.get("quota_refresh_seconds", 60)),
                    60,
                ),
            }
        )
        config_file.write_text(json.dumps(config_raw, indent=2) + "\n")
        config_file.chmod(0o600)
        codex_binary = runtime_binary
        codex_host = runtime_root / "codex-code-mode-host"
        shutil.copy2(codex_source, codex_binary)
        shutil.copy2(codex_host_source, codex_host)
        codex_binary.chmod(0o755)
        codex_host.chmod(0o755)
        codex_version = run(str(codex_binary), "--version").stdout.strip()
        if not auth_link.is_symlink():
            if auth_link.exists():
                raise SystemExit(f"refusing to replace non-symlink auth file: {auth_link}")
            auth_link.symlink_to(auth_source)
        transaction.preserve_existing_app(app_root, state_root)
        shutil.copytree(source, app_root, ignore=shutil.ignore_patterns(".git", "__pycache__", ".workbench"))
        manifest = app_root / "install-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": version,
                    "commit": commit,
                    "tag": tag,
                    "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "codex_version": codex_version,
                    "research_skill": {
                        "name": "Research",
                        "policy": "research-skill/v2",
                        "source": str(research_source),
                        "managed_path": str(research_skill),
                    },
                    "code_as_harness": {
                        "profile": "code-as-harness/v1",
                        "artifact": "workbench-canonical-compatible-skill",
                        "skills": {
                            "codex": str(Path.home() / ".codex" / "skills" / "code-as-harness" / "SKILL.md"),
                            "claude-code": str(Path.home() / ".claude" / "skills" / "code-as-harness" / "SKILL.md"),
                        },
                        "policies": {
                            "codex": str(Path.home() / ".codex" / "AGENTS.md"),
                            "claude-code": str(Path.home() / ".claude" / "CLAUDE.md"),
                        },
                    },
                    "archify": {
                        "repository": archify_lock["repository"],
                        "tag": archify_lock["tag"],
                        "version": archify_lock["version"],
                        "commit": archify_lock["commit"],
                        "license": archify_lock["license"],
                        "core": str(app_root / "vendor" / "archify"),
                        "skills": {
                            "codex": str(Path.home() / ".codex" / "skills" / "archify" / "SKILL.md"),
                            "claude-code": str(Path.home() / ".claude" / "skills" / "archify" / "SKILL.md"),
                        },
                    },
                },
                indent=2,
            )
            + "\n"
        )
        manifest.chmod(0o600)
        bin_dir = app_root / "bin"
        bin_dir.mkdir(exist_ok=True)
        wrapper = bin_dir / "codex-workbench"
        runtime_selector = app_root / "scripts" / "python-runtime"
        if not runtime_selector.is_file():
            raise SystemExit(f"Workbench Python runtime selector is missing: {runtime_selector}")
        wrapper.write_text(
            (
            "#!/bin/zsh\n"
            f"export PYTHONPATH={str(app_root / 'src')!r}\n"
            f"export CODEX_HOME={str(codex_home)!r}\n"
            f"export CODEX_WORKBENCH_PROCESS_HOME={str(process_home)!r}\n"
            f"export CODEX_WORKBENCH_CODEX={str(codex_binary)!r}\n"
            f"export CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE={str(quota_snapshot_file)!r}\n"
            )
            + (f"export CODEX_WORKBENCH_CLAUDE={str(claude_binary)!r}\n" if claude_binary else "")
            + f"exec {str(runtime_selector)!r} -m codex_workbench \"$@\"\n"
        )
        wrapper.chmod(0o755)

        template = (source / "launchd" / f"{LABEL}.plist.in").read_text()
        rendered = (
            template.replace("__APP_ROOT__", str(app_root))
            .replace("__STATE_ROOT__", str(state_root))
            .replace("__CODEX_BINARY__", str(codex_binary))
            .replace("__CODEX_HOME__", str(codex_home))
            .replace("__PROCESS_HOME__", str(process_home))
            .replace("__QUOTA_SNAPSHOT_FILE__", str(quota_snapshot_file))
            .replace("__CLAUDE_BINARY__", str(claude_binary) if claude_binary else "")
        )
        plistlib.loads(rendered.encode())
        launch_agents.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(rendered)
        plist_path.chmod(0o600)
        quota_rendered: str | None = None
        if claude_binary is not None:
            quota_template = (source / "launchd" / f"{QUOTA_LABEL}.plist.in").read_text()
            quota_rendered = (
                quota_template.replace("__APP_ROOT__", str(app_root))
                .replace("__STATE_ROOT__", str(state_root))
                .replace("__USER_HOME__", str(Path.home()))
                .replace("__CLAUDE_BINARY__", str(claude_binary))
                .replace("__QUOTA_SNAPSHOT_FILE__", str(quota_snapshot_file))
            )
            plistlib.loads(quota_rendered.encode())
            quota_plist_path.write_text(quota_rendered)
            quota_plist_path.chmod(0o600)

        services_touched = True
        run("launchctl", "bootout", domain, str(plist_path), check=False)
        run("launchctl", "bootstrap", domain, str(plist_path))
        run("launchctl", "enable", f"{domain}/{LABEL}")
        run("launchctl", "kickstart", "-k", f"{domain}/{LABEL}")
        if claude_binary is not None and quota_rendered is not None:
            run("launchctl", "bootout", domain, str(quota_plist_path), check=False)
            run("launchctl", "bootstrap", domain, str(quota_plist_path))
            run("launchctl", "enable", f"{domain}/{QUOTA_LABEL}")
            run("launchctl", "kickstart", "-k", f"{domain}/{QUOTA_LABEL}")
        else:
            run("launchctl", "bootout", domain, str(quota_plist_path), check=False)
            if quota_plist_path.exists() or quota_plist_path.is_symlink():
                remove_path(quota_plist_path)
        if tailscale_socket:
            configure_tailscale_serve(
                tailscale,
                tailscale_socket,
                https_port=args.tailscale_https_port,
                native_ssh_port=args.tailscale_native_ssh_port,
            )
    except BaseException as error:
        rollback_errors: list[str] = []
        if services_touched:
            for label, path in ((LABEL, plist_path), (QUOTA_LABEL, quota_plist_path)):
                try:
                    run("launchctl", "bootout", domain, str(path), check=False)
                except BaseException as rollback_error:
                    rollback_errors.append(f"{label} service: {rollback_error}")
        try:
            transaction.rollback()
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        if services_touched:
            for label, path in ((LABEL, plist_path), (QUOTA_LABEL, quota_plist_path)):
                if not service_was_loaded[label] or not path.is_file():
                    continue
                try:
                    run("launchctl", "bootstrap", domain, str(path))
                    run("launchctl", "enable", f"{domain}/{label}")
                except BaseException as rollback_error:
                    rollback_errors.append(f"{label} service restore: {rollback_error}")
        if rollback_errors:
            raise SystemExit(f"authority installation failed: {error}; rollback failed: {'; '.join(rollback_errors)}") from error
        raise
    else:
        transaction.commit()
    quota_status = f"installed {QUOTA_LABEL}" if claude_binary is not None else "skipped quota producer: Claude CLI unavailable"
    print(f"installed {LABEL} from {source} to {app_root}; {quota_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
