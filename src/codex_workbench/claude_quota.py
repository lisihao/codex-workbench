from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODUCER = "codex-workbench.claude-quota"
PRODUCER_SCHEMA_VERSION = 1
# Version-pinned interpretation of CLI display text; not an official quota API.
COMPATIBLE_SOURCE = "claude-cli-usage-text-v1"
SUPPORTED_USAGE_VERSION = "2.1.239"
BASE_POOL_NAMES = ("five_hour", "seven_day")
MODEL_POOL_LABELS = {
    "Current week (Sonnet only)": "seven_day_sonnet",
    "Current week (Fable)": "seven_day_fable",
}
POOL_NAMES = (*BASE_POOL_NAMES, *MODEL_POOL_LABELS.values())
_SECRET_ENV_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
_BASE_TEXT_LABELS = ("Current session", "Current week (all models)")
_TEXT_LINE = re.compile(
    r"^(?P<label>[^\r\n:]+): (?P<used>\d{1,3})% used(?: · resets (?P<reset>.+))?$",
    re.MULTILINE,
)
_TIME_RESET = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm) \((?P<zone>[^()]+)\)$", re.I)
_DATE_RESET = re.compile(r"^(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:,? (?P<year>\d{4}))? \((?P<zone>[^()]+)\)$")
_DATE_TIME_RESET = re.compile(
    r"^(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:,? (?P<year>\d{4}))? "
    r"at (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm) "
    r"\((?P<zone>[^()]+)\)$",
    re.I,
)


class ClaudeQuotaError(ValueError):
    """The Claude CLI did not provide a complete, passive quota observation."""


