"""Regression coverage for the small rc.3 acceptance findings (#50), with a temporary HOME."""
from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agenthelm_check_output import Fixture
from test_agenthelm_reporting import write
import agenthelm


class AcceptancePolishTests(Fixture):
    lang = "zh"

    def hook_output(self, *args: str) -> tuple[int, str]:
        """Run the session-start hook in-process, recording network attempts in ``self.network_calls``."""
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(agenthelm.urllib.request, "urlopen",
                                                      side_effect=OSError("network is unavailable")) as opener:
            code = agenthelm.main(["check", "--hook", "--no-project", "--home", str(self.home), *args])
        self.network_calls = opener.call_count
        return code, out.getvalue()

    def test_hook_filters_by_client(self):
        self.three_clients_behind()
        code, only_codex = self.hook_output("--latest", str(self.latest_file), "--client", "codex")
        self.assertEqual(code, 0)
        self.assertIn("Codex", only_codex)
        self.assertNotIn("Claude Code", only_codex)
        self.assertNotIn("Cursor", only_codex)
        shutil.rmtree(self.home / ".cache", ignore_errors=True)
        shutil.rmtree(self.home / ".local/state", ignore_errors=True)
        _code, only_claude = self.hook_output("--latest", str(self.latest_file), "--client", "claude")
        self.assertIn("Claude Code", only_claude)
        self.assertNotIn("Codex", only_claude)

    def test_hook_uses_the_explicit_latest_source(self):
        self.claude_plugin("1.0.0")
        current = self.home / "current-latest.json"
        write(current, {**json.loads(self.latest_file.read_text()), "migrations": [],
                        "plugins": {"agent-pack": {"version": "1.0.0"}}})
        code, text = self.hook_output("--latest", str(current))
        self.assertEqual((code, text), (0, ""))  # up to date under the explicit source: silent
        code, text = self.hook_output("--latest", str(self.latest_file))
        self.assertEqual(code, 0)
        self.assertIn("Claude Code", text)  # behind under the other explicit source
        self.assertEqual(self.network_calls, 0)  # an explicit source is read locally, never fetched

    def test_hook_offline_never_touches_the_network(self):
        self.claude_plugin("1.0.0")
        code, text = self.hook_output("--offline")  # no cache and no source: nothing to compare, and no fetch
        self.assertEqual((code, text), (0, ""))
        self.assertEqual(self.network_calls, 0)
        _code, _text = self.hook_output()  # control: without --offline the hook does try the network
        self.assertGreater(self.network_calls, 0)

    def test_unknown_target_pin_is_not_described_as_known_old_ref(self):
        write(self.home / ".claude/settings.json", {"extraKnownMarketplaces": {"agenthelm": {
            "source": {"source": "github", "repo": "example/agenthelm", "ref": "v2.0.0"}}}})
        self.claude_plugin("1.0.0")
        code, payload = self.run_json("check", "--no-project", "--client", "claude")
        diag = payload["install_state"]["clients"]["claude"]
        self.assertEqual(code, 0)
        self.assertFalse(diag["market_pin"]["at_target"])
        self.assertIsNone(diag["market_pin"]["target"])
        self.assertIn("目标版本未知" if self.lang == "zh" else "Target version unknown", diag["summary"])

    def test_claude_apply_summary_names_where_old_directories_remain(self):
        text = "\n".join(agenthelm.render_outcome([{"client": "claude", "status": "applied"}]))
        self.assertIn("~/.claude/plugins/cache/agenthelm/", text)
        self.assertIn("agenthelm 不清理" if self.lang == "zh" else "agenthelm does not clean", text)
        skipped = "\n".join(agenthelm.render_outcome([{"client": "claude", "status": "blocked"}]))
        self.assertNotIn("~/.claude/plugins/cache/agenthelm/", skipped)

    def test_sync_folder_aggregates_machine_local_directories_with_details_escape(self):
        def finding(identifier, title):
            return {"id": identifier, "title": title, "reason": "reason", "impact": "impact", "next": "next"}
        report = {"machine": {"findings": [finding("local:sync-folder", "Dropbox"),
                   finding("local:real:node_modules", "node_modules"), finding("local:real:.venv", ".venv")]}}
        shown = agenthelm.render_machine(report)
        details = agenthelm.render_machine(report, details=True)
        self.assertEqual(len(shown), 1)
        self.assertEqual(len(details), 3)
        self.assertIn("check --details", shown[0])
        self.assertIn("node_modules", shown[0])
        self.assertIn(".venv", shown[0])
        self.assertIn("ID local:real:.venv", shown[0])
        self.assertEqual(len(report["machine"]["findings"]), 3)

    def test_chinese_migration_notes_are_explicitly_labelled_in_english(self):
        note = agenthelm.migration_language_note([{"summary": "历史说明"}])
        if self.lang == "zh":
            self.assertIsNone(note)
        else:
            self.assertIn("only in Chinese", note)
        self.assertIsNone(agenthelm.migration_language_note([{"summary": "Already in English"}]))

    def test_runtime_status_does_not_claim_a_higher_label_contains_newer_content(self):
        import agenthelm_runtime
        data = {"current": {"version": "6.11.0-rc.3", "path": "/example/current"},
                "installed": [{"version": "6.11.0"}], "running_from": "checkout", "inactive_newer": ["6.11.0"],
                "update_available": False}
        out = io.StringIO()
        with mock.patch.object(agenthelm_runtime, "report", return_value=data), redirect_stdout(out):
            self.assertEqual(agenthelm_runtime.main(["status", "--home", str(self.home), "--latest", str(self.latest_file)], agenthelm), 0)
        self.assertIn("不代表内容更新" if self.lang == "zh" else "its contents may be older", out.getvalue())
        self.assertIn("runtime use 6.11.0 --apply", out.getvalue())


class AcceptancePolishEnglishTests(AcceptancePolishTests):
    lang = "en"


if __name__ == "__main__":
    unittest.main()
