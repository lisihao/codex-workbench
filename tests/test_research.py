from __future__ import annotations

import unittest

from codex_workbench.model import TaskContract
from codex_workbench.research import research_planner_directive, route_research


def contract(**changes: object) -> TaskContract:
    values: dict[str, object] = {
        "task_id": "research-route",
        "repository": "/tmp/example",
        "base_sha": "abc123",
        "objective": "implement a bounded parser change",
        "allowed_scope": ("src", "tests"),
    }
    values.update(changes)
    return TaskContract(**values)  # type: ignore[arg-type]


class ResearchRoutingTests(unittest.TestCase):
    def test_architecture_exploration_and_high_complexity_require_research(self) -> None:
        cases = (
            contract(task_type="architecture"),
            contract(task_type="exploration"),
            contract(complexity="high"),
        )
        for value in cases:
            with self.subTest(task_type=value.task_type, complexity=value.complexity):
                self.assertEqual(route_research(value).mode, "standard")
                self.assertIn("Mandatory skill invocation: $Research", research_planner_directive(value))

    def test_source_grounded_scenarios_require_standard_research(self) -> None:
        objectives = (
            "核验原始论文并复现参考架构",
            "分析上游实现与当前最佳实践",
            "找到性能瓶颈并设计基准测试",
            "完成技术选型和迁移方案",
            "run a compatibility assessment against the upstream benchmark",
        )
        for objective in objectives:
            with self.subTest(objective=objective):
                self.assertEqual(route_research(contract(objective=objective)).mode, "standard")

    def test_parallel_research_requires_explicit_wording(self) -> None:
        self.assertEqual(
            route_research(contract(objective="对 Agent Runtime 做深度并行研究")).mode,
            "deep-parallel",
        )
        self.assertEqual(
            route_research(contract(objective="深入分析 Agent Runtime")).mode,
            "standard",
        )

    def test_bounded_local_implementation_skips_research(self) -> None:
        route = route_research(contract())
        self.assertEqual(route.mode, "none")
        self.assertFalse(route.required)
        self.assertIn("not required", research_planner_directive(contract()))


if __name__ == "__main__":
    unittest.main()
