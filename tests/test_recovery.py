from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from codex_workbench.model import NodeResult, NodeSpec, TaskContract
from codex_workbench.recovery import RecoveryPolicy, WorktreeRecoveryManager, _hash_file
from codex_workbench.store import WorkbenchStore
from codex_workbench.worktrees import WorktreeManager


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()


class WorktreeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repository, check=True)
        (self.repository / "tracked.txt").write_text("base\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repository, check=True, capture_output=True)
        self.base_sha = git(self.repository, "rev-parse", "HEAD")
        self.state = self.root / "state"
        self.store = WorkbenchStore(self.state / "state.sqlite")
        self.store.initialize()
        self.epoch = self.store.activate_coordinator("recovery-test", "fixture-machine")
        self.worktrees = WorktreeManager(self.state / "worktrees")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def policy(
        self,
        *,
        archive_root: Path | None = None,
        remote_archive_host: str | None = None,
    ) -> RecoveryPolicy:
        return RecoveryPolicy(
            state_root=self.state,
            enabled=True,
            recycle_root=self.state / "recycle" / "worktrees",
            archive_root=archive_root,
            restore_root=self.state / "restored-worktrees",
            outgoing_root=self.state / "recycle" / "outgoing",
            sweep_interval_seconds=60,
            home_presence_ttl_seconds=600,
            retry_backoff_seconds=900,
            compression="gzip",
            zstd_binary=None,
            require_smb=False,
            remote_archive_host=remote_archive_host,
            remote_state_root="~/Library/Application Support/Codex Workbench",
        )

    def accepted_allocation(self) -> tuple[str, Path]:
        contract = TaskContract(
            task_id="recovery-task",
            repository=str(self.repository),
            base_sha=self.base_sha,
            objective="exercise worktree recovery",
            allowed_scope=(".",),
            executor_model="fixture",
            verifier_model="fixture",
        )
        nodes = [
            NodeSpec("worker", contract.task_id, "worker", "fixture", "fixture", "ok"),
            NodeSpec(
                "verify",
                contract.task_id,
                "verify",
                "fixture",
                "fixture",
                "accepted",
                depends_on=("worker",),
                verifier=True,
            ),
        ]
        self.store.create_task(contract, nodes, "recovery-create")
        self.store.queue_task(contract.task_id)
        claimed = self.store.claim_ready_node("fixture-worker", self.epoch)
        assert claimed is not None
        worktree = self.worktrees.prepare(
            str(self.repository),
            self.base_sha,
            contract.task_id,
            "worker",
            int(claimed["attempt"]),
        )
        self.store.assign_worktree(
            contract.task_id,
            "worker",
            str(worktree),
            attempt=int(claimed["attempt"]),
            coordinator_epoch=self.epoch,
            lease_epoch=int(claimed["lease_epoch"]),
        )
        (worktree / "tracked.txt").write_text("changed\n")
        (worktree / "untracked.txt").write_text("recover me\n")
        evidence_ref = self.store.artifacts.put_text("attempt evidence\n", "log")
        self.store.settle_claimed(
            claimed,
            NodeResult(
                "succeeded",
                "worker accepted",
                artifacts={"attempt_log": evidence_ref},
                evidence=(evidence_ref,),
                actual_model="fixture",
                result_kind="worker",
                checks=("fixture",),
            ),
        )
        verifier = self.store.claim_ready_node("fixture-verifier", self.epoch)
        assert verifier is not None
        self.store.settle_claimed(
            verifier,
            NodeResult(
                "succeeded",
                "accepted",
                actual_model="fixture",
                result_kind="verifier",
                checks=("fixture",),
                verdict="accepted",
            ),
        )
        allocation = self.store.list_worktree_allocations()[0]
        return str(allocation["allocation_id"]), worktree

    def home_heartbeat(self) -> None:
        observed = datetime.now(UTC).isoformat(timespec="seconds")
        self.store.record_client_heartbeat(
            "macbook-fixture",
            "macbook",
            route="lan",
            reason="home_network_lan_probe_ok",
            observed_at=observed,
        )

    def location_profile(self) -> Path:
        home = self.root / "macbook-home"
        client = home / "Library" / "Application Support" / "Codex Workbench Client"
        proxy = client / "bin" / "workbench-location-proxy"
        proxy.parent.mkdir(parents=True, exist_ok=True)
        proxy.write_text("#!/bin/sh\n")
        (client / "transport.json").write_text('{"schema_version":1}\n')
        return home

    def test_away_sweep_moves_to_recycle_without_archiving_or_deleting(self) -> None:
        allocation_id, original = self.accepted_allocation()
        manager = WorktreeRecoveryManager(self.store, self.policy(archive_root=self.root / "nas"))

        result = manager.sweep()

        allocation = self.store.get_worktree_allocation(allocation_id)
        self.assertEqual(result["processed"][0]["action"], "quarantined")
        self.assertEqual(allocation["state"], "quarantined")
        self.assertFalse(original.exists())
        self.assertTrue(Path(allocation["current_path"]).is_dir())
        self.assertEqual(self.store.list_worktree_archives(), [])

    def test_tailscale_heartbeat_is_not_home_presence(self) -> None:
        self.store.record_client_heartbeat(
            "macbook-away",
            "macbook",
            route="tailscale",
            reason="non_home_network",
            observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.assertIsNone(self.store.active_home_presence())

    def test_expired_home_lease_fails_closed(self) -> None:
        self.home_heartbeat()
        self.assertIsNotNone(self.store.active_home_presence())
        self.assertIsNone(
            self.store.active_home_presence(at=datetime.now(UTC) + timedelta(minutes=11))
        )

    def test_home_sweep_archives_restores_then_purges_and_can_restore_later(self) -> None:
        allocation_id, original = self.accepted_allocation()
        nas = self.root / "nas"
        nas.mkdir()
        manager = WorktreeRecoveryManager(self.store, self.policy(archive_root=nas))
        self.home_heartbeat()

        result = manager.sweep()

        allocation = self.store.get_worktree_allocation(allocation_id)
        self.assertEqual(allocation["state"], "purged")
        self.assertFalse(original.exists())
        archive = self.store.list_worktree_archives()[0]
        self.assertEqual(archive["state"], "verified")
        self.assertIsNotNone(archive["purged_at"])
        archive_path = Path(archive["archive_path"])
        self.assertTrue(archive_path.is_file())
        self.assertTrue(Path(str(archive_path) + ".sha256").is_file())
        self.assertEqual(_hash_file(archive_path), archive["archive_sha256"])
        supporting = archive["manifest"]["supporting_artifacts"]
        self.assertEqual(list(supporting), [self.store.artifacts.put_text("attempt evidence\n", "log")])
        self.assertEqual(result["processed"][-1]["action"], "archived")

        restored = manager.restore(str(archive["archive_id"]))
        restored_path = Path(restored["destination"])
        self.assertEqual((restored_path / "tracked.txt").read_text(), "changed\n")
        self.assertEqual((restored_path / "untracked.txt").read_text(), "recover me\n")
        self.assertEqual(git(restored_path, "rev-parse", "HEAD"), self.base_sha)

    def test_special_files_are_omitted_without_blocking_recovery(self) -> None:
        allocation_id, _ = self.accepted_allocation()
        manager = WorktreeRecoveryManager(self.store, self.policy())
        allocation = manager.quarantine(allocation_id)
        shortcut = Path("/tmp") / f"cwbs-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        shortcut.symlink_to(Path(allocation["current_path"]), target_is_directory=True)
        try:
            with socket.socket(socket.AF_UNIX) as fixture_socket:
                fixture_socket.bind(str(shortcut / "runtime.sock"))
                output = self.root / "special.tar.gz"
                manifest = manager.create_capsule(allocation, "wta-special-fixture", output)
        finally:
            shortcut.unlink(missing_ok=True)

        self.assertIn("runtime.sock", manifest["omitted_special_files"])
        verified = manager.verify_capsule(output, "wta-special-fixture")
        self.assertEqual(verified, manifest)

    def test_remote_ingest_verifies_full_capsule_before_nas_receipt(self) -> None:
        allocation_id, _ = self.accepted_allocation()
        source_manager = WorktreeRecoveryManager(self.store, self.policy())
        allocation = source_manager.quarantine(allocation_id)
        archive_id = "wta-remote-fixture"
        outgoing = self.root / "outgoing.tar.gz"
        source_manager.create_capsule(allocation, archive_id, outgoing)
        digest = _hash_file(outgoing)

        remote_root = self.root / "remote-state"
        remote_store = WorkbenchStore(remote_root / "state.sqlite")
        remote_store.initialize()
        remote_policy = RecoveryPolicy(
            **{
                **self.policy(archive_root=self.root / "remote-nas").__dict__,
                "state_root": remote_root,
                "recycle_root": remote_root / "recycle" / "worktrees",
                "restore_root": remote_root / "restored-worktrees",
                "outgoing_root": remote_root / "recycle" / "outgoing",
            }
        )
        (self.root / "remote-nas").mkdir()
        remote_manager = WorktreeRecoveryManager(remote_store, remote_policy)

        receipt = remote_manager.ingest_remote(
            io.BytesIO(outgoing.read_bytes()),
            archive_id=archive_id,
            expected_sha256=digest,
            transport="tailscale",
            compression="gzip",
        )

        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(receipt["archive_sha256"], digest)
        self.assertTrue(Path(receipt["archive_path"]).is_file())
        retried = remote_manager.ingest_remote(
            io.BytesIO(outgoing.read_bytes()),
            archive_id=archive_id,
            expected_sha256=digest,
            transport="tailscale",
            compression="gzip",
        )
        self.assertEqual(retried["state"], "verified")
        self.assertEqual(len(remote_store.list_worktree_archives()), 1)

    def test_capsule_rejects_a_symlink_that_would_escape_the_restored_worktree(self) -> None:
        allocation_id, _ = self.accepted_allocation()
        manager = WorktreeRecoveryManager(self.store, self.policy())
        allocation = manager.quarantine(allocation_id)
        (Path(allocation["current_path"]) / "escape").symlink_to("../outside")
        output = self.root / "escaping.tar.gz"

        manager.create_capsule(allocation, "wta-escaping-fixture", output)

        with self.assertRaisesRegex(Exception, "symlink escapes recovery root"):
            manager.verify_capsule(output, "wta-escaping-fixture")

    def test_remote_send_requires_matching_verified_receipt_before_purge(self) -> None:
        allocation_id, _ = self.accepted_allocation()
        manager = WorktreeRecoveryManager(
            self.store,
            self.policy(remote_archive_host="macmini"),
        )

        def runner(command, **kwargs):
            archive = kwargs["stdin"].read()
            proxy_options = [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "-o"
            ]
            self.assertTrue(
                any(
                    option.startswith("ProxyCommand=") and "--force-tailscale" in option
                    for option in proxy_options
                )
            )
            self.assertIn("HostKeyAlias=codex-workbench-authority", proxy_options)
            remote_command = command[-1]
            archive_id = remote_command.split("--archive-id ", 1)[1].split(" ", 1)[0]
            digest = remote_command.split("--sha256 ", 1)[1].split(" ", 1)[0]
            self.assertEqual(digest, __import__("hashlib").sha256(archive).hexdigest())
            response = {
                "archive_id": archive_id,
                "state": "verified",
                "archive_sha256": digest,
                "archive_path": f"/nas/{archive_id}.tar.gz",
                "size_bytes": len(archive),
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        with patch("pathlib.Path.home", return_value=self.location_profile()):
            manager.runner = runner
            receipt = manager.send_allocation(allocation_id)

        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(receipt["transport"], "remote-tailscale-forced")
        self.assertEqual(receipt["transport_profile"], "tailscale-forced")
        self.assertEqual(self.store.get_worktree_allocation(allocation_id)["state"], "purged")

    def test_remote_send_mismatch_keeps_quarantined_source(self) -> None:
        allocation_id, _ = self.accepted_allocation()
        manager = WorktreeRecoveryManager(
            self.store,
            self.policy(remote_archive_host="macmini"),
        )

        attempts: list[list[str]] = []

        def runner(command, **kwargs):
            attempts.append(command)
            remote_command = command[-1]
            archive_id = remote_command.split("--archive-id ", 1)[1].split(" ", 1)[0]
            response = {
                "archive_id": archive_id,
                "state": "verified",
                "archive_sha256": "0" * 64,
                "archive_path": f"/nas/{archive_id}.tar.gz",
                "size_bytes": 1,
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        with patch("pathlib.Path.home", return_value=self.location_profile()):
            manager.runner = runner
            with self.assertRaisesRegex(Exception, "matching verified NAS receipt"):
                manager.send_allocation(allocation_id)

        allocation = self.store.get_worktree_allocation(allocation_id)
        self.assertEqual(allocation["state"], "archive_failed")
        self.assertTrue(Path(allocation["current_path"]).is_dir())
        self.assertEqual(manager.sweep()["status"], "idle")
        self.assertEqual(len(attempts), 1)

    def test_remote_ingest_checksum_failure_never_creates_final_archive(self) -> None:
        remote_root = self.root / "checksum-state"
        remote_store = WorkbenchStore(remote_root / "state.sqlite")
        remote_store.initialize()
        nas = self.root / "checksum-nas"
        nas.mkdir()
        base = self.policy(archive_root=nas)
        policy = RecoveryPolicy(
            **{
                **base.__dict__,
                "state_root": remote_root,
                "recycle_root": remote_root / "recycle" / "worktrees",
                "restore_root": remote_root / "restored-worktrees",
                "outgoing_root": remote_root / "recycle" / "outgoing",
            }
        )
        manager = WorktreeRecoveryManager(remote_store, policy)

        with self.assertRaisesRegex(Exception, "checksum"):
            manager.ingest_remote(
                io.BytesIO(b"not-an-archive"),
                archive_id="wta-bad-checksum",
                expected_sha256="f" * 64,
                transport="tailscale",
                compression="gzip",
            )

        receipt = remote_store.get_worktree_archive("wta-bad-checksum")
        self.assertEqual(receipt["state"], "failed")
        self.assertFalse(any(nas.rglob("*.tar.gz")))


if __name__ == "__main__":
    unittest.main()
