from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Protocol

from .artifacts import ArtifactStore
from .model import NodeResult, QuotaSnapshot
from .worktrees import WorktreeManager, scope_allows


@dataclass(frozen=True)
class ExecutionRequest:
    task_id: str
    node_id: str
    attempt: int
    contract: dict
    spec: dict
    worktree: Path | None


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> NodeResult: ...


def subscription_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("ANTHROPIC_API_KEY", None)
    return environment


def codex_subscription_environment() -> dict[str, str]:
    environment = subscription_environment()
    process_home = environment.get("CODEX_WORKBENCH_PROCESS_HOME")
    if process_home:
        environment["HOME"] = process_home
    return environment


class ProcessExecutor:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
        input_text: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=environment or subscription_environment(),
            check=False,
        )
        artifacts = {
            "stdout": self.artifacts.put_text(result.stdout, "stdout.log"),
            "stderr": self.artifacts.put_text(result.stderr, "stderr.log"),
        }
        return result, artifacts


class DeterministicExecutor(ProcessExecutor):
    def execute(self, request: ExecutionRequest) -> NodeResult:
        assert request.worktree is not None
        command = list(request.spec["command"])
        result, artifacts = self._run(
            command,
            cwd=request.worktree,
            timeout=int(request.contract["timeout_seconds"]),
        )
        return NodeResult(
            status="succeeded" if result.returncode == 0 else "failed",
            summary=(result.stdout or result.stderr).strip()[-1000:] or f"exit {result.returncode}",
            artifacts=artifacts,
            exit_code=result.returncode,
            retryable=False,
        )


class CodexExecutor(ProcessExecutor):
    def __init__(self, artifacts: ArtifactStore, binary: str = "codex"):
        super().__init__(artifacts)
        self.binary = binary

    def qualification(self) -> tuple[bool, str]:
        binary = shutil.which(self.binary) if "/" not in self.binary else self.binary
        if not binary or not Path(binary).exists():
            return False, "Codex CLI is not installed"
        companion = Path(binary).resolve().with_name("codex-code-mode-host")
        if not companion.is_file() or not os.access(companion, os.X_OK):
            return False, f"Codex workspace tool host is missing or not executable: {companion}"
        try:
            result = subprocess.run(
                [binary, "login", "status"],
                text=True,
                capture_output=True,
                timeout=15,
                env=codex_subscription_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"Codex qualification failed: {error}"
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode or "Logged in using ChatGPT" not in output:
            return False, "Codex must attest ChatGPT subscription authentication"
        return True, "native-subscription"

    def execute(self, request: ExecutionRequest) -> NodeResult:
        assert request.worktree is not None
        qualified, reason = self.qualification()
        if not qualified:
            return NodeResult(status="blocked", summary=reason)
        prompt = self._prompt(request)
        verifier = bool(request.spec.get("verifier"))
        schema = self._verifier_schema() if verifier else self._worker_schema()
        with tempfile.TemporaryDirectory(prefix="codex-workbench-turn-") as directory:
            schema_path = Path(directory) / "schema.json"
            output_path = Path(directory) / "result.json"
            schema_path.write_text(json.dumps(schema))
            command = [
                self.binary,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--disable",
                "skill_search",
                "--disable",
                "plugins",
                "--disable",
                "plugin_sharing",
                "--enable",
                "code_mode_host",
                "--json",
                "--model",
                request.spec["model"],
                "--sandbox",
                "workspace-write",
                "--cd",
                str(request.worktree),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                result, artifacts = self._run(
                    command,
                    cwd=request.worktree,
                    timeout=int(request.contract["timeout_seconds"]),
                    input_text=prompt,
                    environment=codex_subscription_environment(),
                )
            except subprocess.TimeoutExpired:
                return NodeResult(status="indeterminate", summary="Codex turn timed out; terminal state is unknown")
            structured = None
            if output_path.exists():
                try:
                    structured_text = output_path.read_text()
                    structured = json.loads(structured_text)
                    structured_ref = self.artifacts.put_text(structured_text, "result.json")
                    artifacts = {**artifacts, "structured-result": structured_ref}
                    if verifier:
                        artifacts = {**artifacts, "test-log": structured_ref, "verdict": structured_ref}
                except json.JSONDecodeError:
                    structured = None
        summary = (
            structured.get("summary", "")
            if isinstance(structured, dict)
            else self._summary_from_jsonl(result.stdout) or result.stderr.strip()[-1000:]
        )
        if result.returncode == 0 and isinstance(structured, dict):
            if verifier:
                verdict = structured.get("verdict")
                status = "succeeded" if verdict == "accepted" else "blocked" if verdict == "blocked" else "failed"
            else:
                declared = structured.get("status")
                status = "succeeded" if declared == "succeeded" else "blocked" if declared == "blocked" else "failed"
        else:
            status = "failed"
        return NodeResult(
            status=status,
            summary=summary or f"Codex exited {result.returncode}",
            artifacts=artifacts,
            actual_model=request.spec["model"],
            exit_code=result.returncode,
            retryable=False,
        )

    @staticmethod
    def _prompt(request: ExecutionRequest) -> str:
        contract = request.contract
        base = (
            "You are a bounded Codex Workbench worker. Complete only this node.\n"
            f"Task: {contract['objective']}\n"
            f"Node: {request.spec['title']}\n"
            f"Instructions: {request.spec['prompt']}\n"
            f"Allowed scope: {json.dumps(contract['allowed_scope'])}\n"
            f"Forbidden scope: {json.dumps(contract['forbidden_scope'])}\n"
            f"Acceptance commands: {json.dumps(contract['acceptance_commands'])}\n"
            "Do not push, merge, release, deploy, delete unrelated files, or broaden scope. "
        )
        if request.spec.get("verifier"):
            return base + (
                "You are the independent verifier, not the implementation worker. Inspect the composed diff, "
                "run the declared acceptance commands, and return accepted only when evidence proves the contract."
            )
        return base + "Return the structured worker result with changed paths and checks actually run."

    @staticmethod
    def _worker_schema() -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "summary", "changed_paths", "checks"],
            "properties": {
                "status": {"enum": ["succeeded", "failed", "blocked"]},
                "summary": {"type": "string"},
                "changed_paths": {"type": "array", "items": {"type": "string"}},
                "checks": {"type": "array", "items": {"type": "string"}},
            },
        }

    @staticmethod
    def _verifier_schema() -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdict", "summary", "checks", "evidence"],
            "properties": {
                "verdict": {"enum": ["accepted", "needs_fix", "blocked"]},
                "summary": {"type": "string"},
                "checks": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
        }

    @staticmethod
    def _summary_from_jsonl(output: str) -> str:
        summaries: list[str] = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    summaries.append(text)
            if isinstance(event, dict) and event.get("type") in {"message", "agent_message"}:
                text = event.get("text") or event.get("message")
                if isinstance(text, str):
                    summaries.append(text)
        return summaries[-1][-2000:] if summaries else ""


