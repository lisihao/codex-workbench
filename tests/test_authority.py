from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_workbench.authority import (
    CoordinatorAuthorityError,
    CoordinatorAuthorityLease,
    authority_machine_id,
)


class AuthorityTests(unittest.TestCase):
    def test_macos_machine_id_is_stable_and_injectable(self) -> None:
        def runner(_command: list[str]) -> tuple[int, str]:
            return 0, '    "IOPlatformUUID" = "ABCDEF01-2345-6789-ABCD-EF0123456789"\n'

        self.assertEqual(
            authority_machine_id(platform_name="darwin", runner=runner),
            "darwin:ioplatformuuid:abcdef01-2345-6789-abcd-ef0123456789",
        )

    def test_missing_platform_uuid_fails_closed(self) -> None:
        with self.assertRaisesRegex(CoordinatorAuthorityError, "unavailable"):
            authority_machine_id(
                platform_name="darwin", runner=lambda _command: (0, "no uuid")
            )

    def test_only_one_coordinator_holds_the_authority_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "coordinator.lock"
            first = CoordinatorAuthorityLease(lock)
            with first as identity:
                self.assertEqual(identity.pid, first.identity.pid)
                with self.assertRaisesRegex(CoordinatorAuthorityError, "already held"):
                    with CoordinatorAuthorityLease(lock):
                        self.fail("a second coordinator acquired the same authority")

            with CoordinatorAuthorityLease(lock) as replacement:
                self.assertNotEqual(replacement.instance_id, identity.instance_id)


if __name__ == "__main__":
    unittest.main()
