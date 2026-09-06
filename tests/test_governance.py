from __future__ import annotations

from pathlib import Path
import unittest

from codex_workbench.executors import ClaudeExecutor, CodexExecutor, ExecutionRequest
from codex_workbench.governance import governance_directive
from codex_workbench.model import TaskContract
from codex_workbench.planner import CodexPlanner


def make_contract() -> TaskContract:
    return TaskContract(
        task_id="governance-prompt",
        repository="/tmp/example",
        base_sha="abc123",
        objective="Implement the bounded policy update",
        allowed_scope=("src/codex_workbench", "tests"),
        acceptance_commands=("python -m unittest tests.test_governance",),
        complexity="standard",
        parallelizable=True,
    )


class GovernancePromptWiringTests(unittest.TestCase):
    def test_shared_continuation_policy_is_injected_once_per_prompt_builder(self) -> None:
        contract = make_contract()
        request = ExecutionRequest(
            task_id=contract.task_id,
            node_id="worker",
            attempt=1,
            contract=contract.to_dict(),
            spec={
                "title": "Update the governance wording",
                "prompt": "Implement and verify the bounded policy update.",
                "model": "gpt-5.6-luna",
                "model_profile": "luna_worker",
                "model_reasoning_effort": "max",
                "verifier": False,
                "write_scopes": ["src/codex_workbench/governance.py"],
            },
            worktree=Path("/tmp/example"),
        )
        policy = governance_directive(contract.to_dict())
        codex_prompt = CodexExecutor._prompt(request)
        planner_prompt = CodexPlanner._prompt(
            contract,
            claude_models_available=(),
            default_executor_model="gpt-5.6-luna",
            verifier_model="gpt-5.6-sol",
        )
        tools, allowed_tools, permission_mode = ClaudeExecutor._permission_args(request)
        claude_command = ClaudeExecutor._command(
            "claude",
            request,
            schema=ClaudeExecutor._worker_schema(),
            tools=tools,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
        )
        claude_system_prompt = claude_command[claude_command.index("--append-system-prompt") + 1]

        self.assertEqual(codex_prompt.count(policy), 1)
        self.assertEqual(planner_prompt.count(policy), 1)
        self.assertEqual(claude_command.count(policy), 1)
        self.assertNotIn(policy, claude_command[-1])
        for prompt in (codex_prompt, planner_prompt, claude_system_prompt):
            self.assertIn("Already-authorized work continues through implementation", prompt)
            self.assertIn("Ask only when a material decision is missing", prompt)
            self.assertIn("Use skills only when task-relevant", prompt)
            self.assertIn("Do not rerun valid unchanged evidence merely to report status", prompt)

        self.assertIn("Stay inside the declared scope", policy)
        self.assertIn("The final node must be exactly one Codex verifier", planner_prompt)
        self.assertIn("run acceptance commands", planner_prompt)

