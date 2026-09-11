from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.accepted_source_repair import (
    AcceptedSourceRepairError,
    build_accepted_repair_bindings,
    parse_accepted_source_repair_binding,
    prepare_accepted_source_repair,
)
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.service import Coordinator
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class AcceptedSourceRepairFixture:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "user.name", "Fixture")
        (self.repository / "src" / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(self.repository, "add", "src/base.txt")
        self._git(self.repository, "commit", "-m", "base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.state_root = self.root / "state"
        self.store = WorkbenchStore(self.state_root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("accepted-source-fixture", "fixture")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _create_task(
        self,
        *,
        include_descendant: bool = False,
    ) -> tuple[TaskContract, dict[str, object]]:
        contract = TaskContract(
            task_id="accepted-source-fixture",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="continue only the accepted owner patch after verifier feedback",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        a = NodeSpec(
            "A",
            contract.task_id,
            "accepted ancestor",
            "fixture",
            "fixture",
            write_scopes=("src/a.txt",),
        )
        b = NodeSpec(
            "B",
            contract.task_id,
            "accepted source owner",
            "fixture",
            "fixture",
            depends_on=(a.node_id,),
            read_scopes=("src/a.txt",),
            write_scopes=("src/b.txt",),
        )
        nodes = [a, b]
        if include_descendant:
            c = NodeSpec(
                "C",
                contract.task_id,
                "accepted descendant",
                "fixture",
                "fixture",
                depends_on=(b.node_id,),
                read_scopes=("src/b.txt",),
                write_scopes=("src/c.txt",),
            )
            nodes.append(c)
        verify = NodeSpec(
            "verify",
            contract.task_id,
            "failed verifier",
            "fixture",
            "fixture",
            depends_on=tuple(node.node_id for node in nodes),
            verifier=True,
        )
        self.store.create_task(contract, [*nodes, verify], "accepted-source-create")
        self.store.queue_task(contract.task_id)
        self._accept_worker(a.node_id, "src/a.txt", "ancestor\n")
        self._accept_worker(b.node_id, "src/b.txt", "owner patch\n")
        if include_descendant:
            self._accept_worker("C", "src/c.txt", "descendant\n")
        claimed_verifier = self.store.claim_ready_node("verifier-worker", self.epoch)
        assert claimed_verifier is not None
        self.assertEqual(claimed_verifier["node_id"], "verify")
        return contract, claimed_verifier

    def _accept_worker(self, node_id: str, changed_path: str, content: str) -> None:
        claimed = self.store.claim_ready_node(f"{node_id}-worker", self.epoch)
        assert claimed is not None
        self.assertEqual(claimed["node_id"], node_id)
        worktree = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            str(claimed["task_id"]),
            node_id,
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            str(claimed["task_id"]),
            node_id,
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(str(claimed["task_id"])),
            node_id,
            worktree,
            self.store.artifacts,
            self.worktrees,
        )
        dependency_ref: str | None = None
        input_tree = self.base_sha
        if dependency_input is not None:
            dependency_ref = self.store.artifacts.put_text(
                canonical_json(dependency_input.receipt), "dependency-input.json"
            )
            input_tree = dependency_input.input_tree_sha
        target = worktree / changed_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        patch = self.store.artifacts.put_bytes(
            self.worktrees.diff_patch(worktree, input_tree), "patch"
        )
        artifacts = {"patch": patch}
        if dependency_ref is not None:
            artifacts["dependency-input"] = dependency_ref
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                f"{node_id} accepted",
                artifacts=artifacts,
                changed_paths=(changed_path,),
                checks=("fixture",),
            ),
        )

    def _bindings(
        self,
        contract: TaskContract,
        verifier: dict[str, object],
        repair_node_ids: tuple[str, ...] = ("B",),
    ) -> dict[str, dict]:
        revision = int(self.store.get_task(contract.task_id)["state_revision"])
        with self.store.connection() as connection:
            return build_accepted_repair_bindings(
                self.store,
                connection,
                contract.task_id,
                repair_node_ids,
                "verify",
                int(verifier["attempt"]),
                revision + 1,
            )

    def _stage_claimed_repair(self, binding: dict) -> None:
        source_attempt = int(binding["source"]["attempt"])
        with self.store.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE nodes
                SET state = 'running', attempt = ?, worker_id = 'repair-worker',
                    worktree = NULL, result_json = NULL, recovery_json = ?,
                    coordinator_epoch = ?, lease_epoch = 7, updated_at = updated_at
                WHERE task_id = ? AND node_id = ? AND state = 'accepted' AND attempt = ?
                """,
                (
                    source_attempt + 1,
                    canonical_json(binding),
                    self.epoch,
                    binding["task_id"],
                    binding["node_id"],
                    source_attempt,
                ),
            ).rowcount
        self.assertEqual(changed, 1)


class AcceptedSourceRepairTests(AcceptedSourceRepairFixture, unittest.TestCase):
    def test_verifier_failure_without_owner_ids_preserves_accepted_workers(self) -> None:
        contract, verifier = self._create_task()
        before = self.store.get_task(contract.task_id)
        accepted_before = {
            node["node_id"]: node["result"]
            for node in before["nodes"]
            if node["node_id"] in {"A", "B"}
        }

        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "verifier needs an explicit source owner",
                verdict="needs_fix",
                checks=("fixture verifier",),
            ),
        )

        task = self.store.get_task(contract.task_id)
        nodes = {node["node_id"]: node for node in task["nodes"]}
        self.assertEqual(task["state"], "needs_fix")
        self.assertEqual((nodes["verify"]["state"], nodes["verify"]["attempt"]), ("failed", 1))
        for node_id in ("A", "B"):
            self.assertEqual((nodes[node_id]["state"], nodes[node_id]["attempt"]), ("accepted", 1))
            self.assertEqual(nodes[node_id]["result"], accepted_before[node_id])
        self.assertIsNone(self.store.claim_ready_node("must-not-reset-workers", self.epoch))
        events = self.store.read_events(task_id=contract.task_id)
        event_types = {event["event_type"] for event in events}
        self.assertIn("task.repair_owner_required", event_types)
        self.assertNotIn("task.repair_scheduled", event_types)

    def test_verifier_repair_claim_continues_only_the_selected_accepted_owner(self) -> None:
        contract, verifier = self._create_task()
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "failed",
                "B must update its accepted implementation",
                verdict="needs_fix",
                repair_node_ids=("B",),
                checks=("fixture verifier",),
            ),
        )
        after_verifier = self.store.get_task(contract.task_id)
        a_after_verifier = next(
            node for node in after_verifier["nodes"] if node["node_id"] == "A"
        )
        b_after_verifier = next(
            node for node in after_verifier["nodes"] if node["node_id"] == "B"
        )
        self.assertEqual((after_verifier["state"], a_after_verifier["state"]), ("queued", "accepted"))
        self.assertEqual((b_after_verifier["state"], b_after_verifier["attempt"]), ("pending", 1))
        with self.store.connection() as connection:
            stored = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()["recovery_json"]
        self.assertIsNotNone(parse_accepted_source_repair_binding(stored, next_attempt=2))

        coordinator = Coordinator(self.store, self.state_root, coordinator_epoch=self.epoch)
        observed: dict[str, object] = {}

        def execute(request: object) -> NodeResult:
            worktree = request.worktree  # type: ignore[attr-defined]
            assert worktree is not None
            observed["node_id"] = request.node_id  # type: ignore[attr-defined]
            observed["attempt"] = request.attempt  # type: ignore[attr-defined]
            observed["ancestor"] = (worktree / "src/a.txt").read_text(encoding="utf-8")
            observed["owner_patch"] = (worktree / "src/b.txt").read_text(encoding="utf-8")
            return NodeResult("succeeded", "accepted owner continued", checks=("fixture",))

        try:
            claimed = coordinator._claim_next_ready_node("accepted-owner-repair")
            assert claimed is not None
            self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 2))
            self.assertIn("accepted_source_repair", claimed)
            with patch.object(coordinator, "_executor") as executor:
                executor.return_value.execute.side_effect = execute
                coordinator._execute_claimed(claimed)
        finally:
            coordinator._pool.shutdown(wait=True)

        self.assertEqual(
            observed,
            {
                "node_id": "B",
                "attempt": 2,
                "ancestor": "ancestor\n",
                "owner_patch": "owner patch\n",
            },
        )
        completed = self.store.get_task(contract.task_id)
        a_completed = next(node for node in completed["nodes"] if node["node_id"] == "A")
        b_completed = next(node for node in completed["nodes"] if node["node_id"] == "B")
        self.assertEqual((a_completed["state"], a_completed["attempt"]), ("accepted", 1))
        self.assertEqual((b_completed["state"], b_completed["attempt"]), ("accepted", 2))
        source = self.worktrees.worktree_path(contract.task_id, "B", 1)
        self.assertEqual((source / "src/b.txt").read_text(encoding="utf-8"), "owner patch\n")
        allocations = self.store.list_worktree_allocations()
        self.assertEqual(
            next(item for item in allocations if item["node_id"] == "B" and item["attempt"] == 1)["state"],
            "superseded",
        )

    def test_prepares_only_accepted_owner_on_recorded_ancestor_input(self) -> None:
        contract, verifier = self._create_task()
        binding = self._bindings(contract, verifier)["B"]
        before = self.store.get_task(contract.task_id)
        a_before = next(node for node in before["nodes"] if node["node_id"] == "A")
        self.assertEqual((a_before["state"], a_before["attempt"]), ("accepted", 1))

        self._stage_claimed_repair(binding)
        prepared = prepare_accepted_source_repair(self.store, binding, self.worktrees)

        self.assertEqual(prepared.worktree, self.worktrees.worktree_path(contract.task_id, "B", 2))
        self.assertEqual((prepared.worktree / "src/a.txt").read_text(encoding="utf-8"), "ancestor\n")
        self.assertEqual((prepared.worktree / "src/b.txt").read_text(encoding="utf-8"), "owner patch\n")
        self.assertEqual(
            prepared.dependency_input.receipt["ancestors"],
            [{
                "node_id": "A",
                "attempt": 1,
                "patch_ref": next(
                    node for node in before["nodes"] if node["node_id"] == "A"
                )["result"]["artifacts"]["patch"],
            }],
        )
        self.assertEqual(
            prepared.receipt,
            {
                "schema_version": 1,
                "kind": "accepted-source-repair-v1",
                "state": "prepared",
                "authorization_revision": int(before["state_revision"]) + 1,
                "source_allocation_id": binding["source_allocation_id"],
                "source_attempt": 1,
                "source_status": "succeeded",
                "target_attempt": 2,
                "target_worktree": str(prepared.worktree),
                "target_branch": self.worktrees.branch_name(contract.task_id, "B", 2),
                "dependency_input_ref": binding["source"]["dependency_input_ref"],
                "dependency_input_tree_sha": prepared.dependency_input.input_tree_sha,
                "patch_ref": binding["source"]["patch_ref"],
                "patch_sha256": binding["source"]["patch_sha256"],
                "binding_sha256": sha256(canonical_json(binding).encode()).hexdigest(),
            },
        )
        after = self.store.get_task(contract.task_id)
        a_after = next(node for node in after["nodes"] if node["node_id"] == "A")
        self.assertEqual((a_after["state"], a_after["attempt"]), ("accepted", 1))
        self.assertEqual(a_after["result"], a_before["result"])

    def test_parser_rejects_forged_failed_source_status(self) -> None:
        contract, verifier = self._create_task()
        binding = self._bindings(contract, verifier)["B"]
        forged = json.loads(canonical_json(binding))
        forged["source_status"] = "failed"

        with self.assertRaisesRegex(AcceptedSourceRepairError, "source_status"):
            parse_accepted_source_repair_binding(forged)

        forged = json.loads(canonical_json(binding))
        source_result = json.loads(forged["source_result_json"])
        source_result["status"] = "failed"
        forged["source_result_json"] = canonical_json(source_result)
        with self.assertRaisesRegex(AcceptedSourceRepairError, "not succeeded"):
            parse_accepted_source_repair_binding(forged)

    def test_prepare_rejects_a_validly_hashed_non_patch_artifact(self) -> None:
        contract, verifier = self._create_task()
        binding = json.loads(canonical_json(self._bindings(contract, verifier)["B"]))
        invalid_patch = self.store.artifacts.put_bytes(b"not a git patch\n", "patch")
        source_result = json.loads(binding["source_result_json"])
        source_result["artifacts"]["patch"] = invalid_patch
        binding["source_result_json"] = canonical_json(source_result)
        binding["source"]["patch_ref"] = invalid_patch
        binding["source"]["patch_sha256"] = sha256(b"not a git patch\n").hexdigest()
        self._stage_claimed_repair(binding)

        with self.assertRaisesRegex(AcceptedSourceRepairError, "preparation failed"):
            prepare_accepted_source_repair(self.store, binding, self.worktrees)

    def test_build_rejects_bad_patch_lineage_base_and_allocation(self) -> None:
        contract, verifier = self._create_task()
        with self.store.transaction() as connection:
            result_row = connection.execute(
                "SELECT result_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            ).fetchone()
            original_result = str(result_row["result_json"])
            result = json.loads(original_result)
            result["artifacts"]["patch"] = "sha256:" + "0" * 64 + ":patch"
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (canonical_json(result), contract.task_id),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "patch is unavailable or invalid"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (original_result, contract.task_id),
            )

        bad_receipt = {
            "schema_version": 1,
            "kind": "accepted-ancestor-patch-input",
            "task_id": "other-task",
            "node_id": "B",
            "contract_base_sha": self.base_sha,
            "input_tree_sha": self.base_sha,
            "ancestors": [],
        }
        bad_ref = self.store.artifacts.put_text(
            canonical_json(bad_receipt), "dependency-input.json"
        )
        with self.store.transaction() as connection:
            result = json.loads(original_result)
            result["artifacts"]["dependency-input"] = bad_ref
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (canonical_json(result), contract.task_id),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "dependency input is invalid"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (original_result, contract.task_id),
            )

        with self.store.transaction() as connection:
            allocation = connection.execute(
                """
                SELECT base_sha, state FROM worktree_allocations
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (contract.task_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE worktree_allocations SET base_sha = 'different-base'
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (contract.task_id,),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "allocation does not match"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET base_sha = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (allocation["base_sha"], contract.task_id),
            )

        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET state = 'superseded'
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (contract.task_id,),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "allocation does not match"):
            self._bindings(contract, verifier)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE worktree_allocations SET state = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (allocation["state"], contract.task_id),
            )

    def test_build_rejects_bad_owner_duplicate_verifier_and_nonaccepted_source(self) -> None:
        contract, verifier = self._create_task()
        for repair_ids, message in (
            (("missing",), "owner missing is missing"),
            (("B", "B"), "repair_node_ids are duplicated"),
            (("verify",), "cannot select its verifier"),
        ):
            with self.subTest(repair_ids=repair_ids), self.assertRaisesRegex(
                AcceptedSourceRepairError, message
            ):
                self._bindings(contract, verifier, repair_ids)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'failed' WHERE task_id = ? AND node_id = 'B'",
                (contract.task_id,),
            )
        with self.assertRaisesRegex(AcceptedSourceRepairError, "expected accepted"):
            self._bindings(contract, verifier)

    def test_build_rejects_external_or_destructive_contract(self) -> None:
        contract, verifier = self._create_task()
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT contract_json FROM tasks WHERE task_id = ?", (contract.task_id,)
            ).fetchone()
            original = str(row["contract_json"])
        for field in ("external_write_permission", "destructive_action_permission"):
            with self.subTest(field=field):
                with self.store.transaction() as connection:
                    current = json.loads(original)
                    current[field] = True
                    connection.execute(
                        "UPDATE tasks SET contract_json = ? WHERE task_id = ?",
                        (canonical_json(current), contract.task_id),
                    )
                with self.assertRaisesRegex(AcceptedSourceRepairError, "external or destructive"):
                    self._bindings(contract, verifier)
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET contract_json = ? WHERE task_id = ?",
                        (original, contract.task_id),
                    )

    def test_build_rejects_unselected_accepted_descendant(self) -> None:
        contract, verifier = self._create_task(include_descendant=True)

        with self.assertRaisesRegex(AcceptedSourceRepairError, "accepted descendant C"):
            self._bindings(contract, verifier, ("B",))
