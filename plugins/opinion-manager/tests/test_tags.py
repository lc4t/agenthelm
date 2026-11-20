"""schema 2.2 标签选择引擎：校验、select / core / index / check、custom 行标签、2.0 与 2.1 的字节黄金测试。"""

from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # 断言中文文案；英文路径在 EnglishProjectTests 里用子进程环境单独验证

import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "plugins/opinion-manager/runtime"
SCRIPT = RUNTIME / "opinion_manager.py"
TEST_ROOT = ROOT / "__pycache__" / "opinion-test-work"
FALLBACK = "完整读取项目根目录 OPINION.md"
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
sys.path.insert(0, str(RUNTIME))

import inbox  # noqa: E402
import rule_lines  # noqa: E402
import versioned  # noqa: E402

# 在 dev（5c6d28e）上用同一夹具算出的 2.0 / 2.1 字节指纹；任何改动都不得使它们变化。
GOLDEN = {
    "2.0": {"profile": "7dd7607557df58fa7fd02b923304f2baab1ff7a0148648506b892c65eb79473d", "opinion": "a6c7a9b32bc26fe4de796245ccf743fa134bfd43a564860c49652f0f39e10fce", "lock": "73eb9c730453550e2585a23da6f22fd957afd489a520d5a701d7c95b841b28c8"},
    "2.1": {"profile": "fbf3c4db2c7f100c81968ed326a0aaaaa8e135360abc2a43b6db3e7c90a9de51", "opinion": "135062c17498f0f0bd1d9e54a8803c5fdcf8cd5b28ccd6ad9f9b33bf6eba19dc", "lock": "ee4acf5f99a19b287710de1f2947ba542243f88022c50f8910f65d52c7419679"},
}


def two(zh: object, en: object) -> dict:
    return {"zh": zh, "en": en}


def label(zh: str, en: str) -> dict:
    return two(zh, en)


