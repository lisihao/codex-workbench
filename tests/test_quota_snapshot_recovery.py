from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from codex_workbench.claude_quota import (
    COMPATIBLE_SOURCE,
    PRODUCER,
    PRODUCER_SCHEMA_VERSION,
    SUPPORTED_USAGE_VERSION,
)
from codex_workbench.model import NodeSpec, QuotaSnapshot, TaskContract, canonical_json
from codex_workbench.quota import JsonFileQuotaAdapter, QuotaRefresher
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore
from codex_workbench.submission import _public_compiled_result


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _SequenceAdapter:
    def __init__(self, *snapshots: QuotaSnapshot) -> None:
        self.snapshots = list(snapshots)

    def read(self) -> QuotaSnapshot | None:
        return self.snapshots.pop(0) if self.snapshots else None


class QuotaSnapshotRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "state.sqlite"
        self.store = WorkbenchStore(self.database)
        self.store.initialize()
        self.base = datetime.now(UTC).replace(microsecond=0)
        self.five_hour_reset = self.base + timedelta(hours=2)
        self.weekly_reset = self.base + timedelta(days=2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _windows(
        self,
        *,
        five_hour_reset: datetime | None = None,
        weekly_reset: datetime | None = None,
    ) -> dict[str, str]:
        return {
            "five_hour_window_id": f"five_hour:{_iso(five_hour_reset or self.five_hour_reset)}",
            "weekly_window_id": f"weekly:{(weekly_reset or self.weekly_reset).date().isoformat()}@UTC",
        }

    def _complete(self, observed_at: datetime, **overrides: object) -> QuotaSnapshot:
        values: dict[str, object] = {
            "observed_at": _iso(observed_at),
            "auth_ok": True,
            "auth_method": "native-subscription",
            "five_hour_remaining": 98.0,
            "weekly_all_remaining": 99.0,
            "weekly_sonnet_remaining": None,
            "weekly_fable_remaining": 99.0,
            "source": COMPATIBLE_SOURCE,
            "producer": PRODUCER,
            "producer_schema_version": PRODUCER_SCHEMA_VERSION,
            "claude_version": SUPPORTED_USAGE_VERSION,
            **self._windows(),
        }
        values.update(overrides)
        return QuotaSnapshot(**values)  # type: ignore[arg-type]

    def _empty(self, observed_at: datetime, **overrides: object) -> QuotaSnapshot:
        values = self._complete(observed_at).raw_payload()
        values.update(
            {
                "five_hour_remaining": None,
                "weekly_all_remaining": None,
                "weekly_sonnet_remaining": None,
                "weekly_fable_remaining": None,
            }
        )
        values.update(overrides)
        return QuotaSnapshot(**values)

    def _latest(self, current: datetime, store: WorkbenchStore | None = None) -> QuotaSnapshot:
        snapshot = (store or self.store).latest_quota(
            max_age_seconds=900,
            current_time=current,
        )
        assert snapshot is not None
        return snapshot

    @staticmethod
    def _decision(snapshot: QuotaSnapshot, current: datetime) -> str:
        return snapshot.dispatch_decision(
            "sonnet",
            max_age_seconds=900,
            current_time=current,
        ).action

    def test_full_to_empty_recovers_exact_complete_row_and_reports_nonsecret_provenance(self) -> None:
        complete = self._complete(self.base, weekly_fable_remaining=None)
        empty = self._empty(self.base + timedelta(seconds=30))
        refresher = QuotaRefresher(self.store, _SequenceAdapter(complete, empty))

        self.assertTrue(refresher.refresh_once())
        complete_id = self.store.quota_snapshot_reference(complete)
        self.assertIsInstance(complete_id, int)
        self.assertTrue(refresher.refresh_once())
        empty_id = self.store.quota_snapshot_reference(empty)
        self.assertIsInstance(empty_id, int)

        effective = self._latest(self.base + timedelta(minutes=1))
        self.assertEqual(effective.ledger_id, complete_id)
        self.assertEqual(effective.observed_at, complete.observed_at)
        self.assertEqual(effective.five_hour_window_id, complete.five_hour_window_id)
        self.assertEqual(effective.weekly_window_id, complete.weekly_window_id)
        self.assertEqual(self.store.quota_snapshot_reference(effective), complete_id)
        self.assertEqual(self._decision(effective, self.base + timedelta(minutes=1)), "claude")

        receipt = effective.effective_observation
        assert receipt is not None
        self.assertEqual(receipt["selection"], "last-known-good")
        self.assertEqual(receipt["raw_snapshot_id"], empty_id)
        self.assertEqual(receipt["effective_snapshot_id"], complete_id)
        self.assertEqual(receipt["authentication"], "native-subscription-authenticated")
        self.assertEqual(receipt["quota_collection"], "missing")
        self.assertEqual(receipt["recovery_reason"], "authenticated-empty-recovered")
        self.assertFalse(receipt["admission_blocked"])
        self.assertEqual(receipt["effective_provenance"]["source"], COMPATIBLE_SOURCE)
        public = _public_compiled_result(
            {"ok": True, "nodes": (), "quota_observation": receipt}
        )
        self.assertEqual(
            public["quota_observation"]["authentication"],
            "native-subscription-authenticated",
        )

        raw = self.store.list_quota_snapshots()
        self.assertEqual(len(raw), 2)
        self.assertIsNone(raw[0]["five_hour_remaining"])
        self.assertNotIn("ledger_id", raw[0])
        self.assertNotIn("effective_observation", raw[0])

    def test_repeated_empty_rows_do_not_extend_complete_age_or_ttl(self) -> None:
        complete = self._complete(self.base)
        self.store.write_quota(complete)
        self.store.write_quota(self._empty(self.base + timedelta(seconds=30)))
        last_empty = self._empty(self.base + timedelta(seconds=60))
        self.store.write_quota(last_empty)

        current = self.base + timedelta(seconds=901)
        effective = self._latest(current)
        self.assertEqual(effective.observed_at, last_empty.observed_at)
        self.assertEqual(effective.observation_status(), "authenticated-empty")
        assert effective.effective_observation is not None
        self.assertEqual(
            effective.effective_observation["recovery_reason"],
            "complete-observation-stale",
        )
        self.assertEqual(self._decision(effective, current), "codex")

    def test_reset_expiry_changed_or_unknown_binding_fails_closed(self) -> None:
        reset = self.base + timedelta(minutes=1)
        complete = self._complete(self.base, **self._windows(five_hour_reset=reset))
        expired_empty = self._empty(
            self.base + timedelta(seconds=30),
            **self._windows(five_hour_reset=reset),
        )
        self.store.write_quota(complete)
        self.store.write_quota(expired_empty)
        expired = self._latest(self.base + timedelta(seconds=90))
        assert expired.effective_observation is not None
        self.assertEqual(
            expired.effective_observation["recovery_reason"],
            "complete-reset-binding-unknown-or-expired",
        )
        self.assertEqual(self._decision(expired, self.base + timedelta(seconds=90)), "codex")

        for label, empty, expected in (
            (
                "changed",
                self._empty(
                    self.base + timedelta(seconds=30),
                    **self._windows(five_hour_reset=self.five_hour_reset + timedelta(hours=1)),
                ),
                "reset-window-mismatch",
            ),
            (
                "unknown",
                self._empty(
                    self.base + timedelta(seconds=30),
                    five_hour_window_id=None,
                    weekly_window_id=None,
                ),
                "empty-reset-binding-unknown-or-expired",
            ),
        ):
            with self.subTest(label=label):
                store = WorkbenchStore(Path(self.temporary.name) / f"{label}.sqlite")
                store.initialize()
                store.write_quota(self._complete(self.base))
                store.write_quota(empty)
                effective = self._latest(self.base + timedelta(minutes=1), store)
                assert effective.effective_observation is not None
                self.assertEqual(effective.effective_observation["recovery_reason"], expected)
                self.assertEqual(self._decision(effective, self.base + timedelta(minutes=1)), "codex")

    def test_confirmed_collection_failure_keeps_healthy_auth_distinct_and_bounded(self) -> None:
        path = Path(self.temporary.name) / "collection-failure.json"
        path.write_text(
            json.dumps(
                {
                    "producer": PRODUCER,
                    "producer_schema_version": PRODUCER_SCHEMA_VERSION,
                    "source": COMPATIBLE_SOURCE,
                    "claude_version": SUPPORTED_USAGE_VERSION,
                    "observed_at": _iso(self.base + timedelta(seconds=30)),
                    "auth_ok": True,
                    "auth_method": "native-subscription",
                    "quota_ok": False,
                    "error": "fixture parse failure",
                }
            ),
            encoding="utf-8",
        )
        failed = JsonFileQuotaAdapter(path).read()
        assert failed is not None
        self.assertTrue(failed.auth_ok)
        self.assertEqual(failed.collection_state, "failed")
        self.store.write_quota(self._complete(self.base))
        self.store.write_quota(failed)

        current = self.base + timedelta(minutes=1)
        effective = self._latest(current)
        self.assertEqual(effective.observed_at, _iso(self.base))
        assert effective.effective_observation is not None
        self.assertEqual(
            effective.effective_observation["authentication"],
            "native-subscription-authenticated",
        )
        self.assertEqual(effective.effective_observation["quota_collection"], "failed")
        self.assertEqual(self._decision(effective, current), "claude")

    def test_auth_revocation_and_incompatible_provenance_revoke_recovery(self) -> None:
        cases = (
            (
                "revoked",
                self._empty(
                    self.base + timedelta(seconds=30),
                    auth_ok=False,
                    auth_method="none",
                ),
                "authentication-unavailable",
            ),
            (
                "incompatible",
                self._empty(
                    self.base + timedelta(seconds=30),
                    source="different-passive-source",
                ),
                "incompatible-provenance",
            ),
        )
        for label, latest, expected in cases:
            with self.subTest(label=label):
                store = WorkbenchStore(Path(self.temporary.name) / f"{label}.sqlite")
                store.initialize()
                store.write_quota(self._complete(self.base))
                store.write_quota(latest)
                effective = self._latest(self.base + timedelta(minutes=1), store)
                self.assertEqual(effective.observed_at, latest.observed_at)
                assert effective.effective_observation is not None
                self.assertEqual(effective.effective_observation["recovery_reason"], expected)
                self.assertEqual(self._decision(effective, self.base + timedelta(minutes=1)), "codex")

    def test_partial_low_pool_is_authoritative_and_cannot_be_skipped_by_empty(self) -> None:
        complete = self._complete(self.base)
        partial_low = self._complete(
            self.base + timedelta(seconds=30),
            five_hour_remaining=0.0,
            weekly_all_remaining=None,
            weekly_sonnet_remaining=None,
            weekly_fable_remaining=None,
        )
        self.store.write_quota(complete)
        self.store.write_quota(partial_low)
        direct = self._latest(self.base + timedelta(minutes=1))
        self.assertEqual(direct.ledger_id, self.store.quota_snapshot_reference(partial_low))
        self.assertEqual(direct.quota_zone("sonnet")[0], "protected")
        self.assertEqual(self._decision(direct, self.base + timedelta(minutes=1)), "codex")

        self.store.write_quota(self._empty(self.base + timedelta(seconds=60)))
        after_empty = self._latest(self.base + timedelta(minutes=2))
        assert after_empty.effective_observation is not None
        self.assertEqual(
            after_empty.effective_observation["recovery_reason"],
            "intervening-partial-observation-authoritative",
        )
        self.assertEqual(self._decision(after_empty, self.base + timedelta(minutes=2)), "codex")

    def test_startup_optional_model_pool_and_invalid_time_remain_fail_closed(self) -> None:
        self.assertIsNone(self.store.latest_quota(max_age_seconds=900, current_time=self.base))

        shared_only = self._complete(
            self.base,
            weekly_sonnet_remaining=None,
            weekly_fable_remaining=None,
        )
        self.store.write_quota(shared_only)
        healthy = self._latest(self.base)
        self.assertEqual(self._decision(healthy, self.base), "claude")
        self.assertEqual(
            healthy.dispatch_decision("fable", max_age_seconds=900, current_time=self.base).action,
            "claude",
        )

        invalid = replace(
            self._complete(self.base + timedelta(seconds=30)),
            observed_at="not-a-time",
        )
        self.store.write_quota(invalid)
        unsafe = self._latest(self.base + timedelta(minutes=1))
        self.assertTrue(unsafe.effective_admission_blocked)
        self.assertEqual(self._decision(unsafe, self.base + timedelta(minutes=1)), "codex")

    def test_delayed_out_of_order_append_cannot_override_newer_safety_evidence(self) -> None:
        cases = (
            ("empty", self._empty(self.base + timedelta(seconds=30))),
            (
                "auth-revoked",
                self._empty(
                    self.base + timedelta(seconds=30), auth_ok=False, auth_method="none"
                ),
            ),
            (
                "exhausted",
                self._complete(
                    self.base + timedelta(seconds=30),
                    five_hour_remaining=0.0,
                    weekly_all_remaining=None,
                    weekly_sonnet_remaining=None,
                    weekly_fable_remaining=None,
                ),
            ),
        )
        for label, newer in cases:
            with self.subTest(label=label):
                store = WorkbenchStore(Path(self.temporary.name) / f"delayed-{label}.sqlite")
                store.initialize()
                # Simulate a concurrent delayed writer: newer evidence reaches
                # the ledger first, then an older complete row appends later.
                store.write_quota(newer)
                newer_id = store.quota_snapshot_reference(newer)
                store.write_quota(self._complete(self.base))
                effective = self._latest(self.base + timedelta(minutes=1), store)
                self.assertEqual(effective.ledger_id, newer_id)
                self.assertEqual(self._decision(effective, self.base + timedelta(minutes=1)), "codex")
                assert effective.effective_observation is not None
                if label == "empty":
                    self.assertEqual(
                        effective.effective_observation["recovery_reason"],
                        "out-of-order-observation",
                    )

    def test_same_timestamp_conflict_is_an_actual_admission_barrier(self) -> None:
        complete = self._complete(self.base)
        revoked = self._empty(self.base, auth_ok=False, auth_method="none")
        self.store.write_quota(complete)
        self.store.write_quota(revoked)

        effective = self._latest(self.base + timedelta(seconds=1))
        self.assertTrue(effective.effective_admission_blocked)
        self.assertEqual(self._decision(effective, self.base + timedelta(seconds=1)), "codex")
        assert effective.effective_observation is not None
        self.assertEqual(
            effective.effective_observation["recovery_reason"],
            "ambiguous-observation-order",
        )

    def test_selected_legacy_effective_row_resolves_exact_reference_and_route(self) -> None:
        complete = self._complete(self.base, weekly_fable_remaining=None)
        empty = self._empty(self.base + timedelta(seconds=30))
        legacy_complete = complete.raw_payload()
        legacy_empty = empty.raw_payload()
        legacy_complete.pop("weekly_fable_remaining")
        legacy_empty.pop("weekly_fable_remaining")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO quota_snapshots(provider, snapshot_json, observed_at) VALUES('claude', ?, ?)",
                (canonical_json(legacy_complete), complete.observed_at),
            )
            connection.execute(
                "INSERT INTO quota_snapshots(provider, snapshot_json, observed_at) VALUES('claude', ?, ?)",
                (canonical_json(legacy_empty), empty.observed_at),
            )

        effective = self._latest(self.base + timedelta(minutes=1))
        self.assertEqual(effective.ledger_id, 1)
        self.assertEqual(self.store.quota_snapshot_reference(effective), 1)

        epoch = self.store.activate_coordinator("quota-recovery", "fixture-machine")
        contract = TaskContract(
            task_id="quota-recovery-route",
            repository="/tmp/quota-recovery-fixture",
            base_sha="fixture",
            objective="preserve selected quota evidence",
            allowed_scope=("tests",),
        )
        nodes = [
            NodeSpec("work", contract.task_id, "work", "claude", "sonnet", "fixture"),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "fixture",
                "fixture",
                "accepted",
                depends_on=("work",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "quota-recovery-create")
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("quota-recovery-worker", epoch)
        assert claimed is not None
        self.store.record_node_route(
            contract.task_id,
            "work",
            executor="codex",
            model="gpt-5.6-luna",
            payload={"attempt": claimed["attempt"], "from": "claude", "to": "codex"},
            attempt=claimed["attempt"],
            coordinator_epoch=epoch,
            lease_epoch=claimed["lease_epoch"],
            quota_snapshot_id=1,
        )
        route = next(
            event
            for event in self.store.read_events(limit=20)
            if event["event_type"] == "node.routed"
        )
        self.assertEqual(route["payload"]["quota_snapshot_id"], 1)
        self.assertEqual(route["payload"]["quota_snapshot"]["observed_at"], complete.observed_at)

    def test_unresolved_effective_reference_is_fenced_before_claude_execution(self) -> None:
        self.store.write_quota(self._complete(self.base))
        self.store.write_quota(self._empty(self.base + timedelta(seconds=30)))
        effective = self._latest(self.base + timedelta(minutes=1))
        coordinator = Coordinator(
            self.store,
            Path(self.temporary.name) / "state",
            coordinator_epoch=1,
            max_workers=1,
        )
        try:
            unresolved = replace(effective, ledger_id=999_999)
            self.assertIsNone(coordinator._quota_snapshot_reference(unresolved))
            admitted = effective.dispatch_decision(
                "sonnet", max_age_seconds=900, current_time=self.base + timedelta(minutes=1)
            )
            self.assertEqual(admitted.action, "claude")
            fenced = coordinator._missing_quota_reference_decision(admitted)
            self.assertEqual(fenced.action, "codex")
        finally:
            coordinator._pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
