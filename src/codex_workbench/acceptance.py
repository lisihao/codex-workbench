from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import re
from typing import Any

from .artifacts import ArtifactStore, presentation_format
from .authority import normalize_boot_id
from .claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
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
    legacy_remediations = store.legacy_evidence_remediations()
    quota = _runtime_quota_evidence(store.list_quota_snapshots(limit=5_000))
    authority = store.authority_status()
    checks = [
        _macbook_offline_check(tasks, events),
        _restart_check(tasks, events),
        _model_worker_check(tasks, store.artifacts),
        _sol_verifier_check(events),
        _five_hour_quota_check(quota),
        _weekly_quota_check(quota),
        _fallback_check(tasks, events, quota),
        _auth_storm_check(tasks, events),
        _accepted_evidence_check(tasks, events, store.artifacts, legacy_remediations),
        _authority_check(authority),
        _ppt_reserve_check(events, quota, store.artifacts),
    ]
    backlog = [
        AcceptanceCheck(
            "A2",
            "deferred",
            REQUIREMENTS["A2"],
            "用户已将手机接入移出当前交付范围；回加拿大后再恢复 tailnet 真机验收",
        )
    ]
    counts = {status: sum(check.status == status for check in checks) for status in ("ok", "warn", "error", "pending")}
    return {
        "complete": counts["ok"] == len(checks),
        "counts": counts,
        "checks": [check.to_dict() for check in checks],
        "backlog": [check.to_dict() for check in backlog],
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


def _is_compatible_subscription_snapshot(snapshot: dict[str, Any]) -> bool:
    """Accept only the version-pinned passive producer for quota-based gates."""
    return (
        snapshot.get("auth_ok") is True
        and snapshot.get("auth_method") == "native-subscription"
        and snapshot.get("source") == COMPATIBLE_SOURCE
        and snapshot.get("producer") == PRODUCER
        and snapshot.get("producer_schema_version") == PRODUCER_SCHEMA_VERSION
        and snapshot.get("claude_version") == SUPPORTED_USAGE_VERSION
    )


def _is_real_executor(executor: object) -> bool:
    return isinstance(executor, str) and executor.lower() in {"codex", "claude"}


def _is_real_model(model: object) -> bool:
    if not isinstance(model, str):
        return False
    value = model.strip().lower()
    if not value:
        return False
    return not any(
        marker in re.split(r"[^a-z0-9]+", value)
        for marker in ("fixture", "local", "deterministic", "test", "tests", "controlled", "simulation")
    )


def _nonempty_strings(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def _artifact_is_valid(artifacts: ArtifactStore, ref: object, *, non_empty: bool = True) -> bool:
    if not isinstance(ref, str) or not ref.startswith("sha256:"):
        return False
    try:
        path = artifacts.verify(ref)
        size = path.stat().st_size
    except (OSError, ValueError):
        return False
    return not non_empty or size > 0


def _evidence_artifact_is_valid(artifacts: ArtifactStore, ref: object) -> bool:
    """Accept empty stdout/stderr diagnostics while keeping evidence payloads non-empty."""
    suffix = ref.rsplit(":", 1)[-1] if isinstance(ref, str) else ""
    return _artifact_is_valid(
        artifacts,
        ref,
        non_empty=suffix not in {"stdout.log", "stderr.log"},
    )


def _artifact_digest(ref: str) -> str | None:
    try:
        algorithm, digest, _suffix = ref.split(":", 2)
    except ValueError:
        return None
    return digest if algorithm == "sha256" else None


def _event_attempt(event: dict[str, Any]) -> int | None:
    try:
        return int(event.get("payload", {}).get("attempt", 0))
    except (TypeError, ValueError):
        return None


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


def _export_receipt(
    artifacts: ArtifactStore,
    receipt_ref: object,
) -> dict[str, Any] | None:
    if not _artifact_is_valid(artifacts, receipt_ref):
        return None
    try:
        path = artifacts.verify(str(receipt_ref))
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _receipt_matches_attestation(
    receipt: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    if receipt.get("provider") != "claude-web":
        return False
    status = str(receipt.get("status", "")).lower()
    if status not in {"completed", "exported", "succeeded", "success", "ok"}:
        return False
    session_id = receipt.get("source_session_id", receipt.get("session_id"))
    if session_id != payload.get("source_session_id"):
        return False
    if receipt.get("quota_window_id", receipt.get("window_id")) != payload.get("quota_window_id"):
        return False
    artifact_ref = payload.get("artifact_ref")
    receipt_artifact_ref = receipt.get(
        "artifact_ref",
        receipt.get("output_artifact_ref"),
    )
    receipt_digest = receipt.get("artifact_sha256", receipt.get("artifact_hash"))
    if receipt_artifact_ref is not None:
        if receipt_artifact_ref != artifact_ref:
            return False
    if receipt_digest is not None:
        if not isinstance(receipt_digest, str):
            return False
        normalized_digest = receipt_digest
        if normalized_digest.startswith("sha256:"):
            normalized_digest = normalized_digest.split(":", 1)[1]
        if normalized_digest != _artifact_digest(str(artifact_ref)):
            return False
    if receipt_artifact_ref is None and receipt_digest is None:
        return False
    return True


def _quota_window_snapshot(
    quota: list[dict[str, Any]],
    quota_window_id: object,
) -> dict[str, Any] | None:
    if not isinstance(quota_window_id, str) or not quota_window_id.strip():
        return None
    for snapshot in quota:
        if not _is_compatible_subscription_snapshot(snapshot):
            continue
        if quota_window_id in {
            snapshot.get("five_hour_window_id"),
            snapshot.get("weekly_window_id"),
        }:
            return snapshot
    return None


def _quota_snapshot_zone(snapshot: dict[str, Any], source_model: object) -> str | None:
    if not _is_compatible_subscription_snapshot(snapshot):
        return None
    fields = ["five_hour_remaining", "weekly_all_remaining"]
    model = str(source_model or "").lower()
    if "sonnet" in model:
        fields.append("weekly_sonnet_remaining")
    elif "fable" in model and snapshot.get("weekly_fable_remaining") is not None:
        fields.append("weekly_fable_remaining")
    values: list[float] = []
    for field in fields:
        value = snapshot.get(field)
        if value is None:
            return None
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    minimum = min(values)
    if minimum <= 25:
        return "protected"
    if minimum < 30:
        return "red"
    return None


def _quota_reserve_is_intact(
    snapshot: dict[str, Any],
    receipt: dict[str, Any],
) -> bool:
    fields = ["five_hour_remaining", "weekly_all_remaining"]
    model = str(receipt.get("model", "sonnet")).lower()
    if "sonnet" in model:
        fields.append("weekly_sonnet_remaining")
    elif "fable" in model and snapshot.get("weekly_fable_remaining") is not None:
        fields.append("weekly_fable_remaining")
    # The all-model pool is authoritative for Fable unless a producer supplies
    # an additional Fable-specific pool. Missing required pools fail closed.
    values: list[float] = []
    for field in fields:
        value = snapshot.get(field)
        if value is None:
            return False
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return False
    for field, value in snapshot.items():
        if not field.endswith("_remaining") or field in fields or value is None:
            continue
        try:
            if float(value) < 20:
                return False
        except (TypeError, ValueError):
            return False
    return all(value >= 20 for value in values)


def _ppt_reserve_check(
    events: list[dict[str, Any]],
    quota: list[dict[str, Any]],
    artifacts: ArtifactStore,
) -> AcceptanceCheck:
    valid_artifacts: list[dict[str, Any]] = []
    for event in events:
        payload = event["payload"]
        if (
            event["event_type"] != "acceptance.attested"
            or payload.get("check_id") != "A12"
            or payload.get("provider") != "claude-web"
            or payload.get("provenance_kind") != "real-user-journey"
            or not payload.get("source_session_id")
            or not payload.get("quota_window_id")
            or not _artifact_is_valid(artifacts, payload.get("artifact_ref"))
        ):
            continue
        try:
            path = artifacts.verify(str(payload["artifact_ref"]))
            artifact_size = int(payload.get("artifact_size", 0))
            actual_size = path.stat().st_size
        except (OSError, ValueError, TypeError):
            continue
        try:
            detected_format = presentation_format(path)
        except (OSError, ValueError):
            continue
        if (
            artifact_size <= 0
            or actual_size != artifact_size
            or detected_format != payload.get("detected_format")
        ):
            continue
        valid_artifacts.append(event)
        receipt = _export_receipt(artifacts, payload.get("export_receipt_ref"))
        snapshot = _quota_window_snapshot(quota, payload.get("quota_window_id"))
        if (
            receipt is not None
            and _receipt_matches_attestation(receipt, payload)
            and snapshot is not None
            and _quota_reserve_is_intact(snapshot, receipt)
        ):
            return AcceptanceCheck(
                "A12",
                "ok",
                REQUIREMENTS["A12"],
                f"已核验 {payload.get('artifact_name')}、Claude export receipt 与配额窗口 {payload.get('quota_window_id')}；适用池均保持至少 20%",
            )
    if valid_artifacts:
        latest = valid_artifacts[-1]
        return _pending(
            "A12",
            f"已导入 {latest['payload'].get('artifact_name')}，但缺少可核验 export receipt 或真实配额窗口保留证据",
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


def _model_worker_check(
    tasks: list[dict[str, Any]], artifacts: ArtifactStore
) -> AcceptanceCheck:
    models: set[str] = set()
    for task in tasks:
        if task["state"] != "accepted":
            continue
        verifier_proven = any(
            node.get("verifier")
            and node.get("state") == "accepted"
            and (node.get("effective_executor") or node.get("executor")) == "codex"
            and isinstance(node.get("result"), dict)
            and node["result"].get("result_kind") == "verifier"
            and node["result"].get("verdict") == "accepted"
            and "sol" in str(node["result"].get("actual_model", "")).lower()
            and bool(node["result"].get("checks"))
            and bool(node["result"].get("evidence"))
            and all(
                _evidence_artifact_is_valid(artifacts, ref)
                for ref in node["result"].get("evidence", ())
            )
            for node in task["nodes"]
        )
        if not verifier_proven:
            continue
        for node in task["nodes"]:
            result = node.get("result")
            if (
                node.get("verifier")
                or node.get("state") != "accepted"
                or not _is_real_executor(node.get("effective_executor") or node.get("executor"))
                or not isinstance(result, dict)
                or result.get("result_kind") != "worker"
                or not _is_real_model(result.get("actual_model"))
                or not result.get("checks")
                or not result.get("artifacts")
                or not all(
                    _evidence_artifact_is_valid(artifacts, ref)
                    for ref in result.get("artifacts", {}).values()
                )
            ):
                continue
            models.add(str(result["actual_model"]).lower())
    luna = any("luna" in model for model in models)
    sonnet = any("sonnet" in model for model in models)
    if luna and sonnet:
        return AcceptanceCheck("A4", "ok", REQUIREMENTS["A4"], "Luna 与 Sonnet 均有 accepted 的真实模型 Evidence")
    missing = [name for name, present in (("Luna", luna), ("Sonnet", sonnet)) if not present]
    return _pending("A4", f"缺少 {'、'.join(missing)} 的 accepted 真实模型 + Sol verifier Evidence")


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
    observed = _quota_samples(
        quota,
        window_field="five_hour_window_id",
        remaining_fields=("five_hour_remaining",),
    )
    windows = _quota_coverage(
        quota,
        window_field="five_hour_window_id",
        remaining_fields=("five_hour_remaining",),
    )
    minimum = min((value for _window, (_at, values) in observed for value in values), default=None)
    if minimum is not None and minimum < 20:
        return AcceptanceCheck(
            "A6", "error", REQUIREMENTS["A6"], f"实测配额最低剩余 {minimum:.1f}%，已突破 20% 保留池"
        )
    covered = [window for window, samples in windows.items() if _window_is_covered(samples, timedelta(hours=4), timedelta(minutes=30))]
    if len(covered) < 2:
        return _pending(
            "A6",
            f"只有 {len(covered)} 个五小时窗口具有连续覆盖证据；需要至少 2 个（跨度≥4h、采样间隔≤30m）",
        )
    assert minimum is not None
    return AcceptanceCheck("A6", "ok", REQUIREMENTS["A6"], f"{len(covered)} 个连续覆盖窗口最低剩余 {minimum:.1f}%")


def _weekly_quota_check(quota: list[dict[str, Any]]) -> AcceptanceCheck:
    observed = _quota_samples(
        quota,
        window_field="weekly_window_id",
        remaining_fields=("weekly_all_remaining", "weekly_sonnet_remaining"),
    )
    windows = _quota_coverage(
        quota,
        window_field="weekly_window_id",
        remaining_fields=("weekly_all_remaining", "weekly_sonnet_remaining"),
    )
    minimum = min((value for _window, (_at, values) in observed for value in values), default=None)
    if minimum is not None and minimum < 20:
        return AcceptanceCheck(
            "A7", "error", REQUIREMENTS["A7"], f"实测周配额最低剩余 {minimum:.1f}%，已突破 20% 保留池"
        )
    covered = [window for window, samples in windows.items() if _window_is_covered(samples, timedelta(days=6), timedelta(hours=12))]
    if not covered:
        return _pending(
            "A7",
            "没有完整周窗口连续覆盖证据（跨度≥6d、采样间隔≤12h）的全模型与 Sonnet 配额记录",
        )
    assert minimum is not None
    return AcceptanceCheck("A7", "ok", REQUIREMENTS["A7"], f"{len(covered)} 个连续覆盖周窗口最低剩余 {minimum:.1f}%")


def _quota_coverage(
    quota: list[dict[str, Any]],
    *,
    window_field: str,
    remaining_fields: tuple[str, ...],
) -> dict[str, list[tuple[datetime, tuple[float, ...]]]]:
    windows: dict[str, list[tuple[datetime, tuple[float, ...]]]] = {}
    for window, sample in _quota_samples(quota, window_field=window_field, remaining_fields=remaining_fields):
        windows.setdefault(window, []).append(sample)
    for samples in windows.values():
        samples.sort(key=lambda sample: sample[0])
    return {window: samples for window, samples in windows.items() if not _window_remaining_increases(samples)}


def _quota_samples(
    quota: list[dict[str, Any]],
    *,
    window_field: str,
    remaining_fields: tuple[str, ...],
) -> list[tuple[str, tuple[datetime, tuple[float, ...]]]]:
    samples: list[tuple[str, tuple[datetime, tuple[float, ...]]]] = []
    for snapshot in quota:
        if not _is_compatible_subscription_snapshot(snapshot):
            continue
        window = snapshot.get(window_field)
        if not window or any(snapshot.get(field) is None for field in remaining_fields):
            continue
        try:
            observed_at = _timestamp(str(snapshot["observed_at"]))
            values = tuple(float(snapshot[field]) for field in remaining_fields)
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((str(window), (observed_at, values)))
    return samples


def _window_remaining_increases(samples: list[tuple[datetime, tuple[float, ...]]]) -> bool:
    return any(
        any(current_value > previous_value for previous_value, current_value in zip(previous[1], current[1]))
        for previous, current in zip(samples, samples[1:])
    )


def _window_is_covered(
    samples: list[tuple[datetime, tuple[float, ...]]],
    minimum_span: timedelta,
    maximum_gap: timedelta,
) -> bool:
    if len(samples) < 2 or samples[-1][0] - samples[0][0] < minimum_span:
        return False
    return all(
        current[0] - previous[0] <= maximum_gap
        for previous, current in zip(samples, samples[1:])
    )


def _route_quota_snapshot(
    event: dict[str, Any],
    events: list[dict[str, Any]],
    quota: list[dict[str, Any]],
) -> dict[str, Any] | None:
    payload = event.get("payload", {})
    attached = payload.get("quota_snapshot") or payload.get("quota_provenance")
    required_fields = (
        "observed_at",
        "auth_ok",
        "auth_method",
        "five_hour_remaining",
        "weekly_all_remaining",
        "weekly_sonnet_remaining",
        "weekly_fable_remaining",
        "source",
        "producer",
        "producer_schema_version",
        "claude_version",
        "five_hour_window_id",
        "weekly_window_id",
    )
    if isinstance(attached, dict):
        if not all(field in attached for field in required_fields):
            return None
        for snapshot in quota:
            if _is_compatible_subscription_snapshot(snapshot) and all(
                attached.get(key) == snapshot.get(key)
                for key in required_fields
            ):
                return snapshot
        return None
    # Legacy route events did not copy the snapshot. A prior quota.updated
    # event is the only durable fallback; a caller-provided source string is
    # not sufficient to manufacture provenance.
    route_cursor = int(event.get("cursor", 0))
    prior_updates = [
        item
        for item in events
        if int(item.get("cursor", 0)) < route_cursor
        and item.get("event_type") == "quota.updated"
        and isinstance(item.get("payload", {}).get("snapshot"), dict)
    ]
    if not prior_updates:
        return None
    attached = prior_updates[-1]["payload"]["snapshot"]
    if not all(field in attached for field in required_fields):
        return None
    for snapshot in quota:
        if _is_compatible_subscription_snapshot(snapshot) and all(
            attached.get(key) == snapshot.get(key)
            for key in required_fields
        ):
            return snapshot
    return None


def _fallback_check(
    tasks: list[dict[str, Any]],
    events: list[dict[str, Any]],
    quota: list[dict[str, Any]],
) -> AcceptanceCheck:
    routed = [
        event for event in events
        if event["event_type"] == "node.routed"
        and event["payload"].get("from") == "claude"
        and event["payload"].get("to") == "codex"
    ]
    task_by_id = {str(task["task_id"]): task for task in tasks}
    proven: list[dict[str, Any]] = []
    for event in routed:
        task = task_by_id.get(str(event.get("task_id")))
        if task is None:
            continue
        node = next(
            (item for item in task["nodes"] if item["node_id"] == event.get("node_id")),
            None,
        )
        result = node.get("result") if node is not None else None
        route_quota = _route_quota_snapshot(event, events, quota)
        derived_zone = (
            _quota_snapshot_zone(route_quota, node.get("model"))
            if node is not None and route_quota is not None
            else None
        )
        declared_zone = event["payload"].get("zone")
        if (
            node is None
            or node["state"] != "accepted"
            or not isinstance(result, dict)
            or result.get("result_kind") != "worker"
            or not _is_real_executor(node.get("effective_executor") or node.get("executor"))
            or (node.get("effective_executor") or node.get("executor")) != "codex"
            or not _is_real_model(result.get("actual_model"))
            or result.get("actual_model") != event["payload"].get("model")
            or derived_zone not in {"protected", "red"}
            or declared_zone is not None and declared_zone != derived_zone
            or route_quota is None
        ):
            continue
        try:
            route_attempt = int(event["payload"].get("attempt", 0))
        except (TypeError, ValueError):
            continue
        accepted_events = [
            item
            for item in events
            if item.get("event_type") == "node.accepted"
            and item.get("task_id") == event.get("task_id")
            and item.get("node_id") == event.get("node_id")
            and _event_attempt(item) == route_attempt
        ]
        if accepted_events:
            proven.append(event)
    if proven:
        return AcceptanceCheck(
            "A8",
            "ok",
            REQUIREMENTS["A8"],
            f"{len(proven)} 个 Claude 节点在真实配额触线后持久路由到 Codex 并 accepted",
        )
    return _pending("A8", "需要真实配额快照 provenance、触线窗口及 Claude → Codex accepted 路由 Evidence")


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


def _accepted_evidence_check(
    tasks: list[dict[str, Any]],
    events: list[dict[str, Any]],
    artifacts: ArtifactStore,
    legacy_remediations: list[dict[str, Any]],
) -> AcceptanceCheck:
    real_tasks = [
        task for task in tasks
        if task["state"] == "accepted"
        and any(
            node.get("state") == "accepted"
            and not node.get("verifier")
            and _is_real_executor(node.get("effective_executor") or node.get("executor"))
            for node in task["nodes"]
        )
    ]
    incomplete: list[str] = []
    for task in real_tasks:
        workers = [
            node
            for node in task["nodes"]
            if node.get("state") == "accepted"
            and not node.get("verifier")
            and _is_real_executor(node.get("effective_executor") or node.get("executor"))
        ]
        verifiers = [
            node
            for node in task["nodes"]
            if node.get("state") == "accepted" and node.get("verifier")
        ]
        valid_worker = any(
            isinstance(node.get("result"), dict)
            and node["result"].get("result_kind") == "worker"
            and _is_real_model(node["result"].get("actual_model"))
            and _artifact_is_valid(artifacts, node["result"].get("artifacts", {}).get("patch"))
            and _nonempty_strings(node["result"].get("checks"))
            and all(
                _artifact_is_valid(artifacts, ref, non_empty=False)
                for ref in node["result"].get("artifacts", {}).values()
            )
            for node in workers
        )
        valid_verifier = any(
            isinstance(node.get("result"), dict)
            and (node.get("effective_executor") or node.get("executor")) == "codex"
            and node["result"].get("result_kind") == "verifier"
            and node["result"].get("verdict") == "accepted"
            and "sol" in str(node["result"].get("actual_model", "")).lower()
            and _is_real_model(node["result"].get("actual_model"))
            and _nonempty_strings(node["result"].get("checks"))
            and _nonempty_strings(node["result"].get("evidence"))
            and all(
                _evidence_artifact_is_valid(artifacts, ref)
                for ref in node["result"].get("evidence", ())
            )
            and _artifact_is_valid(
                artifacts,
                node["result"].get("artifacts", {}).get("test-log"),
            )
            and _artifact_is_valid(
                artifacts,
                node["result"].get("artifacts", {}).get("verdict"),
            )
            and all(
                _artifact_is_valid(artifacts, ref, non_empty=False)
                for ref in node["result"].get("artifacts", {}).values()
            )
            for node in verifiers
        )
        remediated = any(
            _legacy_a10_overlay_is_complete(item, artifacts)
            for item in legacy_remediations
            if item["task_id"] == task["task_id"]
        )
        if not ((valid_worker and valid_verifier and _accepted_event_chain(task, events)) or remediated):
            incomplete.append(task["task_id"])
    if real_tasks and not incomplete:
        return AcceptanceCheck(
            "A10",
            "ok",
            REQUIREMENTS["A10"],
            f"{len(real_tasks)} 个 accepted 真实模型任务均含 diff、测试 Evidence、产物和 verifier accepted 记录",
        )
    if incomplete:
        return AcceptanceCheck("A10", "error", REQUIREMENTS["A10"], f"Evidence 不完整：{', '.join(incomplete)}")
    return _pending("A10", "尚无 accepted 真实任务")


def _legacy_a10_overlay_is_complete(overlay: dict[str, Any], artifacts: ArtifactStore) -> bool:
    """A10 only sees overlays already revalidated by the Store query."""
    workers = overlay.get("workers")
    if not isinstance(workers, list) or not workers:
        return False
    valid_worker = any(
        _is_real_model(worker.get("actual_model"))
        and _nonempty_strings(worker.get("checks"))
        and _artifact_is_valid(artifacts, worker.get("artifacts", {}).get("patch", {}).get("ref"))
        and all(
            isinstance(item, dict)
            and _artifact_is_valid(
                artifacts,
                item.get("ref"),
                non_empty=name == "patch",
            )
            for name, item in worker.get("artifacts", {}).items()
        )
        for worker in workers
        if isinstance(worker, dict) and isinstance(worker.get("artifacts"), dict)
    )
    valid_verifier = any(
        "sol" in str(verifier.get("actual_model", "")).lower()
        and _is_real_model(verifier.get("actual_model"))
        and _nonempty_strings(verifier.get("checks"))
        and _artifact_is_valid(artifacts, verifier.get("artifacts", {}).get("test-log", {}).get("ref"))
        and _artifact_is_valid(artifacts, verifier.get("artifacts", {}).get("verdict", {}).get("ref"))
        and bool(verifier.get("evidence"))
        and all(
            isinstance(item, dict)
            and _artifact_is_valid(artifacts, item.get("ref"), non_empty=False)
            for item in verifier["evidence"]
        )
        for verifier in overlay.get("verifiers", [])
        if isinstance(verifier, dict) and isinstance(verifier.get("artifacts"), dict)
    )
    supplemental = overlay.get("supplemental_sol_review")
    valid_supplemental = (
        isinstance(supplemental, dict)
        and "sol" in str(supplemental.get("actual_model", "")).lower()
        and _is_real_model(supplemental.get("actual_model"))
        and _nonempty_strings(supplemental.get("checks"))
        and isinstance(supplemental.get("patch"), dict)
        and _artifact_is_valid(artifacts, supplemental["patch"].get("ref"))
        and isinstance(supplemental.get("review_receipt"), dict)
        and _artifact_is_valid(artifacts, supplemental["review_receipt"].get("ref"))
        and isinstance(supplemental.get("test_log"), dict)
        and _artifact_is_valid(artifacts, supplemental["test_log"].get("ref"))
        and isinstance(supplemental.get("verdict_artifact"), dict)
        and _artifact_is_valid(artifacts, supplemental["verdict_artifact"].get("ref"))
        and isinstance(supplemental.get("review_transcript"), dict)
        and _artifact_is_valid(artifacts, supplemental["review_transcript"].get("ref"))
        and bool(supplemental.get("evidence"))
        and all(
            isinstance(item, dict)
            and _artifact_is_valid(artifacts, item.get("ref"), non_empty=False)
            for item in supplemental["evidence"]
        )
        and bool(supplemental.get("worker_artifacts"))
    )
    return valid_worker and (valid_verifier or valid_supplemental)


def _accepted_event_chain(task: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    def attempt_matches(event: dict[str, Any], attempt: int) -> bool:
        try:
            return int(event.get("payload", {}).get("attempt", 0)) == attempt
        except (TypeError, ValueError):
            return False

    task_events = [item for item in events if item.get("task_id") == task["task_id"]]
    real_workers = [
        node
        for node in task["nodes"]
        if node.get("state") == "accepted"
        and not node.get("verifier")
        and _is_real_executor(node.get("effective_executor") or node.get("executor"))
    ]
    worker_accepts: list[dict[str, Any]] = []
    for node in real_workers:
        node_events = [
            item
            for item in task_events
            if item.get("node_id") == node.get("node_id")
        ]
        try:
            attempt = int(node.get("attempt", 0))
        except (TypeError, ValueError):
            return False
        started = [
            item for item in node_events
            if item.get("event_type") == "node.started"
            and attempt_matches(item, attempt)
        ]
        accepted = [
            item for item in node_events
            if item.get("event_type") == "node.accepted"
            and attempt_matches(item, attempt)
            and item.get("payload", {}).get("result", {}).get("result_kind") == "worker"
        ]
        if not started or not accepted:
            return False
        worker_accepts.extend(accepted)

    valid_verifiers = [
        node
        for node in task["nodes"]
        if node.get("state") == "accepted"
        and node.get("verifier")
        and (node.get("effective_executor") or node.get("executor")) == "codex"
        and isinstance(node.get("result"), dict)
        and node["result"].get("result_kind") == "verifier"
        and node["result"].get("verdict") == "accepted"
    ]
    verifier_accepts: list[dict[str, Any]] = []
    evidence_claims: list[dict[str, Any]] = []
    for node in valid_verifiers:
        node_events = [
            item
            for item in task_events
            if item.get("node_id") == node.get("node_id")
        ]
        try:
            attempt = int(node.get("attempt", 0))
        except (TypeError, ValueError):
            return False
        started = [
            item for item in node_events
            if item.get("event_type") == "node.started"
            and attempt_matches(item, attempt)
        ]
        accepted = [
            item for item in node_events
            if item.get("event_type") == "node.accepted"
            and attempt_matches(item, attempt)
            and item.get("payload", {}).get("result", {}).get("result_kind") == "verifier"
        ]
        if not started or not accepted:
            continue
        verifier_accepts.extend(accepted)
        evidence = set(node["result"].get("evidence", ()))
        evidence_claims.extend(
            item
            for item in node_events
            if item.get("event_type") == "verifier.evidence_claimed"
            and attempt_matches(item, attempt)
            and item.get("payload", {}).get("artifact_ref") in evidence
        )
    task_accepts = [
        item
        for item in task_events
        if item.get("event_type") == "task.state_changed"
        and item.get("payload", {}).get("to") == "accepted"
    ]
    if not worker_accepts or not verifier_accepts or not evidence_claims or not task_accepts:
        return False
    latest_worker = max(int(item["cursor"]) for item in worker_accepts)
    latest_verifier = max(int(item["cursor"]) for item in verifier_accepts)
    latest_claim = max(int(item["cursor"]) for item in evidence_claims)
    latest_task = max(int(item["cursor"]) for item in task_accepts)
    return latest_worker < latest_verifier <= latest_claim <= latest_task


def _authority_check(authority: dict[str, Any] | None) -> AcceptanceCheck:
    if authority is not None and authority.get("active"):
        return AcceptanceCheck("A11", "ok", REQUIREMENTS["A11"], f"单主租约由 {authority['host']} PID {authority['pid']} 持有")
    return _pending("A11", "无法从持久 metadata、authority epoch 与 lifecycle 实例集合证明当前单主协调器仍活跃")
