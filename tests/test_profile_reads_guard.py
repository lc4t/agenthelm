"""守卫：各插件运行时代码读取的 Project Profile 键，必须在 project.schema.json 声明，或登记在
scaffold.READ_BY_OTHER_PLUGINS。新增读取而未登记会失败。

扫描方式（尽力而为的 AST 静态扫描，不追求完备）
- 范围：plugins/*/runtime/*.py、plugins/agents-init/skills/agents-init/scripts/*.py（不含 test_*.py 与 tests/）。
- 读取点：以“看起来是 Profile”的表达式为根，沿 ``.get("a")`` / ``["a"]`` / ``(x or {})`` / ``x if isinstance(...) else {}``
  取常量字符串键，得到点分路径。根：名字或属性名含 ``profile``（profile、self.profile、new_profile、project_profile(...)
  等调用）。
- 别名：同一函数内 ``work = profile.get("work") or {}`` 这类赋值，把 ``work`` 当作路径 ``work`` 的别名，之后
  ``work.get("mode")`` 解析成 ``work.mode``。别名按函数作用域记录，迭代两轮以覆盖“别名的别名”。
- 只检查顶层段 SECTIONS（opinion / capabilities / work / privacy / sop / memory）。缩小范围的原因：
  opinion-manager 里也有名叫 profile 的变量，那是 Opinion Profile（id / version / sources / loading…），
  不是 Project Profile，全量扫描会产生大量误报。
- 判定：读到的路径，自身或某个祖先在 schema 里声明且该节点没有 ``properties``（自由形态对象 / 数组）视为已声明；
  路径落在 READ_BY_OTHER_PLUGINS 的键或其子路径下视为已登记。路径比 schema 更深一层未声明的键会失败。

已知盲区
- 动态键（``profile.get(key)``）、``**`` 展开、``getattr``、经由函数参数传入的子段（跨函数别名）、
  ``dict.items()`` 迭代、字符串拼接的路径、非 Python 代码（shell、模板里的 Profile 引用）都扫不到。
- 根的识别靠名字含 ``profile``；若别的 dict 变量也叫 profile 且顶层键恰好同名（如 ``work``），会误报，
  此时把路径加进 IGNORED_PATHS 并写明理由。

白名单 IGNORED_PATHS：目前为空。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_SCRIPTS = ROOT / "plugins/agents-init/skills/agents-init/scripts"
SCHEMA = ROOT / "plugins/agents-init/skills/agents-init/project.schema.json"
SECTIONS = {"opinion", "capabilities", "work", "privacy", "sop", "memory"}
IGNORED_PATHS: dict[str, str] = {}  # 路径 -> 理由


def scanned_files() -> list[Path]:
    files = sorted((ROOT / "plugins").glob("*/runtime/*.py")) + sorted(INIT_SCRIPTS.glob("*.py"))
    return [path for path in files if not path.name.startswith("test_")]


def load_read_by_other_plugins() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("scaffold_for_reads_guard", INIT_SCRIPTS / "scaffold.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.READ_BY_OTHER_PLUGINS)


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    return ""


def _const_key(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def resolve(node: ast.AST, aliases: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    """The Profile path this expression reads (``()`` = the whole Profile), or None if it is not a Profile read."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return resolve(node.values[0], aliases)
    if isinstance(node, ast.IfExp):
        return resolve(node.body, aliases)
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    if isinstance(node, ast.Subscript):
        base, key = resolve(node.value, aliases), _const_key(node.slice)
        return base + (key,) if base is not None and key else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and _const_key(node.args[0])):
        base = resolve(node.func.value, aliases)
        return base + (_const_key(node.args[0]),) if base is not None else None
    if "profile" in _name_of(node).lower():
        return ()
    return None


