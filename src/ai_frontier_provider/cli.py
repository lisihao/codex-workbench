"""Command-line access to the portable AI Frontier observation provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .provider import (
    DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    AIFrontierRegistry,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-frontier-provider")
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
    refresh.add_argument("--model", dest="model_source_ids", action="append", default=[])
    refresh.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    refresh.add_argument("--stale-after-seconds", type=int, default=DEFAULT_STALE_AFTER_SECONDS)
    refresh.add_argument(
        "--minimum-refresh-interval-seconds",
        type=int,
        default=DEFAULT_MINIMUM_REFRESH_INTERVAL_SECONDS,
    )
    return parser


def _emit(document: object) -> None:
    sys.stdout.write(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = AIFrontierRegistry(args.state_root)
    try:
        if args.command == "consent":
            if not args.personal_use:
                raise ValueError("consent requires --personal-use")
            result = registry.consent_personal_use(args.authorization_file)
        elif args.command == "status":
            result = registry.status()
        elif args.command == "show":
            result = registry.load_generation(args.snapshot_id) if args.snapshot_id else registry.active()
            if result is None:
                result = {"ok": False, "state": "unavailable", "snapshot": None}
        else:
            result = registry.refresh(
                args.authorization_file,
                timeout_seconds=args.timeout_seconds,
                stale_after_seconds=args.stale_after_seconds,
                minimum_refresh_interval_seconds=args.minimum_refresh_interval_seconds,
                model_source_ids=args.model_source_ids,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _emit({"ok": False, "state": "unavailable", "error": str(exc)})
        return 2
    _emit(result)
    return 0 if bool(isinstance(result, dict) and result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
