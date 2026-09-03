from __future__ import annotations

import unittest

from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.planner import CodexPlanner, PlannerError


def make_contract(**changes: object) -> TaskContract:
    values: dict[str, object] = {
        "task_id": "plan-task",
        "repository": "/tmp/example",
        "base_sha": "abc123",
        "objective": "bounded implementation",
        "allowed_scope": ("src", "tests"),
        "complexity": "low",
        "parallelizable": True,
    }
    values.update(changes)
    return TaskContract(**values)  # type: ignore[arg-type]


def node(
    node_id: str,
    *,
    write_scope: str,
    executor: str = "claude",
    model: str = "sonnet",
    depends_on: tuple[str, ...] = (),
    verifier: bool = False,
) -> dict:
    return {
        "node_id": node_id,
        "title": node_id,
        "executor": executor,
        "model": model,
        "prompt": f"work on {node_id}",
        "command": [],
        "depends_on": list(depends_on),
        "read_scopes": [write_scope] if write_scope else ["src"],
        "write_scopes": [write_scope] if write_scope else [],
        "verifier": verifier,
    }


def catalog() -> dict:
    def record(model_id: str, roles: list[str], task_types: list[str], quality: str, weight: int) -> dict:
        return {
            "provider": "codex",
            "model_id": model_id,
            "capability_id": f"codex:{model_id}",
            "status": "available",
            "routable": True,
            "roles": roles,
            "task_types": task_types,
            "quality": {"floor": quality},
            "cost": {"relative": "balanced"},
            "latency": {"class": "balanced"},
            "concurrency": {"weight": weight, "class": "high"},
            "reasoning": {"preferred_effort": "max"},
            "features": {"structured_output": True},
        }

    return {
        "catalog_id": "catalog-planner-v3",
        "digest": "b" * 64,
        "agents": {
            "codex": {"status": "available", "cli_version": "0.149.1"},
            "claude": {"status": "unavailable", "cli_version": "unavailable"},
        },
        "models": [
            record(
                "gpt-5.6-sol",
                ["planner", "verifier", "architecture", "research"],
                ["architecture", "review", "exploration"],
                "frontier",
                3,
            ),
            record(
                "gpt-5.6-luna",
                ["worker"],
                ["implementation", "debugging", "tests", "docs", "exploration"],
                "production",
                1,
            ),
        ],
    }


