from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_workbench.authority import CoordinatorAuthorityError, CoordinatorAuthorityLease


class AuthorityTests(unittest.TestCase):
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
