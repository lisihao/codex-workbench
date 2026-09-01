from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Protocol

from .artifacts import ArtifactStore
from .claude_quota import is_native_subscription_auth
from .governance import governance_directive, governance_receipt_fields
from .model import DEFAULT_QUOTA_TTL_SECONDS, NodeResult, QuotaSnapshot
from .worktrees import WorktreeManager, scope_allows


@dataclass(frozen=True)
class ExecutionRequest:
    task_id: str
    node_id: str
    attempt: int
    contract: dict
    spec: dict
    worktree: Path | None
    steering: tuple[str, ...] = ()


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
        verifier = bool(request.spec.get("verifier"))
        return NodeResult(
            status="succeeded" if result.returncode == 0 else "failed",
            summary=(result.stdout or result.stderr).strip()[-1000:] or f"exit {result.returncode}",
            artifacts=artifacts,
            exit_code=result.returncode,
            retryable=False,
            result_kind="verifier" if verifier else "worker",
            checks=("process-exit:0",) if result.returncode == 0 else (f"process-exit:{result.returncode}",),
            evidence=tuple(artifacts.values()) if verifier else (),
            verdict=("accepted" if result.returncode == 0 else "needs_fix") if verifier else None,
            **governance_receipt_fields(request.contract),
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
        verifier = bool(request.spec.get("verifier"))
        if not qualified:
            return NodeResult(
                status="blocked", summary=reason,
                result_kind="verifier" if verifier else "worker",
                verdict="blocked" if verifier else None,
                **governance_receipt_fields(request.contract),
            )
        prompt = self._prompt(request)
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
                return NodeResult(
                    status="indeterminate",
                    summary="Codex turn timed out; terminal state is unknown",
                    actual_model=request.spec["model"],
                    result_kind="verifier" if verifier else "worker",
                    **governance_receipt_fields(request.contract),
                )
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
            result_kind="verifier" if verifier else "worker",
            changed_paths=tuple(structured.get("changed_paths", ())) if isinstance(structured, dict) else (),
            checks=tuple(structured.get("checks", ())) if isinstance(structured, dict) else (),
            evidence=tuple(artifacts.values()) if verifier else (),
            verdict=structured.get("verdict") if verifier and isinstance(structured, dict) else None,
            **governance_receipt_fields(request.contract),
        )

    @staticmethod
    def _prompt(request: ExecutionRequest, *, include_governance: bool = True) -> str:
        contract = request.contract
        steering = (
            f"Runtime steering: {json.dumps(request.steering, ensure_ascii=False)}\n"
            if request.steering
            else ""
        )
        base = (
            "You are a bounded Codex Workbench worker. Complete only this node.\n"
            f"Task: {contract['objective']}\n"
            f"Node: {request.spec['title']}\n"
            f"Instructions: {request.spec['prompt']}\n"
            f"Allowed scope: {json.dumps(contract['allowed_scope'])}\n"
            f"Forbidden scope: {json.dumps(contract['forbidden_scope'])}\n"
            f"Node write scope: {json.dumps(request.spec.get('write_scopes', ()))}\n"
            f"Acceptance commands: {json.dumps(contract['acceptance_commands'])}\n"
            f"{steering}"
            "The node write scope is a hard boundary; an empty list means this node is read-only. "
            "Do not push, merge, release, deploy, delete unrelated files, or broaden scope. "
        )
        governed = governance_directive(contract) + "\n\n" if include_governance else ""
        if request.spec.get("verifier"):
            return governed + base + (
                "You are the independent verifier, not the implementation worker. Inspect the composed diff, "
                "run the declared acceptance commands, and return accepted only when evidence proves the contract."
            )
        return governed + base + "Return the structured worker result with changed paths and checks actually run."

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
                "checks": {"type": "array", "minItems": 1, "items": {"type": "string"}},
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
                "checks": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}},
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
    _READ_TOOLS = ("Read", "Glob", "Grep")
    _WRITE_TOOLS = ("Edit", "Write")

    def __init__(
        self,
        artifacts: ArtifactStore,
        quota: QuotaSnapshot | None,
        binary: str = "claude",
        quota_ttl_seconds: int = DEFAULT_QUOTA_TTL_SECONDS,
    ):
        super().__init__(artifacts)
        self.quota = quota
        self.binary = binary
        self.quota_ttl_seconds = quota_ttl_seconds

    def authentication(self) -> tuple[bool, str]:
        binary = shutil.which(self.binary) if "/" not in self.binary else self.binary
        if not binary or not Path(binary).exists():
            return False, "Claude Code CLI is not installed"
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
        if result.returncode or not is_native_subscription_auth(status):
            return False, "Claude must attest native-subscription authentication"
        return True, "native-subscription"

    def qualification(self, model: str) -> tuple[bool, str]:
        if self.quota is None:
            return False, "Claude quota is unknown"
        decision = self.quota.dispatch_decision(
            model,
            max_age_seconds=self.quota_ttl_seconds,
        )
        if decision.action != "claude":
            return False, decision.reason
        return self.authentication()

    def execute(self, request: ExecutionRequest) -> NodeResult:
        assert request.worktree is not None
        if request.spec.get("verifier"):
            return NodeResult(
                status="blocked",
                summary="Claude executor is worker-only; verifier must be a Codex Sol node",
                result_kind="worker",
                **governance_receipt_fields(request.contract),
            )
        qualified, reason = self.qualification(request.spec["model"])
        if not qualified:
            return NodeResult(
                status="blocked",
                summary=reason,
                result_kind="worker",
                **governance_receipt_fields(request.contract),
            )
        prompt = CodexExecutor._prompt(request, include_governance=False)
        schema = self._worker_schema()
        tools, allowed_tools, permission_mode = self._permission_args(request)
        command = [
            self.binary,
            "-p",
            "--model",
            request.spec["model"],
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "--no-session-persistence",
            "--append-system-prompt",
            governance_directive(request.contract),
            "--tools",
            ",".join(tools),
            "--allowed-tools",
            *allowed_tools,
            "--permission-mode",
            permission_mode,
            prompt,
        ]
        try:
            result, artifacts = self._run(
                command,
                cwd=request.worktree,
                timeout=int(request.contract["timeout_seconds"]),
                environment=subscription_environment(),
            )
        except subprocess.TimeoutExpired:
            return NodeResult(
                status="indeterminate",
                summary="Claude turn timed out; terminal state is unknown",
                result_kind="worker",
                **governance_receipt_fields(request.contract),
            )
        if result.stdout.strip():
            artifacts = {
                **artifacts,
                "structured-result": self.artifacts.put_text(result.stdout, "result.json"),
            }
        response, response_error = self._decode_response(result.stdout)
        actual_model = self._actual_model(response) if response is not None else None
        structured = response.get("structured_output") if response is not None else None
        worker_result, worker_error = self._validate_worker_result(structured)
        if response_error or worker_error or actual_model is None:
            reason = response_error or worker_error or "CLI response did not attest the actual model"
            return NodeResult(
                status="failed",
                summary=f"Claude structured result rejected: {reason}",
                artifacts=artifacts,
                actual_model=actual_model,
                exit_code=result.returncode,
                retryable=False,
                result_kind="worker",
                **governance_receipt_fields(request.contract),
            )
        assert worker_result is not None
        declared_status = worker_result["status"]
        status = declared_status if result.returncode == 0 else "failed"
        return NodeResult(
            status=status,
            summary=worker_result["summary"] or f"Claude exited {result.returncode}",
            artifacts=artifacts,
            actual_model=actual_model,
            exit_code=result.returncode,
            retryable=False,
            result_kind="worker",
            changed_paths=tuple(worker_result["changed_paths"]),
            checks=tuple(worker_result["checks"]),
            **governance_receipt_fields(request.contract),
        )

    @classmethod
    def _permission_args(
        cls,
        request: ExecutionRequest,
    ) -> tuple[tuple[str, ...], tuple[str, ...], str]:
        """Translate the bounded worker contract into Claude's native CLI policy flags."""
        write_scopes = tuple(request.spec.get("write_scopes", ()))
        acceptance_commands = tuple(request.contract.get("acceptance_commands", ()))
        tools = list(cls._READ_TOOLS)
        allowed = list(cls._READ_TOOLS)
        if write_scopes:
            tools.extend(cls._WRITE_TOOLS)
            allowed.extend(cls._WRITE_TOOLS)
        if write_scopes and acceptance_commands:
            tools.append("Bash")
            allowed.extend(f"Bash({command})" for command in acceptance_commands)
        return tuple(tools), tuple(allowed), "acceptEdits" if write_scopes else "dontAsk"

    @staticmethod
    def _worker_schema() -> dict:
        return CodexExecutor._worker_schema()

    @staticmethod
    def _decode_response(output: str) -> tuple[dict | None, str | None]:
        try:
            response = json.loads(output)
        except json.JSONDecodeError as error:
            return None, f"CLI output is not JSON: {error.msg}"
        if not isinstance(response, dict):
            return None, "CLI JSON response must be an object"
        if response.get("type") not in {None, "result"}:
            return None, "CLI JSON response has an unexpected type"
        if response.get("is_error") is True:
            return None, "CLI reported an error"
        if response.get("subtype") not in {None, "success"}:
            return None, "CLI reported a non-success result"
        return response, None

    @staticmethod
    def _actual_model(response: dict | None) -> str | None:
        if response is None:
            return None
        model_usage = response.get("modelUsage")
        if isinstance(model_usage, dict):
            model_names = [
                name.strip()
                for name in model_usage
                if isinstance(name, str) and name.strip()
            ]
            if len(model_names) == 1:
                return model_names[0]
            if model_names:
                return None
        model = response.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return None

    @staticmethod
    def _validate_worker_result(value: object) -> tuple[dict | None, str | None]:
        if not isinstance(value, dict):
            return None, "structured_output is missing or is not an object"
        expected = {"status", "summary", "changed_paths", "checks"}
        if set(value) != expected:
            return None, "structured_output must contain exactly status, summary, changed_paths, and checks"
        if value["status"] not in {"succeeded", "failed", "blocked"}:
            return None, "structured_output.status is invalid"
        if not isinstance(value["summary"], str):
            return None, "structured_output.summary must be a string"
        changed_paths = value["changed_paths"]
        if not isinstance(changed_paths, list) or not all(
            isinstance(path, str) for path in changed_paths
        ):
            return None, "structured_output.changed_paths must be an array of strings"
        checks = value["checks"]
        if not isinstance(checks, list) or not checks or not all(
            isinstance(check, str) for check in checks
        ):
            return None, "structured_output.checks must be a non-empty array of strings"
        return value, None


