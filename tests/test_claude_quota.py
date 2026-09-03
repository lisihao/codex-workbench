from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from codex_workbench.claude_quota import (
    ClaudeQuotaCollector,
    ClaudeQuotaError,
    COMPATIBLE_SOURCE,
    scrubbed_environment,
    watch_claude_quota,
)
from codex_workbench.quota import JsonFileQuotaAdapter
from codex_workbench.cli import command_quota


def _completed(arguments: list[str], stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, returncode, stdout, "")


class ClaudeQuotaCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "claude-quota.json"
        self.binary = self.root / "claude"
        self.binary.write_text("fixture")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _collector(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]) -> ClaudeQuotaCollector:
        def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return responses[tuple(arguments[1:])]

        return ClaudeQuotaCollector(self.binary, self.output, runner=runner)

    def _logged_in(self) -> str:
        return json.dumps({"loggedIn": True, "authMethod": "subscription", "apiProvider": "firstParty"})

    def _assert_fail_closed_snapshot(self, *, auth_ok: bool = False) -> None:
        persisted = json.loads(self.output.read_text())
        self.assertEqual(persisted["auth_ok"], auth_ok)
        self.assertEqual(
            persisted["auth_method"],
            "native-subscription" if auth_ok else "none",
        )
        self.assertFalse(persisted["quota_ok"])
        self.assertTrue(persisted["error"])
        adapted = JsonFileQuotaAdapter(self.output).read()
        assert adapted is not None
        self.assertEqual(adapted.dispatch_decision("sonnet").action, "codex")

    def _usage(self, result: object | None = None, **extra: object) -> str:
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 0,
            "duration_api_ms": 0,
            "total_cost_usd": 0,
            "modelUsage": {},
            "usage": {
                "iterations": [], "input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "output_tokens_details": {"thinking_tokens": 0},
                "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                "service_tier": "standard",
            },
            "permission_denials": [],
            "subagent_stats": {},
            "stop_reason": None,
            "result": result if result is not None else self._text_result(),
        }
        payload.update(extra)
        return json.dumps(payload)

    @staticmethod
    def _text_result() -> str:
        return "\n".join((
            "Current session: 10% used · resets 4 pm (Asia/Singapore)",
            "Current week (all models): 20% used · resets Dec 31, 2027 (Asia/Singapore)",
            "Current week (Sonnet only): 30% used · resets Dec 31, 2027 (Asia/Singapore)",
        ))

    def test_logged_out_writes_fail_closed_snapshot_without_usage(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(arguments[1:]))
            if arguments[1:] == ["auth", "status", "--json"]:
                return _completed(
                    arguments,
                    json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}),
                    returncode=1,
                )
            return _completed(arguments, "2.1.239")

        snapshot = ClaudeQuotaCollector(self.binary, self.output, runner=runner).collect()
        self.assertFalse(snapshot["auth_ok"])
        self.assertEqual(snapshot["error"], "logged out")
        self.assertEqual(snapshot["claude_version"], "2.1.239")
        self.assertEqual(calls, [("auth", "status", "--json"), ("--version",)])
        persisted = JsonFileQuotaAdapter(self.output).read()
        assert persisted is not None
        self.assertFalse(persisted.auth_ok)
        self.assertIsNone(persisted.five_hour_remaining)

    def test_successful_three_pool_snapshot_is_trusted_and_atomic_0600(self) -> None:
        responses = {
            ("auth", "status", "--json"): _completed([], self._logged_in()),
            ("--version",): _completed([], "2.1.239 (Claude Code)\n"),
            ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed([], self._usage()),
        }
        snapshot = self._collector(responses).collect()
        self.assertEqual(snapshot["source"], COMPATIBLE_SOURCE)
        self.assertTrue(snapshot["pools"]["five_hour"]["window_id"].startswith("five_hour:"))
        self.assertEqual(os.stat(self.output).st_mode & 0o777, 0o600)
        derived = JsonFileQuotaAdapter(self.output).read()
        assert derived is not None
        self.assertEqual((derived.five_hour_remaining, derived.weekly_all_remaining, derived.weekly_sonnet_remaining), (89.0, 79.0, 69.0))
        self.assertIsNone(derived.weekly_fable_remaining)
        self.assertEqual(derived.dispatch_decision("fable").action, "claude")
        self.assertEqual(derived.source, COMPATIBLE_SOURCE)
        self.assertEqual(derived.producer, "codex-workbench.claude-quota")
        self.assertEqual(derived.producer_schema_version, 1)
        self.assertEqual(derived.claude_version, "2.1.239")

    def test_json_array_envelope_and_fable_pool_are_supported_without_idle_reset(self) -> None:
        text = "\n".join((
            "You are currently using your subscription to power your Claude Code usage",
            "Current session: 0% used",
            "Current week (all models): 0% used · resets Sep 9 at 1am (America/Toronto)",
            "Current week (Fable): 1% used · resets Sep 9 at 1am (America/Toronto)",
        ))
        result = json.loads(self._usage(text))
        result["subagent_stats"] = {
            "spawned": 0,
            "requested": {"background": 0, "foreground": 0, "unset": 0},
            "completed": 0,
            "failed": 0,
            "by_type": {},
        }
        responses = {
            ("auth", "status", "--json"): _completed([], self._logged_in()),
            ("--version",): _completed([], "2.1.239 (Claude Code)\n"),
            ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed(
                [],
                json.dumps([
                    {"type": "system", "subtype": "init"},
                    {"type": "assistant", "message": {"model": "<synthetic>"}},
                    result,
                ]),
            ),
        }

        snapshot = self._collector(responses).collect()

        self.assertEqual(snapshot["pools"]["five_hour"]["window_id"], "five_hour:idle")
        self.assertEqual(snapshot["pools"]["five_hour"]["reset_precision"], "idle")
        self.assertIn("seven_day_fable", snapshot["pools"])
        self.assertNotIn("seven_day_sonnet", snapshot["pools"])
        derived = JsonFileQuotaAdapter(self.output).read()
        assert derived is not None
        self.assertEqual(derived.weekly_fable_remaining, 98.0)
        self.assertIsNone(derived.weekly_sonnet_remaining)
        self.assertEqual(derived.dispatch_decision("fable").action, "claude")

    def test_passive_usage_rejects_nonzero_subagent_activity(self) -> None:
        payload = json.loads(self._usage())
        payload["subagent_stats"] = {"spawned": 1}
        responses = {
            ("auth", "status", "--json"): _completed([], self._logged_in()),
            ("--version",): _completed([], "2.1.239 (Claude Code)\n"),
            ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed(
                [], json.dumps(payload)
            ),
        }

        with self.assertRaisesRegex(ClaudeQuotaError, "must not record permissions"):
            self._collector(responses).collect()
        self._assert_fail_closed_snapshot(auth_ok=True)

    def test_missing_pool_or_format_drift_immediately_replaces_old_quota_with_fail_closed_snapshot(self) -> None:
        self.output.write_text("last-known-good\n")
        responses = {
            ("auth", "status", "--json"): _completed([], self._logged_in()),
            ("--version",): _completed([], "2.1.239\n"),
            ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed([], self._usage("format drift")),
        }
        with self.assertRaises(ClaudeQuotaError):
            self._collector(responses).collect()
        persisted = json.loads(self.output.read_text())
        self.assertTrue(persisted["auth_ok"])
        self.assertFalse(persisted["quota_ok"])
        adapted = JsonFileQuotaAdapter(self.output).read()
        assert adapted is not None
        self.assertTrue(adapted.auth_ok)
        self.assertEqual(adapted.quota_zone("sonnet")[0], "unknown")
        self.assertEqual(adapted.dispatch_decision("sonnet").action, "codex")

    def test_nonzero_turn_token_or_cost_invalidates_old_quota(self) -> None:
        for field, value in (("num_turns", 1), ("duration_api_ms", 1), ("total_cost_usd", 0.1)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                self.output = Path(directory) / "quota.json"
                self.output.write_text("last-known-good\n")
                responses = {
                    ("auth", "status", "--json"): _completed([], self._logged_in()),
                    ("--version",): _completed([], "2.1.239 (Claude Code)\n"),
                    ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed([], self._usage(**{field: value})),
                }
                with self.assertRaises(ClaudeQuotaError):
                    self._collector(responses).collect()
                self._assert_fail_closed_snapshot(auth_ok=True)

        self.output = self.root / "quota-token.json"
        self.output.write_text("last-known-good\n")
        token_payload = json.loads(self._usage())
        token_payload["usage"]["output_tokens_details"]["thinking_tokens"] = 1
        responses = {
            ("auth", "status", "--json"): _completed([], self._logged_in()),
            ("--version",): _completed([], "2.1.239 (Claude Code)\n"),
            ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed([], json.dumps(token_payload)),
        }
        with self.assertRaises(ClaudeQuotaError):
            self._collector(responses).collect()
        self._assert_fail_closed_snapshot(auth_ok=True)

    def test_different_weekly_reset_windows_invalidate_old_quota(self) -> None:
        self.output.write_text("last-known-good\n")
        text = self._text_result().replace("Dec 31, 2027 (Asia/Singapore)\nCurrent week (Sonnet only)", "Dec 30, 2027 (Asia/Singapore)\nCurrent week (Sonnet only)", 1)
        responses = {
            ("auth", "status", "--json"): _completed([], self._logged_in()),
            ("--version",): _completed([], "2.1.239 (Claude Code)\n"),
            ("-p", "/usage", "--output-format", "json", "--no-session-persistence"): _completed([], self._usage(text)),
        }
        with self.assertRaises(ClaudeQuotaError):
            self._collector(responses).collect()
        self._assert_fail_closed_snapshot(auth_ok=True)

    def test_auth_command_failure_invalidates_a_previous_usable_snapshot(self) -> None:
        self.output.write_text("last-known-good\n")
        responses = {
            ("auth", "status", "--json"): _completed([], "", returncode=1),
        }

        with self.assertRaises(ClaudeQuotaError):
            self._collector(responses).collect()

        self._assert_fail_closed_snapshot()

    def test_environment_never_forwards_credentials_but_keeps_proxy_and_home(self) -> None:
        environment = {
            "HOME": "/tmp/home",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_AUTH_TOKEN": "token",
            "MY_PASSWORD": "password",
        }
        self.assertEqual(scrubbed_environment(environment), {"HOME": "/tmp/home", "HTTPS_PROXY": "http://127.0.0.1:7890"})

    def test_watcher_keeps_collecting_after_a_fail_closed_observation(self) -> None:
        snapshots: list[object] = [
            {
                "auth_ok": False,
                "source": COMPATIBLE_SOURCE,
            },
            ClaudeQuotaError("auth status failed"),
        ]

        class Collector:
            output = self.output

            @staticmethod
            def collect() -> dict[str, object]:
                value = snapshots.pop(0)
                if isinstance(value, Exception):
                    raise value
                assert isinstance(value, dict)
                return value

        emitted: list[dict[str, object]] = []
        sleeps: list[float] = []
        watch_claude_quota(
            Collector(),  # type: ignore[arg-type]
            interval_seconds=60,
            emit=emitted.append,
            sleeper=sleeps.append,
            max_iterations=2,
        )
        self.assertEqual([event["ok"] for event in emitted], [True, False])
        self.assertEqual(emitted[1]["error"], "auth status failed")
        self.assertEqual(sleeps, [60])

    def test_manual_quota_set_cannot_claim_producer_source(self) -> None:
        args = SimpleNamespace(quota_action="set", source=COMPATIBLE_SOURCE)
        with self.assertRaisesRegex(ValueError, "cannot claim Claude producer provenance"):
            command_quota(args)

    def test_subscription_requires_explicit_first_party_api_provider(self) -> None:
        from codex_workbench.claude_quota import _is_native_subscription

        self.assertFalse(_is_native_subscription({"loggedIn": True, "authMethod": "subscription", "provider": "anthropic"}))
        self.assertTrue(_is_native_subscription({"loggedIn": True, "authMethod": "subscription", "apiProvider": "firstParty"}))
