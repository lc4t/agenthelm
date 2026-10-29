from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_meta", ROOT / "scripts/release_meta.py")
release_meta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_meta)


class ReleaseMetaTests(unittest.TestCase):
    def test_derived_files_are_in_sync(self) -> None:
        self.assertEqual(release_meta.main([]), 0)

    def test_marketplace_versions_come_from_each_plugin_manifest(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "agenthelm")
        entries = {entry["name"]: entry for entry in marketplace["plugins"]}
        roots = sorted(path.parent for path in (ROOT / "plugins").glob("*/plugin.json"))
        self.assertEqual(len(entries), len(roots))
        for root in roots:
            manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            # Claude Code decides updates from this entry version only, so it must never be blank.
            self.assertEqual(entries[manifest["name"]]["version"], manifest["version"])

    def test_installing_agents_init_brings_its_companions_in_claude_code(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        entries = {entry["name"]: entry for entry in marketplace["plugins"]}
        self.assertEqual(sorted(entries["agents-init"]["dependencies"]),
                         ["agent-pack", "opinion-manager", "project-orchestrator"])

    def test_marketplace_uses_sources_every_client_understands(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        for entry in marketplace["plugins"]:
            # Codex silently skips {"source": "github"} entries; relative paths work everywhere.
            self.assertIsInstance(entry["source"], str)
            self.assertTrue(entry["source"].startswith("./"))

    def test_latest_json_lists_every_plugin_version(self) -> None:
        latest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {name: item["version"] for name, item in latest["plugins"].items()},
            {entry["name"]: entry["version"] for entry in marketplace["plugins"]},
        )

    def test_hand_written_claude_mcp_config_expands_in_claude_code_and_cursor(self) -> None:
        for path in (ROOT / "plugins").glob("*/.mcp.json"):
            self.assertNotIn("${PLUGIN_ROOT}", path.read_text(encoding="utf-8"), path.as_posix())

    def test_content_packs_are_listed_in_latest_json(self) -> None:
        latest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        for path in (ROOT / "plugins").glob("*/content.json"):
            content = json.loads(path.read_text(encoding="utf-8"))
            plugin = path.parent.name
            for pack in content["packs"]:
                self.assertTrue(pack["files"], pack["id"])
                self.assertEqual(latest["plugins"][plugin]["content"][pack["id"]]["version"], pack["version"])

    def test_migration_entries_are_validated(self) -> None:
        bad = [{"plugin": "nope", "from": "1", "to": "1.0.0", "change_kind": "docs", "breaking": "no", "date": "x"}]
        with mock.patch.object(release_meta, "read_migrations", return_value=bad):
            problems = release_meta.migration_problems(release_meta.load_plugins())
        for expected in ("unknown plugin", "from must be", "change_kind", "breaking", "date", "summary"):
            self.assertTrue(any(expected in problem for problem in problems), expected)


ATTACH_SPEC = importlib.util.spec_from_file_location("attach_release", ROOT / "scripts/attach_release.py")
attach_release = importlib.util.module_from_spec(ATTACH_SPEC)
ATTACH_SPEC.loader.exec_module(attach_release)


class AttachReleaseTests(unittest.TestCase):
    def attach(self, tagged_versions: dict | None) -> dict:
        latest = {"plugins": {"agent-pack": {"version": "1.6.0"}}}
        release = {"tag": "v1", "tarball_sha256": "ab" * 32, "tarball_url": "https://example.invalid/a.tgz"}
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "latest.json"
            target.write_text(json.dumps(latest), encoding="utf-8")

            def fake_run(*command: str):
                if command[:3] == ("gh", "release", "download"):
                    Path(command[-1], "release.json").write_text(json.dumps(release), encoding="utf-8")
                    return ""
                if command[:2] == ("git", "show"):
                    return None if tagged_versions is None else json.dumps({"plugins": tagged_versions})
                raise AssertionError(command)

            with mock.patch.object(attach_release, "run", side_effect=fake_run), \
                    mock.patch("builtins.print"):
                attach_release.main(["attach", str(target)])
            return json.loads(target.read_text(encoding="utf-8"))

    def test_release_is_attached_only_when_the_tag_ships_the_listed_versions(self) -> None:
        self.assertEqual(self.attach({"agent-pack": {"version": "1.6.0"}})["release"]["tag"], "v1")
        self.assertNotIn("release", self.attach({"agent-pack": {"version": "1.5.1"}}))
        self.assertNotIn("release", self.attach(None))


class SinceGateTests(unittest.TestCase):
    """--since compares a work tree with a release tag in a throwaway repository."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.write("plugins/demo/plugin.json", {"name": "demo", "version": "1.0.0"})
        self.write("plugins/demo/templates/a.md", "one\n")
        self.write("plugins/demo/tests/test_demo.py", "")
        # A released content.json always carries the digests --write computed.
        self.write("plugins/demo/content.json", {"schema_version": 1, "packs": [{
            "id": "demo/templates", "version": "1.0.0", "kind": "template", "sources": ["templates"],
            "materializes": [], "project_lock": "agenthelm",
            "files": release_meta.pack_files(self.root / "plugins/demo", ["templates"])}]})
        self.patch = mock.patch.object(release_meta, "ROOT", self.root)
        self.patch.start()
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
        self.git("tag", "v1")

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, relative: str, data) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def problems(self) -> list[str]:
        return release_meta.since_problems("v1", release_meta.load_plugins())

    def test_tests_only_change_needs_no_bump(self) -> None:
        self.write("plugins/demo/tests/test_demo.py", "# more\n")
        self.assertEqual(self.problems(), [])

    def test_shipped_change_without_bump_fails(self) -> None:
        self.write("plugins/demo/templates/a.md", "two\n")
        problems = self.problems()
        self.assertTrue(any("without a version bump" in problem for problem in problems))
        self.assertTrue(any("demo/templates" in problem for problem in problems))

    def bump(self, **entry_changes) -> None:
        """A template change shipped as demo 1.0.1 with a complete declaration; tests then break one part."""
        self.write("plugins/demo/templates/a.md", "two\n")
        self.write("plugins/demo/plugin.json", {"name": "demo", "version": "1.0.1"})
        content = json.loads((self.root / "plugins/demo/content.json").read_text())
        content["packs"][0]["version"] = "1.0.1"
        content["packs"][0]["files"] = release_meta.pack_files(self.root / "plugins/demo", ["templates"])
        self.write("plugins/demo/content.json", content)
        entry = {"plugin": "demo", "from": "1.0.0", "to": "1.0.1", "change_kind": "content", "breaking": False,
                 "date": "2026-10-29", "summary": "template wording", "declaration": 2, "actions": [],
                 "user_action_required": [{"what": "update-content 逐个选择保留或采用", "session": "current"}],
                 "touches": ["a.md"], "default_for_existing": "advisory",
                 "template_changes": [{"path": "a.md", "class": "semantic"}]}
        entry.update(entry_changes)
        self.write("release/migrations.json", [{k: v for k, v in entry.items() if v is not None}])
        self.write("CHANGELOG.md", "# 更新记录\n\n### demo 1.0.1\n\n- 模板措辞\n")

    def test_bump_requires_a_migration_entry(self) -> None:
        self.write("plugins/demo/templates/a.md", "two\n")
        self.write("plugins/demo/plugin.json", {"name": "demo", "version": "1.0.1"})
        content = json.loads((self.root / "plugins/demo/content.json").read_text())
        content["packs"][0]["version"] = "1.0.1"
        self.write("plugins/demo/content.json", content)
        self.assertTrue(any("has no release/migrations.json entry" in p for p in self.problems()))

    def test_complete_declaration_passes(self) -> None:
        self.bump()
        self.assertEqual(self.problems(), [])
        self.assertEqual(release_meta.migration_problems(release_meta.load_plugins()), [])

    def test_entry_without_declaration_fails(self) -> None:
        self.bump(declaration=None)
        self.assertTrue(any('"declaration": 2' in p for p in self.problems()))

    def test_changelog_must_mention_the_new_version(self) -> None:
        self.bump()
        self.write("CHANGELOG.md", "# 更新记录\n\n### demo 1.0.0\n")
        self.assertTrue(any("CHANGELOG.md does not mention demo 1.0.1" in p for p in self.problems()))
        (self.root / "CHANGELOG.md").unlink()
        self.assertTrue(any("CHANGELOG.md does not mention" in p for p in self.problems()))

    def test_changelog_matches_hyphenated_names_written_with_spaces(self) -> None:
        self.bump()
        self.write("plugins/demo-pack/plugin.json", {"name": "demo-pack", "version": "2.0.0"})
        self.assertTrue(release_meta.changelog_mentions("demo", "1.0.1"))
        self.write("CHANGELOG.md", "## Demo Pack 2.0.0\n")
        self.assertTrue(release_meta.changelog_mentions("demo-pack", "2.0.0"))
        self.assertFalse(release_meta.changelog_mentions("demo-pack", "2.0.1"))

    def test_breaking_release_must_say_what_to_do(self) -> None:
        self.bump(breaking=True, actions=[])
        self.assertTrue(any("breaking release must list what to do" in p
                            for p in release_meta.migration_problems(release_meta.load_plugins())))

    def test_semantic_template_change_needs_a_user_action_unless_off_by_default(self) -> None:
        self.bump(user_action_required=[])
        self.assertTrue(any("semantic template change" in p for p in release_meta.migration_problems(release_meta.load_plugins())))
        self.bump(user_action_required=[], default_for_existing="off")
        self.assertEqual(release_meta.migration_problems(release_meta.load_plugins()), [])

    def test_changed_content_pack_must_list_template_changes(self) -> None:
        self.bump(template_changes=[])
        self.assertTrue(any("list the files in template_changes" in p for p in self.problems()))

    def test_declaration_fields_are_validated(self) -> None:
        for change, expected in ((dict(default_for_existing="maybe"), "default_for_existing"),
                                 (dict(touches="a.md"), "touches"),
                                 (dict(user_action_required=[{"what": ""}]), "user_action_required[0]"),
                                 (dict(user_action_required=[{"what": "x", "session": "later"}]), "session"),
                                 (dict(template_changes=[{"path": "a.md", "class": "big"}]), "template_changes[0]")):
            with self.subTest(change=change):
                self.bump(**change)
                self.assertTrue(any(expected in p for p in release_meta.migration_problems(release_meta.load_plugins())),
                                release_meta.migration_problems(release_meta.load_plugins()))

    def test_changing_what_projects_are_expected_to_contain_must_be_declared(self) -> None:
        self.write("plugins/demo/skills/agents-init/skeleton.json", '{"files": []}\n')
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1b")
        self.git("tag", "-f", "v1")
        self.bump(touches=[])
        self.write("plugins/demo/skills/agents-init/skeleton.json", '{"files": ["new"]}\n')
        problems = self.problems()
        self.assertTrue(any("skeleton.json changed" in p for p in problems), problems)
        self.bump(touches=["new"])
        self.assertFalse(any("skeleton.json changed" in p for p in self.problems()))


if __name__ == "__main__":
    unittest.main()
