from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .model import NodeSpec, TaskContract


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "nodes"],
    "properties": {
        "summary": {"type": "string"},
        "nodes": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "node_id",
                    "title",
                    "executor",
                    "model",
                    "prompt",
                    "command",
                    "depends_on",
                    "read_scopes",
                    "write_scopes",
                    "verifier",
                ],
                "properties": {
                    "node_id": {"type": "string", "pattern": "^[a-zA-Z0-9._-]+$"},
                    "title": {"type": "string"},
                    "executor": {"enum": ["codex", "claude", "deterministic"]},
                    "model": {"type": "string"},
                    "prompt": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "read_scopes": {"type": "array", "items": {"type": "string"}},
                    "write_scopes": {"type": "array", "items": {"type": "string"}},
                    "verifier": {"type": "boolean"},
                },
            },
        },
    },
}


class PlannerError(RuntimeError):
    pass


class CodexPlanner:
    def __init__(self, binary: str = "codex", model: str = "gpt-5.6-sol"):
        self.binary = binary
        self.model = model

    def compile(
        self,
        contract: TaskContract,
        *,
        claude_available: bool,
        default_executor_model: str,
        verifier_model: str,
    ) -> list[NodeSpec]:
        binary = shutil.which(self.binary) if "/" not in self.binary else self.binary
        if not binary or not Path(binary).exists():
            raise PlannerError("Codex CLI is not installed")
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("ANTHROPIC_API_KEY", None)
        process_home = environment.get("CODEX_WORKBENCH_PROCESS_HOME")
        if process_home:
            environment["HOME"] = process_home
        prompt = self._prompt(
            contract,
            claude_available=claude_available,
            default_executor_model=default_executor_model,
            verifier_model=verifier_model,
        )
        with tempfile.TemporaryDirectory(prefix="codex-workbench-plan-") as directory:
            schema_path = Path(directory) / "schema.json"
            output_path = Path(directory) / "plan.json"
            schema_path.write_text(json.dumps(PLAN_SCHEMA))
            result = subprocess.run(
                [
                    binary,
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--disable",
                    "skill_search",
                    "--disable",
                    "plugins",
                    "--disable",
                    "plugin_sharing",
                    "--disable",
                    "code_mode_host",
                    "--json",
                    "--model",
                    self.model,
                    "--sandbox",
                    "read-only",
                    "--cd",
                    contract.repository,
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ],
                input=prompt,
                text=True,
                capture_output=True,
                timeout=min(contract.timeout_seconds, 900),
                env=environment,
                check=False,
            )
            if result.returncode or not output_path.exists():
                raise PlannerError(
                    result.stderr.strip()[-2000:]
                    or result.stdout.strip()[-2000:]
                    or f"Codex planner exited {result.returncode}"
                )
            try:
                plan = json.loads(output_path.read_text())
            except json.JSONDecodeError as error:
                raise PlannerError(f"Codex planner returned invalid JSON: {error}") from error
        nodes = [
            NodeSpec.from_dict(
                {
                    **raw,
                    "task_id": contract.task_id,
                    "ordinal": index + 1,
                    "command": raw.get("command", []),
                }
            )
            for index, raw in enumerate(plan["nodes"])
        ]
        verifiers = [node for node in nodes if node.verifier]
        if len(verifiers) != 1:
            raise PlannerError("compiled plan must contain exactly one independent verifier")
        if verifiers[0].executor != "codex" or verifiers[0].model != verifier_model:
            raise PlannerError("independent verifier must use the configured Codex verifier model")
        if nodes[-1].node_id != verifiers[0].node_id:
            raise PlannerError("independent verifier must be the final plan node")
        non_verifiers = [node.node_id for node in nodes if not node.verifier]
        if set(verifiers[0].depends_on) != set(non_verifiers):
            raise PlannerError("verifier must depend on every execution node")
        return nodes

    @staticmethod
    def _prompt(
        contract: TaskContract,
        *,
        claude_available: bool,
        default_executor_model: str,
        verifier_model: str,
    ) -> str:
        return f"""Compile this request into a bounded development DAG. Do not execute tools or modify files.

Objective: {contract.objective}
Repository: {contract.repository}
Base SHA: {contract.base_sha}
Allowed scopes: {json.dumps(contract.allowed_scope)}
Forbidden scopes: {json.dumps(contract.forbidden_scope)}
Acceptance commands: {json.dumps(contract.acceptance_commands)}
External writes allowed: {contract.external_write_permission}
Destructive actions allowed: {contract.destructive_action_permission}
Claude dispatch available: {claude_available}

Rules:
- Prefer independent parallel nodes when their write scopes do not overlap.
- Use Codex model {default_executor_model} for bounded implementation work.
- Use Claude only when available is true and the node clearly benefits from it.
- Deterministic nodes must provide an argv command and must not use a shell string.
- The final node must be exactly one Codex verifier using {verifier_model}.
- That verifier must depend on every non-verifier node and independently inspect the composed diff and run acceptance commands.
- The verifier must declare read_scopes that cover every source and test path its evidence depends on, and no write_scopes.
- No node may widen the supplied scope or permission contract.
- Return only the required JSON object."""
