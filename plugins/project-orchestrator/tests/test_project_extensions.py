"""技能与 MCP 的只读命令（agenthelm project extensions list / dups / show / check）。

数据来自 ``agenthelm inventory`` 的 JSON：这里用一个假的 agenthelm 脚本回放夹具（tests/fixtures），验证子进程调用、
降级、重复归类、「哪份生效」只在文档明确时下结论、机械体检，以及任何输出里都没有密钥值。演示项目走固定夹具。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import copy
import json
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
CLI = RUNTIME / "project_cli.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "extensions_inventory.json"
FAKE_SECRET = "FAKE_SECRET_VALUE_0001"
sys.path.insert(0, str(RUNTIME))
import project_extensions as ext  # noqa: E402

FAKE_AGENTHELM = """#!{python}
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")
mode = os.environ.get("FAKE_MODE", "ok")
if args[:1] != ["inventory"] or mode == "no-inventory":
    sys.exit(2)
if "--help" in args:
    sys.exit(0)
if mode == "bad-json":
    print("not json at all")
    sys.exit(0)
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(3)
sys.stdout.write(open(os.environ["FAKE_INVENTORY"], encoding="utf-8").read())
"""


class ExtensionsCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.bin, self.empty, self.home, self.project = (self.base / name for name in ("bin", "empty", "home", "project"))
        for path in (self.bin, self.empty, self.home):
            path.mkdir()
        script = self.bin / "agenthelm"
        script.write_text(FAKE_AGENTHELM.format(python=sys.executable), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        profile = self.project / ".agents/moe.sakanano.agent-pack/project.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"schema_version": "2.0", "work": {"mode": "case-workspace", "case": {"root": "cases"}}}),
                           encoding="utf-8")
        self.log = self.base / "calls.log"
        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    def env(self, *, mode: str = "ok", path: Path | None = None, extra: dict | None = None) -> dict:
        base = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "CLAUDE", "CODEX"))}
        return {**base, "HOME": str(self.home), "AGENTHELM_LANG": "zh", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": str(path or self.bin), "FAKE_MODE": mode, "FAKE_INVENTORY": str(self.inventory), "FAKE_LOG": str(self.log),
                **(extra or {})}

    def run_cli(self, *args: str, mode: str = "ok", path: Path | None = None, demo: str | None = None,
                extra: dict | None = None) -> subprocess.CompletedProcess:
        target = ["--demo", demo] if demo else ["--project", str(self.project)]
        return subprocess.run([sys.executable, str(CLI), *target, *args], input="", capture_output=True, text=True,
                              env=self.env(mode=mode, path=path, extra=extra), timeout=60, check=False)

    def run_json(self, *args: str, **kwargs) -> tuple[int, dict]:
        result = self.run_cli(*args, "--output", "json", **kwargs)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"not JSON (exit {result.returncode}): {result.stdout}\n{result.stderr}")

    def set_inventory(self, change) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        change(data)
        self.inventory.write_text(json.dumps(data), encoding="utf-8")


class SubprocessTests(ExtensionsCase):
    def test_inventory_is_called_with_an_argument_list_and_read_only_flags(self) -> None:
        code, payload = self.run_json("extensions", "list")
        self.assertEqual(code, 0, payload)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertIn(["inventory", "--output", "json", "--project", str(self.project)], calls)
        self.assertNotIn("--include-description", sum(calls, []), "默认不要求输出 description 文本")

    def test_check_with_include_description_asks_for_the_text_and_finds_similar_pairs(self) -> None:
        code, payload = self.run_json("extensions", "check", "--include-description")
        self.assertEqual(code, 0)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("--include-description" in call for call in calls))
        pairs = payload["data"]["similar"]
        self.assertTrue(pairs)
        self.assertTrue(all(pair["jaccard"] >= 0.6 for pair in pairs))
        self.assertEqual(payload["data"]["similarity"]["threshold"], 0.6)
        self.assertNotIn("Review a pull request", json.dumps(payload), "description 文本不进入输出")

    def test_without_the_flag_there_is_no_similarity_check(self) -> None:
        code, payload = self.run_json("extensions", "check")
        self.assertEqual((code, payload["data"]["similar"]), (0, []))
        self.assertFalse(payload["data"]["similarity"]["enabled"])

    def test_agenthelm_not_found_is_unavailable_with_the_reason_exit_4(self) -> None:
        code, payload = self.run_json("extensions", "list", path=self.empty)
        self.assertEqual(code, 4)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "unavailable")
        self.assertIn("未找到 agent-pack 的 inventory", payload["error"]["message"])

    def test_an_agenthelm_without_inventory_is_unavailable(self) -> None:
        code, payload = self.run_json("extensions", "dups", mode="no-inventory")
        self.assertEqual(code, 4)
        self.assertIn("没有 inventory 命令", payload["error"]["message"])

    def test_unknown_schema_major_is_unavailable_and_not_guessed(self) -> None:
        self.set_inventory(lambda data: data.update(schema_version=2))
        code, payload = self.run_json("extensions", "list")
        self.assertEqual(code, 4)
        self.assertIn("schema_version 2", payload["error"]["message"])
        self.set_inventory(lambda data: data.pop("schema_version"))
        code, payload = self.run_json("extensions", "check")
        self.assertEqual(code, 4)
        self.assertIn("没有 schema_version", payload["error"]["message"])

    def test_a_minor_schema_version_is_accepted(self) -> None:
        self.set_inventory(lambda data: data.update(schema_version="1.3"))
        self.assertEqual(self.run_json("extensions", "list")[0], 0)

    def test_broken_output_is_unavailable_and_stderr_is_truncated(self) -> None:
        for mode in ("bad-json", "fail"):
            code, payload = self.run_json("extensions", "list", mode=mode)
            self.assertEqual(code, 4, mode)
            self.assertEqual(payload["error"]["code"], "unavailable")

    def test_text_mode_of_an_unavailable_inventory_names_the_next_step(self) -> None:
        result = self.run_cli("extensions", "list", path=self.empty)
        self.assertEqual(result.returncode, 4)
        self.assertIn("未找到 agent-pack 的 inventory", result.stderr)
        self.assertIn("更新 agent-pack", result.stderr)

    def test_the_host_agenthelm_script_is_preferred_over_path(self) -> None:
        host_dir = self.base / "host"
        host_dir.mkdir()
        (host_dir / "agenthelm.py").write_text("", encoding="utf-8")
        host = SimpleNamespace(__file__=str(host_dir / "agenthelm.py"))
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertEqual(ext.agenthelm_command(host), [sys.executable, str(host_dir / "agenthelm.py")])
            self.assertEqual(ext.agenthelm_command(None), [str(self.bin / "agenthelm")])
        with mock.patch.dict(os.environ, {"PATH": str(self.empty)}):
            self.assertIsNone(ext.agenthelm_command(None))
            self.assertEqual(ext.load(None, None)["state"], "unavailable")

    def test_capability_follows_whether_inventory_can_be_called(self) -> None:
        code, payload = self.run_json("capabilities")
        item = next(item for item in payload["data"] if item["id"] == "extensions")
        self.assertEqual((code, item["state"], item["reason"]), (0, "available", ""))
        code, payload = self.run_json("capabilities", path=self.empty)
        item = next(item for item in payload["data"] if item["id"] == "extensions")
        self.assertEqual(item["state"], "unavailable")
        self.assertIn("未找到 agent-pack 的 inventory", item["reason"])
        code, payload = self.run_json("capabilities", mode="no-inventory")
        self.assertEqual(next(item for item in payload["data"] if item["id"] == "extensions")["state"], "unavailable")


class ListAndShowTests(ExtensionsCase):
    def test_list_groups_by_client_and_summarises(self) -> None:
        code, payload = self.run_json("extensions", "list")
        data = payload["data"]
        self.assertEqual((code, payload["command"], payload["ok"], payload["attention"]), (0, "extensions", True, False))
        self.assertEqual((data["summary"]["skills"], data["summary"]["mcps"]), (7, 8))
        self.assertEqual([part["key"] for part in data["groups"]], ["claude", "claude-desktop", "codex", "cursor"])
        self.assertEqual(data["summary"]["duplicate_groups"], {"total": 5, "mcp": 3, "skill": 2})
        self.assertEqual(data["summary"]["unknown"], 4)

    def test_default_subcommand_is_list(self) -> None:
        self.assertEqual(self.run_json("extensions")[1]["data"]["view"], "list")

    def test_filters(self) -> None:
        data = self.run_json("extensions", "list", "--kind", "mcp", "--client", "claude")[1]["data"]
        self.assertEqual({row["kind"] for part in data["groups"] for row in part["entries"]}, {"mcp"})
        self.assertEqual([part["key"] for part in data["groups"]], ["claude"])
        data = self.run_json("extensions", "list", "--scope", "local")[1]["data"]
        self.assertEqual([row["ref"] for part in data["groups"] for row in part["entries"]], ["mcp:search@claude:local"])
        data = self.run_json("extensions", "list", "--group", "scope")[1]["data"]
        self.assertIn("user", [part["key"] for part in data["groups"]])
        data = self.run_json("extensions", "list", "--group", "dup")[1]["data"]
        self.assertEqual(len(data["groups"]), 5)

    def test_unknown_evidence_is_shown_as_unknown_never_folded(self) -> None:
        text = self.run_cli("extensions", "list").stdout
        unknown_rows = [line for line in text.splitlines() if re.match(r"^\s+\? ", line)]
        self.assertEqual(len(unknown_rows), 4)
        self.assertTrue(all("[未知]" in line for line in unknown_rows), unknown_rows)
        self.assertIn("未知 4 项", text)
        cursor = next(row for part in self.run_json("extensions", "list")[1]["data"]["groups"] for row in part["entries"]
                      if row["ref"] == "skill:search-helper@cursor:project")
        self.assertEqual(cursor["evidence"], "unknown")
        self.assertIsNone(cursor["description_bytes"])
        self.assertIn("desc 未知", text)

    def test_rows_keep_name_and_scope_and_do_not_break_words(self) -> None:
        for columns in ("120", "60"):
            result = self.run_cli("extensions", "list", extra={"COLUMNS": columns})
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("code-review", "long-guide", "pdf-notes", "notes-server", "remote-docs"):
                self.assertIn(name, result.stdout)
            rows = [line for line in result.stdout.splitlines() if re.match(r"^\s+[●⚙⌘?] ", line)]
            self.assertTrue(all("用户级" in line or "项目级" in line or "本机项目级" in line or "应用级" in line for line in rows), rows)
            self.assertTrue(all(line.count("[") == line.count("]") for line in result.stdout.splitlines()), "标签整块移动，不拆开")

    def test_narrow_rows_drop_origin_then_size_then_desc_only(self) -> None:
        entry = {"kind": "skill", "name": "code-review", "client": "claude", "scope": "user", "evidence": "file",
                 "description_bytes": 312, "skill_md_bytes": 1230, "origin": {"type": "plugin", "id": "example", "version": "1"},
                 "tags": []}
        wide = ext.entry_line(entry, 12, width=120)[0]
        self.assertIn("来源", wide)
        no_origin = ext.entry_line(entry, 12, width=48)[0]
        self.assertNotIn("来源", no_origin)
        self.assertIn("desc 312 B", no_origin)
        narrow = ext.entry_line(entry, 12, width=30)[0]
        self.assertIn("code-review", narrow)
        self.assertIn("用户级", narrow)
        self.assertNotIn("来源", narrow)
        self.assertNotIn("1.2 KB", narrow)

    def test_show_by_ref_name_and_group(self) -> None:
        code, payload = self.run_json("extensions", "show", "skill:pdf-notes@claude:user")
        entry = payload["data"]["entry"]
        self.assertEqual((code, entry["name"], entry["tags"]), (0, "pdf-notes", ["无 description"]))
        self.assertEqual(self.run_json("extensions", "show", "long-guide")[1]["data"]["entry"]["ref"], "skill:long-guide@claude:user")
        groups = self.run_json("extensions", "dups")[1]["data"]["groups"]
        shown = self.run_json("extensions", "show", groups[0]["id"])[1]["data"]
        self.assertEqual((shown["view"], shown["group"]["id"]), ("group", groups[0]["id"]))

    def test_show_ambiguous_and_missing(self) -> None:
        code, payload = self.run_json("extensions", "show", "code-review")
        self.assertEqual((code, payload["error"]["code"]), (1, "ambiguous"))
        self.assertEqual(sorted(payload["error"]["next"]), ["skill:code-review@claude:project", "skill:code-review@claude:user"])
        code, payload = self.run_json("extensions", "show", "nothing")
        self.assertEqual((code, payload["error"]["code"]), (1, "not-found"))

    def test_referenced_by_is_shown_only_when_the_inventory_provides_it(self) -> None:
        self.assertNotIn("referenced_by", self.run_json("extensions", "show", "skill:pdf-notes@claude:user")[1]["data"]["entry"])
        self.assertNotIn("被引用", self.run_cli("extensions", "show", "skill:pdf-notes@claude:user").stdout)

        def add(data: dict) -> None:
            next(s for s in data["skills"] if s["name"] == "pdf-notes")["referenced_by"] = [{"rule_id": "rule-1", "source": "profile"}, "bad"]
        self.set_inventory(add)
        entry = self.run_json("extensions", "show", "skill:pdf-notes@claude:user")[1]["data"]["entry"]
        self.assertEqual(entry["referenced_by"], [{"rule_id": "rule-1", "source": "profile"}])
        self.assertIn("被 1 条规则引用", self.run_cli("extensions", "show", "skill:pdf-notes@claude:user").stdout)

    def test_empty_inventory(self) -> None:
        self.set_inventory(lambda data: data.update(skills=[], mcps=[], clients=[], unknown=[]))
        code, payload = self.run_json("extensions", "list")
        self.assertEqual((code, payload["data"]["groups"], payload["data"]["summary"]["skills"]), (0, [], 0))
        self.assertIn("没有匹配的条目", self.run_cli("extensions", "list").stdout)
        self.assertIn("没有发现重复", self.run_cli("extensions", "dups").stdout)
        self.assertEqual(self.run_json("extensions", "check")[1]["data"]["findings"], [])


class DuplicateTests(ExtensionsCase):
    def groups(self) -> dict[str, dict]:
        return {f"{g['kind']}:{g['name']}:{g['class']}": g for g in self.run_json("extensions", "dups")[1]["data"]["groups"]}

    def test_classes_and_cross_client_is_not_a_conflict(self) -> None:
        groups = self.groups()
        notes = groups["mcp:notes-server:same-name-same"]
        self.assertTrue(notes["cross_client"])
        self.assertEqual(notes["effective"]["lines"], ["各客户端独立加载，彼此不冲突"])
        self.assertEqual(groups["mcp:search:same-name-differs"]["label"], "同名 · 指纹不同（内容漂移）")
        text = self.run_cli("extensions", "dups").stdout
        self.assertIn("跨客户端重复：各客户端加载各自的配置，可保留，不推荐退役", text)

    def test_effective_only_where_the_docs_state_a_precedence(self) -> None:
        groups = self.groups()
        search = groups["mcp:search:same-name-differs"]["effective"]
        self.assertTrue(search["known"])
        self.assertIn("生效的是 本机项目级 那份", search["lines"][0])
        self.assertIn("local > project > user", search["lines"][0])
        fetch = groups["mcp:fetch:same-name-same"]["effective"]
        self.assertFalse(fetch["known"])
        self.assertIn("生效：未知", fetch["lines"][0])

    def test_cursor_stays_unknown_even_with_file_evidence(self) -> None:
        def known(data: dict) -> None:
            for entry in data["mcps"]:
                if entry["name"] == "fetch":
                    entry["evidence"] = "file"
        self.set_inventory(known)
        effective = self.groups()["mcp:fetch:same-name-same"]["effective"]
        self.assertFalse(effective["known"])
        self.assertIn("优先级未文档化", effective["lines"][0])

    def test_codex_project_over_user_and_claude_skill_precedence(self) -> None:
        def codex(data: dict) -> None:
            data["mcps"] += [{"name": "tool", "client": "codex", "scope": "project", "store": ".codex/config.toml", "evidence": "file",
                              "transport": "stdio", "fingerprint": "aaaa1111bbbb2222"},
                             {"name": "tool", "client": "codex", "scope": "user", "store": "~/.codex/config.toml", "evidence": "file",
                              "transport": "stdio", "fingerprint": "cccc3333dddd4444"}]
        self.set_inventory(codex)
        effective = self.groups()["mcp:tool:same-name-differs"]["effective"]
        self.assertTrue(effective["known"])
        self.assertIn("项目级 那份", effective["lines"][0])
        skill = self.groups()["skill:code-review:same-name-same"]["effective"]
        self.assertIn("企业 > 个人 > 项目", skill["lines"][0])

    def test_desktop_pair_carries_the_qualifier(self) -> None:
        def desktop(data: dict) -> None:
            data["mcps"].append({"name": "notes-server", "client": "claude-desktop", "scope": "desktop", "store": "<app-config>",
                                 "evidence": "file", "transport": "stdio", "fingerprint": "a1b2c3d4e5f60718"})
        self.set_inventory(desktop)
        group = self.groups()["mcp:notes-server:same-name-same"]
        self.assertFalse(group["cross_client"])
        self.assertTrue(any("Desktop 应用的本地 Code 标签会话" in line for line in group["effective"]["lines"]))

    def test_different_names_same_fingerprint(self) -> None:
        def rename(data: dict) -> None:
            data["mcps"].append({"name": "notes-copy", "client": "claude", "scope": "user", "store": "~/.claude.json", "evidence": "file",
                                 "transport": "stdio", "fingerprint": "a1b2c3d4e5f60718"})
        self.set_inventory(rename)
        group = self.groups()["mcp:a1b2c3d4:diff-name-same-fp"]
        self.assertEqual(group["label"], "不同名 · 同指纹")
        self.assertTrue(group["effective"]["known"])

    def test_group_ids_are_stable(self) -> None:
        first = [g["id"] for g in self.run_json("extensions", "dups")[1]["data"]["groups"]]
        second = [g["id"] for g in self.run_json("extensions", "dups")[1]["data"]["groups"]]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first))

    def test_kind_filter(self) -> None:
        groups = self.run_json("extensions", "dups", "--kind", "skill")[1]["data"]["groups"]
        self.assertEqual({g["kind"] for g in groups}, {"skill"})


class CheckTests(ExtensionsCase):
    def test_mechanical_findings_with_thresholds_and_sources(self) -> None:
        data = self.run_json("extensions", "check")[1]["data"]
        codes = {(item["ref"], item["code"]) for item in data["findings"]}
        self.assertIn(("skill:pdf-notes@claude:user", "no-description"), codes)
        self.assertIn(("skill:long-guide@claude:user", "desc-too-long"), codes)
        self.assertIn(("skill:long-guide@claude:user", "skill-md-long"), codes)
        self.assertIn(("skill:code-review@claude:user", "stale"), codes)
        self.assertIn(("skill:meeting-notes@codex:user", "drift"), codes)
        long = next(item for item in data["findings"] if item["code"] == "desc-too-long")
        self.assertIn("1536", long["fact"])
        self.assertTrue(long["source"])
        for forbidden in ("score", "rank", "delete", "recommend"):
            self.assertNotIn(forbidden, json.dumps(data).lower().replace("recommended", ""), forbidden)
        self.assertNotIn("建议删除", self.run_cli("extensions", "check").stdout.replace("不建议删除", ""))

    def test_unknown_evidence_entries_get_no_findings(self) -> None:
        refs = {item["ref"] for item in self.run_json("extensions", "check")[1]["data"]["findings"]}
        self.assertNotIn("skill:search-helper@cursor:project", refs)

    def test_cost_is_per_client_and_counts_only_what_loads(self) -> None:
        cost = {row["client"]: row for row in self.run_json("extensions", "check")[1]["data"]["cost"]}
        self.assertEqual(cost["codex"]["resident_bytes"], 420)
        self.assertEqual((cost["claude"]["counted"], cost["claude"]["uncounted"]), (4, 0))
        # long-guide: 1800 bytes / 1800 chars is capped at 1536 characters -> 1536 bytes; plus 312 + 312 + 0.
        self.assertEqual(cost["claude"]["resident_bytes"], 312 + 312 + 0 + 1536)
        self.assertEqual((cost["cursor"]["counted"], cost["cursor"]["uncounted"]), (0, 1))
        estimate = cost["claude"]["token_estimate"]
        self.assertEqual((estimate["low"], estimate["high"]), (cost["claude"]["resident_bytes"] // 4, cost["claude"]["resident_bytes"] // 2))
        self.assertIn("估算", estimate["label"])
        text = self.run_cli("extensions", "check").stdout
        self.assertIn("cursor：未知", text)

    def test_shadowed_or_not_loaded_entries_are_not_counted(self) -> None:
        def shadow(data: dict) -> None:
            next(s for s in data["skills"] if s["scope"] == "project" and s["name"] == "code-review")["shadowed_by"] = "skill:code-review@claude:user"
            next(s for s in data["skills"] if s["name"] == "long-guide")["loaded"] = False
        self.set_inventory(shadow)
        row = next(row for row in self.run_json("extensions", "check")[1]["data"]["cost"] if row["client"] == "claude")
        self.assertEqual((row["counted"], row["uncounted"], row["resident_bytes"]), (2, 2, 312))

    def test_missing_fields_are_reported_as_not_checked_never_as_zero(self) -> None:
        def strip(data: dict) -> None:
            for skill in data["skills"]:
                skill.pop("skill_md_lines", None)
                skill.pop("description_chars", None)
        self.set_inventory(strip)
        data = self.run_json("extensions", "check")[1]["data"]
        self.assertTrue(any("skill_md_lines" in note and "没有检查" in note for note in data["notes"]), data["notes"])
        self.assertFalse(any(item["code"] == "skill-md-long" for item in data["findings"]))

    def test_old_origin_without_version_is_unknown_not_stale(self) -> None:
        notes = self.run_json("extensions", "check")[1]["data"]["notes"]
        self.assertTrue(any("过期：" in note and "未知" in note for note in notes))

    def test_forbidden_characters_in_names(self) -> None:
        def bad(data: dict) -> None:
            data["skills"].append({"name": "bad<name>", "client": "claude", "scope": "user", "store": "~/.claude/skills", "evidence": "file",
                                   "description_bytes": 10, "description_chars": 10})
        self.set_inventory(bad)
        self.assertIn("name-bad-chars", {item["code"] for item in self.run_json("extensions", "check")[1]["data"]["findings"]})

    def test_similarity_is_mechanical_and_needs_the_flag(self) -> None:
        skills = [{"name": "a", "evidence": "file", "client": "claude", "scope": "user", "_description": "Review pull requests carefully", "ref": "a"},
                  {"name": "b", "evidence": "file", "client": "claude", "scope": "user", "_description": "Review pull requests carefully today", "ref": "b"},
                  {"name": "c", "evidence": "file", "client": "claude", "scope": "user", "_description": "总结会议记录并列出待办事项", "ref": "c"},
                  {"name": "d", "evidence": "file", "client": "claude", "scope": "user", "_description": "总结会议记录并列出待办", "ref": "d"}]
        pairs, why = ext.similar_pairs(skills)
        self.assertIsNone(why)
        self.assertEqual([pair["refs"] for pair in pairs], [["a", "b"], ["c", "d"]])
        self.assertEqual(ext.SIMILARITY_THRESHOLD, 0.6)


class SecretTests(ExtensionsCase):
    COMMANDS = (("extensions", "list"), ("extensions", "list", "--group", "dup"), ("extensions", "dups"),
                ("extensions", "check", "--include-description"), ("extensions", "show", "notes-server@claude:user"),
                ("extensions", "show", "mcp:notes-server@claude:user"), ("extensions", "show", "mcp:remote-docs@claude:project"),
                ("extensions", "show", "mcp:local-db@claude-desktop:desktop"), ("capabilities",))

    def test_no_output_carries_a_secret_value_env_value_or_header(self) -> None:
        for command in self.COMMANDS:
            for output in ("text", "json"):
                result = self.run_cli(*command, "--output", output)
                combined = result.stdout + result.stderr
                for needle in (FAKE_SECRET, "Bearer", "X-Api-Key", "--token", "--password", "?token="):
                    self.assertNotIn(needle, combined, f"{command} {output}")

    def test_mcp_detail_shows_only_the_allowed_fields(self) -> None:
        entry = self.run_json("extensions", "show", "mcp:remote-docs@claude:project")[1]["data"]["entry"]
        self.assertEqual(sorted(entry), sorted(["ref", "kind", "name", "client", "scope", "store", "evidence", "transport",
                                                "fingerprint8", "fingerprint_fields", "env_var_names", "has_inline_secret",
                                                "url_host", "tags", "groups", "findings"]))
        self.assertEqual((entry["url_host"], entry["env_var_names"], entry["fingerprint8"]), ("docs.example.com", ["DOCS_TOKEN"], "11223344"))
        text = self.run_cli("extensions", "show", "mcp:notes-server@claude:user").stdout
        self.assertIn("NOTES_API_KEY", text)
        self.assertIn("不显示命令行参数", text)

    def test_inline_secret_is_a_flag_only(self) -> None:
        entry = self.run_json("extensions", "show", "mcp:local-db@claude-desktop:desktop")[1]["data"]["entry"]
        self.assertIs(entry["has_inline_secret"], True)
        self.assertIn("含内联密钥值", entry["tags"])
        self.assertNotIn("args", entry)

    def test_the_whitelist_drops_unknown_fields_in_unit_form(self) -> None:
        raw = {"name": "x", "client": "claude", "scope": "user", "store": "s", "evidence": "file", "transport": "stdio",
               "fingerprint": "f" * 16, "command": "run", "args": [FAKE_SECRET], "env": {"K": FAKE_SECRET}, "headers": {"A": FAKE_SECRET}}
        entry = ext.public(ext._entry("mcp", raw))
        self.assertNotIn(FAKE_SECRET, json.dumps(entry))
        self.assertEqual(entry["fingerprint8"], "ffffffff")


class DemoTests(ExtensionsCase):
    def test_every_scenario_walks_through_all_four_views(self) -> None:
        for scenario in ("study", "personal", "research", "blank"):
            for command in (("list",), ("dups",), ("show", "code-review@claude:user"), ("check",)):
                result = self.run_cli("extensions", *command, "--output", "json", demo=scenario)
                self.assertEqual(result.returncode, 0, (scenario, command, result.stdout, result.stderr))
                payload = json.loads(result.stdout)
                self.assertTrue(payload["demo"])
                self.assertEqual(payload["data"]["state"], "available")

    def test_demo_never_calls_agenthelm_and_ignores_the_machine(self) -> None:
        self.run_cli("extensions", "list", demo="study", path=self.empty)
        self.assertFalse(self.log.exists())

    def test_demo_is_labelled_and_has_no_secret(self) -> None:
        result = self.run_cli("extensions", "list", demo="study")
        self.assertIn("演示数据", result.stdout)
        self.assertNotIn(FAKE_SECRET, result.stdout)

    def test_demo_capability_is_available(self) -> None:
        payload = self.run_json("capabilities", demo="study")[1]
        self.assertEqual(next(item for item in payload["data"] if item["id"] == "extensions")["state"], "available")

    def test_english_output_has_no_chinese_left(self) -> None:
        cjk = re.compile("[一-鿿]")
        for command in (("list",), ("dups",), ("show", "code-review@claude:user"), ("check", "--include-description"), ("show", "notes-server")):
            result = self.run_cli("extensions", *command, demo="study", extra={"AGENTHELM_LANG": "en"})
            self.assertFalse([line for line in (result.stdout + result.stderr).splitlines() if cjk.search(line)], command)
        result = self.run_cli("extensions", "list", path=self.empty, extra={"AGENTHELM_LANG": "en"})
        self.assertEqual(result.returncode, 4)
        self.assertFalse(cjk.search(result.stderr), result.stderr)


class FunctionTests(unittest.TestCase):
    def test_validate_branches(self) -> None:
        good = {"schema_version": 1, "skills": [], "mcps": []}
        self.assertEqual(ext.validate(good)["state"], "available")
        self.assertEqual(ext.validate({**good, "schema_version": 2})["state"], "unavailable")
        self.assertEqual(ext.validate({**good, "schema_version": "x"})["state"], "unavailable")
        self.assertEqual(ext.validate({**good, "skills": "no"})["state"], "unavailable")

    def test_entries_without_a_valid_evidence_are_unknown(self) -> None:
        entry = ext._entry("skill", {"name": "a", "client": "claude", "scope": "user", "evidence": "guess"})
        self.assertEqual(entry["evidence"], "unknown")
        self.assertIsNone(ext._entry("skill", {"client": "claude"}))

    def test_effective_never_concludes_for_unknown_evidence(self) -> None:
        members = [ext._entry("mcp", {"name": "n", "client": "claude", "scope": scope, "evidence": evidence, "fingerprint": "x" * 8 + str(i)})
                   for i, (scope, evidence) in enumerate((("local", "file"), ("user", "unknown")))]
        result = ext._effective("mcp", "same-name-differs", members)
        self.assertFalse(result["known"])

    def test_listing_is_deterministic(self) -> None:
        data = ext.validate(json.loads(FIXTURE.read_text(encoding="utf-8")))["inventory"]
        first = ext.listing(ext.analyse(copy.deepcopy(data)))
        second = ext.listing(ext.analyse(copy.deepcopy(data)))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
