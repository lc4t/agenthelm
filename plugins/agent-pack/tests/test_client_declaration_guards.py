"""Guard three (#134, design 7.4): every driver declaration is self-consistent, checked for each file in ``drivers/``.

Vocabulary, store paths against their formats, the feature names of ``unsupported`` against the support matrix of the design
(section 6), a fixture directory per driver, and the companion files a driver lists against what the plugin ships. The checks run
over whatever ``drivers/*.json`` holds, so a new driver is covered by adding its file and its fixture directory.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
sys.path.insert(0, str(PLUGIN / "runtime"))

import agent_pack_clients as registry  # noqa: E402

DESIGN = TESTS.parents[2] / "docs" / "design" / "client-protocols.md"
FIXTURES = TESTS / "fixtures" / "clients"
FILES = sorted((PLUGIN / "runtime" / "drivers").glob("*.json"))
EXTENSIONS = {"json": ".json", "toml": ".toml"}


def matrix_feature_names() -> set[str]:
    """The backticked names in the first column of the support matrix (design section 6)."""
    text = DESIGN.read_text(encoding="utf-8")
    section = text[text.index("## 6. "):text.index("## 7. ")]
    names: set[str] = set()
    for line in section.splitlines():
        if line.startswith("|") and not line.startswith("| ---") and not line.startswith("| 功能"):
            names.update(re.findall(r"`([a-z][a-z0-9:-]*)`", line.split("|")[1]))
    return names


class DeclarationGuards(unittest.TestCase):
    def test_there_are_drivers_and_every_file_is_valid_and_named_for_its_id(self) -> None:
        self.assertTrue(FILES)
        for path in FILES:
            with self.subTest(driver=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                registry.validate_builtin(data, path.name)   # vocabulary, structure, cross-field rules
                self.assertEqual(data["id"], path.stem)

    def test_store_paths_match_their_formats(self) -> None:
        for path in FILES:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(driver=data["id"]):
                for store in data["stores"]["skills"]:
                    self.assertEqual(store["format"], "dir", store["path"])
                for store in data["stores"]["mcp"]:
                    file = store["path"].split("#", 1)[0]
                    if store["format"] in EXTENSIONS:
                        self.assertTrue(file.endswith(EXTENSIONS[store["format"]]), store["path"])
                    self.assertTrue(file.startswith(("~/", "<project>/", "<per-platform>/")), store["path"])
                for kind in ("skills", "mcp"):
                    for store in data["stores"][kind]:
                        self.assertFalse(store.get("write_target") and store.get("status") == "undetermined",
                                         f"an undetermined store is never a write target: {store['path']}")

    def test_unsupported_features_belong_to_the_support_matrix(self) -> None:
        names = matrix_feature_names()
        self.assertTrue({"relocate", "dedupe", "hook-event:prompt-submit", "hook-event:stop"} <= names, names)
        self.assertLessEqual(set(registry.UNSUPPORTED_FEATURES), names)
        for path in FILES:
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data.get("unsupported", []):
                with self.subTest(driver=data["id"], feature=entry["feature"]):
                    self.assertIn(entry["feature"], names)
                    self.assertTrue(entry["reason"].strip())

    def test_every_driver_has_a_fixture_directory_and_its_tree_is_detected(self) -> None:
        for path in FILES:
            client = path.stem
            with self.subTest(driver=client):
                folder = FIXTURES / client
                self.assertTrue(folder.is_dir() and any(folder.rglob("*")), f"tests/fixtures/clients/{client}/ is missing or empty")
                home = folder / "home"
                if home.is_dir() and registry.get(client)["detect"].get("dirs"):
                    found = registry.detect(registry.get(client), home, {}.get, lambda name: None)
                    self.assertTrue(found["detected"], f"the fixture tree of {client} is not detected as {client}")

    def test_companions_a_driver_lists_ship_in_the_plugin(self) -> None:
        for path in FILES:
            data = json.loads(path.read_text(encoding="utf-8"))
            for companion in (data.get("content") or {}).get("companions", []):
                if not companion.get("shipped", True):   # declared but not shipped by this release (``shipped: false``)
                    continue
                with self.subTest(driver=data["id"], companion=companion["path"]):
                    self.assertTrue((PLUGIN / companion["path"]).is_file())

    def test_a_declared_hook_file_is_one_the_plugin_ships(self) -> None:
        for path in FILES:
            data = json.loads(path.read_text(encoding="utf-8"))
            plugin_file = ((data.get("hooks") or {}).get("config") or {}).get("plugin_file")
            if plugin_file:
                with self.subTest(driver=data["id"]):
                    self.assertTrue((PLUGIN / plugin_file).is_file(), plugin_file)

    def test_the_messages_a_driver_names_have_texts(self) -> None:
        import agenthelm
        tables = {"recovery": agenthelm.RECOVERY_HINTS, "applied_recovery": agenthelm.RECOVERY_HINTS,
                  "unchanged": agenthelm.UNCHANGED_REASONS, "unchanged_next": agenthelm.UNCHANGED_ADVICE,
                  "describe_update_source": agenthelm.STEP_TEXTS, "describe_update": agenthelm.STEP_TEXTS,
                  "describe_dropin": agenthelm.STEP_TEXTS, "unavailable_next": agenthelm.STEP_TEXTS}
        for driver in registry.BUILTIN:
            for slot, key in registry.distribution(driver).get("messages", {}).items():
                self.assertIn(key, tables[slot], (driver["id"], slot))

    def test_the_planted_faults_are_caught(self) -> None:
        good = json.loads((PLUGIN / "runtime" / "drivers" / "agy.json").read_text(encoding="utf-8"))
        broken = json.loads(json.dumps(good))
        broken["unsupported"][0]["feature"] = "not-in-the-matrix"
        self.assertNotIn(broken["unsupported"][0]["feature"], matrix_feature_names())
        broken = json.loads(json.dumps(good))
        broken["stores"]["mcp"][0]["format"] = "toml"
        self.assertFalse(broken["stores"]["mcp"][0]["path"].endswith(".toml"))
        broken = json.loads(json.dumps(good))
        broken["capabilities"]["unheard_of"] = True
        with self.assertRaises(registry.DriverDataError):
            registry.validate_builtin(broken, "agy.json")


if __name__ == "__main__":
    unittest.main()
