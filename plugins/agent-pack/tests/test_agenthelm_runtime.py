"""用户级运行时：安装、幂等、同版本不改写、切换、命令链接与离线发布包。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"
PLUGINS = ("agent-pack", "agents-init", "opinion-manager", "project-orchestrator")


@unittest.skipIf(sys.platform == "win32", "the runtime supports macOS and Linux")
class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.data = self.home / ".local/share/agenthelm"
        self.env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "AGENTHELM_LATEST_URL": str(ROOT / "latest.json")}
        self.env.pop("XDG_DATA_HOME", None)

    def runtime(self, *args: str, script: Path = AGENTHELM) -> tuple[int, dict]:
        result = subprocess.run([sys.executable, str(script), "runtime", *args, "--output", "json"],
                                capture_output=True, text=True, check=False, env=self.env, timeout=120)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"exit {result.returncode}: {result.stdout}\n{result.stderr}")

    def release_copy(self, version: str, *, marker: str = "") -> Path:
        """A release-shaped directory (index.json + plugins) with its own release version."""
        root = self.base / f"release-{version}{marker}"
        for name in PLUGINS:
            shutil.copytree(ROOT / "plugins" / name, root / "plugins" / name,
                            ignore=shutil.ignore_patterns("__pycache__", "tests"))
        index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
        index["plugin"]["version"] = version
        (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
        if marker:
            (root / "plugins/agent-pack/runtime/marker.py").write_text(f"# {marker}\n", encoding="utf-8")
        return root

    def test_preview_writes_nothing(self) -> None:
        code, payload = self.runtime("install")
        self.assertEqual((code, payload["applied"]), (0, False))
        self.assertFalse(self.data.exists())

    def test_install_is_idempotent_and_the_shim_runs(self) -> None:
        code, payload = self.runtime("install", "--apply")
        self.assertEqual(code, 0, payload)
        version = json.loads((ROOT / "index.json").read_text())["plugin"]["version"]
        self.assertEqual(os.readlink(self.data / "current"), f"runtimes/{version}")
        self.assertFalse((self.data / "current/plugins/agent-pack/tests").exists())
        code, again = self.runtime("install", "--apply")
        self.assertEqual((code, again["plan"]["action"]), (0, "none"))
        shim = self.data / "current/bin/agenthelm"
        result = subprocess.run([str(shim), "runtime", "status", "--offline", "--output", "json"],
                                capture_output=True, text=True, check=False, env=self.env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual((status["current"]["version"], status["running_from"]), (version, "runtime"))

    def test_same_version_with_other_content_is_not_overwritten(self) -> None:
        self.assertEqual(self.runtime("install", "--apply", "--source", str(self.release_copy("9.0.0")))[0], 0)
        code, payload = self.runtime("install", "--apply", "--source", str(self.release_copy("9.0.0", marker="x")))
        self.assertEqual(code, 2)
        self.assertIn("不会被改写", payload["next"][0])
        self.assertFalse((self.data / "runtimes/9.0.0/plugins/agent-pack/runtime/marker.py").exists())

    def test_new_version_installs_beside_and_use_switches_back(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.release_copy("9.0.0")))
        code, payload = self.runtime("install", "--apply", "--source", str(self.release_copy("9.1.0")))
        self.assertEqual(payload["rollback"], "agenthelm runtime use 9.0.0 --apply")
        self.assertTrue((self.data / "runtimes/9.0.0").is_dir(), "旧版本保留")
        code, preview = self.runtime("use", "9.0.0")
        self.assertEqual(os.readlink(self.data / "current"), "runtimes/9.1.0", "use 默认只预览")
        self.runtime("use", "9.0.0", "--apply")
        self.assertEqual(os.readlink(self.data / "current"), "runtimes/9.0.0")
        code, payload = self.runtime("use", "1.2.3")
        self.assertEqual(code, 2)

    def test_offline_install_from_a_release_tarball_and_link_bin(self) -> None:
        source = self.release_copy("9.2.0")
        tarball = self.base / "agenthelm-v9.2.0.tar.gz"
        with tarfile.open(tarball, "w:gz") as bundle:
            bundle.add(source, arcname="agenthelm-v9.2.0")
        bin_dir = self.base / "bin"
        code, payload = self.runtime("install", "--apply", "--source", str(tarball), "--link-bin", str(bin_dir))
        self.assertEqual(code, 0, payload)
        self.assertIn("本地发布包", payload["plan"]["source"])
        link = bin_dir / "agenthelm"
        result = subprocess.run([str(link), "runtime", "status", "--offline", "--output", "json"],
                                capture_output=True, text=True, check=False, env=self.env, timeout=60)
        self.assertEqual(json.loads(result.stdout)["current"]["version"], "9.2.0", result.stderr)
        (bin_dir / "other").write_text("x")
        code, payload = self.runtime("install", "--source", str(tarball), "--link-bin", str(bin_dir))
        self.assertEqual(code, 0, "已是本运行时的链接时不报错")

    def test_link_bin_never_overwrites_a_foreign_file(self) -> None:
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "agenthelm").write_text("#!/bin/sh\necho someone else\n")
        code, payload = self.runtime("install", "--apply", "--link-bin", str(bin_dir))
        self.assertEqual(code, 2)
        self.assertEqual((bin_dir / "agenthelm").read_text(), "#!/bin/sh\necho someone else\n")

    def test_not_a_release_source_is_explained(self) -> None:
        code, payload = self.runtime("install", "--source", str(self.base))
        self.assertEqual(code, 2)
        self.assertIn("不是 agenthelm 发布内容", payload["error"])


if __name__ == "__main__":
    unittest.main()