class ClaudeExecutor(ProcessExecutor):
    def __init__(
        self,
        artifacts: ArtifactStore,
        quota: QuotaSnapshot | None,
        binary: str = "claude",
    ):
        super().__init__(artifacts)
        self.quota = quota
        self.binary = binary

    def qualification(self, model: str) -> tuple[bool, str]:
        binary = shutil.which(self.binary) if "/" not in self.binary else self.binary
        if not binary or not Path(binary).exists():
            return False, "Claude Code CLI is not installed"
        if self.quota is None:
            return False, "Claude quota is unknown"
        permitted, reason = self.quota.permits(model)
        if not permitted:
            return False, reason
        try:
            result = subprocess.run(
                [binary, "auth", "status", "--json"],
                text=True,
                capture_output=True,
                timeout=15,
                env=subscription_environment(),
                check=False,
            )
            status = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            return False, f"Claude qualification failed: {error}"
        if result.returncode or not status.get("loggedIn") or status.get("authMethod") in {None, "none", "api_key"}:
            return False, "Claude must attest native-subscription authentication"
        return True, "native-subscription"

    def execute(self, request: ExecutionRequest) -> NodeResult:
        assert request.worktree is not None
        qualified, reason = self.qualification(request.spec["model"])
        if not qualified:
            return NodeResult(status="blocked", summary=reason)
        prompt = CodexExecutor._prompt(request)
        command = [
            self.binary,
            "-p",
            "--model",
            request.spec["model"],
            "--output-format",
            "json",
            prompt,
        ]
        try:
            result, artifacts = self._run(
                command,
                cwd=request.worktree,
                timeout=int(request.contract["timeout_seconds"]),
            )
        except subprocess.TimeoutExpired:
            return NodeResult(status="indeterminate", summary="Claude turn timed out; terminal state is unknown")
        summary = result.stdout.strip()[-2000:] or result.stderr.strip()[-1000:]
        return NodeResult(
            status="succeeded" if result.returncode == 0 else "failed",
            summary=summary or f"Claude exited {result.returncode}",
            artifacts=artifacts,
            actual_model=request.spec["model"],
            exit_code=result.returncode,
            retryable=False,
        )


class FixtureExecutor:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def execute(self, request: ExecutionRequest) -> NodeResult:
        outcome = request.spec.get("prompt", "ok")
        status = "failed" if outcome.startswith("fail:") else "succeeded"
        summary = outcome.split(":", 1)[1] if ":" in outcome else outcome
        ref = self.artifacts.put_text(summary, "fixture.txt")
        return NodeResult(status=status, summary=summary, artifacts={"fixture": ref})


def validate_worker_scope(
    manager: WorktreeManager,
    request: ExecutionRequest,
    result: NodeResult,
) -> NodeResult:
    if request.worktree is None or result.status != "succeeded":
        return result
    changed = manager.changed_paths(request.worktree, request.contract["base_sha"])
    disallowed = sorted(
        path
        for path in changed
        if not scope_allows(path, request.contract["allowed_scope"], request.contract["forbidden_scope"])
    )
    if disallowed:
        return NodeResult(
            status="failed",
            summary=f"worker changed paths outside the task contract: {', '.join(disallowed)}",
            artifacts=result.artifacts,
            actual_model=result.actual_model,
            exit_code=result.exit_code,
        )
    return result
