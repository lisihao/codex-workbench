from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from codex_workbench.api import WorkbenchHTTPServer
from codex_workbench.config import WorkbenchConfig
from codex_workbench.performance import (
    LEGACY_PERFORMANCE_SNAPSHOT_SOURCE,
    LEGACY_PERFORMANCE_SEMANTIC_VERSION,
    PERFORMANCE_SEMANTIC_VERSION,
    PerformanceRegistry,
    canonical_hash,
    validate_performance_snapshot,
)
from codex_workbench.store import WorkbenchStore


_CATALOG = {
    "catalog_id": "catalog-api-semantics",
    "digest": "a" * 64,
    "models": [],
    "agents": {},
}


@contextmanager
def _running_server(root: Path):
    config = WorkbenchConfig(root, host="127.0.0.1", port=0)
    config.initialize()
    store = WorkbenchStore(config.database)
    store.initialize()
    server = WorkbenchHTTPServer(config, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield config, store, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _legacy_audit_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    body_keys = (
        "schema_version",
        "producer",
        "source",
        "event_cursor",
        "catalog",
        "baseline",
        "ledger",
        "metrics",
        "pools",
        "source_provenance",
        "advisory_policy",
    )
    legacy_body = {key: snapshot[key] for key in body_keys}
    legacy_body["source"] = LEGACY_PERFORMANCE_SNAPSHOT_SOURCE
    digest = canonical_hash(legacy_body)
    return validate_performance_snapshot(
        {
            **legacy_body,
            "snapshot_id": f"performance-{digest[:16]}",
            "digest": digest,
        }
    )


def _get_performance(base_url: str) -> dict[str, object]:
    with urlopen(f"{base_url}/api/performance", timeout=2) as response:
        return json.load(response)


class PerformanceSemanticsAPITests(unittest.TestCase):
    def test_v2_snapshot_exposes_semantics_and_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="performance-api-v2-") as directory:
            root = Path(directory)
            config = WorkbenchConfig(root)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            refreshed = PerformanceRegistry(config.state_root).refresh(store, _CATALOG)

            with _running_server(root) as (_, _, base_url):
                payload = _get_performance(base_url)

            active = payload["active"]
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["read_ok"])
            self.assertTrue(payload["calibration_usable"])
            self.assertTrue(payload["calibration_eligible"])
            self.assertTrue(payload["routing_eligible"])
            self.assertEqual(active["snapshot_id"], refreshed["active_generation_id"])
            self.assertEqual(active["semantic_version"], PERFORMANCE_SEMANTIC_VERSION)
            self.assertEqual(active["calibration_compatibility"], "v2")
            self.assertEqual(active["calibration_policy"], refreshed["snapshot"]["calibration_policy"])
            self.assertEqual(active["public_evidence"], refreshed["snapshot"]["public_evidence"])
            self.assertTrue(active["calibration_usable"])
            self.assertTrue(active["routing_eligible"])

    def test_legacy_audit_snapshot_is_readable_but_not_calibration_or_routing_eligible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="performance-api-legacy-") as directory:
            root = Path(directory)
            config = WorkbenchConfig(root)
            config.initialize()
            store = WorkbenchStore(config.database)
            store.initialize()
            registry = PerformanceRegistry(config.state_root)
            refreshed = registry.refresh(store, _CATALOG)
            legacy = _legacy_audit_snapshot(refreshed["snapshot"])
            registry._write_generation(legacy)
            registry._activate(legacy["snapshot_id"])

            with _running_server(root) as (_, _, base_url):
                payload = _get_performance(base_url)

            active = payload["active"]
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["read_ok"])
            self.assertFalse(payload["calibration_usable"])
            self.assertFalse(payload["calibration_eligible"])
            self.assertFalse(payload["routing_eligible"])
            self.assertEqual(active["semantic_version"], LEGACY_PERFORMANCE_SEMANTIC_VERSION)
            self.assertEqual(active["calibration_compatibility"], "legacy-audit-only")
            self.assertIsNone(active["calibration_policy"])
            self.assertEqual(active["public_evidence"], [])
            self.assertFalse(active["calibration_usable"])
            self.assertFalse(active["routing_eligible"])


if __name__ == "__main__":
    unittest.main()
