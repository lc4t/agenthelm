"""release/acceptance/extract_blocks.py turns an acceptance prompt's bash blocks into a runnable script."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "release" / "acceptance" / "extract_blocks.py"
FENCE = "`" * 3

PROMPT = (
    "# Prompt\n\nSome prose.\n\n"
    f"{FENCE}bash\necho first\nexport MARK=one\n{FENCE}\n\n"
    f"{FENCE}text\necho not-a-command\n{FENCE}\n\n"
    f"{FENCE}bash\necho \"second:$MARK\"\n{FENCE}\n"
)


def run(*args):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True)


class ExtractBlocksTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.prompt = self.dir / "prompt.md"
        self.prompt.write_text(PROMPT, encoding="utf-8")
        self.out = self.dir / "out.sh"

    def test_only_bash_blocks_in_order_with_markers(self):
        result = run(str(self.prompt), str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2 blocks")
        script = self.out.read_text(encoding="utf-8")
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        self.assertLess(script.index("BLOCK 0"), script.index("BLOCK 1"))
        self.assertNotIn("not-a-command", script)

    def test_script_has_valid_syntax_and_runs_blocks_in_one_shell(self):
        run(str(self.prompt), str(self.out))
        self.assertEqual(subprocess.run(["bash", "-n", str(self.out)], capture_output=True).returncode, 0)
        env = {**os.environ, "HOME": str(self.dir)}
        result = subprocess.run(["bash", str(self.out)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("second:one", result.stdout)

    def test_bad_usage_exits_2(self):
        self.assertEqual(run().returncode, 2)
        self.assertEqual(run(str(self.prompt)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
