from __future__ import annotations

from pathlib import Path
import json
import re
import shlex
import subprocess
import tempfile
from typing import Callable


class RepositorySyncError(RuntimeError):
    pass


class RepositorySynchronizer:
    @staticmethod
    def _git(repository: Path, *args: str, timeout: int = 120) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RepositorySyncError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def sync_github(self, repository: str, remote: str, branch: str) -> dict:
        repo = Path(repository).expanduser().resolve(strict=True)
        if self._git(repo, "status", "--porcelain"):
            raise RepositorySyncError("GitHub sync requires a clean working tree")
        current_branch = self._git(repo, "branch", "--show-current")
        if current_branch != branch:
            raise RepositorySyncError(
                f"GitHub sync expected checked-out branch {branch!r}, found {current_branch!r}"
            )
        before = self._git(repo, "rev-parse", "HEAD")
        tracking_ref = f"refs/remotes/{remote}/{branch}"
        self._git(
            repo,
            "fetch",
            "--prune",
            remote,
            f"refs/heads/{branch}:{tracking_ref}",
            timeout=300,
        )
        self._git(repo, "merge", "--ff-only", tracking_ref)
        after = self._git(repo, "rev-parse", "HEAD")
        return {
            "ok": True,
            "mode": "github-primary",
            "repository": str(repo),
            "remote": remote,
            "branch": branch,
            "before": before,
            "after": after,
            "changed": before != after,
        }

    def export_increment(
        self,
        repository: str,
        base_ref: str,
        head_ref: str,
        output: Path,
    ) -> dict:
        repo = Path(repository).expanduser().resolve(strict=True)
        base_sha = self._git(repo, "rev-parse", f"{base_ref}^{{commit}}")
        head_sha = self._git(repo, "rev-parse", f"{head_ref}^{{commit}}")
        self._git(repo, "merge-base", "--is-ancestor", base_sha, head_sha)
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "-C", str(repo), "bundle", "create", str(output), f"{base_sha}..{head_ref}"],
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if result.returncode:
            raise RepositorySyncError(result.stderr.strip() or result.stdout.strip())
        return {
            "ok": True,
            "mode": "tailscale-increment-export",
            "repository": str(repo),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "bundle": str(output),
        }

    def import_increment(self, repository: str, bundle: Path, ref_name: str) -> dict:
        repo = Path(repository).expanduser().resolve(strict=True)
        bundle = bundle.expanduser().resolve(strict=True)
        safe_ref = re.sub(r"[^A-Za-z0-9._/-]+", "-", ref_name).strip("-./")
        if not safe_ref or safe_ref.startswith("-") or ".." in safe_ref.split("/"):
            raise ValueError("increment ref name is invalid")
        verification = subprocess.run(
            ["git", "-C", str(repo), "bundle", "verify", str(bundle)],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if verification.returncode:
            raise RepositorySyncError(
                verification.stderr.strip() or verification.stdout.strip()
            )
        target_ref = f"refs/workbench/increment/{safe_ref}"
        self._git(repo, "fetch", str(bundle), f"HEAD:{target_ref}", timeout=300)
        commit = self._git(repo, "rev-parse", f"{target_ref}^{{commit}}")
        return {
            "ok": True,
            "mode": "tailscale-increment-import",
            "repository": str(repo),
            "ref": target_ref,
            "commit": commit,
        }

    def send_increment(
        self,
        repository: str,
        base_ref: str,
        head_ref: str,
        *,
        host: str,
        remote_repository: str,
        ref_name: str,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> dict:
        with tempfile.TemporaryDirectory(prefix="codex-workbench-sync-") as directory:
            bundle = Path(directory) / "increment.bundle"
            exported = self.export_increment(repository, base_ref, head_ref, bundle)
            remote_executable = '"$HOME/Library/Application Support/Codex Workbench/app/bin/codex-workbench"'
            remote_command = " ".join(
                [
                    "exec",
                    remote_executable,
                    "sync",
                    "import",
                    "--repository",
                    shlex.quote(remote_repository),
                    "--bundle",
                    "-",
                    "--ref-name",
                    shlex.quote(ref_name),
                ]
            )
            result = runner(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=15",
                    host,
                    remote_command,
                ],
                input=bundle.read_bytes(),
                capture_output=True,
                timeout=300,
                check=False,
            )
            if result.returncode:
                raise RepositorySyncError(
                    result.stderr.decode(errors="replace").strip()
                    or result.stdout.decode(errors="replace").strip()
                )
            imported = json.loads(result.stdout.decode())
            if imported.get("commit") != exported["head_sha"]:
                raise RepositorySyncError("Mac mini imported commit does not match the exported increment")
            return {
                "ok": True,
                "mode": "tailscale-increment-send",
                "host": host,
                "exported": exported,
                "imported": imported,
            }
