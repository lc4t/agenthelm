from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))
import agent_pack_nudge as nudge  # noqa: E402
import agenthelm  # noqa: E402
import nudge_text  # noqa: E402

MANAGER = PLUGIN.parent / "opinion-manager" / "runtime" / "opinion_manager.py"


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")


def manager(*arguments: str) -> dict:
    completed = subprocess.run([sys.executable, str(MANAGER), *arguments], capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


class NudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name).resolve()
        self.home, self.project = base / "home", base / "project"
        self.home.mkdir()
        self.project.mkdir()
        isolated = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "CLAUDE_", "CODEX_"))}
        patcher = mock.patch.dict(os.environ, {**isolated, "AGENTHELM_LANG": "zh"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = agenthelm.current_env(self.home)
        write(self.project / ".opinion" / "keep", "")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        write(self.project / ".gitignore", ".opinion/\n")
        self.config, _ = nudge.merge_config(None, None)

    # -- helpers
    def capture(self, title: str, session: str = "s1", **extra) -> str:
        return manager("inbox", "add", "--project", str(self.project), "--title", title, "--text", f"{title}的完整规则",
                       "--scope", "回答", "--quote", f"原话：{title}", "--session", session)["id"]

    def decide(self, identity: str, to: str) -> None:
        manager("inbox", "decide", identity, "--project", str(self.project), "--to", to, "--confirmed")

    def start(self, source: str = "startup", client: str = "claude", config: dict | None = None, session: str = "s1"):
        payload = {"session_id": session, "cwd": str(self.project), "source": source}
        return nudge.session_start(agenthelm, self.env, payload, client, self.project, config or self.config)

    def stop(self, message: str = "已完成修改", client: str = "claude", session: str = "s1", config: dict | None = None, **extra):
        payload = {"session_id": session, "cwd": str(self.project), "last_assistant_message": message, **extra}
        return nudge.stop(agenthelm, self.env, payload, client, self.project, config or self.config)

    def hook(self, event: str, payload: dict, client: str = "auto", extra_env: dict | None = None):
        arguments = argparse.Namespace(event=event, client=client, home=self.home)
        with mock.patch.dict(os.environ, extra_env or {}):
            return nudge.run_hook(arguments, agenthelm, payload)

    # -- configuration
    def test_configuration_layers_user_over_project_over_default(self) -> None:
        config, warnings = nudge.merge_config({"pending_threshold": 3, "stale_days": 2, "stop_prompt": "off"},
                                              {"pending_threshold": 9, "stop_prompt": {"cursor": "always"}})
        self.assertEqual((config["pending_threshold"], config["stale_days"]), (9, 2))
        self.assertEqual(config["stop_prompt"], {"claude": "off", "codex": "off", "cursor": "always"})
        self.assertEqual(warnings, [])

    def test_invalid_configuration_falls_back_and_is_reported(self) -> None:
        config, warnings = nudge.merge_config({"pending_threshold": 0, "keywords": [], "unknown": 1, "enabled": "yes",
                                               "stop_prompt": "sometimes"}, "not an object")
        self.assertEqual(config["pending_threshold"], 5)
        self.assertTrue(config["enabled"])
        self.assertEqual(len(warnings), 6)
        self.assertIn("以后都", config["keywords"])

    def test_configuration_is_read_from_profile_and_user_file(self) -> None:
        write(self.project / agenthelm.PROFILE_FILE, {"opinion": {"provider": "opinion-manager", "nudge": {"stale_days": 3}}})
        write(self.env.config_file, {"nudge": {"pending_threshold": 2}})
        config, warnings = nudge.load_config(agenthelm, self.env, self.project)
        self.assertEqual((config["stale_days"], config["pending_threshold"], warnings), (3, 2, []))

    # -- project detection
    def test_hooks_are_silent_outside_opinion_projects(self) -> None:
        plain = self.project.parent / "plain"
        plain.mkdir()
        self.assertIsNone(nudge.find_project(agenthelm, plain))
        write(self.project / agenthelm.PROFILE_FILE, {"opinion": {"provider": None}})
        (self.project / ".opinion" / "keep").unlink()
        (self.project / ".opinion").rmdir()
        self.assertIsNone(nudge.find_project(agenthelm, self.project))
        write(self.project / agenthelm.PROFILE_FILE, {"opinion": {"provider": "opinion-manager"}})
        self.assertEqual(nudge.find_project(agenthelm, self.project / "sub"), self.project)
        self.assertIsNone(self.hook("session-start", {"cwd": str(plain)}))

    def test_client_detection(self) -> None:
        self.assertEqual(nudge.detect_client("auto", {"workspace_roots": ["/x"]}, {}), "cursor")
        self.assertEqual(nudge.detect_client("auto", {"turn_id": "t"}, {}), "codex")
        self.assertEqual(nudge.detect_client("auto", {}, {"PLUGIN_ROOT": "/p"}), "codex")
        self.assertEqual(nudge.detect_client("auto", {"session_id": "s"}, {}), "claude")
        self.assertEqual(nudge.detect_client("cursor", {"turn_id": "t"}, {}), "cursor")

    # -- session start
    def test_session_start_injects_the_protocol_even_with_nothing_pending(self) -> None:
        output = self.start()
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("inbox add", context)
        self.assertIn("--session s1", context)
        self.assertNotIn("systemMessage", output)

    def test_session_start_injects_trial_rules_and_not_pending_ones(self) -> None:
        trial, pending = self.capture("先给结论"), self.capture("用中文")
        self.decide(trial, "trial")
        context = self.start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("先给结论的完整规则", context)
        self.assertNotIn("用中文的完整规则", context)
        self.assertIn("待确认候选 1 条，尚未生效", context)
        self.assertNotIn(pending, context)

    def test_reminder_when_pending_reaches_the_threshold_and_then_throttles(self) -> None:
        config, _ = nudge.merge_config({"pending_threshold": 3}, None)
        for number in range(2):
            self.capture(f"规则{number}")
        self.assertNotIn("systemMessage", self.start(config=config))
        self.capture("规则2")
        first = self.start(config=config)
        self.assertIn("3 条候选观点待确认", first["systemMessage"])
        self.assertIn("不要打断当前任务", first["hookSpecificOutput"]["additionalContext"])
        again = self.start(config=config)
        self.assertNotIn("systemMessage", again)
        self.assertIn("inbox add", again["hookSpecificOutput"]["additionalContext"])

    def test_reminder_when_the_oldest_pending_item_is_stale(self) -> None:
        self.capture("规则")
        stale = json.loads((self.project / ".opinion/inbox.json").read_text(encoding="utf-8"))
        for item in stale["candidates"].values():
            item["last_action_at"] = "2026-01-01T00:00:00Z"
        write(self.project / ".opinion/inbox.json", stale)
        output = self.start()
        self.assertRegex(output["systemMessage"], r"1 条候选观点待确认（最久 \d+ 天未处理）")

    def test_compact_reinjects_the_protocol_without_a_reminder(self) -> None:
        config, _ = nudge.merge_config({"pending_threshold": 1}, None)
        self.capture("规则")
        output = self.start(source="compact", config=config)
        self.assertNotIn("systemMessage", output)
        self.assertIn("inbox add", output["hookSpecificOutput"]["additionalContext"])

    def test_session_start_reverts_expired_trials(self) -> None:
        identity = self.capture("规则")
        self.decide(identity, "trial")
        stored = json.loads((self.project / ".opinion/inbox.json").read_text(encoding="utf-8"))
        stored["candidates"][identity]["trial_since"] = "2026-01-01T00:00:00Z"
        write(self.project / ".opinion/inbox.json", stored)
        self.start()
        (item,) = manager("inbox", "list", "--project", str(self.project))["value"]
        self.assertEqual(item["status"], "pending")

    def test_cursor_gets_additional_context_in_its_own_format(self) -> None:
        output = self.start(client="cursor")
        self.assertEqual(list(output), ["additional_context"])
        self.assertIn("inbox add", output["additional_context"])

    # -- privacy: the inbox keeps the user's own words
    def test_first_session_start_tells_the_user_once_where_the_words_are_visible(self) -> None:
        self.capture("先给结论")
        (self.project / ".gitignore").unlink()
        first = self.start()
        self.assertIn("可能含敏感信息", first["systemMessage"])
        self.assertIn("由你决定", first["systemMessage"])
        self.assertIn("当前未被 Git 忽略", first["systemMessage"])
        self.assertNotIn("给用户的提示", first["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", self.start())
        self.assertFalse((self.project / ".gitignore").exists(), "the notice must never edit .gitignore")

    def test_other_clients_get_the_notice_through_the_model_when_they_cannot_show_it(self) -> None:
        self.capture("先给结论")
        (self.project / ".gitignore").unlink()
        output = self.start(client="cursor")
        self.assertIn("请在首次回复里用一句话如实转告", output["additional_context"])
        self.assertIn("是否处理由用户决定", output["additional_context"])

    def test_no_notice_when_the_inbox_is_already_ignored_or_absent(self) -> None:
        self.assertNotIn("systemMessage", self.start())
        self.capture("先给结论")
        self.assertNotIn("systemMessage", self.start())

    def test_status_shows_where_the_inbox_is_visible(self) -> None:
        self.capture("规则")
        arguments = argparse.Namespace(project=self.project, home=self.home)
        self.assertEqual(nudge.status(arguments, agenthelm)["exposure"]["git"], "ignored")
        (self.project / ".gitignore").unlink()
        self.assertEqual(nudge.status(arguments, agenthelm)["exposure"]["git"], "not-ignored")

    # -- language
    def test_language_follows_the_config_then_the_environment(self) -> None:
        self.assertEqual(nudge.merge_config(None, None)[0]["lang"], "zh")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en"}):
            self.assertEqual(nudge.merge_config(None, None)[0]["lang"], "en")
            self.assertEqual(nudge.merge_config({"language": "zh"}, None)[0]["lang"], "zh")
            self.assertEqual(nudge.merge_config({"language": "zh"}, {"language": "auto"})[0]["lang"], "en", "the user's own auto wins")
            self.assertEqual(nudge.merge_config({"language": "zh"}, {"language": "en"})[0]["lang"], "en")
        config, warnings = nudge.merge_config({"language": "fr"}, None)
        self.assertEqual((config["language"], len(warnings)), ("auto", 1))

    def test_warnings_use_the_resolved_language(self) -> None:
        _, warnings = nudge.merge_config({"language": "en", "pending_threshold": 0}, None)
        self.assertEqual(warnings, ["Project Profile opinion.nudge.pending_threshold must be a positive integer; ignored"])
        _, warnings = nudge.merge_config({"pending_threshold": 0}, None)
        self.assertEqual(warnings, ["项目 Profile opinion.nudge.pending_threshold 须为正整数，已忽略"])

    def english(self, **overrides) -> dict:
        return nudge.merge_config({"language": "en", "pending_threshold": 1, "stop_prompt": "always", **overrides}, None)[0]

    def test_english_session_start_speaks_one_language_to_the_user_and_the_model(self) -> None:
        self.capture("Answer first")
        (self.project / ".gitignore").unlink()
        output = self.start(config=self.english())
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("1 candidate opinions await confirmation", output["systemMessage"])
        self.assertIn("is your decision", output["systemMessage"])
        self.assertIn("[Opinion protocol · agenthelm]", context)
        self.assertIn("--lang en", context)
        self.assertNotRegex(output["systemMessage"], r"[\u4e00-\u9fff]")
        self.assertNotRegex(context.split("原话：")[0], r"[\u4e00-\u9fff]")

    def test_english_prompt_hint_and_stop_request(self) -> None:
        config = self.english()
        payload = {"session_id": "s1", "cwd": str(self.project), "prompt": "from now on, answer first"}
        hint = nudge.prompt_submit(agenthelm, self.env, payload, "claude", self.project, config)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("matched: from now on", hint)
        self.assertIn('--title "<title>"', hint)
        self.assertIn("--lang en", hint)
        self.capture("Answer first")
        request = self.stop(config=config)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("adopt as formal / trial in this project / hold / discard", request)
        self.assertIn("--confirmed --lang en", request)
        self.assertIn("   - Rule: Answer first的完整规则", request)

    def test_english_status_and_install_output(self) -> None:
        write(self.env.config_file, {"nudge": {"language": "en"}})
        self.capture("Answer first")
        completed = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "nudge", "status", "--project", str(self.project)],
                                   capture_output=True, text=True, check=True, env={**os.environ, "HOME": str(self.home)})
        self.assertIn("Now: 1 pending, 0 on trial", completed.stdout)
        self.assertIn("Switch: on", completed.stdout)
        installed = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "nudge", "install-hooks"], capture_output=True, text=True,
                                   check=True, env={**os.environ, "HOME": str(self.home)})
        self.assertIn("Would write (add --apply to do it):", installed.stdout)

    # -- prompt submit
    def prompt(self, text: str, session: str = "s1", client: str = "claude"):
        payload = {"session_id": session, "cwd": str(self.project), "prompt": text}
        return nudge.prompt_submit(agenthelm, self.env, payload, client, self.project, self.config)

    def test_prompt_with_a_lasting_preference_asks_the_model_to_stash_it(self) -> None:
        output = self.prompt("以后都先给结论再解释")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("命中：以后都", context)
        self.assertIn("inbox add", context)
        self.assertIn("--session s1", context)

    def test_prompt_hints_are_limited_per_session_and_skip_irrelevant_or_long_prompts(self) -> None:
        self.assertIsNone(self.prompt("帮我看看这个函数"))
        self.assertIsNone(self.prompt("必须" + "很长的粘贴内容" * 400))
        self.assertIsNone(self.prompt("以后都先给结论", client="cursor"))
        hits = [self.prompt("记住这点：用中文") for _ in range(5)]
        self.assertEqual(sum(hit is not None for hit in hits), self.config["capture_hints_per_session"])
        self.assertIsNotNone(self.prompt("记住这点", session="another"))

    def test_keywords_are_configurable(self) -> None:
        config, _ = nudge.merge_config({"keywords": ["照我说的做"]}, None)
        payload = {"session_id": "s", "prompt": "照我说的做，以后都这样"}
        self.assertIn("照我说的做", nudge.prompt_submit(agenthelm, self.env, payload, "claude", self.project, config)
                      ["hookSpecificOutput"]["additionalContext"])
        payload["prompt"] = "必须"
        self.assertIsNone(nudge.prompt_submit(agenthelm, self.env, payload, "claude", self.project, config))

    # -- stop
    def test_stop_asks_once_for_this_sessions_unpresented_candidates_when_work_is_done(self) -> None:
        mine, other = self.capture("先给结论"), self.capture("用中文", session="s2")
        output = self.stop()
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertIn("1. **先给结论**", context)
        self.assertNotIn("用中文", context)
        self.assertIn("--confirmed", context)
        self.assertIsNone(self.stop())
        by_id = {item["id"]: item["presented"] for item in manager("inbox", "list", "--project", str(self.project))["value"]}
        self.assertEqual((by_id[mine], by_id[other]), (1, 0))

    def test_stop_waits_for_a_completion_signal_and_respects_loop_guards(self) -> None:
        self.capture("先给结论")
        self.assertIsNone(self.stop("我先看看代码结构"))
        self.assertIsNone(self.stop("已完成修改", stop_hook_active=True))
        self.assertIsNotNone(self.stop("All done, pushed."))

    def test_stop_is_silent_without_candidates_from_this_session(self) -> None:
        self.assertIsNone(self.stop())
        self.capture("先给结论", session="other")
        self.assertIsNone(self.stop())

    def test_stop_modes(self) -> None:
        self.capture("先给结论")
        off, _ = nudge.merge_config({"stop_prompt": "off"}, None)
        self.assertIsNone(self.stop(config=off))
        always, _ = nudge.merge_config({"stop_prompt": "always"}, None)
        self.assertIsNotNone(self.stop("还在进行中", config=always))

    def test_stop_budget_is_per_session(self) -> None:
        config, _ = nudge.merge_config({"max_prompts_per_session": 2}, None)
        self.capture("规则一")
        self.assertIsNotNone(self.stop(config=config))
        self.capture("规则二")
        self.assertIsNotNone(self.stop(config=config))
        self.capture("规则三")
        self.assertIsNone(self.stop(config=config))

    def test_codex_stop_uses_a_block_decision(self) -> None:
        self.capture("先给结论")
        output = self.stop(client="codex")
        self.assertEqual(output["decision"], "block")
        self.assertIn("1. **先给结论**", output["reason"])

    def test_cursor_stop_is_off_by_default_and_follows_up_when_enabled(self) -> None:
        self.capture("先给结论")
        self.assertIsNone(self.stop(client="cursor"))
        enabled, _ = nudge.merge_config({"stop_prompt": {"cursor": "on-completion"}}, None)
        self.assertIsNone(self.stop(client="cursor", config=enabled, status="aborted"))
        output = self.stop(client="cursor", config=enabled, status="completed")
        self.assertEqual(list(output), ["followup_message"])
        self.assertIsNone(self.stop(client="cursor", config=enabled, status="completed", loop_count=1))

    # -- entry points
    def test_hook_entry_never_raises_and_honours_the_switches(self) -> None:
        self.capture("先给结论")
        payload = {"session_id": "s1", "cwd": str(self.project), "source": "startup"}
        self.assertIsNotNone(self.hook("session-start", payload))
        self.assertIsNone(self.hook("session-start", payload, extra_env={nudge.ENV_DISABLE: "1"}))
        write(self.env.config_file, {"nudge": {"enabled": False}})
        self.assertIsNone(self.hook("session-start", payload))
        self.env.config_file.write_text("{broken", encoding="utf-8")
        self.assertIsNotNone(self.hook("session-start", payload))
        with mock.patch.object(nudge, "find_manager", side_effect=RuntimeError("boom")):
            self.env.config_file.unlink()
            self.assertIsNone(self.hook("session-start", payload))

    def test_command_line_reads_stdin_and_prints_json(self) -> None:
        self.capture("先给结论")
        payload = json.dumps({"session_id": "s1", "cwd": str(self.project), "source": "startup"})
        environment = {**os.environ, "HOME": str(self.home)}
        completed = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "nudge", "hook", "--event", "session-start"],
                                   input=payload, capture_output=True, text=True, check=False, env=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("inbox add", json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"])
        broken = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "nudge", "hook", "--event", "stop"],
                                input="not json", capture_output=True, text=True, check=False, env=environment)
        self.assertEqual((broken.returncode, broken.stdout), (0, ""))

    def test_status_reports_the_effective_configuration(self) -> None:
        self.capture("规则")
        arguments = argparse.Namespace(project=self.project, home=self.home)
        result = nudge.status(arguments, agenthelm)
        self.assertEqual(result["summary"]["pending"], 1)
        self.assertIsNone(result["reminder"])
        self.assertEqual(result["config"]["pending_threshold"], 5)

    # -- Cursor hook installation
    def install(self, **options):
        defaults = dict(client="cursor", scope="user", project=None, remove=False, apply=False, home=self.home)
        return nudge.install_hooks(argparse.Namespace(**{**defaults, **options}), agenthelm)

    def test_install_hooks_previews_then_merges_without_touching_other_hooks(self) -> None:
        target = self.home / ".cursor" / "hooks.json"
        write(target, {"version": 1, "hooks": {"afterFileEdit": [{"command": "./format.sh"}],
                                                "stop": [{"command": "./mine.sh"}]}})
        preview = self.install()
        self.assertEqual((preview["changed"], preview["applied"]), (True, False))
        self.assertNotIn("agenthelm.py", target.read_text(encoding="utf-8"))
        applied = self.install(apply=True)
        self.assertTrue(applied["applied"])
        hooks = json.loads(target.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(hooks["afterFileEdit"], [{"command": "./format.sh"}])
        self.assertEqual([entry["command"] for entry in hooks["stop"]][0], "./mine.sh")
        self.assertEqual(len(hooks["stop"]), 2)
        self.assertEqual(len(hooks["sessionStart"]), 1)
        self.assertTrue(Path(applied["backup"]).is_file())
        self.assertFalse(self.install(apply=True)["changed"])

    def test_install_hooks_can_remove_only_its_own_entries(self) -> None:
        target = self.project / ".cursor" / "hooks.json"
        write(target, {"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}})
        self.install(scope="project", project=self.project, apply=True)
        self.install(scope="project", project=self.project, apply=True, remove=True)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}})

    def test_install_hooks_refuses_a_broken_file(self) -> None:
        write(self.home / ".cursor" / "hooks.json", "{oops")
        with self.assertRaises(agenthelm.CheckError):
            self.install(apply=True)


