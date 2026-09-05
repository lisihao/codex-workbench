from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Protocol

from .archify import (
    ArchifyContractError,
    default_vendor_root,
    pinned_archify_cli_identity,
    validate_receipt,
)
from .artifacts import ArtifactStore
from .claude_quota import is_native_subscription_auth
from .dependency_inputs import changed_paths_since_input_tree
from .governance import governance_directive, governance_receipt_fields
from .model import (
    DEFAULT_QUOTA_TTL_SECONDS,
    NodeResult,
    QuotaSnapshot,
    codex_model_long_context_overrides,
    codex_model_profile,
    codex_model_reasoning_effort,
)
from .planner import archify_directive, archify_internal_state
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
    # The coordinator loads every accepted, receipt-bearing worker result into
    # the final Sol verifier request.  These are artifact-backed packets, not
    # model text selected from a single role.
    archify_receipts: tuple[dict[str, Any], ...] = ()
    # A non-fixture node can begin from accepted ancestor patches while its
    # durable contract remains pinned to the original base commit. The
    # coordinator records both the tree object and its immutable receipt.
    input_tree_sha: str | None = None
    input_receipt: dict[str, Any] | None = None
    input_receipt_ref: str | None = None


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> NodeResult: ...


def _archify_context(request: ExecutionRequest) -> tuple[str | None, bool, str]:
    node_text = "\n".join(
        str(value)
        for value in (request.spec.get("title", ""), request.spec.get("prompt", ""))
        if value
    )
    # Raw user/model prompt text must never create a receipt-bearing execution
    # contract.  Only the normalized planner's durable node metadata can do so.
    state = archify_internal_state(request.spec.get("archify"))
    if state is None:
        return None, False, node_text
    role, required = state
    return role, required, node_text


