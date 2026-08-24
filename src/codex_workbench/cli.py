from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import __version__
from .api import WorkbenchHTTPServer
from .artifacts import ArtifactStore
from .config import WorkbenchConfig
from .executors import ClaudeExecutor, CodexExecutor
from .model import NodeSpec, QuotaSnapshot, TaskContract
from .planner import CodexPlanner, PlannerError
from .service import Coordinator
from .store import CommandConflictError, StateConflictError, WorkbenchStore


def _config(args: argparse.Namespace) -> WorkbenchConfig:
    root = Path(args.home).expanduser() if getattr(args, "home", None) else None
    config = WorkbenchConfig.load(root)
    config.initialize()
    return config


def _store(config: WorkbenchConfig) -> WorkbenchStore:
    store = WorkbenchStore(config.database)
    store.initialize()
    return store


def command_init(args: argparse.Namespace) -> int:
    config = _config(args)
    _store(config)
    print(json.dumps({"ok": True, "home": str(config.state_root), "version": __version__}))
    return 0


def command_serve(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.host or args.port or args.max_workers:
        config = WorkbenchConfig(
            state_root=config.state_root,
            host=args.host or config.host,
            port=args.port or config.port,
            max_workers=args.max_workers or config.max_workers,
        )
    store = _store(config)
    coordinator = Coordinator(store, config.state_root, max_workers=config.max_workers)
    recovered = coordinator.recover()
    coordinator_thread = threading.Thread(target=coordinator.run_forever, name="coordinator", daemon=True)
    coordinator_thread.start()
    server = WorkbenchHTTPServer(config, store)

    def stop(*_args) -> None:
        coordinator.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        json.dumps(
            {
                "ok": True,
                "version": __version__,
                "listen": f"http://{config.host}:{config.port}",
                "recovered_indeterminate": recovered,
            }
        ),
        flush=True,
    )
    server.serve_forever(poll_interval=0.5)
    coordinator.stop()
    coordinator_thread.join(timeout=30)
    server.server_close()
    return 0