class FixtureExecutor:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def execute(self, request: ExecutionRequest) -> NodeResult:
        outcome = request.spec.get("prompt", "ok")
        status = "failed" if outcome.startswith("fail:") else "succeeded"
        summary = outcome.split(":", 1)[1] if ":" in outcome else outcome
        ref = self.artifacts.put_text(summary, "fixture.txt")
        return NodeResult(
            status=status,
            summary=summary,
            artifacts={"fixture": ref},
            **governance_receipt_fields(request.contract),
        )


def validate_worker_scope(
    manager: WorktreeManager,
    request: ExecutionRequest,
    result: NodeResult,
) -> NodeResult:
    if request.worktree is None or result.status != "succeeded":
        return result
    changed = tuple(sorted(manager.changed_paths(request.worktree, request.contract["base_sha"])))
    task_disallowed = tuple(
        path
        for path in changed
        if not scope_allows(path, request.contract["allowed_scope"], request.contract["forbidden_scope"])
    )
    if task_disallowed:
        return replace(
            result,
            status="failed",
            summary=f"worker changed paths outside the task contract: {', '.join(task_disallowed)}",
            changed_paths=changed,
        )
    if not request.spec.get("verifier"):
        node_write_scopes = tuple(request.spec.get("write_scopes", ()))
        node_disallowed = tuple(
            path
            for path in changed
            if not scope_allows(path, list(node_write_scopes), [])
        )
        if node_disallowed:
            boundary = (
                "worker changed paths but node declares no write scopes"
                if not node_write_scopes
                else "worker changed paths outside node write scopes"
            )
            return replace(
                result,
                status="failed",
                summary=f"{boundary}: {', '.join(node_disallowed)}",
                changed_paths=changed,
            )
    return replace(result, changed_paths=changed)
