"""模板声明 loading 与 profile 预览物化：校验、合并冲突、往返、--no-tags、1.0.0 夹具升级、catalog 输出、en 无中文、长路径。"""

from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

from test_tags import CJK, ROOT, RUNTIME, TagsBase, label, two

sys.path.insert(0, str(RUNTIME))

import versioned  # noqa: E402


def tag(name: str, zh: str, en: str) -> dict:
    return {"id": name, "name": label(zh, en), "trigger": label(f"{zh}时", f"When {en}"), "exclude": label("无", "None")}


class TemplateBase(TagsBase):
    """在 TagsBase 的 alpha..zeta 规则上再发布带 loading 的模板变体。"""

    def setUp(self) -> None:
        super().setUp()
        self.scene_loading = {
            "always": [],
            "tags": [tag("decisions", "方案", "options"), tag("files", "文件", "files"), tag("reports", "汇报", "reports")],
            "members": {"fixture.beta": ["decisions"], "fixture.gamma": ["decisions", "files"], "fixture.delta": ["files"], "fixture.epsilon": ["reports"]},
        }
        self.base_loading = {"always": ["fixture.alpha"], "tags": [tag("style", "风格", "style")], "members": {"fixture.zeta": ["style"]}}
        self.publish(self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], self.scene_loading))
        self.publish(self.template("base", "tagged", ["alpha", "zeta"], self.base_loading))
        self.scene = ("--template", "scene/tagged@1.0.0")
        self.both = ("--template", "base/tagged@1.0.0", "--template", "scene/tagged@1.0.0")

    def template(self, family: str, variant: str, names: list[str], loading: dict | None, release: str = "1.0.0") -> dict:
        value = {"family": family, "variant": variant, "version": release, "name": two(f"NAME_{family}", f"EN_NAME_{family}"),
                 "description": two("DESC", "EN_DESC"), "scopes": two(["SCOPE"], ["EN_SCOPE"]), "rules": [f"fixture.{name}@1.0.0" for name in names]}
        if loading is not None:
            value["loading"] = loading
        return value

    def reject_publish(self, value: dict, needle: str) -> None:
        result = self.command("publish", "--catalog", self.catalog, "--file", self.file("bad.json", value), code=2)
        self.assertIn(needle, result["error"], result)

    def saved_profile(self, saved: dict) -> dict:
        return json.loads(Path(saved["path"]).read_text(encoding="utf-8"))


class TemplateValidationTests(TemplateBase):
    def test_a_valid_template_loading_is_published_and_listed_by_catalog(self) -> None:
        listing = self.command("catalog", "--catalog", self.catalog)
        by_ref = {f"{item['family']}/{item['variant']}@{item['version']}": item for item in listing["templates"]}
        self.assertEqual(by_ref["scene/tagged@1.0.0"]["loading"], self.scene_loading)
        self.assertNotIn("loading", by_ref["scene/default@1.0.0"])

    def test_loading_takes_part_in_the_template_fingerprint(self) -> None:
        plain = self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], None)
        tagged = self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], self.scene_loading)
        self.assertNotEqual(versioned.digest(plain), versioned.digest(tagged))

    def test_invalid_template_declarations_are_rejected(self) -> None:
        names = ["beta", "gamma", "delta", "epsilon"]

        def broken(mutate) -> dict:
            loading = copy.deepcopy(self.scene_loading)
            mutate(loading)
            return self.template("scene", "bad", names, loading, release="2.0.0")

        self.reject_publish(broken(lambda v: v["members"].update({"fixture.alpha": ["decisions"]})), "不存在的规则")
        self.reject_publish(broken(lambda v: v["always"].append("fixture.alpha")), "不存在的规则")
        self.reject_publish(broken(lambda v: v["members"].update({"fixture.beta": ["nope"]})), "未知标签")
        self.reject_publish(broken(lambda v: v["tags"].append(tag("spare", "多余", "spare"))), "没有任何成员")
        self.reject_publish(broken(lambda v: v["tags"][0].update({"id": "方案"})), "ASCII")
        self.reject_publish(broken(lambda v: v["tags"][0].update({"id": "core"})), "保留名")
        self.reject_publish(broken(lambda v: v["tags"][0]["trigger"].pop("en")), "只含 zh、en")
        self.reject_publish(broken(lambda v: v.pop("members")), "字段必须为")

    def test_a_patch_version_cannot_change_the_loading_declaration(self) -> None:
        changed = copy.deepcopy(self.scene_loading)
        changed["tags"][0] = tag("decisions", "方案二", "options two")
        self.reject_publish(self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], changed, release="1.0.1"), "patch")
        self.reject_publish(self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], None, release="1.0.1"), "patch")
        self.publish(self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], changed, release="1.1.0"))


