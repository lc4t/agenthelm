from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "plugins/opinion-manager/runtime/opinion_manager.py"
TEST_ROOT = ROOT / "__pycache__" / "opinion-test-work"
FALLBACK = "完整读取项目根目录 OPINION.md"


class TieredContextTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        for identity in ("alpha", "beta", "gamma", "delta"):
            self.publish({"id": f"fixture.{identity}", "version": "1.0.0", "slot": f"fixture.{identity}",
                          "title": f"TITLE_{identity.upper()}", "text": f"RULE_{identity.upper()}",
                          "level": "required", "scopes": ["SCOPE_FIXTURE"]})
        self.publish(self.template("base", ["alpha", "beta"]))
        self.publish(self.template("scene", ["gamma", "beta"]))
        self.publish(self.template("extra", ["delta"]))
        self.templates = ("--template", "base/default@1.0.0", "--template", "scene/default@1.0.0",
                          "--template", "extra/default@1.0.0")
        self.loading = {
            "core": ["base"],
            "signals": {"business": ["scene", "none"], "artifact": ["html", "report"]},
            "bundles": [{"family": "scene", "trigger": "TRIGGER_SCENE", "exclude": "EXCLUDE_SCENE",
                         "routes": [{"business": ["scene"]}]}],
        }

    def run_cli(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *(str(item) for item in arguments)],
                              capture_output=True, text=True, check=False)

    def command(self, *arguments: object, code: int = 0) -> dict:
        result = self.run_cli(*arguments, "--output", "json")
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def file(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def template(self, family: str, rules: list[str]) -> dict:
        return {"family": family, "variant": "default", "version": "1.0.0", "name": f"NAME_{family.upper()}",
                "description": "DESCRIPTION_FIXTURE", "scopes": ["SCOPE_FIXTURE"],
                "rules": [f"fixture.{identity}@1.0.0" for identity in rules]}

    def publish(self, value: dict) -> None:
        args = ("publish", "--catalog", self.catalog, "--file", self.file("release.json", value))
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def profile(self, *extra: object, release: str = "1.0.0", code: int = 0) -> dict:
        args = ("profile", "--catalog", self.catalog, "--project", self.project,
                "--id", "private.fixture", "--version", release, *extra)
        preview = self.command(*args, code=code)
        if code:
            return preview
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def adopt(self, loading: dict | None = None, *, release: str = "1.0.0", extra: tuple = ()) -> dict:
        arguments = [*self.templates, *extra]
        if loading is not None:
            arguments += ["--loading-file", self.file("loading.json", loading)]
        saved = self.profile(*arguments, release=release)
        args = ("compose", "--project", self.project, "--profile", saved["path"])
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])
        return saved

    def context(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return self.run_cli("context", *arguments, "--project", self.project)

    def test_profile_without_loading_keeps_schema_and_requires_complete_reading(self) -> None:
        saved = self.adopt()
        self.assertEqual(saved["profile"]["schema_version"], "2.0")
        self.assertNotIn("loading", saved["profile"])
        result = self.context("core")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertEqual(json.loads(result.stdout)["fallback"], FALLBACK)

    def test_core_and_bundles_partition_the_complete_text(self) -> None:
        saved = self.adopt(self.loading)
        self.assertEqual(saved["profile"]["schema_version"], "2.1")
        complete = (self.project / "OPINION.md").read_text(encoding="utf-8")
        core = self.context("core").stdout
        bundle = self.context("bundle", "scene").stdout
        for text in ("RULE_ALPHA", "RULE_BETA", "RULE_DELTA", "`scene`（NAME_SCENE，1 条）：TRIGGER_SCENE。不适用：EXCLUDE_SCENE。"):
            self.assertIn(text, core)
        self.assertNotIn("RULE_GAMMA", core)
        self.assertIn("RULE_GAMMA", bundle)
        self.assertNotIn("RULE_BETA", bundle)
        index = json.loads(self.context("index").stdout)
        self.assertEqual(index["unlisted"], ["extra"])
        self.assertEqual(sum(item["rules"] for item in index["core"] + index["bundles"]), 4)
        for identity in ("ALPHA", "BETA", "GAMMA", "DELTA"):
            block = f"## TITLE_{identity}\n\n规则：`fixture.{identity.lower()}@1.0.0`；强制规则；适用：SCOPE_FIXTURE。\n\nRULE_{identity}"
            self.assertIn(block, complete)
            self.assertEqual((core + bundle).count(block), 1)
        self.assertEqual(self.context("bundle", "scene/default@1.0.0").returncode, 0)

    def test_always_loaded_families_cannot_be_requested_as_bundles(self) -> None:
        self.adopt(self.loading)
        for name in ("base", "extra", "missing"):
            result = self.context("bundle", name)
            self.assertEqual(result.returncode, 2, name)
            self.assertEqual(json.loads(result.stdout)["fallback"], FALLBACK)

    def test_check_compares_declared_signals_with_loaded_bundles(self) -> None:
        self.adopt(self.loading)
        missing = self.context("check", "--signal", "business=scene")
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(json.loads(missing.stdout)["missing"], ["scene"])
        self.assertEqual(self.context("check", "--signal", "business=scene", "--loaded", "scene").returncode, 0)
        unrelated = self.context("check", "--signal", "artifact=html")
        self.assertEqual(unrelated.returncode, 0)
        self.assertEqual(json.loads(unrelated.stdout)["required"], [])
        self.assertEqual(self.context("check", "--signal", "business=unknown").returncode, 2)
        self.assertEqual(self.context("check").returncode, 2)

    def test_edited_text_and_project_switch_fall_back_to_complete_reading(self) -> None:
        self.adopt(self.loading)
        switch = self.project / ".agents" / "moe.sakanano.agent-pack" / "project.json"
        switch.parent.mkdir(parents=True)
        switch.write_text(json.dumps({"opinion": {"loading_mode": "full"}}), encoding="utf-8")
        result = self.context("core")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["fallback"], FALLBACK)
        switch.write_text(json.dumps({"opinion": {"loading_mode": "tiered"}}), encoding="utf-8")
        self.assertEqual(self.context("core").returncode, 0)
        switch.write_text(json.dumps({"opinion": {"loading_mode": "sometimes"}}), encoding="utf-8")
        self.assertEqual(self.context("core").returncode, 2)
        switch.unlink()
        opinion = self.project / "OPINION.md"
        opinion.write_text(opinion.read_text(encoding="utf-8") + "MANUAL_EDIT\n", encoding="utf-8")
        for action in (("core",), ("bundle", "scene"), ("index",), ("check", "--signal", "business=scene")):
            result = self.context(*action)
            self.assertEqual(result.returncode, 2, action)
            self.assertEqual(json.loads(result.stdout)["fallback"], FALLBACK)

    def test_candidate_profile_can_be_previewed_before_it_is_saved(self) -> None:
        args = ("profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture",
                "--version", "1.0.0", *self.templates, "--loading-file", self.file("loading.json", self.loading))
        candidate = self.file("candidate.json", self.command(*args)["profile"])
        result = self.run_cli("context", "core", "--profile", candidate)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("候选 Profile 预览，尚未生效", result.stdout)
        self.assertFalse((self.project / "OPINION.md").exists())

    def test_invalid_loading_declarations_are_rejected(self) -> None:
        broken = [
            {**self.loading, "core": ["missing"]},
            {**self.loading, "core": ["base", "scene"]},
            {**self.loading, "bundles": []},
            {**self.loading, "signals": {}},
            {**self.loading, "bundles": [{**self.loading["bundles"][0], "routes": [{"business": ["typo"]}]}]},
            {**self.loading, "bundles": [{**self.loading["bundles"][0], "routes": []}]},
            {**self.loading, "bundles": [{**self.loading["bundles"][0], "family": "missing"}]},
            {**self.loading, "extra": True},
        ]
        for value in broken:
            self.profile(*self.templates, "--loading-file", self.file("loading.json", value), code=2)

    def test_revision_inherits_replaces_and_removes_loading(self) -> None:
        first = self.adopt(self.loading)
        second = self.profile("--from-profile", first["path"], release="1.1.0")
        self.assertEqual(second["profile"]["loading"], self.loading)
        self.assertFalse(second["changes"]["loading_changed"])
        self.profile("--from-profile", first["path"], "--remove-template", "scene", release="1.2.0", code=2)
        changed = {**self.loading, "core": ["base", "extra"]}
        third = self.profile("--from-profile", first["path"], "--loading-file", self.file("loading.json", changed), release="1.3.0")
        self.assertTrue(third["changes"]["loading_changed"])
        self.assertNotEqual(third["confirmation_sha256"], second["confirmation_sha256"])
        fourth = self.profile("--from-profile", first["path"], "--remove-loading", release="1.4.0")
        self.assertEqual(fourth["profile"]["schema_version"], "2.0")
        self.assertNotIn("loading", fourth["profile"])
        self.profile("--from-profile", first["path"], "--remove-loading", "--loading-file",
                     self.file("loading.json", self.loading), release="1.5.0", code=2)

    def test_disabled_rule_leaves_the_bundle_empty_and_unavailable(self) -> None:
        self.adopt(self.loading, extra=("--overrides-file", self.file("overrides.json", {"fixture.gamma": None})))
        self.assertNotIn("RULE_GAMMA", self.context("core").stdout)
        self.assertNotIn("`scene`", self.context("core").stdout)
        self.assertEqual(self.context("bundle", "scene").returncode, 2)


if __name__ == "__main__":
    unittest.main()
