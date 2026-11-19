"""D2-8 的测试守卫：公开 catalog 的规则与模板文字里不得出现路由标记（路由引用的 skill 属于环境信息，不进公开内容）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "catalog"


class CatalogHasNoRoutesTests(unittest.TestCase):
    def test_no_route_marker_in_any_public_catalog_file(self) -> None:
        files = [path for path in CATALOG.rglob("*") if path.is_file()]
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=str(path.relative_to(CATALOG))):
                self.assertNotIn("[route ", path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main(verbosity=0, argv=sys.argv[:1])
