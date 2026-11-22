"""Guard (#134, transitional): runtime code must not branch on a client's name.

Adding a client is adding a declaration (``runtime/drivers/<id>.json``) and a fixture; code reads the registry. This test
scans ``plugins/agent-pack/runtime/*.py`` (except the registry itself and the translation catalogue loader) with ``ast`` and
counts every string constant equal to a registered driver id, per file, against ``CAPS`` below.

``CAPS`` is a ratchet. A listed file's cap must equal its current count **exactly**: more fails (a new name branch), and fewer
fails too, so whoever removes a literal lowers the cap in the same change. A file that is not listed has a cap of 0. The target
(batch E) is an empty table. The one planned exemption is ``plugins/agents-init/skills/agents-init/launcher.sh``, its fourth
fallback level: a shell file, outside this Python scan, kept on purpose because it runs before any runtime can be located.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402

EXCLUDED = {"agent_pack_clients.py", "i18n_agent_pack.py"}

# file -> number of client-id string constants it still holds. Each is cleared by the batch named next to it.
CAPS = {
    "agent_pack_config.py": 31,     # sync write side, package destinations, Codex TOML writing, residual cleanup (batch D)
    "agent_pack_claude.py": 12,     # native package construction: the sync write path, not install / update (batch D)
    "agent_pack_scope.py": 4,       # scope declaration and shadow findings: the sync path (batch D)
    "agent_pack_mcp_write.py": 3,   # MCP write side: the sync path (batch D)
    "agent_pack_codex_mcp.py": 1,   # Codex native MCP entry: the sync path (batch D)
}
DISTRIBUTION_CLEARED = ("agenthelm.py",)  # batch C: install, update, legacy and the install-state readers read the declarations
READ_SIDE_CLEARED = ("agent_pack_inventory.py", "agent_pack_relocate.py", "agent_pack_dedupe.py", "agent_pack_desktop.py",
                     "agent_pack_ext_common.py")


def literals(path: Path, ids: set[str]) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted((node.lineno, node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ids)


class ClientNameGuard(unittest.TestCase):
    def counts(self) -> dict[str, list[tuple[int, str]]]:
        ids = set(registry.all_ids())
        return {path.name: literals(path, ids) for path in sorted(RUNTIME.glob("*.py")) if path.name not in EXCLUDED}

    def test_every_file_matches_its_cap_exactly(self) -> None:
        found = self.counts()
        problems = []
        for name, hits in found.items():
            cap = CAPS.get(name, 0)
            if len(hits) > cap:
                problems.append(f"{name}: {len(hits)} client-name literals, cap {cap} (a new name branch; "
                                f"read the driver registry instead): lines {[line for line, _ in hits]}")
            elif len(hits) < cap:
                problems.append(f"{name}: {len(hits)} literals, cap {cap}: lower the cap in CAPS to {len(hits)}")
        problems += [f"{name}: listed in CAPS but not a runtime module" for name in CAPS if name not in found]
        self.assertEqual(problems, [])

    def test_the_table_only_lists_files_that_still_have_literals(self) -> None:
        self.assertTrue(all(cap > 0 for cap in CAPS.values()), "a cleared file is removed from CAPS, not set to 0")
        self.assertFalse(set(CAPS) & EXCLUDED)

    def test_the_read_side_is_clear(self) -> None:
        found = self.counts()
        for name in READ_SIDE_CLEARED:
            self.assertIn(name, found)
            self.assertEqual(found[name], [], name)
            self.assertNotIn(name, CAPS)

    def test_install_update_and_legacy_are_clear(self) -> None:
        found = self.counts()
        for name in DISTRIBUTION_CLEARED:
            self.assertIn(name, found)
            self.assertEqual(found[name], [], name)
            self.assertNotIn(name, CAPS)

    def test_the_scan_sees_a_planted_literal(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "planted.py"
            path.write_text('x = 1\nif client == "codex":\n    pass\n"""not-an-id"""\n', encoding="utf-8")
            self.assertEqual(literals(path, set(registry.all_ids())), [(2, "codex")])


if __name__ == "__main__":
    unittest.main()
