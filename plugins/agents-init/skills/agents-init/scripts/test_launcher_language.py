"""The launcher and private/.gitignore follow the project file language; zh and historical sources stay byte-identical."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import baseline  # noqa: E402
import init_project  # noqa: E402

CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
ZH_LAUNCHER = init_project.LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
EN_LAUNCHER = init_project.LAUNCHER_TEMPLATE_EN.read_text(encoding="utf-8")


def inputs(language: str, launcher: str | None = "menu") -> init_project.Inputs:
    return init_project.Inputs(Path("/p"), "Demo", "demo", ("code",), "git", ("python",), "local", ("claude",),
                               entry_launcher=launcher, language=language)


class LauncherLanguageTests(unittest.TestCase):
    def test_zh_output_is_byte_identical_to_the_shipped_files(self) -> None:
        files = init_project.files_for(inputs("zh"))
        self.assertEqual(files[Path("menu")], ZH_LAUNCHER)
        self.assertEqual(files[Path("private/.gitignore")], init_project.PRIVATE_IGNORE)
        self.assertEqual(init_project.launcher_text(), ZH_LAUNCHER)

    def test_en_output_has_no_chinese(self) -> None:
        files = init_project.files_for(inputs("en"))
        self.assertEqual(files[Path("menu")], EN_LAUNCHER)
        self.assertEqual(files[Path("private/.gitignore")], init_project.PRIVATE_IGNORE_EN)
        for path, text in files.items():
            self.assertFalse(CJK.search(text), path.as_posix())

    def test_historical_source_without_an_english_launcher_renders_what_that_release_wrote(self) -> None:
        # Releases before launcher.en.sh wrote the Chinese launcher for language=en projects too; the baseline rendered from
        # such a source must reproduce it, or the content lock digest never matches and the three-way merge is lost.
        files = init_project.files_for(inputs("en"), None, "#!/bin/sh\n# old zh launcher\n", None)
        self.assertEqual(files[Path("menu")], "#!/bin/sh\n# old zh launcher\n")

    def test_source_with_both_launchers_uses_the_one_for_the_language(self) -> None:
        for language, expected in (("zh", "ZH\n"), ("en", "EN\n")):
            files = init_project.files_for(inputs(language), None, "ZH\n", "EN\n")
            self.assertEqual(files[Path("menu")], expected)

    def test_pack_source_reads_the_optional_english_launcher(self) -> None:
        template = init_project.TEMPLATE_PATH.read_bytes()
        names = {"AGENT.template.md": template, "launcher.sh": b"zh\n", "launcher.en.sh": b"en\n"}
        pack = {"id": "p", "version": "1.0.0", "sources": list(names), "files": {n: baseline.sha256(d) for n, d in names.items()}}
        found = baseline._verified({"packs": [pack]}, "p", lambda source: names.get(source), "x", "cache")
        self.assertEqual((found.launcher, found.launcher_en), ("zh\n", "en\n"))
        del names["launcher.en.sh"], pack["sources"][2], pack["files"]["launcher.en.sh"]
        found = baseline._verified({"packs": [pack]}, "p", lambda source: names.get(source), "x", "cache")
        self.assertEqual((found.launcher, found.launcher_en), ("zh\n", None))


if __name__ == "__main__":
    unittest.main()
