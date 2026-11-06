"""The session-start hook's local capability notice must honour --client, like the update notice (#51 with #50)."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from test_agent_pack_config import make_project, write_json
import agent_pack_config as core
import agenthelm


class HookCapabilityClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        os.environ["AGENTHELM_LANG"] = "zh"
        self.project = make_project(self.root / "project", "project")
        write_json(self.project / ".agents/mcp.json", {"mcpServers": {"example": {
            "type": "http", "url": "https://mcp.example.com/mcp", "bearer_token_env_var": "EXAMPLE_MCP_TOKEN"}}})
        self.latest = self.root / "latest.json"
        write_json(self.latest, {"schema": 1, "marketplace": "agenthelm", "repository": "https://github.com/example/agenthelm",
                                 "plugins": {}, "migrations": []})

    def hook(self, *args: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {"EXAMPLE_MCP_TOKEN": "x"}):
            code = agenthelm.main(["check", "--hook", "--project", str(self.project), "--home", str(self.home),
                                   "--latest", str(self.latest), *args])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_local_drift_notice_names_only_the_selected_clients(self) -> None:
        both = self.hook("--client", "codex", "--client", "cursor")
        self.assertIn("Codex", both)
        self.assertIn("Cursor", both)
        self.assertNotIn("Claude Code", both)

    def test_selecting_one_client_hides_drift_of_the_others(self) -> None:
        text = self.hook("--client", "codex")
        self.assertIn("Codex", text)
        self.assertNotIn("Cursor", text)
        self.assertNotIn("Claude Code", text)

    def test_in_sync_client_is_silent(self) -> None:
        core.sync_project(self.project, "codex", home=self.home, apply=True)
        self.assertEqual(self.hook("--client", "codex"), "")


if __name__ == "__main__":
    unittest.main()
