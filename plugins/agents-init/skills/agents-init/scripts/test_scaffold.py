"""脚手架一致性：期望骨架清单、受检 Profile 字段、内容锁与用户决定。

所有项目都在临时目录里造；“旧版项目”用当前初始化器生成后删文件、改 Profile、改内容锁来模拟。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("init_project.py")
sys.path.insert(0, str(SCRIPT.parent))
import init_project  # noqa: E402
import scaffold  # noqa: E402

scaffold.bind(init_project)
PROFILE = init_project.PROFILE_PATH
LOCK = init_project.CONTENT_LOCK_PATH
DECISIONS = scaffold.DECISIONS_PATH


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


class ScaffoldCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        base = Path(self._temporary.name).resolve()
        self.root = base / "sample-project"
        self.root.mkdir()
        self.state = base / "state"
        self.recovery = base / "recovery"

    def init(self, *extra: str, name: str = "Sample Project") -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.root), "--name", name, "--project-type", "notes",
             "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude",
             "--output", "json", "--apply", *extra],
            capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def run_mode(self, mode: str, *extra: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.root), "--mode", mode, "--output", "json",
             "--state-dir", str(self.state), *extra], capture_output=True, text=True, check=False)
        return completed.returncode, json.loads(completed.stdout)

    def check(self, **kwargs) -> dict:
        return scaffold.check(self.root, state_dir=self.state, **kwargs)

    def ids(self, report: dict) -> set[str]:
        return {item["id"] for item in report["findings"]}

    def old_project(self) -> None:
        """A project as an older initializer left it: no launcher, no lock, four scaffold files missing."""
        self.init("--no-entry")
        for relative in (".agents/plugin.json", ".agents/mcp.json", "docs/refs/README.md"):
            (self.root / relative).unlink()
        (self.root / "docs/drafts/.gitkeep").unlink()
        (self.root / "docs/drafts").rmdir()
        lock = self.root / LOCK
        if lock.exists():
            lock.unlink()


class ManifestTests(ScaffoldCase):
    def test_manifest_matches_what_the_initializer_renders(self) -> None:
        for extra in ((), ("--no-entry",), ("--work-mode", "legacy", "--no-entry")):
            with self.subTest(extra=extra):
                self.setUp()
                self.init(*extra)
                ctx = scaffold.Context(self.root)
                expected = {path.as_posix() for path in ctx.rendered} - {PROFILE.as_posix()}
                self.assertEqual({entry["path"] for entry in ctx.entries()}, expected)

    def test_lock_compared_entries_are_exactly_the_tracked_templates(self) -> None:
        self.init()
        ctx = scaffold.Context(self.root)
        locked = {entry["path"] for entry in ctx.entries() if entry["compare"] == "lock"}
        self.assertEqual(locked, ctx.tracked)

    def test_every_profile_rule_points_at_a_field_the_initializer_writes_or_is_manual(self) -> None:
        self.init()
        ctx = scaffold.Context(self.root)
        for rule in ctx.manifest["profile"]:
            present, _ = scaffold._get(ctx.fresh, rule["path"])
            self.assertTrue(present or rule["mode"] == "manual", rule["path"])

    def test_fresh_project_is_consistent(self) -> None:
        self.init()
        report = self.check()
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertTrue(report["ok"])

    def test_opinion_and_audit_fields_are_not_compared(self) -> None:
        self.init()
        profile = json.loads((self.root / PROFILE).read_text())
        profile["opinion"]["strict_mode"] = "strict"
        profile["initializer_version"] = "0.1.0"
        profile["runtime"]["source_ref"] = "v1.0.0"
        (self.root / PROFILE).write_text(json.dumps(profile))
        (self.root / "OPINION.md").write_text("own rules\n")
        report = self.check()
        self.assertEqual(report["findings"], [])
        self.assertNotIn("OPINION.md", {item["path"] for item in report["skeleton"] if item["state"] != "ignored"})


class SkeletonTests(ScaffoldCase):
    def test_missing_files_are_listed_and_fixed_one_by_one(self) -> None:
        self.old_project()
        report = self.check()
        self.assertEqual(self.ids(report), {"skel:.agents/plugin.json", "skel:.agents/mcp.json",
                                            "skel:docs/refs/README.md", "skel:docs/drafts/.gitkeep"})
        before = snapshot(self.root)
        preview = scaffold.add_skeleton(self.root, [".agents/mcp.json"], state_dir=self.state)
        self.assertFalse(preview["applied"])
        self.assertEqual(snapshot(self.root), before)
        scaffold.add_skeleton(self.root, [".agents/mcp.json"], apply=True, state_dir=self.state)
        self.assertEqual(len(self.ids(self.check())), 3)
        scaffold.add_skeleton(self.root, None, apply=True, state_dir=self.state)
        self.assertEqual(self.check()["findings"], [])
        self.assertEqual(json.loads((self.root / ".agents/mcp.json").read_text())["mcpServers"], {})

    def test_existing_files_are_never_touched(self) -> None:
        self.init("--no-entry")
        (self.root / "AGENTS.md").write_text("my own contract\n")
        (self.root / ".agents/plugin.json").write_text('{"name": "mine"}')
        result = scaffold.add_skeleton(self.root, ["AGENTS.md", ".agents/plugin.json"], apply=True, state_dir=self.state)
        self.assertEqual(result["create"], [])
        self.assertEqual((self.root / "AGENTS.md").read_text(), "my own contract\n")
        self.assertEqual((self.root / ".agents/plugin.json").read_text(), '{"name": "mine"}')

    def test_unknown_path_is_refused(self) -> None:
        self.old_project()
        with self.assertRaises(init_project.InitError):
            scaffold.add_skeleton(self.root, ["notes/random.md"], state_dir=self.state)

    def test_check_never_writes(self) -> None:
        self.old_project()
        before = snapshot(self.root)
        self.check()
        self.run_mode("check")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.state.exists())

    def test_a_directory_that_has_other_content_counts_as_present(self) -> None:
        self.init("--no-entry")
        (self.root / "docs/drafts/.gitkeep").unlink()
        (self.root / "docs/drafts/idea.md").write_text("x\n")
        self.assertEqual(self.check()["findings"], [])

    def test_custom_roots_in_the_profile_are_honoured(self) -> None:
        self.init("--no-entry")
        profile = json.loads((self.root / PROFILE).read_text())
        profile["work"]["case"]["root"] = "work-items"
        profile["memory"]["root"] = "notes-memory"
        (self.root / PROFILE).write_text(json.dumps(profile))
        self.assertEqual(self.ids(self.check()), {"skel:work-items/.gitkeep", "skel:notes-memory/.gitkeep"})

    def test_a_non_ascii_project_name_does_not_break_the_check(self) -> None:
        self.init("--no-entry", "--slug", "sample-notes", name="样例笔记")
        (self.root / ".agents/plugin.json").unlink()
        self.assertEqual(self.ids(self.check()), {"skel:.agents/plugin.json"})

    def test_cli_modes_and_exit_codes(self) -> None:
        self.old_project()
        code, report = self.run_mode("check")
        self.assertEqual(code, 1)
        self.assertEqual(len(report["findings"]), 4)
        code, preview = self.run_mode("add-skeleton")
        self.assertEqual((code, preview["applied"]), (0, False))
        code, applied = self.run_mode("add-skeleton", "--apply")
        self.assertEqual((code, applied["applied"]), (0, True))
        self.assertEqual(self.run_mode("check")[0], 0)


class DecisionTests(ScaffoldCase):
    def test_skip_is_remembered_by_another_machine_and_listed(self) -> None:
        self.old_project()
        identifier = "skel:docs/drafts/.gitkeep"
        preview = scaffold.decide(self.root, identifier, "skip", reason="no drafts here", state_dir=self.state)
        self.assertFalse(preview["applied"])
        self.assertFalse((self.root / DECISIONS).exists())
        scaffold.decide(self.root, identifier, "skip", reason="no drafts here", apply=True, state_dir=self.state)
        other_machine = Path(self._temporary.name) / "other-state"
        report = scaffold.check(self.root, state_dir=other_machine)
        self.assertNotIn(identifier, self.ids(report))
        skipped = {item["id"]: item for item in report["skipped"]}
        self.assertEqual(skipped[identifier]["decision"]["reason"], "no drafts here")
        self.assertFalse(other_machine.exists())

    def test_changed_expectation_asks_again_and_reprompt_forces_it(self) -> None:
        self.old_project()
        identifier = "skel:docs/refs/README.md"
        scaffold.decide(self.root, identifier, "skip", apply=True, state_dir=self.state)
        self.assertNotIn(identifier, self.ids(self.check()))
        self.assertIn(identifier, self.ids(self.check(reprompt=True)))
        scaffold.decide(self.root, identifier, "skip", fingerprint="template-as-it-was", apply=True, state_dir=self.state)
        again = {item["id"]: item for item in self.check()["findings"]}
        self.assertIn("changed_since_decision", again[identifier])

    def test_later_does_not_hide_the_finding(self) -> None:
        self.old_project()
        scaffold.decide(self.root, "skel:.agents/mcp.json", "later", apply=True, state_dir=self.state)
        found = {item["id"]: item for item in self.check()["findings"]}
        self.assertIn("deferred", found["skel:.agents/mcp.json"])

    def test_unskip_restores_the_finding(self) -> None:
        self.old_project()
        scaffold.decide(self.root, "skel:.agents/mcp.json", "skip", apply=True, state_dir=self.state)
        scaffold.undecide(self.root, "skel:.agents/mcp.json", apply=True, state_dir=self.state)
        self.assertIn("skel:.agents/mcp.json", self.ids(self.check()))

    def test_unknown_finding_cannot_be_skipped(self) -> None:
        self.init("--no-entry")
        with self.assertRaises(init_project.InitError):
            scaffold.decide(self.root, "skel:nothing", "skip", state_dir=self.state)

    def test_machine_decisions_stay_out_of_the_project(self) -> None:
        self.init("--no-entry")
        before = snapshot(self.root)
        scaffold.decide(self.root, "local:runtime-missing", "skip", fingerprint="runtime-missing", reason="dev box",
                        apply=True, state_dir=self.state)
        self.assertEqual(snapshot(self.root), before)
        decisions = scaffold.load_decisions(self.root, self.state)
        findings = [{"id": "local:runtime-missing", "scope": "machine", "fingerprint": "runtime-missing"}]
        active, skipped = scaffold.partition(findings, decisions)
        self.assertEqual((active, len(skipped)), ([], 1))

    def test_a_decision_file_changed_elsewhere_is_not_overwritten(self) -> None:
        self.old_project()
        scaffold.decide(self.root, "skel:.agents/mcp.json", "skip", apply=True, state_dir=self.state)
        path = self.root / DECISIONS
        table, current, _ = scaffold.read_decisions(path)
        path.write_text(path.read_text() + "\n")  # another machine's sync landed in between
        with self.assertRaises(init_project.InitError) as caught:
            scaffold._store(path, table, current, self.root, "project")
        self.assertEqual(caught.exception.code, "conflict")

    def test_a_broken_decision_file_is_reported_not_fatal(self) -> None:
        self.old_project()
        (self.root / DECISIONS).write_text("{not json")
        report = self.check()
        self.assertTrue(report["decisions"]["errors"])
        self.assertEqual(len(report["findings"]), 4)

    def test_sync_conflict_copies_are_reported(self) -> None:
        self.init("--no-entry")
        directory = (self.root / DECISIONS).parent
        (directory / "check-decisions 2.json").write_text("{}")
        self.assertEqual(self.check()["decisions"]["conflict_copies"], ["check-decisions 2.json"])

    def test_decide_cli(self) -> None:
        self.old_project()
        code, result = self.run_mode("decide", "--target", "skel:.agents/mcp.json", "--decision", "skip",
                                     "--reason", "not needed", "--apply")
        self.assertEqual((code, result["applied"]), (0, True))
        self.assertNotIn("skel:.agents/mcp.json", self.ids(self.check()))
        code, result = self.run_mode("decide", "--target", "skel:.agents/mcp.json", "--decision", "unskip", "--apply")
        self.assertEqual((code, result["applied"]), (0, True))
        self.assertIn("skel:.agents/mcp.json", self.ids(self.check()))


class ProfileTests(ScaffoldCase):
    def drop(self, *paths: str) -> None:
        profile = json.loads((self.root / PROFILE).read_text())
        for dotted in paths:
            parts = dotted.split(".")
            node = profile
            for part in parts[:-1]:
                node = node[part]
            node.pop(parts[-1])
        (self.root / PROFILE).write_text(json.dumps(profile, indent=2))

    def test_missing_fields_are_found_and_fixed_without_touching_the_rest(self) -> None:
        self.init("--no-entry")
        profile = json.loads((self.root / PROFILE).read_text())
        profile["work"]["case"]["directory_naming"] = {"style": "date-topic"}
        profile["custom"] = {"anything": 1}
        (self.root / PROFILE).write_text(json.dumps(profile, indent=2))
        self.drop("runtime.commit_policy", "privacy")
        report = self.check()
        self.assertEqual(self.ids(report), {"profile:runtime.commit_policy", "profile:privacy.forbidden_default_reads",
                                            "profile:privacy.generated_outputs"})
        preview = scaffold.fix_profile(self.root, ["runtime.commit_policy"], state_dir=self.state)
        self.assertIn('+    "commit_policy": "explicit"', preview["diff"])
        self.assertEqual(preview["fields"], ["runtime.commit_policy"])
        with self.assertRaises(init_project.InitError):  # replacing the Profile needs a backup location
            scaffold.fix_profile(self.root, ["runtime.commit_policy"], apply=True, state_dir=self.state)
        applied = scaffold.fix_profile(self.root, ["runtime.commit_policy"], apply=True, recovery_dir=self.recovery,
                                       state_dir=self.state)
        self.assertTrue(applied["applied"])
        updated = json.loads((self.root / PROFILE).read_text())
        self.assertEqual(updated["runtime"]["commit_policy"], "explicit")
        self.assertNotIn("privacy", updated)  # not selected, not touched
        self.assertEqual(updated["custom"], {"anything": 1})
        self.assertEqual(updated["work"]["case"]["directory_naming"], {"style": "date-topic"})
        self.assertTrue((self.recovery / PROFILE).is_file())

    def test_project_own_fields_are_listed_but_never_flagged(self) -> None:
        self.init("--no-entry")
        profile = json.loads((self.root / PROFILE).read_text())
        profile["work"]["case"]["something_of_mine"] = True
        profile["team_notes"] = {"a": 1}
        (self.root / PROFILE).write_text(json.dumps(profile))
        report = self.check()
        self.assertEqual(report["findings"], [])
        own = {item["path"] for item in report["extras"] if item["kind"] == "project-field"}
        self.assertTrue({"team_notes", "work.case.something_of_mine"} <= own)

    def test_wrong_identity_value_is_flagged_and_manual_fields_have_no_fix(self) -> None:
        self.init("--no-entry")
        profile = json.loads((self.root / PROFILE).read_text())
        profile["runtime"]["capability_manager"] = "something-else"
        profile.pop("name")
        (self.root / PROFILE).write_text(json.dumps(profile))
        found = {item["id"]: item for item in self.check()["findings"]}
        self.assertEqual(found["profile:runtime.capability_manager"]["fix"], "profile")
        self.assertIsNone(found["profile:name"]["fix"])

    def test_profile_fix_cli_preview_writes_nothing(self) -> None:
        self.init("--no-entry")
        self.drop("memory.root")
        before = snapshot(self.root)
        code, result = self.run_mode("profile")
        self.assertEqual((code, result["applied"], result["fields"]), (0, False, ["memory.root"]))
        self.assertEqual(snapshot(self.root), before)


class LockTests(ScaffoldCase):
    def lock_with_unknown_version(self, files: dict[str, str]) -> None:
        path = self.root / LOCK
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "packs": {
            "agents-init/project-contract": {"plugin": "agents-init", "version": "0.0.0", "files": files}}}))

    def entry_version(self) -> str:
        return json.loads((self.root / LOCK).read_text())["packs"]["agents-init/project-contract"]["version"]

    def test_entry_mode_writes_the_real_version_not_zero(self) -> None:
        self.old_project()
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.root), "--mode", "entry", "--entry-name", "menu",
             "--apply", "--output", "json"], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(self.entry_version(), init_project.contract_pack()["version"])

    def test_existing_zero_version_is_recognised_and_repaired(self) -> None:
        self.init("--no-entry")
        ctx = scaffold.Context(self.root)
        launcher = init_project.launcher_text()
        (self.root / "menu").write_text(launcher)
        profile = json.loads((self.root / PROFILE).read_text())
        profile["entry"] = {"api": 1, "launcher": "menu"}
        (self.root / PROFILE).write_text(json.dumps(profile))
        self.lock_with_unknown_version({"menu": hashlib.sha256(launcher.encode()).hexdigest()})
        found = {item["id"]: item for item in self.check()["findings"]}
        lock_finding = found["lock:agents-init/project-contract"]
        self.assertEqual(lock_finding["fix"], "repair-lock")
        before = (self.root / LOCK).read_text()
        preview = scaffold.repair_lock(self.root)
        self.assertEqual((self.root / LOCK).read_text(), before)
        self.assertTrue(any("0.0.0" in change for change in preview["changes"]))
        scaffold.repair_lock(self.root, apply=True)
        self.assertEqual(self.entry_version(), ctx.pack["version"])
        self.assertNotIn("lock:agents-init/project-contract", self.ids(self.check()))

    def test_zero_version_with_unrecognised_baselines_is_not_guessed(self) -> None:
        self.init("--no-entry")
        self.lock_with_unknown_version({"AGENTS.md": "0" * 64})
        found = {item["id"]: item for item in self.check()["findings"]}
        self.assertEqual(found["lock:agents-init/project-contract"]["fix"], "update-content")
        result = scaffold.repair_lock(self.root, apply=True)
        self.assertTrue(result["blocked"])
        self.assertEqual(self.entry_version(), "0.0.0")

    def test_template_file_without_a_baseline_that_differs_is_reported(self) -> None:
        self.init("--no-entry")
        (self.root / LOCK).unlink()
        (self.root / "AGENTS.md").write_text("older contract\n")
        found = {item["id"]: item for item in self.check()["findings"]}
        self.assertEqual(found["content:AGENTS.md"]["fix"], "update-content")

    def test_keep_is_remembered_until_the_template_really_changes(self) -> None:
        self.init("--no-entry")
        (self.root / LOCK).unlink()
        (self.root / "AGENTS.md").write_text("my contract\n")

        def refresh(*extra: str) -> tuple[int, dict]:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--project", str(self.root), "--mode", "refresh", "--output", "json",
                 *extra], capture_output=True, text=True, check=False)
            return completed.returncode, json.loads(completed.stdout)

        code, result = refresh()
        self.assertEqual(code, 1)  # blocked: needs a choice
        code, result = refresh("--keep", "AGENTS.md", "--apply")
        self.assertEqual((code, result["applied"]), (0, True))
        self.assertEqual((self.root / "AGENTS.md").read_text(), "my contract\n")
        table, _, _ = scaffold.read_decisions(self.root / DECISIONS)
        self.assertEqual(table["content:AGENTS.md"]["decision"], "keep")
        # Same template later: no new choice required.
        self.assertEqual(refresh()[0], 0)
        # The template changes for this file: asked again.
        table["content:AGENTS.md"]["fingerprint"] = "an-older-render"
        (self.root / DECISIONS).write_text(json.dumps({"schema_version": 1, "decisions": table}))
        self.assertEqual(refresh()[0], 1)


class CaseNamingTests(ScaffoldCase):
    def make_cases(self, *names: str) -> None:
        for name in names:
            directory = self.root / "cases" / name
            directory.mkdir(parents=True)
            (directory / "CASE.md").write_text("# case\n", encoding="utf-8")

    def declare(self, naming) -> None:
        profile = json.loads((self.root / PROFILE).read_text())
        profile["work"]["case"]["directory_naming"] = naming
        (self.root / PROFILE).write_text(json.dumps(profile))

    def test_default_rule_flags_other_conventions_once_in_aggregate(self) -> None:
        self.init("--no-entry")
        self.make_cases("CASE-20260901-001-vocab", "20260902-grammar", "20260903-listening")
        found = {item["id"]: item for item in self.check()["findings"]}
        self.assertIn("2 个 Case 目录", found["case-naming"]["title"])
        self.assertEqual(found["case-naming"]["fix"], "case-naming")

    def test_a_declared_convention_is_checked_instead_of_the_default(self) -> None:
        self.init("--no-entry")
        self.make_cases("20260902-grammar", "20260903-listening")
        self.declare({"style": "date-topic"})
        self.assertEqual(self.check()["findings"], [])
        self.make_cases("grammar-notes")
        self.assertIn("case-naming", self.ids(self.check()))
        self.declare({"style": "custom", "pattern": r"[a-z0-9-]+"})
        self.assertEqual(self.check()["findings"], [])

    def test_a_broken_declaration_is_reported_without_a_fix(self) -> None:
        self.init("--no-entry")
        for naming in ({"style": "weekly"}, {"style": "custom"}, {"pattern": "("}, "date-topic"):
            with self.subTest(naming=naming):
                self.declare(naming)
                found = {item["id"]: item for item in self.check()["findings"]}
                self.assertIsNone(found["profile:work.case.directory_naming"]["fix"])

    def test_directories_without_a_case_file_and_hidden_ones_are_not_cases(self) -> None:
        self.init("--no-entry")
        (self.root / "cases/archive").mkdir(parents=True, exist_ok=True)
        (self.root / "cases/_templates").mkdir()
        self.assertEqual(self.check()["findings"], [])

    def test_plan_lists_renames_and_references_and_moves_nothing(self) -> None:
        self.init("--no-entry")
        self.make_cases("20260902-grammar", "weekly-review")
        (self.root / "docs/refs/overview.md").write_text("see cases/20260902-grammar/CASE.md\n")
        before = snapshot(self.root)
        code, plan = self.run_mode("case-naming")
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.root), before)
        renames = {item["from"]: item for item in plan["renames"]}
        grammar = renames["cases/20260902-grammar"]
        self.assertEqual(grammar["to"], "cases/CASE-20260902-001-grammar")
        self.assertIn("docs/refs/overview.md", grammar["references"])
        self.assertIsNone(renames["cases/weekly-review"]["to"])
        self.assertFalse(plan["moves_directories"])
        self.assertTrue((self.root / "cases/20260902-grammar").is_dir())

    def test_the_schema_declares_the_naming_field(self) -> None:
        schema = json.loads((SCRIPT.parent.parent / "project.schema.json").read_text())
        case = schema["properties"]["work"]["properties"]["case"]["properties"]
        self.assertIn("directory_naming", case)
        self.assertIn("command", case)
        runtime = schema["properties"]["runtime"]["properties"]
        for field in ("source_ref", "plugin_version", "source_commit"):
            self.assertEqual(runtime[field]["x-agenthelm-scope"], "audit")


class FingerprintTests(ScaffoldCase):
    def copy_of_project(self) -> Path:
        import shutil
        other = Path(self._temporary.name) / "synced-copy"
        shutil.copytree(self.root, other)
        return other

    def print_of(self, root: Path, state: Path | None = None) -> dict:
        return scaffold.fingerprint(scaffold.check(root, state_dir=state or self.state))

    def test_same_scaffold_gives_the_same_fingerprint_on_two_machines(self) -> None:
        self.init("--no-entry")
        other = self.copy_of_project()
        first = self.print_of(self.root)
        second = self.print_of(other, Path(self._temporary.name) / "other-state")
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(scaffold.explain(first["detail"], second["detail"]), [])

    def test_a_missing_skeleton_file_changes_it_and_explain_names_the_file(self) -> None:
        self.init("--no-entry")
        other = self.copy_of_project()
        (other / ".agents/mcp.json").unlink()
        first, second = self.print_of(self.root), self.print_of(other)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        lines = scaffold.explain(first["detail"], second["detail"])
        self.assertEqual(len(lines), 1)
        self.assertIn("skeleton:.agents/mcp.json", lines[0])
        self.assertIn("ok", lines[0])
        self.assertIn("missing", lines[0])

    def test_opinion_own_fields_audit_fields_and_skipped_items_do_not_count(self) -> None:
        self.old_project()
        base = self.print_of(self.root)["fingerprint"]
        profile = json.loads((self.root / PROFILE).read_text())
        profile["opinion"]["strict_mode"] = "strict"
        profile["runtime"]["source_ref"] = "v0.0.1"
        profile["my_section"] = {"x": 1}
        (self.root / PROFILE).write_text(json.dumps(profile))
        (self.root / "OPINION.md").write_text("different opinion\n")
        (self.root / "notes.md").write_text("an extra file\n")
        self.assertEqual(self.print_of(self.root)["fingerprint"], base)
        scaffold.decide(self.root, "skel:docs/drafts/.gitkeep", "skip", apply=True, state_dir=self.state)
        skipped = self.print_of(self.root)
        self.assertNotEqual(skipped["fingerprint"], base)
        self.assertNotIn("skeleton:docs/drafts/.gitkeep", skipped["detail"])
        scaffold.add_skeleton(self.root, ["docs/drafts/.gitkeep"], apply=True, state_dir=self.state)
        self.assertEqual(self.print_of(self.root)["detail"].get("skeleton:docs/drafts/.gitkeep"), "ok")  # once it exists the skip no longer matters

    def test_machines_expecting_different_manifest_versions_are_told_so(self) -> None:
        mine = {"expected:agents-init/project-skeleton": "1.1.0", "skeleton:AGENTS.md": "ok"}
        theirs = {"expected:agents-init/project-skeleton": "1.0.0", "skeleton:AGENTS.md": "ok"}
        (line,) = scaffold.explain(mine, theirs)
        self.assertIn("期望清单版本不同", line)

    def test_cli_check_reports_a_fingerprint(self) -> None:
        self.init("--no-entry")
        code, report = self.run_mode("check")
        self.assertEqual(code, 0)
        self.assertTrue(report["fingerprint"]["fingerprint"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
