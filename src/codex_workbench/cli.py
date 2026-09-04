from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import __version__
from .acceptance import build_acceptance_report
from .api import WorkbenchHTTPServer
from .artifacts import ArtifactStore, presentation_format
from .authority import CoordinatorAuthorityError, CoordinatorAuthorityLease, authority_machine_id
from .capabilities import CapabilityCatalogError, CapabilityRegistry
from .claude_quota import COMPATIBLE_SOURCE, ClaudeQuotaCollector, watch_claude_quota
from .config import WorkbenchConfig
from .delivery import GitHubDelivery, GitHubDeliveryRequest
from .executors import ClaudeExecutor, CodexExecutor
from .governance import VERIFICATION_TIERS, code_as_harness_health, governance_status
from .model import DEFAULT_QUOTA_TTL_SECONDS, NodeSpec, QuotaSnapshot, TaskContract
from .mobile import MobileRemote, MobileRemoteError
from .performance import PerformanceRegistry, PerformanceRegistryError
from .planner import PlannerError
from .radar import WorkbenchRadar
from .research import managed_research_skill_status
from .restart_readiness import assess_restart_readiness
from .recovery import RecoveryPolicy, WorktreeRecoveryManager
from .service import Coordinator
from .session_context import import_session_context
from .store import CommandConflictError, StateConflictError, WorkbenchStore
from .submission import submit_natural_language_request
from .sync import RepositorySynchronizer


def _config(args: argparse.Namespace) -> WorkbenchConfig:
    root = Path(args.home).expanduser() if getattr(args, "home", None) else None
    config = WorkbenchConfig.load(root)
    config.initialize()
    return config


def _store(config: WorkbenchConfig) -> WorkbenchStore:
    config.assert_authority()
    store = WorkbenchStore(config.database)
    store.initialize()
    return store


def command_init(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.authority:
        config = WorkbenchConfig(
            state_root=config.state_root,
            host=config.host,
            port=config.port,
            max_workers=config.max_workers,
            spark_workers=config.spark_workers,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id=authority_machine_id(),
            quota_snapshot_file=config.effective_quota_snapshot_file,
            quota_refresh_seconds=config.quota_refresh_seconds,
        )
        config.initialize()
    _store(config)
    print(json.dumps({"ok": True, "home": str(config.state_root), "version": __version__}))
    return 0


def command_serve(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.host or args.port or args.max_workers is not None or args.spark_workers is not None:
        config = WorkbenchConfig(
            state_root=config.state_root,
            host=args.host or config.host,
            port=args.port or config.port,
            max_workers=(
                args.max_workers if args.max_workers is not None else config.max_workers
            ),
            spark_workers=(
                args.spark_workers
                if args.spark_workers is not None
                else config.spark_workers
            ),
            deployment_role=config.deployment_role,
            authority_host=config.authority_host,
            authority_machine_id=config.authority_machine_id,
            quota_snapshot_file=config.effective_quota_snapshot_file,
            quota_refresh_seconds=config.quota_refresh_seconds,
        )
    store = _store(config)
    lease = CoordinatorAuthorityLease(config.state_root / "coordinator.lock")
    with lease as identity:
        coordinator_epoch = store.activate_coordinator(identity.instance_id, identity.machine_id)
        coordinator = Coordinator(
            store,
            config.state_root,
            coordinator_epoch=coordinator_epoch,
            max_workers=config.max_workers,
            spark_workers=config.effective_spark_workers,
            quota_snapshot_file=config.effective_quota_snapshot_file,
            quota_refresh_seconds=config.quota_refresh_seconds,
        )
        recovered = coordinator.recover()
        ledger = store.health()
        store.record_system_event(
            "coordinator.started",
            {
                **identity.to_dict(),
                "coordinator_epoch": coordinator_epoch,
                "governance": governance_status(),
                "ledger_cursor_before_start": ledger["cursor"],
                "ledger_task_count": sum(ledger["task_counts"].values()),
                "recovered_indeterminate": recovered,
            },
        )
        coordinator_thread = threading.Thread(target=coordinator.run_forever, name="coordinator", daemon=True)
        coordinator_thread.start()
        server: WorkbenchHTTPServer | None = None
        try:
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
                        "authority": identity.to_dict(),
                        "governance": governance_status(),
                        "recovered_indeterminate": recovered,
                    }
                ),
                flush=True,
            )
            server.serve_forever(poll_interval=0.5)
        finally:
            coordinator.stop()
            coordinator_thread.join(timeout=30)
            if server is not None:
                server.server_close()
            store.record_system_event(
                "coordinator.stopped",
                {"instance_id": identity.instance_id, "boot_id": identity.boot_id},
            )
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
    try:
        result = submit_natural_language_request(
            config,
            store,
            objective=args.objective,
            repository=args.repository,
            allowed_scope=args.allowed_scope,
            forbidden_scope=args.forbidden_scope or (),
            acceptance_commands=args.acceptance_command or (),
            task_id=args.task_id,
            command_id=args.command_id,
            planner_model=args.planner_model,
            executor_model=args.executor_model,
            verifier_model=args.verifier_model,
            timeout_seconds=args.timeout,
            retry_limit=args.retry_limit,
            external_write_permission=args.allow_external_write,
            queue=args.queue,
            base_sha=args.base_sha,
            task_type=args.task_type,
            complexity=args.complexity,
            parallelizable=not args.serial,
            claude_allowed=not args.no_claude,
            task_points=args.task_points,
            verification_tier=args.verification_tier,
        )
    except PlannerError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_mcp(args: argparse.Namespace) -> int:
    from .mcp import serve_stdio

    config = _config(args)
    serve_stdio(config, _store(config))
    return 0


