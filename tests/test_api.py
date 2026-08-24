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
                request = Request(
                    f"http://127.0.0.1:{port}/api/tasks/missing/control",
                    data=b'{"action":"pause"}',
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

