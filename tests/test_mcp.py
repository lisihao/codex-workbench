from __future__ import annotations

import json
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
from codex_workbench.mcp import WorkbenchMCPServer
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

    def call(self, name: str, arguments: dict) -> dict:
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        self.assertIsNotNone(response)
        return response["result"]

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
                "workbench_request",
                "workbench_get_session",
                "workbench_continue_session",
                "workbench_harness_health",
                "workbench_sync_github",
                "workbench_list_tasks",
                "workbench_inspect_task",
                "workbench_control_task",
                "workbench_deliver_github",
                "workbench_read_events",
                "workbench_read_artifact",
                "workbench_acceptance_report",
                "workbench_list_approvals",
                "workbench_decide_approval",
            },
        )

        report = json.loads(self.call("workbench_acceptance_report", {})["content"][0]["text"])
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["checks"]), 11)
        self.assertEqual(report["backlog"][0]["id"], "A2")

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
            "codex_workbench.mcp.submit_natural_language_request",
            return_value={"ok": True, "task_id": "routed"},
        ) as submit:
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
        kwargs = submit.call_args.kwargs
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
            "codex_workbench.mcp.submit_natural_language_request",
            return_value={"ok": True, "task_id": "from-context"},
        ) as submit:
            result = json.loads(
                self.call(
                    "workbench_request",
                    {"objective": "continue", "source_thread_id": "thread-wb"},
                )["content"][0]["text"]
            )
        self.assertEqual(result["task_id"], "from-context")
        kwargs = submit.call_args.kwargs
        self.assertEqual(kwargs["repository"], str(self.root))
        self.assertEqual(kwargs["allowed_scope"], ["src", "tests"])
        self.assertEqual(kwargs["context_bundle_ref"], context_ref)
        binding = json.loads(
            self.call("workbench_get_session", {"source_thread_id": "thread-wb"})["content"][0]["text"]
        )
        self.assertNotIn("context_excerpt", binding)

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
            self.call("workbench_control_task", {"task_id": "mcp-task", "action": "queue"})["content"][0]["text"]
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
