from __future__ import annotations

import subprocess
import unittest

from codex_workbench.artifacts import ArtifactStore
from codex_workbench.delivery import GitHubDelivery, GitHubDeliveryRequest
from tests import test_delivery as delivery_fixtures


class DeliveryResumeAcceptanceTests(unittest.TestCase):
    setUp = delivery_fixtures.DeliveryTests.setUp
    tearDown = delivery_fixtures.DeliveryTests.tearDown
    create_accepted_task = delivery_fixtures.DeliveryTests.create_accepted_task

    def test_authorization_repaired_after_push_resumes_without_another_push(self):
        self.create_accepted_task(external_write=True)
        authenticated = False
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] == "gh":
                if not authenticated:
                    return subprocess.CompletedProcess(command, 1, "", "HTTP 401: Requires authentication")
                if command[1:3] == ["pr", "view"]:
                    return subprocess.CompletedProcess(command, 0, "https://example.invalid/pr/1\n", "")
                if command[1:3] == ["pr", "checks"]:
                    return subprocess.CompletedProcess(command, 0, "[{\"bucket\":\"pass\",\"name\":\"fixture\",\"state\":\"SUCCESS\"}]\n", "")
                raise AssertionError(f"unexpected external operation: {command}")
            self.assertEqual(command[0], "git")
            return subprocess.run(command, **kwargs)

        delivery = GitHubDelivery(self.store, ArtifactStore(self.root / "artifacts"), runner=runner)
        request = GitHubDeliveryRequest("delivery-task", "resume-after-auth", "main")
        failed = delivery.deliver(request)
        self.assertIn(failed["state"], {"failed", "indeterminate"})
        self.assertIn("401", failed["details"]["error"])
        commit = failed["details"]["commit"]
        authenticated = True
        resumed = delivery.deliver(request)
        self.assertEqual(resumed["state"], "ci_passed", "existing authorized delivery never resumed after credentials became available")
        self.assertEqual(resumed["details"]["commit"], commit)
        self.assertEqual(sum("push" in command for command in calls), 1)
        self.assertEqual(sum(command[0] == "gh" and command[1:3] == ["pr", "create"] for command in calls), 0)


if __name__ == "__main__":
    unittest.main()
