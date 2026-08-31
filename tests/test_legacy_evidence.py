from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from codex_workbench.acceptance import build_acceptance_report
from codex_workbench.cli import build_parser, command_acceptance
from codex_workbench.config import WorkbenchConfig
from codex_workbench.legacy_evidence import MANIFEST_SCHEMA, event_hash
from codex_workbench.model import NodeResult, NodeSpec, TaskContract, canonical_json
from codex_workbench.store import CommandConflictError, WorkbenchStore


class LegacyEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("legacy-test", "test-machine")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _artifact(self, text: str, kind: str) -> dict[str, object]:
        ref = self.store.artifacts.put_text(text, kind)
        return self._descriptor(ref)

    def _descriptor(self, ref: str) -> dict[str, object]:
        path = self.store.artifacts.verify(ref)
        return {"ref": ref, "hash": ref.split(":", 2)[1], "kind": ref.split(":", 2)[2], "size": path.stat().st_size}

    def test_cli_exposes_remediate_legacy(self) -> None:
        args = build_parser().parse_args(
            ["acceptance", "remediate-legacy", "--manifest", "legacy.json", "--command-id", "legacy-1"]
        )
        self.assertEqual(args.acceptance_action, "remediate-legacy")
        self.assertEqual(args.command_id, "legacy-1")

    def test_cli_imports_manifest_and_appends_remediation_event(self) -> None:
        task = self._source_task("cli-legacy")
        manifest_path = self.root / "legacy-remediation.json"
        manifest_path.write_text(json.dumps(self._manifest(task["task_id"])))
        WorkbenchConfig(
            self.root,
            deployment_role="authority",
            authority_host=socket.gethostname(),
            authority_machine_id="test-machine",
        ).initialize()
        output = io.StringIO()
        args = SimpleNamespace(
            home=str(self.root), acceptance_action="remediate-legacy",
            manifest=str(manifest_path), command_id="cli-legacy-remediation",
        )
        with patch.dict(os.environ, {"CODEX_WORKBENCH_PROCESS_HOME": str(self.root)}), patch(
            "codex_workbench.config.authority_machine_id", return_value="test-machine"
        ), redirect_stdout(output):
            self.assertEqual(command_acceptance(args), 0)
        receipt = json.loads(output.getvalue())
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.store.read_events(task_id=task["task_id"])[-1]["event_type"], "acceptance.evidence_remediated")

    def _source_task(self, task_id: str, *, verifier_model: str = "gpt-5.6-sol") -> dict:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.root),
            base_sha="legacy-base",
            objective="legacy task requires append-only Evidence remediation",
            allowed_scope=("src",),
            required_artifacts=(),
        )
        nodes = [
            NodeSpec("work", task_id, "work", "codex", "gpt-5.6-luna", "work"),
            NodeSpec(
                "verify", task_id, "verify",
                "codex" if verifier_model == "gpt-5.6-sol" else "fixture",
                verifier_model if verifier_model == "gpt-5.6-sol" else "fixture",
                "verify", depends_on=("work",), verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, f"{task_id}-create")
        self.store.queue_task(task_id)
        worker = self.store.claim_ready_node("legacy-worker", self.epoch)
        assert worker is not None
        patch = self._artifact(f"diff --git a/src/{task_id} b/src/{task_id}\n", "patch")
        worker_log = self._artifact(f"{task_id}: legacy focused test passed\n", "worker.log")
        self.store.settle_claimed(
            worker,
            NodeResult(
                "succeeded", "legacy worker", actual_model="gpt-5.6-luna", result_kind="worker",
                checks=("legacy test",), artifacts={"patch": patch["ref"], "stdout": worker_log["ref"]},
            ),
        )
        verifier = self.store.claim_ready_node("legacy-verifier", self.epoch)
        assert verifier is not None
        legacy_evidence = self._artifact(f"{task_id}: legacy verifier claim\n", "legacy-evidence")
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "succeeded", "legacy verifier", actual_model=verifier_model,
                result_kind="verifier", checks=("legacy test",),
                evidence=(legacy_evidence["ref"],) if verifier_model == "gpt-5.6-sol" else (),
                artifacts={"stdout": legacy_evidence["ref"]},
                verdict="accepted",
            ),
        )
        if verifier_model != "gpt-5.6-sol":
            deterministic_spec = NodeSpec(
                "verify", task_id, "verify", "deterministic", "local", "verify",
                depends_on=("work",), verifier=True,
            ).to_dict()
            result = self.store.get_task(task_id)["nodes"]
            verifier_result = next(node["result"] for node in result if node["node_id"] == "verify")
            verifier_result["actual_model"] = None
            with self.store.connection() as connection:
                connection.execute(
                    """
                    UPDATE nodes SET spec_json = ?, effective_executor = 'deterministic',
                        effective_model = 'local', result_json = ?
                    WHERE task_id = ? AND node_id = 'verify'
                    """,
                    (canonical_json(deterministic_spec), canonical_json(verifier_result), task_id),
                )
        return self.store.get_task(task_id)

    def _review_task(self, source_task: dict) -> tuple[dict, dict[str, object]]:
        task_id = f"{source_task['task_id']}-supplemental-sol-review-{self.store.health()['cursor']}"
        contract = TaskContract(
            task_id=task_id,
            repository=source_task["contract"]["repository"],
            base_sha=source_task["contract"]["base_sha"],
            objective=(
                f"Independent Sol supplemental review for {source_task['task_id']} "
                f"contract {source_task['contract_hash']}"
            ),
            allowed_scope=("src",),
            dependencies=(source_task["task_id"],),
            required_artifacts=(),
        )
        nodes = [
            NodeSpec(
                "review-work", task_id, "prepare review", "deterministic", "local", "prepare",
                command=("true",),
            ),
            NodeSpec(
                "review-verify", task_id, "Sol review", "codex", "gpt-5.6-sol", "review",
                depends_on=("review-work",), verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, f"{task_id}-create")
        self.store.queue_task(task_id)
        worker = self.store.claim_ready_node("review-worker", self.epoch)
        assert worker is not None
        source_patch_ref = next(
            node["result"]["artifacts"]["patch"]
            for node in source_task["nodes"]
            if node["node_id"] == "work"
        )
        self.store.settle_claimed(
            worker,
            NodeResult(
                "succeeded",
                "source patch materialized for independent review",
                artifacts={"patch": source_patch_ref},
                result_kind="worker",
                checks=("source patch materialized",),
            ),
        )
        verifier = self.store.claim_ready_node("review-verifier", self.epoch)
        assert verifier is not None
        test_log = self._artifact(f"{task_id}: focused tests passed\n", "test.log")
        verdict = self._artifact(f"{task_id}: Sol accepted review\n", "verdict")
        transcript = self._artifact(f"{task_id}: independent Sol review transcript\n", "review.log")
        receipt = self._artifact(f"{task_id}: durable review receipt\n", "receipt")
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "succeeded", "Sol supplemental review accepted",
                artifacts={
                    "test-log": test_log["ref"], "verdict": verdict["ref"],
                    "review-transcript": transcript["ref"], "receipt": receipt["ref"],
                },
                actual_model="gpt-5.6-sol", result_kind="verifier", checks=("focused review",),
                evidence=(test_log["ref"], verdict["ref"]), verdict="accepted",
            ),
        )
        review_task = self.store.get_task(task_id)
        events = self.store.read_events(task_id=task_id, limit=1000)
        by_type = {(event["event_type"], event["node_id"]): event for event in events}
        task_accepted = next(
            event for event in events
            if event["event_type"] == "task.state_changed" and event["payload"].get("to") == "accepted"
        )
        return review_task, {
            "task_id": task_id,
            "contract_hash": review_task["contract_hash"],
            "node_id": "review-verify", "attempt": 1,
            "started_cursor": by_type[("node.started", "review-verify")]["cursor"],
            "started_hash": event_hash(by_type[("node.started", "review-verify")]),
            "accepted_cursor": by_type[("node.accepted", "review-verify")]["cursor"],
            "accepted_hash": event_hash(by_type[("node.accepted", "review-verify")]),
            "task_accepted_cursor": task_accepted["cursor"],
            "task_accepted_hash": event_hash(task_accepted),
        }

    def _manifest(self, task_id: str, *, supplemental: bool = False) -> dict:
        task = self.store.get_task(task_id)
        events = self.store.read_events(task_id=task_id, limit=1000)
        by_type = {(event["event_type"], event["node_id"]): event for event in events}
        work_started, work_accepted = by_type[("node.started", "work")], by_type[("node.accepted", "work")]
        verify_started, verify_accepted = by_type[("node.started", "verify")], by_type[("node.accepted", "verify")]
        nodes_by_id = {node["node_id"]: node for node in task["nodes"]}
        worker_result = nodes_by_id["work"]["result"]
        verifier_result = nodes_by_id["verify"]["result"]
        patch = self._descriptor(worker_result["artifacts"]["patch"])
        worker_log = self._descriptor(worker_result["artifacts"]["stdout"])
        source_verifier_log = self._descriptor(verifier_result["artifacts"]["stdout"])
        source_nodes = [
            {"node_id": "work", "attempt": 1, "started_cursor": work_started["cursor"], "started_hash": event_hash(work_started), "accepted_cursor": work_accepted["cursor"], "accepted_hash": event_hash(work_accepted)},
            {"node_id": "verify", "attempt": 1, "started_cursor": verify_started["cursor"], "started_hash": event_hash(verify_started), "accepted_cursor": verify_accepted["cursor"], "accepted_hash": event_hash(verify_accepted)},
        ]
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "source": {
                "task_id": task_id, "contract_hash": task["contract_hash"], "base_sha": task["contract"]["base_sha"],
                "event_first": events[0]["cursor"], "event_first_hash": event_hash(events[0]),
                "event_last": events[-1]["cursor"], "event_last_hash": event_hash(events[-1]), "nodes": source_nodes,
            },
            "overlay": {
                "workers": [{"node_id": "work", "attempt": 1, "actual_model": "gpt-5.6-luna", "checks": ["legacy focused test"], "artifacts": {"patch": patch, "worker-log": worker_log}}],
                "verifiers": [],
            },
        }
        if supplemental:
            review_task, review_source = self._review_task(task)
            review_result = {node["node_id"]: node for node in review_task["nodes"]}["review-verify"]["result"]
            manifest["overlay"]["supplemental_sol_review"] = {
                "patch": patch, "worker_artifacts": [patch, worker_log],
                "source_verifier": {"node_id": "verify", "attempt": 1}, "review_source": review_source,
                "test_log": self._descriptor(review_result["artifacts"]["test-log"]),
                "verdict_artifact": self._descriptor(review_result["artifacts"]["verdict"]),
                "review_transcript": self._descriptor(review_result["artifacts"]["review-transcript"]),
                "review_receipt": self._descriptor(review_result["artifacts"]["receipt"]),
                "evidence": [self._descriptor(ref) for ref in review_result["evidence"]],
            }
        else:
            manifest["overlay"]["verifiers"] = [{
                "node_id": "verify", "attempt": 1, "actual_model": "gpt-5.6-sol", "checks": ["legacy test"],
                "artifacts": {"test-log": source_verifier_log, "verdict": source_verifier_log},
                "evidence": [source_verifier_log],
            }]
        return manifest

    def _apply(self, manifest: dict, command_id: str = "legacy-remediation") -> dict:
        ref = self.store.artifacts.put_text(json.dumps(manifest), "json")
        return self.store.remediate_legacy_evidence(command_id, ref)


    def test_offline_legacy_task_is_remediated_append_only_and_idempotent(self) -> None:
        task = self._source_task("subscription-e2e-20260825-v2")
        before_task = copy.deepcopy(task)
        before_events = copy.deepcopy(self.store.read_events(task_id=task["task_id"], limit=1000))
        manifest = self._manifest(task["task_id"])
        first = self._apply(manifest)
        second = self._apply(manifest)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["event_cursor"], second["event_cursor"])
        self.assertEqual(self.store.get_task(task["task_id"]), before_task)
        self.assertEqual(self.store.read_events(task_id=task["task_id"], limit=1000)[:-1], before_events)
        with patch.object(self.store, "read_events", return_value=[]):
            self.assertEqual(len(self.store.legacy_evidence_remediations()), 1)
        self.assertEqual(build_acceptance_report(self.store)["checks"][8]["status"], "ok")

    def test_unstructured_source_sol_result_requires_an_independent_review_task(self) -> None:
        task = self._source_task("legacy-sol-without-native-verdict")
        verifier = next(node for node in task["nodes"] if node["node_id"] == "verify")
        legacy_result = dict(verifier["result"])
        for field in ("result_kind", "verdict", "checks", "evidence"):
            legacy_result.pop(field, None)
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'verify'",
                (canonical_json(legacy_result), task["task_id"]),
            )

        with self.assertRaisesRegex(ValueError, "structured accepted source Codex Sol"):
            self._apply(self._manifest(task["task_id"]), "unstructured-source-direct")

        receipt = self._apply(
            self._manifest(task["task_id"], supplemental=True),
            "unstructured-source-reviewed",
        )
        self.assertFalse(receipt["idempotent"])

    def test_command_conflict_and_invalid_bindings_are_rejected(self) -> None:
        task = self._source_task("binding-legacy")
        manifest = self._manifest(task["task_id"])
        self._apply(manifest, "one-command")
        changed = copy.deepcopy(manifest)
        changed["overlay"]["workers"][0]["checks"] = ["different request"]
        with self.assertRaises(CommandConflictError):
            self._apply(changed, "one-command")
        for label, mutate in {
            "cross-task": lambda item: item["source"].__setitem__("task_id", "other-task"),
            "contract-hash": lambda item: item["source"].__setitem__("contract_hash", "bad"),
            "cursor": lambda item: item["source"]["nodes"][0].__setitem__("accepted_cursor", 999999),
            "attempt": lambda item: item["source"]["nodes"][0].__setitem__("attempt", 2),
        }.items():
            with self.subTest(label=label):
                invalid = copy.deepcopy(manifest)
                mutate(invalid)
                with self.assertRaises(ValueError):
                    self._apply(invalid, f"invalid-{label}")

    def test_forged_generic_event_is_not_an_overlay(self) -> None:
        task = self._source_task("forged-event")
        manifest = self._manifest(task["task_id"])
        ref = self.store.artifacts.put_text(json.dumps(manifest), "json")
        self.store.record_system_event("acceptance.evidence_remediated", {"manifest_ref": ref, "task_id": task["task_id"]})
        self.assertEqual(self.store.legacy_evidence_remediations(), [])
        self.assertEqual(build_acceptance_report(self.store)["checks"][8]["status"], "error")

    def test_deterministic_verifier_needs_bound_supplemental_sol_review(self) -> None:
        task = self._source_task("acceptance-v0.4.0-fallback", verifier_model="local-deterministic")
        source_verifier = {node["node_id"]: node for node in task["nodes"]}["verify"]
        self.assertEqual(source_verifier["executor"], "deterministic")
        self.assertEqual(source_verifier["effective_executor"], "deterministic")
        self.assertEqual(source_verifier["model"], "local")
        self.assertEqual(source_verifier["effective_model"], "local")
        self.assertIsNone(source_verifier["result"]["actual_model"])
        missing_sol = self._manifest(task["task_id"], supplemental=False)
        missing_sol["overlay"]["verifiers"][0]["actual_model"] = "local-deterministic"
        with self.assertRaises(ValueError):
            self._apply(missing_sol, "fallback-no-sol")
        receipt = self._apply(self._manifest(task["task_id"], supplemental=True), "fallback-sol")
        self.assertFalse(receipt["idempotent"])
        self.assertEqual(build_acceptance_report(self.store)["checks"][8]["status"], "ok")

    def test_manifest_written_static_supplemental_review_is_rejected(self) -> None:
        task = self._source_task("supplemental-binding", verifier_model="local-deterministic")
        manifest = self._manifest(task["task_id"], supplemental=True)
        supplemental = manifest["overlay"]["supplemental_sol_review"]
        del supplemental["review_source"]
        supplemental["checks"] = ["manifest self-attested Sol review"]
        supplemental["review_transcript"] = self._artifact("static transcript", "review.log")
        supplemental["review_receipt"] = self._artifact("static receipt", "receipt")
        with self.assertRaises(ValueError):
            self._apply(manifest, "tampered-supplemental")

    def test_overlay_rejects_artifact_borrowed_from_another_task(self) -> None:
        task = self._source_task("borrowed-source")
        other = self._source_task("borrowed-other")
        manifest = self._manifest(task["task_id"])
        manifest["overlay"]["workers"][0]["artifacts"]["patch"] = self._descriptor(
            {node["node_id"]: node for node in other["nodes"]}["work"]["result"]["artifacts"]["patch"]
        )
        with self.assertRaises(ValueError):
            self._apply(manifest, "borrowed-worker-artifact")

    def test_supplemental_review_rejects_cross_task_and_forged_event_bindings(self) -> None:
        task = self._source_task("review-binding-source", verifier_model="local-deterministic")
        other = self._source_task("review-binding-other", verifier_model="local-deterministic")
        manifest = self._manifest(task["task_id"], supplemental=True)
        other_manifest = self._manifest(other["task_id"], supplemental=True)
        cross_task = copy.deepcopy(manifest)
        cross_task["overlay"]["supplemental_sol_review"]["review_source"] = (
            other_manifest["overlay"]["supplemental_sol_review"]["review_source"]
        )
        with self.assertRaises(ValueError):
            self._apply(cross_task, "cross-review-task")
        forged_event = copy.deepcopy(manifest)
        forged_event["overlay"]["supplemental_sol_review"]["review_source"]["accepted_hash"] = "forged"
        with self.assertRaises(ValueError):
            self._apply(forged_event, "forged-review-event")

    def test_supplemental_review_requires_a_worker_that_materialized_the_source_patch(self) -> None:
        task = self._source_task("review-worker-binding", verifier_model="local-deterministic")
        manifest = self._manifest(task["task_id"], supplemental=True)
        review_task_id = manifest["overlay"]["supplemental_sol_review"]["review_source"]["task_id"]
        review_task = self.store.get_task(review_task_id)
        review_worker = next(node for node in review_task["nodes"] if node["node_id"] == "review-work")
        invalid_result = dict(review_worker["result"])
        invalid_result["artifacts"] = {}
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE nodes SET result_json = ? WHERE task_id = ? AND node_id = 'review-work'",
                (canonical_json(invalid_result), review_task_id),
            )

        with self.assertRaisesRegex(ValueError, "did not materialize and check the source patch"):
            self._apply(manifest, "review-worker-missing-patch")

    def test_supplemental_rejects_arbitrary_test_log_and_missing_transcript(self) -> None:
        task = self._source_task("supplemental-artifacts", verifier_model="local-deterministic")
        arbitrary_log = self._manifest(task["task_id"], supplemental=True)
        review = arbitrary_log["overlay"]["supplemental_sol_review"]
        review["test_log"] = review["patch"]
        review["evidence"] = [review["patch"], review["verdict_artifact"]]
        with self.assertRaises(ValueError):
            self._apply(arbitrary_log, "arbitrary-test-log")
        missing_transcript = self._manifest(task["task_id"], supplemental=True)
        del missing_transcript["overlay"]["supplemental_sol_review"]["review_transcript"]
        with self.assertRaises(ValueError):
            self._apply(missing_transcript, "missing-review-transcript")
