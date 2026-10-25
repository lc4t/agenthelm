from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SiteReleaseTests(unittest.TestCase):
    def test_homepage_metadata_matches_plugin_authority(self) -> None:
        index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
        homepage = (ROOT / "index.html").read_text(encoding="utf-8")
        cli = json.loads((ROOT / "packages/cli/package.json").read_text(encoding="utf-8"))
        version = index["plugin"]["version"]  # the release version, shared with the git tag and npm
        updated_at = index["site"]["updated_at"]
        self.assertEqual(cli["version"], version)
        self.assertIn(f"Release {version}", homepage)
        self.assertIn(f"更新日期 {updated_at}", homepage)
        self.assertNotIn("main 分支", homepage)
        self.assertNotIn("正式域名", homepage)

    def test_pages_workflow_publishes_complete_install_unit(self) -> None:
        workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        for required in (
            "INSTALL.md",
            "llms.txt",
            "plugins",
            "scripts",
            "latest.json",
            ".claude-plugin/marketplace.json",
            "_site/skills/agents-init",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("runtime", workflow)
        # upload-pages-artifact v4+ drops dot-paths unless asked, which silently removed
        # .claude-plugin/ from the site (llms.txt and INSTALL.md link to it).
        upload = workflow.split("actions/upload-pages-artifact@", 1)[1].split("\n      - ", 1)[0]
        self.assertIn("include-hidden-files: true", upload)

    def test_bundled_plugins_match_manifests(self) -> None:
        index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
        indexed = {item["id"]: item for item in index["skills"]}
        for name in ("agents-init", "project-orchestrator", "agent-pack", "opinion-manager"):
            root = ROOT / "plugins" / name
            manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            adapter = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], name)
            self.assertEqual(adapter["version"], manifest["version"])
            self.assertEqual(indexed[name]["version"], manifest["version"])
            self.assertEqual(manifest["repository"], "https://github.com/lc4t/agenthelm")
            self.assertTrue((root / "skills" / name / "SKILL.md").is_file())
        initializer = json.loads((ROOT / "plugins/agents-init/skills/agents-init/skill.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "plugins/agents-init/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(initializer["version"], manifest["version"])
        self.assertEqual(indexed["agents-init"]["version"], manifest["version"])
        self.assertEqual(initializer["privacy"]["public_opinion_templates_included"],
                         any((ROOT / "plugins/opinion-manager/catalog/templates").rglob("*.json")))

    def test_no_project_runtime_left_in_distribution(self) -> None:
        self.assertFalse((ROOT / "skills").exists())
        self.assertFalse((ROOT / "runtime").exists())
        for path in ("plugins/agents-init/plugin.json", "plugins/agents-init/.codex-plugin/plugin.json", "index.json", "llms.txt"):
            self.assertNotIn("project-runtime", (ROOT / path).read_text(encoding="utf-8"), path)

    def test_versioned_catalog_dependencies_and_legacy_directory(self) -> None:
        template_root = ROOT / "plugins/opinion-manager/templates"
        self.assertEqual(
            [path.name for path in template_root.iterdir() if path.name != ".gitkeep"],
            [],
        )
        schema = json.loads(
            (ROOT / "plugins/opinion-manager/references/template.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["title"], "Opinion template")
        versioned_root = ROOT / "plugins/opinion-manager/catalog"
        rules = {f"{item['id']}@{item['version']}": item
                 for path in (versioned_root / "rules").rglob("*.json")
                 for item in [json.loads(path.read_text(encoding="utf-8"))]}
        templates = [json.loads(path.read_text(encoding="utf-8"))
                     for path in (versioned_root / "templates").rglob("*.json")]
        self.assertTrue(rules)
        self.assertTrue(templates)
        for item in templates:
            self.assertTrue(item["rules"])
            self.assertTrue(set(item["rules"]).issubset(rules))
        self.assertTrue((versioned_root / "README.md").is_file())
        for directory in ("rules", "templates"):
            self.assertTrue((versioned_root / directory / ".gitkeep").is_file())


    def test_active_opinion_entrypoints_match_available_catalog(self) -> None:
        paths = ("README.md", "INSTALL.md", "llms.txt", "index.json", "index.html",
                 "plugins/opinion-manager/skills/opinion-manager/SKILL.md",
                 "plugins/opinion-manager/references/versioning.md")
        for path in paths:
            text = (ROOT / path).read_text(encoding="utf-8")
            for obsolete in ("公开模板目录当前为空", "公开目录当前为空",
                             "公开规则和模板目录保持空白", "公开目录当前没有任何",
                             "本次分发包保持公开目录为空"):
                self.assertNotIn(obsolete, text, path)
        skill = (ROOT / paths[-2]).read_text(encoding="utf-8")
        self.assertNotIn("从模板的 `questions` 字段选择", skill)


if __name__ == "__main__":
    unittest.main()
