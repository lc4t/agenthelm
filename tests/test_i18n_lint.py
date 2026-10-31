"""Static i18n rules for the runtime code (issue #24, items 1 and 8).

* Rule 1: a Chinese string literal in runtime code must be wrapped in ``_()`` / ``N_()`` unless it is on the
  explicit ALLOWED list below (data, file content, regular expressions, accepted input words).
* Rule 2: the *result* of ``_()`` must not be compared, put in a set/tuple that is compared, or used with
  ``startswith`` / ``endswith``: English would then change behaviour, not just wording.
* The allowed list is kept small and honest: an entry that no longer matches any literal fails the test.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works for `python -m unittest tests/test_i18n_lint.py`
from test_i18n import CJK, LAYOUT, PLUGINS, ROOT, runtime_sources

TRANSLATE = ("_", "N_")

# (path relative to plugins/, enclosing function or "<module>" or "*" for the whole file) -> why a Chinese literal is fine there.
ALLOWED: dict[tuple[str, str], str] = {
    ("agent-pack/runtime/agent_pack_nudge.py", "<module>"): "default trigger keywords: user input words, they come with English siblings",
    ("agent-pack/runtime/agenthelm_runtime.py", "<module>"): "text of the generated launcher script, written to disk",
    ("opinion-manager/runtime/opinion_manager.py", "render_opinion"): "OPINION.md body, language independent by design",
    ("opinion-manager/runtime/versioned.py", "render"): "OPINION.md body, language independent by design",
    ("opinion-manager/runtime/versioned.py", "custom_section"): "OPINION.md custom-rules heading, shared by the body and the context slices",
    ("opinion-manager/runtime/versioned.py", "render_rule"): "OPINION.md rule text, language independent by design",
    ("project-orchestrator/runtime/assets.py", "<module>"): "regular expressions that match Chinese file names and dates",
    ("project-orchestrator/runtime/assets.py", "_extract_edges"): "regular expression that stops a path at Chinese punctuation",
    ("project-orchestrator/runtime/assets.py", "classify_broken"): "classifies user text (a full-width colon) - data, not output",
    ("project-orchestrator/runtime/case_structure.py", "<module>"): "vocabulary tables, the Chinese and English spellings side by side",
    ("project-orchestrator/runtime/memory.py", "<module>"): "section names of memory files (project file language, issue #24 item 3)",
    ("project-orchestrator/runtime/memory.py", "index_lines"): "memory index text (project file language, issue #24 item 3)",
    ("project-orchestrator/runtime/memory.py", "command_update"): "retire marker appended to a memory file (issue #24 item 3)",
    ("project-orchestrator/runtime/private_paths.py", "<module>"): ".gitignore text written to disk, pinned by test_private_paths_sync",
    ("project-orchestrator/runtime/project_demo.py", "*"): "demo scenarios: every string is checked by test_demo_data_is_fully_translated",
    ("project-orchestrator/runtime/project_menu.py", "ask"): "accepted input words (quit / back)",
    ("project-orchestrator/runtime/project_menu.py", "confirm"): "accepted input word (yes)",
    ("project-orchestrator/runtime/project_menu.py", "run"): "accepted input word (help)",
    ("project-orchestrator/runtime/sop.py", "parse_steps"): "regular expressions that parse Chinese and English SOP files",
    ("project-orchestrator/runtime/sop.py", "split_list"): "list separators accepted when parsing SOP files",
    ("project-orchestrator/runtime/sop.py", "parse_requirements"): "regular expressions that parse Chinese and English SOP files",
    ("project-orchestrator/runtime/sop.py", "options_for"): "compares the untranslated source label; the label itself goes through _()",
    ("project-orchestrator/runtime/sop.py", "cmd_run"): "normalises list separators written into the executed Case",
    ("project-orchestrator/runtime/sop_structure.py", "*"): "vocabulary tables, the Chinese and English spellings side by side",
    ("project-orchestrator/runtime/wf.py", "command"): "accepted input word (recent)",
    ("project-orchestrator/runtime/wf.py", "interactive"): "accepted input words (quit / help / recent)",
    ("agents-init/skills/agents-init/scripts/adopt.py", "<module>"): "keyword lists that classify the user's own AGENTS.md, and text of the moved-content file",
    ("agents-init/skills/agents-init/scripts/init_project.py", "<module>"): ".gitignore text written to disk, pinned by test_private_paths_sync",
    ("agents-init/skills/agents-init/scripts/init_project.py", "readme_block"): "README block appended to the project (project file language, issue #24 item 3)",
    ("agents-init/skills/agents-init/scripts/init_project.py", "files_for"): "description written into the generated plugin.json (issue #24 item 3)",
}


def scope_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    scope = "<module>"
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name  # the innermost function wins
            break
    return scope


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def is_translate_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in TRANSLATE


def unwrapped_literals(path) -> list[tuple[int, str, str]]:
    """(line, enclosing function, literal) for every Chinese literal outside ``_()`` / ``N_()`` and docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = parent_map(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value)):
            continue
        if isinstance(parents[node], ast.Expr):  # docstrings and bare strings
            continue
        ancestor, wrapped = node, False
        while ancestor in parents:
            ancestor = parents[ancestor]
            if is_translate_call(ancestor):
                wrapped = True
                break
        if not wrapped:
            found.append((node.lineno, scope_of(node, parents), node.value))
    return found


