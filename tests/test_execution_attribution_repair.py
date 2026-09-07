from __future__ import annotations

from dataclasses import replace
import unittest

from codex_workbench.execution_attribution import (
    AttributionReference,
    ExecutionAttribution,
    ExecutionStateReference,
    IdentityProvenance,
    ObservedModelIdentity,
    PhysicalCallIdentity,
    RequestedModelIdentity,
)
from codex_workbench.executors import ExecutionRequest
from codex_workbench.model import NodeResult
from codex_workbench.service import Coordinator, _ExecutionAttributionContext


class CoordinatorAttributionRepairTests(unittest.TestCase):
    def _claimed(self, attempt: int = 2) -> dict:
        return {
            "task_id": "fallback-task",
            "node_id": "work",
            "attempt": attempt,
            "spec": {"executor": "claude", "model": "sonnet"},
        }

    @staticmethod
    def _fallback_request(attempt: int = 2) -> ExecutionRequest:
        return ExecutionRequest(
            task_id="fallback-task",
            node_id="work",
            attempt=attempt,
            contract={},
            spec={"executor": "codex", "model": "gpt-5.6-luna"},
            worktree=None,
        )

    @staticmethod
    def _direct_attribution(attempt: int = 2) -> ExecutionAttribution:
        receipt = AttributionReference(kind="artifact", ref="provider-receipt-1")
        provenance = IdentityProvenance(
            source="provider_receipt", references=(receipt,)
        )
        return ExecutionAttribution(
            state=ExecutionStateReference("fallback-task", "work", attempt, 29),
            requested_model=RequestedModelIdentity(
                provider="codex", model_id="gpt-5.6-luna"
            ),
            observed_model=ObservedModelIdentity.attested(
                provider="codex",
                model_id="gpt-5.6-luna",
                provenance=provenance,
            ),
            physical_call=PhysicalCallIdentity(
                status="attested",
                provider="codex",
                call_id="provider-call-1",
                provenance=provenance,
            ),
        )

    def _attach(
        self,
        result: NodeResult,
        *,
        request: ExecutionRequest | None,
        claimed: dict,
        direct_artifact_refs: frozenset[str] | None = None,
    ) -> NodeResult:
        # This boundary method does not need a running executor or store; the
        # integration test exercises that durable path separately.
        coordinator = object.__new__(Coordinator)
        return coordinator._attach_execution_attribution(
            claimed=claimed,
            request=request,
            result=result,
            context=_ExecutionAttributionContext(
                claim_event_cursor=29,
                direct_artifact_refs=(
                    direct_artifact_refs
                    if direct_artifact_refs is not None
                    else frozenset(
                        ref for ref in result.artifacts.values() if isinstance(ref, str) and ref
                    )
                ),
            ),
        )

    def test_direct_fallback_evidence_survives_enrichment_once(self) -> None:
        supplied = self._direct_attribution()
        result = self._attach(
            NodeResult(
                status="succeeded",
                summary="fallback completed",
                artifacts={"provider-receipt": "provider-receipt-1"},
                actual_model="gpt-5.6-luna",
                provider="codex",
                execution_attribution=supplied.to_dict(),
            ),
            request=self._fallback_request(),
            claimed=self._claimed(),
        )

        attribution = ExecutionAttribution.from_dict(result.execution_attribution)
        self.assertEqual(attribution.observed_model, supplied.observed_model)
        self.assertEqual(attribution.physical_call, supplied.physical_call)
        self.assertEqual(
            attribution.physical_call.usage_deduplication_key,
            supplied.physical_call.usage_deduplication_key,
        )

    def test_legacy_or_prior_attempt_evidence_cannot_be_promoted_or_reused(self) -> None:
        legacy = self._attach(
            NodeResult(
                status="succeeded",
                summary="legacy fixture completed",
                artifacts={"structured-result": "legacy-structured-result"},
                actual_model="gpt-5.6-luna",
                provider="codex",
            ),
            request=self._fallback_request(),
            claimed=self._claimed(),
        )
        legacy_attribution = ExecutionAttribution.from_dict(legacy.execution_attribution)
        self.assertEqual(legacy_attribution.observed_model.status, "unattested")
        self.assertEqual(legacy_attribution.physical_call.status, "unknown")

        prior_attempt = self._attach(
            NodeResult(
                status="succeeded",
                summary="retry completed",
                artifacts={"provider-receipt": "provider-receipt-1"},
                actual_model="gpt-5.6-luna",
                provider="codex",
                execution_attribution=self._direct_attribution(attempt=1).to_dict(),
            ),
            request=self._fallback_request(),
            claimed=self._claimed(attempt=2),
        )
        prior_attribution = ExecutionAttribution.from_dict(prior_attempt.execution_attribution)
        self.assertEqual(prior_attribution.observed_model.status, "unattested")
        self.assertEqual(prior_attribution.physical_call.status, "unknown")

    def test_retry_recovery_artifacts_cannot_promote_identity_and_current_receipts_survive(self) -> None:
        supplied = self._direct_attribution()
        result = NodeResult(
            status="succeeded",
            summary="retry completed",
            artifacts={
                "provider-receipt": "provider-receipt-1",
                "failed-attempt-recovery": "recovery-receipt-1",
            },
            actual_model="gpt-5.6-luna",
            provider="codex",
            execution_attribution=supplied.to_dict(),
        )
        preserved = self._attach(
            result,
            request=self._fallback_request(),
            claimed=self._claimed(),
            direct_artifact_refs=frozenset({"provider-receipt-1"}),
        )
        preserved_attribution = ExecutionAttribution.from_dict(
            preserved.execution_attribution
        )
        self.assertEqual(preserved_attribution.observed_model.status, "attested")
        self.assertEqual(preserved_attribution.physical_call.status, "attested")

        recovery_ref = AttributionReference(kind="artifact", ref="recovery-receipt-1")
        recovery_provenance = IdentityProvenance(
            source="provider_receipt", references=(recovery_ref,)
        )
        stale = ExecutionAttribution(
            state=ExecutionStateReference("fallback-task", "work", 2, 29),
            requested_model=RequestedModelIdentity(
                provider="codex", model_id="gpt-5.6-luna"
            ),
            observed_model=ObservedModelIdentity.attested(
                provider="codex",
                model_id="gpt-5.6-luna",
                provenance=recovery_provenance,
            ),
            physical_call=PhysicalCallIdentity(
                status="attested",
                provider="codex",
                call_id="recovery-call-1",
                provenance=recovery_provenance,
            ),
        )
        rejected = self._attach(
            replace(result, execution_attribution=stale.to_dict()),
            request=self._fallback_request(),
            claimed=self._claimed(),
            direct_artifact_refs=frozenset({"provider-receipt-1"}),
        )
        rejected_attribution = ExecutionAttribution.from_dict(rejected.execution_attribution)
        self.assertEqual(rejected_attribution.observed_model.status, "unattested")
        self.assertEqual(rejected_attribution.physical_call.status, "unknown")


if __name__ == "__main__":
    unittest.main()
