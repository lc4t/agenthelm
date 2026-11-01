"""用户级运行时：安装、幂等、同版本不改写、切换、命令链接与离线发布包。"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

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

    def tarball_of(self, source: Path, top: str) -> Path:
        tarball = self.base / f"{top}.tar.gz"
        with tarfile.open(tarball, "w:gz") as bundle:
            bundle.add(source, arcname=top)
        return tarball

    def test_candidate_and_release_tarballs_get_separate_directories(self) -> None:
        # index.json carries no -rc suffix; the tarball's top directory (agenthelm-<tag>) does.
        candidate = self.tarball_of(self.release_copy("9.3.0", marker="rc"), "agenthelm-v9.3.0-rc.2")
        release = self.tarball_of(self.release_copy("9.3.0"), "agenthelm-v9.3.0")
        code, payload = self.runtime("install", "--apply", "--source", str(candidate))
        self.assertEqual((code, payload["plan"]["version"]), (0, "9.3.0-rc.2"), payload)
        self.assertTrue((self.data / "runtimes/9.3.0-rc.2").is_dir())
        code, payload = self.runtime("install", "--apply", "--source", str(release))
        self.assertEqual((code, payload["plan"]["version"]), (0, "9.3.0"), payload)
        self.assertEqual(os.readlink(self.data / "current"), "runtimes/9.3.0")
        self.assertTrue((self.data / "runtimes/9.3.0-rc.2").is_dir(), "the candidate stays for rollback")
        code, rc1 = self.runtime("install", "--apply", "--source",
                                 str(self.tarball_of(self.release_copy("9.3.0", marker="rc1"), "agenthelm-v9.3.0-rc.1")))
        self.assertEqual((code, rc1["plan"]["version"]), (0, "9.3.0-rc.1"), "rc.1 and rc.2 differ in content and coexist")

    def test_npm_package_layout_takes_the_suffix_from_package_json(self) -> None:
        package = self.base / "pkg"
        shutil.copytree(self.release_copy("9.4.0"), package / "runtime")
        (package / "package.json").write_text(json.dumps({"name": "@sakanano-moe/agenthelm", "version": "9.4.0-rc.3"}))
        code, payload = self.runtime("install", "--apply", "--source", str(package / "runtime"))
        self.assertEqual((code, payload["plan"]["version"]), (0, "9.4.0-rc.3"), payload)

    def test_a_checkout_cannot_tell_the_suffix_and_keeps_the_release_version(self) -> None:
        code, payload = self.runtime("install", "--apply", "--source", str(self.release_copy("9.5.0")))
        self.assertEqual((code, payload["plan"]["version"]), (0, "9.5.0"))
        # An unrelated package.json or a name that does not extend the version is not trusted.
        wrong = self.base / "wrong"
        shutil.copytree(self.release_copy("9.6.0"), wrong / "runtime")
        (wrong / "package.json").write_text(json.dumps({"name": "other", "version": "9.6.0-rc.1"}))
        self.assertEqual(self.runtime("install", "--source", str(wrong / "runtime"))[1]["plan"]["version"], "9.6.0")
        mismatch = self.tarball_of(self.release_copy("9.7.0"), "agenthelm-v9.8.0-rc.1")
        self.assertEqual(self.runtime("install", "--source", str(mismatch))[1]["plan"]["version"], "9.7.0")

    def test_refusal_explains_how_to_handle_a_suffixless_clash(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.release_copy("9.9.0")))
        code, payload = self.runtime("install", "--source", str(self.release_copy("9.9.0", marker="y")))
        self.assertEqual(code, 2)
        self.assertTrue(any("-rc.N" in line for line in payload["next"]), payload["next"])

    def test_release_orders_after_its_candidates(self) -> None:
        sys.path.insert(0, str(ROOT / "plugins/agent-pack/runtime"))
        try:
            import agenthelm_runtime as runtime
        finally:
            sys.path.pop(0)
        newer = runtime._newer
        self.assertTrue(newer("6.11.0", "6.11.0-rc.2"))
        self.assertTrue(newer("6.11.0-rc.2", "6.11.0-rc.1"))
        self.assertTrue(newer("6.11.0-rc.10", "6.11.0-rc.2"))
        self.assertFalse(newer("6.11.0-rc.2", "6.11.0"))
        self.assertFalse(newer("6.11.0", "6.11.0"))
        self.assertTrue(newer("6.11.1-rc.1", "6.11.0"))

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
