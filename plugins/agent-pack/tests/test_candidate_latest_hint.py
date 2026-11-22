"""A candidate (-rc.N) is announced by the latest.json the user passed; the next command must carry that source."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import agenthelm_runtime as runtime  # noqa: E402

AGENTHELM = RUNTIME / "agenthelm.py"


class LatestOptionTests(unittest.TestCase):
    def test_only_a_candidate_with_a_source_gets_the_option(self) -> None:
        self.assertEqual(runtime.latest_option("6.15.0-rc.2", "/tmp/l.json"), " --latest /tmp/l.json")
        self.assertEqual(runtime.latest_option("6.15.0-rc.2", "https://x.example/a b.json"),
                         " --latest 'https://x.example/a b.json'")
        self.assertEqual(runtime.latest_option("6.15.0", "/tmp/l.json"), "")
        self.assertEqual(runtime.latest_option("6.15.0-rc.2", None), "")
        self.assertEqual(runtime.latest_option(None, "/tmp/l.json"), "")


class StatusHintTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        data = self.home / ".local/share/agenthelm"
        installed = data / "runtimes/6.14.0"
        installed.mkdir(parents=True)
        (installed / "runtime.json").write_text(json.dumps({"schema_version": 1, "version": "6.14.0"}), encoding="utf-8")
        (data / "current").symlink_to(installed, target_is_directory=True)

    def status(self, tag: str, lang: str, *, via_env: bool = False) -> str:
        latest = self.root / "latest.json"
        latest.write_text(json.dumps({"marketplace": "agenthelm", "release": {"tag": tag}}), encoding="utf-8")
        env = {**os.environ, "HOME": str(self.home), "AGENTHELM_LANG": lang}
        env.pop("XDG_DATA_HOME", None)
        env.pop("AGENTHELM_LATEST_URL", None)
        args = [sys.executable, str(AGENTHELM), "runtime", "status", "--offline"]
        if via_env:
            env["AGENTHELM_LATEST_URL"] = str(latest)
        else:
            args += ["--latest", str(latest)]
        done = subprocess.run(args, capture_output=True, text=True, check=False, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_candidate_hint_carries_latest_in_both_languages(self) -> None:
        latest = str(self.root / "latest.json")
        self.assertIn(f"agenthelm runtime update --latest {latest} 预览", self.status("v6.15.0-rc.2", "zh"))
        self.assertIn(f"preview with agenthelm runtime update --latest {latest}", self.status("v6.15.0-rc.2", "en"))
        self.assertIn(f"runtime update --latest {latest}", self.status("v6.15.0-rc.2", "en", via_env=True))

    def test_stable_hint_is_unchanged(self) -> None:
        self.assertIn("有新版本 6.15.0：agenthelm runtime update 预览", self.status("v6.15.0", "zh"))
        self.assertIn("preview with agenthelm runtime update\n", self.status("v6.15.0", "en"))


if __name__ == "__main__":
    unittest.main()
