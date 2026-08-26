from __future__ import annotations

from pathlib import Path
import plistlib
import unittest


class InstallerTests(unittest.TestCase):
    def test_macbook_heartbeat_launch_agent_is_valid_and_bounded(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (
            root / "launchd" / "com.lisihao.codex-workbench-heartbeat.plist.in"
        ).read_text()
        rendered = template.replace("__LOG_ROOT__", "/tmp/logs").replace(
            "__CLIENT_ID__", "macbook-fixture"
        )
        payload = plistlib.loads(rendered.encode())
        self.assertEqual(payload["StartInterval"], 300)
        self.assertNotIn("KeepAlive", payload)
        self.assertIn("client heartbeat", payload["ProgramArguments"][-1])


if __name__ == "__main__":
    unittest.main()
