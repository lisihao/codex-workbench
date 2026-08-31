from __future__ import annotations

import unittest

from codex_workbench.model import TaskContract
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


class PlannerRoutingTests(unittest.TestCase):
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
        self.assertEqual([worker.model for worker in workers], ["gpt-5.6-luna", "gpt-5.6-luna"])
        self.assertEqual([worker.depends_on for worker in workers], [(), ()])
        self.assertEqual(planned[-1].model, "gpt-5.6-sol")
        self.assertEqual(set(planned[-1].depends_on), {"worker-a", "worker-b"})
        self.assertEqual(planned[-1].write_scopes, ())

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


if __name__ == "__main__":
    unittest.main()
