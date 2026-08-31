from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import threading
from typing import Protocol

from .claude_quota import COMPATIBLE_SOURCE, validate_producer_snapshot
from .model import QuotaSnapshot, canonical_hash
from .store import WorkbenchStore


class QuotaSnapshotAdapter(Protocol):
    def read(self) -> QuotaSnapshot | None: ...


@dataclass(frozen=True)
class JsonFileQuotaAdapter:
    """Read a local usage export without invoking Claude or using an API key."""

    path: Path
    source: str = "settings-usage-export"

    def read(self) -> QuotaSnapshot | None:
        if not self.path.is_file():
            return None
        raw = json.loads(self.path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("Claude quota snapshot must be a JSON object")
        if raw.get("producer") is not None or raw.get("producer_schema_version") is not None:
            return self._read_producer_snapshot(raw)
        observed_at = str(
            raw.get("observed_at")
            or datetime.fromtimestamp(self.path.stat().st_mtime, UTC).isoformat(timespec="seconds")
        )
        snapshot = QuotaSnapshot(
            observed_at=observed_at,
            auth_ok=raw.get("auth_ok") is True,
            auth_method=str(raw.get("auth_method", "none")),
            five_hour_remaining=_percentage(raw.get("five_hour_remaining")),
            weekly_all_remaining=_percentage(raw.get("weekly_all_remaining")),
            weekly_sonnet_remaining=_percentage(raw.get("weekly_sonnet_remaining")),
            weekly_fable_remaining=_percentage(raw.get("weekly_fable_remaining")),
            source=self.source,
            five_hour_window_id=_optional_text(raw.get("five_hour_window_id")),
            weekly_window_id=_optional_text(raw.get("weekly_window_id")),
        )
        snapshot.validate()
        return snapshot

    def _read_producer_snapshot(self, raw: object) -> QuotaSnapshot:
        producer = validate_producer_snapshot(raw)
        pools = producer.get("pools", {})
        if producer["auth_ok"] is False:
            snapshot = QuotaSnapshot(
                observed_at=str(producer["observed_at"]),
                auth_ok=False,
                auth_method="none",
                five_hour_remaining=None,
                weekly_all_remaining=None,
                weekly_sonnet_remaining=None,
                weekly_fable_remaining=None,
                source=COMPATIBLE_SOURCE,
                five_hour_window_id=None,
                weekly_window_id=None,
                producer=str(producer["producer"]),
                producer_schema_version=int(producer["producer_schema_version"]),
                claude_version=str(producer["claude_version"]),
            )
            snapshot.validate()
            return snapshot
        five_hour = pools["five_hour"]
        seven_day = pools["seven_day"]
        sonnet = pools["seven_day_sonnet"]
        snapshot = QuotaSnapshot(
            observed_at=str(producer["observed_at"]),
            auth_ok=True,
            auth_method="native-subscription",
            five_hour_remaining=float(five_hour["remaining_lower_bound"]),
            weekly_all_remaining=float(seven_day["remaining_lower_bound"]),
            weekly_sonnet_remaining=float(sonnet["remaining_lower_bound"]),
            weekly_fable_remaining=None,
            source=COMPATIBLE_SOURCE,
            five_hour_window_id=str(five_hour["window_id"]),
            weekly_window_id=str(seven_day["window_id"]),
            producer=str(producer["producer"]),
            producer_schema_version=int(producer["producer_schema_version"]),
            claude_version=str(producer["claude_version"]),
        )
        snapshot.validate()
        return snapshot


class QuotaRefresher:
    def __init__(
        self,
        store: WorkbenchStore,
        adapter: QuotaSnapshotAdapter,
        *,
        interval_seconds: float = 60,
    ):
        if interval_seconds <= 0:
            raise ValueError("quota refresh interval must be positive")
        self.store = store
        self.adapter = adapter
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._last_digest: str | None = None

    def refresh_once(self) -> bool:
        snapshot = self.adapter.read()
        if snapshot is None:
            return False
        digest = canonical_hash(snapshot.__dict__)
        if digest == self._last_digest:
            return False
        self.store.write_quota(snapshot)
        self._last_digest = digest
        return True

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.store.record_system_event(
                    "quota.refresh_failed",
                    {"error": f"{type(error).__name__}: {error}"},
                )
            self._stop.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()


def _percentage(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
