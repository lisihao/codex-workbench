from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.config import WorkbenchConfig
from codex_workbench.model import NodeResult, NodeSpec, QuotaSnapshot, TaskContract
from codex_workbench.store import WorkbenchStore


class APITests(unittest.TestCase):
    def test_snapshot_is_readable_and_control_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = WorkbenchConfig(root, host="127.0.0.1", port=0)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            server = WorkbenchHTTPServer(config, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)
                self.assertTrue(snapshot["health"]["ok"])
                self.assertFalse(snapshot["authenticated"])
                self.assertIsNone(snapshot["build"])
                self.assertIsNone(snapshot["quota_policy"])
                self.assertEqual(len(snapshot["acceptance"]["checks"]), 12)
                with urlopen(f"http://127.0.0.1:{port}/api/acceptance", timeout=2) as response:
                    acceptance = json.load(response)
                self.assertFalse(acceptance["complete"])
                request = Request(
                    f"http://127.0.0.1:{port}/api/tasks/missing/control",
                    data=b'{"action":"pause"}',
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()

                store.write_quota(
                    QuotaSnapshot(
                        observed_at="2026-08-26T00:00:00+00:00",
                        auth_ok=True,
                        auth_method="native-subscription",
                        five_hour_remaining=35,
                        weekly_all_remaining=60,
                        weekly_sonnet_remaining=60,
                        source="settings-usage",
                    )
                )
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)
                self.assertEqual(snapshot["quota_policy"]["zone"], "mixed")
                self.assertEqual(snapshot["quota_policy"]["zones"]["sonnet"], "yellow")
                self.assertEqual(snapshot["quota_policy"]["models"]["sonnet"]["max_concurrency"], 1)

                observation = Request(
                    f"http://127.0.0.1:{port}/api/clients/observe",
                    data=json.dumps(
                        {"client_id": "phone-fixture", "snapshot_cursor": snapshot["health"]["cursor"]}
                    ).encode(),
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {config.token()}",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (iPhone; Mobile)",
                    },
                )
                with urlopen(observation, timeout=2) as response:
                    observation_receipt = json.load(response)
                self.assertEqual(observation_receipt["device_class"], "phone")
                with urlopen(f"http://127.0.0.1:{port}/api/acceptance", timeout=2) as response:
                    acceptance = json.load(response)
                checks = {check["id"]: check for check in acceptance["checks"]}
                self.assertEqual(checks["A2"]["status"], "ok")

                contract = TaskContract(
                    task_id="phone-approval",
                    repository=str(root),
                    base_sha="fixture",
                    objective="approve an indeterminate node from the phone cockpit",
                    allowed_scope=("tests",),
                )
                store.create_task(
                    contract,
                    [NodeSpec("work", contract.task_id, "work", "fixture", "fixture", "ok")],
                    "phone-approval-create",
                )
                store.queue_task(contract.task_id)
                store.claim_ready_node("worker")
                store.settle_node(
                    contract.task_id,
                    "work",
                    NodeResult("indeterminate", "fixture outcome unknown"),
                )
                with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    snapshot = json.load(response)
                approval = snapshot["approvals"][0]
                decision = Request(
                    f"http://127.0.0.1:{port}/api/approvals/{approval['approval_id']}/decide",
                    data=json.dumps(
                        {
                            "decision": "retry",
                            "expected_revision": approval["task_revision"],
                        }
                    ).encode(),
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {config.token()}",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(decision, timeout=2) as response:
                    receipt = json.load(response)
                self.assertTrue(receipt["ok"])
                self.assertEqual(store.get_task(contract.task_id)["state"], "queued")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
