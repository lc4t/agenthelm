"""``agenthelm --version`` prints the suite version from index.json, or says it is unknown (#99)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import agenthelm  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class VersionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patch = mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en", "HOME": str(self.root / "home")})
        patch.start()
        self.addCleanup(patch.stop)

    def suite_version(self) -> str:
        return json.loads((REPO / "index.json").read_text(encoding="utf-8"))["plugin"]["version"]

    def test_checkout_layout_reads_the_version_from_index_json(self) -> None:
        self.assertEqual(agenthelm.version_text(), f"agenthelm {self.suite_version()}")

    def test_top_level_flag_exits_zero_and_prints_it(self) -> None:
        completed = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "--version"], capture_output=True,
                                   text=True, check=False, env={**os.environ, "AGENTHELM_NO_UPDATE_CHECK": "1"})
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, f"agenthelm {self.suite_version()}"), completed.stderr)

    def test_any_copied_layout_with_index_json_reports_its_own_version(self) -> None:
        release = self.root / "release"
        write(release / "index.json", {"plugin": {"version": "7.8.9"}})
        self.assertEqual(agenthelm.version_text(release), "agenthelm 7.8.9")

    def test_npm_package_keeps_the_prerelease_suffix(self) -> None:
        package = self.root / "package"
        write(package / "runtime" / "index.json", {"plugin": {"version": "7.8.9"}})
        write(package / "package.json", {"name": "@sakanano-moe/agenthelm", "version": "7.8.9-rc.2"})
        self.assertEqual(agenthelm.version_text(package / "runtime"), "agenthelm 7.8.9-rc.2")

    def test_plugin_cache_without_index_json_falls_back_to_the_plugin_version(self) -> None:
        cache = self.root / "cache" / "agent-pack"
        write(cache / "plugin.json", {"name": "agent-pack", "version": "4.5.6"})
        text = agenthelm.version_text(self.root / "cache", cache)
        self.assertEqual(text, "agenthelm (suite version unknown: this runtime ships without index.json); agent-pack plugin 4.5.6")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "zh"}):
            self.assertEqual(agenthelm.version_text(self.root / "cache", cache),
                             "agenthelm（套件版本未知：此运行时不含 index.json）；agent-pack 插件 4.5.6")

    def test_unreadable_index_json_and_manifest_still_answer(self) -> None:
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "index.json").write_text("{", encoding="utf-8")
        self.assertIn("suite version unknown", agenthelm.version_text(broken, broken))
        self.assertTrue(agenthelm.version_text(broken, broken).endswith("agent-pack plugin unknown"))


if __name__ == "__main__":
    unittest.main()
