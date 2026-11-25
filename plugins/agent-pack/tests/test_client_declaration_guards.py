"""Guard three, the checks ``test_client_declarations_selfcheck`` does not make (#134, design 7.4).

Over every ``drivers/*.json``: the feature names of ``unsupported`` against the support matrix of the design document (section 6,
the first column), the companion files a driver ships, the hook file it names, the message keys it uses, and that a fixture tree
is detected as the client it belongs to.
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


def matrix_unsupported_by_client() -> dict[str, set[str]]:
    """Per client column of the support matrix: the feature names a cell marks 不支持 or 不适用. A cell that opens with the word
    names the backticked features in the cell, or (when it has none) the features of the row's first column; ``不支持（`name`…``
    inside a longer cell names that one feature."""
    text = DESIGN.read_text(encoding="utf-8")
    section = text[text.index("## 6. "):text.index("## 7. ")]
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in section.splitlines()
            if line.startswith("|") and not line.startswith("| ---")]
    header = rows[0]
    found: dict[str, set[str]] = {name: set() for name in header[1:]}
    for row in rows[1:]:
        row_names = set(re.findall(r"`([a-z][a-z0-9:-]*)`", row[0])) & set(registry.UNSUPPORTED_FEATURES)
        for client, cell in zip(header[1:], row[1:]):
            inline = set(re.findall(r"不支持（`([a-z][a-z0-9:-]*)`", cell))
            if cell.startswith(("不支持", "不适用")):
                inline |= set(re.findall(r"`([a-z][a-z0-9:-]*)`", cell)) & set(registry.UNSUPPORTED_FEATURES) or row_names
            found[client] |= inline
    return found


class DeclarationGuards(unittest.TestCase):
    def test_what_the_matrix_marks_unsupported_is_declared_by_the_driver(self) -> None:
        marked = matrix_unsupported_by_client()
        self.assertEqual(set(marked), {"agy", "gemini"})
        self.assertTrue({"nudge-install-hooks", "relocate"} <= marked["agy"] and "sync-mcp" in marked["gemini"], marked)
        for client, names in marked.items():
            declared = {entry["feature"] for entry in registry.get(client).get("unsupported", [])}
            with self.subTest(client=client):
                self.assertEqual(sorted(names - declared), [], f"{client}: the matrix says unsupported, the driver does not declare it")
                self.assertEqual(sorted(declared - names), [], f"{client}: the driver declares it unsupported, the matrix does not say so")

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

    def test_a_fixture_tree_is_detected_as_the_client_it_belongs_to(self) -> None:
        for folder in sorted(path for path in FIXTURES.iterdir() if path.is_dir()):
            home = folder / "home"
            driver = registry.get(folder.name)
            with self.subTest(client=folder.name):
                self.assertIsNotNone(driver, f"tests/fixtures/clients/{folder.name}/ belongs to no declared client")
                if home.is_dir() and driver["detect"].get("dirs"):
                    found = registry.detect(driver, home, {}.get, lambda name: None)
                    self.assertTrue(found["detected"], f"the fixture tree of {folder.name} is not detected as {folder.name}")

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
