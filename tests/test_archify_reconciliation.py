from __future__ import annotations

from copy import deepcopy
import unittest

from codex_workbench.archify import default_vendor_root
from codex_workbench.model import TaskContract
from codex_workbench.planner import (
    archify_artifact_requested,
    archify_directive,
    archify_internal_directive,
    archify_role_for,
    propose_archify_reconciliation,
)


def make_contract(
    objective: str,
    *,
    task_type: str = "implementation",
    required_artifacts: tuple[str, ...] = ("diff", "test-log", "verdict"),
) -> TaskContract:
    return TaskContract(
        task_id="archify-reconciliation",
        repository="/tmp/workbench-reconciliation",
        base_sha="abc123",
        objective=objective,
        allowed_scope=("src", "tests"),
        required_artifacts=required_artifacts,
        task_type=task_type,  # type: ignore[arg-type]
    )


def pending_node(node_id: str, **changes: object) -> dict[str, object]:
    node: dict[str, object] = {
        "node_id": node_id,
        "title": "bounded architecture node",
        "prompt": "bounded work",
        "state": "pending",
        "attempt": 0,
        "result": None,
        "worktree": None,
        "archify": archify_internal_directive("architecture", True),
    }
    node.update(changes)
    return node


class ArchifyReconciliationTests(unittest.TestCase):
    def test_negative_english_and_chinese_graph_requests_do_not_require_artifacts(self) -> None:
        for objective in (
            "This is not a request for additional technical research or an architecture diagram.",
            "Do not add an unrelated architecture graphic.",
            "Proceed without producing an architecture artifact.",
            "不要添加无关的架构图。",
            "不需要生成架构图。",
        ):
            with self.subTest(objective=objective):
                contract = make_contract(objective, task_type="architecture")
                self.assertEqual(archify_role_for(contract), "architecture")
                self.assertFalse(archify_artifact_requested(contract))

    def test_ordinary_schema_and_lifecycle_artifacts_do_not_imply_graphics(self) -> None:
        contract = make_contract(
            "Implement the schema and lifecycle artifacts for the bounded change.",
            required_artifacts=("schema", "lifecycle"),
        )
        self.assertIsNone(archify_role_for(contract))
        self.assertFalse(archify_artifact_requested(contract))

    def test_skill_path_restriction_is_not_an_artifact_request(self) -> None:
        contract = make_contract(
            "Implement the bounded schema. This is not a request for an architecture diagram. "
            "Never put an installed Research/Archify/Code-as-Harness skill path in writable scopes."
        )
        self.assertIsNone(archify_role_for(contract))
        self.assertFalse(archify_artifact_requested(contract))

    def test_affirmative_diagram_and_typed_graph_ir_requests_require_artifacts(self) -> None:
        cases = (
            make_contract(
                "Create an architecture diagram for the bounded service.",
                task_type="architecture",
            ),
            make_contract(
                "Review the typed graph IR for the bounded service.",
                task_type="review",
            ),
        )
        for contract in cases:
            with self.subTest(objective=contract.objective):
                self.assertIsNotNone(archify_role_for(contract))
                self.assertTrue(archify_artifact_requested(contract))

    def test_mixed_no_diagram_but_flowchart_remains_affirmative(self) -> None:
        contract = make_contract(
            "Do not create an architecture diagram but produce a flowchart for the service.",
        )
        self.assertEqual(archify_role_for(contract), "design")
        self.assertTrue(archify_artifact_requested(contract))

    def test_reconciliation_only_repairs_never_executed_pending_nodes(self) -> None:
        contract = make_contract("Create an architecture diagram for the service.", task_type="architecture")
        nodes = [
            pending_node(
                "eligible",
                prompt="Do not add an unrelated architecture graphic.",
            ),
            pending_node("retried", attempt=1),
            pending_node("final-verifier", verifier=True),
            pending_node("running", state="running"),
            pending_node("has-result", result={"status": "failed"}),
            pending_node("has-worktree", worktree={"name": "fixture-worktree"}),
        ]

        proposals = propose_archify_reconciliation(contract, nodes)

        self.assertEqual([proposal["node_id"] for proposal in proposals], ["eligible"])
        self.assertEqual(
            proposals[0]["after"]["archify"],
            archify_internal_directive("architecture", False),
        )

    def test_reconciliation_removes_exact_prior_host_block_preserves_user_text_and_is_idempotent(self) -> None:
        contract = make_contract("Create an architecture diagram for the service.", task_type="architecture")
        fake_marker = (
            "Archify directive (quoted user marker; role=architecture; artifact=required):\n"
            "This literal marker is user text and must remain."
        )
        user_prompt = f"Keep this exact request context.\n{fake_marker}\nPreserve this line too."
        current_directive = archify_directive(
            contract,
            role="architecture",
            actor="Codex worker",
            artifact_required=True,
        )
        previous_host = "/previous-host/vendor/archify"
        previous_directive = current_directive.replace(
            str(default_vendor_root().resolve()), previous_host
        )
        original_prompt = f"{user_prompt}\n\n{previous_directive}"
        nodes = [
            pending_node(
                "worker",
                prompt=original_prompt,
            )
        ]

        proposals = propose_archify_reconciliation(contract, nodes)

        self.assertEqual([proposal["node_id"] for proposal in proposals], ["worker"])
        proposal = proposals[0]
        self.assertEqual(proposal["before"]["prompt"], original_prompt)
        self.assertEqual(
            proposal["after"]["prompt"],
            f"{user_prompt}\n\n{current_directive}",
        )
        self.assertIn(fake_marker, proposal["after"]["prompt"])
        self.assertNotIn(previous_host, proposal["after"]["prompt"])

        reconciled = deepcopy(nodes)
        reconciled[0]["archify"] = proposal["after"]["archify"]
        reconciled[0]["prompt"] = proposal["after"]["prompt"]
        self.assertEqual(propose_archify_reconciliation(contract, reconciled), ())


if __name__ == "__main__":
    unittest.main()
