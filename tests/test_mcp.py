from __future__ import annotations

import json
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.archify import (
    ARCHIFY_COMMIT,
    ARCHIFY_LICENSE,
    ARCHIFY_MANAGED_BY,
    ARCHIFY_MANAGED_MARKER_FILENAME,
    ARCHIFY_REPOSITORY,
    ARCHIFY_TAG,
    ARCHIFY_VERSION,
)
from codex_workbench.config import WorkbenchConfig
from codex_workbench.governance import (
    CODE_AS_HARNESS_POLICY_REQUIRED_TEXT,
    CODE_AS_HARNESS_POLICY_END,
    CODE_AS_HARNESS_POLICY_START,
    CODE_AS_HARNESS_PROFILE,
)
from codex_workbench.mcp import (
    LIST_TASKS_MAX_LIMIT,
    LIST_TASKS_MAX_RESPONSE_BYTES,
    WorkbenchMCPServer,
    serve_stdio,
)
from codex_workbench.dirty_worktree_recovery import DirtyWorktreeRecoveryError
from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.store import WorkbenchStore


ROOT = Path(__file__).resolve().parents[1]
PHYSICAL_TMP = Path(tempfile.gettempdir()).resolve()


class MCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = WorkbenchConfig(self.root)
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("mcp-test", "test-machine")
        self.server = WorkbenchMCPServer(self.config, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_recovery_error_does_not_close_stdio_connection(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "workbench_resume_blocked_worktree", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ]
        output = io.StringIO()
        with patch.object(WorkbenchMCPServer, "_tool_result", side_effect=
                          DirtyWorktreeRecoveryError("recovery worktree no longer matches its contract base")):
            serve_stdio(self.config, self.store,
                        io.StringIO("\n".join(json.dumps(item) for item in requests)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("contract base", responses[0]["result"]["content"][0]["text"])
        self.assertEqual(responses[1]["result"], {})
        self.assertTrue(responses[2]["result"]["tools"])

    def call(self, name: str, arguments: dict) -> dict:
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        self.assertIsNotNone(response)
        return response["result"]

    def _create_list_task(
        self,
        task_id: str,
        *,
        objective: str = "list task fixture",
        prompt: str = "fixture work",
    ) -> TaskContract:
        contract = TaskContract(
            task_id=task_id,
            repository=str(self.root),
            base_sha="fixture-base",
            objective=objective,
            allowed_scope=("tests",),
        )
        self.store.create_task(
            contract,
            [
                NodeSpec(
                    "work",
                    task_id,
                    "work",
                    "fixture",
                    "fixture",
                    prompt,
                ),
                NodeSpec(
                    "verify",
                    task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            f"{task_id}-create",
        )
        return contract

    @staticmethod
    def _harness_binaries(home: Path) -> tuple[str, str]:
        bin_dir = home / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        companion = bin_dir / "codex-code-mode-host"
        claude = bin_dir / "claude"
        for binary in (codex, companion, claude):
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
        return str(codex), str(claude)

    @staticmethod
    def _install_pinned_archify(home: Path) -> None:
        vendor = ROOT / "vendor" / "archify"
        for agent, relative, marker_agent in (
            ("codex", Path(".codex") / "skills" / "archify", "codex"),
            ("claude-code", Path(".claude") / "skills" / "archify", "claude"),
        ):
            target = home / relative
            shutil.copytree(vendor, target)
            (target / ARCHIFY_MANAGED_MARKER_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "managed_by": ARCHIFY_MANAGED_BY,
                        "skill": "archify",
                        "agent": marker_agent,
                        "repository": ARCHIFY_REPOSITORY,
                        "tag": ARCHIFY_TAG,
                        "commit": ARCHIFY_COMMIT,
                        "version": ARCHIFY_VERSION,
                        "license": ARCHIFY_LICENSE,
                    }
                ),
                encoding="utf-8",
            )

    def test_lists_codex_native_tools(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "workbench_validate_blocked_node",
                "workbench_configure_node_recovery",
                "workbench_get_node_recovery",
                "workbench_amend_task_acceptance",
                "workbench_request",
                "workbench_get_request",
                "workbench_get_session",
                "workbench_continue_session",
                "workbench_harness_health",
                "workbench_sync_github",
                "workbench_list_tasks",
                "workbench_inspect_task",
                "workbench_control_task",
                "workbench_deliver_github",
                "workbench_create_delivery_objective",
                "workbench_get_delivery_objective",
                "workbench_read_events",
                "workbench_read_artifact",
                "workbench_acceptance_report",
                "workbench_worktree_status",
                "workbench_reclaim_worktrees",
                "workbench_restore_worktree",
                "workbench_list_approvals",
                "workbench_decide_approval",
            },
        )

        report = json.loads(self.call("workbench_acceptance_report", {})["content"][0]["text"])
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["checks"]), 12)
        self.assertEqual(report["backlog"], [])

        recovery = json.loads(
            self.call("workbench_worktree_status", {})["content"][0]["text"]
        )
        self.assertTrue(recovery["enabled"])
        self.assertEqual(recovery["allocations"], [])
        self.assertEqual(recovery["archives"], [])
        self.assertIsNone(recovery["home_presence"])

        swept = json.loads(
            self.call("workbench_reclaim_worktrees", {"max_items": 1})["content"][0]["text"]
        )
        self.assertEqual(swept["status"], "idle")

    def test_list_tasks_has_deterministic_bounded_pagination(self) -> None:
        for task_id in ("mcp-list-c", "mcp-list-a", "mcp-list-b"):
            self._create_list_task(task_id)

        first_text = self.call("workbench_list_tasks", {"limit": 2})["content"][0]["text"]
        first_page = json.loads(first_text)
        repeated_page = json.loads(
            self.call("workbench_list_tasks", {"limit": 2})["content"][0]["text"]
        )

        self.assertEqual(first_page, repeated_page)
        self.assertEqual(
            [task["task_id"] for task in first_page["tasks"]],
            ["mcp-list-c", "mcp-list-a"],
        )
        self.assertEqual(len(first_page["tasks"]), 2)
        self.assertIsInstance(first_page["next_cursor"], str)
        self.assertTrue(first_page["next_cursor"].isdigit())
        self.assertGreater(int(first_page["next_cursor"]), 0)
        self.assertEqual(
            set(first_page["tasks"][0]),
            {
                "task_id",
                "task_id_truncated",
                "task_ref",
                "state",
                "state_revision",
                "priority",
                "contract_hash",
                "created_at",
                "updated_at",
                "node_counts",
            },
        )
        self.assertEqual(first_page["tasks"][0]["node_counts"]["total"], 2)
        self.assertEqual(first_page["tasks"][0]["node_counts"]["pending"], 2)

        second_page = json.loads(
            self.call(
                "workbench_list_tasks",
                {"limit": 2, "cursor": first_page["next_cursor"]},
            )["content"][0]["text"]
        )
        self.assertEqual([task["task_id"] for task in second_page["tasks"]], ["mcp-list-b"])
        self.assertIsNone(second_page["next_cursor"])

        invalid_limit = self.call(
            "workbench_list_tasks", {"limit": LIST_TASKS_MAX_LIMIT + 1}
        )
        self.assertTrue(invalid_limit["isError"])
        self.assertIn("limit must be between", invalid_limit["content"][0]["text"])

    def test_list_tasks_omits_unbounded_node_payloads_and_caps_response(self) -> None:
        private_objective = "LIST_OBJECTIVE_MUST_NOT_LEAK"
        private_prompt = "LIST_PROMPT_MUST_NOT_LEAK"
        private_result = "LIST_RESULT_MUST_NOT_LEAK"
        private_artifact = "LIST_ARTIFACT_BODY_MUST_NOT_LEAK"
        task_id = "mcp-list-" + ("very-long-task-id-" * 32)
        contract = self._create_list_task(
            task_id,
            objective=private_objective,
            prompt=private_prompt,
        )
        changed_paths = tuple(
            f"node_modules/fixture-{index:05d}/generated-output-file.js"
            for index in range(12_000)
        )
        private_artifact_ref = ArtifactStore(self.root / "artifacts").put_text(
            private_artifact, "txt"
        )
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["node_id"], "work")
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                private_result,
                artifacts={"worker-output": private_artifact_ref},
                changed_paths=changed_paths,
            ),
        )

        response = self.call("workbench_list_tasks", {"limit": 1})
        serialized = response["content"][0]["text"]
        listed = json.loads(serialized)
        summary = listed["tasks"][0]

        self.assertLessEqual(
            len(serialized.encode("utf-8")), LIST_TASKS_MAX_RESPONSE_BYTES
        )
        self.assertLess(len(serialized.encode("utf-8")), 8 * 1024)
        self.assertTrue(summary["task_id_truncated"])
        self.assertTrue(summary["task_id"].endswith("…"))
        self.assertNotIn("node_modules", serialized)
        for private_value in (
            private_objective,
            private_prompt,
            private_result,
            private_artifact,
        ):
            self.assertNotIn(private_value, serialized)
        for forbidden_field in (
            '"prompt"',
            '"result"',
            '"worktree"',
            '"artifacts"',
            '"changed_paths"',
        ):
            self.assertNotIn(forbidden_field, serialized)

        inspected = json.loads(
            self.call(
                "workbench_inspect_task", {"task_ref": summary["task_ref"]}
            )["content"][0]["text"]
        )
        work_node = next(node for node in inspected["nodes"] if node["node_id"] == "work")
        self.assertEqual(inspected["task_id"], task_id)
        self.assertEqual(work_node["prompt"], private_prompt)
        self.assertEqual(work_node["result"]["summary"], private_result)
        self.assertEqual(len(work_node["result"]["changed_paths"]), len(changed_paths))

    def test_continue_session_appends_steering_without_terminating_active_task(self) -> None:
        context_ref = "sha256:" + "e" * 64 + ":tar.gz"
        self.store.record_session_context(
            command_id="mcp-continue-context",
            request_hash="mcp-continue-request",
            source_thread_id="mcp-thread-active",
            context_ref=context_ref,
            archive_ref=context_ref,
            manifest={"schema_version": 1},
            repository=str(self.root),
            base_sha="fixture-base",
            allowed_scopes=("tests",),
            context_excerpt="continue this task",
        )
        contract = TaskContract(
            task_id="mcp-continue-task",
            repository=str(self.root),
            base_sha="fixture-base",
            objective="preserve this MCP objective",
            allowed_scope=("tests",),
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("work", contract.task_id, "work", "fixture", "fixture", "ok"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            "mcp-continue-task-create",
        )
        self.store.bind_task_to_session("mcp-thread-active", contract.task_id)
        self.store.queue_task(contract.task_id)
        self.store.claim_ready_node("fixture-worker", self.epoch)
        before = self.store.get_task(contract.task_id)

        result = json.loads(
            self.call(
                "workbench_continue_session",
                {
                    "source_thread_id": "mcp-thread-active",
                    "instruction": "继续当前目标并检查新增边界。",
                },
            )["content"][0]["text"]
        )

        self.assertEqual(result["task_id"], contract.task_id)
        self.assertEqual(result["revision"], before["state_revision"] + 1)
        self.assertEqual(result["state"], "running")
        self.assertTrue(result["steering_id"])
        after = self.store.get_task(contract.task_id)
        self.assertEqual(after["state"], "running")
        self.assertEqual(after["contract"], before["contract"])
        self.assertEqual(after["steering"][-1]["instruction"], "继续当前目标并检查新增边界。")

    def test_control_requires_strict_revision_and_atomic_instruction_validation(self) -> None:
        contract = TaskContract(
            task_id="mcp-control-cas",
            repository=str(self.root),
            base_sha="fixture",
            objective="MCP control CAS",
            allowed_scope=("tests",),
        )
        self.store.create_task(
            contract,
            [
                NodeSpec("work", contract.task_id, "work", "fixture", "fixture", "ok"),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            "mcp-control-cas-create",
        )

        for invalid_revision in (True, "1", None):
            arguments = {"task_id": contract.task_id, "action": "queue"}
            if invalid_revision is not None:
                arguments["expected_revision"] = invalid_revision
            response = self.call("workbench_control_task", arguments)
            self.assertTrue(response["isError"])
            self.assertIn("expected_revision must be an integer", response["content"][0]["text"])
            self.assertEqual(self.store.get_task(contract.task_id)["state"], "inbox")
            self.assertEqual(self.store.get_task(contract.task_id)["state_revision"], 1)

        for invalid_instruction in (None, "", "   ", "x" * 501):
            response = self.call(
                "workbench_control_task",
                {
                    "task_id": contract.task_id,
                    "action": "queue",
                    "expected_revision": 1,
                    "instruction": invalid_instruction,
                },
            )
            self.assertTrue(response["isError"])
            self.assertEqual(self.store.get_task(contract.task_id)["state"], "inbox")
            self.assertEqual(self.store.get_task(contract.task_id)["state_revision"], 1)
            self.assertEqual(self.store.get_task(contract.task_id)["steering"], [])

        queued = json.loads(
            self.call(
                "workbench_control_task",
                {
                    "task_id": contract.task_id,
                    "action": "queue",
                    "expected_revision": 1,
                    "instruction": "保留公开接口",
                },
            )["content"][0]["text"]
        )
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["revision"], 3)
        self.assertTrue(queued["steering"]["steering_id"])
        self.assertIn("delivery", queued["steering"])

        steered = json.loads(
            self.call(
                "workbench_control_task",
                {
                    "task_id": contract.task_id,
                    "action": "steer",
                    "expected_revision": queued["revision"],
                    "instruction": "补充回归测试",
                },
            )["content"][0]["text"]
        )
        self.assertTrue(steered["steering_id"])
        self.assertEqual(steered["revision"], 4)
        self.assertIn("delivery", steered)

    def test_control_resume_retries_clean_block_only_after_explicit_assertion(self) -> None:
        contract = TaskContract(
            task_id="mcp-clean-blocked-resume",
            repository=str(self.root),
            base_sha="fixture",
            objective="resume a clean blocked attempt",
            allowed_scope=("tests",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        self.store.create_task(
            contract,
            [
                NodeSpec(
                    "work",
                    contract.task_id,
                    "work",
                    "fixture",
                    "fixture",
                    "block without side effects",
                ),
                NodeSpec(
                    "verify",
                    contract.task_id,
                    "verify",
                    "fixture",
                    "fixture",
                    "accepted",
                    depends_on=("work",),
                    verifier=True,
                ),
            ],
            "mcp-clean-blocked-resume-create",
        )
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        assert claimed is not None
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "blocked",
                "fixture stopped before side effects",
                actual_model="fixture",
                result_kind="worker",
                changed_paths=(),
                checks=("fixture",),
            ),
        )
        blocked = self.store.get_task(contract.task_id)

        missing_assertion = self.call(
            "workbench_control_task",
            {
                "task_id": contract.task_id,
                "action": "resume",
                "expected_revision": blocked["state_revision"],
                "node_id": "work",
                "expected_attempt": 1,
                "reason": "retry the clean local attempt",
            },
        )
        self.assertTrue(missing_assertion["isError"])
        self.assertIn("no-side-effects", missing_assertion["content"][0]["text"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

        stale = self.call(
            "workbench_control_task",
            {
                "task_id": contract.task_id,
                "action": "resume",
                "expected_revision": blocked["state_revision"] - 1,
                "node_id": "work",
                "expected_attempt": 1,
                "reason": "retry the clean local attempt",
                "confirm_no_side_effects": True,
            },
        )
        self.assertTrue(stale["isError"])
        self.assertIn("expected task revision", stale["content"][0]["text"])
        self.assertEqual(self.store.get_task(contract.task_id), blocked)

        before_events = self.store.read_events(task_id=contract.task_id)
        for flag in (True, True, "true", 1, None):
            response = self.call("workbench_control_task", {
                "task_id": contract.task_id, "action": "resume",
                "expected_revision": blocked["state_revision"], "node_id": "work",
                "expected_attempt": 1, "reason": "preview the clean local attempt",
                "confirm_no_side_effects": True, "dry_run": flag,
            })
            if flag is True:
                preview = json.loads(response["content"][0]["text"])
                self.assertTrue(preview["dry_run"])
                self.assertEqual(preview["task"]["state"], "blocked")
                self.assertEqual(preview["would_retry"]["attempt"], 2)
            else:
                self.assertTrue(response["isError"])
                self.assertIn("dry_run must be a boolean", response["content"][0]["text"])
            self.assertEqual(self.store.get_task(contract.task_id), blocked)
            self.assertEqual(self.store.read_events(task_id=contract.task_id), before_events)

        resumed = json.loads(
            self.call(
                "workbench_control_task",
                {
                    "task_id": contract.task_id,
                    "action": "resume",
                    "expected_revision": blocked["state_revision"],
                    "node_id": "work",
                    "expected_attempt": 1,
                    "reason": "retry the clean local attempt",
                    "confirm_no_side_effects": True,
                },
            )["content"][0]["text"]
        )
        self.assertEqual(resumed["action"], "retry-blocked")
        self.assertEqual(resumed["task"]["state"], "queued")
        self.assertEqual(resumed["next_attempt"], 2)

    def test_unsupported_control_dry_run_does_not_queue(self) -> None:
        self._create_list_task("unsupported-dry-run")
        before = self.store.get_task("unsupported-dry-run")
        events = self.store.read_events(task_id="unsupported-dry-run")
        response = self.call("workbench_control_task", {
            "task_id": "unsupported-dry-run", "action": "queue",
            "expected_revision": before["state_revision"], "dry_run": True,
        })
        self.assertTrue(response["isError"])
        self.assertIn("dry_run is not supported", response["content"][0]["text"])
        self.assertEqual(self.store.get_task("unsupported-dry-run"), before)
        self.assertEqual(self.store.read_events(task_id="unsupported-dry-run"), events)

    def test_source_only_flags_are_rejected_outside_local_indeterminate_recovery(self) -> None:
        self._create_list_task("unsupported-source-only")
        before = self.store.get_task("unsupported-source-only")
        events = self.store.read_events(task_id="unsupported-source-only")
        for arguments, expected in (
            ({"source_only": True}, "only supported"),
            ({"confirm_preserve_unknown_ignored": True}, "only supported"),
            ({"source_only": "true"}, "must be a boolean"),
            ({"confirm_preserve_unknown_ignored": 1}, "must be a boolean"),
            ({"confirm_source_only_extraction": True}, "only supported"),
            ({"confirm_source_only_extraction": False}, "only supported"),
            ({"expected_source_delta_sha256": "a" * 64}, "only supported"),
        ):
            response = self.call(
                "workbench_control_task",
                {
                    "task_id": "unsupported-source-only",
                    "action": "queue",
                    "expected_revision": before["state_revision"],
                    **arguments,
                },
            )
            self.assertTrue(response["isError"])
            self.assertIn(expected, response["content"][0]["text"])
            self.assertEqual(self.store.get_task("unsupported-source-only"), before)
            self.assertEqual(self.store.read_events(task_id="unsupported-source-only"), events)

    def test_scope_normalization_fields_cannot_silently_queue(self) -> None:
        self._create_list_task("unsupported-normalization")
        before = self.store.get_task("unsupported-normalization")
        events = self.store.read_events(task_id="unsupported-normalization")
        for field, value in (
            ("scope_pattern", "tests/task*.ts"), ("exact_path", "tests/task.spec.ts"),
            ("expected_file_sha256", "a" * 64), ("confirm_scope_normalization", False),
        ):
            with self.subTest(field=field):
                response = self.call("workbench_control_task", {
                    "task_id": "unsupported-normalization", "action": "queue",
                    "expected_revision": before["state_revision"], field: value,
                })
                self.assertTrue(response["isError"])
                self.assertIn("only supported", response["content"][0]["text"])
                self.assertEqual(self.store.get_task("unsupported-normalization"), before)
                self.assertEqual(self.store.read_events(task_id="unsupported-normalization"), events)

    def test_scope_normalization_dispatch_preserves_preview_and_cas_arguments(self) -> None:
        arguments = {
            "task_id": "legacy", "node_id": "A", "action": "normalize_indeterminate_scope",
            "expected_revision": 11, "expected_attempt": 2,
            "scope_pattern": "tests/task*.ts", "exact_path": "tests/task.spec.ts",
            "reason": "Normalize legacy planner scope", "dry_run": True,
        }
        with patch.object(self.store, "normalize_indeterminate_scope", create=True,
                          return_value={"dry_run": True, "revision_after": 11}) as normalize:
            response = self.call("workbench_control_task", arguments)
            self.assertNotIn("isError", response)
            self.assertTrue(json.loads(response["content"][0]["text"])["dry_run"])
            normalize.assert_called_once_with(
                "legacy", "A", expected_revision=11, expected_attempt=2,
                scope_pattern="tests/task*.ts", exact_path="tests/task.spec.ts",
                reason="Normalize legacy planner scope", expected_file_sha256=None,
                confirm_scope_normalization=False, dry_run=True,
            )
            normalize.reset_mock()
            for invalid in ({"dry_run": "true"}, {"confirm_scope_normalization": 1},
                            {"expected_file_sha256": "not-a-hash"}, {"expected_attempt": True}):
                response = self.call("workbench_control_task", {**arguments, **invalid})
                self.assertTrue(response["isError"])
                normalize.assert_not_called()

    def test_harness_health_requires_real_skill_and_policy_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            canonical_skill = (
                Path(__file__).resolve().parents[1]
                / "skills"
                / "code-as-harness"
                / "SKILL.md"
            ).read_text()
            codex_binary, claude_binary = self._harness_binaries(home)
            self._install_pinned_archify(home)
            for root, policy_name in ((".codex", "AGENTS.md"), (".claude", "CLAUDE.md")):
                skill = home / root / "skills" / "code-as-harness" / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(canonical_skill)
                policy = (
                    f"{CODE_AS_HARNESS_POLICY_START}\n"
                    f"Profile: {CODE_AS_HARNESS_PROFILE}\n"
                    + "\n".join(CODE_AS_HARNESS_POLICY_REQUIRED_TEXT)
                    + f"\nTarget agent: `{'codex' if root == '.codex' else 'claude-code'}`.\n"
                    + f"{CODE_AS_HARNESS_POLICY_END}\n"
                )
                (home / root / policy_name).write_text(policy)
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(home),
                    "CODEX_WORKBENCH_CODEX": codex_binary,
                    "CODEX_WORKBENCH_CLAUDE": claude_binary,
                },
                clear=False,
            ):
                health = json.loads(
                    self.call("workbench_harness_health", {})["content"][0]["text"]
                )

        self.assertTrue(health["ok"])
        self.assertEqual(health["profile"], CODE_AS_HARNESS_PROFILE)
        self.assertEqual(health["device"], "macbook")
        self.assertEqual(health["execution_path"], "mcp-to-authority")
        self.assertEqual(health["health_probe"], "filesystem-and-static-wiring")
        self.assertEqual(health["max_safe_parallelism"], self.config.max_workers)
        self.assertFalse(health["authentication_checked"])
        self.assertFalse(health["model_called"])
        self.assertTrue(health["executors"]["codex"]["executable"])
        self.assertTrue(health["executors"]["codex"]["companion_executable"])
        self.assertTrue(health["executors"]["claude"]["executable"])
        self.assertNotIn("governance_injected", health["executors"]["codex"])
        self.assertTrue(health["skill_artifacts"]["codex"]["installed"])
        self.assertTrue(health["skill_artifacts"]["claude-code"]["installed"])
        self.assertTrue(health["global_policies"]["codex"]["installed"])
        self.assertTrue(health["global_policies"]["claude-code"]["installed"])
        self.assertTrue(health["global_policies"]["codex"]["target_agent_declared"])
        self.assertEqual(
            health["workbench_managed_injection"]["status"],
            "compatible-managed-capability",
        )
        self.assertTrue(
            health["workbench_managed_injection"]["executors"]["codex"]["static_wiring_verified"]
        )
        self.assertFalse(
            health["workbench_managed_injection"]["executors"]["codex"]["runtime_execution_observed"]
        )
        self.assertTrue(health["readiness"]["archify_pinned_vendor_projection_and_installations"])
        self.assertTrue(health["archify"]["vendor"]["ok"])
        self.assertTrue(health["archify"]["projection"]["ok"])
        self.assertTrue(health["archify"]["installations"]["codex"]["ok"])
        self.assertTrue(health["archify"]["installations"]["claude-code"]["ok"])
        self.assertFalse(health["archify"]["authentication_checked"])
        self.assertFalse(health["archify"]["model_called"])

    def test_harness_health_rejects_a_marker_only_policy(self) -> None:
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            canonical_skill = (
                Path(__file__).resolve().parents[1]
                / "skills"
                / "code-as-harness"
                / "SKILL.md"
            ).read_text()
            codex_binary, claude_binary = self._harness_binaries(home)
            for root, policy_name in ((".codex", "AGENTS.md"), (".claude", "CLAUDE.md")):
                skill = home / root / "skills" / "code-as-harness" / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(canonical_skill)
                (home / root / policy_name).write_text(
                    f"{CODE_AS_HARNESS_POLICY_START}\n"
                    f"Profile: {CODE_AS_HARNESS_PROFILE}\n"
                    f"{CODE_AS_HARNESS_POLICY_END}\n"
                )
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(home),
                    "CODEX_WORKBENCH_CODEX": codex_binary,
                    "CODEX_WORKBENCH_CLAUDE": claude_binary,
                },
                clear=False,
            ):
                health = json.loads(
                    self.call("workbench_harness_health", {})["content"][0]["text"]
                )

        self.assertFalse(health["ok"])
        self.assertTrue(health["readiness"]["executor_binaries"])
        self.assertTrue(health["readiness"]["canonical_skill_artifacts"])
        self.assertFalse(health["readiness"]["managed_global_policies"])
        self.assertTrue(health["global_policies"]["codex"]["managed_block_present"])
        self.assertFalse(health["global_policies"]["codex"]["required_content_present"])

    def test_harness_health_rejects_metadata_and_policy_content_hidden_in_comments(self) -> None:
        with tempfile.TemporaryDirectory(dir=PHYSICAL_TMP) as directory:
            home = Path(directory)
            codex_binary, claude_binary = self._harness_binaries(home)
            fake_skill = (
                "---\n"
                "name: code-as-harness\n"
                "---\n"
                "<!-- codex_workbench_managed: true\n"
                "profile: code-as-harness/v1\n"
                "artifact_kind: workbench-canonical-compatible-skill\n"
                "## Operating contract\n"
                "Fill all safe independent work slots\n"
                "A matching L3 fingerprint has one full gate\n"
                "A later user message continues the active objective\n"
                "-->\n"
            )
            fake_policy = (
                f"{CODE_AS_HARNESS_POLICY_START}\n"
                "<!--\n"
                f"Profile: {CODE_AS_HARNESS_PROFILE}\n"
                + "\n".join(CODE_AS_HARNESS_POLICY_REQUIRED_TEXT)
                + "\n- Target agent: `codex`.\n"
                + "-->\n"
                + f"{CODE_AS_HARNESS_POLICY_END}\n"
            )
            for root, policy_name in ((".codex", "AGENTS.md"), (".claude", "CLAUDE.md")):
                skill = home / root / "skills" / "code-as-harness" / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(fake_skill)
                policy = home / root / policy_name
                policy.write_text(
                    fake_policy.replace(
                        "`codex`",
                        f"`{'codex' if root == '.codex' else 'claude-code'}`",
                    )
                )
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(home),
                    "CODEX_WORKBENCH_CODEX": codex_binary,
                    "CODEX_WORKBENCH_CLAUDE": claude_binary,
                },
                clear=False,
            ):
                health = json.loads(
                    self.call("workbench_harness_health", {})["content"][0]["text"]
                )

        self.assertFalse(health["ok"])
        self.assertFalse(health["readiness"]["canonical_skill_artifacts"])
        self.assertFalse(health["readiness"]["managed_global_policies"])
        self.assertFalse(health["skill_artifacts"]["codex"]["managed_marker_present"])
        self.assertFalse(health["global_policies"]["codex"]["required_content_present"])

    def test_request_exposes_and_forwards_routing_controls(self) -> None:
        with patch(
            "codex_workbench.mcp.enqueue_natural_language_request",
            return_value={"ok": True, "task_id": "routed"},
        ) as enqueue:
            result = json.loads(
                self.call(
                    "workbench_request",
                    {
                        "objective": "review the architecture",
                        "repository": str(self.root),
                        "allowed_scopes": ["src"],
                        "task_type": "architecture",
                        "complexity": "high",
                        "parallelizable": False,
                        "claude_allowed": False,
                        "task_points": 3.5,
                        "verification_tier": "L3",
                    },
                )["content"][0]["text"]
            )

        self.assertEqual(result["task_id"], "routed")
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["task_type"], "architecture")
        self.assertEqual(kwargs["complexity"], "high")
        self.assertFalse(kwargs["parallelizable"])
        self.assertFalse(kwargs["claude_allowed"])
        self.assertEqual(kwargs["task_points"], 3.5)
        self.assertEqual(kwargs["verification_tier"], "L3")

    def test_request_resolves_repository_and_scope_from_wb_binding(self) -> None:
        context_ref = "sha256:" + "b" * 64 + ":tar.gz"
        self.store.record_session_context(
            command_id="import-thread",
            request_hash="context-hash",
            source_thread_id="thread-wb",
            context_ref=context_ref,
            archive_ref=context_ref,
            manifest={"schema_version": 1},
            repository=str(self.root),
            base_sha="fixture-base",
            allowed_scopes=("src", "tests"),
            context_excerpt="prior requirement",
        )
        with patch(
            "codex_workbench.mcp.enqueue_natural_language_request",
            return_value={"ok": True, "task_id": "from-context"},
        ) as enqueue:
            result = json.loads(
                self.call(
                    "workbench_request",
                    {"objective": "continue", "source_thread_id": "thread-wb"},
                )["content"][0]["text"]
            )
        self.assertEqual(result["task_id"], "from-context")
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["repository"], str(self.root))
        self.assertEqual(kwargs["allowed_scope"], ["src", "tests"])
        self.assertEqual(kwargs["context_bundle_ref"], context_ref)
        binding = json.loads(
            self.call("workbench_get_session", {"source_thread_id": "thread-wb"})["content"][0]["text"]
        )
        self.assertNotIn("context_excerpt", binding)

    def test_get_request_reads_durable_planning_receipt(self) -> None:
        self.store.enqueue_planning_request(
            "mcp-planning-command",
            "mcp-planning-task",
            {
                "request_schema": "natural-language-planning-v1",
                "command_id": "mcp-planning-command",
                "task_id": "mcp-planning-task",
                "objective": "bounded planning",
                "context_excerpt": "private imported context",
            },
        )
        result = json.loads(
            self.call(
                "workbench_get_request",
                {"command_id": "mcp-planning-command"},
            )["content"][0]["text"]
        )
        self.assertEqual(result["command_id"], "mcp-planning-command")
        self.assertEqual(result["task_id"], "mcp-planning-task")
        self.assertIn(result["status"], {"pending", "running", "succeeded", "failed", "indeterminate"})
        self.assertEqual(result["request"]["objective"], "bounded planning")
        self.assertNotIn("context_excerpt", result["request"])
        self.assertFalse(result["context_excerpt_present"])

    def test_get_request_redacts_internal_planning_failure_diagnostics(self) -> None:
        command_id = "mcp-private-diagnostic"
        diagnostic = "planner stderr private context=customer-secret private prompt=never expose"
        self.store.enqueue_planning_request(
            command_id,
            "mcp-private-task",
            {"objective": "bounded planning"},
        )
        claimed = self.store.claim_planning_request(self.epoch)
        self.assertIsNotNone(claimed)
        self.store.fail_planning_request(
            command_id,
            int(claimed["attempt"]),
            self.epoch,
            diagnostic,
        )

        result = json.loads(
            self.call(
                "workbench_get_request",
                {"command_id": command_id},
            )["content"][0]["text"]
        )

        public_text = json.dumps(result, sort_keys=True)
        self.assertNotIn("customer-secret", public_text)
        self.assertNotIn("private prompt", public_text)
        self.assertEqual(result["error"]["type"], "planning-failed")
        self.assertTrue(result["error"]["error_ref"].startswith("sha256:"))

    def test_inspects_controls_and_reads_evidence_without_a_model_call(self) -> None:
        contract = TaskContract(
            task_id="mcp-task",
            repository=str(self.root),
            base_sha="fixture",
            objective="MCP fixture",
            allowed_scope=("tests",),
        )
        nodes = [
            NodeSpec("work", "mcp-task", "work", "fixture", "fixture", "ok"),
            NodeSpec("verify", "mcp-task", "verify", "fixture", "fixture", "accepted", depends_on=("work",), verifier=True),
        ]
        self.store.create_task(contract, nodes, "mcp-create")
        inspected = json.loads(self.call("workbench_inspect_task", {"task_id": "mcp-task"})["content"][0]["text"])
        self.assertEqual(inspected["state"], "inbox")
        controlled = json.loads(
            self.call(
                "workbench_control_task",
                {"task_id": "mcp-task", "action": "queue", "expected_revision": 1},
            )["content"][0]["text"]
        )
        self.assertEqual(controlled["revision"], 2)
        events = json.loads(self.call("workbench_read_events", {"task_id": "mcp-task"})["content"][0]["text"])
        self.assertIn("task.state_changed", {event["event_type"] for event in events})

        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        self.store.settle_claimed(
            claimed,
            NodeResult("indeterminate", "fixture outcome unknown"),
        )
        approvals = json.loads(
            self.call("workbench_list_approvals", {})["content"][0]["text"]
        )
        self.assertEqual(len(approvals), 1)
        decided = json.loads(
            self.call(
                "workbench_decide_approval",
                {
                    "approval_id": approvals[0]["approval_id"],
                    "decision": "retry",
                    "expected_revision": approvals[0]["task_revision"],
                },
            )["content"][0]["text"]
        )
        self.assertTrue(decided["ok"])
        self.assertEqual(self.store.get_task("mcp-task")["state"], "queued")

        ref = ArtifactStore(self.root / "artifacts").put_text("verified evidence", "txt")
        artifact = json.loads(self.call("workbench_read_artifact", {"artifact_ref": ref})["content"][0]["text"])
        self.assertEqual(artifact["text"], "verified evidence")
        self.assertFalse(artifact["truncated"])


if __name__ == "__main__":
    unittest.main()
