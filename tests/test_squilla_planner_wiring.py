from __future__ import annotations

import argparse
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.cli import command_init
from codex_workbench.config import SquillaAdvisorConfig, WorkbenchConfig
from codex_workbench.model import NodeSpec, TaskContract, canonical_json
from codex_workbench.planner import CodexPlanner, PlannerError
from codex_workbench.squilla_advisor import SquillaAdvice, SquillaAdvisor


def make_contract(*, complexity: str = "standard") -> TaskContract:
    return TaskContract(
        task_id="squilla-plan",
        repository="/tmp/squilla-plan",
        base_sha="abc123",
        objective="bounded implementation",
        allowed_scope=("src", "tests"),
        complexity=complexity,  # type: ignore[arg-type]
        parallelizable=True,
    )


def plan_node(
    node_id: str,
    *,
    complexity: str,
    verifier: bool = False,
    write_scope: str = "src/worker",
    executor: str = "codex",
    model: str = "gpt-5.6-luna",
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "title": f"title {node_id}",
        "executor": executor,
        "model": model,
        "prompt": f"original prompt {node_id}",
        "command": ["true"] if executor == "deterministic" else [],
        "depends_on": [],
        "read_scopes": [write_scope] if write_scope else ["src"],
        "write_scopes": [write_scope] if write_scope else [],
        "verifier": verifier,
        "routing_strategy": "model-routing-v2",
        "task_type": "implementation",
        "complexity": complexity,
        "parallelizable": True,
        "claude_allowed": False,
    }


def make_plan(*workers: dict[str, object]) -> dict[str, object]:
    worker_ids = tuple(str(worker["node_id"]) for worker in workers)
    verifier = plan_node(
        "verify",
        complexity="high",
        verifier=True,
        write_scope="",
    )
    verifier["depends_on"] = list(worker_ids)
    return {"summary": "fixture", "nodes": [*workers, verifier]}


class FakeAdvisor:
    def __init__(self, demand_tiers: tuple[str | None, ...]) -> None:
        self.demand_tiers = demand_tiers
        self.batches: list[tuple[object, ...]] = []

    def advise_batch(self, requests: list[object]) -> list[SquillaAdvice]:
        self.batches.append(tuple(requests))
        answers: list[SquillaAdvice] = []
        for request, tier in zip(requests, self.demand_tiers, strict=True):
            request_id = getattr(request, "request_id")
            if tier is None:
                answers.append(
                    SquillaAdvice(
                        request_id=request_id,
                        status="unavailable",
                        demand_tier=None,
                        confidence=None,
                        source={"fixture": "unavailable"},
                        runtime={"mode": "fixture"},
                        diagnostic="bundle_unavailable",
                    )
                )
                continue
            answers.append(
                SquillaAdvice(
                    request_id=request_id,
                    status="available",
                    demand_tier=tier,
                    confidence=0.37,
                    source={
                        "expected_source_revision": "fixed-revision",
                        "observed_source_revision": "fixed-revision",
                        "verification_method": "fixture",
                    },
                    runtime={"mode": "fixture"},
                )
            )
        return answers


