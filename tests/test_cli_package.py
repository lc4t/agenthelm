from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/cli"


class CliPackageTests(unittest.TestCase):
    def test_package_ships_the_launcher_and_the_synced_runtime(self) -> None:
        manifest = json.loads((PACKAGE / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "@agenthelm/cli")
        self.assertEqual(manifest["bin"], {"agenthelm": "bin/agenthelm.js"})
        self.assertEqual(sorted(manifest["files"]), ["bin", "runtime"])
        self.assertEqual(manifest["scripts"]["prepack"], "node scripts/sync-runtime.mjs")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_launcher_runs_the_plugin_runtime(self) -> None:
        subprocess.run(["node", "scripts/sync-runtime.mjs"], cwd=PACKAGE, check=True)
        runtime = PACKAGE / "runtime"
        for script in (ROOT / "plugins/agent-pack/runtime").glob("*.py"):
            self.assertEqual((runtime / script.name).read_bytes(), script.read_bytes(), script.name)
        result = subprocess.run(["node", "bin/agenthelm.js", "check", "--help"], cwd=PACKAGE,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--hook", result.stdout)


if __name__ == "__main__":
    unittest.main()