class UnwrappedLiteralTests(unittest.TestCase):
    def test_no_chinese_literal_outside_translation_or_the_allowed_list(self) -> None:
        stray = []
        for name in LAYOUT:
            for path in runtime_sources(name):
                relative = path.relative_to(PLUGINS).as_posix()
                for line, scope, text in unwrapped_literals(path):
                    if (relative, scope) not in ALLOWED and (relative, "*") not in ALLOWED:
                        stray.append(f"{relative}:{line} in {scope}(): {text[:40]!r}")
        self.assertEqual(stray, [], "中文字面量没有用 _() 包装；是输出文案就包装并补 en.json，确属数据/文件内容就加入 ALLOWED 并写明理由")

    def test_every_allowed_entry_still_matches_something(self) -> None:
        used: set[tuple[str, str]] = set()
        for name in LAYOUT:
            for path in runtime_sources(name):
                relative = path.relative_to(PLUGINS).as_posix()
                for _line, scope, _text in unwrapped_literals(path):
                    used.update({(relative, scope), (relative, "*")})
        self.assertEqual([entry for entry in ALLOWED if entry not in used], [], "ALLOWED 里有不再命中的条目，删掉它")

    def test_every_allowed_entry_names_an_existing_file_and_a_reason(self) -> None:
        for (relative, _scope), reason in ALLOWED.items():
            self.assertTrue((PLUGINS / relative).is_file(), relative)
            self.assertGreater(len(reason.strip()), 10, (relative, _scope))


class TranslatedResultIsOnlyTextTests(unittest.TestCase):
    """``_()`` returns English or Chinese depending on the language, so its result is never data."""

    def test_translated_text_is_not_compared_or_matched(self) -> None:
        offenders = []
        for name in LAYOUT:
            for path in runtime_sources(name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                parents = parent_map(tree)
                for node in ast.walk(tree):
                    if not is_translate_call(node):
                        continue
                    parent = parents.get(node)
                    # climb through containers such as {_("a"), _("b")} / (_("a"), _("b")) used on the right of a comparison
                    while isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
                        node, parent = parent, parents.get(parent)
                    bad = isinstance(parent, ast.Compare) \
                        or (isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Sub, ast.BitAnd, ast.BitOr, ast.BitXor)) and isinstance(node, ast.Set)) \
                        or (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute)
                            and parent.func.attr in ("startswith", "endswith") and node in parent.args) \
                        or (isinstance(parent, ast.Attribute) and parent.attr in ("startswith", "endswith"))
                    if bad:
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])


class LocalesShipTests(unittest.TestCase):
    """#24 item 8: the English catalogs travel with every package (source, marketplace entry and npm copy)."""

    def test_each_plugin_ships_its_catalog_in_the_source_tree(self) -> None:
        for name, (_module, directory) in LAYOUT.items():
            catalog = directory / "locale/en.json"
            self.assertTrue(catalog.is_file(), f"{name}: {catalog.relative_to(ROOT)} 不存在")
            self.assertGreater(len(json.loads(catalog.read_text(encoding="utf-8"))), 0, name)

    def test_the_user_level_runtime_and_the_client_install_copy_the_catalogs(self) -> None:
        runtime_dir = str(LAYOUT["agent-pack"][1])
        sys.path.insert(0, runtime_dir)
        try:
            import agent_pack_config
            import agenthelm_runtime
        finally:
            sys.path.pop(0)
        shipped = {path.as_posix() for path in agenthelm_runtime._shipped_files(ROOT)}  # what `agenthelm runtime install` copies
        for name, (_module, directory) in LAYOUT.items():
            relative = (directory / "locale/en.json").relative_to(ROOT).as_posix()
            self.assertIn(relative, shipped, f"{name}: runtime install 不会复制 locale/en.json")
            with tempfile.TemporaryDirectory() as temporary:  # the copy a client install makes of the marketplace directory
                copy = Path(temporary) / name
                shutil.copytree(PLUGINS / name, copy, symlinks=False, ignore=shutil.ignore_patterns(*agent_pack_config.COPY_IGNORE))
                self.assertTrue((copy / directory.relative_to(PLUGINS / name) / "locale/en.json").is_file(), f"{name}: 客户端安装副本里没有 locale/en.json")

    def test_the_marketplace_lists_all_four_plugins_from_the_directories_that_hold_the_catalogs(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        sources = {entry["name"]: entry["source"] for entry in marketplace["plugins"]}
        for name in LAYOUT:
            self.assertEqual(sources.get(name), f"./plugins/{name}", name)

    @unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "node/npm is not installed")
    def test_the_npm_package_contains_the_catalogs(self) -> None:
        package = ROOT / "packages/cli"
        subprocess.run(["node", "scripts/sync-runtime.mjs"], cwd=package, check=True)
        for name, (_module, directory) in LAYOUT.items():
            copied = package / "runtime" / directory.relative_to(ROOT) / "locale/en.json"
            self.assertEqual(copied.read_bytes(), (directory / "locale/en.json").read_bytes(), f"{name}: sync-runtime 的产物缺少 locale/en.json")
        with tempfile.TemporaryDirectory() as home:  # a throw-away HOME keeps npm away from the real configuration
            packed = subprocess.run(["npm", "pack", "--dry-run", "--json", "--ignore-scripts"], cwd=package, capture_output=True, text=True,
                                    env={**os.environ, "HOME": home, "npm_config_cache": str(Path(home) / "cache")}, check=False)
        self.assertEqual(packed.returncode, 0, packed.stderr)
        files = {item["path"] for item in json.loads(packed.stdout)[0]["files"]}
        for name, (_module, directory) in LAYOUT.items():
            self.assertIn(f"runtime/{(directory / 'locale/en.json').relative_to(ROOT).as_posix()}", files, f"{name}: npm 包里没有 locale/en.json")


class WorkflowTests(unittest.TestCase):
    def test_ci_runs_the_suites_under_three_host_locales(self) -> None:
        text = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertIn("LANG: ${{ matrix.lang }}", text)
        for lang in ("'C'", "'en_US.UTF-8'", "'zh_CN.UTF-8'"):
            self.assertIn(lang, text)
