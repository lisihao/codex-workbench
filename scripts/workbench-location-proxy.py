#!/usr/bin/env python3
"""Select the LAN or Tailscale transport for the Workbench Authority.

The selector deliberately treats the configured home CIDRs as the only
definition of "home".  A private address observed on another network is not
enough to select the LAN route.  In normal mode the selected transport
command replaces this process; ``--select`` is the inspection-only mode.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple


IFCONFIG_PATH = "/sbin/ifconfig"
LAN_NETCAT_PATH = "/usr/bin/nc"
SCHEMA_VERSION = 1
MAX_PROBE_TIMEOUT_SECONDS = 60.0
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

IPv4OrIPv6 = ipaddress.IPv4Address | ipaddress.IPv6Address
Probe = Callable[[str, int, float], bool]


class ConfigError(ValueError):
    """Raised when the external JSON configuration is not valid."""


class Endpoint(NamedTuple):
    host: str
    port: int


class LocationConfig(NamedTuple):
    schema_version: int
    home_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    lan: Endpoint
    tailscale: Endpoint
    tailscale_binary: str
    probe_timeout_seconds: float
    status_file: Path


def _clean_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError(f"{field} contains a control character")
    return value


def _endpoint(raw: object, *, field: str) -> Endpoint:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{field} must be an object")
    host = _clean_text(raw.get("host"), field=f"{field}.host")
    if any(character.isspace() for character in host):
        raise ConfigError(f"{field}.host must not contain whitespace")
    port_value = raw.get("port")
    if isinstance(port_value, bool) or not isinstance(port_value, int):
        raise ConfigError(f"{field}.port must be an integer")
    if not 1 <= port_value <= 65535:
        raise ConfigError(f"{field}.port must be between 1 and 65535")
    return Endpoint(host=host, port=port_value)


def validate_config(raw: object) -> LocationConfig:
    """Validate and normalize a user-provided location selector config."""

    if not isinstance(raw, Mapping):
        raise ConfigError("configuration must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")

    raw_networks = raw.get("home_networks")
    if not isinstance(raw_networks, list):
        raise ConfigError("home_networks must be a list of CIDRs")
    home_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for index, value in enumerate(raw_networks):
        cidr = _clean_text(value, field=f"home_networks[{index}]")
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as error:
            raise ConfigError(f"home_networks[{index}] is not a valid CIDR") from error
        if network.version == 4 and network.overlaps(TAILSCALE_CGNAT):
            raise ConfigError(
                f"home_networks[{index}] overlaps Tailscale 100.64.0.0/10 and cannot identify home"
            )
        home_networks.append(network)
    if not home_networks:
        raise ConfigError("home_networks must contain at least one CIDR")

    lan = _endpoint(raw.get("lan"), field="lan")
    tailscale_raw = raw.get("tailscale")
    if not isinstance(tailscale_raw, Mapping):
        raise ConfigError("tailscale must be an object")
    tailscale = _endpoint(tailscale_raw, field="tailscale")
    tailscale_binary = _clean_text(
        tailscale_raw.get("binary"), field="tailscale.binary"
    )
    if any(character.isspace() for character in tailscale_binary):
        raise ConfigError("tailscale.binary must not contain whitespace")

    timeout_value = raw.get("probe_timeout_seconds")
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise ConfigError("probe_timeout_seconds must be a number")
    try:
        timeout = float(timeout_value)
    except OverflowError as error:
        raise ConfigError("probe_timeout_seconds is too large") from error
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_PROBE_TIMEOUT_SECONDS:
        raise ConfigError(
            f"probe_timeout_seconds must be finite and between 0 and {MAX_PROBE_TIMEOUT_SECONDS}"
        )

    status_file = Path(_clean_text(raw.get("status_file"), field="status_file")).expanduser()
    return LocationConfig(
        schema_version=SCHEMA_VERSION,
        home_networks=tuple(home_networks),
        lan=lan,
        tailscale=tailscale,
        tailscale_binary=tailscale_binary,
        probe_timeout_seconds=timeout,
        status_file=status_file,
    )


def load_config(path: str | os.PathLike[str]) -> LocationConfig:
    """Read and validate the JSON config at ``path``."""

    config_path = Path(path).expanduser()
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except OSError as error:
        raise ConfigError(f"cannot read config: {config_path}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"config is not valid JSON: {config_path}") from error
    return validate_config(raw)


def _address_token(token: str) -> str:
    # ifconfig appends an interface scope to link-local IPv6 addresses.
    return token.split("%", 1)[0]


def parse_ifconfig_addresses(output: str) -> tuple[IPv4OrIPv6, ...]:
    """Extract valid non-loopback IPv4/IPv6 addresses from macOS ifconfig."""

    addresses: list[IPv4OrIPv6] = []
    current_interface: str | None = None
    address_pattern = re.compile(r"^\s+inet6?\s+(\S+)")
    for line in output.splitlines():
        interface_match = re.match(r"^([^\s:]+):", line)
        if interface_match:
            current_interface = interface_match.group(1)
            continue
        match = address_pattern.match(line)
        if not match:
            continue
        if current_interface is not None and current_interface.startswith("lo"):
            continue
        try:
            address = ipaddress.ip_address(_address_token(match.group(1)))
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified:
            continue
        addresses.append(address)
    return tuple(addresses)


def read_ifconfig() -> str:
    """Return the current interface listing, treating unavailable data as unknown."""

    try:
        result = subprocess.run(
            [IFCONFIG_PATH],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if isinstance(result.stdout, str) else ""


def probe_lan(host: str, port: int, timeout_seconds: float) -> bool:
    """Perform one bounded TCP connect probe against the configured LAN endpoint."""

    try:
        connection = socket.create_connection((host, port), timeout=timeout_seconds)
    except (OSError, TimeoutError, ValueError):
        return False
    try:
        connection.close()
    except OSError:
        pass
    return True


def _normalize_config(config: LocationConfig | Mapping[str, object]) -> LocationConfig:
    return config if isinstance(config, LocationConfig) else validate_config(config)


def select_route(
    config: LocationConfig | Mapping[str, object],
    ifconfig_output: str | None = None,
    *,
    probe: Probe | None = None,
) -> dict[str, object]:
    """Return a transport decision without exposing observed local addresses."""

    normalized = _normalize_config(config)
    addresses = parse_ifconfig_addresses(
        read_ifconfig() if ifconfig_output is None else ifconfig_output
    )
    matched_network: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
    for network in normalized.home_networks:
        if any(address.version == network.version and address in network for address in addresses):
            matched_network = network
            break

    if matched_network is None:
        reason = "unknown_network" if not addresses else "non_home_network"
        return {"route": "tailscale", "reason": reason, "matched_network": None}

    lan_probe = probe or probe_lan
    if lan_probe(
        normalized.lan.host,
        normalized.lan.port,
        normalized.probe_timeout_seconds,
    ):
        return {
            "route": "lan",
            "reason": "home_network_lan_probe_ok",
            "matched_network": str(matched_network),
        }
    return {
        "route": "tailscale",
        "reason": "home_network_lan_probe_failed",
        "matched_network": str(matched_network),
    }


def _format_timeout(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))


def build_lan_command(config: LocationConfig | Mapping[str, object]) -> list[str]:
    normalized = _normalize_config(config)
    return [
        LAN_NETCAT_PATH,
        "-w",
        _format_timeout(normalized.probe_timeout_seconds),
        normalized.lan.host,
        str(normalized.lan.port),
    ]


def build_tailscale_command(config: LocationConfig | Mapping[str, object]) -> list[str]:
    normalized = _normalize_config(config)
    return [
        normalized.tailscale_binary,
        "nc",
        normalized.tailscale.host,
        str(normalized.tailscale.port),
    ]


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_status(
    status_file: str | os.PathLike[str],
    selection: Mapping[str, object],
) -> None:
    """Atomically replace the status file with route metadata only."""

    status_path = Path(status_file).expanduser()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "route": selection["route"],
        "reason": selection["reason"],
        "matched_network": selection["matched_network"],
        "observed_at": _observed_at(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{status_path.name}.",
            dir=str(status_path.parent),
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, status_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="location selector JSON config")
    parser.add_argument(
        "--select",
        action="store_true",
        help="print the decision as JSON instead of replacing the process",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    selection = select_route(config)
    write_status(config.status_file, selection)
    if args.select:
        print(json.dumps({**selection, "observed_at": _observed_at()}, ensure_ascii=False, sort_keys=True))
        return 0

    command = build_lan_command(config) if selection["route"] == "lan" else build_tailscale_command(config)
    try:
        os.execv(command[0], command)
    except OSError as error:
        raise SystemExit(f"cannot exec selected {selection['route']} transport: {error}") from error
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as error:
        raise SystemExit(f"configuration error: {error}") from error
