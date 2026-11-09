"""#81：nudge install-hooks 经 npx 等包运行器的临时缓存运行时，hook 命令必须指向稳定入口，找不到就拒绝写入。

所有路径是临时目录里的虚构路径；HOME 是临时目录，不碰真实的 ~/.cursor、~/.codex、~/.claude。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))
import agent_pack_nudge as nudge  # noqa: E402

BRIDGE = [{"command": f"/opt/vibe-island/bin/vibe-island-bridge --source cursor --event {n}"} for n in range(3)]


class EphemeralPathRuleTests(unittest.TestCase):
    EPHEMERAL = (
        "/Volumes/data/ann/.npm/_npx/9f8e7d6c5b4a3210/node_modules/@sakanano-moe/agenthelm/runtime/plugins/agent-pack/runtime/agenthelm.py",  # macOS (seen in #81)
        "/home/ann/.npm/_npx/9f8e7d6c5b4a3210/node_modules/@sakanano-moe/agenthelm/runtime/plugins/agent-pack/runtime/agenthelm.py",  # Linux
        "/home/ann/my cache/npm/_npx/ab12/node_modules/x/agenthelm.py",  # npm_config_cache moved, with a space
        "C:\\Users\\Ann\\AppData\\Local\\npm-cache\\_npx\\9f8e\\node_modules\\@sakanano-moe\\agenthelm\\runtime\\plugins\\agent-pack\\runtime\\agenthelm.py",  # Windows
        "c:/users/ann/appdata/local/NPM-CACHE/_NPX/9f8e/node_modules/x/agenthelm.py",  # Windows, forward slashes, other case
        "/home/ann/.cache/pnpm/dlx/abc123/node_modules/x/agenthelm.py",  # pnpm dlx (Linux)
        "/Volumes/data/ann/Library/Caches/pnpm/dlx/abc123/node_modules/x/agenthelm.py",  # pnpm dlx (macOS)
        "C:\\Users\\Ann\\AppData\\Local\\pnpm-cache\\dlx\\abc123\\x\\agenthelm.py",  # pnpm dlx (Windows)
        "/tmp/xfs-1a2b3c4d/dlx-4242/node_modules/x/agenthelm.py",  # yarn dlx
        "/tmp/bunx-1000-@sakanano-moe/agenthelm@6.12.0/node_modules/x/agenthelm.py",  # bunx
    )
    STABLE = (
        "/Volumes/data/ann/.cursor/plugins/local/agent-pack/runtime/agenthelm.py",
        "/home/ann/.local/share/agenthelm/current/plugins/agent-pack/runtime/agenthelm.py",
        "/home/ann/.local/share/agenthelm/runtimes/1.2.3/plugins/agent-pack/runtime/agenthelm.py",
        "/home/ann/.claude/plugins/cache/agenthelm/agent-pack/1.0.0/runtime/agenthelm.py",
        "/home/ann/src/agenthelm/plugins/agent-pack/runtime/agenthelm.py",
        "C:\\Users\\Ann\\.cursor\\plugins\\local\\agent-pack\\runtime\\agenthelm.py",
        "/home/ann/npx-tools/agenthelm.py", "/home/ann/my_npx/agenthelm.py", "/home/ann/.npm/_cacache/x/agenthelm.py",
        "/home/ann/dlx-tools/agenthelm.py", "/home/ann/dlx/agenthelm.py", "/home/ann/bunx-notes/agenthelm.py",
    )

    def test_cache_paths_are_ephemeral_on_every_platform(self) -> None:
        for path in self.EPHEMERAL:
            with self.subTest(path=path):
                self.assertTrue(nudge.is_ephemeral_runtime(path))

    def test_installed_and_checkout_paths_are_stable(self) -> None:
        for path in self.STABLE:
            with self.subTest(path=path):
                self.assertFalse(nudge.is_ephemeral_runtime(path))


class InstallFromACacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        # a copy of the runtime where `npx` would unpack it, in a directory whose name has spaces
        self.cache_root = self.base / "my npm cache" / "_npx" / "0123abcd" / "node_modules" / "@scope" / "agenthelm" / "runtime" / "plugins"
        shutil.copytree(PLUGIN, self.cache_root / "agent-pack", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.ephemeral = self.cache_root / "agent-pack" / "runtime" / "agenthelm.py"
        self.target = self.home / ".cursor" / "hooks.json"
        self.target.parent.mkdir(parents=True)
        self.target.write_text(json.dumps({"version": 1, "hooks": {
            "afterFileEdit": [{"command": "./format.sh"}], "stop": list(BRIDGE), "beforeSubmitPrompt": list(BRIDGE)}}), encoding="utf-8")
        self.original = self.target.read_bytes()

    def run_hooks(self, *flags: str, script: Path | None = None, lang: str = "en") -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "AGENTHELM_LANG": lang}
        return subprocess.run([sys.executable, str(script or self.ephemeral), "nudge", "install-hooks", "--client", "cursor", *flags],
                              capture_output=True, text=True, env=env, check=False, timeout=60)

    def install_stable_plugin(self) -> Path:
        stable = self.home / ".cursor" / "plugins" / "local" / "agent-pack"
        shutil.copytree(PLUGIN, stable, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        return stable / "runtime" / "agenthelm.py"

    def commands(self, document: dict) -> list[str]:
        return [entry["command"] for event in ("sessionStart", "stop") for entry in document["hooks"].get(event, [])
                if "nudge hook" in entry["command"]]

    def test_the_cache_copy_really_is_treated_as_ephemeral(self) -> None:
        self.assertTrue(nudge.is_ephemeral_runtime(self.ephemeral))
        self.assertIn(" ", str(self.ephemeral))

    def test_preview_and_apply_point_at_the_installed_cursor_plugin(self) -> None:
        stable = self.install_stable_plugin()
        preview = self.run_hooks("--output", "json")
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        payload = json.loads(preview.stdout)
        self.assertEqual((payload["runtime"]["source"], payload["runtime"]["path"]), ("cursor-plugin", str(stable)))
        self.assertEqual(payload["runtime"]["ephemeral"], str(self.ephemeral))
        self.assertEqual(self.target.read_bytes(), self.original)  # preview writes nothing
        applied = self.run_hooks("--apply", "--output", "json")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        written = json.loads(self.target.read_text(encoding="utf-8"))
        commands = self.commands(written)
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertNotIn("_npx", command)
            self.assertEqual(shlex.split(command)[:2], ["python3", str(stable)])
        # the other entries are untouched
        self.assertEqual(written["hooks"]["afterFileEdit"], [{"command": "./format.sh"}])
        self.assertEqual(written["hooks"]["beforeSubmitPrompt"], BRIDGE)
        self.assertEqual(written["hooks"]["stop"][:3], BRIDGE)

    def test_the_runtime_install_directory_is_the_second_choice(self) -> None:
        current = self.home / ".local" / "share" / "agenthelm" / "runtimes" / "9.9.9"
        shutil.copytree(PLUGIN.parent / "agent-pack", current / "plugins" / "agent-pack", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        link = self.home / ".local" / "share" / "agenthelm" / "current"
        link.symlink_to(current)
        payload = json.loads(self.run_hooks("--output", "json").stdout)
        self.assertEqual(payload["runtime"]["source"], "runtime-current")
        self.assertEqual(payload["runtime"]["path"], str(link / "plugins" / "agent-pack" / "runtime" / "agenthelm.py"))  # the link, not a versioned directory
        self.install_stable_plugin()
        self.assertEqual(json.loads(self.run_hooks("--output", "json").stdout)["runtime"]["source"], "cursor-plugin")

    def test_without_a_stable_entry_the_preview_warns_and_apply_refuses(self) -> None:
        for lang in ("zh", "en"):
            preview = self.run_hooks(lang=lang)
            self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
            self.assertIn(str(self.ephemeral), preview.stdout)
            self.assertIn("agenthelm runtime install --apply", preview.stdout)
            self.assertNotIn("add --apply to do it", preview.stdout)
            refused = self.run_hooks("--apply", lang=lang)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertEqual(len(refused.stderr.strip().splitlines()), 1, refused.stderr)  # one line
            self.assertIn("_npx", refused.stderr)
            self.assertEqual(refused.stdout, "")
            self.assertEqual(self.target.read_bytes(), self.original)
            self.assertFalse(self.target.with_name("hooks.json.agenthelm-bak").exists())
        as_json = json.loads(self.run_hooks("--output", "json").stdout)
        self.assertIn("blocked", as_json)
        self.assertEqual(as_json["hooks"]["stop"], BRIDGE)  # nothing would be written

    def test_remove_does_not_need_a_stable_entry(self) -> None:
        stable = self.install_stable_plugin()
        self.assertEqual(self.run_hooks("--apply").returncode, 0)
        shutil.rmtree(stable.parents[1])  # the cache is all that is left
        removed = self.run_hooks("--remove", "--apply")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(document["hooks"]["stop"], BRIDGE)
        self.assertEqual(self.commands(document), [])

    def test_running_the_installed_script_is_unchanged(self) -> None:
        stable = self.install_stable_plugin()
        payload = json.loads(self.run_hooks("--output", "json", script=stable).stdout)
        self.assertEqual((payload["runtime"]["source"], payload["runtime"]["ephemeral"]), ("running-file", None))
        self.assertEqual(payload["runtime"]["path"], str(stable.resolve()))
        self.assertEqual(self.run_hooks(script=stable).returncode, 0)


if __name__ == "__main__":
    unittest.main()
