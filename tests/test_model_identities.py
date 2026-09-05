from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import unittest

from codex_workbench.model_identities import (
    catalog_with_model_identities,
    derive_model_identities,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CLI_VERSION = "2.1.42"


def catalog(*aliases: str) -> dict[str, object]:
    models = [
        {
            "provider": "claude",
            "model_id": alias,
            "model_family": alias,
            "identity": {
                "selection_id": alias,
                "kind": "cli-alias",
                "canonical_model_id": None,
            },
            "routable": True,
        }
        for alias in aliases
    ]
    models.append(
        {
            "provider": "codex",
            "model_id": "gpt-5.6-luna",
            "model_family": "luna",
            "identity": {
                "selection_id": "gpt-5.6-luna",
                "kind": "exact-cli-id",
                "canonical_model_id": "gpt-5.6-luna",
            },
            "routable": True,
        }
    )
    return {
        "catalog_id": "catalog-test",
        "digest": "d" * 64,
        "agents": {"claude": {"cli_version": CLI_VERSION}},
        "models": models,
    }


def task(alias: str, *, executor: str = "claude", verifier: bool = False) -> list[dict[str, object]]:
    return [
        {
            "task_id": "task-1",
            "state": "accepted",
            "nodes": [
                {
                    "node_id": "node-1",
                    "executor": executor,
                    "model": alias,
                    "verifier": verifier,
                }
            ],
        }
    ]


def event(
    event_type: str,
    *,
    cursor: int = 2,
    created_at: datetime = NOW - timedelta(hours=1),
    alias: str = "opus",
    actual_model: str | None = "claude-opus-4-6",
    provider: str | None = "claude",
    agent_version: str | None = CLI_VERSION,
    exit_code: object = 0,
    result_kind: str = "worker",
    attempt: int = 1,
    executor: str = "claude",
) -> dict[str, object]:
    return {
        "cursor": cursor,
        "event_type": event_type,
        "task_id": "task-1",
        "node_id": "node-1",
        "created_at": created_at.isoformat(),
        "payload": {
            "attempt": attempt,
            "executor": executor,
            "effective_model": alias,
            "result": {
                "provider": provider,
                "actual_model": actual_model,
                "agent_version": agent_version,
                "exit_code": exit_code,
                "result_kind": result_kind,
            },
        },
    }


class ModelIdentityTests(unittest.TestCase):
    def test_valid_native_receipt_binds_exact_actual_and_copies_evidence(self) -> None:
        events = [
            {
                "cursor": 1,
                "event_type": "node.started",
                "task_id": "task-1",
                "node_id": "node-1",
                "created_at": (NOW - timedelta(hours=1, minutes=1)).isoformat(),
                "payload": {"attempt": 1, "executor": "claude", "effective_model": "opus"},
            },
            event("node.accepted"),
        ]
        report = derive_model_identities(events, task("opus"), catalog("opus"), now=NOW)
        self.assertEqual([item["selection_id"] for item in report["bindings"]], ["opus"])
        binding = report["bindings"][0]
        self.assertEqual(binding["canonical_model_id"], "claude-opus-4-6")
        self.assertEqual(binding["agent_cli_version"], CLI_VERSION)
        self.assertEqual(binding["evidence"], {"task_id": "task-1", "node_id": "node-1", "attempt": 1, "cursor": 2})
        self.assertEqual(binding["valid_until"], "2026-09-11T11:00:00+00:00")

        view = catalog("opus")
        derived = catalog_with_model_identities(view, report)
        self.assertIsNone(view["models"][0]["identity"]["canonical_model_id"])
        self.assertEqual(
            derived["models"][0]["identity"]["canonical_model_id"],
            "claude-opus-4-6",
        )
        self.assertEqual(derived["models"][0]["identity"]["evidence"], binding["evidence"])
        codex = next(item for item in derived["models"] if item["provider"] == "codex")
        self.assertEqual(codex["identity"]["canonical_model_id"], "gpt-5.6-luna")

    def test_stale_and_future_receipts_remain_unresolved(self) -> None:
        stale = event("node.accepted", created_at=NOW - timedelta(days=7, seconds=1))
        future = event("node.accepted", cursor=3, created_at=NOW + timedelta(seconds=1))
        report = derive_model_identities([stale, future], task("opus"), catalog("opus"), now=NOW)
        self.assertEqual(report["bindings"], [])
        self.assertIn("stale_observation", report["unresolved"]["opus"])
        self.assertIn("future_observation", report["unresolved"]["opus"])

    def test_version_mismatch_and_provider_fallback_do_not_bind(self) -> None:
        wrong_version = event("node.accepted", agent_version="2.1.41")
        no_provider = event("node.accepted", cursor=3, provider=None)
        report = derive_model_identities(
            [wrong_version, no_provider], task("opus"), catalog("opus"), now=NOW
        )
        self.assertEqual(report["bindings"], [])
        self.assertIn("agent_version_mismatch", report["unresolved"]["opus"])
        self.assertIn("provider_missing", report["unresolved"]["opus"])

    def test_missing_actual_family_mismatch_and_conflict_are_fail_closed(self) -> None:
        missing = event("node.accepted", actual_model=None)
        report = derive_model_identities([missing], task("opus"), catalog("opus"), now=NOW)
        self.assertIn("missing_actual_model", report["unresolved"]["opus"])

        mismatch = event("node.accepted", actual_model="claude-sonnet-4-6")
        report = derive_model_identities([mismatch], task("opus"), catalog("opus"), now=NOW)
        self.assertIn("family_mismatch", report["unresolved"]["opus"])

        first = event("node.accepted", actual_model="claude-opus-4-6", cursor=2)
        second = event("node.accepted", actual_model="claude-opus-4-7", cursor=3)
        report = derive_model_identities([first, second], task("opus"), catalog("opus"), now=NOW)
        self.assertEqual(report["bindings"], [])
        self.assertIn(
            "conflicting_canonical_ids_at_newest_timestamp",
            report["unresolved"]["opus"],
        )

    def test_reused_fixture_deterministic_and_verifier_paths_do_not_bind(self) -> None:
        valid = event("node.accepted")
        reused = {
            "cursor": 3,
            "event_type": "node.evidence_reused",
            "task_id": "task-1",
            "node_id": "node-1",
            "payload": {"attempt": 1},
            "created_at": NOW.isoformat(),
        }
        report = derive_model_identities([valid, reused], task("opus"), catalog("opus"), now=NOW)
        self.assertIn("evidence_reused", report["unresolved"]["opus"])

        report = derive_model_identities([valid], task("opus", verifier=True), catalog("opus"), now=NOW)
        self.assertIn("verifier_path", report["unresolved"]["opus"])

        report = derive_model_identities(
            [event("node.accepted", executor="fixture")],
            task("opus", executor="fixture"),
            catalog("opus"),
            now=NOW,
        )
        self.assertEqual(report["bindings"], [])

    def test_catalog_view_only_changes_existing_alias_and_does_not_mutate_source(self) -> None:
        source = catalog("opus", "sonnet")
        original = deepcopy(source)
        report = derive_model_identities([event("node.accepted")], task("opus"), source, now=NOW)
        view = catalog_with_model_identities(source, report)
        self.assertEqual(source, original)
        self.assertEqual(
            view["models"][0]["identity"]["canonical_model_id"],
            "claude-opus-4-6",
        )
        self.assertIsNone(view["models"][1]["identity"]["canonical_model_id"])
        self.assertEqual(view["models"][2], source["models"][2])


if __name__ == "__main__":
    unittest.main()
