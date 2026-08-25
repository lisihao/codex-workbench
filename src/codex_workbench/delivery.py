from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Callable

from .artifacts import ArtifactStore
from .executors import subscription_environment
from .store import WorkbenchStore


class DeliveryError(RuntimeError):
    pass


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
        if request.release_tag and not request.merge:
            raise ValueError("release_tag requires merge=true")
        receipt = self.store.begin_delivery(request.task_id, request.command_id, request.to_dict())
        if receipt["state"] in {"merged", "released"}:
            return receipt
        task = self.store.get_task(request.task_id)
        verifier = next((node for node in task["nodes"] if node.get("verifier")), None)
        if verifier is None or not verifier.get("worktree"):
            raise DeliveryError("accepted task has no verifier integration worktree")
        worktree = Path(verifier["worktree"]).resolve(strict=True)
        branch = f"codex-workbench/integration/{self._safe(request.task_id)}"
        try:
            if receipt["state"] == "accepted":
                commit = self._prepare(worktree, task, branch)
                receipt = self.store.update_delivery(
                    request.command_id,
                    "prepared",
                    {"branch": branch, "commit": commit},
                )
            else:
                commit = receipt["details"]["commit"]

            if receipt["state"] == "prepared":
                pushed = self._run(
                    ["git", "-C", str(worktree), "push", "--set-upstream", request.remote, f"HEAD:{branch}"],
                    timeout=120,
                )
                receipt = self.store.update_delivery(
                    request.command_id,
                    "pushed",
                    {"push_log": self._evidence(pushed, "push.log")},
                )

            if receipt["state"] == "pushed":
                pr_url = self._ensure_pr(worktree, branch, request.base_branch, request.task_id)
                receipt = self.store.update_delivery(request.command_id, "pr_open", {"pr_url": pr_url})

            if receipt["state"] == "pr_open":
                checks = self._run(
                    ["gh", "pr", "checks", receipt["details"]["pr_url"], "--watch"],
                    cwd=worktree,
                    timeout=1800,
                )
                receipt = self.store.update_delivery(
                    request.command_id,
                    "ci_passed",
                    {"ci_log": self._evidence(checks, "ci.log")},
                )

            if request.merge and receipt["state"] == "ci_passed":
                merged = self._run(
                    ["gh", "pr", "merge", receipt["details"]["pr_url"], "--merge", "--delete-branch=false"],
                    cwd=worktree,
                    timeout=180,
                )
                view = self._run(
                    ["gh", "pr", "view", receipt["details"]["pr_url"], "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
                    cwd=worktree,
                    timeout=60,
                )
                merge_sha = view.stdout.strip()
                if not merge_sha:
                    raise DeliveryError("GitHub did not return a merge commit")
                receipt = self.store.update_delivery(
                    request.command_id,
                    "merged",
                    {"merge_sha": merge_sha, "merge_log": self._evidence(merged, "merge.log")},
                )

            if request.release_tag and receipt["state"] == "merged":
                released = self._ensure_release(
                    worktree,
                    request.release_tag,
                    receipt["details"]["merge_sha"],
                )
                receipt = self.store.update_delivery(
                    request.command_id,
                    "released",
                    {"release_tag": request.release_tag, "release_log": self._evidence(released, "release.log")},
                )
            return receipt
        except subprocess.TimeoutExpired as error:
            return self.store.update_delivery(
                request.command_id,
                "indeterminate",
                {"error": f"delivery command timed out: {error.cmd}"},
            )
        except (DeliveryError, OSError, subprocess.CalledProcessError) as error:
            return self.store.update_delivery(
                request.command_id,
                "failed",
                {"error": str(error)},
            )

    @staticmethod
    def _safe(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
        if not normalized:
            raise ValueError("task_id cannot form a Git branch")
        return normalized[:80]

    def _prepare(self, worktree: Path, task: dict, branch: str) -> str:
        base_sha = task["contract"]["base_sha"]
        self._run(["git", "-C", str(worktree), "switch", "-C", branch], timeout=60)
        status = self._run(["git", "-C", str(worktree), "status", "--porcelain"], timeout=60)
        if status.stdout.strip():
            self._run(["git", "-C", str(worktree), "add", "--all"], timeout=60)
            self._run(
                ["git", "-C", str(worktree), "commit", "-m", f"workbench: {task['contract']['objective'][:72]}"],
                timeout=60,
            )
        commit = self._run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=60).stdout.strip()
        changed = self._run(
            ["git", "-C", str(worktree), "diff", "--quiet", base_sha, commit],
            timeout=60,
            allow_nonzero=True,
        )
        if changed.returncode == 0:
            raise DeliveryError("accepted task produced no integration diff")
        if changed.returncode != 1:
            raise DeliveryError("cannot verify integration diff")
        return commit

    def _ensure_pr(self, worktree: Path, branch: str, base_branch: str, task_id: str) -> str:
        existing = self._run(
            ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
            cwd=worktree,
            timeout=60,
            allow_nonzero=True,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip()
        created = self._run(
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--base",
                base_branch,
                "--title",
                f"Workbench delivery: {task_id}",
                "--body",
                f"Accepted and independently verified Workbench task `{task_id}`.",
            ],
            cwd=worktree,
            timeout=120,
        )
        return created.stdout.strip().splitlines()[-1]

    def _ensure_release(self, worktree: Path, tag: str, merge_sha: str) -> subprocess.CompletedProcess[str]:
        existing = self._run(
            ["gh", "release", "view", tag, "--json", "tagName", "--jq", ".tagName"],
            cwd=worktree,
            timeout=60,
            allow_nonzero=True,
        )
        if existing.returncode == 0 and existing.stdout.strip() == tag:
            return existing
        return self._run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--target",
                merge_sha,
                "--generate-notes",
            ],
            cwd=worktree,
            timeout=180,
        )

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
        )
        if result.returncode and not allow_nonzero:
            raise DeliveryError(result.stderr.strip() or result.stdout.strip() or f"command exited {result.returncode}")
        return result

    def _evidence(self, result: subprocess.CompletedProcess[str], suffix: str) -> str:
        content = f"$ {' '.join(result.args)}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}"
        return self.artifacts.put_text(content, suffix)
