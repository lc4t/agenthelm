"""项目入口的插件检查只看没有被忽略的客户端（与 agenthelm 的 active_clients 一致），并兼容没有它的旧版 host。"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import project_maintain  # noqa: E402

REPORT = {"plugins": [], "summary": {"legacy_installs": 0}}


class NewHost:
    def __init__(self, detected, active):
        self._detected, self._active = detected, active

    def detect_clients(self, env):
        return list(self._detected)

    def active_clients(self, env):
        return list(self._active)

    def doctor(self, env, report):
        return {"issues": []}


class OldHost:
    def detect_clients(self, env):
        return ["claude", "codex"]

    def doctor(self, env, report):
        return {"issues": []}


def findings_for(host) -> list[dict]:
    findings: list[dict] = []
    project_maintain._plugin_findings(SimpleNamespace(host=host), object(), REPORT, findings, [])
    return findings


class IgnoredClients(unittest.TestCase):
    def test_ignored_clients_are_not_named_in_the_no_plugins_finding(self) -> None:
        (item,) = findings_for(NewHost(["claude", "codex", "cursor"], ["claude"]))
        self.assertEqual(item["id"], "no-plugins")
        self.assertIn("Claude Code", item["reason"])
        self.assertNotIn("Codex", item["reason"])
        self.assertNotIn("Cursor", item["reason"])

    def test_all_detected_clients_ignored_raises_no_finding_and_not_a_false_no_client_one(self) -> None:
        self.assertEqual(findings_for(NewHost(["claude"], [])), [])

    def test_no_client_at_all_is_still_reported(self) -> None:
        (item,) = findings_for(NewHost([], []))
        self.assertEqual(item["id"], "no-ai-client")

    def test_an_older_host_without_active_clients_falls_back_to_detect_clients(self) -> None:
        (item,) = findings_for(OldHost())
        self.assertEqual(item["id"], "no-plugins")
        self.assertIn("Codex", item["reason"])


if __name__ == "__main__":
    unittest.main()
