from __future__ import annotations

import json
import unittest

from codex_workbench.execution_attribution import (
    ATTESTED_IDENTITY_SOURCES,
    EXECUTION_ATTRIBUTION_SCHEMA_VERSION,
    FAILURE_ORIGINS,
    MAX_REFERENCES,
    AttributionReference,
    CandidateDecision,
    ExecutionAttribution,
    ExecutionCondition,
    ExecutionConditions,
    ExecutionStateReference,
    ExecutionTimings,
    FailureAttribution,
    IdentityProvenance,
    ObservedModelIdentity,
    PhaseTiming,
    PhysicalCallIdentity,
    RequestedModelIdentity,
    TimingBoundary,
    physical_call_usage_key,
)


RESPONSE_REF = AttributionReference(
    kind="artifact",
    ref="sha256:" + "a" * 64 + ":native-response.json",
)
EVENT_REF = AttributionReference(kind="event", ref="node-terminal", cursor=42)
NATIVE_PROVENANCE = IdentityProvenance(
    source="native_cli_response",
    references=(RESPONSE_REF, EVENT_REF),
    observed_at="2026-09-06T12:34:56+00:00",
)


def requested_identity() -> RequestedModelIdentity:
    return RequestedModelIdentity(
        provider="codex",
        model_id="gpt-5.6-terra",
        provenance=IdentityProvenance(
            source="routing_decision",
            references=(AttributionReference(kind="receipt", ref="routing-v3-decision"),),
        ),
    )


def complete_attribution(*, attempt: int = 1, call_id: str = "provider-call-7") -> ExecutionAttribution:
    return ExecutionAttribution(
        state=ExecutionStateReference(
            task_id="wb-sched-1",
            node_id="attribution-contract",
            attempt=attempt,
            event_cursor=42,
        ),
        requested_model=requested_identity(),
        observed_model=ObservedModelIdentity.attested(
            provider="codex",
            model_id="gpt-5.6-terra-2026-09-01",
            provenance=NATIVE_PROVENANCE,
        ),
        physical_call=PhysicalCallIdentity(
            status="attested",
            provider="codex",
            call_id=call_id,
            provenance=NATIVE_PROVENANCE,
        ),
        candidate_decisions=(
            CandidateDecision(
                candidate_id="codex:gpt-5.6-terra",
                disposition="selected",
                reasons=("capability policy admitted the exact candidate",),
                provider="codex",
                model_id="gpt-5.6-terra",
                references=(AttributionReference(kind="snapshot", ref="capability-catalog-9"),),
            ),
            CandidateDecision(
                candidate_id="claude:opus",
                disposition="rejected",
                reasons=("subscription quota gate did not admit the candidate",),
                provider="claude",
                model_id="opus",
                references=(AttributionReference(kind="quota_snapshot", ref="quota-42"),),
            ),
        ),
        failure=FailureAttribution(origin="unknown"),
        conditions=ExecutionConditions(
            dependency=ExecutionCondition(
                kind="dependency",
                status="observed",
                references=(AttributionReference(kind="dependency_report", ref="deps-42"),),
            ),
            scope=ExecutionCondition(
                kind="scope",
                status="observed",
                references=(
                    AttributionReference(kind="scope_contract", ref="task-contract-wb-sched-1"),
                ),
            ),
            provider_quota=ExecutionCondition(
                kind="provider_quota",
                status="observed",
                references=(AttributionReference(kind="quota_snapshot", ref="quota-42"),),
            ),
            environment_readiness=ExecutionCondition(
                kind="environment_readiness",
                status="observed",
                references=(AttributionReference(kind="readiness_report", ref="readiness-42"),),
            ),
            cpu_wait=ExecutionCondition(kind="cpu_wait"),
            memory_wait=ExecutionCondition(kind="memory_wait"),
            io_wait=ExecutionCondition(kind="io_wait"),
        ),
        timings=ExecutionTimings(
            queue=PhaseTiming(
                started=TimingBoundary("2026-09-06T12:00:00+00:00"),
                finished=TimingBoundary("2026-09-06T12:00:02+00:00"),
                duration_ms=2000,
            ),
            prepare=PhaseTiming(
                started=TimingBoundary("2026-09-06T12:00:02+00:00"),
                finished=TimingBoundary("2026-09-06T12:00:04+00:00"),
                duration_ms=2000,
            ),
            execute=PhaseTiming(
                started=TimingBoundary("2026-09-06T12:00:04+00:00"),
                finished=TimingBoundary("2026-09-06T12:01:04+00:00"),
                duration_ms=60000,
            ),
            verify=PhaseTiming(
                started=TimingBoundary("2026-09-06T12:01:04+00:00"),
                finished=TimingBoundary("2026-09-06T12:01:08+00:00"),
                duration_ms=4000,
            ),
        ),
        references=(
            AttributionReference(kind="scope_contract", ref="task-contract-wb-sched-1"),
        ),
    )