@dataclass(frozen=True)
class ClaudeQuotaCollector:
    binary: Path
    output: Path
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    timeout_seconds: float = 20

    def collect(self) -> dict[str, Any]:
        version = "unknown"
        native_auth_ok = False
        try:
            auth = self._command("auth", "status", "--json")
            auth_payload = _json_object(auth.stdout, "claude auth status")
            version = self._claude_version()
            if _is_logged_out(auth_payload):
                snapshot = _unavailable_snapshot(version, "logged out")
                atomic_write_snapshot(self.output, snapshot)
                return snapshot
            if auth.returncode != 0:
                raise ClaudeQuotaError("claude auth status failed")
            if not is_native_subscription_auth(auth_payload):
                raise ClaudeQuotaError("Claude is not logged in with a first-party subscription")
            native_auth_ok = True
            if version != SUPPORTED_USAGE_VERSION:
                raise ClaudeQuotaError(f"unsupported Claude /usage display version: {version}")
            usage = self._command("-p", "/usage", "--output-format", "json", "--no-session-persistence")
            if usage.returncode != 0:
                raise ClaudeQuotaError("claude /usage command failed")
            outer = _json_result_object(usage.stdout, "claude /usage output")
            _validate_passive_usage(outer)
            observed = datetime.now(UTC)
            pools = _validated_pools(_parse_usage_text(str(outer["result"]), observed))
            snapshot = {
                "producer": PRODUCER, "producer_schema_version": PRODUCER_SCHEMA_VERSION,
                "source": COMPATIBLE_SOURCE, "claude_version": version,
                "observed_at": observed.isoformat(timespec="seconds"),
                "auth_ok": True, "auth_method": "native-subscription",
                "quota_ok": True,
                "pools": pools,
            }
            atomic_write_snapshot(self.output, snapshot)
            return snapshot
        except ClaudeQuotaError as error:
            atomic_write_snapshot(
                self.output,
                _unavailable_snapshot(
                    version,
                    str(error),
                    auth_ok=native_auth_ok,
                ),
            )
            raise

    def _claude_version(self) -> str:
        result = self._command("--version")
        if result.returncode != 0 or not result.stdout.strip():
            raise ClaudeQuotaError("claude --version failed")
        match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)
        if match is None:
            raise ClaudeQuotaError("claude --version did not contain a semantic version")
        return match.group(0)

    def _command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner([str(self.binary), *arguments], text=True, capture_output=True, check=False, env=scrubbed_environment(), timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise ClaudeQuotaError(f"Claude command timed out after {self.timeout_seconds:g}s") from error
        except OSError as error:
            raise ClaudeQuotaError(f"Claude command could not start: {error}") from error


def watch_claude_quota(
    collector: ClaudeQuotaCollector,
    *,
    interval_seconds: float,
    emit: Callable[[dict[str, Any]], None],
    sleeper: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> None:
    """Keep passive quota observations alive after an explicit launchd kickstart.

    Headless macOS GUI domains can enter on-demand-only mode, where a
    ``StartInterval`` trigger remains pending indefinitely.  The installer
    therefore starts one long-running watcher and launchd restarts it only if
    the process exits unsuccessfully.
    """
    if interval_seconds <= 0:
        raise ValueError("Claude quota watch interval must be positive")
    if max_iterations is not None and max_iterations <= 0:
        raise ValueError("Claude quota watcher max_iterations must be positive")
    completed = 0
    while max_iterations is None or completed < max_iterations:
        try:
            snapshot = collector.collect()
            event = {
                "ok": True,
                "auth_ok": snapshot["auth_ok"],
                "source": snapshot["source"],
                "output": str(collector.output),
            }
        except ClaudeQuotaError as error:
            event = {
                "ok": False,
                "error": str(error),
                "output": str(collector.output),
            }
        emit(event)
        completed += 1
        if max_iterations is None or completed < max_iterations:
            sleeper(interval_seconds)


def scrubbed_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    candidate = dict(os.environ if environment is None else environment)
    return {name: value for name, value in candidate.items() if not any(part in name.upper() for part in _SECRET_ENV_PARTS)}


def atomic_write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def validate_producer_snapshot(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Claude quota producer snapshot must be an object")
    for field, expected in (("producer", PRODUCER), ("producer_schema_version", PRODUCER_SCHEMA_VERSION), ("source", COMPATIBLE_SOURCE)):
        if raw.get(field) != expected:
            raise ValueError(f"invalid Claude quota producer {field}")
    if not isinstance(raw.get("claude_version"), str) or not raw["claude_version"].strip():
        raise ValueError("Claude quota producer version is required")
    if not isinstance(raw.get("observed_at"), str) or not raw["observed_at"].strip():
        raise ValueError("Claude quota producer observation time is required")
    if raw.get("auth_ok") is False:
        if raw.get("auth_method") != "none":
            raise ValueError("logged-out snapshot must have auth_method none")
        return dict(raw)
    if raw.get("auth_ok") is not True or raw.get("auth_method") != "native-subscription":
        raise ValueError("Claude quota producer authentication is invalid")
    if raw.get("quota_ok") is False:
        if not isinstance(raw.get("error"), str) or not raw["error"].strip():
            raise ValueError("unavailable Claude quota snapshot must contain an error")
        return dict(raw)
    pools = _validated_pools(raw.get("pools"))
    model_pool = next(name for name in MODEL_POOL_LABELS.values() if name in pools)
    if pools["seven_day"]["window_id"] != pools[model_pool]["window_id"]:
        raise ClaudeQuotaError("weekly quota pools must share one reset window")
    return {**raw, "pools": pools}


def _unavailable_snapshot(
    version: str,
    reason: str,
    *,
    auth_ok: bool = False,
) -> dict[str, Any]:
    return {
        "producer": PRODUCER,
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "source": COMPATIBLE_SOURCE,
        "claude_version": version,
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "auth_ok": auth_ok,
        "auth_method": "native-subscription" if auth_ok else "none",
        "quota_ok": False,
        "error": reason,
        "pools": {
            name: {
                "displayed_used_percent": None,
                "remaining_lower_bound": None,
                "window_id": None,
                "reset_precision": "none",
            }
            for name in POOL_NAMES
        },
    }


def _json_object(text: str, description: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ClaudeQuotaError(f"{description} is not JSON") from error
    if not isinstance(value, dict):
        raise ClaudeQuotaError(f"{description} is not an object")
    return value


def _json_result_object(text: str, description: str) -> dict[str, Any]:
    """Accept Claude's object or JSON-array envelope and return one final result."""

    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ClaudeQuotaError(f"{description} is not JSON") from error
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        results = [
            item
            for item in value
            if isinstance(item, dict) and item.get("type") == "result"
        ]
        if len(results) == 1:
            return results[0]
    raise ClaudeQuotaError(f"{description} does not contain exactly one result object")


def _is_logged_out(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status", "")).replace("_", "").replace("-", "").lower()
    return payload.get("loggedIn") is False or payload.get("logged_in") is False or status == "loggedout"


def is_native_subscription_auth(payload: Mapping[str, Any]) -> bool:
    if payload.get("loggedIn") is not True and payload.get("logged_in") is not True:
        return False
    methods = {str(payload.get(field, "")).strip().lower() for field in ("authMethod", "auth_method", "accountType", "account_type")}
    api_provider = payload.get("apiProvider", payload.get("api_provider"))
    return (
        bool(methods.intersection({"native-subscription", "subscription", "claude.ai"}))
        and isinstance(api_provider, str)
        and api_provider.strip().lower() == "firstparty"
    )


# Backward-compatible private name for existing integrations and tests.
_is_native_subscription = is_native_subscription_auth


def _validate_passive_usage(outer: Mapping[str, Any]) -> None:
    if outer.get("type") != "result" or outer.get("subtype") != "success" or outer.get("is_error") is not False:
        raise ClaudeQuotaError("/usage did not return a successful result envelope")
    for field in ("num_turns", "duration_api_ms", "total_cost_usd"):
        value = outer.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != 0:
            raise ClaudeQuotaError(f"/usage must report {field}=0")
    if outer.get("modelUsage") != {}:
        raise ClaudeQuotaError("/usage must report modelUsage={}")
    usage = outer.get("usage")
    if not isinstance(usage, Mapping) or usage.get("iterations") != []:
        raise ClaudeQuotaError("/usage must report usage.iterations=[]")
    if (
        outer.get("permission_denials") != []
        or not _zero_activity_mapping(outer.get("subagent_stats"))
        or outer.get("stop_reason") is not None
    ):
        raise ClaudeQuotaError("/usage must not record permissions, subagents, or a stop reason")
    for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        _zero_number(usage, field)
    _zero_number(usage.get("output_tokens_details"), "thinking_tokens")
    for field in ("web_search_requests", "web_fetch_requests"):
        _zero_number(usage.get("server_tool_use"), field)
    for field in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        _zero_number(usage.get("cache_creation"), field)
    if not isinstance(outer.get("result"), str):
        raise ClaudeQuotaError("/usage result display is missing")


def _zero_activity_mapping(value: object) -> bool:
    """Accept Claude's version-pinned empty or recursively zero activity map."""

    if not isinstance(value, Mapping):
        return False
    for item in value.values():
        if isinstance(item, Mapping):
            if not _zero_activity_mapping(item):
                return False
        elif isinstance(item, bool) or not isinstance(item, (int, float)) or item != 0:
            return False
    return True


def _zero_number(container: object, field: str) -> None:
    value = container.get(field) if isinstance(container, Mapping) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != 0:
        raise ClaudeQuotaError(f"/usage must report {field}=0")


def _parse_usage_text(text: str, observed: datetime) -> dict[str, dict[str, Any]]:
    matches = list(_TEXT_LINE.finditer(text))
    labels = [match.group("label") for match in matches]
    if (
        labels[:2] != list(_BASE_TEXT_LABELS)
        or len(labels) != 3
        or labels[2] not in MODEL_POOL_LABELS
    ):
        raise ClaudeQuotaError("Claude /usage display labels are missing, duplicated, or reordered")
    pools: dict[str, dict[str, Any]] = {}
    names = (*BASE_POOL_NAMES, MODEL_POOL_LABELS[labels[2]])
    for name, match in zip(names, matches):
        used = int(match.group("used"))
        if not 0 <= used <= 100:
            raise ClaudeQuotaError(f"quota pool {name} displayed utilization is invalid")
        reset = match.group("reset")
        if name == "five_hour" and reset is None and used == 0:
            window, precision, fingerprint = "five_hour:idle", "idle", "idle"
        elif reset is None:
            raise ClaudeQuotaError(f"quota pool {name} reset is missing")
        else:
            window, precision = (
                _five_hour_window_id(reset, observed)
                if name == "five_hour"
                else _week_window_id(name, reset, observed)
            )
            fingerprint = reset
        pools[name] = {"displayed_used_percent": used, "remaining_lower_bound": max(0, 99 - used), "window_id": window, "reset_precision": precision, "reset_fingerprint": fingerprint}
    return pools


def _validated_pools(raw: object) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise ClaudeQuotaError("Claude quota producer must contain all three pools")
    model_pools = set(raw).intersection(MODEL_POOL_LABELS.values())
    if set(raw) != set(BASE_POOL_NAMES).union(model_pools) or len(model_pools) != 1:
        raise ClaudeQuotaError("Claude quota producer must contain all three pools")
    names = (*BASE_POOL_NAMES, *sorted(model_pools))
    pools: dict[str, dict[str, Any]] = {}
    for name in names:
        value = raw[name]
        if not isinstance(value, Mapping):
            raise ClaudeQuotaError(f"quota pool {name} is invalid")
        used, remaining, window, precision, fingerprint = value.get("displayed_used_percent"), value.get("remaining_lower_bound"), value.get("window_id"), value.get("reset_precision"), value.get("reset_fingerprint")
        if not isinstance(used, int) or not 0 <= used <= 100 or remaining != max(0, 99 - used):
            raise ClaudeQuotaError(f"quota pool {name} bounds are invalid")
        expected_prefix = "five_hour:" if name == "five_hour" else "weekly:"
        allowed_precision = {"precise", "idle"} if name == "five_hour" else {"precise", "date-only-compatible"}
        if not isinstance(window, str) or not window.startswith(expected_prefix) or precision not in allowed_precision:
            raise ClaudeQuotaError(f"quota pool {name} reset metadata is invalid")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ClaudeQuotaError(f"quota pool {name} reset fingerprint is invalid")
        pools[name] = {"displayed_used_percent": used, "remaining_lower_bound": remaining, "window_id": window, "reset_precision": precision, "reset_fingerprint": fingerprint}
    if pools["five_hour"]["reset_precision"] not in {"precise", "idle"}:
        raise ClaudeQuotaError("five-hour reset must be precise or explicitly idle")
    model_pool = next(iter(model_pools))
    if pools["seven_day"]["window_id"] != pools[model_pool]["window_id"]:
        raise ClaudeQuotaError("weekly quota pools must share one reset window")
    if pools["seven_day"]["reset_fingerprint"] != pools[model_pool]["reset_fingerprint"]:
        raise ClaudeQuotaError("weekly quota pools must share one reset fingerprint")
    return pools


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as error:
        raise ClaudeQuotaError("Claude /usage reset timezone is invalid") from error


def _five_hour_window_id(value: str, observed: datetime) -> tuple[str, str]:
    match = _TIME_RESET.fullmatch(value)
    dated_clock = _DATE_TIME_RESET.fullmatch(value)
    if match is None and dated_clock is None:
        raise ClaudeQuotaError("five-hour reset is not a supported clock time")
    active = match or dated_clock
    assert active is not None
    hour, minute = int(active.group("hour")), int(active.group("minute") or 0)
    if not 1 <= hour <= 12 or minute > 59:
        raise ClaudeQuotaError("five-hour reset clock is invalid")
    if active.group("ampm").lower() == "pm" and hour != 12:
        hour += 12
    if active.group("ampm").lower() == "am" and hour == 12:
        hour = 0
    if dated_clock is None:
        candidate, local_observed, _zone_info = _clock_reset_datetime(
            hour,
            minute,
            active.group("zone"),
            observed,
        )
    else:
        zone = _zone(dated_clock.group("zone"))
        local_observed = observed.astimezone(zone)
        try:
            month = datetime.strptime(dated_clock.group("month"), "%b").month
            year = (
                int(dated_clock.group("year"))
                if dated_clock.group("year")
                else local_observed.year
            )
            candidate = datetime(
                year,
                month,
                int(dated_clock.group("day")),
                hour,
                minute,
                tzinfo=zone,
            )
        except ValueError as error:
            raise ClaudeQuotaError("five-hour reset date is invalid") from error
        if candidate <= local_observed:
            raise ClaudeQuotaError("five-hour reset must be in the future")
    if candidate - local_observed > timedelta(hours=24):
        raise ClaudeQuotaError("five-hour reset is not within the next 24 hours")
    return f"five_hour:{candidate.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}", "precise"


def _clock_reset_datetime(hour: int, minute: int, zone_name: str, observed: datetime) -> tuple[datetime, datetime, ZoneInfo]:
    zone = _zone(zone_name)
    local_observed = observed.astimezone(zone)
    candidate = local_observed.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_observed:
        candidate += timedelta(days=1)
    return candidate, local_observed, zone


def _week_window_id(name: str, value: str, observed: datetime) -> tuple[str, str]:
    clock = _TIME_RESET.fullmatch(value)
    if clock is not None:
        hour, minute = int(clock.group("hour")), int(clock.group("minute") or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise ClaudeQuotaError("weekly reset clock is invalid")
        if clock.group("ampm").lower() == "pm" and hour != 12:
            hour += 12
        if clock.group("ampm").lower() == "am" and hour == 12:
            hour = 0
        candidate, _local_observed, zone = _clock_reset_datetime(hour, minute, clock.group("zone"), observed)
        return f"weekly:{candidate.date().isoformat()}@{zone.key}", "date-only-compatible"
    dated_clock = _DATE_TIME_RESET.fullmatch(value)
    if dated_clock is not None:
        zone = _zone(dated_clock.group("zone"))
        local_observed = observed.astimezone(zone)
        hour = int(dated_clock.group("hour"))
        minute = int(dated_clock.group("minute") or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise ClaudeQuotaError("weekly reset clock is invalid")
        if dated_clock.group("ampm").lower() == "pm" and hour != 12:
            hour += 12
        if dated_clock.group("ampm").lower() == "am" and hour == 12:
            hour = 0
        try:
            month = datetime.strptime(dated_clock.group("month"), "%b").month
            year = int(dated_clock.group("year")) if dated_clock.group("year") else local_observed.year
            candidate = datetime(
                year,
                month,
                int(dated_clock.group("day")),
                hour,
                minute,
                tzinfo=zone,
            )
            if dated_clock.group("year") is None and candidate <= local_observed:
                candidate = candidate.replace(year=year + 1)
        except ValueError as error:
            raise ClaudeQuotaError("weekly reset date is invalid") from error
        if candidate - local_observed <= timedelta(hours=24):
            raise ClaudeQuotaError("weekly reset must be more than 24 hours away")
        return f"weekly:{candidate.date().isoformat()}@{zone.key}", "precise"
    match = _DATE_RESET.fullmatch(value)
    if match is None:
        raise ClaudeQuotaError("weekly reset is not a supported date")
    zone = _zone(match.group("zone"))
    local_observed = observed.astimezone(zone)
    try:
        month = datetime.strptime(match.group("month"), "%b").month
        year = int(match.group("year")) if match.group("year") else local_observed.year
        candidate = datetime(year, month, int(match.group("day")), tzinfo=zone)
        if match.group("year") is None and candidate.date() <= local_observed.date():
            candidate = candidate.replace(year=year + 1)
    except ValueError as error:
        raise ClaudeQuotaError("weekly reset date is invalid") from error
    if candidate - local_observed <= timedelta(hours=24):
        raise ClaudeQuotaError("weekly reset must be more than 24 hours away")
    return f"weekly:{candidate.date().isoformat()}@{zone.key}", "date-only-compatible"
