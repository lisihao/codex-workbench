from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import re
from typing import Any

from .artifacts import ArtifactStore, presentation_format
from .authority import normalize_boot_id
from .store import WorkbenchStore


@dataclass(frozen=True)
class AcceptanceCheck:
    id: str
    status: str
    requirement: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


REQUIREMENTS = {
    "A1": "MacBook 关闭 8 小时后，Mac mini 任务正常完成",
    "A2": "手机能看到真实任务状态和最后更新时间",
    "A3": "Mac mini 重启后队列和任务可以恢复",
    "A4": "Sonnet 与 Luna 分别完成真实工作包并返回 Evidence",
    "A5": "Sol 能独立验收并退回失败任务",
    "A6": "连续多个五小时窗口均未突破 Claude 20% 保留池",
    "A7": "每周全模型与 Sonnet 配额均保留至少 20%",
    "A8": "Claude 配额触线后自动转移到 Codex",
    "A9": "Claude 认证过期不会产生 Worker 重启风暴",
    "A10": "每个 accepted 真实任务都有 diff、测试、产物和验收记录",
    "A11": "MacBook 与手机不会形成两个冲突的主协调器",
    "A12": "Claude 网页端仍可使用保留额度完成 PPT 写作",
}


def build_acceptance_report(store: WorkbenchStore) -> dict[str, Any]:
    tasks = store.list_tasks(limit=500)
    events = store.read_events(after=0, limit=10_000)
    quota = _runtime_quota_evidence(store.list_quota_snapshots(limit=5_000))
    authority = store.authority_status()
    checks = [
        _macbook_offline_check(tasks, events),
        _phone_observation_check(events),
        _restart_check(tasks, events),
        _model_worker_check(tasks),
        _sol_verifier_check(events),
        _five_hour_quota_check(quota),
        _weekly_quota_check(quota),
        _fallback_check(tasks, events),
        _auth_storm_check(tasks, events),
        _accepted_evidence_check(tasks),
        _authority_check(authority),
        _ppt_reserve_check(events, ArtifactStore(store.path.parent / "artifacts")),
    ]
    counts = {status: sum(check.status == status for check in checks) for status in ("ok", "warn", "error", "pending")}
    return {
        "complete": counts["ok"] == len(checks),
        "counts": counts,
        "checks": [check.to_dict() for check in checks],
    }


