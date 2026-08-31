from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "codex_workbench" / "static"


class MobileCockpitStaticTests(unittest.TestCase):
    def test_task_ledger_is_not_hidden_on_narrow_screens(self) -> None:
        html = (STATIC / "index.html").read_text()
        css = (STATIC / "app.css").read_text()

        self.assertIn('class="task-ledger-section" id="task-ledger-section"', html)
        self.assertNotIn('class="desktop-detail" id="task-ledger-section"', html)
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn(".task-actions { display: grid;", css)
        self.assertIn(".task-brief { grid-template-columns: repeat(2", css)
        self.assertIn(".nodes { flex-wrap: nowrap; overflow-x: auto;", css)

    def test_mobile_task_cards_keep_status_controls_and_short_instruction(self) -> None:
        javascript = (STATIC / "app.js").read_text()

        for contract in (
            'data-task-card=',
            'data-task-state=',
            'data-task-updated=',
            '? "pause" : ["paused", "inbox", "ready", "needs_fix"].includes(task.state) ? "resume"',
            'data-action="set_priority"',
            'data-steer-input=',
            'data-steer-task=',
            'data-approval=',
            'class="task-contract"',
            'contract.allowed_scope',
            'contract.acceptance_commands',
        ):
            self.assertIn(contract, javascript)

    def test_phone_observation_requires_a_visible_rendered_task_summary(self) -> None:
        javascript = (STATIC / "app.js").read_text()

        self.assertIn("async function capturePhoneRender(data)", javascript)
        self.assertIn("data.tasks.length === 0", javascript)
        self.assertIn('getComputedStyle(section).display === "none"', javascript)
        self.assertIn("state?.textContent.trim() !== String(task.state)", javascript)
        self.assertIn("taskUpdated.dataset.updatedAt !== String(task.updated_at)", javascript)
        self.assertIn("const renderedReceipt = await capturePhoneRender(data);", javascript)
        self.assertIn("await recordPhoneObservation(data, renderedReceipt);", javascript)
        self.assertIn("if (!renderedReceipt) return;", javascript)


if __name__ == "__main__":
    unittest.main()
