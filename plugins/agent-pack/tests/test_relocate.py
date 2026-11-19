"""`agenthelm relocate`: Skill copy / move between clients and scopes (preview by default, digest-confirmed apply)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_ext_common as ext  # noqa: E402
import agent_pack_relocate as relocate  # noqa: E402

FAKE_TOKEN = "sk-abcdefghijklmnopqrstuv"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "proj"
        self.home.mkdir()
        self.project.mkdir()
        self.skill = self.make_skill(self.home / ".claude" / "skills", "demo")

    def make_skill(self, base: Path, name: str, extra: str = "one") -> Path:
        directory = base / name
        (directory / "sub").mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: A demo skill used by the relocate tests\n---\nbody\n", encoding="utf-8")
        (directory / "sub" / "a.txt").write_text(extra, encoding="utf-8")
        return directory

    def cli(self, *args: str, lang: str = "en") -> tuple[int, str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG=lang)
        proc = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "relocate", "--home", str(self.home),
                               "--project", str(self.project), *args], capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def preview(self, ref: str, to: str, *args: str) -> dict:
        code, out, err = self.cli(ref, "--to", to, "--output", "json", *args)
        self.assertIn(code, (0, 1), err)
        return json.loads(out)

    def apply(self, ref: str, to: str, *args: str, digest: str | None = None) -> tuple[int, dict]:
        digest = digest or self.preview(ref, to, *args)["plan"]["plan_digest"]
        code, out, err = self.cli(ref, "--to", to, "--output", "json", "--apply", "--expect-plan", digest, *args)
        self.assertIn(code, (0, 1), err)
        return code, json.loads(out)

    def snapshot(self) -> dict[str, str]:
        found = {}
        for base in (self.home, self.project):
            for path in sorted(base.rglob("*")):
                found[str(path)] = "L:" + os.readlink(path) if path.is_symlink() else (
                    "D" if path.is_dir() else path.read_text(encoding="utf-8", errors="replace"))
        return found


class PlanTests(Base):
    def test_preview_writes_nothing_and_has_digest_and_promises(self) -> None:
        before = self.snapshot()
        document = self.preview("skill:demo@claude:user", "codex:project")
        self.assertEqual(before, self.snapshot())
        plan = document["plan"]
        self.assertEqual("preview", document["mode"])
        self.assertEqual("ready", plan["status"])
        self.assertEqual("narrow", plan["direction"])
        self.assertRegex(plan["plan_digest"], r"^[0-9a-f]{64}$")
        self.assertTrue(all(step["promise"] in ("certain", "best-effort", "cannot-promise") for step in plan["steps"]))
        self.assertEqual("project-content", plan["confirmations"][0]["id"])
        self.assertEqual(f"{relocate.inv.PROJECT}/.agents/skills/demo", plan["target"]["path"])

    def test_digest_is_stable_and_changes_with_content(self) -> None:
        first = self.preview("skill:demo@claude:user", "codex:project")["plan"]["plan_digest"]
        self.assertEqual(first, self.preview("skill:demo@claude:user", "codex:project")["plan"]["plan_digest"])
        (self.skill / "sub" / "a.txt").write_text("changed", encoding="utf-8")
        self.assertNotEqual(first, self.preview("skill:demo@claude:user", "codex:project")["plan"]["plan_digest"])

    def test_bad_inputs_are_blocked_not_crashing(self) -> None:
        self.assertEqual("invalid-ref", self.preview("nonsense", "codex:project")["plan"]["blocked"][0]["code"])
        self.assertEqual("invalid-target", self.preview("skill:demo@claude:user", "vim:user")["plan"]["blocked"][0]["code"])
        self.assertEqual("unsupported-kind", self.preview("mcp:x@claude:user", "codex:user")["plan"]["blocked"][0]["code"])
        missing = self.preview("skill:nope@claude:user", "codex:user")["plan"]["blocked"][0]
        self.assertEqual("source-not-found", missing["code"])

    def test_native_store_is_a_zero_write_plan(self) -> None:
        shared = self.make_skill(self.home / ".agents" / "skills", "shared")
        plan = self.preview("skill:shared@codex:user#agents", "codex:user")["plan"]
        self.assertEqual("native", plan["status"])
        self.assertEqual([], plan["blocked"])
        self.assertTrue(shared.exists())

    def test_unknown_evidence_source_is_read_only(self) -> None:
        (self.skill / "SKILL.md").write_bytes(b"x" * (5 * 1024 * 1024))
        plan = self.preview("skill:demo@claude:user", "codex:project")["plan"]
        self.assertEqual("blocked", plan["status"])
        self.assertIn("source-not-file-evidence", [item["code"] for item in plan["blocked"]])

    def test_digest_mismatch_is_refused_without_writing(self) -> None:
        before = self.snapshot()
        code, document = self.apply("skill:demo@claude:user", "codex:project", "--confirm-project-content", digest="0" * 64)
        self.assertEqual(1, code)
        self.assertEqual("plan-digest-mismatch", document["applied"]["reason"])
        self.assertEqual(before, self.snapshot())

    def test_apply_needs_expect_plan(self) -> None:
        code, _out, err = self.cli("skill:demo@claude:user", "--to", "codex:project", "--apply")
        self.assertEqual(2, code)
        self.assertIn("--expect-plan", err)

    def test_changed_source_after_preview_is_refused(self) -> None:
        digest = self.preview("skill:demo@claude:user", "codex:project")["plan"]["plan_digest"]
        (self.skill / "sub" / "a.txt").write_text("edited after preview", encoding="utf-8")
        code, document = self.apply("skill:demo@claude:user", "codex:project", "--confirm-project-content", digest=digest)
        self.assertEqual("plan-digest-mismatch", document["applied"]["reason"])
        self.assertFalse((self.project / ".agents").exists())


class ApplyTests(Base):
    def test_user_to_project_copy_moves_source_into_recovery(self) -> None:
        code, document = self.apply("skill:demo@claude:user", "codex:project", "--confirm-project-content")
        self.assertEqual(0, code, document)
        applied = document["applied"]
        target = self.project / ".agents" / "skills" / "demo"
        self.assertEqual("applied", applied["status"])
        self.assertTrue(applied["readback"]["ok"])
        self.assertEqual("one", (target / "sub" / "a.txt").read_text())
        self.assertFalse(self.skill.exists(), "source must be archived")
        archived = next((self.home / ".agent-pack" / "recovery").glob("*-relocate-demo/skills/demo"))
        self.assertTrue((archived / "SKILL.md").is_file(), "archived, not deleted")
        self.assertIn("mv ", applied["restore"][0])
        self.assertEqual(0o700, (archived.parents[1]).stat().st_mode & 0o777)

    def test_project_content_needs_confirmation(self) -> None:
        code, document = self.apply("skill:demo@claude:user", "codex:project")
        self.assertEqual(1, code)
        self.assertEqual("project-content-unconfirmed", document["applied"]["reason"])
        self.assertTrue(self.skill.exists())
        self.assertFalse((self.project / ".agents").exists())

    def test_keep_source_copies_and_leaves_source(self) -> None:
        code, document = self.apply("skill:demo@claude:user", "cursor:user", "--keep-source")
        self.assertEqual(0, code, document)
        self.assertTrue(self.skill.exists())
        self.assertTrue((self.home / ".cursor" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertIsNone(document["applied"]["archived"])

    def test_existing_target_is_never_touched(self) -> None:
        other = self.make_skill(self.project / ".agents" / "skills", "demo", extra="different")
        before = self.snapshot()
        plan = self.preview("skill:demo@claude:user", "codex:project")["plan"]
        self.assertEqual("target-exists-different", plan["blocked"][0]["code"])
        code, document = self.apply("skill:demo@claude:user", "codex:project", "--confirm-project-content")
        self.assertEqual(1, code)
        self.assertEqual(before, self.snapshot())
        self.assertEqual("different", (other / "sub" / "a.txt").read_text())

    def test_identical_existing_target_is_reported_as_identical(self) -> None:
        self.make_skill(self.project / ".agents" / "skills", "demo")
        plan = self.preview("skill:demo@claude:user", "codex:project")["plan"]
        self.assertEqual("target-exists-identical", plan["blocked"][0]["code"])

    def test_mid_failure_rolls_back_only_what_was_created(self) -> None:
        original = ext.archive

        def failing(*_args, **_kwargs):
            raise ext.ExtError("boom")

        plan, ctx = relocate.build_plan(self.home, self.project, "skill:demo@claude:user", "codex:project")
        with mock.patch.object(ext, "archive", failing):
            applied = relocate.apply_plan(plan, ctx, True)
        self.assertEqual("rolled-back", applied["status"])
        self.assertTrue((self.skill / "sub" / "a.txt").is_file(), "source untouched")
        self.assertFalse((self.project / ".agents").exists(), "created target and parents removed")
        self.assertEqual([], [p for p in self.project.rglob(".*agenthelm-tmp*")])
        self.assertTrue(list((self.home / ".agent-pack" / "recovery").glob("*-relocate-demo")), "recovery dir kept")
        self.assertIs(original, ext.archive)

    def test_failure_after_archive_puts_source_back(self) -> None:
        plan, ctx = relocate.build_plan(self.home, self.project, "skill:demo@claude:user", "cursor:user")
        real = ext.readback_matches
        calls = {"n": 0}

        def flaky(manifest, target):
            calls["n"] += 1
            return real(manifest, target) if calls["n"] < 2 else False  # the read-back of the installed copy fails

        with mock.patch.object(ext, "readback_matches", flaky):
            applied = relocate.apply_plan(plan, ctx, True)
        self.assertEqual("rolled-back", applied["status"])
        self.assertTrue((self.skill / "SKILL.md").is_file())
        self.assertFalse((self.home / ".cursor" / "skills" / "demo").exists())

    def test_inner_symlink_stays_a_link_and_external_one_blocks(self) -> None:
        os.symlink("a.txt", self.skill / "sub" / "alias")
        plan = self.preview("skill:demo@claude:user", "cursor:user", "--keep-source")["plan"]
        self.assertEqual("ready", plan["status"])
        self.apply("skill:demo@claude:user", "cursor:user", "--keep-source")
        self.assertEqual("a.txt", os.readlink(self.home / ".cursor" / "skills" / "demo" / "sub" / "alias"))
        os.symlink(str(self.root), self.skill / "escape")
        blocked = self.preview("skill:demo@claude:user", "codex:user")["plan"]["blocked"]
        self.assertEqual("source-has-external-symlink", blocked[0]["code"])


class ScopeAndModeTests(Base):
    def test_widen_needs_flag_then_works(self) -> None:
        self.make_skill(self.project / ".claude" / "skills", "local")
        ref = "skill:local@claude:project"
        plan = self.preview(ref, "claude:user")["plan"]
        self.assertEqual("widen", plan["direction"])
        self.assertEqual("widen-needs-flag", plan["blocked"][0]["code"])
        widened = self.preview(ref, "claude:user", "--allow-widen")["plan"]
        self.assertEqual("ready", widened["status"])
        self.assertEqual("widen", widened["direction"])
        code, document = self.apply(ref, "claude:user", "--allow-widen")
        self.assertEqual(0, code, document)
        self.assertTrue((self.home / ".claude" / "skills" / "local" / "SKILL.md").is_file())

    def test_widen_is_red_only_on_a_terminal(self) -> None:
        self.make_skill(self.project / ".claude" / "skills", "local")
        _code, out, _err = self.cli("skill:local@claude:project", "--to", "claude:user", "--allow-widen")
        self.assertNotIn("\033[", out)
        plan, _ctx = relocate.build_plan(self.home, self.project, "skill:local@claude:project", "claude:user", allow_widen=True)
        self.assertIn("\033[31m", relocate.render_text(plan, None, "cmd", color=True))

    def test_link_inside_a_project_is_relative(self) -> None:
        self.make_skill(self.project / ".agents" / "skills", "proj")
        ref = "skill:proj@codex:project"
        plan = self.preview(ref, "claude:project", "--mode", "link")["plan"]
        self.assertEqual("link", plan["mode"])
        self.assertFalse(plan["retire_source"])
        code, document = self.apply(ref, "claude:project", "--mode", "link")
        self.assertEqual(0, code, document)
        link = self.project / ".claude" / "skills" / "proj"
        self.assertTrue(link.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(link)))
        self.assertEqual("../../.agents/skills/proj", os.readlink(link))
        self.assertEqual((self.project / ".agents" / "skills" / "proj").resolve(), link.resolve())

    def test_link_across_scopes_is_blocked(self) -> None:
        plan = self.preview("skill:demo@claude:user", "codex:project", "--mode", "link")["plan"]
        self.assertEqual("link-cross-scope", plan["blocked"][0]["code"])

    def test_synced_folder_downgrades_link_to_copy(self) -> None:
        synced = self.root / "Dropbox" / "proj"
        synced.mkdir(parents=True)
        self.project = synced
        self.make_skill(synced / ".agents" / "skills", "proj")
        plan = self.preview("skill:proj@codex:project", "claude:project", "--mode", "link")["plan"]
        self.assertEqual("copy", plan["mode"])
        self.assertEqual("Dropbox", plan["sync_folder"])
        self.assertEqual("cannot-promise", plan["steps"][-1]["promise"])
        self.assertTrue("sync-folder-demoted" in [item["code"] for item in plan["warnings"]])
        self.apply("skill:proj@codex:project", "claude:project", "--mode", "link")
        self.assertFalse((synced / ".claude" / "skills" / "proj").is_symlink())

    def test_sync_heuristic(self) -> None:
        self.assertEqual("OneDrive - Corp", ext.sync_folder_hint(Path("/x/OneDrive - Corp/a")))
        self.assertEqual("Mobile Documents", ext.sync_folder_hint(Path("/Users/u/Library/Mobile Documents/com~apple~CloudDocs/p")))
        self.assertIsNone(ext.sync_folder_hint(Path("/x/dropboxing/a")))


class SecretAndLanguageTests(Base):
    def test_fake_token_in_unrelated_config_never_reaches_output_or_recovery(self) -> None:
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"s": {
            "command": "npx", "args": ["x", FAKE_TOKEN], "env": {"API_KEY": FAKE_TOKEN}}}}), encoding="utf-8")
        _code, out, err = self.cli("skill:demo@claude:user", "--to", "codex:project", "--output", "json")
        document = json.loads(out)
        self.apply("skill:demo@claude:user", "codex:project", "--confirm-project-content")
        blob = out + err
        for path in (self.home / ".agent-pack").rglob("*"):
            if path.is_file():
                blob += path.read_text(errors="replace")
        self.assertNotIn(FAKE_TOKEN, blob)
        self.assertEqual("ready", document["plan"]["status"])

    def test_text_follows_language_and_json_keys_are_fixed(self) -> None:
        _code, english, _ = self.cli("skill:demo@claude:user", "--to", "codex:project", lang="en")
        _code, chinese, _ = self.cli("skill:demo@claude:user", "--to", "codex:project", lang="zh")
        self.assertIn("Plan digest:", english)
        self.assertNotRegex(english, r"[一-鿿]")
        self.assertIn("计划指纹", chinese)
        self.assertEqual(self.preview("skill:demo@claude:user", "codex:project", "--lang", "zh")["plan"]["plan_digest"],
                         self.preview("skill:demo@claude:user", "codex:project", "--lang", "en")["plan"]["plan_digest"])


if __name__ == "__main__":
    unittest.main()