def _runtime_quota_evidence(quota: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded_markers = {"fixture", "test", "tests", "controlled", "simulation"}
    return [
        snapshot
        for snapshot in quota
        if not excluded_markers.intersection(
            re.split(r"[^a-z0-9]+", str(snapshot.get("source", "")).lower())
        )
    ]


def _pending(check_id: str, evidence: str) -> AcceptanceCheck:
    return AcceptanceCheck(check_id, "pending", REQUIREMENTS[check_id], evidence)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _macbook_offline_check(
    tasks: list[dict[str, Any]], events: list[dict[str, Any]]
) -> AcceptanceCheck:
    by_client: dict[str, list[datetime]] = {}
    for event in events:
        if event["event_type"] != "client.heartbeat":
            continue
        payload = event["payload"]
        if payload.get("client_kind") != "macbook":
            continue
        by_client.setdefault(str(payload.get("client_id", "unknown")), []).append(
            _timestamp(event["created_at"])
        )
    accepted = [
        (task["task_id"], _timestamp(task["updated_at"]))
        for task in tasks
        if task["state"] == "accepted"
    ]
    for client_id, heartbeats in by_client.items():
        heartbeats.sort()
        for before, after in zip(heartbeats, heartbeats[1:]):
            gap = after - before
            if gap < timedelta(hours=8):
                continue
            completed = [task_id for task_id, timestamp in accepted if before < timestamp < after]
            if completed:
                return AcceptanceCheck(
                    "A1",
                    "ok",
                    REQUIREMENTS["A1"],
                    f"{client_id} 心跳中断 {gap.total_seconds() / 3600:.1f} 小时；期间完成 {len(completed)} 个 accepted 任务",
                )
    return _pending("A1", "等待同一 MacBook 两次心跳间隔至少 8 小时，且间隔内有任务进入 accepted")


def _phone_observation_check(events: list[dict[str, Any]]) -> AcceptanceCheck:
    observations = [
        event
        for event in events
        if event["event_type"] == "client.observed"
        and event["payload"].get("device_class") == "phone"
        and event["payload"].get("authenticated") is True
        and event["payload"].get("rendered") is True
    ]
    if observations:
        latest = observations[-1]
        return AcceptanceCheck(
            "A2",
            "ok",
            REQUIREMENTS["A2"],
            f"手机客户端 {latest['payload'].get('client_id')} 已渲染游标 {latest['payload'].get('snapshot_cursor')}，服务端回执时间 {latest['created_at']}",
        )
    return _pending("A2", "等待已登录手机浏览器成功渲染一次真实快照并写入服务端回执")


def _ppt_reserve_check(
    events: list[dict[str, Any]], artifacts: ArtifactStore
) -> AcceptanceCheck:
    attestations: list[dict[str, Any]] = []
    for event in events:
        payload = event["payload"]
        if (
            event["event_type"] != "acceptance.attested"
            or payload.get("check_id") != "A12"
            or not str(payload.get("artifact_ref", "")).startswith("sha256:")
            or int(payload.get("artifact_size", 0)) <= 0
            or not payload.get("quota_window_id")
            or payload.get("provider") != "claude-web"
            or payload.get("provenance_kind") != "real-user-journey"
            or not payload.get("source_session_id")
        ):
            continue
        try:
            path = artifacts.verify(str(payload["artifact_ref"]))
        except ValueError:
            continue
        if (
            path.stat().st_size == int(payload["artifact_size"])
            and presentation_format(path) == payload.get("detected_format")
        ):
            attestations.append(event)
    if attestations:
        latest = attestations[-1]
        return AcceptanceCheck(
            "A12",
            "pending",
            REQUIREMENTS["A12"],
            f"管理员已导入 {latest['payload'].get('artifact_name')}，但缺少可核验 Claude export/receipt 与配额快照关联；等待人工验收",
        )
    return _pending("A12", "等待本地管理员导入一次保留池内完成的 Claude 网页 PPT/PDF 工件")


def _restart_check(tasks: list[dict[str, Any]], events: list[dict[str, Any]]) -> AcceptanceCheck:
    starts = [event for event in events if event["event_type"] == "coordinator.started"]
    boot_ids = {
        normalize_boot_id(str(event["payload"]["boot_id"]))
        for event in starts
        if event["payload"].get("boot_id")
        and normalize_boot_id(str(event["payload"]["boot_id"])) not in {"unknown", "darwin:unknown"}
    }
    accepted = [task for task in tasks if task["state"] == "accepted"]
    latest = starts[-1]["payload"] if starts else {}
    recovered_ledger = int(latest.get("ledger_task_count", 0)) > 0 and int(
        latest.get("ledger_cursor_before_start", 0)
    ) > 0
    latest_start_cursor = int(starts[-1]["cursor"]) if starts else 0
    accepted_ids = {str(task["task_id"]) for task in accepted}
    created_before = {
        str(event["task_id"])
        for event in events
        if event["event_type"] == "task.created"
        and event.get("task_id") in accepted_ids
        and int(event["cursor"]) < latest_start_cursor
    }
    accepted_after = {
        str(event["task_id"])
        for event in events
        if event["event_type"] == "task.state_changed"
        and event.get("task_id") in accepted_ids
        and event["payload"].get("to") == "accepted"
        and int(event["cursor"]) > latest_start_cursor
    }
    crossed_boot = bool(len(starts) >= 2 and created_before & accepted_after)
    if len(boot_ids) >= 2 and crossed_boot and recovered_ledger:
        return AcceptanceCheck(
            "A3",
            "ok",
            REQUIREMENTS["A3"],
            f"记录到 {len(boot_ids)} 个 boot ID；新 boot 启动前已恢复 {latest['ledger_task_count']} 个任务，当前 {len(accepted)} 个 accepted",
        )
    return _pending("A3", f"当前记录 {len(boot_ids)} 个 boot ID；需要一次整机重启后的持久账本证据")


def _model_worker_check(tasks: list[dict[str, Any]]) -> AcceptanceCheck:
    models = {
        str(node.get("result", {}).get("actual_model", "")).lower()
        for task in tasks
        if task["state"] == "accepted"
        for node in task["nodes"]
        if not node.get("verifier")
        and node["executor"] != "fixture"
        and node["state"] == "accepted"
        and node.get("result")
    }
    luna = any("luna" in model for model in models)
    sonnet = any("sonnet" in model for model in models)
    if luna and sonnet:
        return AcceptanceCheck("A4", "ok", REQUIREMENTS["A4"], "Luna 与 Sonnet 均有 accepted 的真实模型 Evidence")
    missing = [name for name, present in (("Luna", luna), ("Sonnet", sonnet)) if not present]
    return _pending("A4", f"缺少 {'、'.join(missing)} 的 accepted 真实模型 Evidence")


def _sol_verifier_check(events: list[dict[str, Any]]) -> AcceptanceCheck:
    # Final-state projections erase rejected attempts. A5 is therefore proven
    # by the append-only reject -> repair -> accepted event chain instead.
    by_task: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("task_id"):
            by_task.setdefault(str(event["task_id"]), []).append(event)
    for task_id, chain in by_task.items():
        rejected = [
            event for event in chain
            if event["event_type"] == "node.failed"
            and event["payload"].get("result", {}).get("result_kind") == "verifier"
            and "sol" in str(event["payload"].get("result", {}).get("actual_model", "")).lower()
        ]
        repairs = [event for event in chain if event["event_type"] == "task.repair_scheduled"]
        accepted = [
            event for event in chain
            if event["event_type"] == "node.accepted"
            and event["payload"].get("result", {}).get("result_kind") == "verifier"
            and "sol" in str(event["payload"].get("result", {}).get("actual_model", "")).lower()
        ]
        if rejected and repairs and accepted and rejected[0]["cursor"] < repairs[0]["cursor"] < accepted[-1]["cursor"]:
            return AcceptanceCheck(
                "A5", "ok", REQUIREMENTS["A5"],
                f"任务 {task_id} 已保存 Sol reject → repair → accepted 事件链",
            )
    return _pending("A5", "需要同一任务的 Sol reject、repair_scheduled 与后续 accepted 持久事件链")


def _five_hour_quota_check(quota: list[dict[str, Any]]) -> AcceptanceCheck:
    windows: dict[str, list[float]] = {}
    for snapshot in quota:
        window = snapshot.get("five_hour_window_id")
        remaining = snapshot.get("five_hour_remaining")
        if window and remaining is not None:
            windows.setdefault(str(window), []).append(float(remaining))
    if len(windows) < 2:
        return _pending("A6", f"只有 {len(windows)} 个具名五小时窗口；至少需要 2 个")
    minimum = min(value for values in windows.values() for value in values)
    status = "ok" if minimum >= 20 else "error"
    return AcceptanceCheck("A6", status, REQUIREMENTS["A6"], f"{len(windows)} 个窗口最低剩余 {minimum:.1f}%")


def _weekly_quota_check(quota: list[dict[str, Any]]) -> AcceptanceCheck:
    windows: dict[str, list[tuple[float, float]]] = {}
    for snapshot in quota:
        window = snapshot.get("weekly_window_id")
        all_remaining = snapshot.get("weekly_all_remaining")
        sonnet_remaining = snapshot.get("weekly_sonnet_remaining")
        if window and all_remaining is not None and sonnet_remaining is not None:
            windows.setdefault(str(window), []).append((float(all_remaining), float(sonnet_remaining)))
    if not windows:
        return _pending("A7", "没有具名周窗口的全模型与 Sonnet 配额证据")
    minimum = min(value for values in windows.values() for pair in values for value in pair)
    status = "ok" if minimum >= 20 else "error"
    return AcceptanceCheck("A7", status, REQUIREMENTS["A7"], f"{len(windows)} 个周窗口最低剩余 {minimum:.1f}%")


def _fallback_check(tasks: list[dict[str, Any]], events: list[dict[str, Any]]) -> AcceptanceCheck:
    routed = [
        event for event in events
        if event["event_type"] == "node.routed"
        and event["payload"].get("from") == "claude"
        and event["payload"].get("to") == "codex"
        and any(
            marker in str(event["payload"].get("reason", "")).lower()
            for marker in ("quota", "protection active")
        )
    ]
    settled = {
        (task["task_id"], node["node_id"]): node["result"].get("actual_model")
        for task in tasks for node in task["nodes"]
        if node["state"] == "accepted"
        and node.get("result")
    }
    proven = [
        event for event in routed
        if settled.get((event["task_id"], event["node_id"])) == event["payload"].get("model")
    ]
    if proven:
        return AcceptanceCheck("A8", "ok", REQUIREMENTS["A8"], f"{len(proven)} 个 Claude 节点已路由到 Codex 并 accepted")
    return _pending("A8", "需要一次 Claude 配额触线后 Codex 接管的持久路由 Evidence")


def _auth_storm_check(tasks: list[dict[str, Any]], events: list[dict[str, Any]]) -> AcceptanceCheck:
    blocked = [
        node for task in tasks for node in task["nodes"]
        if node["executor"] == "claude" and node["state"] == "blocked"
        and "authentication" in str(node.get("result", {}).get("summary", "")).lower()
    ]
    routed = [
        event for event in events
        if event["event_type"] == "node.routed"
        and event["payload"].get("from") == "claude"
        and event["payload"].get("to") == "codex"
        and "authentication" in str(event["payload"].get("reason", "")).lower()
    ]
    attempts = [int(node["attempt"]) for node in blocked] + [
        int(event["payload"].get("attempt", 0)) for event in routed
    ]
    if attempts and all(attempt == 1 for attempt in attempts):
        return AcceptanceCheck("A9", "ok", REQUIREMENTS["A9"], f"{len(attempts)} 个认证阻断或接管节点均只尝试 1 次")
    return _pending("A9", "需要一次 Claude 认证失效且只尝试一次的持久 Evidence")


def _accepted_evidence_check(tasks: list[dict[str, Any]]) -> AcceptanceCheck:
    real_tasks = [
        task for task in tasks
        if task["state"] == "accepted"
        and any(node["executor"] != "fixture" for node in task["nodes"])
    ]
    incomplete: list[str] = []
    for task in real_tasks:
        artifacts = {
            key for node in task["nodes"] if node.get("result")
            for key in node["result"].get("artifacts", {})
        }
        has_patch = "patch" in artifacts
        has_log = bool({"stdout", "stderr"} & artifacts)
        has_verdict = bool(task.get("verdict"))
        if not (has_patch and has_log and has_verdict):
            incomplete.append(task["task_id"])
    if real_tasks and not incomplete:
        return AcceptanceCheck("A10", "ok", REQUIREMENTS["A10"], f"{len(real_tasks)} 个 accepted 真实任务均含 patch、日志和 verdict")
    if incomplete:
        return AcceptanceCheck("A10", "error", REQUIREMENTS["A10"], f"Evidence 不完整：{', '.join(incomplete)}")
    return _pending("A10", "尚无 accepted 真实任务")


def _authority_check(authority: dict[str, Any] | None) -> AcceptanceCheck:
    if authority is not None and authority.get("active"):
        return AcceptanceCheck("A11", "ok", REQUIREMENTS["A11"], f"单主租约由 {authority['host']} PID {authority['pid']} 持有")
    return AcceptanceCheck("A11", "error", REQUIREMENTS["A11"], "没有活动的单主协调器租约")