class MaterializationTests(TemplateBase):
    def test_preview_materializes_loading_and_apply_roundtrips_through_context(self) -> None:
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0", *self.scene)
        self.assertEqual(preview["profile"]["schema_version"], "2.2")
        self.assertEqual(preview["loading_source"], "template")
        self.assertTrue(preview["changes"]["loading_changed"])
        self.assertIsNone(preview["changes"]["loading"]["before"])
        self.assertEqual(preview["changes"]["loading"]["after"], preview["profile"]["loading"])
        self.assertEqual({item["id"] for item in preview["profile"]["loading"]["tags"]}, {"decisions", "files", "reports"})
        self.assertFalse((self.project / ".opinion").exists(), "预览不写任何文件")
        saved = self.profile(*self.scene)
        self.assertEqual(self.saved_profile(saved)["loading"], preview["profile"]["loading"])
        args = ("compose", "--project", self.project, "--profile", saved["path"])
        confirmation = self.command(*args)["confirmation_sha256"]
        self.command(*args, "--apply", "--confirm", confirmation)
        self.assertEqual(self.command("verify", "--project", self.project)["status"], "verified")
        index = json.loads(self.context("index").stdout)
        self.assertEqual(index["on_demand_tags"], 3)
        self.assertIn(self.block("beta"), self.context("select", "--tag", "decisions").stdout)
        self.assertEqual(self.context("check", "--tag", "files", "--loaded", "files").returncode, 0)

    def test_templates_without_loading_keep_full_reading(self) -> None:
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0", *self.templates)
        self.assertEqual(preview["profile"]["schema_version"], "2.0")
        self.assertNotIn("loading", preview["profile"])
        self.assertEqual(preview["loading_source"], "none")
        self.assertNotIn("loading_changed", preview["changes"])

    def test_no_tags_keeps_full_reading(self) -> None:
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0", *self.scene, "--no-tags")
        self.assertEqual(preview["profile"]["schema_version"], "2.0")
        self.assertNotIn("loading", preview["profile"])
        self.adopt(extra=("--no-tags",))
        self.assertEqual(self.context("core").returncode, 3)

    def test_no_tags_cannot_be_combined_with_a_loading_file(self) -> None:
        result = self.profile(*self.scene, "--no-tags", "--loading-file", self.file("loading.json", self.loading), code=2)
        self.assertIn("--no-tags", result["error"])

    def test_two_templates_merge_vocabularies_always_and_members(self) -> None:
        shared = tag("decisions", "方案", "options")
        self.scene_loading["tags"][0] = shared
        base = {"always": ["fixture.alpha"], "tags": [shared], "members": {"fixture.zeta": ["decisions"]}}
        self.publish(self.template("base", "merge", ["alpha", "zeta"], base))
        self.publish(self.template("scene", "tagged", ["beta", "gamma", "delta", "epsilon"], self.scene_loading, release="1.1.0"))
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0",
                               "--template", "base/merge@1.0.0", "--template", "scene/tagged@1.1.0")
        loading = preview["profile"]["loading"]
        self.assertEqual(loading["always"], ["fixture.alpha"])
        self.assertEqual([item["id"] for item in loading["tags"]], ["decisions", "files", "reports"])
        self.assertEqual(loading["members"]["fixture.zeta"], ["decisions"])
        self.assertEqual(loading["members"]["fixture.beta"], ["decisions"])

    def test_conflicting_tag_definitions_name_the_sources(self) -> None:
        clash = {"always": [], "tags": [tag("decisions", "决策", "decisions")], "members": {"fixture.zeta": ["decisions"]}}
        self.publish(self.template("base", "clash", ["alpha", "zeta"], clash))
        result = self.profile("--template", "base/clash@1.0.0", *self.scene, code=2)
        self.assertIn("标签定义冲突", result["error"])
        self.assertIn("decisions", result["error"])
        self.assertIn("base/clash@1.0.0", result["error"])
        self.assertIn("scene/tagged@1.0.0", result["error"])

    def test_rules_of_a_template_without_loading_stay_resident(self) -> None:
        overlap = {"always": [], "tags": [tag("pair", "成对", "pair")], "members": {"fixture.alpha": ["pair"], "fixture.beta": ["pair"]}}
        self.publish(self.template("scene", "overlap", ["alpha", "beta"], overlap))
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0",
                               "--template", "base/default@1.0.0", "--template", "scene/overlap@1.0.0")
        loading = preview["profile"]["loading"]
        self.assertIn("fixture.alpha", loading["always"], "没有声明的模板里的规则归常驻")
        self.assertNotIn("fixture.alpha", loading["members"])
        self.assertEqual(loading["members"], {"fixture.beta": ["pair"]})

    def test_directly_selected_rules_and_disabled_rules_follow_rule_ids(self) -> None:
        saved = self.profile(*self.scene, "--rule", "fixture.alpha@1.0.0")
        self.assertEqual(self.saved_profile(saved)["rules"], ["fixture.alpha@1.0.0"])
        self.assertNotIn("fixture.alpha", self.saved_profile(saved)["loading"]["members"], "直接选择的规则没有标签，保持常驻")
        overrides = self.file("overrides.json", {"fixture.epsilon": None})
        revised = self.profile(*self.scene, "--overrides-file", overrides, "--from-profile", saved["path"], release="1.0.1")
        profile = self.saved_profile(revised)
        self.assertEqual(profile["loading"], self.saved_profile(saved)["loading"], "模板选择不变，声明沿用")
        args = ("compose", "--project", self.project, "--profile", revised["path"])
        self.command(*args, "--apply", "--confirm", self.command(*args)["confirmation_sha256"])
        result = self.context("select", "--tag", "reports")
        self.assertEqual(result.returncode, 2)
        self.assertIn("没有有效规则", result.stdout + result.stderr)

    def test_a_hand_written_loading_is_not_merged_only_suggested(self) -> None:
        saved = self.profile(*self.templates, "--loading-file", self.file("loading.json", self.loading))
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.1",
                               "--from-profile", saved["path"], *self.scene)
        self.assertEqual(preview["profile"]["loading"], self.loading)
        self.assertEqual(preview["loading_source"], "inherited")
        self.assertEqual(preview["loading_suggestion"], versioned.template_loading(preview["profile"]["sources"]))
        self.assertTrue(any("loading_suggestion" in warning for warning in preview["warnings"]))
        self.assertFalse(preview["changes"]["loading_changed"])

    def test_loading_file_and_remove_loading_behave_as_before(self) -> None:
        saved = self.profile(*self.scene)
        removed = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.1",
                               "--from-profile", saved["path"], "--remove-loading")
        self.assertEqual(removed["profile"]["schema_version"], "2.0")
        self.assertTrue(removed["changes"]["loading_changed"])
        self.assertEqual(removed["loading_source"], "removed")