def _decoded_archify_receipt(
    value: object,
    *,
    role: str | None,
    required: bool,
    request: ExecutionRequest | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate an optional/required model-returned Archify receipt.

    The model result remains the ordinary Workbench result envelope.  When a
    node claims to have created an architecture artifact, the nested receipt
    is checked with the pinned adapter and semantic proof is mandatory.  A
    renderer-only receipt is therefore never upgraded to worker success.
    """

    if not isinstance(value, dict):
        return None, "structured result must be an object"
    receipt = value.get("archify_receipt")
    if receipt is None:
        return None, "archify_receipt is required for an architecture-class artifact" if required else None
    if role is None:
        return None, "archify_receipt is not allowed for a node without an Archify role"
    if not isinstance(receipt, str):
        return None, "archify_receipt must be a JSON string"
    try:
        decoded = json.loads(receipt)
    except json.JSONDecodeError as error:
        return None, f"archify_receipt is not valid JSON: {error.msg}"
    if not isinstance(decoded, dict):
        return None, "archify_receipt JSON must decode to an object"
    if request is None or request.worktree is None:
        return None, "archify_receipt validation requires the request worktree"
    verdict = validate_receipt(
        decoded,
        role=role,
        require_semantic=True,
        worktree=request.worktree,
        read_scopes=tuple(request.spec.get("read_scopes", ())),
        write_scopes=tuple(request.spec.get("write_scopes", ())),
        require_output_write_scope=not bool(request.spec.get("verifier")),
    )
    if not verdict["ok"]:
        return None, "; ".join(str(reason) for reason in verdict["reasons"])
    return decoded, None


def _archify_receipt_error(
    value: object,
    *,
    role: str | None,
    required: bool,
    request: ExecutionRequest | None = None,
) -> str | None:
    """Return the receipt error while retaining the legacy test-facing seam."""

    _, error = _decoded_archify_receipt(
        value,
        role=role,
        required=required,
        request=request,
    )
    return error


def strict_schema_errors(schema: Mapping[str, Any], path: str = "$") -> tuple[str, ...]:
    """Return strict-output-schema violations accepted by neither provider.

    Codex and Claude Code both require every object to close its property set,
    and their strict modes require every declared property to be required.  The
    Archify receipt stays a JSON string so this check never delegates an open
    arbitrary-object schema to either model.
    """

    errors: list[str] = []
    schema_type = schema.get("type")
    object_schema = schema_type == "object" or "properties" in schema
    if object_schema:
        if schema_type != "object":
            errors.append(f"{path}: object schema must declare type object")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            errors.append(f"{path}: object properties must be an object")
        else:
            if schema.get("additionalProperties") is not False:
                errors.append(f"{path}: object additionalProperties must be false")
            required = schema.get("required")
            if not isinstance(required, list) or set(required) != set(properties):
                errors.append(f"{path}: every object property must be required")
            for name, child in properties.items():
                if isinstance(child, Mapping):
                    errors.extend(strict_schema_errors(child, f"{path}.properties.{name}"))
                else:
                    errors.append(f"{path}.properties.{name}: schema must be an object")
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            errors.append(f"{path}: array items must be an object")
        else:
            errors.extend(strict_schema_errors(items, f"{path}.items"))
    definitions = schema.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, Mapping):
            errors.append(f"{path}.$defs must be an object")
        else:
            for name, child in definitions.items():
                if isinstance(child, Mapping):
                    errors.extend(strict_schema_errors(child, f"{path}.$defs.{name}"))
                else:
                    errors.append(f"{path}.$defs.{name}: schema must be an object")
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list):
            errors.append(f"{path}.{keyword} must be an array")
            continue
        for index, child in enumerate(branches):
            if isinstance(child, Mapping):
                errors.extend(strict_schema_errors(child, f"{path}.{keyword}[{index}]"))
            else:
                errors.append(f"{path}.{keyword}[{index}]: schema must be an object")
    for keyword in ("not", "if", "then", "else"):
        child = schema.get(keyword)
        if child is None:
            continue
        if isinstance(child, Mapping):
            errors.extend(strict_schema_errors(child, f"{path}.{keyword}"))
        else:
            errors.append(f"{path}.{keyword}: schema must be an object")
    return tuple(errors)


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


def _execution_receipt_metadata(
    request: ExecutionRequest,
    *,
    provider: str,
    agent_version: str | None = None,
) -> dict[str, str | None]:
    """Collect pinned execution metadata without probing either model.

    Capability fields are copied from the normalized node first and then the
    task contract.  Older plans do not contain these optional fields; their
    result remains valid and simply carries the model/provider fields that
    can be established from the executor boundary.  ``agent_version`` is
    only accepted from already-attested request metadata (or an explicit
    caller value); this helper never logs in or starts a model turn to obtain
    it.
    """

    spec = request.spec if isinstance(request.spec, Mapping) else {}
    contract = request.contract if isinstance(request.contract, Mapping) else {}

    def first_string(*containers: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
        for container in containers:
            for key in keys:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    requested_model = first_string(spec, contract, keys=("requested_model", "model"))
    if requested_model is None:
        requested_model = first_string(
            contract,
            keys=("verifier_model",) if spec.get("verifier") else ("executor_model",),
        )
    selected_provider = first_string(
        spec,
        contract,
        keys=("provider", "model_provider", "executor_provider"),
    ) or provider
    version = agent_version or first_string(
        spec,
        contract,
        keys=("agent_version", "agent_cli_version", "cli_version"),
    )
    return {
        "requested_model": requested_model,
        "provider": selected_provider,
        "agent_name": first_string(spec, contract, keys=("agent_name",)),
        "agent_version": version,
        "capability_snapshot_id": first_string(
            spec,
            contract,
            keys=("capability_snapshot_id",),
        ),
        "model_capability_id": first_string(
            spec,
            contract,
            keys=("model_capability_id", "capability_id"),
        ),
        "agent_capability_id": first_string(
            spec,
            contract,
            keys=("agent_capability_id",),
        ),
    }


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

    def _validate_archify_delivery(
        self,
        request: ExecutionRequest,
        receipt: Mapping[str, Any],
        receipt_ref: str,
    ) -> tuple[dict[str, str], str | None]:
        """Validate the command-specific artifact boundary with pinned tools.

        ``deliver`` still receives an independent read-only ``validate`` of
        its frozen specification.  Every command that owns a graphic artifact
        additionally runs the pinned ``check-render-output.mjs`` against that
        exact file.  This prevents a valid specification plus an arbitrary HTML
        blob from being accepted as an artifact receipt.
        """

        command = receipt.get("command")
        if command in {"validate", "migrate"}:
            return self._validate_archify_command(
                request,
                receipt,
                receipt_ref,
            )
        if command not in {"deliver", "compare", "visual-check"}:
            # The model receipt decoder rejects unsupported commands before
            # this path; keep the executor fail-closed if called directly.
            return {}, f"unsupported Archify command evidence: {command!r}"

        frozen_specification: dict[str, Any] | None = (
            self._frozen_archify_binding(receipt.get("specification"))
            if command == "deliver"
            else None
        )
        frozen_artifact = self._frozen_archify_binding(receipt.get("artifact"))
        record: dict[str, Any] = {
            "schema_version": 1,
            "kind": "archify-executor-render-validation",
            "receipt_ref": receipt_ref,
            "receipt_command": command,
            "frozen_specification": frozen_specification,
            "frozen_artifact": frozen_artifact,
            "proof": {
                "mode": (
                    "independent-validate-and-render-check-and-frozen-byte-binding"
                    if command == "deliver"
                    else "render-check-and-frozen-byte-binding"
                ),
                "deliver_replayed": False,
            },
            "argv": [],
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "provenance": {},
            "cli_receipt": None,
            "artifact_checker": {
                "argv": [],
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "receipt": None,
            },
        }
        error: str | None = None
        pinned_identity: dict[str, Any] | None = None
        try:
            pinned_identity = pinned_archify_cli_identity()
        except ArchifyContractError as exception:
            record["provenance"] = {"schema_version": 1, "ok": False, "error": str(exception)}
            error = f"pinned Archify provenance rejected: {exception}"

        if error is None and request.worktree is None:
            error = "Archify executor validation requires a worktree"

        specification_path: Path | None = None
        artifact_path: Path | None = None
        if error is None:
            try:
                if command == "deliver":
                    assert frozen_specification is not None
                    specification_path = self._frozen_binding_path(
                        request.worktree,
                        frozen_specification,
                        "specification",
                    )
                artifact_path = self._frozen_binding_path(
                    request.worktree,
                    frozen_artifact,
                    "artifact",
                )
            except ValueError as exception:
                error = str(exception)
            else:
                if (
                    specification_path is not None
                    and frozen_specification is not None
                    and not self._binding_matches(specification_path, frozen_specification)
                ):
                    error = "frozen specification bytes changed before executor validation"
                elif artifact_path is not None and not self._binding_matches(artifact_path, frozen_artifact):
                    error = "frozen delivered artifact bytes changed before executor validation"

        diagram_type = receipt.get("type")
        node_binary = shutil.which("node")
        if error is None and command != "visual-check" and (not isinstance(diagram_type, str) or not diagram_type):
            error = "worker receipt has no Archify diagram type"
        if error is None and node_binary is None:
            error = "pinned Archify CLI is unavailable for executor validation"
        node_identity: dict[str, str] | None = None
        if error is None:
            assert pinned_identity is not None
            try:
                node_identity = self._node_identity(node_binary)
            except ValueError as exception:
                error = f"pinned Archify runtime provenance rejected: {exception}"
            if node_identity is None:
                record["provenance"] = {
                    "schema_version": 1,
                    "ok": False,
                    "error": error,
                }
            else:
                record["provenance"] = {
                    "schema_version": 1,
                    "ok": True,
                    **pinned_identity,
                    "node": node_identity,
                }

        timeout = max(1, min(int(request.contract["timeout_seconds"]), 60))
        if error is None and command == "deliver":
            assert specification_path is not None
            assert node_identity is not None and pinned_identity is not None
            record["argv"] = [
                node_identity["path"],
                pinned_identity["cli"]["path"],
                "validate",
                diagram_type,
                str(specification_path),
                "--quality",
                "showcase",
                "--json",
            ]
            try:
                result = subprocess.run(
                    [str(value) for value in record["argv"]],
                    cwd=request.worktree,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=subscription_environment(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exception:
                error = f"pinned Archify validation did not complete: {type(exception).__name__}: {exception}"
            else:
                record["stdout"] = result.stdout
                record["stderr"] = result.stderr
                record["exit_code"] = result.returncode
                if result.returncode != 0:
                    error = f"pinned Archify validation exited {result.returncode}"
                else:
                    try:
                        cli_receipt = json.loads(result.stdout)
                    except json.JSONDecodeError as exception:
                        error = f"pinned Archify validation emitted invalid JSON: {exception.msg}"
                    else:
                        if not isinstance(cli_receipt, dict):
                            error = "pinned Archify validation emitted a non-object receipt"
                        else:
                            record["cli_receipt"] = cli_receipt
                            verdict = validate_receipt(cli_receipt, require_semantic=False)
                            if not verdict["ok"]:
                                error = "pinned Archify validation receipt rejected: " + "; ".join(
                                    str(reason) for reason in verdict["reasons"]
                                )
                            elif cli_receipt.get("type") != diagram_type:
                                error = "pinned Archify validation type does not match the delivered receipt"
                            elif not self._same_path(cli_receipt.get("input"), specification_path):
                                error = "pinned Archify validation input does not match the frozen specification"

        if error is None:
            assert artifact_path is not None
            assert node_identity is not None and pinned_identity is not None
            checker = pinned_identity["checker"]["path"]
            checker_argv = [node_identity["path"], checker, str(artifact_path)]
            artifact_checker = record["artifact_checker"]
            artifact_checker["argv"] = checker_argv
            try:
                result = subprocess.run(
                    checker_argv,
                    cwd=request.worktree,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=subscription_environment(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exception:
                error = f"pinned Archify renderer checker did not complete: {type(exception).__name__}: {exception}"
            else:
                artifact_checker["stdout"] = result.stdout
                artifact_checker["stderr"] = result.stderr
                artifact_checker["exit_code"] = result.returncode
                if result.returncode != 0:
                    error = f"pinned Archify renderer checker exited {result.returncode}"
                else:
                    try:
                        checker_receipt = json.loads(result.stdout)
                    except json.JSONDecodeError as exception:
                        error = f"pinned Archify renderer checker emitted invalid JSON: {exception.msg}"
                    else:
                        artifact_checker["receipt"] = checker_receipt
                        if not isinstance(checker_receipt, Mapping):
                            error = "pinned Archify renderer checker emitted a non-object receipt"
                        elif checker_receipt.get("ok") is not True:
                            error = "pinned Archify renderer checker did not pass the delivered artifact"
                        elif not self._same_path(checker_receipt.get("file"), artifact_path):
                            error = "pinned Archify renderer checker file does not match the frozen artifact"
                        elif (
                            not isinstance(checker_receipt.get("checks"), list)
                            or len(checker_receipt["checks"]) != 9
                            or any(item.get("ok") is not True for item in checker_receipt["checks"] if isinstance(item, Mapping))
                            or any(not isinstance(item, Mapping) for item in checker_receipt["checks"])
                        ):
                            error = "pinned Archify renderer checker must pass all 9 artifact checks"
                        else:
                            composition = checker_receipt.get("composition")
                            summary = composition.get("summary") if isinstance(composition, Mapping) else None
                            if (
                                not isinstance(composition, Mapping)
                                or composition.get("profile") != "showcase"
                                or composition.get("status") != "pass"
                                or not isinstance(summary, Mapping)
                                or summary.get("errors") != 0
                                or summary.get("warnings") != 0
                            ):
                                error = "pinned Archify renderer checker composition must be showcase/pass with zero errors and warnings"

        stdout_ref = self.artifacts.put_text(str(record["stdout"]), "archify-validation.stdout.log")
        stderr_ref = self.artifacts.put_text(str(record["stderr"]), "archify-validation.stderr.log")
        record["stdout_ref"] = stdout_ref
        record["stderr_ref"] = stderr_ref
        checker_stdout_ref = self.artifacts.put_text(
            str(record["artifact_checker"]["stdout"]),
            "archify-render-check.stdout.log",
        )
        checker_stderr_ref = self.artifacts.put_text(
            str(record["artifact_checker"]["stderr"]),
            "archify-render-check.stderr.log",
        )
        record["artifact_checker"]["stdout_ref"] = checker_stdout_ref
        record["artifact_checker"]["stderr_ref"] = checker_stderr_ref
        evidence_ref = self.artifacts.put_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "archify-execution.json",
        )
        return {
            "archify-execution": evidence_ref,
            "archify-validation-stdout": stdout_ref,
            "archify-validation-stderr": stderr_ref,
            "archify-render-check-stdout": checker_stdout_ref,
            "archify-render-check-stderr": checker_stderr_ref,
        }, error

    @staticmethod
    def _archify_file_binding(path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        return {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }

    @staticmethod
    def _archify_input_path(worktree: Path | None, value: object, label: str) -> Path:
        if worktree is None:
            raise ValueError("Archify command validation requires a worktree")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} path is missing")
        root = worktree.resolve(strict=True)
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            raise ValueError(f"{label} path must not be a symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exception:
            raise ValueError(f"{label} file is unavailable: {exception}") from exception
        if not resolved.is_file():
            raise ValueError(f"{label} path is not a regular file")
        try:
            resolved.relative_to(root)
        except ValueError as exception:
            raise ValueError(f"{label} path is outside the authorized worktree") from exception
        return resolved

    @staticmethod
    def _archify_command_receipt_mismatch(
        claimed: Mapping[str, Any],
        actual: Mapping[str, Any],
        *,
        command: str,
        input_path: Path | None = None,
        source_path: Path | None = None,
        destination_binding: Mapping[str, Any] | None = None,
        actual_destination_path: Path | None = None,
    ) -> str | None:
        """Return a mismatch between a model receipt and pinned CLI output.

        Workbench-only identity and semantic fields are intentionally excluded;
        every upstream command field is otherwise compared to the host result.
        Paths are compared after resolution so a worker may use a relative path,
        while migration destination bytes must match the host-produced output.
        """

        workbench_fields = {"workbenchReceiptVersion", "role", "semantic"}
        for key, value in actual.items():
            if key in workbench_fields:
                continue
            if key == "input":
                if input_path is None or not ProcessExecutor._same_path(value, input_path):
                    return "pinned Archify command input does not match the frozen input"
                continue
            if key == "source":
                if source_path is None or not isinstance(value, Mapping):
                    return "pinned Archify migration source evidence is invalid"
                claimed_source = claimed.get("source")
                if not isinstance(claimed_source, Mapping):
                    return "pinned Archify migration source receipt is invalid"
                if (
                    value.get("sha256") != claimed_source.get("sha256")
                    or value.get("bytes") != claimed_source.get("bytes")
                    or not ProcessExecutor._same_path(value.get("path"), source_path)
                ):
                    return "pinned Archify migration source evidence does not match the receipt"
                continue
            if key == "destination":
                if not isinstance(value, Mapping) or not isinstance(destination_binding, Mapping):
                    return "pinned Archify migration destination evidence is invalid"
                if actual_destination_path is not None and not ProcessExecutor._same_path(
                    value.get("path"), actual_destination_path
                ):
                    return "pinned Archify migration destination evidence does not match the execution"
                if (
                    value.get("sha256") != destination_binding.get("sha256")
                    or value.get("bytes") != destination_binding.get("bytes")
                ):
                    return "pinned Archify migration output does not match the receipt destination"
                continue
            if claimed.get(key) != value:
                return f"pinned Archify {command} receipt field {key!r} does not match host execution"

        common_fields = {
            "schemaVersion",
            "workbenchReceiptVersion",
            "role",
            "ok",
            "command",
            "type",
            "semantic",
        }
        for key in claimed:
            if key in common_fields or key in {"input", "source", "destination"}:
                continue
            if key not in actual:
                return f"Archify {command} receipt contains an unexecuted field {key!r}"
        return None

    def _validate_archify_command(
        self,
        request: ExecutionRequest,
        receipt: Mapping[str, Any],
        receipt_ref: str,
    ) -> tuple[dict[str, str], str | None]:
        """Run the pinned CLI for validate/migrate and bind its output.

        These commands do not create a persistent HTML artifact, but a model
        supplied receipt is still only a claim.  ``validate`` is replayed on
        its frozen input.  ``migrate`` is replayed into a private temporary
        destination and its bytes are compared with the worker destination;
        the worker destination is never overwritten by this host check.
        """

        command = receipt.get("command")
        if command not in {"validate", "migrate"}:
            return {}, f"unsupported Archify command evidence: {command!r}"
        record: dict[str, Any] = {
            "schema_version": 1,
            "kind": "archify-executor-command-validation",
            "receipt_ref": receipt_ref,
            "receipt_command": command,
            "frozen_input": None,
            "frozen_source": None,
            "frozen_destination": None,
            "proof": {
                "mode": (
                    "pinned-validate-and-frozen-input-binding"
                    if command == "validate"
                    else "pinned-migrate-and-frozen-source-destination-binding"
                ),
                "renderer_check": "not-applicable",
            },
            "argv": [],
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "provenance": {},
            "cli_receipt": None,
        }
        error: str | None = None
        pinned_identity: dict[str, Any] | None = None
        try:
            pinned_identity = pinned_archify_cli_identity()
        except ArchifyContractError as exception:
            error = f"pinned Archify provenance rejected: {exception}"

        node_binary = shutil.which("node")
        node_identity: dict[str, str] | None = None
        if error is None and node_binary is None:
            error = "pinned Archify CLI is unavailable for command validation"
        if error is None:
            assert pinned_identity is not None
            try:
                node_identity = self._node_identity(node_binary)
            except ValueError as exception:
                error = f"pinned Archify runtime provenance rejected: {exception}"
            if node_identity is None:
                record["provenance"] = {
                    "schema_version": 1,
                    "ok": False,
                    "error": error,
                }
            else:
                record["provenance"] = {
                    "schema_version": 1,
                    "ok": True,
                    **pinned_identity,
                    "node": node_identity,
                }

        input_path: Path | None = None
        source_path: Path | None = None
        destination_path: Path | None = None
        destination_binding: Mapping[str, Any] | None = None
        if error is None:
            try:
                if command == "validate":
                    if not isinstance(receipt.get("type"), str) or not receipt.get("type"):
                        raise ValueError("validate receipt has no Archify diagram type")
                    input_path = self._archify_input_path(request.worktree, receipt.get("input"), "input")
                    record["frozen_input"] = self._archify_file_binding(input_path)
                else:
                    source_binding = self._frozen_archify_binding(receipt.get("source"))
                    destination_binding = self._frozen_archify_binding(receipt.get("destination"))
                    source_path = self._frozen_binding_path(request.worktree, source_binding, "source")
                    destination_path = self._frozen_binding_path(request.worktree, destination_binding, "destination")
                    if source_path == destination_path:
                        raise ValueError("migration source and destination must be distinct files")
                    if not self._binding_matches(source_path, source_binding):
                        raise ValueError("frozen migration source bytes changed before executor validation")
                    if not self._binding_matches(destination_path, destination_binding):
                        raise ValueError("frozen migration destination bytes changed before executor validation")
                    record["frozen_source"] = dict(source_binding)
                    record["frozen_destination"] = dict(destination_binding)
            except ValueError as exception:
                error = str(exception)

        timeout = max(1, min(int(request.contract["timeout_seconds"]), 60))
        temp_destination: Path | None = None
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        if error is None:
            assert node_identity is not None and pinned_identity is not None
            if command == "validate":
                assert input_path is not None
                record["argv"] = [
                    node_identity["path"],
                    pinned_identity["cli"]["path"],
                    "validate",
                    receipt.get("type"),
                    str(input_path),
                    "--quality",
                    "showcase",
                    "--json",
                ]
            else:
                assert source_path is not None
                temporary_directory = tempfile.TemporaryDirectory(prefix="archify-migrate-execution-")
                temp_destination = Path(temporary_directory.name) / "migrated.workflow.json"
                record["argv"] = [
                    node_identity["path"],
                    pinned_identity["cli"]["path"],
                    "migrate",
                    "workflow",
                    str(source_path),
                    str(temp_destination),
                    "--to-schema",
                    "2",
                    "--json",
                ]
            try:
                result = subprocess.run(
                    [str(value) for value in record["argv"]],
                    cwd=request.worktree,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=subscription_environment(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exception:
                error = f"pinned Archify {command} did not complete: {type(exception).__name__}: {exception}"
            else:
                record["stdout"] = result.stdout
                record["stderr"] = result.stderr
                record["exit_code"] = result.returncode
                try:
                    parsed = json.loads(result.stdout)
                except json.JSONDecodeError as exception:
                    parsed = None
                    if result.returncode == 0:
                        error = f"pinned Archify {command} emitted invalid JSON: {exception.msg}"
                if isinstance(parsed, Mapping):
                    record["cli_receipt"] = dict(parsed)
                elif result.returncode == 0:
                    error = f"pinned Archify {command} emitted a non-object receipt"
                if result.returncode != 0:
                    error = f"pinned Archify {command} exited {result.returncode}"
                elif isinstance(parsed, Mapping):
                    verdict = validate_receipt(parsed, require_semantic=False)
                    if not verdict["ok"]:
                        error = "pinned Archify command receipt rejected: " + "; ".join(
                            str(reason) for reason in verdict["reasons"]
                        )
                    elif command == "validate":
                        mismatch = self._archify_command_receipt_mismatch(
                            receipt,
                            parsed,
                            command=command,
                            input_path=input_path,
                        )
                        if mismatch:
                            error = mismatch
                    else:
                        assert source_path is not None and temp_destination is not None
                        assert destination_binding is not None and destination_path is not None
                        if not temp_destination.is_file():
                            error = "pinned Archify migration did not produce a destination file"
                        else:
                            output_binding = self._archify_file_binding(temp_destination)
                            mismatch = self._archify_command_receipt_mismatch(
                                receipt,
                                parsed,
                                command=command,
                                source_path=source_path,
                                destination_binding=destination_binding,
                                actual_destination_path=temp_destination.resolve(),
                            )
                            if mismatch:
                                error = mismatch
                            elif output_binding["sha256"] != destination_binding.get("sha256") or output_binding["bytes"] != destination_binding.get("bytes"):
                                error = "pinned Archify migration output bytes do not match the worker destination"
                            elif temp_destination.read_bytes() != destination_path.read_bytes():
                                error = "pinned Archify migration output differs from the worker destination"

        stdout_ref = self.artifacts.put_text(str(record["stdout"]), "archify-command.stdout.log")
        stderr_ref = self.artifacts.put_text(str(record["stderr"]), "archify-command.stderr.log")
        record["stdout_ref"] = stdout_ref
        record["stderr_ref"] = stderr_ref
        evidence_ref = self.artifacts.put_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "archify-execution.json",
        )
        if temporary_directory is not None:
            temporary_directory.cleanup()
        return {
            "archify-execution": evidence_ref,
            "archify-command-stdout": stdout_ref,
            "archify-command-stderr": stderr_ref,
        }, error

    @staticmethod
    def _frozen_archify_binding(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        return {key: value.get(key) for key in ("path", "sha256", "bytes")}

    @staticmethod
    def _frozen_binding_path(
        worktree: Path | None,
        binding: Mapping[str, Any],
        label: str,
    ) -> Path:
        if worktree is None:
            raise ValueError("Archify executor validation requires a worktree")
        raw_path = binding.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"frozen {label} binding is missing a path")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = worktree / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exception:
            raise ValueError(f"frozen {label} file is unavailable: {exception}") from exception
        if not resolved.is_file():
            raise ValueError(f"frozen {label} path is not a regular file")
        return resolved

    @staticmethod
    def _binding_matches(path: Path, binding: Mapping[str, Any]) -> bool:
        data = path.read_bytes()
        return binding.get("bytes") == len(data) and binding.get("sha256") == hashlib.sha256(data).hexdigest()

    @staticmethod
    def _same_path(value: object, expected: Path) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            return Path(value).resolve(strict=True) == expected
        except OSError:
            return False

    @staticmethod
    def _node_identity(node_binary: str) -> dict[str, str]:
        try:
            node = Path(node_binary).resolve(strict=True)
        except OSError as exception:
            raise ValueError(f"node runtime path is unavailable: {exception}") from exception
        result = subprocess.run(
            [str(node), "--version"],
            text=True,
            capture_output=True,
            timeout=15,
            env=subscription_environment(),
            check=False,
        )
        version = result.stdout.strip()
        if result.returncode != 0 or not version or "\n" in version:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise ValueError(f"node runtime version is unavailable: {detail}")
        return {"path": str(node), "version": version}

    def _pinned_archify_provenance_error(
        self,
        provenance: object,
        argv: object,
        *,
        tool: str = "cli",
    ) -> str | None:
        """Bind evidence to the exact local pinned core and Node runtime."""

        expected_fields = {"schema_version", "ok", "source", "cli", "checker", "node"}
        if not isinstance(provenance, Mapping):
            return "provenance is not an object"
        unknown = sorted(set(provenance) - expected_fields)
        missing = sorted(expected_fields - set(provenance))
        if unknown or missing:
            details = []
            if unknown:
                details.append(f"unknown fields: {', '.join(unknown)}")
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            return "; ".join(details)
        try:
            pinned_identity = pinned_archify_cli_identity()
        except ArchifyContractError as exception:
            return f"pinned Archify identity is unavailable: {exception}"
        node_binary = shutil.which("node")
        if node_binary is None:
            return "pinned Node runtime is unavailable"
        try:
            node_identity = self._node_identity(node_binary)
        except ValueError as exception:
            return str(exception)
        expected = {
            "schema_version": 1,
            "ok": True,
            **pinned_identity,
            "node": node_identity,
        }
        if dict(provenance) != expected:
            return "provenance does not match the actual pinned CLI, source, or runtime identity"
        if tool not in {"cli", "checker"}:
            return f"unsupported pinned Archify evidence tool: {tool}"
        if (
            not isinstance(argv, list)
            or len(argv) < 2
            or argv[0] != node_identity["path"]
            or argv[1] != pinned_identity[tool]["path"]
        ):
            return "Archify evidence argv is not bound to the actual pinned runtime and tool"
        return None

    def _verify_archify_command_execution(
        self,
        execution: Mapping[str, Any],
        receipt: Mapping[str, Any],
        *,
        command: str,
        worktree: Path,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Verify host-generated validate/migrate evidence before Sol review."""

        refs: list[str] = []
        expected_mode = (
            "pinned-validate-and-frozen-input-binding"
            if command == "validate"
            else "pinned-migrate-and-frozen-source-destination-binding"
        )
        proof = execution.get("proof")
        if (
            not isinstance(proof, Mapping)
            or set(proof) != {"mode", "renderer_check"}
            or proof.get("mode") != expected_mode
            or proof.get("renderer_check") != "not-applicable"
        ):
            return f"Archify verifier command evidence proof is invalid for {command}", tuple(refs)

        input_path: Path | None = None
        source_path: Path | None = None
        destination_path: Path | None = None
        destination_binding: Mapping[str, Any] | None = None
        try:
            if command == "validate":
                input_path = self._archify_input_path(worktree, receipt.get("input"), "input")
                expected_input = self._archify_file_binding(input_path)
                if execution.get("frozen_input") != expected_input:
                    return "Archify verifier command evidence is not bound to the frozen input", tuple(refs)
                if execution.get("frozen_source") is not None or execution.get("frozen_destination") is not None:
                    return "validate command evidence contains migration bindings", tuple(refs)
            else:
                source_binding = self._frozen_archify_binding(receipt.get("source"))
                destination_binding = self._frozen_archify_binding(receipt.get("destination"))
                source_path = self._frozen_binding_path(worktree, source_binding, "source")
                destination_path = self._frozen_binding_path(worktree, destination_binding, "destination")
                if execution.get("frozen_input") is not None:
                    return "migrate command evidence contains a validate input binding", tuple(refs)
                if execution.get("frozen_source") != source_binding or execution.get("frozen_destination") != destination_binding:
                    return "Archify verifier command evidence is not bound to the frozen migration files", tuple(refs)
                if (
                    not self._binding_matches(source_path, source_binding)
                    or not self._binding_matches(destination_path, destination_binding)
                ):
                    return "Archify verifier migration bytes no longer match the receipt", tuple(refs)
        except ValueError as exception:
            return f"Archify verifier command evidence has invalid frozen bindings: {exception}", tuple(refs)

        argv = execution.get("argv")
        cli_receipt = execution.get("cli_receipt")
        if execution.get("exit_code") != 0 or not isinstance(argv, list) or not isinstance(cli_receipt, Mapping):
            return f"Archify verifier command evidence has no successful pinned {command} execution", tuple(refs)
        if command == "validate":
            if (
                input_path is None
                or len(argv) != 8
                or argv[2:]
                != [
                    "validate",
                    receipt.get("type"),
                    str(input_path),
                    "--quality",
                    "showcase",
                    "--json",
                ]
            ):
                return "Archify verifier validate argv does not match the requested command", tuple(refs)
        else:
            if (
                source_path is None
                or destination_path is None
                or len(argv) != 9
                or argv[2:4] != ["migrate", "workflow"]
                or argv[4] != str(source_path)
                or not isinstance(argv[5], str)
                or not Path(argv[5]).is_absolute()
                or Path(argv[5]).resolve() == source_path
                or argv[6:] != ["--to-schema", "2", "--json"]
            ):
                return "Archify verifier migrate argv does not match the requested command", tuple(refs)
            actual_destination_path = Path(argv[5]).resolve()

        try:
            stdout = self.artifacts.verify(str(execution["stdout_ref"])).read_text(encoding="utf-8")
            stderr = self.artifacts.verify(str(execution["stderr_ref"])).read_text(encoding="utf-8")
            refs.extend((str(execution["stdout_ref"]), str(execution["stderr_ref"])))
        except (OSError, UnicodeError, ValueError) as exception:
            return f"Archify verifier cannot read {command} execution logs: {exception}", tuple(dict.fromkeys(refs))
        if execution.get("stdout") != stdout or execution.get("stderr") != stderr:
            return f"Archify verifier {command} logs do not match execution evidence", tuple(dict.fromkeys(refs))
        provenance_error = self._pinned_archify_provenance_error(execution.get("provenance"), argv, tool="cli")
        if provenance_error:
            return f"Archify verifier {command} provenance rejected: {provenance_error}", tuple(dict.fromkeys(refs))
        cli_verdict = validate_receipt(cli_receipt, require_semantic=False)
        if not cli_verdict["ok"]:
            return f"Archify verifier pinned {command} receipt is invalid", tuple(dict.fromkeys(refs))
        mismatch = self._archify_command_receipt_mismatch(
            receipt,
            cli_receipt,
            command=command,
            input_path=input_path,
            source_path=source_path,
            destination_binding=destination_binding,
            actual_destination_path=(actual_destination_path if command == "migrate" else None),
        )
        if mismatch:
            return f"Archify verifier {mismatch}", tuple(dict.fromkeys(refs))
        return None, tuple(dict.fromkeys(refs))

    def _verifier_archify_packets_error(
        self,
        request: ExecutionRequest,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Recheck every artifact-backed worker receipt before Sol can accept.

        The provider prompt gets the complete packets for review, while this
        host-side gate makes a missing or substituted receipt fail the verifier
        result rather than silently trusting the first worker role.
        """

        refs: list[str] = []
        seen_nodes: set[str] = set()
        for index, packet in enumerate(request.archify_receipts):
            if not isinstance(packet, Mapping):
                return f"Archify verifier packet {index} is not an object", tuple(refs)
            node_id = packet.get("node_id")
            role = packet.get("role")
            receipt_ref = packet.get("receipt_ref")
            execution_ref = packet.get("execution_ref")
            receipt = packet.get("receipt")
            if not isinstance(node_id, str) or not node_id:
                return f"Archify verifier packet {index} has no worker node_id", tuple(refs)
            if node_id in seen_nodes:
                return f"Archify verifier received duplicate receipt for worker {node_id}", tuple(refs)
            seen_nodes.add(node_id)
            if not isinstance(role, str) or not role:
                return f"Archify verifier packet {node_id} has no role", tuple(refs)
            if not isinstance(receipt_ref, str) or not isinstance(execution_ref, str):
                return f"Archify verifier packet {node_id} lacks artifact references", tuple(refs)
            if not isinstance(receipt, Mapping):
                return f"Archify verifier packet {node_id} has no receipt object", tuple(dict.fromkeys(refs))
            worktree = packet.get("worktree")
            read_scopes = packet.get("read_scopes")
            write_scopes = packet.get("write_scopes")
            if (
                not isinstance(worktree, str)
                or not isinstance(read_scopes, (list, tuple))
                or not isinstance(write_scopes, (list, tuple))
            ):
                return f"Archify verifier packet {node_id} lacks validated worker scope metadata", tuple(dict.fromkeys(refs))
            try:
                stored_receipt = json.loads(self.artifacts.verify(receipt_ref).read_text(encoding="utf-8"))
                execution = json.loads(self.artifacts.verify(execution_ref).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exception:
                return f"Archify verifier cannot read {node_id} receipt evidence: {exception}", tuple(dict.fromkeys(refs))
            refs.extend((receipt_ref, execution_ref))
            if stored_receipt != receipt:
                return f"Archify verifier packet {node_id} receipt does not match its artifact", tuple(dict.fromkeys(refs))
            if not isinstance(execution, Mapping):
                return f"Archify verifier packet {node_id} execution evidence is not an object", tuple(dict.fromkeys(refs))
            command = receipt.get("command")
            command_only = command in {"validate", "migrate"}
            execution_fields = (
                {
                    "schema_version",
                    "kind",
                    "receipt_ref",
                    "receipt_command",
                    "frozen_input",
                    "frozen_source",
                    "frozen_destination",
                    "proof",
                    "argv",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "provenance",
                    "stdout_ref",
                    "stderr_ref",
                    "cli_receipt",
                }
                if command_only
                else {
                    "schema_version",
                    "kind",
                    "receipt_ref",
                    "receipt_command",
                    "frozen_specification",
                    "frozen_artifact",
                    "proof",
                    "argv",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "provenance",
                    "stdout_ref",
                    "stderr_ref",
                    "cli_receipt",
                    "artifact_checker",
                }
            )
            if set(execution) != execution_fields:
                return f"Archify verifier packet {node_id} execution evidence has unknown or missing fields", tuple(dict.fromkeys(refs))
            if (
                execution.get("schema_version") != 1
                or execution.get("kind")
                != (
                    "archify-executor-command-validation"
                    if command_only
                    else "archify-executor-render-validation"
                )
                or command not in {"deliver", "compare", "visual-check", "validate", "migrate"}
                or execution.get("receipt_command") != command
                or not isinstance(execution.get("stdout"), str)
                or not isinstance(execution.get("stderr"), str)
            ):
                return f"Archify verifier packet {node_id} execution evidence identity is invalid", tuple(dict.fromkeys(refs))
            try:
                verdict = validate_receipt(
                    receipt,
                    role=role,
                    require_semantic=True,
                    worktree=Path(worktree),
                    read_scopes=tuple(read_scopes),
                    write_scopes=tuple(write_scopes),
                    require_output_write_scope=True,
                )
            except ArchifyContractError as exception:
                return f"Archify verifier packet {node_id} has an invalid role contract: {exception}", tuple(dict.fromkeys(refs))
            if not verdict["ok"]:
                return (
                    f"Archify verifier rejected {node_id} receipt: "
                    + "; ".join(str(reason) for reason in verdict["reasons"]),
                    tuple(dict.fromkeys(refs)),
                )
            if receipt.get("role") != role:
                return f"Archify verifier packet {node_id} receipt role does not match its packet", tuple(dict.fromkeys(refs))
            if execution.get("receipt_ref") != receipt_ref:
                return f"Archify verifier packet {node_id} execution evidence is not bound to its receipt", tuple(dict.fromkeys(refs))
            if command_only:
                command_error, command_refs = self._verify_archify_command_execution(
                    execution,
                    receipt,
                    command=command,
                    worktree=Path(worktree),
                )
                refs.extend(command_refs)
                if command_error:
                    return (
                        f"Archify verifier packet {node_id} command evidence rejected: {command_error}",
                        tuple(dict.fromkeys(refs)),
                    )
                continue
            expected_specification = (
                self._frozen_archify_binding(receipt.get("specification"))
                if command == "deliver"
                else None
            )
            if execution.get("frozen_specification") != expected_specification:
                return f"Archify verifier packet {node_id} execution evidence is not bound to its frozen specification", tuple(dict.fromkeys(refs))
            if execution.get("frozen_artifact") != self._frozen_archify_binding(receipt.get("artifact")):
                return f"Archify verifier packet {node_id} execution evidence is not bound to its frozen artifact", tuple(dict.fromkeys(refs))
            frozen_specification_path: Path | None = None
            try:
                if command == "deliver":
                    frozen_specification_path = self._frozen_binding_path(
                        Path(worktree),
                        self._frozen_archify_binding(receipt.get("specification")),
                        "specification",
                    )
                frozen_artifact_path = self._frozen_binding_path(
                    Path(worktree),
                    self._frozen_archify_binding(receipt.get("artifact")),
                    "artifact",
                )
            except ValueError as exception:
                return f"Archify verifier packet {node_id} has invalid frozen bindings: {exception}", tuple(dict.fromkeys(refs))
            if (
                (
                    frozen_specification_path is not None
                    and not self._binding_matches(
                        frozen_specification_path,
                        self._frozen_archify_binding(receipt.get("specification")),
                    )
                )
                or not self._binding_matches(
                    frozen_artifact_path,
                    self._frozen_archify_binding(receipt.get("artifact")),
                )
            ):
                return f"Archify verifier packet {node_id} frozen bytes no longer match the receipt", tuple(dict.fromkeys(refs))
            provenance = execution.get("provenance")
            proof = execution.get("proof")
            expected_mode = (
                "independent-validate-and-render-check-and-frozen-byte-binding"
                if command == "deliver"
                else "render-check-and-frozen-byte-binding"
            )
            if (
                not isinstance(proof, Mapping)
                or set(proof) != {"mode", "deliver_replayed"}
                or proof.get("mode") != expected_mode
                or proof.get("deliver_replayed") is not False
            ):
                return f"Archify verifier packet {node_id} did not preserve the no-replay delivery boundary", tuple(dict.fromkeys(refs))
            argv = execution.get("argv")
            cli_receipt = execution.get("cli_receipt")
            if command == "deliver":
                if (
                    execution.get("exit_code") != 0
                    or not isinstance(argv, list)
                    or frozen_specification_path is None
                    or argv[2:]
                    != [
                        "validate",
                        receipt.get("type"),
                        str(frozen_specification_path),
                        "--quality",
                        "showcase",
                        "--json",
                    ]
                    or not isinstance(cli_receipt, Mapping)
                ):
                    return f"Archify verifier packet {node_id} has no successful showcase specification validation", tuple(dict.fromkeys(refs))
            elif argv != [] or execution.get("exit_code") is not None or cli_receipt is not None:
                return f"Archify verifier packet {node_id} has unexpected specification-validation fields for {command}", tuple(dict.fromkeys(refs))
            try:
                stdout = self.artifacts.verify(str(execution["stdout_ref"])).read_text(encoding="utf-8")
                stderr = self.artifacts.verify(str(execution["stderr_ref"])).read_text(encoding="utf-8")
                refs.extend((str(execution["stdout_ref"]), str(execution["stderr_ref"])))
            except (OSError, UnicodeError, ValueError) as exception:
                return f"Archify verifier cannot read {node_id} validation logs: {exception}", tuple(dict.fromkeys(refs))
            if stdout != execution["stdout"] or stderr != execution["stderr"]:
                return f"Archify verifier packet {node_id} validation logs do not match execution evidence", tuple(dict.fromkeys(refs))
            if command == "deliver":
                provenance_error = self._pinned_archify_provenance_error(provenance, argv, tool="cli")
                if provenance_error:
                    return (
                        f"Archify verifier packet {node_id} provenance rejected: {provenance_error}",
                        tuple(dict.fromkeys(refs)),
                    )
                cli_verdict = validate_receipt(cli_receipt, require_semantic=False)
                if (
                    not cli_verdict["ok"]
                    or cli_receipt.get("type") != receipt.get("type")
                    or not self._same_path(cli_receipt.get("input"), frozen_specification_path)
                ):
                    return f"Archify verifier packet {node_id} CLI receipt is invalid", tuple(dict.fromkeys(refs))

            checker = execution.get("artifact_checker")
            checker_fields = {"argv", "stdout", "stderr", "exit_code", "receipt", "stdout_ref", "stderr_ref"}
            if not isinstance(checker, Mapping) or set(checker) != checker_fields:
                return f"Archify verifier packet {node_id} renderer-check evidence has unknown or missing fields", tuple(dict.fromkeys(refs))
            checker_argv = checker.get("argv")
            if (
                checker.get("exit_code") != 0
                or not isinstance(checker_argv, list)
                or checker_argv[2:] != [str(frozen_artifact_path)]
                or not isinstance(checker.get("receipt"), Mapping)
                or not isinstance(checker.get("stdout"), str)
                or not isinstance(checker.get("stderr"), str)
            ):
                return f"Archify verifier packet {node_id} has no successful pinned renderer check", tuple(dict.fromkeys(refs))
            try:
                checker_stdout = self.artifacts.verify(str(checker["stdout_ref"])).read_text(encoding="utf-8")
                checker_stderr = self.artifacts.verify(str(checker["stderr_ref"])).read_text(encoding="utf-8")
                refs.extend((str(checker["stdout_ref"]), str(checker["stderr_ref"])))
            except (OSError, UnicodeError, ValueError) as exception:
                return f"Archify verifier cannot read {node_id} renderer-check logs: {exception}", tuple(dict.fromkeys(refs))
            if checker_stdout != checker["stdout"] or checker_stderr != checker["stderr"]:
                return f"Archify verifier packet {node_id} renderer-check logs do not match execution evidence", tuple(dict.fromkeys(refs))
            provenance_error = self._pinned_archify_provenance_error(provenance, checker_argv, tool="checker")
            if provenance_error:
                return (
                    f"Archify verifier packet {node_id} renderer-check provenance rejected: {provenance_error}",
                    tuple(dict.fromkeys(refs)),
                )
            checker_receipt = checker["receipt"]
            checks = checker_receipt.get("checks")
            composition = checker_receipt.get("composition")
            summary = composition.get("summary") if isinstance(composition, Mapping) else None
            if (
                checker_receipt.get("ok") is not True
                or not self._same_path(checker_receipt.get("file"), frozen_artifact_path)
                or not isinstance(checks, list)
                or len(checks) != 9
                or any(not isinstance(item, Mapping) or item.get("ok") is not True for item in checks)
                or not isinstance(composition, Mapping)
                or composition.get("profile") != "showcase"
                or composition.get("status") != "pass"
                or not isinstance(summary, Mapping)
                or summary.get("errors") != 0
                or summary.get("warnings") != 0
            ):
                return f"Archify verifier packet {node_id} renderer-check receipt is invalid", tuple(dict.fromkeys(refs))
        return None, tuple(dict.fromkeys(refs))


def validate_archify_verifier_packets(
    request: ExecutionRequest,
    artifacts: ArtifactStore,
) -> tuple[str | None, tuple[str, ...]]:
    """Run the same host receipt gate for a verifier execution or cache reuse."""

    return ProcessExecutor(artifacts)._verifier_archify_packets_error(request)


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
        metadata = _execution_receipt_metadata(request, provider="codex")
        qualified, reason = self.qualification()
        verifier = bool(request.spec.get("verifier"))
        if not qualified:
            return NodeResult(
                status="blocked", summary=reason,
                result_kind="verifier" if verifier else "worker",
                verdict="blocked" if verifier else None,
                **metadata,
                **governance_receipt_fields(request.contract),
            )
        prompt = self._prompt(request)
        archify_role, archify_required, _ = _archify_context(request)
        receipt_role = archify_role if not verifier else None
        receipt_required = archify_required if not verifier else False
        packet_error, packet_refs = (
            self._verifier_archify_packets_error(request) if verifier else (None, ())
        )
        schema = (
            self._verifier_schema(archify_required=False)
            if verifier
            else self._worker_schema(archify_required=archify_required)
        )
        with tempfile.TemporaryDirectory(prefix="codex-workbench-turn-") as directory:
            schema_path = Path(directory) / "schema.json"
            output_path = Path(directory) / "result.json"
            schema_path.write_text(json.dumps(schema))
            command = self._command(self.binary, request, schema_path, output_path)
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
                    **metadata,
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
        decoded_receipt: dict[str, Any] | None = None
        archify_error: str | None = None
        if isinstance(structured, dict):
            decoded_receipt, archify_error = _decoded_archify_receipt(
                structured,
                role=receipt_role,
                required=receipt_required,
                request=request,
            )
        if result.returncode == 0 and isinstance(structured, dict):
            if archify_error:
                status = "failed"
                summary = f"Archify receipt rejected: {archify_error}"
            elif packet_error:
                status = "failed"
                summary = f"Archify verifier receipts rejected: {packet_error}"
            elif verifier:
                verdict = structured.get("verdict")
                status = "succeeded" if verdict == "accepted" else "blocked" if verdict == "blocked" else "failed"
            else:
                declared = structured.get("status")
                status = "succeeded" if declared == "succeeded" else "blocked" if declared == "blocked" else "failed"
        else:
            status = "failed"
        if not verifier and status == "succeeded" and decoded_receipt is not None:
            receipt_ref = self.artifacts.put_text(
                json.dumps(decoded_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "archify-receipt.json",
            )
            execution_artifacts, execution_error = self._validate_archify_delivery(
                request,
                decoded_receipt,
                receipt_ref,
            )
            artifacts = {
                **artifacts,
                "archify-receipt": receipt_ref,
                **execution_artifacts,
            }
            if execution_error:
                status = "failed"
                summary = f"Archify executor validation rejected: {execution_error}"
        verifier_evidence = tuple(dict.fromkeys((*artifacts.values(), *packet_refs))) if verifier else ()
        verifier_verdict = None
        if verifier and isinstance(structured, dict):
            verifier_verdict = (
                "accepted" if status == "succeeded" else "blocked" if status == "blocked" else "needs_fix"
            )
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
            evidence=verifier_evidence,
            verdict=verifier_verdict,
            **metadata,
            **governance_receipt_fields(request.contract),
        )

    @staticmethod
    def _prompt(request: ExecutionRequest, *, include_governance: bool = True) -> str:
        contract = request.contract
        archify_role, archify_required, node_text = _archify_context(request)
        verifier = bool(request.spec.get("verifier"))
        archify_block = (
            archify_directive(
                contract,
                text=node_text,
                role=archify_role,
                artifact_required=archify_required,
                actor="Codex worker",
            )
            if archify_role is not None and not verifier
            else ""
        )
        if archify_block and archify_block in str(request.spec.get("prompt", "")):
            archify_block = ""
        steering = (
            f"Runtime steering: {json.dumps(request.steering, ensure_ascii=False)}\n"
            if request.steering
            else ""
        )
        selected_model = request.spec.get("model", "")
        selected_profile = codex_model_profile(selected_model) or request.spec.get("model_profile")
        selected_effort = codex_model_reasoning_effort(selected_model) or request.spec.get("model_reasoning_effort")
        base = (
            "You are a bounded Codex Workbench worker. Complete only this node.\n"
            f"Task: {contract['objective']}\n"
            f"Node: {request.spec['title']}\n"
            f"Execution profile: {selected_profile or 'N/A'}\n"
            f"Model reasoning effort: {selected_effort or 'N/A'}\n"
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
        archified = archify_block + "\n\n" if archify_block else ""
        if verifier:
            receipt_packets = (
                "Validated worker Archify receipt packets follow. Inspect and account for every listed worker receipt; "
                "each is independently artifact-backed and must remain visible in your evidence:\n"
                + json.dumps(request.archify_receipts, ensure_ascii=False, sort_keys=True)
                + "\n"
                if request.archify_receipts
                else ""
            )
            return governed + archified + base + (
                "You are the independent verifier, not the implementation worker. Inspect the composed diff, "
                "run the declared acceptance commands, inspect every applicable Archify receipt and external semantic evidence, "
                + receipt_packets
                + "and return accepted only when evidence proves the contract."
            )
        receipt_suffix = ", and any required Archify receipt" if archify_block else ""
        return governed + archified + base + f"Return the structured worker result with changed paths, checks actually run{receipt_suffix}."

    @staticmethod
    def _command(
        binary: str,
        request: ExecutionRequest,
        schema_path: Path,
        output_path: Path,
    ) -> list[str]:
        model = str(request.spec["model"])
        effort = codex_model_reasoning_effort(model) or request.spec.get("model_reasoning_effort")
        command = [
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
            "--enable",
            "code_mode_host",
            "--json",
            "--model",
            model,
        ]
        # Workbench workers run with --ignore-user-config, so relying on a
        # user-level profile would make Luna's requested max effort unverifiable.
        # Emit the supported Codex config override explicitly in the argv and
        # retain the profile/effort metadata on the durable NodeSpec.
        if effort is not None:
            command.extend(("--config", f"model_reasoning_effort={effort}"))
        for override in codex_model_long_context_overrides(model):
            command.extend(("--config", override))
        command.extend((
            "--sandbox",
            "workspace-write",
            "--cd",
            str(request.worktree),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ))
        return command

    @staticmethod
    def _worker_schema(*, archify_required: bool = False) -> dict:
        required = ["status", "summary", "changed_paths", "checks"]
        properties: dict[str, Any] = {
            "status": {"enum": ["succeeded", "failed", "blocked"]},
            "summary": {"type": "string"},
            "changed_paths": {"type": "array", "items": {"type": "string"}},
            "checks": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        }
        if archify_required:
            required.append("archify_receipt")
            properties["archify_receipt"] = {"type": "string", "minLength": 2}
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }

    @staticmethod
    def _verifier_schema(*, archify_required: bool = False) -> dict:
        required = ["verdict", "summary", "checks", "evidence"]
        properties: dict[str, Any] = {
            "verdict": {"enum": ["accepted", "needs_fix", "blocked"]},
            "summary": {"type": "string"},
            "checks": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        }
        if archify_required:
            required.append("archify_receipt")
            properties["archify_receipt"] = {"type": "string", "minLength": 2}
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
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
        metadata = _execution_receipt_metadata(request, provider="claude")
        if request.spec.get("verifier"):
            return NodeResult(
                status="blocked",
                summary="Claude executor is worker-only; verifier must be a Codex Sol node",
                result_kind="worker",
                **metadata,
                **governance_receipt_fields(request.contract),
            )
        archify_role, archify_required, _ = _archify_context(request)
        qualified, reason = self.qualification(request.spec["model"])
        if not qualified:
            return NodeResult(
                status="blocked",
                summary=reason,
                result_kind="worker",
                **metadata,
                **governance_receipt_fields(request.contract),
            )
        schema = self._worker_schema(archify_required=archify_required)
        tools, allowed_tools, permission_mode = self._permission_args(request)
        command = self._command(
            self.binary,
            request,
            schema=schema,
            tools=tools,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
        )
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
                **metadata,
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
        worker_result, worker_error = self._validate_worker_result(
            structured,
            archify_role=archify_role,
            archify_required=archify_required,
            request=request,
        )
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
                **metadata,
                **governance_receipt_fields(request.contract),
            )
        assert worker_result is not None
        declared_status = worker_result["status"]
        status = declared_status if result.returncode == 0 else "failed"
        summary = worker_result["summary"] or f"Claude exited {result.returncode}"
        decoded_receipt, _ = _decoded_archify_receipt(
            worker_result,
            role=archify_role,
            required=archify_required,
            request=request,
        )
        if status == "succeeded" and decoded_receipt is not None:
            receipt_ref = self.artifacts.put_text(
                json.dumps(decoded_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "archify-receipt.json",
            )
            execution_artifacts, execution_error = self._validate_archify_delivery(
                request,
                decoded_receipt,
                receipt_ref,
            )
            artifacts = {
                **artifacts,
                "archify-receipt": receipt_ref,
                **execution_artifacts,
            }
            if execution_error:
                status = "failed"
                summary = f"Archify executor validation rejected: {execution_error}"
        return NodeResult(
            status=status,
            summary=summary,
            artifacts=artifacts,
            actual_model=actual_model,
            exit_code=result.returncode,
            retryable=False,
            result_kind="worker",
            changed_paths=tuple(worker_result["changed_paths"]),
            checks=tuple(worker_result["checks"]),
            **metadata,
            **governance_receipt_fields(request.contract),
        )

    @staticmethod
    def _command(
        binary: str,
        request: ExecutionRequest,
        *,
        schema: dict,
        tools: tuple[str, ...],
        allowed_tools: tuple[str, ...],
        permission_mode: str,
    ) -> list[str]:
        prompt = CodexExecutor._prompt(request, include_governance=False)
        return [
            binary,
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
        _, archify_required, _ = _archify_context(request)
        if archify_required:
            if "Bash" not in tools:
                tools.append("Bash")
            cli = default_vendor_root().resolve() / "bin" / "archify.mjs"
            # Claude's Bash allowlist supports a command-prefix wildcard with
            # the `:*` spelling.  Keep the prefix pinned to this exact CLI and
            # expose read-only commands to read-only nodes; delivery/visual
            # commands are available only when the node already owns writes.
            archify_commands = ["guide", "validate", "compare"]
            if write_scopes:
                archify_commands.extend(("deliver", "visual-check", "migrate"))
            allowed.extend(
                f"Bash({shlex.join(('node', str(cli), command))}:*)"
                for command in archify_commands
            )
        return tuple(tools), tuple(allowed), "acceptEdits" if write_scopes else "dontAsk"

    @staticmethod
    def _worker_schema(*, archify_required: bool = False) -> dict:
        return CodexExecutor._worker_schema(archify_required=archify_required)

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
    def _validate_worker_result(
        value: object,
        *,
        archify_role: str | None = None,
        archify_required: bool = False,
        request: ExecutionRequest | None = None,
    ) -> tuple[dict | None, str | None]:
        if not isinstance(value, dict):
            return None, "structured_output is missing or is not an object"
        expected = {"status", "summary", "changed_paths", "checks"}
        allowed = expected | ({"archify_receipt"} if archify_required else set())
        if set(value) != allowed:
            return None, "structured_output contains unsupported fields"
        archify_error = _archify_receipt_error(
            value,
            role=archify_role,
            required=archify_required,
            request=request,
        )
        if archify_error:
            return None, f"Archify receipt rejected: {archify_error}"
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


def managed_harness_static_wiring() -> dict[str, dict[str, object]]:
    """Exercise the local prompt/command builders without starting either model CLI."""

    contract = {
        "objective": "verify Workbench Code-as-Harness wiring",
        "allowed_scope": ["src"],
        "forbidden_scope": [],
        "acceptance_commands": ["python -m unittest"],
        "timeout_seconds": 1,
    }
    request = ExecutionRequest(
        task_id="harness-static-probe",
        node_id="worker",
        attempt=1,
        contract=contract,
        spec={
            "title": "static wiring probe",
            "prompt": "do not execute this probe",
            "model": "fixture",
            "verifier": False,
            "write_scopes": (),
        },
        worktree=Path("."),
    )
    directive = governance_directive(contract)
    codex_prompt = CodexExecutor._prompt(request)
    codex_command = CodexExecutor._command(
        "codex",
        request,
        Path("schema.json"),
        Path("result.json"),
    )
    codex_disabled = {
        codex_command[index + 1]
        for index, argument in enumerate(codex_command[:-1])
        if argument == "--disable"
    }
    codex_static_wiring = (
        directive in codex_prompt
        and "--ignore-user-config" in codex_command
        and {"skill_search", "plugins", "plugin_sharing"} <= codex_disabled
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
    claude_directive = claude_command[
        claude_command.index("--append-system-prompt") + 1
    ]
    return {
        "codex": {
            "static_wiring_verified": codex_static_wiring,
            "mechanism": "CodexExecutor._prompt + CodexExecutor._command",
            "external_skill_execution": (
                "disabled-by-workbench-exec-flags"
                if codex_static_wiring
                else "not-ready"
            ),
            "runtime_execution_observed": False,
        },
        "claude-code": {
            "static_wiring_verified": claude_directive == directive,
            "mechanism": "ClaudeExecutor --append-system-prompt",
            "external_skill_execution": "not-observed",
            "runtime_execution_observed": False,
        },
    }


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
    changed = tuple(
        sorted(
            changed_paths_since_input_tree(request.worktree, request.input_tree_sha)
            if request.input_tree_sha is not None
            else manager.changed_paths(request.worktree, request.contract["base_sha"])
        )
    )
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
