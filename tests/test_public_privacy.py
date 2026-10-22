from __future__ import annotations

import hashlib
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# 公开仓库里不应出现的标记：本机路径、同步盘路径与私钥。
FORBIDDEN_TEXT = ("/Users/", "CloudDocs/", "BEGIN PRIVATE KEY")
# 私有项目标识只保存 SHA-256，避免把标识本身写进公开仓库。
FORBIDDEN_TOKEN_DIGESTS = {
    "cf16a9a09ccde020f1f1539b54ebcf918bd7b9668cd178f8a2bec15823953654",
    "e0132c30db86c621e78f0d5495730752ac55d692d3d069499306a0fab82416aa",
}
# 本文件为了定义标记而包含它们。
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        return []
    return [line for line in result.stdout.splitlines() if line]


class PublicPrivacyTests(unittest.TestCase):
    def test_tracked_files_carry_no_private_markers(self) -> None:
        files = tracked_files()
        if not files:
            self.skipTest("not a git checkout")
        hits: dict[str, list[str]] = {}
        for relative in files:
            if relative == SELF:
                continue
            try:
                text = (ROOT / relative).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            found = [marker for marker in FORBIDDEN_TEXT if marker in text]
            tokens = {hashlib.sha256(token.encode("utf-8")).hexdigest() for token in re.findall(r"[A-Za-z0-9._-]+", text)}
            if tokens & FORBIDDEN_TOKEN_DIGESTS:
                found.append("private-project-identifier")
            if found:
                hits[relative] = found
        self.assertEqual(hits, {})

    def test_bundled_plugins_declare_the_public_repository(self) -> None:
        for manifest in sorted((ROOT / "plugins").glob("*/plugin.json")):
            text = manifest.read_text(encoding="utf-8")
            self.assertIn('"repository": "https://github.com/lc4t/agenthelm"', text, manifest.as_posix())


if __name__ == "__main__":
    unittest.main()
