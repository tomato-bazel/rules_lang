"""Exercise public macros and generated mains against the compiled Atlas only."""
import os
from pathlib import Path
import subprocess
import unittest


class CheckedEmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "smoke"

    def invoke(self, name):
        return subprocess.run([str(self.root / name)], text=True, capture_output=True, timeout=30)

    def test_typed_failures_have_nonzero_status_and_no_source_stdout(self):
        for language in ["typescript", "rust"]:
            with self.subTest(language=language):
                result = self.invoke(language + "_wrong")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(language + " lowering failed", result.stderr)
                self.assertIn("fixture-other", result.stderr)

    def test_supported_outputs_match_the_real_macros(self):
        for language in ["typescript", "rust"]:
            with self.subTest(language=language):
                result = self.invoke(language + "_valid")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("answer", result.stdout)
                self.assertIn("42", result.stdout)
                self.assertNotIn("LOWERING ERROR", result.stdout)
                self.assertEqual(result.stdout, (self.root / (language + ".txt")).read_text())

    def test_legacy_custom_projection_retains_its_contract(self):
        result = self.invoke("legacy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "legacy fixture\n")


if __name__ == "__main__":
    unittest.main()
