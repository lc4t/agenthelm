"""#80：update 输出随所选客户端措辞、阻塞时不再提示追加 --apply、预览与 apply 都写明备份位置。

HOME 是临时目录，客户端命令是桩脚本；不会运行真实的 claude / codex / cursor。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agenthelm_update_path as path_base  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins/agents-init"
CLI = REPO / "plugins/agent-pack/runtime/agenthelm.py"
LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
PACK = "agents-init/project-contract"
CJK = re.compile(r"[一-鿿]")


class BlockedPreviewTests(path_base.Fixture):
    def test_a_fully_blocked_preview_does_not_ask_for_apply(self) -> None:
        path_base.declare(self.home, path_base.TAG)
        path_base.register(self.home, path_base.OLD)
        for lang, blocked_text, advice in (("zh", "预览被阻塞", "追加 --apply"), ("en", "Preview blocked", "add --apply")):
            os.environ["AGENTHELM_LANG"] = lang
            code, text = self.run_text("update", "--client", "claude", "--no-project", "--only", "plugins")
            self.assertEqual(code, 0, text)
            self.assertIn(blocked_text, text)
            self.assertNotIn(advice, text)
            self.assertEqual(self.called(), [])

    def test_a_partly_blocked_preview_says_apply_runs_only_the_rest(self) -> None:
        path_base.declare(self.home, path_base.TAG)
        path_base.register(self.home, path_base.OLD)
        self.stub("codex")
        self.codex_plugin("1.0.0")
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("--apply 只执行未被阻塞的步骤", text)
        self.assertNotIn("确认后追加 --apply", text)

    def test_an_unblocked_preview_keeps_the_usual_ending(self) -> None:
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("以上为预览；确认后追加 --apply 执行。", text)


class ContentBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home, self.project = self.base / "home", self.base / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "AGENTHELM_LANG": "en"}
        done = subprocess.run([sys.executable, str(PLUGIN / "skills/agents-init/scripts/init_project.py"),
                               "--project", str(self.project), "--name", "Notes", "--project-type", "notes",
                               "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "none",
                               "--no-entry", "--language", "zh", "--apply", "--output", "json"],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lock = json.loads((self.project / LOCK).read_text())
        lock["packs"][PACK]["version"] = "0.0.0"
        (self.project / LOCK).write_text(json.dumps(lock), encoding="utf-8")
        profile = self.project / ".agents/moe.sakanano.agent-pack/project.json"
        data = json.loads(profile.read_text())
        data["language"] = "en"  # the unedited Chinese templates now differ from what is rendered: they are replaced
        profile.write_text(json.dumps(data), encoding="utf-8")
        sibling = json.loads((PLUGIN / "plugin.json").read_text())["version"]  # the runtime's own copy satisfies the release
        pack = next(p for p in json.loads((PLUGIN / "content.json").read_text())["packs"] if p["id"] == PACK)
        self.latest = self.base / "latest.json"
        self.latest.write_text(json.dumps({"schema": 1, "release": {"unavailable": True}, "migrations": [], "plugins": {
            "agents-init": {"version": sibling, "content": {PACK: {"version": pack["version"], "kind": pack["kind"]}}}}}),
            encoding="utf-8")
        self.sibling = sibling

    def update(self, *flags: str, lang: str = "en", output: str = "text") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), "update", "--only", "content", "--project", str(self.project),
                               "--latest", str(self.latest), "--output", output, *flags], capture_output=True, text=True,
                              env={**self.env, "AGENTHELM_LANG": lang}, timeout=60)

    def install_claude(self, version: str) -> None:
        path = self.home / ".claude/plugins/cache/agenthelm/agents-init" / version
        shutil.copytree(PLUGIN, path)
        manifest = json.loads((path / "plugin.json").read_text())
        manifest["version"] = version
        (path / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        registry = self.home / ".claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"version": 2, "plugins": {"agents-init@agenthelm": [
            {"scope": "user", "installPath": str(path), "version": version}]}}), encoding="utf-8")

    def test_a_selected_client_is_not_called_another_client(self) -> None:
        self.install_claude("0.0.1")
        for lang in ("zh", "en"):
            chosen = json.loads(self.update("--client", "claude", lang=lang, output="json").stdout)["steps"][0]["note"]
            self.assertIn("Claude Code 0.0.1", chosen)
            self.assertNotIn("其他客户端" if lang == "zh" else "other clients", chosen)
            self.assertIn("所选客户端" if lang == "zh" else "selected clients", chosen)
            if lang == "en":
                self.assertFalse(CJK.search(chosen), chosen)
        unspecified = json.loads(self.update(output="json").stdout)["steps"][0]["note"]
        self.assertIn("other clients", unspecified)  # without --client the old wording stands

    def test_preview_names_where_the_backup_will_go_and_apply_writes_it_there(self) -> None:
        default = self.home / ".agent-pack" / "recovery"
        preview = self.update()
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        flat = " ".join(preview.stdout.split())
        self.assertIn("will be backed up to", flat)
        self.assertIn(str(default), preview.stdout)
        self.assertIn("timestamp is fixed when it runs", flat)
        self.assertFalse(default.exists())  # a preview writes no backup
        exact = self.base / "my-backup"
        preview = self.update("--recovery-dir", str(exact))
        self.assertIn(str(exact), preview.stdout)
        self.assertNotIn("timestamp", preview.stdout)
        self.assertFalse(exact.exists())
        zh = self.update("--recovery-dir", str(exact), lang="zh")
        self.assertIn("替换前会把", zh.stdout)
        applied = self.update("--recovery-dir", str(exact), "--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn(str(exact), applied.stdout)
        self.assertTrue((exact / "AGENTS.md").is_file(), "the replaced files were backed up in the chosen folder")
        self.assertFalse(default.exists(), "the default recovery folder must stay untouched when --recovery-dir is given")
        self.assertFalse(CJK.search(applied.stdout), applied.stdout)

if __name__ == "__main__":
    unittest.main()
