from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_workbench.authority_service import AuthorityService
from codex_workbench.dependency_inputs import apply_accepted_ancestor_patches
from codex_workbench.historical_accepted_source import (
    HistoricalAcceptedSourceError,
    assert_historical_accepted_source_dispatch,
    historical_accepted_source,
    parse_historical_accepted_source_binding,
    prepare_historical_accepted_source,
)
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.store import StateConflictError, WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


class HistoricalAcceptedSourceFixture:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "user.name", "Fixture")
        (self.repository / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        (self.repository / "src" / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(self.repository, "add", ".gitignore", "src/base.txt")
        self._git(self.repository, "commit", "-m", "base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self.state_root = self.root / "state"
        self.store = WorkbenchStore(self.state_root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("historical-source-fixture", "fixture")
        self.worktrees = WorktreeManager(self.state_root / "worktrees")

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _fixture(self, *, task_id: str = "historical-source-fixture") -> dict[str, object]:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="restore an exact old accepted owner patch on current input",
            allowed_scope=("src",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        ancestor = NodeSpec(
            "A", task_id, "accepted ancestor", "fixture", "fixture",
            write_scopes=("src/a.txt",),
        )
        owner = NodeSpec(
            "B", task_id, "historical owner", "fixture", "fixture",
            depends_on=("A",), read_scopes=("src/a.txt",), write_scopes=("src/b.txt",),
        )
        downstream = NodeSpec(
            "C", task_id, "ordinary downstream stays pending", "fixture", "fixture",
            depends_on=("B",), read_scopes=("src/b.txt",), write_scopes=("src/c.txt",),
        )
        verifier = NodeSpec(
            "verify", task_id, "fixture verifier", "fixture", "fixture",
            depends_on=("A", "B", "C"), verifier=True,
        )
        self.store.create_task(contract, [ancestor, owner, downstream, verifier], task_id + "-create")
        self.store.queue_task(task_id)
        self._accept("A", "src/a.txt", "historical A\n")
        self._accept("B", "src/b.txt", "historical B\n")
        historical_cursor = next(
            event["cursor"]
            for event in reversed(self.store.read_events(task_id=task_id))
            if event["event_type"] == "node.accepted" and event["node_id"] == "B"
        )

        before_retry = self.store.get_task(task_id)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE nodes
                SET state = 'pending', worker_id = NULL, worktree = NULL,
                    effective_executor = NULL, effective_model = NULL,
                    started_at = NULL, settled_at = NULL, result_json = NULL,
                    recovery_json = NULL, coordinator_epoch = 0, lease_epoch = 0
                WHERE task_id = ? AND node_id = 'B' AND state = 'accepted' AND attempt = 1
                """,
                (task_id,),
            )
            connection.execute(
                """
                UPDATE worktree_allocations SET state = 'superseded'
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (task_id,),
            )
            connection.execute(
                """
                UPDATE tasks
                SET state = 'queued', state_revision = ?, blocker = NULL, verdict = NULL
                WHERE task_id = ? AND state_revision = ?
                """,
                (int(before_retry["state_revision"]) + 1, task_id, int(before_retry["state_revision"])),
            )

        current_claim = self.store.claim_ready_node("current-blocked-worker", self.epoch)
        assert current_claim is not None
        self.assertEqual((current_claim["node_id"], current_claim["attempt"]), ("B", 2))
        current_tree = self.worktrees.prepare(
            str(self.repository), self.base_sha, task_id, "B", 2
        )
        self.store.assign_worktree(
            task_id,
            "B",
            str(current_tree),
            attempt=2,
            coordinator_epoch=int(current_claim["coordinator_epoch"]),
            lease_epoch=int(current_claim["lease_epoch"]),
        )
        current_input = apply_accepted_ancestor_patches(
            self.store.get_task(task_id), "B", current_tree, self.store.artifacts, self.worktrees
        )
        assert current_input is not None
        current_input_ref = self.store.artifacts.put_text(
            canonical_json(current_input.receipt), "dependency-input.json"
        )
        self.store.settle_claimed(
            current_claim,
            NodeResult(
                "blocked",
                "current attempt is blocked without an owner delta",
                artifacts={"dependency-input": current_input_ref},
                changed_paths=(),
                checks=("fixture blocked",),
            ),
        )
        task = self.store.get_task(task_id)
        owner_node = next(node for node in task["nodes"] if node["node_id"] == "B")
        self.assertEqual((task["state"], owner_node["state"], owner_node["attempt"]), ("blocked", "blocked", 2))
        self.assertEqual(next(node for node in task["nodes"] if node["node_id"] == "C")["state"], "pending")
        return {
            "contract": contract,
            "historical_cursor": historical_cursor,
            "current_tree": current_tree,
        }

    def _accept(self, node_id: str, path: str, content: str) -> None:
        claimed = self.store.claim_ready_node(f"{node_id}-worker", self.epoch)
        assert claimed is not None
        tree = self.worktrees.prepare(
            str(self.repository), self.base_sha, str(claimed["task_id"]), node_id, int(claimed["attempt"])
        )
        self.store.assign_worktree(
            str(claimed["task_id"]),
            node_id,
            str(tree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
        )
        dependency_input = apply_accepted_ancestor_patches(
            self.store.get_task(str(claimed["task_id"])), node_id, tree,
            self.store.artifacts, self.worktrees,
        )
        input_tree = self.base_sha
        artifacts: dict[str, str] = {}
        if dependency_input is not None:
            input_tree = dependency_input.input_tree_sha
            artifacts["dependency-input"] = self.store.artifacts.put_text(
                canonical_json(dependency_input.receipt), "dependency-input.json"
            )
        target = tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        artifacts["patch"] = self.store.artifacts.put_bytes(
            self.worktrees.diff_patch(tree, input_tree), "patch"
        )
        self.store.settle_claimed(
            claimed,
            NodeResult("succeeded", node_id + " accepted", artifacts=artifacts,
                       changed_paths=(path,), checks=("fixture",)),
        )

    def _arguments(self, fixture: dict[str, object], *, request_id: str = "restore-request") -> dict[str, object]:
        contract = fixture["contract"]
        assert isinstance(contract, TaskContract)
        task = self.store.get_task(contract.task_id)
        owner = next(node for node in task["nodes"] if node["node_id"] == "B")
        return {
            "op": "preview",
            "task_id": contract.task_id,
            "node_id": "B",
            "accepted_event_cursor": fixture["historical_cursor"],
            "expected_attempt": owner["attempt"],
            "expected_revision": task["state_revision"],
            "expected_contract_hash": task["contract_hash"],
            "request_id": request_id,
        }

    def _apply(self, arguments: dict[str, object], preview: dict[str, object]) -> dict[str, object]:
        applied = {
            **arguments,
            "op": "apply",
            "expected_fingerprint": preview["fingerprint"],
        }

        def invoke(name: str, values: dict[str, object]) -> dict[str, object]:
            self.assertEqual(name, "workbench_restore_accepted_source")
            return historical_accepted_source(self.store, values)

        authority = AuthorityService(self.store, invoke, "historical-source-authority")
        result = authority.dispatch(
            {
                "request_id": applied["request_id"],
                "tool": "workbench_restore_accepted_source",
                "task_id": applied["task_id"],
                "arguments": applied,
            }
        )
        self.assertEqual(result["state"], "completed")
        returned = result["result"]
        assert isinstance(returned, dict)
        return returned


class HistoricalAcceptedSourceTests(HistoricalAcceptedSourceFixture, unittest.TestCase):
    def test_claim_prepare_and_assignment_use_current_ancestors_and_old_patch(self) -> None:
        fixture = self._fixture()
        arguments = self._arguments(fixture)
        preview = historical_accepted_source(self.store, arguments)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["source"]["attempt"], 1)
        self.assertEqual(preview["current"]["attempt"], 2)
        receipt = self._apply(arguments, preview)
        self.assertTrue(receipt["queued"])
        self.assertEqual(receipt["next_attempt"], 3)
        applied_arguments = {
            **arguments,
            "op": "apply",
            "expected_fingerprint": preview["fingerprint"],
        }
        self.assertEqual(historical_accepted_source(self.store, applied_arguments), receipt)
        self.assertEqual(
            historical_accepted_source(
                self.store,
                {"op": "status", "task_id": arguments["task_id"], "request_id": arguments["request_id"]},
            ),
            receipt,
        )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "different input"):
            historical_accepted_source(
                self.store,
                {**applied_arguments, "accepted_event_cursor": int(arguments["accepted_event_cursor"]) + 1},
            )

        claimed = self.store.claim_ready_node("historical-restorer", self.epoch)
        assert claimed is not None
        self.assertEqual((claimed["node_id"], claimed["attempt"]), ("B", 3))
        binding = claimed["historical_accepted_source"]
        prepared = prepare_historical_accepted_source(self.store, binding, self.worktrees)
        self.assertEqual((prepared.worktree / "src" / "a.txt").read_text(encoding="utf-8"), "historical A\n")
        self.assertEqual((prepared.worktree / "src" / "b.txt").read_text(encoding="utf-8"), "historical B\n")
        self.store.assign_worktree(
            str(claimed["task_id"]),
            str(claimed["node_id"]),
            str(prepared.worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]),
            recovery_preflight=prepared.receipt,
        )
        assert_historical_accepted_source_dispatch(self.store, claimed)
        allocations = self.store.list_worktree_allocations()
        self.assertEqual(
            next(item for item in allocations if item["node_id"] == "B" and item["attempt"] == 2)["state"],
            "superseded",
        )
        self.assertEqual(
            next(item for item in allocations if item["node_id"] == "B" and item["attempt"] == 1)["state"],
            "superseded",
        )
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT recovery_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (claimed["task_id"],),
            ).fetchone()
        assert row is not None
        assigned = parse_historical_accepted_source_binding(
            row["recovery_json"], next_attempt=3, allow_assigned=True
        )
        assert assigned is not None
        self.assertEqual(assigned["state"], "assigned")

    def test_legacy_current_source_without_a_dependency_receipt_is_reconstructed(self) -> None:
        fixture = self._fixture(task_id="historical-source-legacy-current")
        arguments = self._arguments(fixture, request_id="legacy-current-request")
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM nodes WHERE task_id = ? AND node_id = 'B'",
                (arguments["task_id"],),
            ).fetchone()
        assert row is not None
        result = json.loads(row["result_json"])
        result["artifacts"].pop("dependency-input")
        current_result = canonical_json(result)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'B'",
                (current_result, arguments["task_id"]),
            )
            connection.execute(
                """
                UPDATE worktree_allocations SET node_result_json = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 2
                """,
                (current_result, arguments["task_id"]),
            )
        preview = historical_accepted_source(self.store, arguments)
        self.assertTrue(preview["current"]["legacy_reconstructed"])

    def test_rejects_wrong_event_attempt_and_dirty_current_source(self) -> None:
        fixture = self._fixture()
        arguments = self._arguments(fixture)
        with self.assertRaises(KeyError):
            historical_accepted_source(self.store, {**arguments, "task_id": "another-task"})
        ancestor_cursor = next(
            event["cursor"]
            for event in self.store.read_events(task_id=arguments["task_id"])
            if event["event_type"] == "node.accepted" and event["node_id"] == "A"
        )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "not an accepted event"):
            historical_accepted_source(
                self.store, {**arguments, "accepted_event_cursor": ancestor_cursor}
            )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "expected node attempt"):
            historical_accepted_source(
                self.store, {**arguments, "expected_attempt": 3}
            )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "expected task revision"):
            historical_accepted_source(
                self.store, {**arguments, "expected_revision": int(arguments["expected_revision"]) + 1}
            )
        current_tree = fixture["current_tree"]
        assert isinstance(current_tree, Path)
        (current_tree / "src" / "local.txt").write_text("must not be dropped\n", encoding="utf-8")
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "local source delta"):
            historical_accepted_source(self.store, arguments)

    def test_rejects_historical_ancestor_gap_and_missing_patch(self) -> None:
        fixture = self._fixture()
        arguments = self._arguments(fixture)
        source_cursor = int(fixture["historical_cursor"])
        with self.store.connection() as connection:
            row = connection.execute("SELECT payload_json FROM events WHERE cursor = ?", (source_cursor,)).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        source_result = payload["result"]
        wrong_receipt = {
            "schema_version": 1,
            "kind": "accepted-ancestor-patch-input",
            "task_id": arguments["task_id"],
            "node_id": "B",
            "contract_base_sha": self.base_sha,
            "input_tree_sha": self.base_sha,
            "ancestors": [],
        }
        wrong_ref = self.store.artifacts.put_text(canonical_json(wrong_receipt), "dependency-input.json")
        source_result["artifacts"]["dependency-input"] = wrong_ref
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (canonical_json(payload), source_cursor),
            )
            connection.execute(
                """
                UPDATE worktree_allocations SET node_result_json = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (canonical_json(source_result), arguments["task_id"]),
            )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "as of its event"):
            historical_accepted_source(self.store, arguments)

        fixture = self._fixture(task_id="historical-source-missing-patch")
        arguments = self._arguments(fixture, request_id="missing-patch")
        source_cursor = int(fixture["historical_cursor"])
        with self.store.connection() as connection:
            row = connection.execute("SELECT payload_json FROM events WHERE cursor = ?", (source_cursor,)).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["result"]["artifacts"]["patch"] = "sha256:" + "0" * 64 + ":patch"
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE cursor = ?",
                (canonical_json(payload), source_cursor),
            )
            connection.execute(
                """
                UPDATE worktree_allocations SET node_result_json = ?
                WHERE task_id = ? AND node_id = 'B' AND attempt = 1
                """,
                (canonical_json(payload["result"]), arguments["task_id"]),
            )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "patch is unavailable"):
            historical_accepted_source(self.store, arguments)

    def test_rejects_active_peer_and_stale_dispatch_after_pause_or_epoch_change(self) -> None:
        fixture = self._fixture()
        arguments = self._arguments(fixture)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'running', attempt = 1 WHERE task_id = ? AND node_id = 'C'",
                (arguments["task_id"],),
            )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "active node"):
            historical_accepted_source(self.store, arguments)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE nodes SET state = 'pending', attempt = 0 WHERE task_id = ? AND node_id = 'C'",
                (arguments["task_id"],),
            )

        fixture = self._fixture(task_id="historical-source-dispatch")
        arguments = self._arguments(fixture, request_id="dispatch-request")
        preview = historical_accepted_source(self.store, arguments)
        self._apply(arguments, preview)
        claimed = self.store.claim_ready_node("dispatch-worker", self.epoch)
        assert claimed is not None
        prepared = prepare_historical_accepted_source(
            self.store, claimed["historical_accepted_source"], self.worktrees
        )
        self.store.assign_worktree(
            str(claimed["task_id"]), str(claimed["node_id"]), str(prepared.worktree),
            attempt=int(claimed["attempt"]), coordinator_epoch=int(claimed["coordinator_epoch"]),
            lease_epoch=int(claimed["lease_epoch"]), recovery_preflight=prepared.receipt,
        )
        running = self.store.get_task(str(claimed["task_id"]))
        self.store.transition_task(
            str(claimed["task_id"]), "paused", expected_revision=int(running["state_revision"])
        )
        with self.assertRaisesRegex(HistoricalAcceptedSourceError, "paused or cancelled"):
            assert_historical_accepted_source_dispatch(self.store, claimed)
        self.store.activate_coordinator("replacement", "fixture")
        with self.assertRaises(StateConflictError):
            assert_historical_accepted_source_dispatch(self.store, claimed)


if __name__ == "__main__":
    unittest.main()
