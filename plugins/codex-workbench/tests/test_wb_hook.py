from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "wb_hook.py"
SPEC = importlib.util.spec_from_file_location("wb_hook", SCRIPT)
assert SPEC and SPEC.loader
wb_hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wb_hook)


class WBHookTests(unittest.TestCase):
    def test_unbound_normal_prompt_is_inert(self) -> None:
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-inert",
            "cwd": "/tmp",
            "prompt": "hello",
            "transcript_path": None,
        }
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(output):
            self.assertEqual(wb_hook.main(), 0)
        self.assertEqual(output.getvalue(), "")

    def test_activation_syncs_and_persists_active_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = self._repository(root)
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-09-01T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "existing request"}],
                        },
                    }
                )
                + "\n"
            )
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-active",
                "cwd": str(repository),
                "prompt": "$WB",
                "transcript_path": str(transcript),
            }
            receipt = {
                "context_ref": "sha256:" + "a" * 64 + ":tar.gz",
                "repository": "/remote/context",
                "base_sha": "b" * 40,
            }
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"PLUGIN_DATA": str(root / "plugin-data")}),
                patch.object(wb_hook, "_send", return_value=receipt),
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(wb_hook.main(), 0)
            context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertIn("WB_SYNC_RECEIPT", context)
            self.assertIn('"state":"active"', context)
            binding = json.loads(
                (root / "plugin-data" / "bindings" / "session-active.json").read_text()
            )
            self.assertTrue(binding["existing_conversation"])

    def test_supported_one_token_invocations_activate(self) -> None:
        prompts = (
            "wb",
            "wb continue",
            "wb，只读检查",
            "wb,read-only check",
            "$WB：状态",
            "/wb，继续",
            "启动工作台",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                event = {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-supported",
                    "cwd": str(self._repository(root)),
                    "prompt": prompt,
                    "transcript_path": None,
                }
                receipt = {
                    "context_ref": "sha256:" + "a" * 64 + ":tar.gz",
                    "repository": "/remote/context",
                    "base_sha": "b" * 40,
                }
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {"PLUGIN_DATA": str(root / "plugin-data")}),
                    patch.object(wb_hook, "_send", return_value=receipt) as send,
                    patch.object(sys, "stdin", io.StringIO(json.dumps(event))),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(wb_hook.main(), 0)
                send.assert_called_once()
                context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
                self.assertIn('"state":"active"', context)

    def test_wb_prefix_inside_a_word_is_inert(self) -> None:
        for prompt in ("wbackup", "$wblegacy", "/wbench"):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                event = {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-inert-prefix",
                    "cwd": "/tmp",
                    "prompt": prompt,
                    "transcript_path": None,
                }
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {"PLUGIN_DATA": str(root / "plugin-data")}),
                    patch.object(wb_hook, "_send") as send,
                    patch.object(sys, "stdin", io.StringIO(json.dumps(event))),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(wb_hook.main(), 0)
                send.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_transport_is_derived_from_existing_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(
                '[mcp_servers.codex-workbench]\n'
                'command = "ssh"\n'
                'args = ["authority-fixture", \'exec "$HOME/app/codex-workbench" mcp\']\n'
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                command = wb_hook._mcp_ssh_command("command-1")
            self.assertEqual(command[0], "ssh")
            self.assertEqual(command[1], "authority-fixture")
            self.assertTrue(command[-1].endswith("context import --archive - --command-id command-1"))

    def test_local_mcp_transport_replaces_final_mcp_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(
                "[mcp_servers.codex-workbench]\n"
                'command = "/Applications/Codex Workbench.app/bin/codex-workbench"\n'
                'args = ["mcp"]\n'
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                command = wb_hook._mcp_ssh_command("command-local")
            self.assertEqual(
                command,
                [
                    "/Applications/Codex Workbench.app/bin/codex-workbench",
                    "context",
                    "import",
                    "--archive",
                    "-",
                    "--command-id",
                    "command-local",
                ],
            )

    def test_active_binding_is_reused_without_a_new_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = root / "bindings" / "session-reuse.json"
            binding.parent.mkdir()
            binding.write_text(
                json.dumps(
                    {
                        "state": "active",
                        "source_thread_id": "session-reuse",
                        "context_ref": "sha256:" + "c" * 64 + ":tar.gz",
                    }
                )
            )
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-reuse",
                "cwd": "/tmp",
                "prompt": "status?",
                "transcript_path": None,
            }
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"PLUGIN_DATA": str(root)}),
                patch.object(wb_hook, "_bundle") as bundle,
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(wb_hook.main(), 0)
            bundle.assert_not_called()
            context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertIn("already bound", context)

    @staticmethod
    def _repository(root: Path) -> Path:
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
        (repository / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
        return repository


if __name__ == "__main__":
    unittest.main()