class UpgradeTests(TemplateBase):
    """从没有声明的 1.0.0 模板升到带声明的版本：updates -> profile 预览 -> compose 被手工修改阻断 -> 纳入后写入 -> verify。"""

    def test_upgrade_from_a_template_without_loading(self) -> None:
        names = ["beta", "gamma", "delta", "epsilon"]
        self.publish(self.template("scene", "default", names, self.scene_loading, release="1.1.0"))
        saved = self.adopt()
        updates = self.command("updates", "--catalog", self.catalog, "--profile", saved["path"])
        scene = next(item for item in updates["updates"] if item["current"] == "scene/default@1.0.0")
        self.assertIsNone(scene["loading"]["scene/default@1.0.0"])
        self.assertEqual(scene["loading"]["scene/default@1.1.0"]["tags"], ["decisions", "files", "reports"])
        opinion = self.project / "OPINION.md"
        edited = opinion.read_text(encoding="utf-8") + "\nMANUAL_NOTE\n"
        opinion.write_text(edited, encoding="utf-8")
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.1",
                               "--from-profile", saved["path"], "--template", "scene/default@1.1.0", "--custom-file", self.file("custom.md", "MANUAL_NOTE"))
        self.assertTrue(preview["changes"]["loading_changed"])
        self.assertIsNone(preview["changes"]["loading"]["before"])
        self.assertEqual(preview["profile"]["schema_version"], "2.2")
        revised = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.1",
                               "--from-profile", saved["path"], "--template", "scene/default@1.1.0", "--custom-file", self.root / "custom.md",
                               "--apply", "--confirm", preview["confirmation_sha256"])
        blocked = self.command("compose", "--project", self.project, "--profile", revised["path"])
        self.assertIn("人工修改", blocked["blocked"])
        self.command("compose", "--project", self.project, "--profile", revised["path"], "--apply", "--confirm", blocked["confirmation_sha256"], code=2)
        sha = hashlib.sha256(edited.encode("utf-8")).hexdigest()
        accepted = self.command("compose", "--project", self.project, "--profile", revised["path"], "--accept-current", sha)
        self.assertIsNone(accepted["blocked"])
        self.command("compose", "--project", self.project, "--profile", revised["path"], "--accept-current", sha, "--apply", "--confirm", accepted["confirmation_sha256"])
        self.assertEqual(self.command("verify", "--project", self.project)["status"], "verified")
        self.assertIn("MANUAL_NOTE", opinion.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.context("index").stdout)["on_demand_tags"], 3)

    def test_compare_shows_the_loading_change(self) -> None:
        saved = self.profile("--template", "scene/default@1.0.0")
        tagged = self.profile(*self.scene, "--from-profile", saved["path"], release="1.0.1")
        comparison = self.command("compare", "--from-profile", saved["path"], "--to-profile", tagged["path"])
        self.assertTrue(comparison["loading_changed"])
        self.assertIsNone(comparison["loading"]["before"])
        self.assertEqual(comparison["loading"]["after"]["always"], [])


