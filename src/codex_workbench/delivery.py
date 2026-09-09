from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Literal, Mapping

from .artifacts import ArtifactStore
from .delivery_lifecycle import DeliveryStageContext, DeliveryStageOutcome
from .executors import subscription_environment
from .model import canonical_hash
from .store import WorkbenchStore


class DeliveryError(RuntimeError):
    pass


class _TerminalDeliveryError(DeliveryError):
    """A proven delivery conflict that must not be retried automatically."""


@dataclass(frozen=True)
class GitHubDeliveryRequest:
    task_id: str
    command_id: str
    base_branch: str
    remote: str = "origin"
    merge: bool = False
    release_tag: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "command_id": self.command_id,
            "base_branch": self.base_branch,
            "remote": self.remote,
            "merge": self.merge,
            "release_tag": self.release_tag,
        }


class GitHubDelivery:
    _REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
    _BASE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
    _RELEASE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+=%-]*\Z")
    _COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
    _PR_URL = re.compile(r"https?://[^\s]+\Z")
    _RECOVERY_STAGES = frozenset(
        {
            "prepared",
            "push_started",
            "pushed",
            "pr_create_started",
            "pr_open",
            "ci_passed",
            "merge_started",
            "merged",
            "release_started",
        }
    )
    _TERMINAL_STATES = frozenset({"released"})

    def __init__(
        self,
        store: WorkbenchStore,
        artifacts: ArtifactStore,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.store = store
        self.artifacts = artifacts
        self.runner = runner

    def deliver(self, request: GitHubDeliveryRequest) -> dict:
        """Advance through the explicit publication boundary (legacy API)."""

        return self.advance_to(request, through="publish")

    def advance_to(
        self,
        request: GitHubDeliveryRequest,
        *,
        through: str,
    ) -> dict:
        """Advance one durable GitHub receipt only through a named lifecycle boundary.

        ``integrate`` opens the PR, ``ci`` observes its checks, and
        ``publish`` performs only the request's already-authorized merge and
        release policy.  A later lifecycle stage resumes the same command ID;
        this method never turns a PR request into a deployment.
        """

        if through not in {"integrate", "ci", "publish"}:
            raise ValueError("GitHub delivery boundary must be integrate, ci, or publish")
        self._validate_request(request)
        existing = self.store.check_delivery_command(request.command_id, request.to_dict())
        if existing is not None and self._is_terminal_for_request(existing, request):
            return existing

        task = self.store.get_task(request.task_id)
        verifier = next((node for node in task["nodes"] if node.get("verifier")), None)
        if verifier is None or not verifier.get("worktree"):
            raise DeliveryError("accepted task has no verifier integration worktree")
        worktree = Path(verifier["worktree"]).resolve(strict=True)
        self._require_named_remote(worktree, request.remote)

        receipt = existing or self.store.begin_delivery(
            request.task_id,
            request.command_id,
            request.to_dict(),
        )
        if self._is_terminal_for_request(receipt, request):
            return receipt
        branch = f"codex-workbench/integration/{self._safe(request.task_id)}"
        lease = self.store.acquire_delivery_lease(request.command_id)
        fence = str(lease["fence"])
        receipt = lease["receipt"]
        try:
            self._validate_receipt(request, receipt, branch)
            receipt = self._restore_recovery(request.command_id, receipt, fence)
            if self._is_terminal_for_request(receipt, request) or receipt["state"] in {"failed", "indeterminate"}:
                return receipt

            if receipt["state"] == "accepted":
                commit = self._prepare(worktree, task, branch, request.command_id, fence)
                receipt = self._advance(
                    request.command_id,
                    receipt,
                    fence,
                    "prepared",
                    {"branch": branch, "commit": commit, "recovery": None},
                )

            if receipt["state"] == "prepared":
                receipt = self._push_prepared(
                    request,
                    worktree,
                    receipt,
                    branch,
                    fence,
                )
            if receipt["state"] == "push_started":
                receipt = self._reconcile_unknown_push(
                    request,
                    worktree,
                    receipt,
                    branch,
                    fence,
                )

            if receipt["state"] == "pushed":
                receipt = self._confirm_pushed_branch(
                    request,
                    worktree,
                    receipt,
                    branch,
                    fence,
                )
            if receipt["state"] == "pushed":
                receipt = self._ensure_pr(
                    request,
                    worktree,
                    receipt,
                    branch,
                    fence,
                )
            if receipt["state"] == "pr_create_started":
                receipt = self._reconcile_unknown_pr_create(
                    request,
                    worktree,
                    receipt,
                    branch,
                    fence,
                )

            if through == "integrate":
                return receipt

            if receipt["state"] == "pr_open":
                checks = self._run_owned(
                    request.command_id,
                    fence,
                    ["gh", "pr", "checks", receipt["details"]["pr_url"], "--json", "bucket,name,state"],
                    cwd=worktree,
                    timeout=30,
                    allow_nonzero=True,
                )
                detail = self._command_detail(checks)
                no_checks = checks.returncode == 1 and "no checks reported" in detail.lower()
                if checks.returncode not in {0, 8} and not no_checks:
                    raise DeliveryError(detail)
                try:
                    rows = [] if no_checks else json.loads(checks.stdout)
                except (TypeError, json.JSONDecodeError) as error:
                    raise DeliveryError("GitHub checks returned invalid JSON") from error
                if (not isinstance(rows, list) or any(not isinstance(row, dict)
                        or not isinstance(row.get("bucket"), str)
                        or row["bucket"] not in {"pass", "fail", "pending", "skipping", "cancel"}
                        for row in rows)):
                    raise DeliveryError("GitHub checks returned invalid status fields")
                if any(row["bucket"] in {"fail", "cancel"} for row in rows):
                    raise DeliveryError("GitHub checks failed or were cancelled")
                ci_log = self._evidence(checks, "ci.log")
                if not rows or checks.returncode == 8 or any(row["bucket"] == "pending" for row in rows):
                    if receipt["details"].get("ci_pending") is True and receipt["details"].get("ci_log") == ci_log:
                        return receipt
                    return self._advance(request.command_id, receipt, fence, "pr_open",
                                         {"ci_pending": True, "ci_log": ci_log})
                receipt = self._advance(
                    request.command_id,
                    receipt,
                    fence,
                    "ci_passed",
                    {"ci_pending": False, "ci_log": ci_log},
                )

            if through == "ci":
                return receipt

            if request.merge and receipt["state"] == "ci_passed":
                receipt = self._advance(
                    request.command_id,
                    receipt,
                    fence,
                    "merge_started",
                    {"merge_intent": {"pr_url": receipt["details"]["pr_url"]}},
                )
                merged = self._run_owned(
                    request.command_id,
                    fence,
                    ["gh", "pr", "merge", receipt["details"]["pr_url"], "--merge", "--delete-branch=false"],
                    cwd=worktree,
                    timeout=180,
                )
                view = self._run_owned(
                    request.command_id,
                    fence,
                    ["gh", "pr", "view", receipt["details"]["pr_url"], "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
                    cwd=worktree,
                    timeout=60,
                )
                merge_sha = view.stdout.strip()
                if not merge_sha:
                    raise DeliveryError("GitHub did not return a merge commit")
                receipt = self._advance(
                    request.command_id,
                    receipt,
                    fence,
                    "merged",
                    {"merge_sha": merge_sha, "merge_log": self._evidence(merged, "merge.log")},
                )
            if request.merge and receipt["state"] == "merge_started":
                receipt = self._reconcile_unknown_merge(request, worktree, receipt, fence)

            if request.release_tag and receipt["state"] == "merged":
                receipt = self._ensure_release(
                    worktree,
                    request.release_tag,
                    receipt["details"]["merge_sha"],
                    request.command_id,
                    receipt,
                    fence,
                )
            if request.release_tag and receipt["state"] == "release_started":
                receipt = self._reconcile_unknown_release(
                    request,
                    worktree,
                    receipt,
                    fence,
                )
            return receipt
        except subprocess.TimeoutExpired as error:
            return self._record_failure(
                request.command_id,
                self._current_owned_receipt(request.command_id, fence),
                fence,
                f"delivery command timed out: {error.cmd}",
                timed_out=True,
            )
        except _TerminalDeliveryError as error:
            return self._record_terminal_failure(
                request.command_id,
                self._current_owned_receipt(request.command_id, fence),
                fence,
                str(error),
            )
        except (DeliveryError, OSError, subprocess.CalledProcessError) as error:
            return self._record_failure(
                request.command_id,
                self._current_owned_receipt(request.command_id, fence),
                fence,
                str(error),
                timed_out=False,
            )
        finally:
            self.store.release_delivery_lease(request.command_id, fence)

    @staticmethod
    def _safe(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
        if not normalized:
            raise ValueError("task_id cannot form a Git branch")
        return normalized[:80]

    def _is_terminal_for_request(self, receipt: dict, request: GitHubDeliveryRequest) -> bool:
        """A merged receipt is final only when its frozen request has no release."""

        return receipt["state"] in self._TERMINAL_STATES or (
            receipt["state"] == "merged" and request.release_tag is None
        )

    @classmethod
    def _validate_request(cls, request: GitHubDeliveryRequest) -> None:
        remote = request.remote
        if (
            not isinstance(remote, str)
            or not cls._REMOTE_NAME.fullmatch(remote)
            or remote.endswith(".")
            or ".." in remote
            or remote.lower().endswith(".lock")
        ):
            raise ValueError("remote must be a simple named Git remote")

        base_branch = request.base_branch
        if (
            not isinstance(base_branch, str)
            or not cls._BASE_BRANCH.fullmatch(base_branch)
            or base_branch.startswith("/")
            or base_branch.endswith((".", "/"))
            or ".." in base_branch
            or "//" in base_branch
            or "@{" in base_branch
            or any(part.startswith(".") or part.lower().endswith(".lock") for part in base_branch.split("/"))
        ):
            raise ValueError("base_branch must be a safe Git branch name")

        tag = request.release_tag
        if tag is None:
            return
        if not request.merge:
            raise ValueError("release_tag requires merge=true")
        if (
            not isinstance(tag, str)
            or not cls._RELEASE_TAG.fullmatch(tag)
            or tag.startswith("refs/")
            or tag == "@"
            or "@{" in tag
            or tag.endswith((".", "/"))
            or ".." in tag
            or "//" in tag
            or any(part.startswith(".") or part.lower().endswith(".lock") for part in tag.split("/"))
        ):
            raise ValueError("release_tag must be a safe Git tag name")

    def _require_named_remote(self, worktree: Path, remote: str) -> None:
        result = self._run(
            ["git", "-C", str(worktree), "remote", "get-url", "--", remote],
            timeout=60,
            allow_nonzero=True,
        )
        if result.returncode:
            raise DeliveryError("named Git remote does not exist")

    def _prepare(
        self,
        worktree: Path,
        task: dict,
        branch: str,
        command_id: str,
        fence: str,
    ) -> str:
        base_sha = task["contract"]["base_sha"]
        self._run_owned(command_id, fence, ["git", "-C", str(worktree), "switch", "-C", branch], timeout=60)
        status = self._run_owned(
            command_id,
            fence,
            ["git", "-C", str(worktree), "status", "--porcelain"],
            timeout=60,
        )
        if status.stdout.strip():
            self._run_owned(command_id, fence, ["git", "-C", str(worktree), "add", "--all"], timeout=60)
            self._run_owned(
                command_id,
                fence,
                ["git", "-C", str(worktree), "commit", "-m", f"workbench: {task['contract']['objective'][:72]}"],
                timeout=60,
            )
        commit = self._run_owned(
            command_id,
            fence,
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            timeout=60,
        ).stdout.strip()
        changed = self._run_owned(
            command_id,
            fence,
            ["git", "-C", str(worktree), "diff", "--quiet", base_sha, commit, "--"],
            timeout=60,
            allow_nonzero=True,
        )
        if changed.returncode == 0:
            raise DeliveryError("accepted task produced no integration diff")
        if changed.returncode != 1:
            raise DeliveryError("cannot verify integration diff")
        return commit

    def _push_prepared(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        branch: str,
        fence: str,
    ) -> dict:
        commit = self._receipt_commit(receipt, branch)
        kind, remote_commit, detail = self._probe_remote_branch(
            request.command_id,
            fence,
            worktree,
            request.remote,
            branch,
        )
        if kind == "present":
            if remote_commit != commit:
                raise _TerminalDeliveryError("remote delivery branch does not match the durable commit")
            return self._advance(
                request.command_id,
                receipt,
                fence,
                "pushed",
                {"push_reconciled": True, "remote_commit": remote_commit},
            )
        if kind == "unknown":
            return self._defer(
                request.command_id,
                receipt,
                fence,
                "indeterminate",
                "prepared",
                f"cannot determine whether the remote delivery branch is absent: {detail}",
            )

        self._require_local_commit(request.command_id, fence, worktree, commit)
        receipt = self._advance(
            request.command_id,
            receipt,
            fence,
            "push_started",
            {"push_intent": {"branch": branch, "commit": commit}},
        )
        pushed = self._run_owned(
            request.command_id,
            fence,
            [
                "git",
                "-C",
                str(worktree),
                "push",
                "--set-upstream",
                "--",
                request.remote,
                f"HEAD:{branch}",
            ],
            timeout=120,
        )
        return self._advance(
            request.command_id,
            receipt,
            fence,
            "pushed",
            {"push_log": self._evidence(pushed, "push.log"), "remote_commit": commit},
        )

    def _reconcile_unknown_push(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        branch: str,
        fence: str,
    ) -> dict:
        commit = self._receipt_commit(receipt, branch)
        kind, remote_commit, detail = self._probe_remote_branch(
            request.command_id,
            fence,
            worktree,
            request.remote,
            branch,
        )
        if kind == "present":
            if remote_commit != commit:
                raise _TerminalDeliveryError("remote delivery branch does not match the durable commit")
            return self._advance(
                request.command_id,
                receipt,
                fence,
                "pushed",
                {"push_reconciled": True, "remote_commit": remote_commit},
            )
        if kind == "absent":
            detail = "remote delivery branch is absent after an attempted push"
        return self._defer(
            request.command_id,
            receipt,
            fence,
            "indeterminate",
            "push_started",
            f"cannot prove the prior push outcome; refusing to replay it: {detail}",
        )

    def _confirm_pushed_branch(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        branch: str,
        fence: str,
    ) -> dict:
        commit = self._receipt_commit(receipt, branch)
        kind, remote_commit, detail = self._probe_remote_branch(
            request.command_id,
            fence,
            worktree,
            request.remote,
            branch,
        )
        if kind == "present" and remote_commit == commit:
            return receipt
        if kind == "present":
            raise _TerminalDeliveryError("remote delivery branch does not match the durable commit")
        if kind == "absent":
            detail = "remote delivery branch is absent despite the pushed receipt"
        return self._defer(
            request.command_id,
            receipt,
            fence,
            "failed",
            "pushed",
            f"cannot confirm the durable delivery branch before PR work: {detail}",
        )

    def _ensure_pr(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        branch: str,
        fence: str,
    ) -> dict:
        kind, pr_url, detail = self._probe_pr(request.command_id, fence, worktree, branch)
        if kind == "present":
            assert pr_url is not None
            return self._advance(
                request.command_id,
                receipt,
                fence,
                "pr_open",
                {"pr_url": pr_url, "pr_reconciled": True},
            )
        if kind == "unknown":
            return self._defer(
                request.command_id,
                receipt,
                fence,
                "failed",
                "pushed",
                f"cannot determine whether the pull request exists: {detail}",
            )

        receipt = self._advance(
            request.command_id,
            receipt,
            fence,
            "pr_create_started",
            {"pr_create_intent": {"branch": branch, "base_branch": request.base_branch}},
        )
        created = self._run_owned(
            request.command_id,
            fence,
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--base",
                request.base_branch,
                "--title",
                f"Workbench delivery: {request.task_id}",
                "--body",
                f"Accepted and independently verified Workbench task `{request.task_id}`.",
            ],
            cwd=worktree,
            timeout=120,
        )
        pr_url = self._created_pr_url(created)
        return self._advance(
            request.command_id,
            receipt,
            fence,
            "pr_open",
            {"pr_url": pr_url, "pr_create_log": self._evidence(created, "pr-create.log")},
        )

    def _reconcile_unknown_pr_create(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        branch: str,
        fence: str,
    ) -> dict:
        kind, pr_url, detail = self._probe_pr(request.command_id, fence, worktree, branch)
        if kind == "present":
            assert pr_url is not None
            return self._advance(
                request.command_id,
                receipt,
                fence,
                "pr_open",
                {"pr_url": pr_url, "pr_reconciled": True},
            )
        if kind == "absent":
            detail = "pull request is absent after an attempted create"
        return self._defer(
            request.command_id,
            receipt,
            fence,
            "indeterminate",
            "pr_create_started",
            f"cannot prove the prior pull-request create outcome; refusing to replay it: {detail}",
        )

    def _reconcile_unknown_merge(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        fence: str,
    ) -> dict:
        view = self._run_owned(
            request.command_id,
            fence,
            ["gh", "pr", "view", receipt["details"]["pr_url"], "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
            cwd=worktree,
            timeout=60,
            allow_nonzero=True,
        )
        merge_sha = view.stdout.strip() if view.returncode == 0 else ""
        if merge_sha:
            return self._advance(
                request.command_id,
                receipt,
                fence,
                "merged",
                {"merge_sha": merge_sha, "merge_reconciled": True},
            )
        detail = self._command_detail(view)
        return self._defer(
            request.command_id,
            receipt,
            fence,
            "indeterminate",
            "merge_started",
            f"cannot prove the prior merge outcome; refusing to replay it: {detail}",
        )

    def _ensure_release(
        self,
        worktree: Path,
        tag: str,
        merge_sha: str,
        command_id: str,
        receipt: dict,
        fence: str,
    ) -> dict:
        kind, detail = self._probe_release(command_id, fence, worktree, tag)
        if kind == "present":
            return self._advance(
                command_id,
                receipt,
                fence,
                "released",
                {"release_tag": tag, "release_reconciled": True},
            )
        if kind == "unknown":
            return self._defer(
                command_id,
                receipt,
                fence,
                "failed",
                "merged",
                f"cannot determine whether the release exists: {detail}",
            )
        receipt = self._advance(
            command_id,
            receipt,
            fence,
            "release_started",
            {"release_intent": {"tag": tag, "merge_sha": merge_sha}},
        )
        released = self._run_owned(
            command_id,
            fence,
            [
                "gh",
                "release",
                "create",
                "--target",
                merge_sha,
                "--generate-notes",
                "--",
                tag,
            ],
            cwd=worktree,
            timeout=180,
        )
        return self._advance(
            command_id,
            receipt,
            fence,
            "released",
            {"release_tag": tag, "release_log": self._evidence(released, "release.log")},
        )

    def _reconcile_unknown_release(
        self,
        request: GitHubDeliveryRequest,
        worktree: Path,
        receipt: dict,
        fence: str,
    ) -> dict:
        assert request.release_tag is not None
        kind, detail = self._probe_release(request.command_id, fence, worktree, request.release_tag)
        if kind == "present":
            return self._advance(
                request.command_id,
                receipt,
                fence,
                "released",
                {"release_tag": request.release_tag, "release_reconciled": True},
            )
        if kind == "absent":
            detail = "release is absent after an attempted create"
        return self._defer(
            request.command_id,
            receipt,
            fence,
            "indeterminate",
            "release_started",
            f"cannot prove the prior release outcome; refusing to replay it: {detail}",
        )

    def _validate_receipt(
        self,
        request: GitHubDeliveryRequest,
        receipt: dict,
        branch: str,
    ) -> None:
        if receipt.get("task_id") != request.task_id:
            raise _TerminalDeliveryError("delivery receipt task identity does not match the request")
        details = receipt.get("details")
        if not isinstance(details, dict) or details.get("request") != request.to_dict():
            raise _TerminalDeliveryError("delivery receipt request identity does not match the request")
        state = receipt.get("state")
        recovery = details.get("recovery")
        needs_source = (
            state in self._RECOVERY_STAGES
            or isinstance(recovery, dict) and recovery.get("resume_state") in self._RECOVERY_STAGES
        )
        if needs_source:
            self._receipt_commit(receipt, branch)

    def _restore_recovery(self, command_id: str, receipt: dict, fence: str) -> dict:
        """Restore only a durable, explicitly safe continuation stage."""

        if receipt["state"] not in {"failed", "indeterminate"}:
            return receipt
        recovery = receipt["details"].get("recovery")
        legacy_stage = self._legacy_recovery_stage(receipt)
        if isinstance(recovery, dict) and recovery.get("schema_version") == 1:
            resume_state = recovery.get("resume_state")
            legacy = False
        else:
            resume_state = legacy_stage
            legacy = resume_state is not None
        if resume_state not in self._RECOVERY_STAGES:
            return receipt
        if legacy:
            # A pre-fence receipt only implies a successful push when its
            # durable push evidence exists.  A bare indeterminate prepared
            # receipt becomes ``push_started`` and is reconciled without a
            # replay.  A failed-before-push receipt has neither proof and
            # remains terminal.
            self._receipt_commit(receipt, str(receipt["details"].get("branch", "")))
        return self._advance(
            command_id,
            receipt,
            fence,
            str(resume_state),
            {
                "recovery": None,
                "recovered_from": {
                    "state": receipt["state"],
                    "resume_state": resume_state,
                    "legacy_inference": legacy,
                },
            },
        )

    def _legacy_recovery_stage(self, receipt: dict) -> str | None:
        """Recover receipts written before delivery fences were introduced.

        The old implementation wrote ``push_log`` only after a successful
        local push command and before invoking GitHub.  That is enough to
        re-enter the read-only remote-branch reconciliation path, but never to
        replay a failed push.  Older indeterminate receipts without that proof
        are intentionally treated as an in-flight push.
        """

        details = receipt.get("details")
        if not isinstance(details, dict):
            return None
        if isinstance(details.get("pr_url"), str) and details["pr_url"].strip():
            return "pr_open"
        if isinstance(details.get("push_log"), str) and details["push_log"].strip():
            return "pushed"
        if (
            receipt.get("state") == "indeterminate"
            and isinstance(details.get("branch"), str)
            and isinstance(details.get("commit"), str)
        ):
            return "push_started"
        return None

    def _advance(
        self,
        command_id: str,
        receipt: dict,
        fence: str,
        state: str,
        details: dict,
    ) -> dict:
        self.store.renew_delivery_lease(command_id, fence)
        return self.store.update_delivery(
            command_id,
            state,
            details,
            expected_states=(str(receipt["state"]),),
            fence=fence,
        )

    def _current_owned_receipt(self, command_id: str, fence: str) -> dict:
        """Read the current receipt only after proving this fence still owns it.

        A helper can durably record an external-effect intent and then raise
        before returning its updated local receipt to ``deliver``.  Reloading
        here makes error handling classify that durable stage (for example
        ``push_started``), rather than an obsolete caller-local snapshot.
        """

        self.store.renew_delivery_lease(command_id, fence)
        return self.store.get_delivery(command_id)

    def _defer(
        self,
        command_id: str,
        receipt: dict,
        fence: str,
        state: Literal["failed", "indeterminate"],
        resume_state: str,
        error: str,
    ) -> dict:
        if resume_state not in self._RECOVERY_STAGES:
            raise AssertionError(f"unsupported delivery recovery stage {resume_state!r}")
        return self._advance(
            command_id,
            receipt,
            fence,
            state,
            {
                "error": error,
                "recovery": {"schema_version": 1, "resume_state": resume_state},
            },
        )

    def _record_terminal_failure(
        self,
        command_id: str,
        receipt: dict,
        fence: str,
        error: str,
    ) -> dict:
        return self._advance(
            command_id,
            receipt,
            fence,
            "failed",
            {"error": error, "recovery": None},
        )

    def _record_failure(
        self,
        command_id: str,
        receipt: dict,
        fence: str,
        error: str,
        *,
        timed_out: bool,
    ) -> dict:
        """Persist whether retry may safely resume or must reconcile first."""

        stage = str(receipt["state"])
        if stage in {"push_started", "pr_create_started", "merge_started", "release_started"}:
            return self._defer(command_id, receipt, fence, "indeterminate", stage, error)
        if stage in {"pushed", "pr_open", "ci_passed", "merged"}:
            return self._defer(
                command_id,
                receipt,
                fence,
                "indeterminate" if timed_out else "failed",
                stage,
                error,
            )
        if stage == "prepared" and timed_out:
            # The timeout happened while probing before the durable push
            # intent.  Retrying the probe is safe; a future push still needs a
            # confirmed absent remote ref and a fresh local-commit check.
            return self._defer(command_id, receipt, fence, "indeterminate", "prepared", error)
        return self._record_terminal_failure(command_id, receipt, fence, error)

    def _receipt_commit(self, receipt: dict, branch: str) -> str:
        details = receipt.get("details")
        if not isinstance(details, dict) or details.get("branch") != branch:
            raise _TerminalDeliveryError("delivery receipt branch does not match the task identity")
        commit = details.get("commit")
        if not isinstance(commit, str) or not self._COMMIT.fullmatch(commit):
            raise _TerminalDeliveryError("delivery receipt has an invalid integration commit")
        return commit

    def _require_local_commit(
        self,
        command_id: str,
        fence: str,
        worktree: Path,
        commit: str,
    ) -> None:
        current = self._run_owned(
            command_id,
            fence,
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            timeout=60,
        ).stdout.strip()
        if current != commit:
            raise _TerminalDeliveryError("verifier worktree HEAD no longer matches the durable commit")

    def _probe_remote_branch(
        self,
        command_id: str,
        fence: str,
        worktree: Path,
        remote: str,
        branch: str,
    ) -> tuple[Literal["present", "absent", "unknown"], str | None, str]:
        ref = f"refs/heads/{branch}"
        result = self._run_owned(
            command_id,
            fence,
            ["git", "-C", str(worktree), "ls-remote", "--exit-code", "--heads", "--", remote, ref],
            timeout=60,
            allow_nonzero=True,
        )
        if result.returncode == 2:
            return "absent", None, "remote ref is absent"
        if result.returncode != 0:
            return "unknown", None, self._command_detail(result)
        matches = []
        for line in result.stdout.splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[1].strip() == ref and self._COMMIT.fullmatch(fields[0].strip()):
                matches.append(fields[0].strip())
        if len(matches) != 1:
            return "unknown", None, "remote ref probe returned an unexpected result"
        return "present", matches[0], "remote ref is present"

    def _probe_pr(
        self,
        command_id: str,
        fence: str,
        worktree: Path,
        branch: str,
    ) -> tuple[Literal["present", "absent", "unknown"], str | None, str]:
        result = self._run_owned(
            command_id,
            fence,
            ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
            cwd=worktree,
            timeout=60,
            allow_nonzero=True,
        )
        if result.returncode == 0:
            url = self._url_from_output(result.stdout)
            if url is not None:
                return "present", url, "pull request is present"
            return "unknown", None, "pull-request lookup returned no valid URL"
        if self._is_confirmed_absent_pr(result):
            return "absent", None, "pull request is confirmed absent"
        return "unknown", None, self._command_detail(result)

    def _probe_release(
        self,
        command_id: str,
        fence: str,
        worktree: Path,
        tag: str,
    ) -> tuple[Literal["present", "absent", "unknown"], str]:
        result = self._run_owned(
            command_id,
            fence,
            ["gh", "release", "view", "--json", "tagName", "--jq", ".tagName", "--", tag],
            cwd=worktree,
            timeout=60,
            allow_nonzero=True,
        )
        if result.returncode == 0 and result.stdout.strip() == tag:
            return "present", "release is present"
        text = self._command_detail(result).lower()
        if result.returncode == 1 and "not found" in text and not self._has_unknown_response_marker(text):
            return "absent", "release is confirmed absent"
        return "unknown", self._command_detail(result)

    def _created_pr_url(self, result: subprocess.CompletedProcess[str]) -> str:
        url = self._url_from_output(result.stdout)
        if url is None:
            raise DeliveryError("GitHub did not return a pull-request URL")
        return url

    def _url_from_output(self, output: str) -> str | None:
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if self._PR_URL.fullmatch(candidate):
                return candidate
        return None

    @classmethod
    def _is_confirmed_absent_pr(cls, result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode != 1:
            return False
        text = cls._command_detail(result).lower()
        if cls._has_unknown_response_marker(text):
            return False
        return any(
            marker in text
            for marker in (
                "no pull requests found",
                "no pull request found",
                "pull request not found",
                "not found",
            )
        )

    @staticmethod
    def _has_unknown_response_marker(text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "http 401",
                "401 unauthorized",
                "bad credentials",
                "authentication",
                "unauthorized",
                "forbidden",
                "rate limit",
                "timeout",
                "timed out",
                "connection",
                "network",
                "temporarily unavailable",
                "http 5",
            )
        )

    @staticmethod
    def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        return detail[:500] or f"command exited {result.returncode}"

    def _run_owned(
        self,
        command_id: str,
        fence: str,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int,
        allow_nonzero: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.store.renew_delivery_lease(command_id, fence)
        return self._run(command, cwd=cwd, timeout=timeout, allow_nonzero=allow_nonzero)

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int,
        allow_nonzero: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=subscription_environment(),
            check=False,
            shell=False,
        )
        if result.returncode and not allow_nonzero:
            raise DeliveryError(result.stderr.strip() or result.stdout.strip() or f"command exited {result.returncode}")
        return result

    def _evidence(self, result: subprocess.CompletedProcess[str], suffix: str) -> str:
        content = f"$ {' '.join(result.args)}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}"
        return self.artifacts.put_text(content, suffix)


class GitHubDeliveryStageAdapter:
    """Adapter bridge from a lifecycle stage to the existing GitHub receipt.

    It is intentionally narrow: it can integrate, observe CI, and publish a
    previously requested merge/release.  Deployment and live verification are
    supplied by a separate authority-owned adapter, preserving their distinct
    interruption and rollback policy.
    """

    def __init__(
        self,
        delivery: GitHubDelivery,
        *,
        identity_provider: Callable[[DeliveryStageContext, dict[str, Any]], Mapping[str, Any]]
        | None = None,
    ):
        self.delivery = delivery
        self.identity_provider = identity_provider

    def execute_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        if context.stage not in {"integrate", "ci", "publish"}:
            raise ValueError("GitHub lifecycle adapter only handles integrate, ci, and publish")
        endpoints = context.objective.get("requested_endpoints")
        github = endpoints.get("github") if isinstance(endpoints, dict) else None
        if not isinstance(github, dict):
            raise ValueError("objective has no requested GitHub endpoint")
        # GitHub integration, CI, and publication are three lifecycle
        # receipts over one external delivery command.  Reusing this stable
        # command ID lets later stages resume the PR/CI state rather than
        # opening a second PR after a coordinator restart.
        github_command_id = "delivery-github-" + canonical_hash(
            {"objective_id": context.objective["objective_id"], "endpoint": github}
        )[:24]
        request = GitHubDeliveryRequest(
            task_id=str(context.objective["task_id"]),
            command_id=github_command_id,
            remote=str(github.get("remote", "origin")),
            base_branch=str(github.get("base_branch", "")),
            merge=github.get("merge", False) is True,
            release_tag=(
                str(github["release_tag"])
                if github.get("release_tag") is not None
                else None
            ),
        )
        receipt = self.delivery.advance_to(request, through=context.stage)
        succeeded_states = {
            "integrate": {"pr_open", "ci_passed", "merged", "released"},
            "ci": {"ci_passed", "merged", "released"},
            "publish": {"merged", "released"},
        }
        if not request.merge:
            succeeded_states["publish"].add("ci_passed")
        if request.release_tag is not None:
            succeeded_states["publish"] = {"released"}
        evidence = canonical_hash(
            {
                "dispatch_id": context.dispatch_id,
                "stage": context.stage,
                "github_receipt": receipt,
            }
        )
        if receipt["state"] in succeeded_states[context.stage]:
            identities = (
                dict(self.identity_provider(context, receipt))
                if self.identity_provider is not None
                else {}
            )
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:github",
                receipt={"github_delivery": receipt},
                evidence_fingerprint=evidence,
                identities=identities,
            )
        if receipt["state"] == "pr_open" and receipt["details"].get("ci_pending") is True:
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:ci-pending", status="deferred",
                receipt={"github_delivery": receipt},
                failure={"kind": "execution-environment", "detail": "GitHub CI has not finished reporting checks"},
            )
        if receipt["state"] == "indeterminate":
            return DeliveryStageOutcome(
                receipt_id=f"{context.dispatch_id}:github-indeterminate",
                status="indeterminate",
                receipt={"github_delivery": receipt},
                failure={
                    "kind": "unknown-effects",
                    "detail": "GitHub delivery has an indeterminate external effect",
                },
                retry_eligible=False,
            )
        return DeliveryStageOutcome(
            receipt_id=f"{context.dispatch_id}:github-failed",
            status="failed",
            receipt={"github_delivery": receipt},
            failure={
                "kind": "verification-failure",
                "detail": "GitHub integration, CI, or publication did not reach its requested receipt",
            },
            retry_eligible=True,
        )

    def reconcile_stage(self, context: DeliveryStageContext) -> DeliveryStageOutcome:
        """GitHub's durable command receipt is the authoritative reconciliation path."""

        return self.execute_stage(context)