class TagsBase(unittest.TestCase):
    language = "zh"

    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        if self.language == "en":
            config = self.project / ".agents/moe.sakanano.agent-pack/project.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"language": "en"}), encoding="utf-8")
        for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
            self.publish({"id": f"fixture.{name}", "version": "1.0.0", "slot": f"fixture.{name}",
                          "title": two(f"TITLE_{name.upper()}", f"EN_TITLE_{name.upper()}"),
                          "text": two(f"RULE_{name.upper()}", f"EN_RULE_{name.upper()}"),
                          "level": "required", "scopes": two(["SCOPE"], ["EN_SCOPE"])})
        for family, names in (("base", ["alpha", "zeta"]), ("scene", ["beta", "gamma", "delta", "epsilon"])):
            self.publish({"family": family, "variant": "default", "version": "1.0.0", "name": two(f"NAME_{family}", f"EN_NAME_{family}"),
                          "description": two("DESC", "EN_DESC"), "scopes": two(["SCOPE"], ["EN_SCOPE"]),
                          "rules": [f"fixture.{name}@1.0.0" for name in names]})
        self.templates = ("--template", "base/default@1.0.0", "--template", "scene/default@1.0.0")
        self.legacy = {"core": ["base"], "signals": {"business": ["scene", "none"]},
                       "bundles": [{"family": "scene", "trigger": "T", "exclude": "X", "routes": [{"business": ["scene"]}]}]}
        self.loading = {
            "always": ["fixture.alpha"],
            "tags": [
                {"id": "decisions", "name": label("方案", "Options"), "trigger": label("给方案时", "When proposing options"), "exclude": label("纯执行", "Pure execution")},
                {"id": "files", "name": label("文件", "Files"), "trigger": label("改文件时", "When editing files"), "exclude": label("只读", "Read only")},
                {"id": "reports", "name": label("汇报", "Reports"), "trigger": label("做汇报时", "When reporting"), "exclude": label("闲聊", "Chat")},
                {"id": "style", "name": label("风格", "Style"), "trigger": label("写作时", "When writing"), "exclude": label("无", "None")},
            ],
            "members": {"fixture.alpha": ["decisions", "style"], "fixture.beta": ["decisions"], "fixture.gamma": ["decisions", "files"],
                        "fixture.delta": ["files"], "fixture.epsilon": ["reports"]},
        }

    # ---- 命令行辅助 ----
    def run_cli(self, *arguments: object, language: str | None = None, project: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "AGENTHELM_LANG": language or self.language}
        return subprocess.run([sys.executable, str(SCRIPT), *(str(item) for item in arguments)], capture_output=True, text=True, check=False, env=env)

    def command(self, *arguments: object, code: int = 0) -> dict:
        result = self.run_cli(*arguments, "--output", "json")
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def file(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def publish(self, value: dict) -> None:
        args = ("publish", "--catalog", self.catalog, "--file", self.file("release.json", value))
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def profile(self, *extra: object, release: str = "1.0.0", code: int = 0, project: Path | None = None) -> dict:
        args = ("profile", "--catalog", self.catalog, "--project", project or self.project, "--id", "private.fixture", "--version", release, *extra)
        preview = self.command(*args, code=code)
        if code:
            return preview
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def adopt(self, loading: dict | None = None, *, custom: str | None = None, extra: tuple = (), project: Path | None = None) -> dict:
        project = project or self.project
        arguments = [*self.templates, *extra]
        if loading is not None:
            arguments += ["--loading-file", self.file("loading.json", loading)]
        if custom is not None:
            (self.root / "custom.md").write_text(custom, encoding="utf-8")
            arguments += ["--custom-file", self.root / "custom.md"]
        saved = self.profile(*arguments, project=project)
        args = ("compose", "--project", project, "--profile", saved["path"])
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])
        return saved

    def context(self, *arguments: object, project: Path | None = None, language: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_cli("context", *arguments, "--project", project or self.project, language=language)

    def block(self, name: str) -> str:
        return f"RULE_{name.upper()}" if self.language == "zh" else f"EN_RULE_{name.upper()}"


class GoldenTests(TagsBase):
    """2.0 与 2.1 的 Profile 文件、OPINION.md、锁在 dev 上的 SHA-256 固定为常量。"""

    def digests(self) -> dict[str, str]:
        profile = next((self.project / ".opinion/profiles").rglob("1.0.0.json"))
        return {"profile": hashlib.sha256(profile.read_bytes()).hexdigest(),
                "opinion": hashlib.sha256((self.project / "OPINION.md").read_bytes()).hexdigest(),
                "lock": hashlib.sha256((self.project / "opinion.lock.json").read_bytes()).hexdigest()}

    def test_schema_20_bytes_are_unchanged(self) -> None:
        self.assertEqual(self.adopt()["profile"]["schema_version"], "2.0")
        self.assertEqual(self.digests(), GOLDEN["2.0"])

    def test_schema_21_bytes_are_unchanged(self) -> None:
        self.assertEqual(self.adopt(self.legacy)["profile"]["schema_version"], "2.1")
        self.assertEqual(self.digests(), GOLDEN["2.1"])

    def test_schema_22_is_written_only_with_a_tag_declaration(self) -> None:
        self.assertEqual(self.adopt(self.loading)["profile"]["schema_version"], "2.2")
        saved = self.profile(*self.templates, "--remove-loading", release="1.0.1")
        self.assertEqual(saved["profile"]["schema_version"], "2.0")
        self.assertNotIn("loading", saved["profile"])


class ValidationTests(TagsBase):
    def reject(self, loading: object, needle: str = "") -> None:
        result = self.profile(*self.templates, "--loading-file", self.file("bad.json", loading), code=2)
        self.assertFalse(result["ok"])
        self.assertIn(needle, result.get("error", ""), result)

    def changed(self, mutate) -> dict:
        value = copy.deepcopy(self.loading)
        mutate(value)
        return value

    def test_invalid_declarations_are_rejected_with_a_reason(self) -> None:
        cases = [
            (self.changed(lambda v: v["always"].append("fixture.missing")), "loading.always"),
            (self.changed(lambda v: v["always"].append("fixture.alpha")), "重复"),
            (self.changed(lambda v: v["members"].update({"fixture.missing": ["files"]})), "loading.members"),
            (self.changed(lambda v: v["members"].update({"fixture.delta": ["nope"]})), "未知标签"),
            (self.changed(lambda v: v["members"].update({"fixture.delta": ["files", "files"]})), "重复"),
            (self.changed(lambda v: v["members"].update({"fixture.delta": []})), "字符串数组"),
            (self.changed(lambda v: v["tags"].append(copy.deepcopy(v["tags"][0]))), "重复"),
            (self.changed(lambda v: v["tags"][0].update({"id": "方案"})), "ASCII"),
            (self.changed(lambda v: v["tags"][0].update({"id": "Decisions"})), "ASCII"),
            (self.changed(lambda v: v["tags"][0].update({"id": "core"})), "保留名"),
            (self.changed(lambda v: v["tags"][0].update({"id": "x" * 33})), "ASCII"),
            (self.changed(lambda v: v["tags"][0].update({"name": "只有中文"})), "zh、en"),
            (self.changed(lambda v: v["tags"][0].update({"trigger": {"zh": "仅中文"}})), "zh、en"),
            (self.changed(lambda v: v["tags"][0].update({"exclude": {"zh": "a", "en": ""}})), "非空字符串"),
            (self.changed(lambda v: v["tags"][0].update({"extra": 1})), "字段必须为"),
            (self.changed(lambda v: v["tags"].append({"id": "orphan", "name": label("a", "a"), "trigger": label("a", "a"), "exclude": label("a", "a")})), "没有任何成员"),
            (self.changed(lambda v: v.update({"tags": []})), "至少包含一个标签"),
            (self.changed(lambda v: v.update({"core": ["base"]})), "字段必须为"),
            (self.changed(lambda v: v.pop("members")), "字段必须为"),
            ({**self.legacy, "tags": self.loading["tags"]}, "字段必须为"),
        ]
        for loading, needle in cases:
            with self.subTest(needle=needle, loading=json.dumps(loading, ensure_ascii=False)[:80]):
                self.reject(loading, needle)

    def test_tags_in_a_21_profile_need_schema_22(self) -> None:
        saved = self.adopt(self.loading)
        stored = copy.deepcopy(saved["profile"])
        stored["schema_version"] = "2.1"
        path = self.file("p21.json", stored)
        result = self.run_cli("context", "core", "--profile", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("schema_version 2.2", json.loads(result.stdout)["error"])

    def test_schema_22_without_loading_is_rejected(self) -> None:
        stored = copy.deepcopy(self.adopt(self.loading)["profile"])
        del stored["loading"]
        result = self.run_cli("context", "core", "--profile", self.file("p22.json", stored))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Profile 字段必须为", json.loads(result.stdout)["error"])

    def test_old_tool_reading_a_22_profile_gets_an_explicit_error(self) -> None:
        """旧工具（dev 5c6d28e）的校验：2.2 不在 2.0 / 2.1 的字段集合内，读取必须报错而不是静默读取。"""
        stored = self.adopt(self.loading)["profile"]
        legacy_fields = {"schema_version", "id", "version", "templates", "rules", "sources", "overrides", "custom", "derived_from"}

        def legacy_validate(value: dict) -> None:  # 逐行对应旧 validate_profile 开头的两道检查
            allowed = legacy_fields | ({"loading"} if value["schema_version"] == "2.1" else set())
            if set(value) != allowed and not (legacy_fields <= set(value) <= allowed):
                raise ValueError(f"Profile must have exactly these fields: {sorted(allowed)}")

        with self.assertRaisesRegex(ValueError, "Profile must have exactly these fields"):
            legacy_validate(stored)
        legacy_validate(self.profile(*self.templates, "--remove-loading", release="1.0.1")["profile"])  # 2.0 仍可读

    def test_disabled_rules_leave_a_tag_empty_and_unavailable_without_invalidating_the_profile(self) -> None:
        self.adopt(self.loading, extra=("--overrides-file", self.file("off.json", {"fixture.epsilon": None})))
        result = self.context("select", "--tag", "reports")
        self.assertEqual(result.returncode, 2)
        self.assertIn("没有有效规则", json.loads(result.stdout)["error"])
        index = json.loads(self.context("index").stdout)
        self.assertEqual({tag["id"]: tag["state"] for tag in index["tags"]}["reports"], "empty")
        self.assertEqual(index["on_demand_tags"], 2)
        self.assertNotIn("`reports`", self.context("core").stdout)


class SelectionTests(TagsBase):
    def setUp(self) -> None:
        super().setUp()
        self.adopt(self.loading)

    def test_core_keeps_resident_rules_and_indexes_the_selectable_tags(self) -> None:
        core = self.context("core").stdout
        for name in ("alpha", "zeta"):  # alpha 同时在 always 与标签里：归常驻；zeta 没有标签：常驻
            self.assertIn(self.block(name), core)
        for name in ("beta", "gamma", "delta", "epsilon"):
            self.assertNotIn(self.block(name), core)
        self.assertIn("- `decisions`（方案，2 条）：给方案时。不适用：纯执行。", core)
        self.assertIn("`files`（文件，2 条）", core)
        self.assertIn("`reports`（汇报，1 条）", core)
        self.assertNotIn("`style`", core)  # 成员全部常驻：不进索引
        self.assertIn("context select --tag", core)

    def test_select_returns_the_deduplicated_union_without_resident_rules(self) -> None:
        text = self.context("select", "--tag", "decisions", "--tag", "files", "--tag", "decisions").stdout
        for name in ("beta", "gamma", "delta"):
            self.assertEqual(text.count(self.block(name)), 1, name)
        for name in ("alpha", "zeta", "epsilon"):
            self.assertNotIn(self.block(name), text)
        self.assertLess(text.index("RULE_BETA"), text.index("RULE_GAMMA"))  # 保持 OPINION.md 顺序
        self.assertTrue(text.startswith("# 按需规则：`decisions`、`files`"))

    def test_core_plus_all_selections_reproduce_the_complete_text_exactly_once(self) -> None:
        complete = (self.project / "OPINION.md").read_text(encoding="utf-8")
        everything = self.context("core").stdout + self.context("select", "--tag", "decisions", "--tag", "files", "--tag", "reports").stdout
        for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
            rule_block = f"## TITLE_{name.upper()}\n\n规则：`fixture.{name}@1.0.0`；强制规则；适用：SCOPE。\n\nRULE_{name.upper()}"
            self.assertIn(rule_block, complete)
            self.assertEqual(everything.count(rule_block), 1, name)

    def test_selection_errors_are_explicit_and_keep_the_fallback(self) -> None:
        cases = [
            (("select",), "至少一个 --tag；有效标签：decisions、files、reports"),
            (("select", "--tag", "nope"), "未知标签：nope；有效标签：decisions、files、reports"),
            (("select", "--tag", "decisions", "--tag", "nope"), "未知标签：nope"),
            (("select", "--tag", "core"), "core 是常驻集合"),
            (("select", "--tag", "style"), "标签 style 的规则全部常驻"),
            (("select", "decisions"), "只接受 --tag"),
            (("bundle", "scene"), "context select --tag"),
            (("core", "--tag", "files"), "--tag 只用于 select 与 check"),
        ]
        for arguments, needle in cases:
            with self.subTest(arguments=arguments):
                result = self.context(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout)
                payload = json.loads(result.stdout)
                self.assertIn(needle, payload["error"])
                self.assertEqual(payload["fallback"], FALLBACK)

    def test_select_on_a_bundle_profile_explains_the_other_command(self) -> None:
        (self.root / "other").mkdir()
        self.adopt(self.legacy, project=self.root / "other")
        result = self.context("select", "--tag", "files", project=self.root / "other")
        self.assertEqual(result.returncode, 2)
        self.assertIn("context bundle", json.loads(result.stdout)["error"])
        self.assertEqual(self.context("bundle", "scene", project=self.root / "other").returncode, 0)

    def test_index_reports_counts_and_bytes_per_tag(self) -> None:
        index = json.loads(self.context("index").stdout)
        tags = {tag["id"]: tag for tag in index["tags"]}
        self.assertEqual((tags["decisions"]["rules"], tags["files"]["rules"], tags["reports"]["rules"]), (2, 2, 1))
        self.assertEqual(tags["decisions"]["rule_ids"], ["fixture.beta", "fixture.gamma"])
        self.assertEqual(tags["style"]["state"], "resident")
        self.assertEqual(index["on_demand_tags"], 3)
        self.assertEqual(index["resident"]["rules"], ["fixture.alpha", "fixture.zeta"])
        for name in ("decisions", "files", "reports"):
            self.assertEqual(tags[name]["bytes"], len(self.context("select", "--tag", name).stdout.encode("utf-8")))
            self.assertGreater(tags[name]["index_bytes"], 0)
        self.assertEqual(index["core_bytes"], len(self.context("core").stdout.encode("utf-8")))
        self.assertLess(index["resident_bytes"], index["core_bytes"])
        self.assertEqual(index["complete_bytes"], len((self.project / "OPINION.md").read_bytes()))

    def test_check_derives_required_rules_from_declared_tags(self) -> None:
        missing = self.context("check", "--tag", "decisions")
        self.assertEqual(missing.returncode, 1)
        payload = json.loads(missing.stdout)
        self.assertEqual((payload["missing"], payload["missing_rules"]), (["decisions"], ["fixture.beta", "fixture.gamma"]))
        self.assertIn("--tag decisions", payload["next"][0])
        self.assertEqual(self.context("check", "--tag", "decisions", "--loaded", "decisions").returncode, 0)
        self.assertEqual(self.context("check", "--signal", "touches=reports", "--loaded", "reports").returncode, 0)
        self.assertEqual(self.context("check", "--signal", "touches=reports", "--loaded", "files").returncode, 1)
        # 重叠：files 的 gamma 已被 decisions 读取，但 delta 没有
        partial = self.context("check", "--tag", "files", "--loaded", "decisions")
        self.assertEqual((partial.returncode, json.loads(partial.stdout)["missing_rules"]), (1, ["fixture.delta"]))
        self.assertEqual(self.context("check", "--tag", "files", "--loaded", "decisions", "--loaded", "files").returncode, 0)
        self.assertEqual(self.context("check", "--tag", "none").returncode, 0)

    def test_check_rejects_unusable_declarations(self) -> None:
        for arguments, needle in ((("check",), "至少需要一个"), (("check", "--tag", "nope"), "未知标签"),
                                  (("check", "--signal", "artifact=html"), "未声明的交付信号"),
                                  (("check", "--tag", "none", "--tag", "files"), "none 表示"),
                                  (("check", "--tag", "style"), "全部常驻")):
            with self.subTest(arguments=arguments):
                result = self.context(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn(needle, json.loads(result.stdout)["error"])

    def test_fallbacks_are_unchanged(self) -> None:
        switch = self.project / ".agents/moe.sakanano.agent-pack/project.json"
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text(json.dumps({"opinion": {"loading_mode": "full"}}), encoding="utf-8")
        self.assertEqual(self.context("select", "--tag", "files").returncode, 3)
        switch.unlink()
        (self.project / "OPINION.md").write_text("tampered\n", encoding="utf-8")
        self.assertEqual(self.context("core").returncode, 2)


class CustomLineTests(TagsBase):
    TAGGED = "- **CT**（适用：S）：CUSTOM_TAGGED [tags decisions]"
    PLAIN = "- **CP**（适用：S）：CUSTOM_PLAIN"
    TWO = "- **C2**（适用：S）：CUSTOM_TWO [tags files reports]"
    LINES = (TAGGED, PLAIN, TWO, "手写自由文本 HAND_FREE", "手写自由文本 HAND_TAGGED [tags decisions]",
             "- **CU**（适用：S）：CUSTOM_UNKNOWN [tags nope]", "- **CD**（适用：S）：CUSTOM_DAMAGED [tags Bad]")

    def setUp(self) -> None:
        super().setUp()
        self.adopt(self.loading, custom="\n\n".join(self.LINES))

    def test_tagged_lines_load_on_demand_and_everything_else_stays_resident(self) -> None:
        core = self.context("core").stdout
        for token in ("CUSTOM_PLAIN", "HAND_FREE", "HAND_TAGGED", "CUSTOM_UNKNOWN", "CUSTOM_DAMAGED"):
            self.assertIn(token, core)
        for token in ("CUSTOM_TAGGED", "CUSTOM_TWO"):
            self.assertNotIn(token, core)
        self.assertIn("`decisions`（方案，3 条）", core)  # beta、gamma 加一条自定义行
        selected = self.context("select", "--tag", "decisions").stdout
        self.assertIn("## 用户自定义规则", selected)
        self.assertIn(self.TAGGED, selected)
        self.assertNotIn("CUSTOM_TWO", selected)
        both = self.context("select", "--tag", "files", "--tag", "reports").stdout
        self.assertEqual(both.count("CUSTOM_TWO"), 1)

    def test_every_custom_line_is_in_exactly_one_of_core_or_the_full_selection(self) -> None:
        everything = self.context("core").stdout + self.context("select", "--tag", "decisions", "--tag", "files", "--tag", "reports").stdout
        for line in self.LINES:
            self.assertEqual(everything.count(line), 1, line)
        self.assertEqual(json.loads(self.context("index").stdout)["rejected_custom_lines"], 2)

    def test_check_requires_tagged_custom_lines(self) -> None:
        result = self.context("check", "--tag", "reports", "--loaded", "reports")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.context("check", "--tag", "reports", "--loaded", "decisions").returncode, 1)


class RuleLineTests(unittest.TestCase):
    LEGACY = (
        "- **先给结论**（适用：回答；条件：给出建议；目的：节省时间）：回答先给结论再展开",
        "- **Answer first** (Applies to: replies; When: advising; Purpose: save time): put the conclusion first",
        "- **用 skill**（适用：汇报；条件：做汇报；不适用：skill deck 自身的执行过程）：先整理。 路由：开始主体工作前先使用 skill deck；该 skill 执行期间不再触发本规则；该 skill 不可用时告知用户并按目的自行完成。 [route skill=deck when=before]",
    )

    def candidate(self, parsed: dict) -> dict:
        return {key: parsed[key] for key in ("title", "text", "scopes", "trigger", "purpose", "exclude")} | ({"route": parsed["route"]} if "route" in parsed else {})

    def test_legacy_lines_parse_and_render_back_byte_for_byte(self) -> None:
        for line in self.LEGACY:
            parsed = rule_lines.parse_line(line)
            self.assertIsNotNone(parsed, line)
            self.assertNotIn("tags", parsed)
            self.assertNotIn("tags_error", parsed)
            self.assertEqual(rule_lines.candidate_rule(self.candidate(parsed), parsed["format"]), line)

    def test_legacy_candidate_fingerprint_ignores_tags_unless_present(self) -> None:
        item = {"id": "cand-1", "title": "T", "text": "X", "scopes": ["S"], "trigger": "", "purpose": "", "exclude": ""}
        plain = inbox.fingerprint_input(item)
        self.assertEqual(plain, ["cand-1", versioned.digest(["T", "X", ["S"]])])
        self.assertEqual(inbox.fingerprint_input({**item, "tags": []}), plain)
        self.assertNotEqual(inbox.fingerprint_input({**item, "tags": ["a"]}), plain)
        self.assertEqual(inbox.fingerprint_input({**item, "tags": ["b", "a"]}), inbox.fingerprint_input({**item, "tags": ["a", "b"]}))

    def test_tag_marker_is_ascii_sorted_and_round_trips_with_and_without_a_route(self) -> None:
        for language in ("zh", "en"):
            base = {"title": "T", "text": "X", "scopes": ["S"], "trigger": "when", "purpose": "", "exclude": "", "tags": ["files", "decisions"]}
            for route in (None, {"skill": "deck", "when": "after", "final": True, "on": "ppt"}):
                candidate = {**base, **({"route": route} if route else {})}
                line = rule_lines.candidate_rule(candidate, language)
                self.assertTrue(line.endswith(" [tags decisions files]"), line)
                self.assertEqual(rule_lines.tags_marker(candidate["tags"]).isascii(), True)
                parsed = rule_lines.parse_line(line)
                self.assertEqual(parsed["tags"], ["decisions", "files"])
                self.assertEqual(parsed["text"], "X")
                self.assertEqual(parsed.get("route"), route and {**route})
                rebuilt = {**self.candidate(parsed), "tags": parsed["tags"]}
                self.assertEqual(rule_lines.candidate_rule(rebuilt, language), line)

    def test_damaged_markers_are_reported_and_never_guessed(self) -> None:
        for body in ("Bad", "a a", "", "a,b", "中文", "a  b"):
            parsed = rule_lines.parse_line(f"- **T**（适用：S）：X [tags {body}]")
            self.assertEqual(parsed["tags_error"], "marker_damaged", body)
            self.assertNotIn("tags", parsed)
        self.assertIsNone(rule_lines.parse_line("free text [tags a]"))  # 认不出的行不因标记而被认出


class InboxTagTests(TagsBase):
    def inbox(self, *arguments: object, code: int = 0) -> dict:
        result = self.run_cli("inbox", *arguments, "--project", self.project, "--output", "json")
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def add(self, *extra: str, title: str = "加标签的规则", code: int = 0) -> dict:
        return self.inbox("add", "--title", title, "--text", "TEXT_TAGGED", "--scope", "S", "--trigger", "T", "--purpose", "P", *extra, code=code)

    def test_add_and_revise_validate_tags_against_the_vocabulary(self) -> None:
        self.adopt(self.loading)
        self.assertIn("未知标签：nope；有效标签：decisions、files、reports、style", self.add("--tag", "nope", code=2)["error"])
        added = self.add("--tag", "files", "--tag", "decisions", "--tag", "files")
        candidate = self.inbox("list")["value"][0]
        self.assertEqual(candidate["tags"], ["decisions", "files"])
        self.assertIn(added["id"], candidate["id"])
        self.assertIn("未知标签", self.inbox("revise", added["id"], "--tag", "nope", code=2)["error"])
        self.inbox("revise", added["id"], "--tag", "reports")
        self.assertEqual(self.inbox("list")["value"][0]["tags"], ["reports"])
        self.assertIn("不能与 --tag 同时使用", self.inbox("revise", added["id"], "--tag", "files", "--tag-clear", code=2)["error"])
        self.inbox("revise", added["id"], "--tag-clear")
        self.assertNotIn("tags", self.inbox("list")["value"][0])

    def test_tags_need_a_vocabulary(self) -> None:
        self.assertIn("没有标签词表", self.add("--tag", "files", code=2)["error"])
        self.adopt(self.legacy)
        self.assertIn("没有标签词表", self.add("--tag", "files", title="另一条", code=2)["error"])

    def test_adopt_keeps_preview_then_confirm_and_the_line_loads_by_tag(self) -> None:
        self.adopt(self.loading)
        added = self.add("--tag", "reports")
        before = {path.name: path.read_bytes() for path in self.project.glob("*") if path.is_file()}
        preview = self.inbox("adopt", added["id"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.project.glob("*") if path.is_file()})
        self.assertEqual(preview["status"], "preview")
        self.assertIn(" [tags reports]", preview["content"])
        self.inbox("adopt", added["id"], "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertIn("TEXT_TAGGED", self.context("select", "--tag", "reports").stdout)
        self.assertNotIn("TEXT_TAGGED", self.context("core").stdout)
        self.assertEqual(versioned.verify_project(self.project)["status"], "verified")

    def test_a_confirmation_does_not_cover_different_tags(self) -> None:
        self.adopt(self.loading)
        first = self.add("--tag", "reports")
        digest_a = self.inbox("adopt", first["id"])["confirmation_sha256"]
        self.inbox("revise", first["id"], "--tag", "files")
        digest_b = self.inbox("adopt", first["id"])["confirmation_sha256"]
        self.assertNotEqual(digest_a, digest_b)
        self.inbox("adopt", first["id"], "--apply", "--confirm", digest_a, code=2)


class LongPathTests(TagsBase):
    def test_commands_work_with_a_very_long_project_path(self) -> None:
        long_project = self.root.joinpath(*("long-directory-segment-" + str(index) for index in range(6))) / "project"
        long_project.mkdir(parents=True)
        self.assertGreaterEqual(len(str(long_project)), 138)
        self.adopt(self.loading, project=long_project)
        self.assertIn(self.block("alpha"), self.context("core", project=long_project).stdout)
        self.assertIn(self.block("beta"), self.context("select", "--tag", "decisions", project=long_project).stdout)
        self.assertEqual(json.loads(self.context("index", project=long_project).stdout)["on_demand_tags"], 3)
        self.assertEqual(self.context("check", "--tag", "files", project=long_project).returncode, 1)
        self.assertEqual(self.context("check", "--tag", "files", "--loaded", "files", project=long_project).returncode, 0)


class EnglishProjectTests(TagsBase):
    language = "en"

    def test_english_project_output_has_no_chinese(self) -> None:
        self.adopt(self.loading, custom="- **CT** (Applies to: S): CUSTOM_TAGGED [tags decisions]\n\n- **CP** (Applies to: S): CUSTOM_PLAIN")
        outputs = [self.context("core").stdout, self.context("select", "--tag", "decisions", "--tag", "reports").stdout,
                   self.context("index").stdout, self.context("check", "--tag", "files").stdout,
                   self.context("select", "--tag", "nope").stdout, self.context("select", "--tag", "style").stdout]
        for text in outputs:
            self.assertIsNone(CJK.search(text), text)
        core = self.context("core").stdout
        self.assertIn("EN_RULE_ALPHA", core)
        self.assertIn("CUSTOM_PLAIN", core)
        self.assertNotIn("CUSTOM_TAGGED", core)
        self.assertIn("CUSTOM_TAGGED", self.context("select", "--tag", "decisions").stdout)
        self.assertIn("## User-defined rules", self.context("select", "--tag", "decisions").stdout)


if __name__ == "__main__":
    unittest.main()
