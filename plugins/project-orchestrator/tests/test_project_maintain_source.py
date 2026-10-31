"""health 的运行时修复显示实际用了哪个版本来源，且不返回其他来源的缓存（#32）。HOME 是临时目录。"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "runtime"))
sys.path.insert(0, str(HERE.parents[2] / "agent-pack" / "runtime"))
import agenthelm  # noqa: E402
import project_maintain  # noqa: E402

A, B = "https://a.example/latest.json", "https://b.example/latest.json"
LATEST = {"schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21", "plugins": {"agent-pack": {"version": "2.0.0"}},
          "migrations": []}


class RuntimeStepSourceTests(unittest.TestCase):
    def test_the_update_runtime_preview_names_the_source_and_ignores_another_sources_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            with mock.patch.object(agenthelm, "_fetch", side_effect=lambda url, timeout=0: dict(LATEST)) as fetch:
                agenthelm.load_latest(agenthelm.Env(home=home, variables={}), A)  # cache belongs to A
                env = agenthelm.Env(home=home, variables={agenthelm.ENV_LATEST: B})  # an explicit source B
                provider = SimpleNamespace(host=agenthelm, offline=False)
                with mock.patch.object(agenthelm, "current_env", return_value=env):
                    step = project_maintain._plan_runtime(provider, env, "update-runtime")
            self.assertEqual(fetch.call_count, 2)
            self.assertTrue(any(f"云端版本源：{B}" in line and f"已忽略来自 {A} 的缓存" in line for line in step["changes"]), step["changes"])


if __name__ == "__main__":
    unittest.main()
