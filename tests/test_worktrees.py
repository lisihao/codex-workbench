from __future__ import annotations

import unittest

from codex_workbench.worktrees import normalize_scope, scope_access_conflicts, scope_allows


class ScopeTests(unittest.TestCase):
    def test_normalizes_cross_platform_relative_scopes(self) -> None:
        self.assertEqual(normalize_scope("./src\\parser/"), "src/parser")
        with self.assertRaises(ValueError):
            normalize_scope("../outside")
        with self.assertRaises(ValueError):
            normalize_scope("C:\\outside")

    def test_authorization_uses_scope_containment(self) -> None:
        self.assertTrue(scope_allows("src/parser/tokenizer.py", ["src"], []))
        self.assertFalse(scope_allows("src/private/token.txt", ["src"], ["src/private"]))

    def test_access_matrix_blocks_writes_but_not_read_read(self) -> None:
        self.assertTrue(scope_access_conflicts((), ("src/parser",), (), ("src/parser/tokenizer",)))
        self.assertTrue(scope_access_conflicts(("src/parser",), (), (), ("src/parser/tokenizer",)))
        self.assertFalse(scope_access_conflicts(("src/parser",), (), ("src/parser/tokenizer",), ()))
        self.assertFalse(scope_access_conflicts((), ("src/parser",), (), ("src/renderer",)))


if __name__ == "__main__":
    unittest.main()