class SquillaPlannerWiringTests(unittest.TestCase):
    def normalize(
        self,
        plan: dict[str, object],
        advisor: object | None = None,
    ) -> list[NodeSpec]:
        return CodexPlanner.normalize_and_validate_plan(
            make_contract(),
            plan,
            claude_models_available=(),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
            squilla_advisor=advisor,  # type: ignore[arg-type]
        )

    def test_batch_promotes_c2_c3_workers_before_route_and_skips_verifier(self) -> None:
        advisor = FakeAdvisor(("c2", "c3", "c0"))
        planned = self.normalize(
            make_plan(
                plan_node("low", complexity="low", write_scope="src/low"),
                plan_node("standard", complexity="standard", write_scope="src/standard"),
                plan_node("already-high", complexity="high", write_scope="src/high"),
            ),
            advisor,
        )
        by_id = {node.node_id: node for node in planned}

        self.assertEqual(len(advisor.batches), 1)
        batch = advisor.batches[0]
        self.assertEqual(len(batch), 3)
        self.assertEqual(
            [getattr(request, "prompt") for request in batch],
            [
                "title low\n\noriginal prompt low",
                "title standard\n\noriginal prompt standard",
                "title already-high\n\noriginal prompt already-high",
            ],
        )
        for node_id in ("low", "standard", "already-high"):
            node = by_id[node_id]
            self.assertEqual(node.effective_complexity, "high")
            self.assertEqual(node.complexity, "high")
            self.assertEqual(node.model, "gpt-5.6-terra")
            self.assertEqual(node.model_reasoning_effort, "max")
        self.assertEqual(by_id["low"].declared_complexity, "low")
        self.assertEqual(by_id["standard"].declared_complexity, "standard")
        self.assertEqual(by_id["already-high"].declared_complexity, "high")
        verifier = by_id["verify"]
        self.assertIsNone(verifier.squilla_advice_receipt)
        self.assertIsNone(verifier.declared_complexity)
        self.assertIsNone(verifier.effective_complexity)

    def test_only_model_workers_are_classified(self) -> None:
        advisor = FakeAdvisor(("c2",))
        planned = self.normalize(
            make_plan(
                plan_node("model", complexity="low", write_scope="src/model"),
                plan_node(
                    "deterministic",
                    complexity="low",
                    executor="deterministic",
                    model="local",
                    write_scope="src/deterministic",
                ),
                plan_node(
                    "fixture",
                    complexity="low",
                    executor="fixture",
                    model="fixture",
                    write_scope="src/fixture",
                ),
            ),
            advisor,
        )
        by_id = {node.node_id: node for node in planned}

        self.assertEqual(len(advisor.batches), 1)
        self.assertEqual([getattr(request, "request_id") for request in advisor.batches[0]], ["planner-worker-0"])
        self.assertEqual(by_id["model"].effective_complexity, "high")
        for node_id in ("deterministic", "fixture"):
            self.assertIsNone(by_id[node_id].squilla_advice_receipt)
            self.assertIsNone(by_id[node_id].declared_complexity)
            self.assertIsNone(by_id[node_id].effective_complexity)


    def test_c0_never_downgrades_declared_complexity(self) -> None:
        baseline = self.normalize(
            make_plan(plan_node("standard", complexity="standard")),
        )[0]
        advisor = FakeAdvisor(("c0",))
        advised = self.normalize(
            make_plan(plan_node("standard", complexity="standard")), advisor
        )[0]

        self.assertEqual((advised.executor, advised.model, advised.complexity), (
            baseline.executor,
            baseline.model,
            baseline.complexity,
        ))
        self.assertEqual(advised.declared_complexity, "standard")
        self.assertEqual(advised.effective_complexity, "standard")
        self.assertEqual(advised.squilla_advice_receipt["demand_tier"], "c0")

    def test_unavailable_advice_preserves_policy_and_records_reason(self) -> None:
        baseline = self.normalize(
            make_plan(plan_node("standard", complexity="standard")),
        )[0]
        advised = self.normalize(
            make_plan(plan_node("standard", complexity="standard")),
            FakeAdvisor((None,)),
        )[0]

        self.assertEqual((advised.executor, advised.model, advised.complexity), (
            baseline.executor,
            baseline.model,
            baseline.complexity,
        ))
        self.assertEqual(advised.declared_complexity, "standard")
        self.assertEqual(advised.effective_complexity, "standard")
        self.assertEqual(advised.squilla_advice_receipt["status"], "unavailable")
        self.assertEqual(advised.squilla_advice_receipt["diagnostic"], "bundle_unavailable")

    def test_missing_local_install_is_explicitly_unavailable_without_native_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            advisor = SquillaAdvisor(
                runtime_python=root / "missing-python",
                source_root=root / "source",
                bundle_dir=root / "bundle",
            )
            node = self.normalize(
                make_plan(plan_node("standard", complexity="standard")), advisor
            )[0]

        self.assertEqual(node.complexity, "standard")
        self.assertEqual(node.squilla_advice_receipt["status"], "unavailable")
        self.assertEqual(node.squilla_advice_receipt["diagnostic"], "runtime_python_unavailable")

    def test_forged_planner_advice_is_rejected(self) -> None:
        worker = plan_node("worker", complexity="low")
        worker["squilla_advice_receipt"] = {"status": "available"}
        worker["performance_routing_receipt"] = {"forged": True}
        with self.assertRaisesRegex(PlannerError, "derived routing metadata"):
            self.normalize(make_plan(worker), FakeAdvisor(("c2",)))

    def test_receipt_round_trips_and_changes_full_node_evidence_without_prompt(self) -> None:
        first = self.normalize(
            make_plan(plan_node("worker", complexity="low")), FakeAdvisor(("c2",))
        )[0]
        second = self.normalize(
            make_plan(plan_node("worker", complexity="low")), FakeAdvisor(("c3",))
        )[0]

        serialized = first.to_dict()
        rendered = canonical_json(serialized["squilla_advice_receipt"])
        self.assertEqual(NodeSpec.from_dict(serialized), first)
        self.assertNotIn("original prompt worker", rendered)
        self.assertEqual(serialized["squilla_advice_receipt"]["prompt_hint"], None)
        self.assertNotEqual(canonical_json(first.to_dict()), canonical_json(second.to_dict()))


class SquillaConfigurationTests(unittest.TestCase):
    def test_venv_python_symlink_stays_under_advisor_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            runtime_python = root / "advisors" / "opensquilla" / "venv" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True)
            runtime_python.symlink_to(Path(directory) / "external-python")
            config = WorkbenchConfig(
                root,
                squilla_advisor=SquillaAdvisorConfig(enabled=True),
            )

            effective = config.effective_squilla_advisor_config
            config.initialize()
            raw = json.loads(config.config_file.read_text(encoding="utf-8"))
            loaded = WorkbenchConfig.load(root)

            self.assertTrue(runtime_python.is_symlink())
            self.assertEqual(effective.runtime_python, runtime_python)
            self.assertEqual(raw["squilla_advisor"]["runtime_python"], str(runtime_python))
            self.assertEqual(loaded.effective_squilla_advisor_config.runtime_python, runtime_python)
            self.assertEqual(loaded.effective_squilla_advisor_config.timeout_seconds, 45.0)

    def test_authority_init_reconstruction_preserves_explicit_squilla_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            configured = WorkbenchConfig(
                root,
                squilla_advisor=SquillaAdvisorConfig(enabled=True),
            )
            configured.initialize()
            args = argparse.Namespace(home=str(root), authority=True)
            with (
                patch("codex_workbench.cli._store") as store,
                patch("codex_workbench.cli.authority_machine_id", return_value="fixture-machine"),
            ):
                self.assertEqual(command_init(args), 0)

            captured = store.call_args.args[0]
            self.assertTrue(captured.squilla_advisor.enabled)
            self.assertEqual(
                captured.effective_squilla_advisor_config.runtime_python,
                root / "advisors" / "opensquilla" / "venv" / "bin" / "python",
            )


if __name__ == "__main__":
    unittest.main()
