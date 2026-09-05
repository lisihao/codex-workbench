from pathlib import Path
import tempfile
import unittest

from ai_frontier_provider import AIFrontierRegistry
from codex_workbench.ai_frontier import WorkbenchAIFrontier
from codex_workbench.model import now_iso
from codex_workbench.performance import PerformanceRegistry, build_performance_snapshot, _prior_for
from tests.test_performance import event, result, task


class PerformanceIdentityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.receipt = root / "authorization.json"
        provider = AIFrontierRegistry(root / "source")
        provider.consent_personal_use(self.receipt)
        source = "anthropic/claude-opus-4-8"
        imported = provider.import_payloads({
            "reliability_leaderboard": [{"Executor": source, "Quality": 0.8, "Cost": 1.0, "Consistency": 0.9}],
            "cost_comparison": [{"LLMs": source, "Real Cost": 1.0, "Quoted Cost": 1.0, "Cost Surprise": 0.0}],
            "model_benchmarks": {source: {"categories": [{"id": "reasoning", "label": "reasoning", "quality": 0.85, "cost": 1.0}]}},
        }, self.receipt)
        self.assertTrue(imported["ok"])
        self.status = WorkbenchAIFrontier(root / "source", self.receipt).status()
        self.catalog = {
            "catalog_id": "catalog-identity-integration", "digest": "a" * 64,
            "agents": {"claude": {"cli_version": "2.1.239"}},
            "models": [{"provider": "claude", "model_id": "opus", "model_family": "opus", "routable": True,
                "agent_cli_version": "2.1.239", "reasoning": {"preferred_effort": "max"},
                "identity": {"kind": "cli-alias", "selection_id": "opus", "canonical_model_id": None}}],
        }
        self.tasks = [task("t", executor="claude", model="opus", task_type="architecture", complexity="high", reasoning_effort="max")]
        self.events = [
            event(1, "node.started", task_id="t", node_id="work", created_at=now_iso(),
                payload={"attempt": 1, "executor": "claude", "model": "opus", "model_reasoning_effort": "max"}),
            event(2, "node.accepted", task_id="t", node_id="work", created_at=now_iso(),
                payload={"attempt": 1, "result": result("accepted", "claude-opus-4-8", provider="claude", agent_version="2.1.239")}),
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_unbound_source_reports_zero_actual_coverage(self):
        snapshot = build_performance_snapshot([], [], self.catalog, ai_frontier_status=self.status)
        source = snapshot["source_provenance"]["external_priors"]["ai_frontier"]
        self.assertTrue(source["routing_prior_eligible"])
        self.assertEqual(source["imported_record_count"], 0)
        self.assertEqual(source["model_coverage_rate"], 0)
        self.assertFalse(source["used_for_prior"])

    def test_attested_alias_joins_source_and_exact_runtime_bucket(self):
        snapshot = build_performance_snapshot(self.events, self.tasks, self.catalog, ai_frontier_status=self.status)
        source = snapshot["source_provenance"]["external_priors"]["ai_frontier"]
        self.assertEqual(source["matched_selection_ids"], ["opus"])
        self.assertGreater(source["imported_record_count"], 0)
        calibration = PerformanceRegistry._calibrate_context(snapshot, self.catalog, snapshot["baseline"], task_type="architecture", complexity="high")
        candidate = calibration["candidates"][0]
        self.assertEqual(candidate["model_id"], "opus")
        self.assertEqual(candidate["canonical_model_id"], "claude-opus-4-8")
        self.assertEqual(candidate["quality"]["posterior"]["runtime_sample_count"], 1)
        self.assertTrue(any(item["record_id"].startswith("ai-frontier-") for item in candidate["quality"]["prior"]["evidence"]))

    def test_external_exact_model_does_not_leak_through_family_fallback(self):
        snapshot = build_performance_snapshot(self.events, self.tasks, self.catalog, ai_frontier_status=self.status)
        prior = _prior_for(snapshot["baseline"], provider="claude", model_id="claude-opus-4-6", model_family="opus", task_type="architecture", reasoning_effort="max")
        self.assertFalse(any(item["record_id"].startswith("ai-frontier-") for item in prior["evidence"]))


if __name__ == "__main__":
    unittest.main()
