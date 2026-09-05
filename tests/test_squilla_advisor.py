from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_workbench.squilla_advisor import (
    UPSTREAM_REVISION,
    SquillaAdvisor,
    SquillaAdvisorRequest,
)


_LFS_POINTER = "version https://git-lfs.github.com/spec/v1\noid sha256:fixture\nsize 1\n"


class SquillaAdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "opensquilla-source"
        self.bundle_dir = self.root / "bundle"
        self._write_fake_upstream()
        self._write_bundle()
        self.source_revision = self._initialize_source_git()
        self.runtime_python = Path("/opt/homebrew/bin/python3.12")
        if not self.runtime_python.is_file():
            self.skipTest("requires an already-installed Python 3.12 runtime")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def advisor(self, **changes: object) -> SquillaAdvisor:
        values: dict[str, object] = {
            "runtime_python": self.runtime_python,
            "source_root": self.source_root,
            "bundle_dir": self.bundle_dir,
            "timeout_seconds": 3.0,
        }
        values.update(changes)
        return SquillaAdvisor(**values)  # type: ignore[arg-type]

    def request(self, prompt: str = "visible prompt") -> SquillaAdvisorRequest:
        return SquillaAdvisorRequest(
            request_id="node-1",
            prompt=prompt,
            valid_tiers=("c0", "c1", "c2", "c3"),
            history_user_texts=("earlier visible user turn",),
            routing_history=(
                {
                    "route_class": "R1",
                    "margin": 0.12,
                    "hidden_reasoning": "must never leave the caller",
                },
            ),
            previous_public_summary="published prior-turn summary",
            previous_public_usage={"total_tokens": 31},
            state_flags={"quota_known": True, "protected_pool": "healthy"},
        )

    def advise(self, request: SquillaAdvisorRequest, **changes: object):
        """Fixture-only override of the adapter's fixed internal expected pin."""

        with mock.patch(
            "codex_workbench.squilla_advisor.UPSTREAM_REVISION", self.source_revision
        ):
            return self.advisor(**changes).advise(request)

    def advise_batch(self, requests: list[SquillaAdvisorRequest], **changes: object):
        with mock.patch(
            "codex_workbench.squilla_advisor.UPSTREAM_REVISION", self.source_revision
        ):
            return self.advisor(**changes).advise_batch(requests)

    def test_protocol_uses_actual_worker_boundary_and_prompt_free_receipt(self) -> None:
        prompt = "do not persist this unique prompt"
        with mock.patch(
            "codex_workbench.squilla_advisor.subprocess.run", wraps=subprocess.run
        ) as runner:
            advice = self.advise(self.request(prompt))

        self.assertEqual(runner.call_count, 1)
        command = runner.call_args.args[0]
        self.assertEqual(command[0], str(self.runtime_python))
        self.assertIn("squilla_advisor_worker.py", command[-1])
        self.assertEqual(advice.status, "available")
        self.assertEqual(advice.demand_tier, "c2")
        self.assertEqual(advice.confidence, 0.82)
        self.assertEqual(advice.route_class, "R2")
        self.assertEqual(advice.thinking_hint, "T2")
        self.assertEqual(advice.prompt_hint, "Use deliberate reasoning.")
        self.assertEqual(
            advice.classification_semantics,
            "demand_classification_not_task_success_or_model_success_ranking",
        )
        self.assertFalse(advice.source["quality"]["native_inference_acceptance"] == "passed")  # type: ignore[index]
        self.assertEqual(advice.source["expected_source_revision"], self.source_revision)
        self.assertEqual(advice.source["observed_source_revision"], self.source_revision)
        self.assertEqual(advice.source["verification_method"], "git_rev_parse_head")
        self.assertNotIn(prompt, json.dumps(advice.to_receipt()))
        self.assertNotIn("selected_model", json.dumps(advice.to_receipt()))
        self.assertNotIn("hidden_reasoning", self.fake_artifact("captured.json").read_text())

    def test_batch_loads_one_strategy_instance_and_preserves_public_only_context(self) -> None:
        requests = [
            self.request("first visible prompt"),
            SquillaAdvisorRequest(
                request_id="node-2",
                prompt="second visible prompt",
                valid_tiers=("c0", "c1", "c2", "c3"),
            ),
        ]

        advice = self.advise_batch(requests)

        self.assertEqual([item.status for item in advice], ["available", "available"])
        self.assertEqual(self.fake_artifact("load-count.txt").read_text(encoding="utf-8"), "1")
        captured = json.loads(self.fake_artifact("captured.json").read_text(encoding="utf-8"))
        self.assertEqual(captured["previous_public_summary"], "published prior-turn summary")
        self.assertEqual(captured["history_user_texts"], ["earlier visible user turn"])
        self.assertEqual(captured["routing_history"], [{"route_class": "R1", "margin": 0.12}])
        self.assertEqual(captured["flags_text_override"], '{"protected_pool":"healthy","quota_known":true}')

    def test_lfs_pointer_is_unavailable_before_worker_load(self) -> None:
        asset = self.bundle_dir / "lgbm_main.bin"
        asset.write_text(_LFS_POINTER, encoding="utf-8")
        (self.bundle_dir / "artifact_manifest.json").write_text(
            json.dumps({"schema_version": 1, "files": [{"path": "lgbm_main.bin"}]}),
            encoding="utf-8",
        )

        advice = self.advise(self.request())

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "bundle_asset_lfs_pointer")
        self.assertFalse(self.fake_artifact("load-count.txt").exists())

    def test_invalid_upstream_result_is_not_relabelled_as_a_default_tier(self) -> None:
        advice = self.advise(self.request("invalid-result"))

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "invalid_strategy_result")
        self.assertIsNone(advice.demand_tier)
        self.assertIsNone(advice.confidence)

    def test_upstream_v4_unavailable_is_not_relabelled_as_c1(self) -> None:
        advice = self.advise(self.request("v4-unavailable"))

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "v4_unavailable")
        self.assertIsNone(advice.demand_tier)

    def test_missing_upstream_dependency_is_diagnostic(self) -> None:
        self.fake_artifact("v4_phase3.py").write_text(
            "import deliberately_missing_opensquilla_dependency\n", encoding="utf-8"
        )

        advice = self.advise(self.request())

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "upstream_dependency_missing")

    def test_timeout_is_diagnostic_and_never_returns_a_tier(self) -> None:
        advice = self.advise(self.request("slow"), timeout_seconds=0.2)

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "worker_timeout")
        self.assertIsNone(advice.demand_tier)

    def test_worker_blocks_network_and_does_not_use_provider_or_llm_methods(self) -> None:
        advice = self.advise(self.request("network-attempt"))

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "strategy_classify_failed")
        self.assertTrue(self.fake_artifact("network-attempted.txt").is_file())
        self.assertFalse(self.fake_artifact("provider-or-llm-called.txt").exists())

    def test_source_revision_mismatch_is_unavailable_and_observed(self) -> None:
        advice = self.advisor().advise(self.request())

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "source_revision_mismatch")
        self.assertEqual(advice.source["expected_source_revision"], UPSTREAM_REVISION)
        self.assertEqual(advice.source["observed_source_revision"], self.source_revision)
        self.assertEqual(advice.source["verification_method"], "git_rev_parse_head")
        self.assertFalse(self.fake_artifact("load-count.txt").exists())

    def test_missing_git_source_identity_is_unavailable(self) -> None:
        (self.source_root / ".git").rename(self.source_root / ".git-hidden")

        advice = self.advise(self.request())

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "source_identity_unverified")
        self.assertEqual(advice.source["observed_source_revision"], None)
        self.assertEqual(advice.source["verification_method"], "not_performed")

    def test_manifest_is_required_before_worker_start(self) -> None:
        (self.bundle_dir / "artifact_manifest.json").unlink()

        advice = self.advise(self.request())

        self.assertEqual(advice.status, "unavailable")
        self.assertEqual(advice.diagnostic, "bundle_manifest_missing")
        self.assertFalse(self.fake_artifact("load-count.txt").exists())

    def fake_artifact(self, name: str) -> Path:
        return self.source_root / "src" / "opensquilla" / "squilla_router" / name

    def _write_bundle(self) -> None:
        (self.bundle_dir / "runtime_src").mkdir(parents=True)
        (self.bundle_dir / "router.runtime.yaml").write_text("v4: {}\n", encoding="utf-8")
        (self.bundle_dir / "version.json").write_text(
            json.dumps({"version": "fixture-v4"}), encoding="utf-8"
        )
        (self.bundle_dir / "inference_manifest.json").write_text(
            json.dumps(
                {
                    "bundle_version": 1,
                    "source_model_version": "fixture-v4",
                    "feature_dim": 390,
                    "feature_meta": {"schema_version": 2},
                }
            ),
            encoding="utf-8",
        )
        (self.bundle_dir / "fixture.bin").write_bytes(b"fixture-asset")
        (self.bundle_dir / "artifact_manifest.json").write_text(
            json.dumps({"schema_version": 1, "files": [{"path": "fixture.bin"}]}),
            encoding="utf-8",
        )

    def _initialize_source_git(self) -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.source_root, check=True)
        subprocess.run(["git", "add", "src"], cwd=self.source_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture source",
            ],
            cwd=self.source_root,
            check=True,
        )
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.source_root, text=True
        ).strip()

    def _write_fake_upstream(self) -> None:
        package = self.source_root / "src" / "opensquilla" / "squilla_router"
        package.mkdir(parents=True)
        (package.parent / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "v4_phase3.py").write_text(
            """
import asyncio
import json
from pathlib import Path
import socket


class V4Phase3Strategy:
    source = \"v4_phase3\"

    def __init__(self, bundle_dir=None, require_router_runtime=False):
        counter = Path(__file__).with_name(\"load-count.txt\")
        current = int(counter.read_text() or \"0\") if counter.exists() else 0
        counter.write_text(str(current + 1), encoding=\"utf-8\")
        self.bundle_dir = bundle_dir
        self.require_router_runtime = require_router_runtime

    async def classify(
        self,
        message,
        valid_tiers,
        routing_history=None,
        prev_assistant_text=None,
        prev_assistant_usage=None,
        history_user_texts=None,
        flags_text_override=None,
    ):
        captured = Path(__file__).with_name(\"captured.json\")
        if not captured.exists():
            captured.write_text(
                json.dumps(
                    {
                        \"routing_history\": routing_history,
                        \"previous_public_summary\": prev_assistant_text,
                        \"previous_public_usage\": prev_assistant_usage,
                        \"history_user_texts\": history_user_texts,
                        \"flags_text_override\": flags_text_override,
                    }
                ),
                encoding=\"utf-8\",
            )
        if message == \"slow\":
            await asyncio.sleep(1)
        if message == \"invalid-result\":
            return (\"c1\", \"not-a-confidence\", \"v4_phase3\", {})
        if message == \"v4-unavailable\":
            return (\"c1\", 0.0, \"v4_unavailable\", {})
        if message == \"network-attempt\":
            Path(__file__).with_name(\"network-attempted.txt\").write_text(\"attempted\")
            socket.create_connection((\"127.0.0.1\", 9), timeout=0.1)
        return (
            \"c2\",
            0.82,
            \"v4_phase3\",
            {
                \"route_class\": \"R2\",
                \"thinking_mode\": \"T2\",
                \"prompt_policy\": \"P2\",
                \"prompt_hint\": \"Use deliberate reasoning.\",
                \"selected_model\": \"must-not-reach-consumer\",
                \"leaked_prompt\": message,
            },
        )

    def select_provider(self):
        Path(__file__).with_name(\"provider-or-llm-called.txt\").write_text(\"provider\")
        raise AssertionError(\"provider selection is outside advisor scope\")

    async def call_llm(self):
        Path(__file__).with_name(\"provider-or-llm-called.txt\").write_text(\"llm\")
        raise AssertionError(\"LLM invocation is outside advisor scope\")
""".lstrip(),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
