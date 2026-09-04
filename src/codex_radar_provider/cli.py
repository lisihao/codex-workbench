"""Command-line access to the portable Codex Radar provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .provider import DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS, RadarRegistry


_PAYLOAD_FILE_NAMES = {
    "current": "current.json",
    "intelligence_efficiency": "intelligence-efficiency.json",
    "model_ratings": "model-ratings.json",
    "radar_insights": "radar-insights.json",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-radar-provider")
    parser.add_argument("--state-root", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status")
    show = subparsers.add_parser("show")
    show.add_argument("--snapshot-id")

    consent = subparsers.add_parser("consent")
    consent.add_argument("--personal-use", action="store_true")
    consent.add_argument("--authorization-file", type=Path)

    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--authorization-file", required=True, type=Path)
    refresh.add_argument("--timeout-seconds", type=int, default=15)
    refresh.add_argument("--stale-after-seconds", type=int, default=604800)
    refresh.add_argument(
        "--minimum-refresh-interval-seconds",
        type=int,
        default=DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS,
    )
    refresh.add_argument("--api-key-env")
    refresh.add_argument("--api-key-header")

    imported = subparsers.add_parser("import")
    imported.add_argument("--authorization-file", required=True, type=Path)
    source = imported.add_mutually_exclusive_group(required=True)
    source.add_argument("--payloads-json", type=Path)
    source.add_argument("--payload-dir", type=Path)
    imported.add_argument("--stale-after-seconds", type=int, default=604800)
    imported.add_argument("--fetched-at")
    return parser


def _load_payloads(args: argparse.Namespace) -> dict[str, object]:
    if args.payloads_json is not None:
        document = json.loads(args.payloads_json.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("payloads JSON must be an object keyed by endpoint")
        return document
    return {
        endpoint: json.loads((args.payload_dir / filename).read_text(encoding="utf-8"))
        for endpoint, filename in _PAYLOAD_FILE_NAMES.items()
    }


def _emit(document: object) -> None:
    sys.stdout.write(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = RadarRegistry(args.state_root)
    try:
        if args.command == "consent":
            if not args.personal_use:
                raise ValueError("consent requires --personal-use")
            result = registry.consent_personal_use(args.authorization_file)
        elif args.command == "status":
            result = registry.status()
        elif args.command == "show":
            result = (
                registry.load_generation(args.snapshot_id)
                if args.snapshot_id
                else registry.active()
            )
            if result is None:
                result = {"ok": False, "state": "unavailable", "snapshot": None}
        elif args.command == "refresh":
            result = registry.refresh(
                args.authorization_file,
                timeout_seconds=args.timeout_seconds,
                stale_after_seconds=args.stale_after_seconds,
                minimum_refresh_interval_seconds=args.minimum_refresh_interval_seconds,
                api_key_env=args.api_key_env,
                api_key_header=args.api_key_header,
            )
        else:
            result = registry.import_payloads(
                _load_payloads(args),
                args.authorization_file,
                stale_after_seconds=args.stale_after_seconds,
                fetched_at=args.fetched_at,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _emit({"ok": False, "state": "unavailable", "error": str(exc)})
        return 2
    _emit(result)
    return 0 if bool(isinstance(result, dict) and result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
