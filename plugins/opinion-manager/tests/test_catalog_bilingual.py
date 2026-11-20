"""PR4：公开规则与模板的双语字段与可选的 trigger / purpose / exclude（工具支持）；按项目语言选取文字；untranslated 警告；catalog 守卫。"""

from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "plugins/opinion-manager/runtime/opinion_manager.py"
PUBLIC_CATALOG = ROOT / "plugins/opinion-manager/catalog"
TEST_ROOT = ROOT / "__pycache__" / "opinion-test-work"
sys.path.insert(0, str(SCRIPT.parent))
import versioned  # noqa: E402

CJK = re.compile(r"[一-鿿　-〿＀-￯]")

# 存量：公开 catalog 里尚未提供 trigger / purpose / exclude 与 zh / en 双语文字的 1.0.0 发布。维护者改写内容时（升到更高版本）
# 同步从这里删除；新增的 1.0.0 发布若不在此表，必须自带全部字段与双语文字。
LEGACY_UNTRANSLATED = (
    "delivery-standard/compact-tables@1.0.0",
    "delivery-standard/list-first@1.0.0",
    "development-standard/chinese-maintenance@1.0.0",
    "development-standard/project-language@1.0.0",
    "om.align-requirements@1.0.0",
    "om.assumptions-authority@1.0.0",
    "om.batch-questions@1.0.0",
    "om.bilingual-terms@1.0.0",
    "om.calculations@1.0.0",
    "om.chinese-response@1.0.0",
    "om.clean-deliverable@1.0.0",
    "om.complete-options@1.0.0",
    "om.conclusion-first@1.0.0",
    "om.concrete-titles@1.0.0",
    "om.consistent-items@1.0.0",
    "om.device-readable@1.0.0",
    "om.editable-diagrams@1.0.0",
    "om.evidence-inference@1.0.0",
    "om.maintenance-chinese@1.0.0",
    "om.maintenance-project-language@1.0.0",
    "om.markdown-structure@1.0.0",
    "om.portable-files@1.0.0",
    "om.report-context@1.0.0",
    "om.report-formats@1.0.0",
    "om.report-review@1.0.0",
    "om.response-brief@1.0.0",
    "om.response-dense@1.0.0",
    "om.rules-tools@1.0.0",
    "om.semantic-wrap@1.0.0",
    "om.solutions-proportional@1.0.0",
    "om.solutions-three-level@1.0.0",
    "om.tables-compact@1.0.0",
    "om.tables-list-first@1.0.0",
    "om.test-evidence@1.0.0",
    "om.versioned-files@1.0.0",
    "response-style/brief-chinese@1.0.0",
    "response-style/dense-chinese@1.0.0",
    "scenario/concise-project@1.0.0",
    "scenario/reviewed-project@1.0.0",
    "work-method/proportional@1.0.0",
    "work-method/three-level@1.0.0",
)
THREE = ("trigger", "purpose", "exclude")


def bilingual(value: object) -> bool:
    return isinstance(value, dict) and {"zh", "en"} <= set(value) and all(value[key] for key in ("zh", "en"))


