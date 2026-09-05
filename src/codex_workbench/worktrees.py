from __future__ import annotations

import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import subprocess
import threading
from uuid import uuid4


class WorktreeError(RuntimeError):
    pass


def _safe_segment(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not result:
        raise ValueError("identifier cannot be normalized to an empty path segment")
    return result[:80]


class WorktreeManager:
    def __init__(self, root: Path):
        self.root = root
        self._repository_locks: dict[Path, threading.Lock] = {}
        self._repository_locks_guard = threading.Lock()

    def _repository_lock(self, repository: Path) -> threading.Lock:
        with self._repository_locks_guard:
            return self._repository_locks.setdefault(repository, threading.Lock())

    @staticmethod
    def branch_name(task_id: str, node_id: str, attempt: int) -> str:
        task_segment = _safe_segment(task_id)
        node_segment = _safe_segment(node_id)
        return f"codex-workbench/{task_segment}/{node_segment}-a{attempt}"

    @staticmethod
    def _git(repository: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise WorktreeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def prepare(
        self,
        repository: str,
        base_sha: str,
        task_id: str,
        node_id: str,
        attempt: int,
    ) -> Path:
        repo = Path(repository).expanduser().resolve(strict=True)
        with self._repository_lock(repo):
            if not (repo / ".git").exists() and not self._git(repo, "rev-parse", "--git-dir"):
                raise WorktreeError(f"repository is not a Git checkout: {repo}")
            resolved_base = self._git(repo, "rev-parse", f"{base_sha}^{{commit}}")
            task_segment = _safe_segment(task_id)
            node_segment = _safe_segment(node_id)
            worktree = (
                self.root / task_segment / f"{node_segment}-a{attempt}"
            ).expanduser().resolve(strict=False)
            branch = self.branch_name(task_id, node_id, attempt)
            if worktree.exists():
                actual = self._git(worktree, "rev-parse", "HEAD")
                if actual != resolved_base:
                    raise WorktreeError(f"existing worktree {worktree} is not at contract base {resolved_base}")
                return worktree
            worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), resolved_base],
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if result.returncode:
                raise WorktreeError(result.stderr.strip() or result.stdout.strip())
            os.chmod(worktree.parent, 0o700)
            return worktree

    def prepare_clean(
        self,
        repository: str,
        base_sha: str,
        task_id: str,
        node_id: str,
        attempt: int,
    ) -> Path:
        """Prepare a recovery target and require its input tree to be clean.

        A blocked worker's worktree is evidence, not an execution target.  The
        recovery attempt therefore gets the normal deterministic Workbench
        worktree for its new attempt, and the patch is applied only after this
        method proves that the target starts at the contract base with no
        tracked, staged, or untracked changes.
        """

        repo = Path(repository).expanduser().resolve(strict=True)
        target = (
            self.root / _safe_segment(task_id) / f"{_safe_segment(node_id)}-a{attempt}"
        ).expanduser().resolve(strict=False)
        expected_branch = self.branch_name(task_id, node_id, attempt)
        if target.is_symlink():
            raise WorktreeError(f"recovery target {target} must not be a symlink")
        if target.exists():
            # A prior recovery may have crashed while moving a failed a2 into
            # diagnostics. Preserve that tree first, then reclaim only this
            # deterministic retry slot.
            self.archive_failed_recovery(
                repo,
                target,
                expected_branch,
                task_id=task_id,
                node_id=node_id,
                attempt=attempt,
            )
        with self._repository_lock(repo):
            self._release_detached_recovery_branch(repo, expected_branch)
        worktree = self.prepare(str(repo), base_sha, task_id, node_id, attempt)
        actual_branch = self._git(worktree, "branch", "--show-current")
        if actual_branch != expected_branch:
            raise WorktreeError(
                f"recovery target {worktree} is on branch {actual_branch!r}, "
                f"expected {expected_branch!r}"
            )
        changed = self.changed_paths(worktree, base_sha)
        if changed:
            raise WorktreeError(
                f"recovery target {worktree} is not clean: {', '.join(sorted(changed))}"
            )
        return worktree

    def move(self, repository: str | Path, worktree: Path, destination: Path) -> Path:
        repo = Path(repository).expanduser().resolve(strict=True)
        source = worktree.expanduser().resolve(strict=True)
        target = destination.expanduser().absolute()
        with self._repository_lock(repo):
            if target.exists():
                raise WorktreeError(f"worktree quarantine destination already exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._git(repo, "worktree", "move", str(source), str(target))
            os.chmod(target.parent, 0o700)
            return target

    def remove(
        self,
        repository: str | Path,
        worktree: Path,
        branch: str,
    ) -> None:
        repo = Path(repository).expanduser().resolve(strict=True)
        target = worktree.expanduser().absolute()
        with self._repository_lock(repo):
            if target.exists():
                self._git(repo, "worktree", "remove", "--force", str(target))
            self._git(repo, "worktree", "prune")
            branch_result = subprocess.run(
                ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if branch_result.returncode == 0:
                self._git(repo, "branch", "-D", branch)
            elif branch_result.returncode != 1:
                raise WorktreeError(f"cannot inspect worktree branch {branch!r}")
            if target.exists():
                raise WorktreeError(f"worktree path still exists after removal: {target}")

    def archive_failed_recovery(
        self,
        repository: str | Path,
        worktree: Path,
        branch: str,
        *,
        task_id: str,
        node_id: str,
        attempt: int,
    ) -> Path:
        """Preserve a failed clean recovery target outside its reusable slot.

        The target has never become the node's authoritative allocation.  It is
        therefore detached, moved under a private recovery archive, and its
        branch is released so the same deterministic a2 slot can be prepared
        again without discarding the failed target's diagnostic state.
        """

        repo = Path(repository).expanduser().resolve(strict=True)
        source = worktree.expanduser().resolve(strict=True)
        root = self.root.expanduser().resolve(strict=False)
        if not source.is_relative_to(root):
            raise WorktreeError("failed recovery target is outside the Workbench worktree root")
        expected_branch = self.branch_name(task_id, node_id, attempt)
        if branch != expected_branch:
            raise WorktreeError("failed recovery target branch does not match its attempt")
        archive_parent = root / _safe_segment(task_id) / "recovery-failures"
        destination = archive_parent / (
            f"{_safe_segment(node_id)}-a{attempt}-{uuid4().hex[:12]}"
        )
        with self._repository_lock(repo):
            current_branch = self._git(source, "branch", "--show-current")
            if current_branch not in {"", branch}:
                raise WorktreeError("failed recovery target no longer matches its allocated branch")
            if current_branch == branch:
                self._git(source, "switch", "--detach")
            archive_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._git(repo, "worktree", "move", str(source), str(destination))
            os.chmod(archive_parent, 0o700)
            self._release_detached_recovery_branch(repo, branch)
            if not destination.exists():
                raise WorktreeError(f"failed recovery archive is missing: {destination}")
            return destination

    def _release_detached_recovery_branch(self, repository: Path, branch: str) -> None:
        """Release a stale recovery branch only when no worktree owns it."""

        binding = f"branch refs/heads/{branch}"
        if binding in self._git(repository, "worktree", "list", "--porcelain").splitlines():
            raise WorktreeError(f"recovery branch {branch!r} is still checked out")
        branch_result = subprocess.run(
            ["git", "-C", str(repository), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if branch_result.returncode == 0:
            self._git(repository, "branch", "-D", branch)
        elif branch_result.returncode != 1:
            raise WorktreeError(f"cannot inspect failed recovery branch {branch!r}")

    def changed_paths(self, worktree: Path, base_sha: str) -> set[str]:
        committed = self._git(worktree, "diff", "--name-only", f"{base_sha}...HEAD").splitlines()
        unstaged = self._git(worktree, "diff", "--name-only").splitlines()
        staged = self._git(worktree, "diff", "--name-only", "--cached").splitlines()
        untracked = self._git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
        return {path for path in (*committed, *unstaged, *staged, *untracked) if path}

    def diff_patch(self, worktree: Path, base_sha: str) -> bytes:
        untracked_result = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if untracked_result.returncode:
            raise WorktreeError(untracked_result.stderr.decode(errors="replace").strip())
        untracked = [
            item.decode(errors="surrogateescape")
            for item in untracked_result.stdout.split(b"\0")
            if item
        ]
        if untracked:
            intent = subprocess.run(
                ["git", "-C", str(worktree), "add", "--intent-to-add", "--", *untracked],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if intent.returncode:
                raise WorktreeError(intent.stderr.strip() or intent.stdout.strip())
        result = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--binary", base_sha],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise WorktreeError(result.stderr.decode(errors="replace").strip())
        return result.stdout

    def apply_patch(self, worktree: Path, patch: Path) -> None:
        result = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--3way", str(patch)],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise WorktreeError(
                f"cannot compose worker patch {patch.name}: {result.stderr.strip() or result.stdout.strip()}"
            )


def normalize_scope(value: str) -> str:
    """Return a canonical repository-relative scope."""
    raw = value.strip().replace("\\", "/")
    if raw in {"", ".", "*"}:
        return "."
    path = PurePosixPath(raw)
    if path.is_absolute() or re.match(r"^[A-Za-z]:/", raw):
        raise ValueError(f"scope must be repository-relative: {value!r}")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if ".." in parts:
        raise ValueError(f"scope must not escape the repository: {value!r}")
    return "/".join(parts) or "."


def scopes_overlap(left: str, right: str) -> bool:
    """Return whether either repository-relative scope contains the other."""
    normalized_left = normalize_scope(left)
    normalized_right = normalize_scope(right)
    return (
        normalized_left == "."
        or normalized_right == "."
        or normalized_left == normalized_right
        or normalized_left.startswith(normalized_right + "/")
        or normalized_right.startswith(normalized_left + "/")
    )


def scope_access_conflicts(
    candidate_reads: list[str] | tuple[str, ...],
    candidate_writes: list[str] | tuple[str, ...],
    running_reads: list[str] | tuple[str, ...],
    running_writes: list[str] | tuple[str, ...],
) -> bool:
    """Apply the read/write conflict matrix for two concurrently running nodes."""
    return (
        any(scopes_overlap(left, right) for left in candidate_writes for right in running_writes)
        or any(scopes_overlap(left, right) for left in candidate_writes for right in running_reads)
        or any(scopes_overlap(left, right) for left in candidate_reads for right in running_writes)
    )


def scope_allows(path: str, allowed: list[str], forbidden: list[str]) -> bool:
    normalized = normalize_scope(path)

    def contains(scope: str) -> bool:
        clean = normalize_scope(scope)
        return clean == "." or normalized == clean or normalized.startswith(clean + "/")

    return any(contains(scope) for scope in allowed) and not any(contains(scope) for scope in forbidden)
