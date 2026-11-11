"""Guards release/acceptance/mkfixture.py against make_project interface drift."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "release" / "acceptance" / "mkfixture.py"


def run(*args):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args],
                          capture_output=True, text=True)


class MkFixtureTests(unittest.TestCase):
    def build(self, kind):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name).resolve()
        r = run(kind, str(out), str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        root = Path(r.stdout.strip().splitlines()[-1]).resolve()
        self.assertTrue(root.is_dir())
        self.assertTrue(root.is_relative_to(out))
        return out, root

    def servers(self, root, rel=".agents/mcp.json"):
        return json.loads((root / rel).read_text())["mcpServers"]

    def test_clean(self):
        _, root = self.build("clean")
        self.assertEqual(root.name, "project")
        self.assertTrue((root / "AGENTS.md").is_file())
        self.assertIn("local-check", self.servers(root))

    def test_token(self):
        _, root = self.build("token")
        v = self.servers(root)["leaky"]["env"]["API_TOKEN"]
        self.assertTrue(v.startswith("sk-") and len(v) > 30)

    def test_home(self):
        _, root = self.build("home")
        self.assertIn("${HOME}", self.servers(root)["example"]["args"][0])
        self.assertTrue((root / ".env").is_file())

    def test_multisource(self):
        _, root = self.build("multisource")
        self.assertIn("${PLUGIN_ROOT}", self.servers(root, "tools/p/mcp.json")["pserver"]["args"][0])
        self.assertIn("own", self.servers(root, "tools/own/mcp.json"))
        self.assertTrue((root / "tools/p/runtime/server.py").is_file())

    def test_credname(self):
        out, root = self.build("credname")
        self.assertEqual(root.parent.name, "credential-present")
        self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.servers(root)["needs-key"]["env"]["SERVICE_KEY"], "${SERVICE_KEY}")

    def test_wrapped(self):
        _, root = self.build("wrapped")
        self.assertIn("${PLUGIN_ROOT}", self.servers(root)["needs-key"]["args"][0])
        self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)

    def test_usage_errors(self):
        with tempfile.TemporaryDirectory() as t:
            for args in ([], ["clean"], ["clean", t], ["nope", t, str(REPO)],
                         ["clean", t, str(Path(t) / "missing")]):
                r = run(*args)
                self.assertEqual(r.returncode, 2, (args, r.stderr))
                self.assertIn("usage:", r.stderr)
                self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