class PlannerRoutingTests(unittest.TestCase):
    def test_normalization_accepts_nodes_serialized_with_archify_metadata(self) -> None:
        contract = make_contract()
        worker = NodeSpec(
            "worker",
            contract.task_id,
            "worker",
            "codex",
            "gpt-5.6-luna",
            "bounded work",
            read_scopes=("src/worker",),
            write_scopes=("src/worker",),
        )
        verifier = NodeSpec(
            "verify",
            contract.task_id,
            "verify",
            "codex",
            "gpt-5.6-sol",
            "accept",
            depends_on=("worker",),
            verifier=True,
        )

        planned = CodexPlanner.normalize_and_validate_plan(
            contract,
            {"summary": "serialized nodes", "nodes": [worker.to_dict(), verifier.to_dict()]},
            claude_models_available=(),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
        )

        self.assertEqual([node.node_id for node in planned], ["worker", "verify"])

    def test_normalization_keeps_disjoint_workers_parallel_and_routes_them(self) -> None:
        raw = {
            "summary": "parallel work",
            "nodes": [
                node("worker-a", write_scope="src/a"),
                node("worker-b", write_scope="src/b"),
                node(
                    "verify",
                    write_scope="",
                    executor="codex",
                    model="gpt-5.6-luna",
                    depends_on=("worker-a", "worker-b"),
                    verifier=True,
                ),
            ],
        }

        planned = CodexPlanner.normalize_and_validate_plan(
            make_contract(),
            raw,
            claude_models_available=(),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
        )

        workers = planned[:2]
        self.assertEqual([worker.executor for worker in workers], ["codex", "codex"])
        self.assertEqual(
            [worker.model for worker in workers],
            ["gpt-5.3-codex-spark", "gpt-5.3-codex-spark"],
        )
        self.assertEqual([worker.depends_on for worker in workers], [(), ()])
        self.assertEqual(planned[-1].model, "gpt-5.6-sol")
        self.assertEqual(set(planned[-1].depends_on), {"worker-a", "worker-b"})
        self.assertEqual(planned[-1].write_scopes, ())
        self.assertEqual(
            [(worker.task_type, worker.complexity, worker.parallelizable) for worker in workers],
            [("implementation", "low", True), ("implementation", "low", True)],
        )
        self.assertEqual(
            [(worker.model_profile, worker.model_reasoning_effort) for worker in workers],
            [("spark_worker", "xhigh"), ("spark_worker", "xhigh")],
        )

    def test_mixed_dag_routes_each_node_from_its_typed_metadata(self) -> None:
        contract = make_contract(complexity="standard")
        micro = node("micro", write_scope="src/micro")
        micro.update(
            {
                "task_type": "tests",
                "complexity": "low",
                "parallelizable": True,
                "claude_allowed": True,
                "routing_strategy": "model-routing-v2",
            }
        )
        feature = node("feature", write_scope="src/feature")
        feature.update(
            {
                "task_type": "implementation",
                "complexity": "standard",
                "parallelizable": True,
                "claude_allowed": False,
                "routing_strategy": "model-routing-v2",
            }
        )
        large = node("large", write_scope="src/large")
        large.update(
            {
                "task_type": "implementation",
                "complexity": "high",
                "parallelizable": True,
                "claude_allowed": False,
                "routing_strategy": "model-routing-v2",
            }
        )
        verifier = node(
            "verify",
            write_scope="",
            executor="codex",
            model="gpt-5.6-sol",
            depends_on=("micro", "feature", "large"),
            verifier=True,
        )
        planned = CodexPlanner.normalize_and_validate_plan(
            contract,
            {"summary": "mixed routing", "nodes": [micro, feature, large, verifier]},
            claude_models_available=(),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
        )

        self.assertEqual(
            [
                (item.node_id, item.model, item.model_profile, item.model_reasoning_effort)
                for item in planned[:3]
            ],
            [
                ("micro", "gpt-5.3-codex-spark", "spark_worker", "xhigh"),
                ("feature", "gpt-5.6-luna", "luna_worker", "max"),
                ("large", "gpt-5.6-terra", "terra_worker", "max"),
            ],
        )
        self.assertEqual(
            (planned[-1].model, planned[-1].model_profile, planned[-1].model_reasoning_effort),
            ("gpt-5.6-sol", "sol_control_plane", "max"),
        )

    def test_overlapping_parallel_access_is_rejected_instead_of_serialized(self) -> None:
        raw = {
            "summary": "unsafe parallel work",
            "nodes": [
                node("worker-a", write_scope="src/shared"),
                node("worker-b", write_scope="src/shared/subtree"),
                node(
                    "verify",
                    write_scope="",
                    executor="codex",
                    model="gpt-5.6-sol",
                    verifier=True,
                ),
            ],
        }

        with self.assertRaisesRegex(PlannerError, "overlap"):
            CodexPlanner.normalize_and_validate_plan(
                make_contract(),
                raw,
                claude_models_available=(),
                default_executor_model="gpt-5.6-luna",
                verifier_model="gpt-5.6-sol",
            )

    def test_pinned_catalog_routes_and_persists_capability_metadata(self) -> None:
        active = catalog()
        contract = make_contract(
            complexity="standard",
            capability_snapshot_id=active["catalog_id"],
            capability_digest=active["digest"],
        )
        raw = {
            "summary": "catalog routing",
            "nodes": [
                node("worker", write_scope="src/worker", executor="codex", model="gpt-5.6-luna"),
                node("verify", write_scope="", executor="codex", model="gpt-5.6-sol", verifier=True),
            ],
        }

        planned = CodexPlanner.normalize_and_validate_plan(
            contract,
            raw,
            claude_models_available=(),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
            capability_snapshot=active,
            provider_capacity={"codex": {"capacity": 4}},
        )

        worker, verifier = planned
        self.assertEqual((worker.executor, worker.model), ("codex", "gpt-5.6-luna"))
        self.assertEqual((verifier.executor, verifier.model), ("codex", "gpt-5.6-sol"))
        for item, expected_capability in (
            (worker, "codex:gpt-5.6-luna"),
            (verifier, "codex:gpt-5.6-sol"),
        ):
            with self.subTest(node=item.node_id):
                self.assertEqual(item.capability_snapshot_id, active["catalog_id"])
                self.assertEqual(item.capability_digest, active["digest"])
                self.assertEqual(item.model_capability_id, expected_capability)
                self.assertEqual(item.agent_capability_id, "codex-cli:0.149.1")
                self.assertEqual(item.agent_name, "codex-cli")
                self.assertEqual(item.agent_version, "0.149.1")
                self.assertEqual(item.routing_policy_version, "model-routing-v3")


if __name__ == "__main__":
    unittest.main()
