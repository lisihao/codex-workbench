from __future__ import annotations

import unittest

from codex_workbench.connection_evidence import (
    AUTHORITY_SERVICE_PROTOCOL,
    CONNECTION_EVIDENCE_VERSION,
    authority_observation,
    unknown_connection_evidence,
)


class ConnectionEvidenceTests(unittest.TestCase):
    def test_unknown_evidence_has_four_unknown_layers_and_null_measurements(self) -> None:
        self.assertEqual(
            unknown_connection_evidence(),
            {
                "schema_version": CONNECTION_EVIDENCE_VERSION,
                "host_catalog": {"status": "unknown"},
                "bridge": {"status": "unknown"},
                "adapter": {"status": "unknown"},
                "authority": {"status": "unknown"},
                "measurements": {
                    "restart_requests": None,
                    "recovery_duration_ms": None,
                    "duplicate_effects": None,
                    "orphan_processes": None,
                    "data_loss": None,
                },
            },
        )

    def test_complete_new_peer_is_observed(self) -> None:
        digest = "a" * 64
        self.assertEqual(
            authority_observation(
                {
                    "service_instance": "authority-instance-1",
                    "version": "1.19.4",
                    "service_protocol": AUTHORITY_SERVICE_PROTOCOL,
                    "tools_sha256": digest,
                }
            ),
            {
                "status": "observed",
                "instance_id": "authority-instance-1",
                "version": "1.19.4",
                "protocol": AUTHORITY_SERVICE_PROTOCOL,
                "capabilities_sha256": digest,
            },
        )

    def test_missing_or_invalid_authority_fields_stay_unknown(self) -> None:
        complete = {
            "service_instance": "authority-instance-1",
            "version": "1.19.4",
            "service_protocol": AUTHORITY_SERVICE_PROTOCOL,
            "tools_sha256": "a" * 64,
        }
        cases = {
            "service_instance_missing": {**complete, "service_instance": None},
            "service_instance_empty": {**complete, "service_instance": "   "},
            "service_instance_long": {**complete, "service_instance": "x" * 201},
            "service_instance_control": {**complete, "service_instance": "authority\ninstance"},
            "version_missing": {**complete, "version": None},
            "version_empty": {**complete, "version": ""},
            "version_long": {**complete, "version": "x" * 201},
            "version_control": {**complete, "version": "1.19\t4"},
            "protocol_missing": {**complete, "service_protocol": None},
            "protocol_wrong": {**complete, "service_protocol": "workbench-authority-service/v0"},
            "digest_missing": {**complete, "tools_sha256": None},
            "digest_short": {**complete, "tools_sha256": "a" * 63},
            "digest_uppercase": {**complete, "tools_sha256": "A" * 64},
            "digest_non_hex": {**complete, "tools_sha256": "g" * 64},
        }
        for name, status in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    authority_observation(status),
                    {"status": "unknown", "reason": "incomplete_authority_status"},
                )

    def test_old_legacy_status_without_protocol_is_unknown_without_install_fields(self) -> None:
        observation = authority_observation(
            {
                "ok": True,
                "version": "1.18.0",
                "service_instance": "legacy-authority",
                "tools_sha256": "a" * 64,
            }
        )
        self.assertEqual(
            observation,
            {"status": "unknown", "reason": "incomplete_authority_status"},
        )
        self.assertNotIn("installed_version", observation)
        self.assertNotIn("install_manifest", observation)

    def test_unknown_results_are_fresh_independent_dicts(self) -> None:
        first = unknown_connection_evidence()
        second = unknown_connection_evidence()
        first["authority"]["status"] = "changed"
        first["measurements"]["data_loss"] = False
        self.assertEqual(second["authority"], {"status": "unknown"})
        self.assertIsNone(second["measurements"]["data_loss"])

        first_observation = authority_observation({})
        second_observation = authority_observation({})
        first_observation["reason"] = "changed"
        self.assertEqual(
            second_observation,
            {"status": "unknown", "reason": "incomplete_authority_status"},
        )


if __name__ == "__main__":
    unittest.main()
