"""The two static files agent-pack ships at its root for Antigravity: ``hooks.json`` and ``mcp_config.json`` (#134, D-K9).

They are committed data, not generated at install time, so these tests keep them from drifting from what the rest of the
plugin declares: the MCP server set of ``mcp.json``, the hook events of the ``agy`` driver, and the companion files the
driver lists. What a real ``agy`` does with them (does a hook fire, is ``${PLUGIN_ROOT}`` expanded) is not tested here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
sys.path.insert(0, str(PLUGIN / "runtime"))

import agent_pack_clients as registry  # noqa: E402

DRIVER = registry.get("agy")


def load(name: str) -> dict:
    return json.loads((PLUGIN / name).read_text(encoding="utf-8"))


class McpConfigTests(unittest.TestCase):
    def test_the_server_set_is_the_one_of_mcp_json(self) -> None:
        portable = load("mcp.json")["mcpServers"]
        client = load("mcp_config.json")["mcpServers"]
        self.assertEqual(sorted(client), sorted(portable))

    def test_an_entry_differs_from_mcp_json_by_the_type_field_only(self) -> None:
        portable = load("mcp.json")["mcpServers"]
        for name, entry in load("mcp_config.json")["mcpServers"].items():
            self.assertNotIn("type", entry, "agy warns about a type field and ignores it")
            self.assertEqual(entry, {key: value for key, value in portable[name].items() if key != "type"}, name)

    def test_it_is_the_agy_format_with_plugin_placeholders_kept(self) -> None:
        data = load("mcp_config.json")
        self.assertEqual(list(data), ["mcpServers"])   # no $schema: it is not an Agent Plugins file
        for entry in data["mcpServers"].values():
            self.assertTrue(any("${PLUGIN_ROOT}" in arg for arg in entry["args"]))

    def test_the_command_it_launches_exists(self) -> None:
        for entry in load("mcp_config.json")["mcpServers"].values():
            script = entry["args"][0].replace("${PLUGIN_ROOT}/", "")
            self.assertTrue((PLUGIN / script).is_file(), script)


class HooksTests(unittest.TestCase):
    def handlers(self) -> list[tuple[str, str, dict]]:
        found = []
        for group, events in load("hooks.json").items():
            for event, entries in events.items():
                found += [(group, event, entry) for entry in entries]
        return found

    def test_the_format_is_group_then_event_then_handlers(self) -> None:
        data = load("hooks.json")
        self.assertEqual(list(data), ["agenthelm"])
        self.assertNotIn("hooks", data)   # the Claude layout (a top-level hooks key) is read as a group name by agy
        for event, entries in data["agenthelm"].items():
            self.assertIsInstance(entries, list, event)
            for entry in entries:
                self.assertEqual(sorted(entry), ["command", "timeout", "type"])
                self.assertEqual(entry["type"], "command")
                self.assertIsInstance(entry["timeout"], int)

    def test_the_events_are_a_subset_of_what_the_driver_declares(self) -> None:
        declared = set(DRIVER["hooks"]["events"].values())
        used = {event for _group, event, _entry in self.handlers()}
        self.assertTrue(used and used <= declared, (used, declared))

    def test_each_command_names_its_event_the_client_and_a_file_that_exists(self) -> None:
        canonical = {native: name for name, native in DRIVER["hooks"]["events"].items()}
        for _group, event, entry in self.handlers():
            self.assertEqual(entry["command"],
                             f"python3 ./runtime/agenthelm.py nudge hook --event {canonical[event]} --client {DRIVER['id']}")
            self.assertTrue((PLUGIN / "runtime" / "agenthelm.py").is_file())

    def test_the_command_runs_from_the_plugin_directory_the_way_agy_runs_it(self) -> None:
        for _group, _event, entry in self.handlers():
            done = subprocess.run(entry["command"].split(), cwd=PLUGIN, input="{}", capture_output=True, text=True,
                                  env={**os.environ, "AGENTHELM_NO_NUDGE": "1"}, timeout=60, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_claude_layout_file_is_untouched_and_is_not_what_agy_reads(self) -> None:
        claude = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(claude), ["hooks"])
        self.assertIn("UserPromptSubmit", claude["hooks"])   # the events agy does not map stay with the clients that do


class CompanionTests(unittest.TestCase):
    def test_every_companion_the_driver_lists_ships_in_the_plugin_root(self) -> None:
        companions = DRIVER["content"]["companions"]
        self.assertEqual(sorted(item["path"] for item in companions), ["hooks.json", "mcp_config.json"])
        for item in companions:
            self.assertTrue((PLUGIN / item["path"]).is_file(), item["path"])

    def test_the_roles_match_the_files(self) -> None:
        roles = {item["path"]: item["role"] for item in DRIVER["content"]["companions"]}
        self.assertEqual(roles, {"hooks.json": "hooks", "mcp_config.json": "mcp"})

    def test_the_sync_package_projection_uses_the_same_file_name(self) -> None:
        self.assertEqual(DRIVER["sync"]["companions"]["mcp"], "mcp_config.json")


if __name__ == "__main__":
    unittest.main()
