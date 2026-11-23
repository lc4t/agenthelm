"""Guard three (design section 7.4): every built-in declaration is self-consistent and a client added after batch D2 has a fixture.

Parameterised over ``runtime/drivers/*.json``. Adding a client is a declaration plus ``tests/fixtures/clients/<id>/``; the four
clients of batches A to D2 build their fixtures in code and are listed in ``BUILT_IN_CODE`` (this table only ever shrinks).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(PLUGIN / "runtime"))

import agent_pack_clients as registry  # noqa: E402

BUILT_IN_CODE = {"claude", "codex", "cursor", "claude-desktop"}  # fixtures are written by the tests of batches A-D2
FIXTURES = HERE / "fixtures" / "clients"
DRIVER_FILES = sorted(registry.DRIVERS_DIR.glob("*.json"))


class DeclarationSelfCheck(unittest.TestCase):
    def test_every_declaration_loads_under_the_strict_validator(self) -> None:
        for path in DRIVER_FILES:
            with self.subTest(driver=path.stem):
                registry.validate_builtin(json.loads(path.read_text(encoding="utf-8")), path.name)

    def test_a_new_client_has_a_fixture_directory(self) -> None:
        for path in DRIVER_FILES:
            if path.stem in BUILT_IN_CODE:
                continue
            with self.subTest(driver=path.stem):
                self.assertTrue((FIXTURES / path.stem).is_dir(), f"tests/fixtures/clients/{path.stem}/ is missing")

    def test_every_fixture_directory_belongs_to_a_declared_client(self) -> None:
        self.assertEqual(sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) if FIXTURES.is_dir() else [],
                         sorted(name for name in registry.all_ids() if name not in BUILT_IN_CODE))

    def test_the_exemption_table_lists_only_declared_clients(self) -> None:
        self.assertLessEqual(BUILT_IN_CODE, set(registry.all_ids()))

    def test_unsupported_features_are_names_of_the_support_matrix(self) -> None:
        for driver in registry.BUILTIN:
            for entry in driver.get("unsupported", []):
                with self.subTest(driver=driver["id"], feature=entry["feature"]):
                    self.assertIn(entry["feature"], registry.UNSUPPORTED_FEATURES)
                    self.assertTrue(entry["reason"].strip())

    def test_store_paths_and_formats_agree(self) -> None:
        for driver in registry.BUILTIN:
            for kind, stores in driver["stores"].items():
                for store in stores:
                    with self.subTest(driver=driver["id"], store=store["path"]):
                        if kind == "skills":
                            self.assertEqual(store["format"], "dir")
                        else:
                            suffix = store["path"].split("#", 1)[0].rsplit(".", 1)[-1]
                            self.assertEqual(store["format"], {"json": "json", "toml": "toml"}[suffix])

    def test_an_undetermined_store_is_never_a_write_target(self) -> None:
        for driver in registry.BUILTIN:
            for stores in driver["stores"].values():
                for store in stores:
                    if store.get("status") == "undetermined":
                        self.assertFalse(store.get("write_target"), f"{driver['id']}: {store['path']}")

    def test_declared_companions_that_are_shipped_exist_in_the_plugin(self) -> None:
        for driver in registry.BUILTIN:
            for entry in registry.companions(driver):  # only those the release ships
                with self.subTest(driver=driver["id"], companion=entry["path"]):
                    self.assertTrue((PLUGIN / entry["path"]).is_file())

    def test_a_trust_prompt_client_declares_the_steps_it_needs(self) -> None:
        for driver in registry.BUILTIN:
            if registry.interactive(driver) == "trust-prompt":
                with self.subTest(driver=driver["id"]):
                    self.assertEqual(registry.source_kind(driver), "bundle-dir")
                    self.assertIsNotNone(registry.expand_step(driver, "install"))
                    self.assertIsNotNone(registry.manifest_file(driver))


if __name__ == "__main__":
    unittest.main()