def command_context(args: argparse.Namespace) -> int:
    if args.context_action != "import":
        raise AssertionError(args.context_action)
    config = _config(args)
    store = _store(config)
    if args.archive == "-":
        result = import_session_context(
            config,
            store,
            sys.stdin.buffer,
            command_id=args.command_id,
        )
    else:
        with Path(args.archive).expanduser().open("rb") as source:
            result = import_session_context(
                config,
                store,
                source,
                command_id=args.command_id,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_sync(args: argparse.Namespace) -> int:
    synchronizer = RepositorySynchronizer()
    if args.sync_action == "github":
        result = synchronizer.sync_github(args.repository, args.remote, args.branch)
    elif args.sync_action == "export":
        result = synchronizer.export_increment(
            args.repository,
            args.base_ref,
            args.head_ref,
            Path(args.output),
        )
    elif args.sync_action == "import":
        if args.bundle == "-":
            with tempfile.NamedTemporaryFile(prefix="codex-workbench-increment-", suffix=".bundle") as temporary:
                temporary.write(sys.stdin.buffer.read())
                temporary.flush()
                result = synchronizer.import_increment(
                    args.repository,
                    Path(temporary.name),
                    args.ref_name,
                )
        else:
            result = synchronizer.import_increment(
                args.repository,
                Path(args.bundle),
                args.ref_name,
            )
    elif args.sync_action == "send":
        result = synchronizer.send_increment(
            args.repository,
            args.base_ref,
            args.head_ref,
            host=args.host,
            remote_repository=args.remote_repository,
            ref_name=args.ref_name,
        )
    else:
        raise AssertionError(args.sync_action)
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
    elif args.action == "priority":
        revision = store.set_task_priority(
            args.task_id,
            args.priority,
            expected_revision=args.expected_revision,
        )
        result = {"ok": True, "task_id": args.task_id, "revision": revision}
    elif args.action == "steer":
        revision = store.append_task_steering(
            args.task_id,
            args.instruction,
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


def command_approval(args: argparse.Namespace) -> int:
    store = _store(_config(args))
    if args.approval_action == "list":
        result: object = store.list_approvals(
            pending_only=not args.all,
            limit=args.limit,
        )
    elif args.approval_action == "decide":
        revision = store.decide_approval(
            args.approval_id,
            args.decision,
            expected_revision=args.expected_revision,
        )
        result = {
            "ok": True,
            "approval_id": args.approval_id,
            "revision": revision,
        }
    else:
        raise AssertionError(args.approval_action)
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


def command_deliver(args: argparse.Namespace) -> int:
    config = _config(args)
    receipt = GitHubDelivery(
        _store(config), ArtifactStore(config.state_root / "artifacts")
    ).deliver(
        GitHubDeliveryRequest(
            task_id=args.task_id,
            command_id=args.command_id or f"deliver-{uuid.uuid4()}",
            base_branch=args.base_branch,
            remote=args.remote,
            merge=args.merge,
            release_tag=args.release_tag,
        )
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["state"] not in {"failed", "indeterminate"} else 2


def command_quota(args: argparse.Namespace) -> int:
    if args.quota_action in {"collect-claude", "watch-claude"}:
        root = Path(args.home).expanduser() if getattr(args, "home", None) else None
        config = WorkbenchConfig.load(root)
        selected = args.claude_binary or os.environ.get("CODEX_WORKBENCH_CLAUDE") or shutil.which("claude")
        if not selected:
            raise ValueError("Claude CLI is unavailable; refusing to create a quota producer")
        binary = Path(selected).expanduser().resolve(strict=True)
        if not binary.is_file():
            raise ValueError("Claude CLI path is not a file")
        output = (
            Path(args.output).expanduser().resolve()
            if args.output
            else config.effective_quota_snapshot_file
        )
        collector = ClaudeQuotaCollector(binary, output)
        if args.quota_action == "watch-claude":
            watch_claude_quota(
                collector,
                interval_seconds=args.interval,
                emit=lambda event: print(
                    json.dumps(event, ensure_ascii=False),
                    flush=True,
                ),
            )
            return 0
        snapshot = collector.collect()
        print(
            json.dumps(
                {
                    "ok": True,
                    "auth_ok": snapshot["auth_ok"],
                    "source": snapshot["source"],
                    "output": str(output),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.quota_action == "set" and args.source == COMPATIBLE_SOURCE:
        raise ValueError("quota set cannot claim Claude producer provenance")
    store = _store(_config(args))
    if args.quota_action == "show":
        snapshot = store.latest_quota()
        print(
            json.dumps(
                {
                    "snapshot": asdict(snapshot),
                    "policy": snapshot.policy_summary(
                        max_age_seconds=DEFAULT_QUOTA_TTL_SECONDS
                    ),
                }
                if snapshot
                else None,
                ensure_ascii=False,
                indent=2,
            )
        )
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
        five_hour_window_id=args.five_hour_window,
        weekly_window_id=args.weekly_window,
    )
    store.write_quota(snapshot)
    print(
        json.dumps(
            {
                "ok": True,
                "snapshot": asdict(snapshot),
                "policy": snapshot.policy_summary(
                    max_age_seconds=DEFAULT_QUOTA_TTL_SECONDS
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_acceptance(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    if args.acceptance_action == "remediate-legacy":
        if not args.manifest or not args.command_id:
            raise ValueError("remediate-legacy requires --manifest and --command-id")
        manifest_path = Path(args.manifest).expanduser()
        if not manifest_path.is_file():
            raise ValueError("legacy remediation manifest file does not exist")
        manifest_ref = store.artifacts.put_bytes(manifest_path.read_bytes(), "json")
        try:
            receipt = store.remediate_legacy_evidence(args.command_id, manifest_ref)
        except (ValueError, CommandConflictError, StateConflictError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 2
        print(json.dumps({"ok": True, **receipt}, ensure_ascii=False, indent=2))
        return 0
    if args.acceptance_action == "attest-a12":
        if not args.artifact or not args.quota_window or not args.note or not args.source_session_id:
            raise ValueError(
                "attest-a12 requires --artifact, --quota-window, --source-session-id, and --note"
            )
        artifact = Path(args.artifact).expanduser()
        if not artifact.is_file():
            raise ValueError("A12 artifact file does not exist")
        artifact = artifact.resolve()
        suffix = presentation_format(artifact)
        if suffix is None:
            raise ValueError("A12 artifact content must be a valid PPT, PPTX, or PDF file")
        data = artifact.read_bytes()
        if not data:
            raise ValueError("A12 artifact must not be empty")
        artifact_ref = ArtifactStore(config.state_root / "artifacts").put_bytes(data, suffix)
        export_receipt = getattr(args, "export_receipt", None)
        cursor = store.record_acceptance_attestation(
            "A12",
            artifact_ref,
            artifact.name,
            len(data),
            args.quota_window,
            args.source_session_id,
            args.note,
            export_receipt=export_receipt,
        )
        print(
            json.dumps(
                {"ok": True, "check_id": "A12", "event_cursor": cursor, "artifact_ref": artifact_ref},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    report = build_acceptance_report(store)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["complete"] else 1


def command_client(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    if args.client_action == "heartbeat":
        policy = RecoveryPolicy.load(config.state_root)
        cursor = store.record_client_heartbeat(
            args.client_id,
            args.kind,
            route=args.route,
            reason=args.reason,
            observed_at=args.observed_at,
            presence_ttl_seconds=policy.home_presence_ttl_seconds,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "event_cursor": cursor,
                    "client_id": args.client_id,
                    "kind": args.kind,
                    "route": args.route,
                    "home_presence": store.active_home_presence(),
                },
                ensure_ascii=False,
            )
        )
        return 0
    raise ValueError(f"unsupported client action: {args.client_action}")


def command_worktree(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _store(config)
    policy = RecoveryPolicy.load(config.state_root)
    manager = WorktreeRecoveryManager(store, policy)
    action = args.worktree_action
    if action == "status":
        result = {
            "ok": True,
            "policy": {
                "enabled": policy.enabled,
                "recycle_root": str(policy.recycle_root),
                "nas_archive_root": str(policy.archive_root) if policy.archive_root else None,
                "compression": policy.compression,
                "require_smb": policy.require_smb,
                "remote_archive_host": policy.remote_archive_host,
                "sweep_interval_seconds": policy.sweep_interval_seconds,
                "retry_backoff_seconds": policy.retry_backoff_seconds,
            },
            "home_presence": store.active_home_presence(),
            "allocations": store.list_worktree_allocations(),
            "archives": store.list_worktree_archives(),
        }
    elif action == "sweep":
        if args.max_items < 1 or args.max_items > 100:
            raise ValueError("--max-items must be between 1 and 100")
        result = manager.sweep(max_items=args.max_items)
    elif action == "quarantine":
        result = manager.quarantine(args.allocation_id)
    elif action == "archive":
        if store.active_home_presence() is None:
            raise StateConflictError("local NAS archive requires a fresh MacBook home-LAN presence lease")
        result = manager.archive_allocation(args.allocation_id, purge=not args.keep_local)
    elif action == "send":
        result = manager.send_allocation(args.allocation_id, args.host)
    elif action == "ingest":
        if args.archive == "-":
            source = sys.stdin.buffer
            result = manager.ingest_remote(
                source,
                archive_id=args.archive_id,
                expected_sha256=args.sha256,
                transport=args.transport,
                compression=args.compression,
            )
        else:
            with Path(args.archive).expanduser().open("rb") as source:
                result = manager.ingest_remote(
                    source,
                    archive_id=args.archive_id,
                    expected_sha256=args.sha256,
                    transport=args.transport,
                    compression=args.compression,
                )
    elif action == "restore":
        destination = Path(args.destination) if args.destination else None
        result = manager.restore(args.archive_id, destination)
    else:
        raise ValueError(f"unsupported worktree action: {action}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_token(args: argparse.Namespace) -> int:
    config = _config(args)
    print(config.token())
    return 0


def command_harness(args: argparse.Namespace) -> int:
    config = _config(args)
    report = code_as_harness_health(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _capability_registry(config: WorkbenchConfig) -> CapabilityRegistry:
    """Build a registry against the binaries used by this Workbench process."""

    return CapabilityRegistry(
        config.state_root,
        codex_binary=os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
        claude_binary=os.environ.get("CODEX_WORKBENCH_CLAUDE", "claude"),
    )


def _radar(config: WorkbenchConfig) -> WorkbenchRadar:
    return WorkbenchRadar(
        state_root=config.effective_radar_state_root,
        authorization_file=config.effective_radar_authorization_file,
        enabled=config.radar_enabled,
        stale_after_seconds=config.radar_stale_after_seconds,
        expire_after_seconds=config.radar_expire_after_seconds,
    )


def _refresh_performance_after_capabilities(
    config: WorkbenchConfig,
    refresh: dict[str, object],
) -> dict[str, object]:
    """Materialize the matching calibration after a safe catalog activation.

    The capability watcher runs on the Mac mini authority.  A client may
    inspect or refresh its own catalog, but it must never write the authority
    SQLite-backed performance ledger.  The registry is refreshed only when
    the returned catalog is the active generation, so a staged/unsafe catalog
    cannot silently influence new-task calibration.
    """

    catalog = refresh.get("catalog")
    active_generation_id = refresh.get("active_generation_id")
    if not isinstance(catalog, dict):
        return {
            "ok": False,
            "status": "unavailable",
            "reason": "capability refresh did not return a catalog generation",
        }
    if catalog.get("catalog_id") != active_generation_id:
        return {
            "ok": False,
            "status": "deferred",
            "reason": "catalog is not the active safe generation",
            "catalog_id": catalog.get("catalog_id"),
            "active_generation_id": active_generation_id,
        }
    if config.deployment_role != "authority":
        return {
            "ok": False,
            "status": "deferred",
            "reason": "client installation cannot write the authority performance ledger",
            "catalog_id": catalog.get("catalog_id"),
        }
    try:
        result = PerformanceRegistry(config.state_root).refresh(
            _store(config),
            catalog,
            radar_status=_radar(config).status(),
        )
    except PerformanceRegistryError as error:
        return {"ok": False, "status": "unavailable", "reason": str(error)}
    return {
        "ok": True,
        "status": "active",
        "snapshot_id": result["active_generation_id"],
        "activated": result["activated"],
        "unchanged": result["unchanged"],
        "model_calls": 0,
    }


def command_capabilities(args: argparse.Namespace) -> int:
    """Inspect and manage the passive, versioned capability catalog."""

    config = _config(args)
    registry = _capability_registry(config)
    action = args.capabilities_action
    try:
        if action == "status":
            result = registry.status()
        elif action == "show":
            catalog = (
                registry.load_generation(args.catalog_id)
                if args.catalog_id
                else registry.active()
            )
            result = {
                "ok": catalog is not None,
                "catalog": catalog,
                "catalog_id": catalog["catalog_id"] if catalog is not None else None,
                **({} if catalog is not None else {"error": "no active capability catalog"}),
            }
        elif action == "refresh":
            result = registry.refresh(
                bundled=bool(args.bundled),
                activate_safe=bool(args.activate_safe),
            )
            if result.get("ok") is True:
                result["performance"] = _refresh_performance_after_capabilities(
                    config,
                    result,
                )
        elif action == "diff":
            result = registry.diff(args.from_generation, args.to_generation)
        elif action == "activate":
            result = registry.activate(args.catalog_id, safe=True)
        elif action == "rollback":
            result = registry.rollback()
        else:
            raise ValueError(f"unsupported capabilities action: {action}")
    except CapabilityCatalogError as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _performance_catalog(config: WorkbenchConfig) -> tuple[dict[str, object], dict[str, object]]:
    """Read, but never probe or refresh, the capability catalog for a ledger run."""

    try:
        active = _capability_registry(config).active()
    except CapabilityCatalogError as error:
        active = None
        reason = str(error)
    else:
        reason = None
    if isinstance(active, dict):
        return active, {
            "status": "active",
            "catalog_id": active.get("catalog_id"),
            "capability_digest": active.get("digest"),
            "refreshed": False,
        }
    # A performance generation can still safely describe the historical
    # ledger before the passive capability catalog has been established.  It
    # carries an explicit anonymous catalog identity rather than inventing a
    # model/version claim.
    return {
        "catalog_id": None,
        "digest": None,
        "models": [],
        "agents": {},
    }, {
        "status": "unavailable",
        "catalog_id": None,
        "capability_digest": None,
        "refreshed": False,
        "reason": reason or "no active capability catalog",
    }


def command_performance(args: argparse.Namespace) -> int:
    """Inspect or materialize the advisory performance calibration ledger.

    All actions are local: ``refresh`` replays SQLite task/event evidence and
    writes a content-addressed snapshot.  It does not authenticate, prompt, or
    invoke either provider's model.
    """

    config = _config(args)
    store = _store(config)
    registry = PerformanceRegistry(config.state_root)
    try:
        if args.performance_action == "status":
            result = registry.status()
        elif args.performance_action == "show":
            snapshot = (
                registry.load_generation(args.snapshot_id)
                if args.snapshot_id
                else registry.active()
            )
            result = {
                "ok": snapshot is not None,
                "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
                "snapshot": snapshot,
                **({} if snapshot is not None else {"error": "no active performance snapshot"}),
            }
        elif args.performance_action == "refresh":
            catalog, catalog_status = _performance_catalog(config)
            result = registry.refresh(
                store,
                catalog,
                radar_status=_radar(config).status(),
            )
            result["catalog"] = catalog_status
            result["model_calls"] = 0
        else:
            raise ValueError(f"unsupported performance action: {args.performance_action}")
    except PerformanceRegistryError as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def command_radar(args: argparse.Namespace) -> int:
    """Inspect or refresh the portable, offline-capable Radar provider."""

    config = _config(args)
    radar = _radar(config)
    action = args.radar_action
    if action == "status":
        result = radar.status()
    elif action == "show":
        snapshot = (
            radar.registry.load_generation(args.snapshot_id)
            if args.snapshot_id
            else radar.registry.active()
        )
        result = {
            "ok": snapshot is not None,
            "state": radar.status().get("state"),
            "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
            "snapshot": snapshot,
            **({} if snapshot is not None else {"error": "no cached Radar snapshot"}),
        }
    elif action == "refresh":
        store = _store(config)
        result = radar.refresh()
        usable = radar.status()
        # Rebuild for every deterministic Radar state.  Ineligible states add
        # no external records, which actively removes an expired, disabled, or
        # revoked Radar prior from the performance snapshot instead of leaving
        # a formerly eligible generation in use indefinitely.
        catalog, catalog_status = _performance_catalog(config)
        performance = PerformanceRegistry(config.state_root).refresh(
            store,
            catalog,
            radar_status=usable,
        )
        result["performance"] = {
            "ok": True,
            "snapshot_id": performance["active_generation_id"],
            "activated": performance["activated"],
            "unchanged": performance["unchanged"],
            "radar_state": usable.get("state"),
            "imported_radar_prior": usable.get("routing_prior_eligible") is True,
            "catalog": catalog_status,
            "model_calls": 0,
        }
    else:
        raise ValueError(f"unsupported radar action: {action}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def command_mobile(args: argparse.Namespace) -> int:
    """Manage native Codex Remote Control without login or model execution."""

    try:
        remote = MobileRemote(
            codex_binary=args.codex_binary or os.environ.get("CODEX_WORKBENCH_CODEX", "codex"),
            user_codex_home=args.user_codex_home,
            marketplace_source=args.marketplace_source,
            workbench_binary=args.workbench_binary,
            dry_run=bool(args.dry_run),
        )
        action = args.mobile_action
        if action == "status":
            result = remote.status()
        elif action == "enable":
            result = remote.enable()
        elif action == "pair":
            result = remote.pair()
        elif action == "disable":
            result = remote.disable()
        else:
            raise ValueError(f"unsupported mobile action: {action}")
    except MobileRemoteError as error:
        result = {"ok": False, "action": args.mobile_action, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


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
    restart_recovery = assess_restart_readiness()
    research_skill = managed_research_skill_status(
        os.environ.get("CODEX_WORKBENCH_PROCESS_HOME")
    )
    harness_health = code_as_harness_health(config)
    overall_ok = codex_ok and git_code == 0 and bool(harness_health["ok"]) and research_skill["ok"] is not False and (
        restart_recovery["ready"] if args.require_restart_ready else True
    )
    report = {
        "ok": overall_ok,
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
        "restart_recovery": restart_recovery,
        "governance": governance_status(),
        "harness": harness_health,
        "research_skill": research_skill,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if overall_ok else 1


def command_fixture_demo(args: argparse.Namespace) -> int:
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
    with tempfile.TemporaryDirectory(prefix="codex-workbench-fixture-") as directory:
        fixture_root = Path(directory)
        store = WorkbenchStore(fixture_root / "state.sqlite")
        store.initialize()
        store.create_task(contract, nodes, f"fixture-demo-{task_id}")
        store.queue_task(task_id)
        lease = CoordinatorAuthorityLease(fixture_root / "coordinator.lock")
        with lease as identity:
            coordinator_epoch = store.activate_coordinator(identity.instance_id, identity.machine_id)
            coordinator = Coordinator(
                store,
                fixture_root,
                coordinator_epoch=coordinator_epoch,
                max_workers=2,
                poll_seconds=0.05,
            )
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
    init.add_argument(
        "--authority",
        action="store_true",
        help="pin this host as the sole Workbench state writer",
    )
    init.set_defaults(func=command_init)

    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--max-workers", type=int)
    serve.add_argument(
        "--spark-workers",
        type=int,
        help="logical Spark lane cap inside the shared executor; zero disables its priority lane",
    )
    serve.set_defaults(func=command_serve)

    mcp = sub.add_parser("mcp", help="serve the Codex-native Workbench tools over stdio")
    mcp.set_defaults(func=command_mcp)

    context = sub.add_parser("context", help="import and bind a Codex session context")
    context_sub = context.add_subparsers(dest="context_action", required=True)
    context_import = context_sub.add_parser("import")
    context_import.add_argument("--archive", required=True, help="tar.gz path or '-' for stdin")
    context_import.add_argument("--command-id", required=True)
    context.set_defaults(func=command_context)

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
    request.add_argument("--base-sha", help="fixed commit or imported increment ref; defaults to HEAD")
    request.add_argument("--planner-model", default="gpt-5.6-sol")
    request.add_argument("--executor-model", default="gpt-5.6-luna")
    request.add_argument("--verifier-model", default="gpt-5.6-sol")
    request.add_argument("--timeout", type=int, default=3600)
    request.add_argument("--retry-limit", type=int, default=3)
    request.add_argument(
        "--task-type",
        choices=("implementation", "debugging", "architecture", "review", "tests", "docs", "creative", "exploration"),
        default="implementation",
    )
    request.add_argument("--complexity", choices=("low", "standard", "high"), default="standard")
    request.add_argument("--serial", action="store_true", help="declare that worker nodes must not run in parallel")
    request.add_argument("--no-claude", action="store_true", help="route the task only through Codex subscription models")
    request.add_argument("--task-points", type=float, default=1.0, help="positive acceptance weight used for quota productivity")
    request.add_argument(
        "--verification-tier",
        choices=VERIFICATION_TIERS,
        default="L2",
        help="code-as-harness verification tier persisted in the task contract",
    )
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
    priority = task_sub.add_parser("priority")
    priority.add_argument("task_id")
    priority.add_argument("priority", type=int)
    priority.add_argument("--expected-revision", type=int, required=True)
    steer = task_sub.add_parser("steer")
    steer.add_argument("task_id")
    steer.add_argument("instruction")
    steer.add_argument("--expected-revision", type=int, required=True)
    task.set_defaults(func=command_task)

    approval = sub.add_parser("approval", help="list or decide durable approval receipts")
    approval_sub = approval.add_subparsers(dest="approval_action", required=True)
    approval_list = approval_sub.add_parser("list")
    approval_list.add_argument("--all", action="store_true")
    approval_list.add_argument("--limit", type=int, default=100)
    approval_decide = approval_sub.add_parser("decide")
    approval_decide.add_argument("approval_id")
    approval_decide.add_argument("--decision", choices=("retry", "fail", "cancel"), required=True)
    approval_decide.add_argument("--expected-revision", type=int, required=True)
    approval.set_defaults(func=command_approval)

    events = sub.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--task-id")
    events.set_defaults(func=command_events)

    sync = sub.add_parser("sync", help="synchronize repositories without copying active worktrees")
    sync_sub = sync.add_subparsers(dest="sync_action", required=True)
    sync_github = sync_sub.add_parser("github")
    sync_github.add_argument("--repository", required=True)
    sync_github.add_argument("--remote", default="origin")
    sync_github.add_argument("--branch", required=True)
    sync_export = sync_sub.add_parser("export")
    sync_export.add_argument("--repository", required=True)
    sync_export.add_argument("--base-ref", required=True)
    sync_export.add_argument("--head-ref", default="HEAD")
    sync_export.add_argument("--output", required=True)
    sync_import = sync_sub.add_parser("import")
    sync_import.add_argument("--repository", required=True)
    sync_import.add_argument("--bundle", required=True, help="bundle path or '-' for stdin")
    sync_import.add_argument("--ref-name", required=True)
    sync_send = sync_sub.add_parser("send")
    sync_send.add_argument("--repository", required=True)
    sync_send.add_argument("--base-ref", required=True)
    sync_send.add_argument("--head-ref", default="HEAD")
    sync_send.add_argument("--host", default="macmini")
    sync_send.add_argument("--remote-repository", required=True)
    sync_send.add_argument("--ref-name", required=True)
    sync.set_defaults(func=command_sync)

    deliver = sub.add_parser("deliver", help="deliver an accepted task through GitHub")
    deliver.add_argument("task_id")
    deliver.add_argument("--command-id")
    deliver.add_argument("--base-branch", required=True)
    deliver.add_argument("--remote", default="origin")
    deliver.add_argument("--merge", action="store_true")
    deliver.add_argument("--release-tag")
    deliver.set_defaults(func=command_deliver)

    quota = sub.add_parser("quota")
    quota_sub = quota.add_subparsers(dest="quota_action", required=True)
    quota_sub.add_parser("show")
    quota_collect = quota_sub.add_parser(
        "collect-claude",
        help="passively collect the local Claude subscription usage display",
    )
    quota_collect.add_argument("--claude-binary")
    quota_collect.add_argument("--output", help="v1 producer snapshot file; defaults to configured quota file")
    quota_watch = quota_sub.add_parser(
        "watch-claude",
        help="keep passive Claude subscription quota observations fresh",
    )
    quota_watch.add_argument("--claude-binary")
    quota_watch.add_argument("--output", help="v1 producer snapshot file; defaults to configured quota file")
    quota_watch.add_argument("--interval", type=int, default=60)
    quota_set = quota_sub.add_parser("set")
    quota_set.add_argument("--auth-ok", action="store_true")
    quota_set.add_argument("--auth-method", default="none")
    quota_set.add_argument("--five-hour", type=float)
    quota_set.add_argument("--weekly-all", type=float)
    quota_set.add_argument("--weekly-sonnet", type=float)
    quota_set.add_argument("--weekly-fable", type=float)
    quota_set.add_argument("--five-hour-window")
    quota_set.add_argument("--weekly-window")
    quota_set.add_argument("--source", default="manual")
    quota.set_defaults(func=command_quota)

    token = sub.add_parser("token")
    token.set_defaults(func=command_token)

    harness = sub.add_parser(
        "harness",
        help="inspect the managed Code-as-Harness capability without logging in or calling a model",
    )
    harness_sub = harness.add_subparsers(dest="harness_action", required=True)
    harness_sub.add_parser("health")
    harness.set_defaults(func=command_harness)

    capabilities = sub.add_parser(
        "capabilities",
        help="inspect or refresh the passive model and Agent capability catalog",
    )
    capabilities_sub = capabilities.add_subparsers(
        dest="capabilities_action", required=True
    )
    capabilities_sub.add_parser("status")
    capabilities_show = capabilities_sub.add_parser("show")
    capabilities_show.add_argument("catalog_id", nargs="?")
    capabilities_refresh = capabilities_sub.add_parser("refresh")
    capabilities_refresh.add_argument(
        "--bundled",
        action="store_true",
        help="read Codex's bundled model catalog instead of its live catalog",
    )
    capabilities_refresh.add_argument(
        "--activate-safe",
        action="store_true",
        help="activate the new generation only when its control-plane and worker gates pass",
    )
    capabilities_diff = capabilities_sub.add_parser("diff")
    capabilities_diff.add_argument("--from", dest="from_generation")
    capabilities_diff.add_argument("--to", dest="to_generation")
    capabilities_activate = capabilities_sub.add_parser("activate")
    capabilities_activate.add_argument("catalog_id")
    capabilities_sub.add_parser("rollback")
    capabilities.set_defaults(func=command_capabilities)

    performance = sub.add_parser(
        "performance",
        help="inspect or calibrate the local benchmark-prior and runtime performance ledger",
    )
    performance_sub = performance.add_subparsers(dest="performance_action", required=True)
    performance_sub.add_parser("status")
    performance_show = performance_sub.add_parser("show")
    performance_show.add_argument("snapshot_id", nargs="?")
    performance_sub.add_parser(
        "refresh",
        help="replay local SQLite events/tasks into a content-addressed snapshot; never calls a model",
    )
    performance.set_defaults(func=command_performance)

    radar = sub.add_parser(
        "radar",
        help="inspect or refresh authorized Codex Radar data and its offline cache",
    )
    radar_sub = radar.add_subparsers(dest="radar_action", required=True)
    radar_sub.add_parser("status")
    radar_show = radar_sub.add_parser("show")
    radar_show.add_argument("snapshot_id", nargs="?")
    radar_sub.add_parser(
        "refresh",
        help="refresh authorized JSON data and update the advisory performance snapshot",
    )
    radar.set_defaults(func=command_radar)

    mobile = sub.add_parser(
        "mobile",
        help="manage native Codex Remote Control for mobile observation and task control",
    )
    mobile_sub = mobile.add_subparsers(dest="mobile_action", required=True)
    for mobile_action in ("status", "enable", "pair", "disable"):
        mobile_sub.add_parser(mobile_action)
    for mobile_parser in mobile_sub.choices.values():
        mobile_parser.add_argument("--codex-binary")
        mobile_parser.add_argument("--user-codex-home")
        mobile_parser.add_argument("--marketplace-source")
        mobile_parser.add_argument("--workbench-binary")
        mobile_parser.add_argument("--dry-run", action="store_true")
    mobile.set_defaults(func=command_mobile)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--require-restart-ready", action="store_true")
    doctor.set_defaults(func=command_doctor)

    acceptance = sub.add_parser("acceptance", help="evaluate or attest A1-A12 durable Evidence")
    acceptance.add_argument(
        "acceptance_action",
        nargs="?",
        choices=("report", "attest-a12", "remediate-legacy"),
        default="report",
    )
    acceptance.add_argument("--artifact")
    acceptance.add_argument("--export-receipt", help="path or content-addressed Claude export receipt")
    acceptance.add_argument("--quota-window")
    acceptance.add_argument("--source-session-id")
    acceptance.add_argument("--note")
    acceptance.add_argument("--manifest", help="legacy A10 remediation manifest JSON")
    acceptance.add_argument("--command-id", help="idempotency key for legacy remediation")
    acceptance.set_defaults(func=command_acceptance)

    client = sub.add_parser("client", help="record a trusted cockpit heartbeat")
    client_sub = client.add_subparsers(dest="client_action", required=True)
    client_heartbeat = client_sub.add_parser("heartbeat")
    client_heartbeat.add_argument("--client-id", required=True)
    client_heartbeat.add_argument("--kind", choices=("macbook", "phone"), required=True)
    client_heartbeat.add_argument("--route", choices=("lan", "tailscale"))
    client_heartbeat.add_argument("--reason")
    client_heartbeat.add_argument("--observed-at")
    client.set_defaults(func=command_client)

    worktree = sub.add_parser("worktree", help="quarantine, archive, transfer, and restore Workbench worktrees")
    worktree_sub = worktree.add_subparsers(dest="worktree_action", required=True)
    worktree_sub.add_parser("status")
    worktree_sweep = worktree_sub.add_parser("sweep")
    worktree_sweep.add_argument("--max-items", type=int, default=1)
    worktree_quarantine = worktree_sub.add_parser("quarantine")
    worktree_quarantine.add_argument("allocation_id")
    worktree_archive = worktree_sub.add_parser("archive")
    worktree_archive.add_argument("allocation_id")
    worktree_archive.add_argument("--keep-local", action="store_true")
    worktree_send = worktree_sub.add_parser("send")
    worktree_send.add_argument("allocation_id")
    worktree_send.add_argument("--host")
    worktree_ingest = worktree_sub.add_parser("ingest")
    worktree_ingest.add_argument("--archive", default="-", help="archive path or '-' for stdin")
    worktree_ingest.add_argument("--archive-id", required=True)
    worktree_ingest.add_argument("--sha256", required=True)
    worktree_ingest.add_argument("--transport", choices=("tailscale",), required=True)
    worktree_ingest.add_argument("--compression", choices=("zstd", "gzip"), required=True)
    worktree_restore = worktree_sub.add_parser("restore")
    worktree_restore.add_argument("archive_id")
    worktree_restore.add_argument("--destination")
    worktree.set_defaults(func=command_worktree)

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
    except (
        CommandConflictError,
        CoordinatorAuthorityError,
        KeyError,
        ValueError,
        StateConflictError,
        RuntimeError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        code = 2
    raise SystemExit(code)