class CatalogTests(unittest.TestCase):
    CJK = __import__("re").compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

    def test_both_languages_have_the_same_keys_and_placeholders(self) -> None:
        for key, versions in nudge_text.MESSAGES.items():
            self.assertEqual(set(versions), set(nudge_text.LANGUAGES), key)
            self.assertEqual(set(nudge_text.PLACEHOLDER.findall(versions["zh"])),
                             set(nudge_text.PLACEHOLDER.findall(versions["en"])), key)

    def test_english_text_has_no_chinese(self) -> None:
        for key, versions in nudge_text.MESSAGES.items():
            self.assertFalse(self.CJK.search(versions["en"]), f"{key} 的英文里有中文")
        for key in ("reminder", "prompt_hint", "stop_request", "status_thresholds"):
            self.assertTrue(self.CJK.search(nudge_text.MESSAGES[key]["zh"]), key)

    def test_every_message_formats_in_both_languages(self) -> None:
        for key, versions in nudge_text.MESSAGES.items():
            fields = {name: "x" for language in versions.values() for name in nudge_text.PLACEHOLDER.findall(language)}
            for language in nudge_text.LANGUAGES:
                self.assertTrue(nudge_text.text(key, language, **fields), key)

    def test_both_catalogs_resolve_languages_identically(self) -> None:
        sys.path.insert(0, str(MANAGER.parent))
        import inbox_text
        for requested, environ in ((None, {}), ("en", {"LANG": "zh_CN"}), (None, {"LANG": "fr_FR"}), ("auto", {"LC_ALL": "C", "LANG": "zh_TW"}),
                                   (None, {"AGENTHELM_LANG": "en"}), ("zh", {"AGENTHELM_LANG": "en"})):
            self.assertEqual(nudge_text.resolve_language(requested, environ), inbox_text.resolve_language(requested, environ), (requested, environ))


class ShippedHooksTests(unittest.TestCase):
    def test_hooks_json_registers_every_event_with_existing_commands(self) -> None:
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "Stop"})
        commands = [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertIn('"${CLAUDE_PLUGIN_ROOT}/runtime/agenthelm.py"', command)
            self.assertTrue(all(isinstance(hook["timeout"], int) for groups in hooks.values() for group in groups for hook in group["hooks"]))
        starts = {group["matcher"] for group in hooks["SessionStart"]}
        self.assertEqual(starts, {"startup", "startup|resume|clear|compact"})
        self.assertTrue((RUNTIME / "agenthelm.py").is_file())

    def test_render_covers_each_client_and_event(self) -> None:
        self.assertIsNone(nudge.render("claude", "stop"))
        self.assertEqual(nudge.render("claude", "prompt-submit", context="x")["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(nudge.render("codex", "stop", context="x"), {"decision": "block", "reason": "x"})
        self.assertEqual(nudge.render("cursor", "stop", context="x"), {"followup_message": "x"})
        self.assertIsNone(nudge.render("cursor", "prompt-submit", context="x"))


if __name__ == "__main__":
    unittest.main()
