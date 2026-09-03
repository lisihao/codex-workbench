from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workbench-location-proxy.py"
SPEC = importlib.util.spec_from_file_location("workbench_location_proxy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
location = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(location)


def config(status_file: Path, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "home_networks": ["192.168.50.0/24", "fd12:3456:789a::/64"],
        "lan": {"host": "mac-mini.home", "port": 10022},
        "tailscale": {
            "host": "mac-mini.tailnet",
            "port": 10022,
            "binary": "/opt/homebrew/bin/tailscale",
            "socket": "/Users/example/.local/share/tailscale/tailscaled.sock",
        },
        "probe_timeout_seconds": 2,
        "status_file": str(status_file),
    }
    value.update(changes)
    return value


HOME_IPV4 = """\
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.50.42 netmask 0xffffff00 broadcast 192.168.50.255
\tinet6 fe80::1234%en0 prefixlen 64 secured scopeid 0x6
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
"""


class LocationTransportTests(unittest.TestCase):
    def test_parse_ifconfig_returns_non_loopback_ipv4_and_ipv6_only(self) -> None:
        addresses = location.parse_ifconfig_addresses(HOME_IPV4)

        self.assertEqual(
            {str(address) for address in addresses},
            {"192.168.50.42", "fe80::1234"},
        )

    def test_home_lan_requires_explicit_cidr_and_successful_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[tuple[str, int, float]] = []

            def probe(host: str, port: int, timeout: float) -> bool:
                calls.append((host, port, timeout))
                return True

            result = location.select_route(
                config(Path(directory) / "status.json"),
                HOME_IPV4,
                probe=probe,
            )

        self.assertEqual(result["route"], "lan")
        self.assertEqual(result["reason"], "home_network_lan_probe_ok")
        self.assertEqual(result["matched_network"], "192.168.50.0/24")
        self.assertEqual(calls, [("mac-mini.home", 10022, 2.0)])

    def test_foreign_private_network_uses_tailscale_without_implicit_rfc1918_home(self) -> None:
        foreign = "en0: flags=8863\n\tinet 192.168.99.42 netmask 0xffffff00\n"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(location, "probe_lan") as probe:
                result = location.select_route(
                    config(Path(directory) / "status.json"),
                    foreign,
                )

        self.assertEqual(result, {
            "route": "tailscale",
            "reason": "non_home_network",
            "matched_network": None,
        })
        probe.assert_not_called()

    def test_unknown_network_uses_tailscale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = location.select_route(
                config(Path(directory) / "status.json"),
                "lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n",
            )

        self.assertEqual(result["route"], "tailscale")
        self.assertEqual(result["reason"], "unknown_network")
        self.assertIsNone(result["matched_network"])

    def test_home_lan_probe_failure_falls_back_to_tailscale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = location.select_route(
                config(Path(directory) / "status.json"),
                HOME_IPV4,
                probe=lambda *_: False,
            )

        self.assertEqual(result["route"], "tailscale")
        self.assertEqual(result["reason"], "home_network_lan_probe_failed")
        self.assertEqual(result["matched_network"], "192.168.50.0/24")

    def test_cidr_is_normalized_and_invalid_cidr_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized = location.validate_config(
                config(Path(directory) / "status.json", home_networks=["192.168.50.42/24"])
            )
            self.assertEqual(str(normalized.home_networks[0]), "192.168.50.0/24")
            with self.assertRaises(location.ConfigError):
                location.validate_config(
                    config(Path(directory) / "status.json", home_networks=["not-a-cidr"])
                )
            with self.assertRaisesRegex(location.ConfigError, "overlaps Tailscale"):
                location.validate_config(
                    config(Path(directory) / "status.json", home_networks=["100.64.0.0/10"])
                )
            with self.assertRaisesRegex(location.ConfigError, "at least one CIDR"):
                location.validate_config(
                    config(Path(directory) / "status.json", home_networks=[])
                )

    def test_transport_commands_are_argument_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = config(Path(directory) / "status.json")
            self.assertEqual(
                location.build_lan_command(value),
                ["/usr/bin/nc", "-w", "2", "mac-mini.home", "10022"],
            )
            self.assertEqual(
                location.build_tailscale_command(value),
                [
                    "/opt/homebrew/bin/tailscale",
                    "--socket=/Users/example/.local/share/tailscale/tailscaled.sock",
                    "nc",
                    "mac-mini.tailnet",
                    "10022",
                ],
            )

    def test_tailscale_socket_is_optional_but_must_be_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = config(Path(directory) / "status.json")
            tailscale = dict(value["tailscale"])
            tailscale.pop("socket")
            value["tailscale"] = tailscale
            self.assertEqual(
                location.build_tailscale_command(value),
                ["/opt/homebrew/bin/tailscale", "nc", "mac-mini.tailnet", "10022"],
            )
            tailscale["socket"] = ""
            with self.assertRaisesRegex(location.ConfigError, "tailscale.socket"):
                location.validate_config(value)

    def test_status_is_atomic_metadata_without_observed_local_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "nested" / "status.json"
            selection = {
                "route": "lan",
                "reason": "home_network_lan_probe_ok",
                "matched_network": "192.168.50.0/24",
            }
            location.write_status(status_file, selection)
            status_text = status_file.read_text(encoding="utf-8")
            payload = json.loads(status_text)

        self.assertEqual(
            set(payload),
            {"route", "reason", "matched_network", "observed_at"},
        )
        self.assertNotIn("192.168.50.42", status_text)
        self.assertNotIn("fe80::1234", status_text)
        self.assertEqual(payload["matched_network"], "192.168.50.0/24")

    def test_select_mode_prints_json_and_does_not_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "location.json"
            status_file = Path(directory) / "status.json"
            config_file.write_text(json.dumps(config(status_file)), encoding="utf-8")
            with (
                mock.patch.object(location, "read_ifconfig", return_value=HOME_IPV4),
                mock.patch.object(location, "probe_lan", return_value=True),
                mock.patch.object(location.os, "execv") as execv,
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(location.main(["--config", str(config_file), "--select"]), 0)

        execv.assert_not_called()
        printed = stdout.getvalue()
        self.assertEqual(json.loads(printed)["route"], "lan")

    def test_normal_mode_execs_the_selected_argument_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "location.json"
            status_file = Path(directory) / "status.json"
            config_file.write_text(json.dumps(config(status_file)), encoding="utf-8")
            with (
                mock.patch.object(location, "read_ifconfig", return_value=HOME_IPV4),
                mock.patch.object(location, "probe_lan", return_value=True),
                mock.patch.object(location.os, "execv") as execv,
            ):
                self.assertEqual(location.main(["--config", str(config_file)]), 0)

        execv.assert_called_once_with(
            "/usr/bin/nc",
            ["/usr/bin/nc", "-w", "2", "mac-mini.home", "10022"],
        )


if __name__ == "__main__":
    unittest.main()