def command_submit(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    raw = json.loads(Path(args.file).read_text())
    contract = TaskContract.from_dict(raw["contract"])
    nodes = [NodeSpec.from_dict(node) for node in raw["nodes"]]
    command_id = args.command_id or f"submit-{uuid.uuid4()}"
    try:
        task_id = store.create_task(contract, nodes, command_id)
        if args.queue:
            store.queue_task(task_id)
    except (ValueError, CommandConflictError, StateConflictError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    print(json.dumps({"ok": True, "task_id": task_id, "command_id": command_id}))
    return 0


def command_request(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    repository = Path(args.repository).expanduser().resolve(strict=True)
    base_sha = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    task_id = args.task_id or f"task-{uuid.uuid4().hex[:12]}"
    contract = TaskContract(
        task_id=task_id,
        repository=str(repository),
        base_sha=base_sha,
        objective=args.objective,
        allowed_scope=tuple(args.allowed_scope),
        forbidden_scope=tuple(args.forbidden_scope or ()),
        acceptance_commands=tuple(args.acceptance_command or ()),
        planner_model=args.planner_model,
        executor_model=args.executor_model,
        verifier_model=args.verifier_model,
        timeout_seconds=args.timeout,
        retry_limit=args.retry_limit,
        external_write_permission=args.allow_external_write,
        destructive_action_permission=False,
    )
    contract.validate()
    artifacts = ArtifactStore(config.state_root / "artifacts")
    quota = store.latest_quota()
    claude_ok, _ = ClaudeExecutor(
        artifacts,
        quota,
        os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude"),
    ).qualification("sonnet")
    planner = CodexPlanner(
        os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
        model=args.planner_model,
    )
    try:
        nodes = planner.compile(
            contract,
            claude_available=claude_ok,
            default_executor_model=args.executor_model,
            verifier_model=args.verifier_model,
        )
    except PlannerError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    command_id = args.command_id or f"request-{uuid.uuid4()}"
    store.create_task(contract, nodes, command_id)
    if args.queue:
        store.queue_task(task_id)
    print(
        json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "command_id": command_id,
                "base_sha": base_sha,
                "claude_dispatch_available": claude_ok,
                "nodes": [node.to_dict() for node in nodes],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_task(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    if args.action == "list":
        result = store.list_tasks()
    elif args.action == "get":
        result = store.get_task(args.task_id)
    elif args.action == "resolve":
        revision = store.resolve_indeterminate(
            args.task_id,
            args.node_id,
            args.resolution,
            expected_revision=args.expected_revision,
        )
        result = {"ok": True, "task_id": args.task_id, "revision": revision}
    else:
        task = store.get_task(args.task_id)
        if args.action in {"queue", "resume"}:
            revision = store.queue_task(args.task_id)
        elif args.action == "pause":
            revision = store.transition_task(
                args.task_id, "paused", expected_revision=task["state_revision"]
            )
        elif args.action == "cancel":
            revision = store.transition_task(
                args.task_id, "cancelled", expected_revision=task["state_revision"]
            )
        else:
            raise AssertionError(args.action)
        result = {"ok": True, "task_id": args.task_id, "revision": revision}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_events(args: argparse.Namespace) -> int:
    store = _store(_config(args))
    print(
        json.dumps(
            store.read_events(after=args.after, task_id=args.task_id),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_quota(args: argparse.Namespace) -> int:
    store = _store(_config(args))
    if args.quota_action == "show":
        snapshot = store.latest_quota()
        print(json.dumps(asdict(snapshot) if snapshot else None, ensure_ascii=False, indent=2))
        return 0
    snapshot = QuotaSnapshot(
        observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        auth_ok=args.auth_ok,
        auth_method=args.auth_method,
        five_hour_remaining=args.five_hour,
        weekly_all_remaining=args.weekly_all,
        weekly_sonnet_remaining=args.weekly_sonnet,
        weekly_fable_remaining=args.weekly_fable,
        source=args.source,
    )
    store.write_quota(snapshot)
    print(json.dumps({"ok": True, "snapshot": asdict(snapshot)}, ensure_ascii=False))
    return 0


def command_token(args: argparse.Namespace) -> int:
    config = _config(args)
    print(config.token())
    return 0


def _run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
            },
            check=False,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)


def command_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    codex_binary = os.environ.get("CODEX_WORKBENCH_CODEX", "codex")
    claude_binary = os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude")
    artifacts = ArtifactStore(config.state_root / "artifacts")
    codex_ok, codex_reason = CodexExecutor(artifacts, codex_binary).qualification()
    quota = store.latest_quota()
    claude_ok, claude_reason = ClaudeExecutor(artifacts, quota, claude_binary).qualification("sonnet")
    git_code, git_output = _run(["git", "--version"])
    report = {
        "ok": codex_ok and git_code == 0,
        "version": __version__,
        "home": str(config.state_root),
        "database": store.health(),
        "codex": {"ok": codex_ok, "reason": codex_reason, "binary": codex_binary},
        "claude": {
            "ok": claude_ok,
            "reason": claude_reason,
            "binary": claude_binary,
            "dispatch_enabled": claude_ok,
        },
        "git": {"ok": git_code == 0, "version": git_output},
        "api_key_environment_forwarded": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def command_fixture_demo(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    repository = Path(args.repository).expanduser().resolve()
    base_sha = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    task_id = args.task_id or f"fixture-{uuid.uuid4().hex[:8]}"
    contract = TaskContract(
        task_id=task_id,
        repository=str(repository),
        base_sha=base_sha,
        objective="Verify persistent DAG, parallel workers, scope admission, and independent acceptance",
        allowed_scope=("tests",),
        acceptance_commands=("python3 -m unittest discover -s tests",),
        executor_model="fixture",
        verifier_model="fixture",
    )
    nodes = [
        NodeSpec("worker-a", task_id, "parallel worker A", "fixture", "fixture", "worker A ok", write_scopes=("tests/a",), ordinal=1),
        NodeSpec("worker-b", task_id, "parallel worker B", "fixture", "fixture", "worker B ok", write_scopes=("tests/b",), ordinal=1),
        NodeSpec(
            "verify",
            task_id,
            "independent acceptance",
            "fixture",
            "fixture",
            "verifier accepted both workers",
            depends_on=("worker-a", "worker-b"),
            verifier=True,
            ordinal=2,
        ),
    ]
    store.create_task(contract, nodes, f"fixture-demo-{task_id}")
    store.queue_task(task_id)
    coordinator = Coordinator(store, config.state_root, max_workers=2, poll_seconds=0.05)
    thread = threading.Thread(target=coordinator.run_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = store.get_task(task_id)
        if task["state"] in {"accepted", "blocked", "needs_fix", "needs_approval"}:
            break
        time.sleep(0.05)
    coordinator.stop()
    thread.join(timeout=5)
    task = store.get_task(task_id)
    print(json.dumps(task, ensure_ascii=False, indent=2))
    return 0 if task["state"] == "accepted" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-workbench")
    parser.add_argument("--home", help="override persistent state directory")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.set_defaults(func=command_init)

    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--max-workers", type=int)
    serve.set_defaults(func=command_serve)

    submit = sub.add_parser("submit")
    submit.add_argument("file")
    submit.add_argument("--command-id")
    submit.add_argument("--queue", action="store_true")
    submit.set_defaults(func=command_submit)

    request = sub.add_parser("request", help="compile a natural-language goal with Codex Sol")
    request.add_argument("objective")
    request.add_argument("--repository", required=True)
    request.add_argument("--allowed-scope", action="append", required=True)
    request.add_argument("--forbidden-scope", action="append")
    request.add_argument("--acceptance-command", action="append")
    request.add_argument("--task-id")
    request.add_argument("--command-id")
    request.add_argument("--planner-model", default="gpt-5.6-sol")
    request.add_argument("--executor-model", default="gpt-5.6-luna")
    request.add_argument("--verifier-model", default="gpt-5.6-sol")
    request.add_argument("--timeout", type=int, default=3600)
    request.add_argument("--retry-limit", type=int, default=3)
    request.add_argument("--allow-external-write", action="store_true")
    request.add_argument("--queue", action="store_true")
    request.set_defaults(func=command_request)

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="action", required=True)
    task_sub.add_parser("list")
    for action in ("get", "queue", "resume", "pause", "cancel"):
        action_parser = task_sub.add_parser(action)
        action_parser.add_argument("task_id")
    resolve = task_sub.add_parser("resolve")
    resolve.add_argument("task_id")
    resolve.add_argument("node_id")
    resolve.add_argument("--resolution", choices=("retry", "fail", "cancel"), required=True)
    resolve.add_argument("--expected-revision", type=int, required=True)
    task.set_defaults(func=command_task)

    events = sub.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--task-id")
    events.set_defaults(func=command_events)

    quota = sub.add_parser("quota")
    quota_sub = quota.add_subparsers(dest="quota_action", required=True)
    quota_sub.add_parser("show")
    quota_set = quota_sub.add_parser("set")
    quota_set.add_argument("--auth-ok", action="store_true")
    quota_set.add_argument("--auth-method", default="none")
    quota_set.add_argument("--five-hour", type=float)
    quota_set.add_argument("--weekly-all", type=float)
    quota_set.add_argument("--weekly-sonnet", type=float)
    quota_set.add_argument("--weekly-fable", type=float)
    quota_set.add_argument("--source", default="manual")
    quota.set_defaults(func=command_quota)

    token = sub.add_parser("token")
    token.set_defaults(func=command_token)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=command_doctor)

    demo = sub.add_parser("fixture-demo")
    demo.add_argument("--repository", default=".")
    demo.add_argument("--task-id")
    demo.set_defaults(func=command_fixture_demo)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = args.func(args)
    except (KeyError, ValueError, StateConflictError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        code = 2
    raise SystemExit(code)
