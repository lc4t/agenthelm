"""运行时文案的多语言：四个插件各带一份语言模块与英文译文，默认英文，中文原样。

守住几件事：四份语言模块行为一致；源码里每个 ``_("…")`` 都有英文译文；译文的占位符与原文一致、不含漏翻的中文；
``_`` 没有被局部变量遮蔽；演示数据完整；英文下的真实命令输出里没有漏翻的中文。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"
AGENTHELM = PLUGINS / "agent-pack/runtime/agenthelm.py"
# 插件目录 → (语言模块名, 含 locale/ 的运行时目录)
LAYOUT = {
    "agent-pack": ("i18n_agent_pack", PLUGINS / "agent-pack/runtime"),
    "opinion-manager": ("i18n_opinion", PLUGINS / "opinion-manager/runtime"),
    "project-orchestrator": ("i18n_orchestrator", PLUGINS / "project-orchestrator/runtime"),
    "agents-init": ("i18n_init", PLUGINS / "agents-init/skills/agents-init/scripts"),
}
CJK = re.compile(r"[　-〿一-鿿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\d+)(![rsa])?(:[^{}]*)?\}")
SKIP_FILES = {"inbox_text.py", "nudge_text.py", "constants.py"}  # 这两份有自己的中英对照表；constants 是写进项目文件的占位正文


def load(name: str):
    module_name, directory = LAYOUT[name]
    spec = importlib.util.spec_from_file_location(f"_test_{module_name}", directory / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalog(name: str) -> dict[str, str]:
    return json.loads((LAYOUT[name][1] / "locale/en.json").read_text(encoding="utf-8"))


def runtime_sources(name: str) -> list[Path]:
    directory = LAYOUT[name][1]
    return sorted(path for path in directory.glob("*.py")
                  if not path.name.startswith(("test_", "i18n_")) and path.name not in SKIP_FILES)


def message_calls(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "N_") and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.append((node.lineno, node.func.id, node.args[0].value))
    return found


class ResolverTests(unittest.TestCase):
    def test_all_four_language_modules_resolve_alike(self) -> None:
        modules = [load(name) for name in LAYOUT]
        cases = [(None, {}), ("en", {"LANG": "zh_CN.UTF-8"}), ("zh", {"AGENTHELM_LANG": "en"}), ("auto", {"AGENTHELM_LANG": "zh"}),
                 (None, {"AGENTHELM_LANG": "en", "LANG": "zh_CN"}), (None, {"LC_ALL": "zh_TW.UTF-8"}), (None, {"LC_ALL": "C", "LANG": "zh_CN"}),
                 (None, {"LANG": "fr_FR.UTF-8"}), (None, {"LANG": "C.UTF-8"}), ("", {"LANG": "POSIX"}), ("ZH-cn", {})]
        for requested, environ in cases:
            results = {module.resolve_language(requested, environ) for module in modules}
            self.assertEqual(len(results), 1, (requested, environ, results))

    def test_resolution_order_and_english_fallback(self) -> None:
        module = load("agent-pack")
        self.assertEqual(module.resolve_language(None, {}), "en")
        self.assertEqual(module.resolve_language(None, {"LANG": "zh_CN.UTF-8"}), "zh")
        self.assertEqual(module.resolve_language(None, {"LC_ALL": "C", "LANG": "zh_CN.UTF-8"}), "zh")
        self.assertEqual(module.resolve_language("en", {"AGENTHELM_LANG": "zh"}), "en")
        self.assertEqual(module.resolve_language(None, {"AGENTHELM_LANG": "zh", "LANG": "en_US"}), "zh")

    def test_the_user_config_language_sits_between_the_environment_and_the_locale(self) -> None:
        module = load("opinion-manager")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agenthelm" / "config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"language": "zh"}), encoding="utf-8")
            environ = {"XDG_CONFIG_HOME": directory, "LANG": "en_US.UTF-8"}
            self.assertEqual(module.resolve_language(None, environ), "zh")
            self.assertEqual(module.resolve_language(None, {**environ, "AGENTHELM_LANG": "en"}), "en")
            config.write_text("{not json", encoding="utf-8")
            self.assertEqual(module.resolve_language(None, environ), "en")
            config.write_text(json.dumps({"language": "auto"}), encoding="utf-8")
            self.assertEqual(module.resolve_language(None, {**environ, "LANG": "zh_CN"}), "zh")

    def test_translation_follows_the_environment_at_call_time(self) -> None:
        module = load("project-orchestrator")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "zh"}):
            self.assertEqual(module._("没有 Case。"), "没有 Case。")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en"}):
            self.assertEqual(module._("没有 Case。"), "No Cases.")
            self.assertEqual(module._("没有翻译的文案"), "没有翻译的文案", "a missing translation falls back to the original")
            self.assertEqual(module.N_("没有 Case。"), "没有 Case。", "N_ only marks")


class CatalogTests(unittest.TestCase):
    def test_every_message_in_the_source_has_an_english_translation(self) -> None:
        for name in LAYOUT:
            entries = catalog(name)
            for path in runtime_sources(name):
                for line, function, text in message_calls(path):
                    if CJK.search(text):
                        self.assertIn(text, entries, f"{path.relative_to(ROOT)}:{line} {function}({text[:40]!r}) 没有英文译文")

    def test_translations_keep_placeholders_and_leave_no_chinese_behind(self) -> None:
        for name in LAYOUT:
            for original, english in catalog(name).items():
                plain = original.replace("{{", "").replace("}}", "")
                self.assertEqual(sorted(PLACEHOLDER.findall(plain)), sorted(PLACEHOLDER.findall(english.replace("{{", "").replace("}}", ""))),
                                 f"{name}: {original!r}")
                self.assertEqual(sum(original.count(token) for token in ("%s", "%d", "%r")),
                                 sum(english.count(token) for token in ("%s", "%d", "%r")), f"{name}: {original!r}")
                self.assertFalse(CJK.search(english), f"{name}: {english!r} 里有漏翻的中文")
                self.assertTrue(english.strip() or not original.strip(), f"{name}: {original!r} 的译文为空")
                if PLACEHOLDER.search(plain) or "{{" in original:
                    english.format(*["x"] * (1 + max(int(item[0]) for item in PLACEHOLDER.findall(english.replace("{{", "").replace("}}", "")) or [(-1,)])))

    def test_no_function_shadows_the_translation_function(self) -> None:
        class Collector(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stores: list[int] = []

            def visit_Name(self, node: ast.Name) -> None:
                if node.id in ("_", "N_") and isinstance(node.ctx, ast.Store):
                    self.stores.append(node.lineno)

            def visit_FunctionDef(self, node: ast.AST) -> None:  # nested functions have their own scope
                pass

            visit_AsyncFunctionDef = visit_Lambda = visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_FunctionDef

        for name in LAYOUT:
            for path in runtime_sources(name):
                for function in [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                    uses = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_" for node in ast.walk(function))
                    collector = Collector()
                    for statement in function.body:
                        collector.visit(statement)
                    arguments = [arg.arg for arg in function.args.args + function.args.kwonlyargs]
                    if function.args.vararg:
                        arguments.append(function.args.vararg.arg)
                    self.assertFalse(uses and (collector.stores or "_" in arguments),
                                     f"{path.relative_to(ROOT)}:{function.lineno} {function.name} 里 _ 被赋值，会遮蔽翻译函数")

    def test_demo_data_is_fully_translated(self) -> None:
        sys.path.insert(0, str(LAYOUT["project-orchestrator"][1]))
        try:
            import project_demo
        finally:
            sys.path.pop(0)
        entries = catalog("project-orchestrator")
        missing: list[str] = []

        def walk(value: object, key: str | None = None) -> None:
            if key in project_demo.KEEP_KEYS:
                return
            if isinstance(value, dict):
                for item_key, item in value.items():
                    walk(item, item_key)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)
            elif isinstance(value, str) and CJK.search(value) and value not in entries:
                missing.append(value)

        for scenario in project_demo.SCENARIOS.values():
            walk(scenario)
        self.assertEqual(missing, [])

    def test_the_two_hand_written_catalogs_stay_in_step(self) -> None:
        for path in (PLUGINS / "agent-pack/runtime/nudge_text.py", PLUGINS / "opinion-manager/runtime/inbox_text.py"):
            namespace: dict = {}
            sys.path.insert(0, str(path.parent))
            try:
                spec = importlib.util.spec_from_file_location(f"_test_{path.stem}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            finally:
                sys.path.pop(0)
            for key, versions in module.MESSAGES.items():
                self.assertEqual(set(versions), {"zh", "en"}, key)
                self.assertFalse(CJK.search(versions["en"]), f"{path.name}:{key}")


def drive(command: list[str], inputs: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run the command in a pseudo-terminal and type the inputs one line at a time (the menu only opens on a terminal)."""
    import pty
    import select
    import time

    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execve(command[0], command, env)
    output = b""

    def drain(seconds: float) -> None:
        nonlocal output
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                output += chunk

    drain(1.5)
    for line in inputs:
        os.write(fd, (line + "\r").encode("utf-8"))
        drain(0.4)
    deadline = time.monotonic() + 10
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            status = 9
            break
        drain(0.1)
    drain(0.2)
    return os.waitstatus_to_exitcode(status), re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output.decode("utf-8", "replace")).replace("\r", "")