def release_items() -> list[tuple[str, str, dict]]:
    items = []
    for path in sorted(PUBLIC_CATALOG.rglob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        kind = "rules" if "id" in value else "templates"
        items.append((kind, versioned.release_ref(value, kind), value))
    return items


def complete(kind: str, value: dict) -> list[str]:
    """缺失项：规则需要三项字段与 title / text / scopes 的双语；模板需要 name / description / scopes 的双语。"""
    wanted = ("title", "text", "scopes", *THREE) if kind == "rules" else ("name", "description", "scopes")
    return [name for name in wanted if not bilingual(value.get(name))]


class PublicCatalogGuardTests(unittest.TestCase):
    def test_new_versions_are_complete_and_bilingual(self) -> None:
        problems = []
        for kind, ref, value in release_items():
            if versioned.version(value["version"]) > (1, 0, 0) or (ref not in LEGACY_UNTRANSLATED and value["version"] == "1.0.0"):
                if complete(kind, value):
                    problems.append(f"{ref}: 缺少 {complete(kind, value)}")
        self.assertEqual(problems, [], "高于 1.0.0 的规则与模板（以及不在存量白名单里的 1.0.0）必须带 trigger / purpose / exclude 与 zh / en 两种文字")

    def test_the_whitelist_only_lists_existing_legacy_items_and_shrinks(self) -> None:
        legacy = {ref: (kind, value) for kind, ref, value in release_items()}
        self.assertEqual(len(LEGACY_UNTRANSLATED), len(set(LEGACY_UNTRANSLATED)))
        for ref in LEGACY_UNTRANSLATED:
            self.assertIn(ref, legacy, f"{ref} 已不在 catalog 里，请从白名单删除")
            kind, value = legacy[ref]
            self.assertEqual(value["version"], "1.0.0", f"{ref} 已升版，请从白名单删除")
            self.assertTrue(complete(kind, value), f"{ref} 已经完整，请从白名单删除")

    def test_the_guard_detects_an_incomplete_new_version(self) -> None:
        rule = {"id": "demo.rule", "version": "1.1.0", "slot": "demo", "level": "required", "title": {"zh": "标题", "en": "Title"},
                "text": {"zh": "正文", "en": "Text"}, "scopes": {"zh": ["范围"], "en": ["scope"]}, "trigger": {"zh": "何时", "en": "when"}}
        self.assertEqual(complete("rules", rule), ["purpose", "exclude"])
        self.assertEqual(complete("rules", {**rule, "purpose": {"zh": "为什么", "en": "why"}, "exclude": {"zh": "无", "en": "none"}}), [])
        self.assertEqual(complete("rules", {**rule, "text": "只有中文"}), ["text", "purpose", "exclude"])


class BilingualCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()

    def command(self, *arguments: object, success: bool = True, language: str | None = None) -> dict:
        env = {**os.environ, **({"AGENTHELM_LANG": language} if language else {})}
        result = subprocess.run([sys.executable, str(SCRIPT), *(str(item) for item in arguments), "--output", "json"],
                                capture_output=True, text=True, check=False, env=env)
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def file(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def language(self, language: str) -> None:
        path = self.project / ".agents/moe.sakanano.agent-pack/project.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"language": language}), encoding="utf-8")

    def two(self, zh: object, en: object) -> dict:
        return {"zh": zh, "en": en}

    def rule(self, release: str = "1.0.0", *, identity: str = "fixture.alpha", slot: str = "fixture.slot", **extra) -> dict:
        base = {"id": identity, "version": release, "slot": slot, "level": "required",
                "title": self.two("标题甲", "Title Alpha"), "text": self.two("正文甲", "Text Alpha"),
                "scopes": self.two(["范围甲", "范围乙"], ["Scope A", "Scope B"]),
                "trigger": self.two("写报告时", "when writing reports"), "purpose": self.two("省时间", "save time"),
                "exclude": self.two("代码注释", "code comments")}
        return {**base, **extra}

    def legacy(self, release: str = "1.0.0", *, identity: str = "fixture.legacy", slot: str = "fixture.legacy") -> dict:
        return {"id": identity, "version": release, "slot": slot, "title": "旧标题", "text": "旧正文", "level": "preferred", "scopes": ["旧范围"]}

    def template(self, rules: list[str], *, family: str = "fixture", release: str = "1.0.0", **extra) -> dict:
        base = {"family": family, "variant": "alpha", "version": release, "name": self.two("模板甲", "Template Alpha"),
                "description": self.two("说明", "Description"), "scopes": self.two(["范围"], ["scope"]), "rules": rules}
        return {**base, **extra}

    def publish(self, value: dict, *, success: bool = True) -> dict:
        path = self.file("release.json", value)
        args = ("publish", "--catalog", self.catalog, "--file", path)
        preview = self.command(*args, success=success)
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"]) if success else preview

    def profile(self, *extra: object, language: str | None = None) -> dict:
        args = ("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0", *extra)
        preview = self.command(*args, language=language)
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], language=language)

    def seed(self) -> None:
        self.publish(self.rule())
        self.publish(self.legacy())
        self.publish(self.template(["fixture.alpha@1.0.0", "fixture.legacy@1.0.0"]))

    # —— 校验 ——
    def test_publish_accepts_bilingual_rules_templates_and_the_new_fields(self) -> None:
        self.assertEqual(self.publish(self.rule())["status"], "written")
        self.assertEqual(self.publish(self.template(["fixture.alpha@1.0.0"]))["status"], "written")
        self.assertEqual(self.publish(self.legacy())["status"], "written")
        self.assertEqual(self.publish(self.rule(identity="fixture.single", slot="fixture.single", title={"zh": "只有中文"}, trigger="旧写法的条件"))["status"], "written")

    def test_publish_rejects_malformed_localized_values(self) -> None:
        for field, value in (("title", {}), ("title", {"fr": "x"}), ("title", {"zh": " "}), ("text", {"zh": "x", "en": ""}),
                             ("scopes", {"zh": []}), ("scopes", {"zh": ["a", "a"]}), ("scopes", {"zh": "a"}), ("scopes", []),
                             ("trigger", ""), ("purpose", {"en": 1}), ("exclude", [])):
            with self.subTest(field=field, value=value):
                result = self.publish(self.rule(**{field: value}), success=False)
                self.assertFalse(result["ok"])
        self.assertFalse(self.publish({**self.rule(), "unknown": "x"}, success=False)["ok"])
        self.assertFalse(self.publish(self.template(["fixture.alpha@1.0.0"], name={"zh": ""}), success=False)["ok"])

    def test_published_versions_stay_immutable_and_legacy_files_keep_their_fingerprint(self) -> None:
        legacy = self.legacy()
        before = versioned.digest(legacy)
        self.publish(legacy)
        stored = json.loads(next(self.catalog.rglob("1.0.0.json")).read_text(encoding="utf-8"))
        self.assertEqual((stored, versioned.digest(stored)), (legacy, before))
        self.assertFalse(self.publish({**legacy, "text": self.two("旧正文", "Old text")}, success=False)["ok"])

    def test_adding_a_translation_or_a_field_is_not_a_patch(self) -> None:
        self.publish(self.legacy())
        for change in ({"text": {"zh": "旧正文", "en": "Old text"}}, {"trigger": "写报告时"}):
            with self.subTest(change=change):
                self.assertFalse(self.publish({**self.legacy("1.0.1"), **change}, success=False)["ok"])
        self.assertEqual(self.publish({**self.legacy("1.1.0"), "trigger": "写报告时"})["status"], "written")

    # —— 渲染 ——
    def test_zh_project_selects_the_zh_text_and_renders_the_new_fields(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        preview = self.command("compose", "--project", self.project, "--profile", profile["path"])
        self.assertIn("## 标题甲\n\n规则：`fixture.alpha@1.0.0`；强制规则；适用：范围甲、范围乙；条件：写报告时；目的：省时间；不适用：代码注释。\n\n正文甲", preview["content"])
        self.assertIn("## 旧标题\n\n规则：`fixture.legacy@1.0.0`；偏好；适用：旧范围。\n\n旧正文", preview["content"])
        for key in ("untranslated", "warnings"):
            self.assertNotIn(key, preview)
            self.assertNotIn(key, profile)
        self.assertNotIn("language", preview["lock"])

    def test_en_project_selects_the_en_text_and_warns_about_untranslated_rules(self) -> None:
        self.seed()
        self.language("en")
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        self.assertEqual(profile["untranslated"], [{"ref": "fixture.legacy@1.0.0", "kind": "rule", "fields": ["title", "text", "scopes"]}])
        preview = self.command("compose", "--project", self.project, "--profile", profile["path"], language="en")
        self.assertIn("## Title Alpha\n\nRule: `fixture.alpha@1.0.0`; Required; Applies to: Scope A, Scope B; When: when writing reports; Purpose: save time; Not for: code comments.\n\nText Alpha", preview["content"])
        self.assertEqual(preview["untranslated"], profile["untranslated"])
        self.assertIn("fixture.legacy@1.0.0", preview["warnings"][0])
        self.assertEqual(preview["lock"]["language"], "en")
        legacy_block = preview["content"].split("## 旧标题")[1]
        self.assertNotRegex(preview["content"].replace("## 旧标题" + legacy_block, ""), CJK)

    def test_untranslated_warning_does_not_block_apply_and_verify(self) -> None:
        self.publish(self.legacy())
        self.publish(self.template(["fixture.legacy@1.0.0"]))
        self.language("en")
        profile = self.profile("--template", "fixture/alpha@1.0.0", language="en")
        applied = self.command("compose", "--project", self.project, "--profile", profile["path"])
        self.command("compose", "--project", self.project, "--profile", profile["path"], "--apply", "--confirm", applied["confirmation_sha256"])
        self.assertEqual(self.command("verify", "--project", self.project)["status"], "verified")
        self.assertEqual(versioned.untranslated({"sources": {"rules": [], "templates": []}, "overrides": {}}, "en"), [])

    def test_inbox_adopt_preview_passes_the_warning_through(self) -> None:
        self.publish(self.legacy())
        self.publish(self.template(["fixture.legacy@1.0.0"]))
        self.language("en")
        profile = self.profile("--template", "fixture/alpha@1.0.0", language="en")
        args = ("compose", "--project", self.project, "--profile", profile["path"])
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])
        inbox = lambda *items: subprocess.run([sys.executable, str(SCRIPT), "inbox", *items, "--project", str(self.project), "--lang", "en"],  # noqa: E731
                                              capture_output=True, text=True, check=False)
        added = json.loads(inbox("add", "--title", "Reply first", "--text", "Give the conclusion first", "--scope", "replies").stdout)
        result = json.loads(inbox("adopt", added["id"]).stdout)
        self.assertEqual([item["ref"] for item in result["untranslated"]], ["fixture.legacy@1.0.0"])
        self.assertIn("fixture.legacy@1.0.0", result["warnings"][0])

    def test_fully_bilingual_selection_has_an_empty_warning_in_en(self) -> None:
        self.publish(self.rule())
        self.publish(self.template(["fixture.alpha@1.0.0"]))
        self.language("en")
        profile = self.profile("--template", "fixture/alpha@1.0.0", language="en")
        self.assertEqual((profile["untranslated"], profile["warnings"]), ([], []))
        preview = self.command("compose", "--project", self.project, "--profile", profile["path"])
        self.assertNotRegex(preview["content"], CJK)

    def test_personal_overrides_are_not_reported_as_untranslated(self) -> None:
        self.publish(self.legacy())
        self.publish(self.template(["fixture.legacy@1.0.0"]))
        self.language("en")
        overrides = self.file("overrides.json", {"fixture.legacy": {"title": "My title", "text": "My text", "level": "preferred", "scopes": ["mine"]}})
        profile = self.profile("--template", "fixture/alpha@1.0.0", "--overrides-file", overrides, language="en")
        self.assertEqual(profile["untranslated"], [])

    def test_localized_override_values_are_accepted(self) -> None:
        self.publish(self.legacy())
        self.publish(self.template(["fixture.legacy@1.0.0"]))
        overrides = self.file("overrides.json", {"fixture.legacy": {"title": self.two("我的", "Mine"), "text": self.two("我的正文", "My text"),
                                                                      "level": "preferred", "scopes": self.two(["我"], ["me"]), "trigger": "写报告时"}})
        profile = self.profile("--template", "fixture/alpha@1.0.0", "--overrides-file", overrides)
        self.assertIn("## 我的", self.command("compose", "--project", self.project, "--profile", profile["path"])["content"])

    def test_catalog_markdown_lists_bilingual_public_templates_in_the_interface_language(self) -> None:
        for language, expected in (("en", "## Evidence checks with three-level solutions `work-method/three-level@1.1.0`"),
                                   ("zh", "## 证据核验与三层方案 `work-method/three-level@1.1.0`")):
            result = subprocess.run([sys.executable, str(SCRIPT), "catalog", "--output", "markdown"],
                                    capture_output=True, text=True, check=False, env={**os.environ, "AGENTHELM_LANG": language})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)

    def test_the_lock_of_an_en_project_round_trips_through_verify(self) -> None:
        self.publish(self.rule())
        self.publish(self.template(["fixture.alpha@1.0.0"]))
        self.language("en")
        profile = self.profile("--template", "fixture/alpha@1.0.0", language="en")
        args = ("compose", "--project", self.project, "--profile", profile["path"])
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertEqual(self.command("verify", "--project", self.project)["status"], "verified")
        self.assertEqual(json.loads((self.project / "opinion.lock.json").read_text(encoding="utf-8"))["language"], "en")

    # —— 按需加载切片 ——
    def test_context_slices_follow_the_project_language(self) -> None:
        self.publish(self.rule())
        self.publish(self.rule(identity="fixture.beta", slot="fixture.beta", title=self.two("标题乙", "Title Beta"), text=self.two("正文乙", "Text Beta")))
        self.publish(self.template(["fixture.alpha@1.0.0"], family="base"))
        self.publish(self.template(["fixture.beta@1.0.0"], family="scene"))
        loading = self.file("loading.json", {"core": ["base"], "signals": {"business": ["scene", "none"]},
                                             "bundles": [{"family": "scene", "trigger": "TRIGGER", "exclude": "EXCLUDE", "routes": [{"business": ["scene"]}]}]})
        self.language("en")
        profile = self.profile("--template", "base/alpha@1.0.0", "--template", "scene/alpha@1.0.0", "--loading-file", loading, language="en")
        args = ("compose", "--project", self.project, "--profile", profile["path"])
        preview = self.command(*args, language="en")
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], language="en")
        core = subprocess.run([sys.executable, str(SCRIPT), "context", "core", "--project", str(self.project)], capture_output=True, text=True, check=False,
                              env={**os.environ, "AGENTHELM_LANG": "en"}).stdout
        self.assertIn("## Title Alpha", core)
        self.assertIn("Template Alpha", core)
        self.assertNotIn("标题甲", core)
        bundle = subprocess.run([sys.executable, str(SCRIPT), "context", "bundle", "scene", "--project", str(self.project)], capture_output=True, text=True, check=False,
                                env={**os.environ, "AGENTHELM_LANG": "en"}).stdout
        self.assertIn("## Title Beta", bundle)

    def test_schema_files_describe_the_localized_forms(self) -> None:
        references = SCRIPT.parents[1] / "references"
        rule = json.loads((references / "rule.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(rule["properties"]) - set(rule["required"]), versioned.RULE_OPTIONAL_FIELDS)
        self.assertEqual({"localizedText", "localizedScopes"}, set(rule["$defs"]))
        template = json.loads((references / "versioned-template.schema.json").read_text(encoding="utf-8"))
        for name in ("name", "description", "scopes"):
            self.assertIn("$ref", template["properties"][name])


if __name__ == "__main__":
    unittest.main()
