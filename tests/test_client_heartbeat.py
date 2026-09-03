from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workbench-client-heartbeat.py"
SPEC = importlib.util.spec_from_file_location("workbench_client_heartbeat", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
heartbeat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(heartbeat)


class ClientHeartbeatTests(unittest.TestCase):
    def test_location_evidence_is_forwarded_over_the_same_location_proxy(self) -> None:
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            if "--select" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "route": "lan",
                            "reason": "home_network_lan_probe_ok",
                            "observed_at": "2026-09-03T12:00:00Z",
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(heartbeat.subprocess, "run", side_effect=run):
            self.assertEqual(
                heartbeat.main(
                    [
                        "workbench-client-heartbeat.py",
                        "--location-proxy",
                        "/client/workbench-location-proxy",
                        "--transport-config",
                        "/client/transport.json",
                        "--authority",
                        "macmini",
                        "--authority-state-root",
                        "~/Library/Application Support/Codex Workbench",
                        "--client-id",
                        "macbook-fixture",
                    ][1:]
                ),
                0,
            )

        ssh = calls[1]
        self.assertIn(
            "ProxyCommand=/client/workbench-location-proxy --config /client/transport.json",
            ssh,
        )
        self.assertEqual(ssh[-2], "macmini")
        self.assertIn("--route lan", ssh[-1])
        self.assertIn("--reason home_network_lan_probe_ok", ssh[-1])
        self.assertIn("--observed-at 2026-09-03T12:00:00Z", ssh[-1])


if __name__ == "__main__":
    unittest.main()