class EnglishOutputTests(unittest.TestCase):
    """英文下的真实命令输出里不应有漏翻的中文（引号里引用的界面名称与用户数据除外）。"""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.home.mkdir()
        self.environment = {**{key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "LC_", "LANG"))},
                            "HOME": str(self.home), "AGENTHELM_LANG": "en", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}

    def run_command(self, *command: str, language: str = "en", cwd: Path | None = None, expect: tuple[int, ...] = (0, 1, 2, 3, 4, 5)):
        environment = {**self.environment, "AGENTHELM_LANG": language}
        completed = subprocess.run([sys.executable, *command], capture_output=True, text=True, env=environment, cwd=cwd, timeout=120, check=False)
        self.assertIn(completed.returncode, expect, completed.stderr)
        return completed

    def assertEnglish(self, completed: subprocess.CompletedProcess, label: str) -> None:
        for line in (completed.stdout + "\n" + completed.stderr).splitlines():
            self.assertFalse(CJK.search(line), f"{label}: 英文输出里有漏翻的中文：{line[:160]}")

    def test_the_demo_projects_speak_english(self) -> None:
        for scenario in ("study", "personal", "research", "blank"):
            for subcommand in (["guide"], ["progress", "--all"], ["health"], ["fix"], ["tools"], ["capabilities"], ["find", "x"],
                               ["record", "new", "--title", "t", "--goal", "g"]):
                completed = self.run_command(str(AGENTHELM), "project", "--demo", scenario, "--no-input", *subcommand)
                self.assertEnglish(completed, f"{scenario} {' '.join(subcommand)}")
        completed = self.run_command(str(AGENTHELM), "project", "--demo", "study", "--no-input", "scenarios")
        self.assertIn("JLPT N2 exam prep", completed.stdout)

    def test_the_interactive_menu_speaks_english(self) -> None:
        scripts = [("study", ["1", "summary", "v1", "o1", "", "2", "", "3", "", "4", "", "5", "", "6", "n", "Title", "Goal", "", "no", "7", "", "?", "99", "q"]),
                   ("research", ["1", "x", "", "2", "1", "", "4", "", "5", "", "6", "x", "", "7", "1", "", "q"]),
                   ("blank", ["1", "", "2", "", "6", "n", "T", "G", "", "yes", "", "q"])]
        for scenario, inputs in scripts:
            executable = [str(AGENTHELM), "project", "--demo", scenario]
            code, text = drive([sys.executable, str(AGENTHELM), "project", "--demo", scenario], inputs, self.environment)
            self.assertEqual(code, 0, text[-400:])
            for line in text.splitlines():
                self.assertFalse(CJK.search(line), f"menu {scenario}: 英文菜单里有漏翻的中文：{line[:160]}")
        code, text = drive([sys.executable, str(AGENTHELM), "project", "--demo", "study", "--demo-fault", "index-broken"],
                           ["summary", "5", "yes", "", "q"], self.environment)
        self.assertEqual(code, 0, text[-400:])
        self.assertIn("Rebuild the file index", text)
        for line in text.splitlines():
            self.assertFalse(CJK.search(line), f"menu fix: 英文菜单里有漏翻的中文：{line[:160]}")

    def test_every_help_screen_speaks_english(self) -> None:
        orchestrator = PLUGINS / "project-orchestrator/runtime"
        for command in ([str(AGENTHELM), "--help"], [str(AGENTHELM), "check", "--help"], [str(AGENTHELM), "runtime", "--help"],
                        [str(AGENTHELM), "nudge", "--help"], [str(AGENTHELM), "project", "--help"], [str(AGENTHELM), "project", "fix", "--help"],
                        [str(orchestrator / "wf.py"), "--help"], [str(orchestrator / "assets.py"), "--help"], [str(orchestrator / "case.py"), "--help"],
                        [str(orchestrator / "memory.py"), "--help"], [str(orchestrator / "case_workspace.py"), "--help"]):
            self.assertEnglish(self.run_command(*command), " ".join(command[:2])[-60:])

    def test_the_demo_faults_speak_english(self) -> None:
        for fault in ("index-broken", "offline", "runtime-old", "conflict"):
            for subcommand in (["health"], ["fix"], ["guide"]):
                completed = self.run_command(str(AGENTHELM), "project", "--demo", "study", "--demo-fault", fault, "--no-input", *subcommand)
                self.assertEnglish(completed, f"{fault} {' '.join(subcommand)}")

    def test_the_same_demo_stays_chinese_when_asked(self) -> None:
        completed = self.run_command(str(AGENTHELM), "project", "--demo", "study", "--no-input", "guide", language="zh")
        self.assertIn("这个项目做什么", completed.stdout)
        self.assertIn("日语 N2 备考", completed.stdout)

    def test_the_agenthelm_commands_speak_english(self) -> None:
        for command in (["runtime", "status"], ["runtime", "install"], ["runtime", "use", "9.9.9"], ["nudge", "status"],
                        ["nudge", "install-hooks", "--client", "cursor"], ["check", "--offline", "--no-project"], ["doctor", "--no-project"],
                        ["project", "--project", str(self.home), "--no-input", "health"], ["project", "--no-input", "--demo", "nope", "guide"]):
            completed = self.run_command(str(AGENTHELM), *command, "--home", str(self.home)) if command[0] in ("check", "doctor", "runtime", "nudge") \
                else self.run_command(str(AGENTHELM), *command)
            self.assertEnglish(completed, " ".join(command))

    def test_a_real_project_speaks_english(self) -> None:
        project = Path(self.directory.name) / "project"
        profile = project / ".agents/moe.sakanano.agent-pack/project.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"schema_version": "2.0", "language": "en", "work": {"mode": "case-workspace", "case": {"root": "cases"}}}),
                           encoding="utf-8")
        case = PLUGINS / "project-orchestrator/runtime/case.py"
        assets = PLUGINS / "project-orchestrator/runtime/assets.py"
        steps = [
            [str(case), "--project", str(project), "new", "--title", "Quarterly report", "--slug", "quarterly", "--goal", "Ship it", "--next-action", "Collect data"],
            [str(case), "--project", str(project), "list"],
            [str(case), "--project", str(project), "show", "quarterly"],
            [str(case), "--project", str(project), "update", "quarterly"],
            [str(case), "--project", str(project), "validate"],
            [str(assets), "--project", str(project), "find", "report"],
            [str(assets), "--project", str(project), "cases"],
            [str(assets), "--project", str(project), "check"],
            [str(assets), "--project", str(project), "refs", "nothing/here.md"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "progress"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "find", "nothing"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "health", "--offline"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "guide"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "show", "missing"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "record", "new", "--title", "Second", "--goal", "Goal"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "record", "new", "--title", "Third", "--goal", "Goal", "--apply"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "record", "quarterly", "--log", "Did a thing", "--apply"],
            [str(AGENTHELM), "project", "--project", str(project), "--no-input", "show", "quarterly"],
        ]
        for step in steps:
            self.assertEnglish(self.run_command(*step), " ".join(step[1:4]))
        case_file = next((project / "cases").glob("CASE-*/CASE.md"))
        self.assertFalse(CJK.search(case_file.read_text(encoding="utf-8")), "an English project writes an English CASE.md")

    def test_the_language_changes_only_the_words_never_the_facts(self) -> None:
        """同一个项目在两种语言下：发现 ID、脚手架指纹与已记录的决定完全一致；决定在一种语言下写、另一种语言下读。"""
        initializer = str(PLUGINS / "agents-init/skills/agents-init/scripts/init_project.py")
        project = Path(self.directory.name) / "twin"
        project.mkdir()
        state = Path(self.directory.name) / "state"
        made = self.run_command(initializer, "--project", str(project), "--name", "Twin", "--project-type", "notes", "--vcs", "none",
                                "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude", "--output", "json", "--apply", language="zh")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        for relative in (".agents/plugin.json", ".agents/mcp.json"):
            (project / relative).unlink()

        def check(language: str) -> dict:
            completed = self.run_command(initializer, "--project", str(project), "--mode", "check", "--output", "json", "--state-dir", str(state), language=language)
            return json.loads(completed.stdout)

        chinese, english = check("zh"), check("en")
        self.assertEqual([item["id"] for item in chinese["findings"]], [item["id"] for item in english["findings"]])
        self.assertEqual(chinese.get("fingerprint"), english.get("fingerprint"))
        self.assertNotEqual([item["title"] for item in chinese["findings"]], [item["title"] for item in english["findings"]])
        self.assertFalse(any(CJK.search(item["title"] + item["reason"]) for item in english["findings"]))
        decided = self.run_command(initializer, "--project", str(project), "--mode", "decide", "--target", "skel:.agents/mcp.json", "--decision", "skip",
                                   "--reason", "不需要 not needed", "--apply", "--output", "json", "--state-dir", str(state), language="zh")
        self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
        self.assertNotIn("skel:.agents/mcp.json", [item["id"] for item in check("en")["findings"]])
        reopened = self.run_command(initializer, "--project", str(project), "--mode", "decide", "--target", "skel:.agents/mcp.json", "--decision", "unskip",
                                    "--apply", "--output", "json", "--state-dir", str(state), language="en")
        self.assertEqual(reopened.returncode, 0, reopened.stdout + reopened.stderr)
        self.assertIn("skel:.agents/mcp.json", [item["id"] for item in check("zh")["findings"]])

    def test_the_opinion_files_do_not_depend_on_the_language(self) -> None:
        """OPINION.md 与锁按 Profile 确定性渲染：Profile 预览、正文与两份确认指纹都不随界面语言变化。"""
        manager = str(PLUGINS / "opinion-manager/runtime/opinion_manager.py")
        catalog = str(PLUGINS / "opinion-manager/catalog")
        results = []
        for language in ("zh", "en"):
            project = Path(self.directory.name) / f"opinion-{language}"
            project.mkdir()
            profile = self.run_command(manager, "profile", "--project", str(project), "--id", "private.i18n", "--version", "1.0.0",
                                       "--template", "response-style/brief-chinese@1.0.0", "--catalog", catalog, "--output", "json", language=language)
            preview = json.loads(profile.stdout)
            self.assertTrue(preview["ok"], profile.stdout)
            saved = project / "profile.json"
            saved.write_text(json.dumps(preview["profile"]), encoding="utf-8")
            compose = self.run_command(manager, "compose", "--project", str(project), "--profile", str(saved), "--output", "json", language=language)
            composed = json.loads(compose.stdout)
            self.assertTrue(composed["ok"], compose.stdout)
            results.append((preview["confirmation_sha256"], composed["confirmation_sha256"], composed["content"]))
        self.assertEqual(results[0], results[1])
        self.assertIn("简", results[0][2], "the rendered body is the Chinese rule text in both languages")

    def test_opinion_manager_and_agents_init_speak_english(self) -> None:
        manager = PLUGINS / "opinion-manager/runtime/opinion_manager.py"
        initializer = PLUGINS / "agents-init/skills/agents-init/scripts/init_project.py"
        project = Path(self.directory.name) / "opinion-project"
        project.mkdir()
        catalog = self.run_command(str(manager), "catalog")
        self.assertIn("# Opinion template catalog", catalog.stdout)
        for command in ([str(manager), "compose", "--project", str(project)],
                        [str(manager), "compose", "--project", str(project), "--template", "no/such@1.0.0"],
                        [str(manager), "verify", "--project", str(project)], [str(manager), "context", "core", "--project", str(project)],
                        [str(manager), "lifecycle", "status", "--project", str(project), "--store", "x"],
                        [str(initializer), "--mode", "check", "--target", str(project)], [str(initializer), "--help"], [str(manager), "--help"]):
            self.assertEnglish(self.run_command(*command), " ".join(command[1:3]))


if __name__ == "__main__":
    unittest.main()
