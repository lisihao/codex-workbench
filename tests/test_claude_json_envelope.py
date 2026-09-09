import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_workbench.executors import ClaudeExecutor, ExecutionRequest


class RecordingArtifacts:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def put_text(self, text: str, suffix: str) -> str:
        self.writes.append((text, suffix))
        return f"fixture-{len(self.writes)}.{suffix}"


class ClaudeJsonEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.worktree = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def _request(self) -> ExecutionRequest:
        return ExecutionRequest(
            task_id="fixture-task",
            node_id="fixture-node",
            attempt=1,
            contract={
                "objective": "exercise a synthesized Claude JSON envelope",
                "allowed_scope": [],
                "forbidden_scope": [],
                "acceptance_commands": [],
                "timeout_seconds": 5,
                "governance_profile": "code-as-harness/v1",
                "verification_tier": "L2",
            },
            spec={
                "title": "Claude JSON envelope fixture",
                "prompt": "Return the fixture response.",
                "model": "claude-sonnet-5",
                "write_scopes": [],
            },
            worktree=self.worktree,
        )

    def _run_fixture(self, output: str, *, returncode: int = 0):
        fixture = self.worktree / "claude-fixture.py"
        fixture.write_text(
            "#!" + sys.executable + "\n"
            "import sys\n"
            f"sys.stdout.write({output!r})\n"
            f"sys.exit({returncode})\n",
            encoding="utf-8",
        )
        fixture.chmod(0o700)
        artifacts = RecordingArtifacts()
        executor = ClaudeExecutor(artifacts, quota=None, binary=str(fixture))
        with mock.patch.object(executor, "qualification", return_value=(True, "fixture")):
            result = executor.execute(self._request())
        return result, artifacts

    @staticmethod
    def _structured(status: str = "succeeded") -> dict:
        return {
            "status": status,
            "summary": f"fixture {status}",
            "changed_paths": [],
            "checks": ["fixture check"],
        }

    @classmethod
    def _terminal(cls, status: str = "succeeded", **overrides: object) -> dict:
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "modelUsage": {"claude-sonnet-5": {"inputTokens": 1}},
            "structured_output": cls._structured(status),
        }
        terminal.update(overrides)
        return terminal

    @staticmethod
    def _events() -> list[dict]:
        return [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"role": "assistant"}},
            {"type": "user", "message": {"role": "user"}},
            {"type": "rate_limit_event", "rate_limit_info": {}},
        ]

    @classmethod
    def _event_array(cls, terminal: dict | None = None) -> list[dict]:
        events = cls._events()
        if terminal is not None:
            events.append(terminal)
        return events

    def _rejected_result(self, value: object, *, raw: bool = False):
        output = value if raw else json.dumps(value, separators=(",", ":"))
        result, _ = self._run_fixture(output)
        self.assertEqual(result.status, "failed")
        return result

    def test_event_array_preserves_business_status_and_actual_model(self) -> None:
        for status in ("succeeded", "blocked", "failed"):
            with self.subTest(status=status):
                output = json.dumps(self._event_array(self._terminal(status)), separators=(",", ":"))
                result, artifacts = self._run_fixture(output)

                self.assertEqual(result.status, status)
                self.assertEqual(result.actual_model, "claude-sonnet-5")
                self.assertEqual(result.changed_paths, ())
                self.assertEqual(result.checks, ("fixture check",))
                self.assertIn((output, "stdout.log"), artifacts.writes)
                self.assertIn((output, "result.json"), artifacts.writes)

    def test_legacy_object_envelope_remains_accepted(self) -> None:
        legacy = self._terminal()
        legacy.pop("type")
        legacy.pop("subtype")
        legacy.pop("is_error")

        result, _ = self._run_fixture(json.dumps(legacy, separators=(",", ":")))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.actual_model, "claude-sonnet-5")

    def test_event_array_rejections_are_fail_closed_and_sanitized(self) -> None:
        terminal = self._terminal()
        terminal_without_error = self._terminal()
        terminal_without_error.pop("is_error")
        terminal_without_output = self._terminal()
        terminal_without_output.pop("structured_output")
        terminal_without_model = self._terminal()
        terminal_without_model.pop("modelUsage")
        duplicate_terminal = self._terminal()
        invalid_cases: tuple[tuple[str, object, bool], ...] = (
            ("empty", [], False),
            ("scalar", "not an envelope", False),
            ("malformed JSON", "[", True),
            (
                "non-object event",
                [
                    {"type": "system", "message": "sensitive-event-text"},
                    1,
                ],
                False,
            ),
            ("no terminal", self._events(), False),
            (
                "duplicate terminals",
                self._events() + [terminal, duplicate_terminal],
                False,
            ),
            (
                "conflicting terminals",
                self._events()
                + [self._terminal(), self._terminal(subtype="error", is_error=True)],
                False,
            ),
            (
                "terminal is not final",
                self._events() + [self._terminal(), {"type": "system"}],
                False,
            ),
            (
                "non-success subtype",
                self._event_array(self._terminal(subtype="error")),
                False,
            ),
            (
                "error terminal",
                self._event_array(self._terminal(is_error=True)),
                False,
            ),
            (
                "missing false error flag",
                self._event_array(terminal_without_error),
                False,
            ),
            (
                "non-boolean false error flag",
                self._event_array(self._terminal(is_error=0)),
                False,
            ),
            (
                "missing structured output",
                self._event_array(terminal_without_output),
                False,
            ),
            (
                "non-object structured output",
                self._event_array(self._terminal(structured_output=[])),
                False,
            ),
            (
                "invalid business schema",
                self._event_array(self._terminal(structured_output={})),
                False,
            ),
            ("missing model attestation", self._event_array(terminal_without_model), False),
            (
                "ambiguous model attestation",
                self._event_array(
                    self._terminal(
                        modelUsage={
                            "claude-sonnet-5": {"inputTokens": 1},
                            "claude-haiku-5": {"inputTokens": 1},
                        }
                    )
                ),
                False,
            ),
            (
                "conflicting model attestations",
                self._event_array(self._terminal(model="claude-haiku-5")),
                False,
            ),
        )

        for name, value, raw in invalid_cases:
            with self.subTest(case=name):
                result = self._rejected_result(value, raw=raw)
                self.assertNotIn("sensitive-event-text", result.summary)

    def test_event_array_preserves_nonzero_exit_failure(self) -> None:
        output = json.dumps(self._event_array(self._terminal()), separators=(",", ":"))
        result, _ = self._run_fixture(output, returncode=17)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 17)
        self.assertEqual(result.actual_model, "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
