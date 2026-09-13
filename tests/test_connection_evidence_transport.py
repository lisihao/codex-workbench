"""Connection observations do not infer loaded peers from installed versions."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_workbench import __version__
from codex_workbench.connection_evidence import unknown_connection_evidence
from codex_workbench.service_mcp import AuthorityMCPAdapter

spec = importlib.util.spec_from_file_location(
    "connection_evidence_bridge", Path(__file__).resolve().parents[1] / "scripts/workbench-mcp-bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


def result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class ConnectionEvidenceTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def connection(self):
        return bridge.MCPConnectionBridge(bridge.validate_config({
            "schema_version": 1, "command": [sys.executable, "-c", "pass"],
            "state_file": str(Path(self.temp.name) / "state.json"),
        }), io.StringIO())

    def test_adapter_reports_loaded_version_and_does_not_guess_legacy_authority(self):
        adapter = AuthorityMCPAdapter(Mock())
        receipt = adapter._health_connection_evidence(result({"ok": True}))
        evidence = json.loads(receipt["content"][0]["text"])["connection_evidence"]
        self.assertEqual(evidence["adapter"]["version"], __version__)
        self.assertEqual(evidence["authority"], {"status": "unknown"})
        self.assertEqual(evidence["bridge"], {"status": "unknown"})
        self.assertTrue(all(value is None for value in evidence["measurements"].values()))

    def test_same_adapter_observes_changed_authority_without_replacing_identity(self):
        adapter = AuthorityMCPAdapter(Mock())
        ids = []
        for version in ("old-peer", "new-peer"):
            evidence = unknown_connection_evidence()
            evidence["authority"] = {"status": "observed", "version": version}
            receipt = adapter._health_connection_evidence(result({"connection_evidence": evidence}))
            observed = json.loads(receipt["content"][0]["text"])["connection_evidence"]
            self.assertEqual(observed["authority"]["version"], version)
            self.assertEqual(observed["adapter"]["version"], __version__)
            ids.append(observed["adapter"]["instance_id"])
        self.assertEqual(ids[0], ids[1])

    def test_bridge_health_call_does_not_claim_ui_catalog_refresh(self):
        connection = self.connection()
        connection._handle_tool_call = Mock(return_value={"id": 1, "result": result({"ok": True})})
        with patch.object(Path, "read_bytes", side_effect=AssertionError("no per-call source read")):
            response = connection.handle({"id": 1, "method": "tools/call", "params": {
                "name": "workbench_harness_health", "arguments": {},
            }})
        evidence = json.loads(response["result"]["content"][0]["text"])["connection_evidence"]
        self.assertEqual(evidence["bridge"]["source_sha256_at_import"], bridge.SOURCE_SHA256_AT_IMPORT)
        self.assertIsNone(evidence["bridge"]["installed_version"])
        self.assertIsNone(evidence["host_catalog"]["last_host_list_sha256"])
        self.assertEqual(evidence["host_catalog"]["ui_tool_availability"], "unknown")
        self.assertFalse(connection.config.state_file.exists())

    def test_internal_catalog_refresh_is_not_a_host_list_request(self):
        connection = self.connection()
        catalog = {"result": {"tools": []}}
        connection._observe_catalog(catalog, reconnect=False, host_requested=False)
        self.assertIsNone(connection._host_catalog_digest)
        connection._observe_catalog(catalog, reconnect=False, host_requested=True)
        self.assertEqual(connection._host_catalog_digest, connection.tools_digest)
        identity = connection._instance_id
        connection._observe_catalog({"result": {"tools": [{"name": "new"}]}}, reconnect=True, host_requested=False)
        self.assertEqual(connection._instance_id, identity)
        self.assertNotEqual(connection._host_catalog_digest, connection.tools_digest)
        self.assertTrue(connection.catalog_refresh_required)

    def test_failed_health_is_not_replaced_by_synthetic_success(self):
        failure = {"isError": True, "content": [{"type": "text", "text": "unavailable"}]}
        adapter = AuthorityMCPAdapter(Mock())
        self.assertIs(adapter._health_connection_evidence(failure), failure)
        response = {"id": 1, "result": failure}
        self.assertIs(self.connection()._health_connection_evidence(response), response)

    def test_future_evidence_schema_is_rejected_without_state_write(self):
        response = self.connection()._health_connection_evidence({
            "id": 1, "result": result({"connection_evidence": {"schema_version": "future"}}),
        })
        self.assertTrue(response["result"]["isError"])
        self.assertFalse((Path(self.temp.name) / "state.json").exists())
