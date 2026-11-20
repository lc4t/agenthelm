"""公开 catalog 1.1.0 的体积守卫（设计文档 4.6 节与第 8 节）：读取固定路径 plugins/opinion-manager/catalog，zh、en 各一遍，
对 scenario/reviewed-project@1.1.0 与 scenario/concise-project@1.1.0 用 ``context index`` 的真实输出断言。

实测（字节，Profile 编号为 private.budget@1.0.0）：
    zh reviewed  core 8,221 / 完整 21,238 / 常驻 7,496；索引行 148、126、141、138；各标签 bytes 之和 13,750
    en reviewed  core 10,051 / 完整 26,294 / 常驻 9,247；索引行 183、140、157、152；各标签 bytes 之和 17,026
    zh concise   core 7,194 / 完整 20,700；en concise core 8,845 / 完整 25,723
Profile 编号越长，core 的 Profile 行越长；断言留有余量，数值依据见设计文档第 8 节，不得为通过测试而改 catalog。
"""

from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "plugins/opinion-manager/runtime"
CATALOG = ROOT / "plugins/opinion-manager/catalog"
TEST_ROOT = ROOT / "__pycache__" / "opinion-test-work"
sys.path.insert(0, str(RUNTIME))

import opinion_manager  # noqa: E402
import versioned  # noqa: E402

CORE_LIMIT = {"zh": 9_000, "en": 11_000}
INDEX_LIMIT = {"zh": 160, "en": 200}
MAX_ON_DEMAND_TAGS = 6
RESIDENT_SHARE = 0.40
OVERLAP_LIMIT = 1_200  # 各标签 bytes 之和 + core_bytes - 完整正文：标签重叠与切片框架的合计开销


def run(*argv: object) -> dict:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = opinion_manager.main([str(item) for item in argv])
    if code != 0:
        raise AssertionError(buffer.getvalue())
    return json.loads(buffer.getvalue())


class LoadingBudgetTests(unittest.TestCase):
    def index(self, template: str, language: str) -> dict:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            project = Path(directory)
            config = project / ".agents/moe.sakanano.agent-pack/project.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"language": language}), encoding="utf-8")
            arguments = ["profile", "--project", project, "--id", "private.budget", "--version", "1.0.0", "--template", f"scenario/{template}@1.1.0", "--catalog", CATALOG]
            run(*arguments, "--apply", "--confirm", run(*arguments)["confirmation_sha256"])
            profile = project / ".opinion/profiles/private.budget/versions/1.0.0.json"
            compose = ["compose", "--project", project, "--profile", profile, "--output", "json"]
            run(*compose, "--apply", "--confirm", run(*compose)["confirmation_sha256"])
            self.assertEqual(versioned.verify_project(project)["status"], "verified")
            return run("context", "index", "--project", project, "--output", "json")

    def test_the_public_catalog_stays_within_the_loading_budget(self) -> None:
        for language in ("zh", "en"):
            for template in ("reviewed-project", "concise-project"):
                with self.subTest(language=language, template=template):
                    index = self.index(template, language)
                    self.assertLessEqual(index["core_bytes"], CORE_LIMIT[language], index["core_bytes"])
                    self.assertLessEqual(index["on_demand_tags"], MAX_ON_DEMAND_TAGS)
                    self.assertGreater(index["on_demand_tags"], 0)
                    ready = [tag for tag in index["tags"] if tag["state"] == "ready"]
                    self.assertEqual(len(ready), index["on_demand_tags"])
                    for tag in ready:
                        self.assertLessEqual(tag["index_bytes"], INDEX_LIMIT[language], (tag["id"], tag["index_bytes"]))
                    self.assertLessEqual(index["resident_bytes"] / index["complete_bytes"], RESIDENT_SHARE, index)
                    self.assertLessEqual(index["core_bytes"] / index["complete_bytes"], RESIDENT_SHARE, index)
                    overhead = sum(tag["bytes"] for tag in ready) + index["core_bytes"] - index["complete_bytes"]
                    self.assertLessEqual(overhead, OVERLAP_LIMIT, overhead)


if __name__ == "__main__":
    unittest.main()