class ExecutionAttributionTests(unittest.TestCase):
    def test_requested_identity_never_becomes_an_observed_model(self) -> None:
        attribution = ExecutionAttribution(
            state=ExecutionStateReference("task-1", "work", 1),
            requested_model=requested_identity(),
        )

        payload = attribution.to_dict()
        self.assertEqual(payload["requested_model"]["status"], "requested")
        self.assertEqual(payload["requested_model"]["model_id"], "gpt-5.6-terra")
        self.assertEqual(payload["observed_model"], {
            "status": "unknown",
            "provider": None,
            "model_id": None,
            "provenance": {"source": "unknown", "references": [], "observed_at": None},
        })
        self.assertIsNone(payload["physical_call"]["usage_deduplication_key"])

        with self.assertRaisesRegex(ValueError, "unknown observed_model"):
            ObservedModelIdentity(status="unknown", provider="codex", model_id="gpt-5.6-terra")

    def test_attested_and_unattested_identity_preserve_provenance(self) -> None:
        observed = ObservedModelIdentity.attested(
            provider="codex",
            model_id="gpt-5.6-terra-2026-09-01",
            provenance=NATIVE_PROVENANCE,
        )
        self.assertEqual(observed.status, "attested")
        self.assertEqual(observed.to_dict()["provenance"]["source"], "native_cli_response")
        self.assertEqual(observed.to_dict()["provenance"]["references"][0], RESPONSE_REF.to_dict())
        self.assertIn("native_cli_response", ATTESTED_IDENTITY_SOURCES)

        legacy = ObservedModelIdentity.unattested(
            provider="codex",
            model_id="gpt-5.6-terra",
            provenance=IdentityProvenance(source="legacy_result"),
        )
        self.assertEqual(legacy.status, "unattested")
        self.assertEqual(legacy.to_dict()["provenance"]["observed_at"], None)

        with self.assertRaisesRegex(ValueError, "direct receipt/native-CLI"):
            ObservedModelIdentity(
                status="attested",
                provider="codex",
                model_id="gpt-5.6-terra",
                provenance=IdentityProvenance(source="worker_result", references=(EVENT_REF,)),
            )

    def test_references_are_bounded_identifiers_not_transcript_copies(self) -> None:
        with self.assertRaisesRegex(ValueError, "bounded identifier"):
            AttributionReference(kind="artifact", ref="a transcript line is not a reference")
        with self.assertRaisesRegex(ValueError, "32-item bound"):
            ExecutionAttribution(
                state=ExecutionStateReference("task-1", "work", 1),
                requested_model=requested_identity(),
                references=tuple(
                    AttributionReference(kind="event", ref=f"event-{index}")
                    for index in range(MAX_REFERENCES + 1)
                ),
            )

        record = complete_attribution().to_dict()
        self.assertEqual(record["references"], [
            {"kind": "scope_contract", "ref": "task-contract-wb-sched-1", "cursor": None}
        ])
        self.assertNotIn("transcript", json.dumps(record))

    def test_selected_and_rejected_candidates_both_require_reasons(self) -> None:
        attribution = complete_attribution()
        decisions = attribution.to_dict()["candidate_decisions"]
        self.assertEqual([item["disposition"] for item in decisions], ["selected", "rejected"])
        self.assertTrue(all(item["reasons"] for item in decisions))
        self.assertEqual(decisions[1]["references"][0]["kind"], "quota_snapshot")

        with self.assertRaisesRegex(ValueError, "must explain"):
            CandidateDecision(
                candidate_id="codex:gpt-5.6-luna",
                disposition="rejected",
                reasons=(),
            )

    def test_failure_origin_taxonomy_is_explicit_and_does_not_infer_from_detail(self) -> None:
        self.assertEqual(
            FAILURE_ORIGINS,
            frozenset(
                {
                    "environment",
                    "auth",
                    "quota",
                    "transport",
                    "model",
                    "verification",
                    "tooling_bug",
                    "scope",
                    "cancel",
                    "unknown",
                }
            ),
        )
        for origin in sorted(FAILURE_ORIGINS):
            self.assertEqual(FailureAttribution(origin=origin).to_dict()["origin"], origin)
        self.assertEqual(
            FailureAttribution(detail="the caller did not classify this failure").origin,
            "unknown",
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            FailureAttribution(origin="dependency")  # type: ignore[arg-type]

    def test_missing_timing_boundaries_remain_explicitly_unknown(self) -> None:
        timings = ExecutionTimings()
        serialized = timings.to_dict()
        for phase in ("queue", "prepare", "execute", "verify"):
            self.assertEqual(serialized[phase]["status"], "unknown")
            self.assertEqual(serialized[phase]["started"], {"status": "unknown", "at": None})
            self.assertEqual(serialized[phase]["finished"], {"status": "unknown", "at": None})
            self.assertIsNone(serialized[phase]["duration_ms"])

        partial = PhaseTiming(started=TimingBoundary("2026-09-06T12:00:00+00:00"))
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.to_dict()["finished"], {"status": "unknown", "at": None})

    def test_operational_conditions_keep_waits_unknown_until_measured(self) -> None:
        conditions = ExecutionConditions().to_dict()
        self.assertEqual(
            set(conditions),
            {
                "dependency",
                "scope",
                "provider_quota",
                "environment_readiness",
                "cpu_wait",
                "memory_wait",
                "io_wait",
            },
        )
        for condition in conditions.values():
            self.assertEqual(condition["status"], "unknown")
            self.assertIsNone(condition["duration_ms"])

        measured_io_wait = ExecutionCondition(
            kind="io_wait",
            status="observed",
            duration_ms=35,
            references=(AttributionReference(kind="event", ref="resource-sample", cursor=44),),
        )
        self.assertEqual(measured_io_wait.to_dict()["duration_ms"], 35)
        with self.assertRaisesRegex(ValueError, "measured duration"):
            ExecutionCondition(
                kind="memory_wait",
                status="observed",
                references=(EVENT_REF,),
            )
        with self.assertRaisesRegex(ValueError, "only CPU, memory, and I/O"):
            ExecutionCondition(kind="provider_quota", duration_ms=1)

    def test_physical_call_key_is_stable_and_unknown_calls_are_not_deduplicated(self) -> None:
        first = complete_attribution(attempt=1, call_id="provider-call-7")
        retry = complete_attribution(attempt=2, call_id="provider-call-7")
        different = complete_attribution(attempt=3, call_id="provider-call-8")

        self.assertEqual(
            physical_call_usage_key(first.physical_call),
            physical_call_usage_key(retry.physical_call),
        )
        self.assertNotEqual(
            first.physical_call.usage_deduplication_key,
            different.physical_call.usage_deduplication_key,
        )
        self.assertIsNone(physical_call_usage_key(PhysicalCallIdentity.unknown(provider="codex")))

    def test_json_round_trip_is_typed_and_serialization_compatible(self) -> None:
        original = complete_attribution()
        payload = original.to_dict()
        encoded = original.to_json()

        self.assertEqual(payload["schema_version"], EXECUTION_ATTRIBUTION_SCHEMA_VERSION)
        self.assertEqual(json.loads(encoded), payload)
        self.assertEqual(ExecutionAttribution.from_dict(payload), original)
        self.assertEqual(ExecutionAttribution.from_json(encoded), original)

        tampered = json.loads(encoded)
        tampered["physical_call"]["usage_deduplication_key"] = "physical-call-v1:wrong"
        with self.assertRaisesRegex(ValueError, "not bound"):
            ExecutionAttribution.from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
