from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("init_project.py")
sys.path.insert(0, str(SCRIPT.parent))
import init_project  # noqa: E402


class InitProjectTests(unittest.TestCase):
    def run_script(self, root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--project", str(root),
                "--name", "Example Project",
                "--project-type", "code,docs",
                "--vcs", "github",
                "--stack", "python,markdown",
                "--runtime", "local",
                "--agent-cli", "codex,cursor",
                "--output", "json",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_script(root)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["applied"])
            self.assertFalse((root / "AGENTS.md").exists())

    def test_work_mode_default_and_legacy(self):
        for mode in ('case-workspace', 'legacy'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                args = () if mode == 'case-workspace' else ('--work-mode', mode)
                result = self.run_script(root, '--apply', *args)
                self.assertEqual(result.returncode, 0, result.stdout)
                profile = json.loads((root / init_project.PROFILE_PATH).read_text())
                self.assertEqual(profile['work']['mode'], mode)
                self.assertEqual((root / 'cases/.gitkeep').exists(), mode == 'case-workspace')

    def test_migrate_preserves_custom_profile_and_explicit_mode(self):
        for mode in (None, 'case-workspace'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as recovery:
                root = Path(temporary)
                profile = {'schema_version': '1.0', 'name': 'Existing',
                           'runtime': {'commit_policy': 'explicit', 'custom': True},
                           'opinion': {'custom': 'keep'}, 'privacy': {'keep': True},
                           'work': {'task': {'root': 'tasks'}, 'case': {'root': 'cases', 'command': 'own-tool'}}}
                path = root / init_project.PROFILE_PATH
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(profile))
                args = () if mode is None else ('--work-mode', mode)
                result = self.run_script(root, '--mode', 'migrate', '--replace', str(init_project.PROFILE_PATH),
                                         '--recovery-dir', recovery, '--apply', *args)
                self.assertEqual(result.returncode, 0, result.stdout)
                updated = json.loads(path.read_text())
                self.assertEqual(updated['opinion'], profile['opinion'])
                self.assertEqual(updated['privacy'], profile['privacy'])
                self.assertEqual(updated['runtime']['custom'], True)
                self.assertEqual(updated['runtime']['commit_policy'], 'explicit')
                if mode is None:
                    self.assertEqual(updated['work'], profile['work'])
                    self.assertFalse((root / 'cases').exists())
                else:
                    self.assertEqual(updated['work']['legacy_task']['root'], 'tasks')
                    self.assertIsNone(updated['work']['task'])
                    self.assertEqual(updated['work']['case']['command'], 'own-tool')

    def test_apply_creates_v6_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_script(root, "--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = json.loads((root / ".agents/moe.sakanano.agent-pack/project.json").read_text())
            self.assertFalse((root / ".agents/moe.sakanano.project-runtime").exists())
            self.assertEqual(profile["schema_version"], "1.0")
            self.assertEqual(profile["initializer_version"], "6.5.0")
            self.assertEqual(profile["$schema"], "https://skill.sakanano.moe/skills/agents-init/project.schema.json")
            self.assertEqual(profile["runtime"]["skill"], "project-orchestrator")
            self.assertEqual(profile["runtime"]["plugin"], "project-orchestrator")
            self.assertEqual(profile["runtime"]["capability_manager"], "agent-pack")
            self.assertEqual(profile["runtime"]["distribution"], "marketplace")
            self.assertEqual(profile["opinion"]["provider"], "opinion-manager")
            self.assertEqual(profile["opinion"]["loading_mode"], "tiered")
            self.assertEqual(profile["memory"], {"provider": "project-orchestrator", "root": "memory",
                                                 "index_command": None, "check_command": None, "policy": None})
            self.assertTrue((root / "docs/drafts/.gitkeep").exists())
            self.assertTrue((root / "memory/.gitkeep").exists())
            payload = json.loads(result.stdout)
            self.assertEqual(payload["runtime"]["source"], "bundled")
            self.assertTrue(payload["runtime"]["lifecycle"].startswith("project-orchestrator@"))
            self.assertTrue(payload["runtime"]["capabilities"].startswith("agent-pack@"))
            self.assertTrue(payload["runtime"]["opinion"].startswith("opinion-manager@"))
            agents = (root / "AGENTS.md").read_text()
            self.assertIn("# Example Project — Agent 执行入口", agents)
            self.assertIn("project-orchestrator", agents)
            self.assertNotIn("project-runtime", agents)
            rules = (root / "AGENT.RULES.md").read_text()
            self.assertIn("# 项目专属规则", rules)
            for phrase in ("## 记忆", "写入去向", "不写入记忆", "召回", "客户端私有记忆", "收尾"):
                self.assertIn(phrase, rules)
            self.assertIn("`memory` 段声明的记忆索引", agents)
            self.assertIn("`fallback` 时完整读取 `OPINION.md`", agents)
            summary = (root / ".agent-doc/chat-summary.md").read_text()
            for heading in ("## 待确认假设", "## 未解决冲突", "## Opinion 演化候选"):
                self.assertIn(heading, summary)
            self.assertIn("只保存尚未处理的事项与指向权威位置的指针", summary)

    def test_apply_records_content_baseline_for_rendered_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_script(root, "--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            lock = json.loads((root / init_project.CONTENT_LOCK_PATH).read_text())
            entry = lock["packs"]["agents-init/project-contract"]
            pack = init_project.contract_pack()
            self.assertEqual(entry["version"], pack["version"])
            self.assertEqual(sorted(entry["files"]), sorted(pack["materializes"]))
            for relative, digest in entry["files"].items():
                self.assertEqual(hashlib.sha256((root / relative).read_bytes()).hexdigest(), digest)
            # Working documents the user fills in are not template content.
            self.assertNotIn(".agent-doc/progress.md", entry["files"])
            self.assertEqual(json.loads(result.stdout)["content_lock"]["path"], str(init_project.CONTENT_LOCK_PATH))

    def test_dry_run_writes_no_content_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = json.loads(self.run_script(root).stdout)
            self.assertEqual(payload["content_lock"]["pack"].split("@")[0], "agents-init/project-contract")
            self.assertFalse((root / init_project.CONTENT_LOCK_PATH).exists())

    def test_migrate_merges_baseline_only_for_rewritten_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            lock_path = root / init_project.CONTENT_LOCK_PATH
            before = json.loads(lock_path.read_text())["packs"]["agents-init/project-contract"]["files"]
            (root / "AGENTS.md").write_text("edited\n", encoding="utf-8")
            result = self.run_script(root, "--mode", "migrate", "--replace", "AGENT.md",
                                     "--recovery-dir", str(Path(backup) / "r"), "--apply")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            after = json.loads(lock_path.read_text())["packs"]["agents-init/project-contract"]["files"]
            # AGENTS.md was preserved, so its install-time baseline must survive the user's edit.
            self.assertEqual(after["AGENTS.md"], before["AGENTS.md"])
            self.assertEqual(after["AGENT.md"], hashlib.sha256((root / "AGENT.md").read_bytes()).hexdigest())

    def refresh(self, root: Path, *extra: str, template_suffix: str = "\n<!-- v1.1 -->\n") -> tuple[int, dict]:
        """Run --mode refresh in-process against a newer AGENTS.md template and pack version."""
        templates = init_project.load_templates()
        templates["AGENTS.md"] += template_suffix
        pack = {**init_project.contract_pack(), "version": "1.1.0"}
        output = io.StringIO()
        with mock.patch.object(init_project, "load_templates", return_value=templates), \
                mock.patch.object(init_project, "contract_pack", return_value=pack), \
                mock.patch("sys.stdout", output):
            code = init_project.main(["--project", str(root), "--mode", "refresh", "--output", "json", *extra])
        return code, json.loads(output.getvalue())

    def lock_entry(self, root: Path) -> dict:
        return json.loads((root / init_project.CONTENT_LOCK_PATH).read_text())["packs"]["agents-init/project-contract"]

    def test_refresh_replaces_clean_files_after_backup_and_bumps_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            original = (root / "AGENTS.md").read_text()
            code, preview = self.refresh(root)
            self.assertEqual(code, 0)
            actions = {item["path"]: (item["state"], item["action"]) for item in preview["files"]}
            self.assertEqual(actions["AGENTS.md"], ("clean", "replace"))
            self.assertEqual(actions["AGENT.md"], ("unchanged", "keep"))
            self.assertIn("+<!-- v1.1 -->", next(i for i in preview["files"] if i["path"] == "AGENTS.md")["diff"])
            self.assertEqual((root / "AGENTS.md").read_text(), original)  # dry-run writes nothing
            code, applied = self.refresh(root, "--apply", "--recovery-dir", str(Path(backup) / "r"))
            self.assertTrue(applied["applied"], applied)
            self.assertTrue((root / "AGENTS.md").read_text().endswith("<!-- v1.1 -->\n"))
            self.assertEqual((Path(backup) / "r/AGENTS.md").read_text(), original)
            entry = self.lock_entry(root)
            self.assertEqual(entry["version"], "1.1.0")
            self.assertEqual(entry["files"]["AGENTS.md"], hashlib.sha256((root / "AGENTS.md").read_bytes()).hexdigest())

    def test_refresh_blocks_on_local_edits_until_the_user_chooses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            (root / "AGENTS.md").write_text("my own rules\n", encoding="utf-8")
            old_baseline = self.lock_entry(root)["files"]["AGENTS.md"]
            code, blocked = self.refresh(root, "--apply", "--recovery-dir", str(Path(backup) / "a"))
            self.assertEqual(code, 1)
            self.assertEqual(blocked["blocked"], ["AGENTS.md"])
            self.assertFalse(blocked["applied"])
            self.assertEqual((root / "AGENTS.md").read_text(), "my own rules\n")
            code, kept = self.refresh(root, "--apply", "--keep", "AGENTS.md")
            self.assertTrue(kept["applied"])
            self.assertEqual((root / "AGENTS.md").read_text(), "my own rules\n")
            entry = self.lock_entry(root)
            self.assertEqual(entry["version"], "1.1.0")
            self.assertEqual(entry["files"]["AGENTS.md"], old_baseline)

    def test_refresh_leaves_edits_alone_when_the_template_output_did_not_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            (root / "AGENT.RULES.md").write_text("project rules of my own\n", encoding="utf-8")
            code, preview = self.refresh(root)
            self.assertEqual(code, 0, preview)
            states = {item["path"]: (item["state"], item["action"]) for item in preview["files"]}
            self.assertEqual(states["AGENT.RULES.md"], ("local-only", "keep"))
            self.assertEqual(preview["blocked"], [])

    def test_refresh_accept_replaces_an_edited_file_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            (root / "AGENTS.md").write_text("my own rules\n", encoding="utf-8")
            code, applied = self.refresh(root, "--apply", "--accept", "AGENTS.md",
                                         "--recovery-dir", str(Path(backup) / "r"))
            self.assertEqual(code, 0, applied)
            self.assertEqual((Path(backup) / "r/AGENTS.md").read_text(), "my own rules\n")
            self.assertIn("<!-- v1.1 -->", (root / "AGENTS.md").read_text())

    def test_refresh_without_a_lock_treats_differences_as_local_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            (root / init_project.CONTENT_LOCK_PATH).unlink()
            code, preview = self.refresh(root)
            self.assertEqual(code, 1)
            self.assertEqual(preview["blocked"], ["AGENTS.md"])
            self.assertIsNone(preview["from_version"])

    def test_refresh_rejects_untracked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.run_script(root, "--apply").returncode, 0)
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                code = init_project.main(["--project", str(root), "--mode", "refresh", "--output", "json",
                                          "--accept", ".agent-doc/progress.md"])
            self.assertEqual(code, 2)
            self.assertIn("不是内容包跟踪的文件", json.loads(output.getvalue())["error"])

    def test_profile_schema_declares_every_generated_top_level_field(self) -> None:
        schema = json.loads((SCRIPT.parents[1] / "project.schema.json").read_text(encoding="utf-8"))
        inputs = init_project.Inputs(Path("."), "Example", "example", ("code",), "github", ("python",), "local", ("codex",))
        profile = json.loads(init_project.files_for(inputs)[init_project.PROFILE_PATH])
        self.assertLessEqual(set(profile), set(schema["properties"]))
        self.assertLessEqual(set(schema["required"]), set(profile))
        self.assertNotIn("memory", schema["required"])
        self.assertIn(profile["memory"]["provider"], schema["properties"]["memory"]["properties"]["provider"]["enum"])
        self.assertIn(profile["opinion"]["loading_mode"], schema["properties"]["opinion"]["properties"]["loading_mode"]["enum"])

    def test_generated_markdown_uses_named_chinese_template_blocks(self) -> None:
        templates = init_project.load_templates()
        inputs = init_project.Inputs(
            Path("."), "示例项目", "example", ("代码",), "github", ("python",), "本地", ("codex",)
        )
        files = init_project.files_for(inputs, templates)
        self.assertEqual(files[Path("AGENTS.md")], init_project.render_template(
            templates["AGENTS.md"],
            {
                "PROJECT_NAME": "示例项目",
                "PROJECT_SLUG": "example",
                "PROJECT_TYPE": "代码",
                "VCS": "github",
                "STACK": "python",
                "RUNTIME": "本地",
                "AGENT_CLI": "codex",
            },
        ))
        self.assertEqual(files[Path("docs/refs/README.md")], templates["docs/refs/README.md"])

    def make_companions(self, parent: Path, layout: str) -> Path:
        """Lay out agents-init plus its three companions the way a client stores them."""
        def plugin(root: Path, name: str, version: str) -> None:
            (root).mkdir(parents=True, exist_ok=True)
            (root / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
            for relative in init_project.BUNDLED_PLUGINS.get(name, ()):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text("x")
        if layout == "flat":
            plugin(parent / "agents-init", "agents-init", "7.0.0")
            for name in init_project.BUNDLED_PLUGINS:
                plugin(parent / name, name, "9.0.0")
            return parent / "agents-init"
        market = parent / "plugins/cache/agenthelm"
        plugin(market / "agents-init/7.0.0", "agents-init", "7.0.0")
        for name in init_project.BUNDLED_PLUGINS:
            plugin(market / name / "1.0.0", name, "1.0.0")
            plugin(market / name / "2.0.0", name, "2.0.0")
        if layout == "claude-cache":
            # Claude Code leaves the superseded 2.0.0 directory behind; its record points at 1.0.0.
            record = {"version": 2, "plugins": {
                f"{name}@agenthelm": [{"installPath": str(market / name / "1.0.0")}]
                for name in init_project.BUNDLED_PLUGINS}}
            (parent / "plugins/installed_plugins.json").write_text(json.dumps(record))
        return market / "agents-init/7.0.0"

    def test_companions_are_found_in_every_client_layout(self) -> None:
        for layout, expected in (("flat", "9.0.0"), ("codex-cache", "2.0.0"), ("claude-cache", "1.0.0")):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                agents_init = self.make_companions(Path(temporary), layout)
                status = init_project.validate_runtime(agents_init)
                self.assertEqual(status["source"], "external")
                self.assertEqual(status["capabilities"], f"agent-pack@{expected}")
                self.assertEqual(status["opinion"], f"opinion-manager@{expected}")

    def test_repository_root_is_accepted_as_runtime_root(self) -> None:
        repository = SCRIPT.parents[5]
        self.assertTrue(init_project.validate_runtime(repository)["available"])

    def test_missing_runtime_fails_before_any_project_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as incomplete:
            root = Path(temporary)
            result = self.run_script(
                root, "--runtime-plugin-root", incomplete, "--apply"
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["code"], "runtime-required")
            self.assertEqual(list(root.iterdir()), [])

    def test_apply_collision_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text("existing\n")
            result = self.run_script(root, "--apply")
            self.assertEqual(result.returncode, 1)
            self.assertEqual((root / "AGENTS.md").read_text(), "existing\n")
            payload = json.loads(result.stdout)
            self.assertIn("AGENTS.md", payload["preserved_collisions"])
            self.assertFalse(payload["applied"])
            self.assertFalse((root / ".agents/plugin.json").exists())

    def test_rejects_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / ".agents").symlink_to(Path(outside), target_is_directory=True)
            result = self.run_script(root, "--apply")
            self.assertEqual(result.returncode, 2)
            self.assertIn("symlinked parent", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_midway_failure_rolls_back_known_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {Path("one/file.txt"): "one\n", Path("two/file.txt"): "two\n"}
            original = init_project._write_exclusive
            calls = 0

            def fail_second(parent_fd: int, name: str, payload: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected failure")
                original(parent_fd, name, payload)

            with mock.patch.object(init_project, "_write_exclusive", side_effect=fail_second):
                with self.assertRaises(init_project.InitError):
                    init_project.apply_writes(root, files, files)
            self.assertFalse((root / "one/file.txt").exists())
            self.assertFalse((root / "two/file.txt").exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_invalid_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_script(Path(temporary), "--slug", "Bad Slug")
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)["ok"])

    def test_migrate_creates_missing_and_preserves_unselected_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text("legacy\n", encoding="utf-8")
            result = self.run_script(root, "--mode", "migrate", "--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["applied"])
            self.assertEqual((root / "AGENTS.md").read_text(), "legacy\n")
            self.assertTrue((root / ".agents/plugin.json").is_file())

    def test_migrate_replaces_only_selected_path_with_external_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup:
            root = Path(temporary)
            (root / "AGENTS.md").write_text("legacy\n", encoding="utf-8")
            recovery = Path(backup) / "migration"
            result = self.run_script(
                root,
                "--mode", "migrate",
                "--replace", "AGENTS.md",
                "--recovery-dir", str(recovery),
                "--apply",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual((root / "AGENTS.md").read_text(), "legacy\n")
            self.assertEqual((recovery / "AGENTS.md").read_text(), "legacy\n")

    def write_v5_profile(self, root: Path) -> Path:
        legacy = root / ".agents/moe.sakanano.project-runtime/project.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({
            "schema_version": "1.0",
            "initializer_version": "5.0.0",
            "name": "Legacy",
            "profile": {"project_type": ["docs"], "vcs": "github", "stack": ["python"], "runtime": "local", "agent_cli": ["codex"]},
            "runtime": {
                "skill": "project-runtime",
                "required": True,
                "session_bootstrap": "make brief",
                "commit_policy": "explicit",
                "push_policy": "explicit",
            },
            "opinion": {"provider": "opinion-workflow", "project_overlay": "OPINION.md", "strict_mode": "smart"},
            "capabilities": {
                "plugin_roots": [".agents", "vendor/plugins"],
                "plugin_dirs": [],
                "skill_roots": [".agents/skills"],
                "mcp_sources": [".agents/mcp.json"],
                "native_mcp_sources": ["config/mcp.jsonc"],
                "credential_env_file": ".env",
                "mcp_client_policy": {"tapd": {"exclude": ["claude"]}},
                "destination_skill_root": ".agents/skills",
                "destination_mcp": ".agents/mcp.json",
            },
            "work": {"task": {"root": "tasks"}},
            "privacy": {"forbidden_default_reads": [".env"], "generated_outputs": []},
        }), encoding="utf-8")
        return legacy

    def test_migrate_upgrades_v5_profile_and_retires_it_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup:
            root = Path(temporary)
            legacy = self.write_v5_profile(root)
            original = legacy.read_text(encoding="utf-8")
            recovery = Path(backup) / "migration"
            result = self.run_script(root, "--mode", "migrate", "--recovery-dir", str(recovery), "--apply")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["retire"], [".agents/moe.sakanano.project-runtime/project.json"])
            self.assertFalse(legacy.exists())
            self.assertFalse(legacy.parent.exists())
            self.assertEqual((recovery / ".agents/moe.sakanano.project-runtime/project.json").read_text(), original)
            profile = json.loads((root / ".agents/moe.sakanano.agent-pack/project.json").read_text())
            self.assertEqual(profile["initializer_version"], "6.5.0")
            self.assertEqual(profile["runtime"]["skill"], "project-orchestrator")
            self.assertEqual(profile["runtime"]["capability_manager"], "agent-pack")
            self.assertEqual(profile["runtime"]["session_bootstrap"], "make brief")
            self.assertEqual(profile["name"], "Legacy")
            self.assertEqual(profile["capabilities"]["plugin_roots"], [".agents", "vendor/plugins"])
            self.assertEqual(profile["capabilities"]["mcp_client_policy"], {"tapd": {"exclude": ["claude"]}})
            self.assertEqual(profile["work"], {"task": {"root": "tasks"}})
            self.assertEqual(profile["opinion"]["provider"], "opinion-workflow")

    def test_migrate_v5_profile_requires_recovery_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = self.write_v5_profile(root)
            result = self.run_script(root, "--mode", "migrate", "--apply")
            self.assertEqual(result.returncode, 2)
            self.assertTrue(legacy.exists())
            self.assertFalse((root / ".agents/moe.sakanano.agent-pack").exists())

    def test_migrate_skips_symlinked_skill_dir_but_still_upgrades_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup, \
                tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            self.write_v5_profile(root)
            (root / ".agents/skills").symlink_to(Path(outside), target_is_directory=True)
            result = self.run_script(
                root, "--mode", "migrate", "--recovery-dir", str(Path(backup) / "r"), "--apply"
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["skipped_symlinked_parents"], [".agents/skills/.gitkeep"])
            self.assertEqual(list(Path(outside).iterdir()), [])
            self.assertTrue((root / ".agents/skills").is_symlink())
            self.assertTrue((root / ".agents/moe.sakanano.agent-pack/project.json").is_file())

    def test_migrate_refuses_replace_through_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as backup, \
                tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / ".agents").mkdir()
            (root / ".agents/skills").symlink_to(Path(outside), target_is_directory=True)
            result = self.run_script(
                root, "--mode", "migrate", "--replace", ".agents/skills/.gitkeep",
                "--recovery-dir", str(Path(backup) / "r"), "--apply",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("symlinked parent", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_migrate_skip_omits_unwanted_skeleton_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_script(
                root, "--mode", "migrate",
                "--skip", "docs/refs/README.md", "--skip", "docs/drafts/.gitkeep", "--apply",
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse((root / "docs").exists())
            self.assertTrue((root / "AGENTS.md").is_file())
            self.assertEqual(
                json.loads(result.stdout)["skipped_by_request"],
                ["docs/drafts/.gitkeep", "docs/refs/README.md"],
            )

    def test_skip_rejected_in_init_mode_and_for_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init = self.run_script(root, "--skip", "docs/refs/README.md")
            self.assertEqual(init.returncode, 2)
            profile = self.run_script(
                root, "--mode", "migrate", "--skip", ".agents/moe.sakanano.agent-pack/project.json"
            )
            self.assertEqual(profile.returncode, 2)

    def test_init_mode_refuses_v5_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_v5_profile(root)
            result = self.run_script(root, "--apply")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "migrate-required")
            self.assertFalse((root / "AGENTS.md").exists())

    def test_migrate_replacement_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text("legacy\n", encoding="utf-8")
            result = self.run_script(
                root, "--mode", "migrate", "--replace", "AGENTS.md", "--apply"
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual((root / "AGENTS.md").read_text(), "legacy\n")


if __name__ == "__main__":
    unittest.main()
