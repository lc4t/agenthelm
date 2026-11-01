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
sys.path.insert(0, str(SCRIPT.parent))
import versioned  # noqa: E402


class VersionedOpinionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()

    def command(self, *arguments: object, success: bool = True) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *(str(item) for item in arguments), "--output", "json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def file(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def rule(self, release: str = "1.0.0", *, identity: str = "fixture.alpha", slot: str = "fixture.slot", text: str = "RULE_ALPHA") -> dict:
        return {"id": identity, "version": release, "slot": slot, "title": "TITLE_ALPHA", "text": text, "level": "required", "scopes": ["SCOPE_ALPHA"]}

    def template(self, release: str = "1.0.0", *, variant: str = "alpha", rules: list[str] | None = None, family: str = "fixture") -> dict:
        return {"family": family, "variant": variant, "version": release, "name": "TEMPLATE_ALPHA", "description": "DESCRIPTION_ALPHA", "scopes": ["SCOPE_ALPHA"], "rules": rules or ["fixture.alpha@1.0.0"]}

    def publish(self, value: dict) -> dict:
        path = self.file("release.json", value)
        args = ("publish", "--catalog", self.catalog, "--file", path)
        preview = self.command(*args)
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def seed(self) -> None:
        self.publish(self.rule())
        self.publish(self.template())

    def profile(self, *extra: object, identity: str = "private.fixture", release: str = "1.0.0") -> dict:
        args = ("profile", "--catalog", self.catalog, "--project", self.project, "--id", identity, "--version", release, *extra)
        preview = self.command(*args)
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def compose(self, profile: dict, *extra: object) -> dict:
        args = ("compose", "--project", self.project, "--profile", profile["path"], *extra)
        preview = self.command(*args)
        return self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"])

    def test_public_catalog_dependencies_compose_complete_previews(self) -> None:
        payload = self.command("catalog", "--catalog", versioned.PUBLIC_CATALOG)
        self.assertTrue(payload["templates"])
        self.assertTrue(payload["rules"])
        by_ref = {f"{rule['id']}@{rule['version']}": rule for rule in payload["rules"]}
        for template in payload["templates"]:
            ref = f"{template['family']}/{template['variant']}@{template['version']}"
            with self.subTest(template=ref):
                preview = self.command("profile", "--catalog", versioned.PUBLIC_CATALOG,
                    "--project", self.project, "--id", "fixture.public-preview",
                    "--version", "1.0.0", "--template", ref)
                self.assertFalse(preview["unresolved"])
                selected = {row["ref"] for row in preview["profile"]["sources"]["rules"]}
                self.assertEqual(selected, set(template["rules"]))
                for rule_ref in selected:
                    self.assertIn(by_ref[rule_ref]["text"], preview["content"])
                self.assertFalse((self.project / ".opinion").exists())

    def test_missing_exact_version_fails_without_writes(self) -> None:
        self.seed()
        payload = self.command("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha", success=False)
        self.assertIn("精确指定", payload["error"])
        self.assertFalse((self.project / ".opinion").exists())

    def test_parallel_variants_and_multiple_versions_listed(self) -> None:
        self.seed()
        self.publish(self.template("1.1.0"))
        self.publish(self.template(variant="beta"))
        payload = self.command("catalog", "--catalog", self.catalog)
        self.assertEqual(len(payload["templates"]), 3)
        profile = self.profile("--template", "fixture/beta@1.0.0")
        self.assertEqual(profile["profile"]["templates"], ["fixture/beta@1.0.0"])

    def test_mutually_exclusive_variants(self) -> None:
        self.seed()
        self.publish(self.template(variant="beta"))
        payload = self.command("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha@1.0.0", "--template", "fixture/beta@1.0.0", success=False)
        self.assertIn("重复身份", payload["error"])

    def test_immutable_rule_release(self) -> None:
        original = self.publish(self.rule())
        path = self.file("changed.json", self.rule(text="RULE_CHANGED"))
        error = self.command("publish", "--catalog", self.catalog, "--file", path, success=False)
        self.assertIn("不可修改", error["error"])
        self.assertEqual(json.loads(Path(original["path"]).read_text())["text"], "RULE_ALPHA")

    def test_patch_does_not_change_behavior(self) -> None:
        self.publish(self.rule())
        value = self.rule("1.0.1")
        value["title"] = "TITLE_PATCH"
        self.publish(value)
        path = self.file("bad-patch.json", self.rule("1.0.2", text="RULE_CHANGED"))
        self.command("publish", "--catalog", self.catalog, "--file", path, success=False)

    def test_template_patch_checks_effective_rules(self) -> None:
        self.seed()
        self.publish(self.rule("1.1.0", text="RULE_CHANGED"))
        path = self.file("bad-template-patch.json", self.template("1.0.1", rules=["fixture.alpha@1.1.0"]))
        self.command("publish", "--catalog", self.catalog, "--file", path, success=False)

    def test_missing_dependency_prevents_publish(self) -> None:
        path = self.file("template.json", self.template())
        payload = self.command("publish", "--catalog", self.catalog, "--file", path, success=False)
        self.assertIn("未知规则", payload["error"])
        self.assertEqual(list(self.catalog.rglob("*.json")), [])

    def test_rule_versions_conflict(self) -> None:
        self.seed()
        self.publish(self.rule("1.1.0"))
        error = self.command("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha@1.0.0", "--rule", "fixture.alpha@1.1.0", success=False)
        self.assertIn("不同版本", error["error"])

    def test_slot_conflict_across_families(self) -> None:
        self.seed()
        self.publish(self.rule(identity="fixture.beta"))
        self.publish(self.template(family="fixture-other", rules=["fixture.beta@1.0.0"]))
        error = self.command("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha@1.0.0", "--template", "fixture-other/alpha@1.0.0", success=False)
        self.assertIn("slot 冲突", error["error"])

    def test_shared_rule_is_deduplicated(self) -> None:
        self.seed()
        self.publish(self.template(family="fixture-other"))
        profile = self.profile("--template", "fixture/alpha@1.0.0", "--template", "fixture-other/alpha@1.0.0")
        self.assertEqual(len(profile["profile"]["sources"]["rules"]), 1)

    def test_preview_never_writes_and_confirmation_is_exact(self) -> None:
        self.seed()
        args = ("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha@1.0.0")
        self.command(*args)
        self.assertFalse((self.project / ".opinion").exists())
        self.command(*args, "--apply", "--confirm", "incorrect", success=False)
        self.assertFalse((self.project / ".opinion").exists())

    def test_complete_deterministic_opinion_and_lock(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        result = self.compose(profile)
        self.assertIn("RULE_ALPHA", result["content"])
        self.assertEqual(self.compose(profile)["status"], "unchanged")
        self.assertEqual(self.command("verify", "--project", self.project)["status"], "verified")
        preview1 = self.command("compose", "--project", self.project, "--profile", profile["path"])
        preview2 = self.command("compose", "--project", self.project, "--profile", profile["path"])
        self.assertEqual(preview1, preview2)

    def test_old_version_reconstructs_without_source_catalog(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        before = self.compose(profile)["content"]
        self.catalog.rename(self.root / "offline-catalog")
        self.assertEqual(self.compose(profile)["content"], before)
        self.command("verify", "--project", self.project)

    def test_updates_are_numeric_and_read_only(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        self.compose(profile)
        before = (self.project / "OPINION.md").read_bytes()
        self.publish(self.template("1.10.0"))
        self.publish(self.template("1.2.0"))
        result = self.command("updates", "--catalog", self.catalog, "--profile", profile["path"])
        self.assertEqual(result["updates"][0]["available"], ["fixture/alpha@1.2.0", "fixture/alpha@1.10.0"])
        self.assertFalse(result["applied"])
        self.assertEqual((self.project / "OPINION.md").read_bytes(), before)

    def test_upgrade_replaces_same_family_and_preserves_custom(self) -> None:
        self.seed()
        custom = self.root / "custom.md"
        custom.write_text("CUSTOM_ALPHA", encoding="utf-8")
        old = self.profile("--template", "fixture/alpha@1.0.0", "--custom-file", custom)
        self.publish(self.template("1.1.0"))
        new = self.profile("--from-profile", old["path"], "--template", "fixture/alpha@1.1.0", release="1.1.0")
        self.assertEqual(new["profile"]["templates"], ["fixture/alpha@1.1.0"])
        self.assertEqual(new["profile"]["custom"], "CUSTOM_ALPHA")
        self.assertEqual(json.loads(Path(old["path"]).read_text())["version"], "1.0.0")

    def test_private_fork_and_upstream_conflict(self) -> None:
        self.seed()
        old = self.profile("--template", "fixture/alpha@1.0.0")
        local_edit = {key: self.rule(text="LOCAL_ALPHA")[key] for key in versioned.EDITABLE}
        edits = self.file("overrides.json", {"fixture.alpha": local_edit})
        fork = self.profile("--from-profile", old["path"], "--overrides-file", edits, identity="private.fork")
        self.assertEqual(fork["profile"]["derived_from"]["id"], "private.fixture")
        self.publish(self.rule("1.1.0", text="UPSTREAM_ALPHA"))
        self.publish(self.template("1.1.0", rules=["fixture.alpha@1.1.0"]))
        args = ("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fork", "--version", "1.1.0", "--from-profile", fork["path"], "--template", "fixture/alpha@1.1.0")
        preview = self.command(*args)
        self.assertEqual(preview["unresolved"], ["fixture.alpha"])
        self.assertEqual(preview["changes"]["conflicts"][0]["local"]["text"], "LOCAL_ALPHA")
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], success=False)
        resolutions = self.file("resolution.json", {"fixture.alpha": "keep-local"})
        new = self.profile("--from-profile", fork["path"], "--template", "fixture/alpha@1.1.0", "--resolutions-file", resolutions, identity="private.fork", release="1.1.0")
        self.assertIn("LOCAL_ALPHA", self.compose(new)["content"])
        self.assertNotIn("UPSTREAM_ALPHA", self.compose(new)["content"])

    def test_choose_upstream_resolution(self) -> None:
        self.seed()
        edits = self.file("overrides.json", {"fixture.alpha": {key: self.rule(text="LOCAL_ALPHA")[key] for key in versioned.EDITABLE}})
        old = self.profile("--template", "fixture/alpha@1.0.0", "--overrides-file", edits)
        self.publish(self.rule("1.1.0", text="UPSTREAM_ALPHA"))
        self.publish(self.template("1.1.0", rules=["fixture.alpha@1.1.0"]))
        resolution = self.file("resolution.json", {"fixture.alpha": "use-upstream"})
        new = self.profile("--from-profile", old["path"], "--template", "fixture/alpha@1.1.0", "--resolutions-file", resolution, release="1.1.0")
        self.assertEqual(new["profile"]["overrides"], {})
        self.assertIn("UPSTREAM_ALPHA", self.compose(new)["content"])

    def test_removed_upstream_rule_preserves_private_override(self) -> None:
        self.seed()
        edits = self.file("overrides.json", {"fixture.alpha": {key: self.rule(text="LOCAL_ALPHA")[key] for key in versioned.EDITABLE}})
        old = self.profile("--template", "fixture/alpha@1.0.0", "--overrides-file", edits)
        self.publish(self.rule(identity="fixture.beta", slot="fixture.beta-slot"))
        self.publish(self.template("2.0.0", rules=["fixture.beta@1.0.0"]))
        resolution = self.file("resolution.json", {"fixture.alpha": "keep-local"})
        new = self.profile("--from-profile", old["path"], "--template", "fixture/alpha@2.0.0", "--resolutions-file", resolution, release="2.0.0")
        self.assertIn("LOCAL_ALPHA", self.compose(new)["content"])

    def test_manual_edit_blocks_overwrite_and_can_be_imported(self) -> None:
        self.seed()
        old = self.profile("--template", "fixture/alpha@1.0.0")
        self.compose(old)
        opinion = self.project / "OPINION.md"
        edited = opinion.read_text() + "\nMANUAL_ALPHA\n"
        opinion.write_text(edited, encoding="utf-8")
        self.command("verify", "--project", self.project, success=False)
        args = ("compose", "--project", self.project, "--profile", old["path"], "--replace")
        preview = self.command(*args)
        self.assertIsNotNone(preview["blocked"])
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], success=False)
        custom = self.root / "manual.md"
        custom.write_text("MANUAL_ALPHA", encoding="utf-8")
        new = self.profile("--from-profile", old["path"], "--custom-file", custom, release="1.1.0")
        self.compose(new, "--accept-current", versioned.text_digest(edited))
        self.assertIn("MANUAL_ALPHA", opinion.read_text())
        self.command("verify", "--project", self.project)

    def test_confirmation_invalidated_by_project_change(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        args = ("compose", "--project", self.project, "--profile", profile["path"], "--replace")
        preview = self.command(*args)
        (self.project / "OPINION.md").write_text("EXISTING_ALPHA", encoding="utf-8")
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], success=False)
        self.assertEqual((self.project / "OPINION.md").read_text(), "EXISTING_ALPHA")

    def test_source_content_drift_is_detected(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        path = self.catalog / "rules/fixture.alpha/versions/1.0.0.json"
        value = json.loads(path.read_text())
        value["text"] = "DRIFT_ALPHA"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.command("updates", "--catalog", self.catalog, "--profile", profile["path"], success=False)
        self.command("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.1.0", "--from-profile", profile["path"], success=False)

    def test_profile_snapshot_tampering_is_detected(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        value = profile["profile"]
        value["sources"]["rules"][0]["value"]["text"] = "DRIFT_ALPHA"
        path = self.file("tampered.json", value)
        self.command("compose", "--project", self.project, "--profile", path, success=False)

    def test_parent_symlink_rejected(self) -> None:
        self.seed()
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".opinion").symlink_to(outside, target_is_directory=True)
        args = ("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha@1.0.0")
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], success=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_publication_requires_explicit_public_approval(self) -> None:
        path = self.file("release.json", self.rule())
        args = ("publish", "--catalog", versioned.PUBLIC_CATALOG, "--file", path)
        before = {str(item.relative_to(versioned.PUBLIC_CATALOG)): item.read_bytes()
                  for item in versioned.PUBLIC_CATALOG.rglob("*.json")}
        preview = self.command(*args)
        result = self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], success=False)
        self.assertIn("公开内容需要用户批准", result["error"])
        after = {str(item.relative_to(versioned.PUBLIC_CATALOG)): item.read_bytes()
                 for item in versioned.PUBLIC_CATALOG.rglob("*.json")}
        self.assertEqual(after, before)
        self.assertFalse((versioned.PUBLIC_CATALOG / "rules/fixture.alpha/versions/1.0.0.json").exists())

    def test_custom_only_profile_and_compare(self) -> None:
        custom = self.root / "custom.md"
        custom.write_text("CUSTOM_ALPHA", encoding="utf-8")
        old = self.profile("--custom-file", custom)
        custom.write_text("CUSTOM_BETA", encoding="utf-8")
        new = self.profile("--from-profile", old["path"], "--custom-file", custom, release="1.1.0")
        comparison = self.command("compare", "--from-profile", old["path"], "--to-profile", new["path"])
        self.assertTrue(comparison["custom_changed"])
        self.assertIn("CUSTOM_BETA", self.compose(new)["content"])

    def test_explicit_removal_and_rule_disabling(self) -> None:
        self.seed()
        self.publish(self.rule(identity="fixture.beta", slot="fixture.beta-slot"))
        old = self.profile("--template", "fixture/alpha@1.0.0", "--rule", "fixture.beta@1.0.0")
        new = self.profile("--from-profile", old["path"], "--remove-template", "fixture", release="1.1.0")
        self.assertEqual(new["profile"]["templates"], [])
        self.assertIn("fixture.beta", versioned.effective_rules(new["profile"]))
        disabled = self.file("disabled.json", {"fixture.alpha": None})
        third = self.profile("--from-profile", old["path"], "--overrides-file", disabled, release="1.2.0")
        self.assertNotIn("fixture.alpha", versioned.effective_rules(third["profile"]))

    def test_existing_profile_release_cannot_be_changed(self) -> None:
        self.seed()
        self.profile("--template", "fixture/alpha@1.0.0")
        edits = self.file("overrides.json", {"fixture.alpha": {key: self.rule(text="LOCAL_ALPHA")[key] for key in versioned.EDITABLE}})
        args = ("profile", "--project", self.project, "--catalog", self.catalog, "--id", "private.fixture", "--version", "1.0.0", "--template", "fixture/alpha@1.0.0", "--overrides-file", edits)
        preview = self.command(*args)
        self.command(*args, "--apply", "--confirm", preview["confirmation_sha256"], success=False)

    def test_legacy_compose_cannot_bypass_lock(self) -> None:
        self.seed()
        old = self.profile("--template", "fixture/alpha@1.0.0")
        self.compose(old)
        before = (self.project / "OPINION.md").read_bytes()
        custom = self.root / "custom.md"
        custom.write_text("CUSTOM_BETA", encoding="utf-8")
        error = self.command("compose", "--project", self.project, "--custom-file", custom, "--apply", "--replace", success=False)
        self.assertIn("compose --profile", error["error"])
        self.assertEqual((self.project / "OPINION.md").read_bytes(), before)

    def test_lock_tampering_is_detected(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        self.compose(profile)
        path = self.project / "opinion.lock.json"
        lock = json.loads(path.read_text())
        lock["profile_sha256"] = "0" * 64
        path.write_text(json.dumps(lock), encoding="utf-8")
        self.command("verify", "--project", self.project, success=False)
        self.command("compose", "--project", self.project, "--profile", profile["path"], success=False)

    def test_same_locked_profile_cannot_be_redefined(self) -> None:
        custom = self.root / "custom.md"
        custom.write_text("CUSTOM_ALPHA", encoding="utf-8")
        profile = self.profile("--custom-file", custom)
        self.compose(profile)
        changed = {**profile["profile"], "custom": "CUSTOM_BETA"}
        path = self.file("changed-profile.json", changed)
        self.command("compose", "--project", self.project, "--profile", path, success=False)

    def test_invalid_structure_returns_controlled_error(self) -> None:
        invalid_values = [self.rule("01.0.0"), {**self.rule(), "scopes": [{}]}, {**self.rule(), "level": []}, {**self.template(), "rules": ["fixture.alpha@latest"]}, {**self.rule(), "extra": True}]
        for value in invalid_values:
            with self.subTest(value=value):
                path = self.file("invalid.json", value)
                result = self.command("publish", "--catalog", self.catalog, "--file", path, success=False)
                self.assertFalse(result["ok"])

    def test_versioned_schema_contracts_match_runtime_fields(self) -> None:
        references = SCRIPT.parents[1] / "references"
        for filename, expected, optional in (("rule.schema.json", versioned.RULE_FIELDS, set()), ("versioned-template.schema.json", versioned.TEMPLATE_FIELDS, set()), ("profile.schema.json", versioned.PROFILE_FIELDS, versioned.PROFILE_OPTIONAL_FIELDS)):
            schema = json.loads((references / filename).read_text())
            self.assertEqual(set(schema["required"]), expected)
            self.assertEqual(set(schema["properties"]), expected | optional)
            self.assertFalse(schema["additionalProperties"])
        definitions = json.loads((references / "profile.schema.json").read_text())["$defs"]
        for name, expected in (("loading", versioned.LOADING_FIELDS), ("bundle", versioned.BUNDLE_FIELDS)):
            self.assertEqual(set(definitions[name]["required"]), expected)
            self.assertEqual(set(definitions[name]["properties"]), expected)
            self.assertFalse(definitions[name]["additionalProperties"])

    def test_out_of_order_patch_cannot_change_behavior(self) -> None:
        self.publish(self.rule("1.0.1"))
        path = self.file("older.json", self.rule("1.0.0", text="RULE_BETA"))
        result = self.command("publish", "--catalog", self.catalog, "--file", path, success=False)
        self.assertIn("patch", result["error"])

    def test_profile_preview_shows_final_content_and_differences(self) -> None:
        self.seed()
        old = self.profile("--template", "fixture/alpha@1.0.0")
        edits = self.file("overrides.json", {"fixture.alpha": {key: self.rule(text="LOCAL_ALPHA")[key] for key in versioned.EDITABLE}})
        new = self.profile("--from-profile", old["path"], "--overrides-file", edits, release="1.1.0")
        self.assertIn("LOCAL_ALPHA", new["content"])
        self.assertEqual(new["changes"]["rule_changes"][0]["before"]["text"], "RULE_ALPHA")
        self.assertEqual(new["changes"]["rule_changes"][0]["after"]["text"], "LOCAL_ALPHA")

    def test_missing_opinion_rebuilds_exact_locked_profile(self) -> None:
        self.seed()
        profile = self.profile("--template", "fixture/alpha@1.0.0")
        original = self.compose(profile)["content"]
        (self.project / "OPINION.md").unlink()
        self.command("verify", "--project", self.project, success=False)
        self.assertEqual(self.compose(profile)["content"], original)
        self.command("verify", "--project", self.project)


if __name__ == "__main__":
    unittest.main()