class UntranslatedTests(unittest.TestCase):
    def test_untranslated_reports_tag_texts_without_a_translation(self) -> None:
        loading = {"always": [], "tags": [{"id": "only-zh", "name": {"zh": "仅中文"}, "trigger": {"zh": "触发"}, "exclude": {"zh": "排除", "en": "Exclude"}}], "members": {}}
        rule = {"id": "x.rule", "version": "1.0.0", "slot": "x.rule", "title": {"zh": "标题", "en": "Title"}, "text": {"zh": "文字", "en": "Text"}, "level": "required", "scopes": {"zh": ["范围"], "en": ["Scope"]}}
        profile = {"sources": {"templates": [], "rules": [{"ref": "x.rule@1.0.0", "value": rule}]}, "overrides": {}, "loading": loading}
        report = versioned.untranslated(profile, "en")
        self.assertEqual(report, [{"ref": "only-zh", "kind": "tag", "fields": ["name", "trigger"]}])
        self.assertEqual(versioned.untranslated(profile, "zh"), [])


class EnglishProjectTests(TemplateBase):
    language = "en"

    def test_english_project_has_no_chinese_and_no_untranslated(self) -> None:
        preview = self.command("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", "1.0.0", *self.scene)
        self.assertEqual(preview["untranslated"], [])
        saved = self.profile(*self.scene)
        args = ("compose", "--project", self.project, "--profile", saved["path"])
        self.command(*args, "--apply", "--confirm", self.command(*args)["confirmation_sha256"])
        for action in (("core",), ("select", "--tag", "decisions"), ("check", "--tag", "files", "--loaded", "files")):
            result = self.context(*action, language="en")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(CJK.search(result.stdout), result.stdout)
        self.assertIsNone(CJK.search(self.context("index", language="en").stdout))


