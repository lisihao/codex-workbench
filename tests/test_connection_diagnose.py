from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workbench-connection-diagnose.py"
SPEC = importlib.util.spec_from_file_location("workbench_connection_diagnose", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
diagnose = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnose)


REMOTE_BINARY = "$HOME/Library/Application Support/Codex Workbench/app/bin/codex-workbench"
SSH_COMMAND = [
    "/usr/bin/ssh",
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "macmini",
    f'exec "{REMOTE_BINARY}" mcp',
]


def write_fixture(
    root: Path,
    *,
    state: dict[str, object] | None = None,
    command: list[str] | None = None,
) -> Path:
    state_path = root / "private" / "bridge-state.json"
    if state is not None:
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    config_path = root / "private" / "diagnostic-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "command": command or SSH_COMMAND,
                "state_file": str(state_path),
                "diagnostic_timeout_seconds": 0.1,
            }
        ),
        encoding="utf-8",
    )
    return config_path


class ConnectionDiagnoseTests(unittest.TestCase):
    def test_replaces_only_recognised_ssh_and_local_mcp_commands(self) -> None:
        replacement, transport = diagnose.replace_mcp_with_service_status(SSH_COMMAND)

        self.assertEqual(transport, "ssh")
        self.assertEqual(
            replacement[-1],
            f'exec "{REMOTE_BINARY}" service status',
        )
        local, local_transport = diagnose.replace_mcp_with_service_status(
            ["/opt/codex-workbench", "mcp"]
        )
        self.assertEqual(local, ["/opt/codex-workbench", "service", "status"])
        self.assertEqual(local_transport, "local")
        with self.assertRaises(diagnose.DiagnosticConfigError):
            diagnose.replace_mcp_with_service_status(["/usr/bin/ssh", "macmini", "shell"])

    def test_timeout_is_unknown_and_probe_is_bounded_to_two_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory))
            timeout = subprocess.TimeoutExpired(SSH_COMMAND, 0.1, stderr="connect token=secret")
            with (
                mock.patch.object(diagnose.subprocess, "run", side_effect=[timeout, timeout]) as run,
                mock.patch.object(diagnose.time, "sleep") as sleep,
            ):
                result = diagnose.diagnose(config_path)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(result["fault_key"], "transport-timeout")
        self.assertEqual(result["components"]["ssh_or_local_process"]["status"], "unknown")
        self.assertEqual(result["components"]["authority_http"]["status"], "unknown")
        self.assertEqual(result["probe"]["attempts"], 2)
        self.assertEqual(
            run.call_args.args[0][-1],
            f'exec "{REMOTE_BINARY}" service status',
        )
        self.assertNotIn("secret", json.dumps(result))

    def test_successful_ssh_with_unhealthy_json_keeps_layers_separate(self) -> None:
        state = {"components": {"mcp_child": {"exit_code": 0, "stderr": ""}}}
        response = subprocess.CompletedProcess(
            SSH_COMMAND,
            0,
            json.dumps(
                {
                    "ok": False,
                    "version": "1.2.3",
                    "build": "fixture-build",
                    "service_instance": "fixture-service",
                    "http_status": 503,
                }
            ),
            "",
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory), state=state)
            with mock.patch.object(diagnose.subprocess, "run", return_value=response) as run:
                result = diagnose.diagnose(config_path)

        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["components"]["ssh_or_local_process"]["status"], "ready")
        self.assertEqual(result["components"]["authority_http"]["status"], "error")
        self.assertEqual(
            result["components"]["authority_http"]["summary"],
            "authority_http_unhealthy",
        )
        self.assertEqual(result["components"]["authority_http"]["http_status"], 503)
        self.assertEqual(result["components"]["mcp_child"]["status"], "ready")
        self.assertEqual(result["fault_key"], "authority-http-unhealthy")
        self.assertFalse(result["ok"])

    def test_healthy_authority_does_not_hide_nonzero_mcp_child_or_stderr(self) -> None:
        state = {
            "layers": {
                "mcp_child": {
                    "exit_code": 7,
                    "stderr": (
                        "child token=secret alice@example.com "
                        "https://authority.invalid/private /Users/alice/private "
                        "session=opaque-session"
                    ),
                }
            }
        }
        response = subprocess.CompletedProcess(
            SSH_COMMAND,
            0,
            json.dumps(
                {
                    "ok": True,
                    "version": "1.2.3",
                    "build": "fixture-build",
                    "service_instance": "fixture-service",
                    "http_status": 200,
                }
            ),
            "",
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory), state=state)
            with mock.patch.object(diagnose.subprocess, "run", return_value=response):
                result = diagnose.diagnose(config_path)

        child = result["components"]["mcp_child"]
        self.assertEqual(result["components"]["authority_http"]["status"], "ready")
        self.assertEqual(child["status"], "error")
        self.assertEqual(child["exit_code"], 7)
        self.assertEqual(result["fault_key"], "mcp-child-exit-7")
        self.assertFalse(result["ok"])
        sample = child["stderr_sample"]
        self.assertIn("[REDACTED", sample)
        for private_value in (
            "secret",
            "alice@example.com",
            "https://authority.invalid",
            "/Users/alice",
            "opaque-session",
        ):
            self.assertNotIn(private_value, sample)

    def test_status_only_missing_state_is_unknown_and_does_not_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory))
            with mock.patch.object(diagnose.subprocess, "run") as run:
                result = diagnose.diagnose(config_path, status_only=True)

        run.assert_not_called()
        self.assertEqual(result["state"]["status"], "missing")
        self.assertFalse(result["probe"]["performed"])
        self.assertEqual(result["probe"]["attempts"], 0)
        self.assertEqual(result["fault_key"], "state-missing")
        self.assertEqual(
            result["components"]["mcp_child"]["summary"],
            "child_exit_stderr_unknown",
        )
        self.assertFalse(result["ok"])

    def test_status_only_connected_without_exit_evidence_is_not_a_child_fault(self) -> None:
        state = {
            "connection": {"state": "connected"},
            "layers": {"authority_http": {"status": "ready", "http_status": 200}},
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory), state=state)
            with mock.patch.object(diagnose.subprocess, "run") as run:
                result = diagnose.diagnose(config_path, status_only=True)

        run.assert_not_called()
        self.assertEqual(
            result["components"]["ssh_or_local_process"]["summary"],
            "bridge_state_connected",
        )
        self.assertEqual(
            result["components"]["mcp_child"]["summary"],
            "child_exit_not_observed",
        )
        self.assertEqual(result["fault_key"], "mcp-child-not-observed")
        self.assertEqual(result["recovery_steps"], [])
        self.assertFalse(result["ok"])

    def test_status_only_disconnected_without_exit_evidence_stays_unknown(self) -> None:
        state = {
            "connection": {
                "state": "disconnected",
                "error": {"layer": "child", "kind": "eof", "stderr_tail": []},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory), state=state)
            with mock.patch.object(diagnose.subprocess, "run") as run:
                result = diagnose.diagnose(config_path, status_only=True)

        run.assert_not_called()
        self.assertEqual(
            result["components"]["ssh_or_local_process"]["summary"],
            "bridge_state_disconnected",
        )
        self.assertEqual(
            result["components"]["mcp_child"]["summary"],
            "child_exit_stderr_unknown",
        )
        self.assertEqual(result["fault_key"], "bridge-disconnected")
        self.assertFalse(result["ok"])

    def test_status_only_uses_explicit_state_for_each_connection_layer(self) -> None:
        state = {
            "layers": {
                "configured_transport": {"status": "ready"},
                "ssh_or_local_process": {"status": "ready", "version": "1.2.3"},
                "authority_http": {"status": "ready", "http_status": 200},
                "mcp_child": {"exit_code": 0, "stderr": ""},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory), state=state)
            with mock.patch.object(diagnose.subprocess, "run") as run:
                result = diagnose.diagnose(config_path, status_only=True)

        run.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["fault_key"], "ok")
        self.assertEqual(result["components"]["ssh_or_local_process"]["status"], "ready")
        self.assertEqual(result["components"]["authority_http"]["http_status"], 200)
        self.assertEqual(result["components"]["mcp_child"]["status"], "ready")

    def test_status_only_accepts_the_bridge_state_child_exit_fingerprint(self) -> None:
        state = {
            "schema_version": 1,
            "connection": {
                "state": "disconnected",
                "error": {
                    "layer": "child",
                    "kind": "child_exited",
                    "exit_code": 9,
                    "stderr_tail": [{"sha256_prefix": "deadbeef", "bytes": 12}],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory), state=state)
            result = diagnose.diagnose(config_path, status_only=True)

        self.assertEqual(
            result["components"]["ssh_or_local_process"]["summary"],
            "bridge_state_disconnected",
        )
        self.assertEqual(result["components"]["mcp_child"]["status"], "error")
        self.assertEqual(result["components"]["mcp_child"]["exit_code"], 9)
        self.assertEqual(
            result["components"]["mcp_child"]["stderr_sample"],
            "redacted_stderr_tail_count:1",
        )
        self.assertEqual(result["fault_key"], "mcp-child-exit-9")
        self.assertFalse(result["ok"])

    def test_auth_or_hostkey_failure_is_not_retried_and_http_stays_unknown(self) -> None:
        response = subprocess.CompletedProcess(
            SSH_COMMAND,
            255,
            "",
            "Host key verification failed for macmini",
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = write_fixture(Path(directory))
            with (
                mock.patch.object(diagnose.subprocess, "run", return_value=response) as run,
                mock.patch.object(diagnose.time, "sleep") as sleep,
            ):
                result = diagnose.diagnose(config_path)

        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            result["components"]["ssh_or_local_process"]["summary"],
            "auth_or_hostkey_error",
        )
        self.assertEqual(result["components"]["authority_http"]["status"], "unknown")
        self.assertEqual(result["fault_key"], "ssh-auth-hostkey-error")
        self.assertNotIn("macmini", result["components"]["ssh_or_local_process"]["stderr_sample"])


if __name__ == "__main__":
    unittest.main()
