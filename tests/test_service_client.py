from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from codex_workbench.service_client import (
    AuthorityHTTPClient, IndeterminateServiceRequest, ServiceHTTPError,
)


class ServiceClientTests(unittest.TestCase):
    def setUp(self) -> None:
        owner = self
        self.posts = 0
        self.reads = 0
        self.mode = "lost-response"
        self.result = {"content": [{"type": "text", "text": "fixture completed"}]}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, body, status=200):
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                owner.posts += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Authorization") != "Bearer fixture-control-token":
                    return self._json({}, 401)
                if owner.mode in {"lost-response", "missing", "unknown"}:
                    self.close_connection = True
                    return
                if owner.mode == "readonly-retry" and owner.posts == 1:
                    return self._json({}, 503)
                return self._json({"state": "completed", "result": owner.result})

            def do_GET(self):
                owner.reads += 1
                if owner.mode == "missing":
                    return self._json({}, 404)
                if owner.mode == "unknown":
                    return self._json({"request_id": "request-1", "state": "unknown"})
                return self._json({"request_id": "request-1", "state": "completed", "result": owner.result})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = AuthorityHTTPClient(self.url, "fixture-control-token", backoff_seconds=0)
        self.envelope = {"request_id": "request-1", "tool": "workbench_control_task",
                         "arguments": {"task_id": "fixture", "action": "pause", "expected_revision": 1}}

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_lost_write_response_queries_receipt_without_resending(self):
        receipt = self.client.dispatch(self.envelope)
        self.assertEqual(receipt["result"], self.result)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.reads, 1)

    def test_unknown_or_missing_receipt_never_replays_write(self):
        for mode in ("unknown", "missing"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.posts = self.reads = 0
                with self.assertRaises(IndeterminateServiceRequest) as caught:
                    self.client.dispatch(self.envelope)
                self.assertEqual(caught.exception.request_id, "request-1")
                self.assertEqual(self.posts, 1)
                self.assertEqual(self.reads, 1)

    def test_readonly_requests_can_retry_boundedly(self):
        self.mode = "readonly-retry"
        receipt = self.client.dispatch({"tool": "workbench_list_tasks", "arguments": {}}, read_only=True)
        self.assertEqual(receipt["result"], self.result)
        self.assertEqual(self.posts, 2)
        self.assertEqual(self.reads, 0)

    def test_rejected_auth_is_not_retried_or_reported_as_success(self):
        client = AuthorityHTTPClient(self.url, "wrong-token", backoff_seconds=0)
        with self.assertRaises(ServiceHTTPError) as caught:
            client.dispatch(self.envelope)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.reads, 0)
        self.assertNotIn("wrong-token", str(caught.exception))

    def test_nonlocal_token_destinations_are_rejected(self):
        for url in ("http://example.invalid", "http://user:password@127.0.0.1", "file:///tmp/control"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                AuthorityHTTPClient(url, "fixture-control-token")