class LongPathTests(TemplateBase):
    def test_materialization_end_to_end_with_a_very_long_path(self) -> None:
        long_project = self.root.joinpath(*("long-directory-segment-" + str(index) for index in range(6))) / "project"
        long_project.mkdir(parents=True)
        self.assertGreaterEqual(len(str(long_project)), 138)
        saved = self.profile(*self.scene, project=long_project)
        self.assertEqual(self.saved_profile(saved)["schema_version"], "2.2")
        args = ("compose", "--project", long_project, "--profile", saved["path"])
        self.command(*args, "--apply", "--confirm", self.command(*args)["confirmation_sha256"])
        self.assertEqual(self.command("verify", "--project", long_project)["status"], "verified")
        self.assertIn(self.block("beta"), self.context("select", "--tag", "decisions", project=long_project).stdout)


class PublicUpgradeTests(TagsBase):
    """用公开 catalog 的 1.0.0 场景模板组成夹具项目（含一处手工修改），升到带声明的 1.1.0。"""

    def cli(self, *arguments: object, code: int = 0) -> dict:
        result = self.run_cli(*arguments, "--output", "json")
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_scenario_upgrade_from_1_0_0_to_1_1_0(self) -> None:
        old = "scenario/reviewed-project@1.0.0"
        args = ("profile", "--project", self.project, "--id", "private.scene", "--version", "1.0.0", "--template", old)
        saved = self.cli(*args, "--apply", "--confirm", self.cli(*args)["confirmation_sha256"])
        compose = ("compose", "--project", self.project, "--profile", saved["path"])
        self.cli(*compose, "--apply", "--confirm", self.cli(*compose)["confirmation_sha256"])
        self.assertEqual(self.context("core").returncode, 3, "1.0.0 没有声明，完整读取")
        updates = self.cli("updates", "--profile", saved["path"])
        entry = next(item for item in updates["updates"] if item["kind"] == "templates")
        self.assertIn("scenario/reviewed-project@1.1.0", entry["available"])
        self.assertIsNone(entry["loading"][old])
        self.assertEqual(entry["loading"]["scenario/reviewed-project@1.1.0"]["tags"], ["decisions", "files", "reports", "maintenance"])
        opinion = self.project / "OPINION.md"
        edited = opinion.read_text(encoding="utf-8") + "\nMANUAL_NOTE\n"
        opinion.write_text(edited, encoding="utf-8")
        revise = ("profile", "--project", self.project, "--id", "private.scene", "--version", "1.0.1", "--from-profile", saved["path"],
                  "--template", "scenario/reviewed-project@1.1.0", "--custom-file", self.file("custom.md", "MANUAL_NOTE"))
        preview = self.cli(*revise)
        self.assertTrue(preview["changes"]["loading_changed"])
        self.assertEqual(preview["profile"]["schema_version"], "2.2")
        self.assertEqual(preview["loading_source"], "template")
        revised = self.cli(*revise, "--apply", "--confirm", preview["confirmation_sha256"])
        compose = ("compose", "--project", self.project, "--profile", revised["path"])
        self.assertIn("人工修改", self.cli(*compose)["blocked"])
        sha = hashlib.sha256(edited.encode("utf-8")).hexdigest()
        accepted = ("--accept-current", sha)
        self.cli(*compose, *accepted, "--apply", "--confirm", self.cli(*compose, *accepted)["confirmation_sha256"])
        self.assertEqual(self.cli("verify", "--project", self.project)["status"], "verified")
        index = json.loads(self.context("index").stdout)
        self.assertEqual(index["on_demand_tags"], 4)
        self.assertEqual(len(index["resident"]["rules"]), 10)
        self.assertEqual(index["resident"]["custom_lines"], 1)
        self.assertIn("MANUAL_NOTE", self.context("core").stdout)
        self.assertEqual(self.context("select", "--tag", "reports").returncode, 0)
        self.assertEqual(self.context("check", "--tag", "reports", "--loaded", "reports").returncode, 0)
        self.assertEqual(self.context("check", "--tag", "reports").returncode, 1)


