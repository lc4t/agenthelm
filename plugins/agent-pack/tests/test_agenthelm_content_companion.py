"""Issue #48: content refresh gates the copy it runs, independently of other clients."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins/agents-init"
CLI = REPO / "plugins/agent-pack/runtime/agenthelm.py"
LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
PACK = "agents-init/project-contract"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ContentCompanionTests(unittest.TestCase):
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
                               "--no-entry", "--apply", "--output", "json"],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lock = json.loads((self.project / LOCK).read_text())
        lock["packs"][PACK]["version"] = "0.0.0"
        write(self.project / LOCK, lock)
        pack = next(p for p in json.loads((PLUGIN / "content.json").read_text())["packs"] if p["id"] == PACK)
        self.latest = self.base / "latest.json"
        # An invalid release block is deliberate: Cursor is unavailable and no network fallback is needed.
        write(self.latest, {"schema": 1, "release": {"unavailable": True}, "migrations": [],
                            "plugins": {"agents-init": {"version": "9.0.0", "content": {
                                PACK: {"version": pack["version"], "kind": pack["kind"]}}}}})

    def install(self, client: str, version: str) -> Path:
        if client == "codex":
            path = self.home / ".codex/plugins/cache/agenthelm/agents-init" / version
        elif client == "claude":
            path = self.home / ".claude/plugins/cache/agenthelm/agents-init" / version
            write(self.home / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {
                "agents-init@agenthelm": [{"scope": "user", "installPath": str(path), "version": version}]}})
        else:
            path = self.home / ".cursor/plugins/local/agents-init"
        shutil.copytree(PLUGIN, path)
        manifest = json.loads((path / "plugin.json").read_text())
        manifest["version"] = version
        write(path / "plugin.json", manifest)
        return path

    def update(self, *flags: str, lang: str = "en") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), "update", "--only", "content", "--project", str(self.project),
                               "--latest", str(self.latest), "--output", "json", *flags], capture_output=True, text=True,
                              env={**self.env, "AGENTHELM_LANG": lang}, timeout=30)

    def test_newest_copy_previews_and_applies_despite_an_older_client(self) -> None:
        current = self.install("codex", "9.0.0")
        self.install("claude", "8.0.0")
        lock = json.loads((self.project / LOCK).read_text())
        lock["packs"][PACK]["files"].pop("AGENTS.md", None)
        write(self.project / LOCK, lock)
        agents = self.project / "AGENTS.md"
        agents.write_text(agents.read_text() + "\nLocal instructions\n", encoding="utf-8")
        for flags in ((), ("--merge", "AGENTS.md"), ("--diff", "AGENTS.md"), ("--keep", "AGENTS.md", "--apply")):
            with self.subTest(flags=flags):
                done = self.update(*flags)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                step = json.loads(done.stdout)["steps"][0]
                self.assertNotIn("blocker", step)
                self.assertIn("result", step)
                self.assertIn(str(current), step["note"])
                self.assertIn("Claude Code 8.0.0", step["note"])

    def test_unavailable_cursor_does_not_block_the_current_copy(self) -> None:
        self.install("codex", "9.0.0")
        self.install("cursor", "8.0.0")
        done = self.update("--keep", "AGENTS.md", "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        step = json.loads(done.stdout)["steps"][0]
        self.assertEqual(step["status"], "applied")
        self.assertIn("Cursor 8.0.0", step["note"])

    def test_explicit_client_excludes_another_clients_newer_copy(self) -> None:
        excluded = self.install("codex", "9.0.0")
        current = self.install("claude", "8.0.0")
        for apply in (False, True):
            done = self.update("--client", "claude", *(["--apply"] if apply else []))
            self.assertEqual(done.returncode, 1 if apply else 0, done.stdout + done.stderr)
            step = json.loads(done.stdout)["steps"][0]
            self.assertEqual(step["status"], "blocked")
            self.assertIn(str(current), step["blocked"])
            self.assertNotIn(str(excluded), step["blocked"])
            self.assertIn("--client claude", step["next"])
            self.assertNotIn("--client codex", step["next"])

    def test_ignored_client_is_excluded_and_explicit_selection_can_override_the_ignore(self) -> None:
        ignored = self.install("codex", "9.0.0")
        current = self.install("claude", "8.0.0")
        write(self.home / ".config/agenthelm/config.json", {"ignored_clients": ["codex"]})
        done = self.update("--apply")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        step = json.loads(done.stdout)["steps"][0]
        self.assertIn(str(current), step["blocked"])
        self.assertNotIn(str(ignored), step["blocked"])
        self.assertIn("--client claude", step["next"])
        overridden = self.update("--client", "codex", "--apply")
        self.assertEqual(overridden.returncode, 0, overridden.stdout + overridden.stderr)
        self.assertEqual(json.loads(overridden.stdout)["steps"][0]["status"], "applied")

    def test_ignored_sibling_client_does_not_reenter_as_a_runtime_fallback(self) -> None:
        sys.path.insert(0, str(CLI.parent))
        import agenthelm
        from unittest import mock
        ignored = self.install("cursor", "9.0.0")
        current = self.install("claude", "8.0.0")
        env = agenthelm.Env(self.home, self.env)
        with mock.patch.object(agenthelm, "PLUGIN_ROOT", ignored.parent / "agent-pack"):
            manager, installed = agenthelm.content_companion(env, {"clients": {"selected": ["claude"], "excluded": ["cursor", "codex"]}})
        self.assertEqual(manager, current)
        self.assertEqual([entry["client"] for entry in installed], ["claude"])

    def test_cursor_old_copy_recommends_an_updatable_client_or_the_runtime(self) -> None:
        cursor = self.install("cursor", "8.0.0")
        for other in (None, "claude"):
            if other:
                self.install(other, "7.0.0")
            done = self.update("--apply")
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            step = json.loads(done.stdout)["steps"][0]
            self.assertIn(str(cursor), step["blocked"])
            self.assertIn("runtime update --apply" if other is None else "update --only plugins --client claude", step["next"])
            self.assertNotIn("--client cursor", step["next"])
            self.assertNotIn("ignore-client", step["blocked"])

    def test_all_copies_old_blocks_every_named_file_entry_point_and_names_the_next_command(self) -> None:
        current = self.install("codex", "8.0.0")
        self.install("claude", "7.0.0")
        for flags, apply in ((("--merge", "AGENTS.md"), False), (("--diff", "AGENTS.md"), False),
                             (("--keep", "AGENTS.md", "--apply"), True)):
            with self.subTest(flags=flags):
                before = (self.project / LOCK).read_bytes()
                done = self.update(*flags)
                self.assertEqual(done.returncode, 1 if apply else 0, done.stdout + done.stderr)
                step = json.loads(done.stdout)["steps"][0]
                self.assertEqual((step["status"], step["blocker"]), ("blocked", "outdated-agents-init"))
                self.assertIn(str(current), step["blocked"])
                self.assertIn("update --only plugins --client codex", step["next"])
                self.assertNotIn("result", step)
                self.assertEqual((self.project / LOCK).read_bytes(), before)
        text = subprocess.run([sys.executable, str(CLI), "update", "--only", "content", "--project", str(self.project),
                               "--latest", str(self.latest), "--diff", "AGENTS.md"], capture_output=True, text=True,
                              env=self.env, timeout=30)
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)

    def test_all_copies_old_block_apply_and_name_the_selected_copy_and_next_command(self) -> None:
        current = self.install("codex", "8.0.0")
        self.install("claude", "7.0.0")
        for lang in ("en", "zh"):
            for apply in (False, True):
                with self.subTest(lang=lang, apply=apply):
                    before = (self.project / LOCK).read_bytes()
                    done = self.update(*(["--apply"] if apply else []), lang=lang)
                    self.assertEqual(done.returncode, 1 if apply else 0, done.stdout + done.stderr)
                    step = json.loads(done.stdout)["steps"][0]
                    self.assertEqual(step["status"], "blocked")
                    self.assertEqual(step["blocker"], "outdated-agents-init")
                    for text in ("Codex", "8.0.0", "9.0.0", str(current), "update --only plugins --client codex"):
                        self.assertIn(text, step["blocked"])
                    self.assertEqual((self.project / LOCK).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