def _own_nodes(scope: ast.AST):
    """Nodes of ``scope`` without descending into nested functions (they are scopes of their own)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def reads_in(source: str) -> set[tuple[tuple[str, ...], int]]:
    tree = ast.parse(source)
    found: set[tuple[tuple[str, ...], int]] = set()
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        aliases: dict[str, tuple[str, ...]] = {}
        assigns = [n for n in _own_nodes(scope) if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name)]
        for _ in range(2):
            for node in assigns:
                path = resolve(node.value, aliases)
                if path:  # 非空：整个 Profile 本身不当别名
                    aliases[node.targets[0].id] = path
        for node in _own_nodes(scope):
            if isinstance(node, (ast.Subscript, ast.Call)):
                path = resolve(node, aliases)
                if path and path[0] in SECTIONS:
                    found.add((path, node.lineno))
    return found


def declared(path: tuple[str, ...], schema: dict) -> bool:
    node = schema
    for key in path:
        properties = node.get("properties")
        if properties is None:  # 自由形态对象 / 数组：更深的键不再逐个声明
            return True
        if key not in properties:
            return False
        node = properties[key]
    return True


def registered(path: tuple[str, ...], other: dict[str, str]) -> bool:
    dotted = ".".join(path)
    return any(dotted == item or dotted.startswith(item + ".") for item in other)


def undeclared_reads(files: list[Path] | None = None) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    other = load_read_by_other_plugins()
    problems: set[str] = set()
    for path in files if files is not None else scanned_files():
        for key_path, line in reads_in(path.read_text(encoding="utf-8")):
            dotted = ".".join(key_path)
            if dotted in IGNORED_PATHS or declared(key_path, schema) or registered(key_path, other):
                continue
            problems.add(f"{path.relative_to(ROOT)}:{line}  {dotted}")
    return sorted(problems)


class ProfileReadsGuardTests(unittest.TestCase):
    def test_every_profile_key_the_runtime_reads_is_declared_or_registered(self) -> None:
        problems = undeclared_reads()
        self.assertEqual(problems, [],
                         "运行时代码读取了 Project Profile 里未声明的键。请在 plugins/agents-init/skills/agents-init/"
                         "project.schema.json 声明（首选），或在 scaffold.py 的 READ_BY_OTHER_PLUGINS 登记"
                         "“键路径 -> 读取它的插件”：\n" + "\n".join(problems))

    def test_scanner_resolves_the_common_spellings(self) -> None:
        source = (
            "def f(profile, self):\n"
            "    a = profile.get('work') or {}\n"
            "    a.get('mode'); a['case']['root']\n"
            "    profile['privacy']['forbidden_default_reads']\n"
            "    work = profile.get('work') if isinstance(profile.get('work'), dict) else {}\n"
            "    work.get('bogus')\n"
            "    self.profile.get('opinion').get('nudge')\n"
            "    project_profile(1).get('sop')\n"
            "    other.get('work')\n"
        )
        paths = {".".join(path) for path, _ in reads_in(source)}
        self.assertTrue({"work.mode", "work.case.root", "privacy.forbidden_default_reads", "work.bogus",
                         "opinion.nudge", "sop"} <= paths, paths)
        self.assertNotIn("other", {p.split(".")[0] for p in paths if p != "work"})

    def test_an_unregistered_new_read_fails_with_a_clear_message(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sample = ROOT / "plugins" / "reads_guard_sample_runtime.py"
            try:
                sample.write_text("def f(profile):\n    return profile.get('opinion', {}).get('brand_new')\n"
                                  "def g(profile):\n    return profile.get('opinion').get('brand_new')\n",
                                  encoding="utf-8")
                problems = undeclared_reads([sample])
            finally:
                sample.unlink(missing_ok=True)
        self.assertTrue(any(line.endswith("opinion.brand_new") for line in problems), problems)

    def test_registered_and_declared_reads_pass(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertTrue(declared(("work", "case", "root"), schema))
        self.assertTrue(declared(("capabilities", "plugin_roots", "x"), schema))  # 数组之下不再逐键声明
        self.assertFalse(declared(("opinion", "brand_new"), schema))
        self.assertTrue(registered(("opinion", "nudge", "x"), {"opinion.nudge": "agent-pack"}))


if __name__ == "__main__":
    unittest.main()
