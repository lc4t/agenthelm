"""A 0-byte configuration file is an empty object for the write adapters too (#134 batch E).

A client may create its configuration empty (Antigravity's global ``mcp_config.json`` starts as a 0-byte file). Before this
change the read side already treated it as empty (batch B), but ``sync`` and ``relocate`` refused to write into a 0-byte JSON
file as damaged: ``sync --client claude`` stopped with ``invalid JSON <path>: Expecting value: line 1 column 1 (char 0)``.
Now the JSON adapter reads 0 bytes as ``{}`` and the write goes through. The TOML adapter already did (an empty TOML text is an
empty table); a test pins it. Anything that is not empty (whitespace only, a list, a symlink) is still refused as before.
"""
from __future__ import annotations

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

import agent_pack_config as core  # noqa: E402
import agent_pack_mcp_json as mcp_json  # noqa: E402
import agent_pack_native_package as native  # noqa: E402
from test_agent_pack_config import make_project  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = make_project(self.root / "proj", "proj")

    def sync(self, client: str, **kwargs):
        return core.sync_project(self.project, client, home=self.home, apply=True, recovery_root=self.root / "rec", **kwargs)


class JsonAdapterTests(Base):
    def test_zero_bytes_read_as_an_empty_object(self) -> None:
        path = self.root / "a.json"
        path.write_text("")
        self.assertEqual(mcp_json.read_config(path), {})

    def test_anything_else_is_read_or_refused_as_before(self) -> None:
        path = self.root / "a.json"
        path.write_text('{"mcpServers": {}}')
        self.assertEqual(mcp_json.read_config(path), {"mcpServers": {}})
        for text in ("  \n", "[]", "{"):
            path.write_text(text)
            with self.assertRaises(core.AgentPackError, msg=repr(text)):
                mcp_json.read_config(path)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(core.AgentPackError):
            mcp_json.read_config(link)


class ClaudeUserFileTests(Base):
    def test_sync_writes_into_a_zero_byte_user_config(self) -> None:
        config = self.home / ".claude.json"
        config.write_text("")
        self.assertTrue(self.sync("claude")["applied"])   # before: AgentPackError "invalid JSON ... Expecting value"
        written = json.loads(config.read_text())
        self.assertEqual(sorted(written["mcpServers"]), ["local-check"])

    def test_a_whitespace_only_file_is_still_damaged(self) -> None:
        (self.home / ".claude.json").write_text(" \n")
        with self.assertRaisesRegex(core.AgentPackError, "invalid JSON"):
            self.sync("claude")

    def test_the_project_mcp_file_read_side_of_the_writer(self) -> None:
        path = self.root / ".mcp.json"
        path.write_text("")
        self.assertEqual(native._read_project_file(path), ({}, None))   # before: ({}, "mcp-json-unreadable")
        path.write_text(" ")
        self.assertEqual(native._read_project_file(path), ({}, "mcp-json-unreadable"))


class TomlAdapterTests(Base):
    def test_sync_writes_into_a_zero_byte_codex_config(self) -> None:
        mcp = self.project / ".agents" / "mcp.json"
        data = json.loads(mcp.read_text())
        data["mcpServers"]["remote-x"] = {"type": "streamable-http", "url": "https://mcp.example.com/mcp"}
        mcp.write_text(json.dumps(data))
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text("")
        self.assertTrue(self.sync("codex")["applied"])   # unchanged by this batch: an empty TOML text is an empty table
        self.assertEqual(tomllib.loads(config.read_text())["mcp_servers"]["remote-x"], {"url": "https://mcp.example.com/mcp"})


if __name__ == "__main__":
    unittest.main()
