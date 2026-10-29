from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

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
        self.assertEqual(manifest["name"], "@sakanano-moe/agenthelm")
        self.assertEqual(manifest["bin"], {"agenthelm": "bin/agenthelm.js"})
        self.assertEqual(sorted(manifest["files"]), ["bin", "runtime"])
        self.assertEqual(manifest["scripts"]["prepack"], "node scripts/sync-runtime.mjs")
        # A scoped package is private by default; the first staged publish must be public.
        self.assertEqual(manifest["publishConfig"], {"access": "public"})

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_launcher_runs_the_plugin_runtime(self) -> None:
        subprocess.run(["node", "scripts/sync-runtime.mjs"], cwd=PACKAGE, check=True)
        runtime = PACKAGE / "runtime"
        # The package carries the whole release content: index.json and every plugin, minus tests.
        self.assertEqual((runtime / "index.json").read_bytes(), (ROOT / "index.json").read_bytes())
        for plugin in ("agent-pack", "agents-init", "opinion-manager", "project-orchestrator"):
            for script in (ROOT / "plugins" / plugin / "runtime").glob("*.py"):
                copied = runtime / "plugins" / plugin / "runtime" / script.name
                self.assertEqual(copied.read_bytes(), script.read_bytes(), f"{plugin}/{script.name}")
            self.assertFalse((runtime / "plugins" / plugin / "tests").exists(), plugin)
        result = subprocess.run(["node", "bin/agenthelm.js", "check", "--help"], cwd=PACKAGE,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--hook", result.stdout)
        demo = subprocess.run(["node", "bin/agenthelm.js", "project", "--demo", "blank", "guide"], cwd=PACKAGE,
                              capture_output=True, text=True, check=False, input="")
        self.assertEqual(demo.returncode, 0, demo.stderr)
        self.assertIn("项目指南", demo.stdout)


if __name__ == "__main__":
    unittest.main()