class PublicCatalogTests(unittest.TestCase):
    """catalog 1.1.0 的两个场景模板：常驻集合、标签与规则覆盖。"""

    ALWAYS = ["om.chinese-response", "om.conclusion-first", "om.response-dense", "om.evidence-inference", "om.bilingual-terms",
              "om.markdown-structure", "om.batch-questions", "om.align-requirements", "om.clean-deliverable", "om.tables-list-first"]
    TAGS = {"decisions": ["om.calculations", "om.complete-options", "om.solutions-three-level", "om.assumptions-authority", "om.test-evidence"],
            "files": ["om.versioned-files", "om.concrete-titles", "om.portable-files", "om.consistent-items"],
            "reports": ["om.report-context", "om.report-formats", "om.report-review", "om.device-readable", "om.semantic-wrap", "om.editable-diagrams"],
            "maintenance": ["om.rules-tools", "om.maintenance-chinese"]}

    def setUp(self) -> None:
        self.catalog = versioned.load_catalog(versioned.PUBLIC_CATALOG)

    def template(self, variant: str, release: str = "1.1.0") -> dict:
        return self.catalog["templates"][f"scenario/{variant}@{release}"]

    def test_reviewed_project_declares_the_wide_resident_set_and_four_tags(self) -> None:
        loading = self.template("reviewed-project")["loading"]
        self.assertEqual(loading["always"], self.ALWAYS)
        grouped = {name: [identity for identity, names in loading["members"].items() if name in names] for name in self.TAGS}
        self.assertEqual({name: sorted(rules) for name, rules in grouped.items()}, {name: sorted(rules) for name, rules in self.TAGS.items()})

    def test_concise_project_swaps_the_four_variant_rules(self) -> None:
        loading = self.template("concise-project")["loading"]
        self.assertEqual(len(loading["always"]), 9)
        self.assertIn("om.response-brief", loading["always"])
        self.assertNotIn("om.tables-list-first", loading["always"])
        self.assertIn("files", loading["members"]["om.tables-compact"])
        self.assertIn("decisions", loading["members"]["om.solutions-proportional"])
        self.assertIn("maintenance", loading["members"]["om.maintenance-project-language"])

    def test_every_rule_is_either_resident_or_has_a_tag_and_at_most_six_tags(self) -> None:
        for variant in ("reviewed-project", "concise-project"):
            template = self.template(variant)
            loading = template["loading"]
            ids = {ref.split("@")[0] for ref in template["rules"]}
            self.assertEqual(set(loading["always"]) | set(loading["members"]), ids, variant)
            self.assertFalse(set(loading["always"]) & set(loading["members"]), variant)
            self.assertLessEqual(len(loading["tags"]), 6)

    def test_other_templates_and_1_0_0_files_declare_no_loading(self) -> None:
        for ref, template in self.catalog["templates"].items():
            if ref.endswith("@1.0.0") or not ref.startswith("scenario/"):
                self.assertNotIn("loading", template, ref)

    def test_preview_of_the_public_templates_materializes_loading(self) -> None:
        for variant in ("reviewed-project", "concise-project"):
            profile = versioned.resolve(self.catalog, [f"scenario/{variant}@1.1.0"], [])
            loading = versioned.template_loading(profile)
            self.assertEqual(loading, self.template(variant)["loading"])
        self.assertIsNone(versioned.template_loading(versioned.resolve(self.catalog, ["scenario/reviewed-project@1.0.0"], [])))


if __name__ == "__main__":
    unittest.main()
