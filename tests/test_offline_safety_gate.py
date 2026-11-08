"""Deterministic checks of the offline gate, with no network or bot imports."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from offline_safety_gate import inspect_tracked_paths, is_sensitive_path


class OfflineGateTests(unittest.TestCase):
    def test_valid_python_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "bot.py").write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(inspect_tracked_paths(Path(temp), ["bot.py"]), (1, []))

    def test_invalid_python_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "bot.py").write_text("def broken(:\n", encoding="utf-8")
            count, errors = inspect_tracked_paths(Path(temp), ["bot.py"])
            self.assertEqual(count, 0)
            self.assertEqual(len(errors), 1)
            self.assertIn("Python parse failed", errors[0])

    def test_credential_file_is_blocked(self):
        self.assertTrue(is_sensitive_path("config/.env.production"))
        self.assertTrue(is_sensitive_path("secrets/signing.key"))
        self.assertFalse(is_sensitive_path(".env.example"))
        self.assertFalse(is_sensitive_path("readme.md"))

    def test_missing_tracked_python_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            count, errors = inspect_tracked_paths(Path(temp), ["missing.py"])
            self.assertEqual(count, 0)
            self.assertIn("Missing tracked Python", errors[0])


if __name__ == "__main__":
    unittest.main()
