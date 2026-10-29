from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import assets  # noqa: E402

ALPHA = "CASE-20261001-001-alpha"
BETA = "CASE-20261002-001-beta"


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def case_record(root: Path, case_id: str, title: str, *, case_root: str = "cases", sensitivity: str = "shared",
                deliverables: tuple = (), evidence: tuple = (), legacy_ids: tuple = (), files: dict | None = None) -> Path:
    lines = ["---", 'schema_version: "1.0"', f'id: "{case_id}"', f'title: "{title}"', 'status: "active"',
             f'sensitivity: "{sensitivity}"', 'goal: "交付季度材料"', 'next_action: "整理"']
    for key, values in (("deliverables", deliverables), ("evidence", evidence), ("legacy_ids", legacy_ids)):
        lines.append(f"{key}:" if values else f"{key}: []")
        lines.extend(f'  - "{value}"' for value in values)
    lines += ['updated_at: "2026-10-14T10:00:00+08:00"', "---", "", "# 摘要", ""]
    directory = root / case_root / case_id
    write(directory / "CASE.md", "\n".join(lines))
    for relative, content in (files or {}).items():
        write(directory / relative, content)
    return directory


class AssetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.cache = base / "cache"
        self.root = base / "project"
        self.root.mkdir()
        environment = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        environment.start()
        self.addCleanup(environment.stop)

    def profile(self, work: dict | None) -> None:
        payload = {"schema_version": "1.0"} if work is None else {"schema_version": "1.0", "work": work}
        write(self.root / assets.PROFILE, json.dumps(payload))

    def project(self) -> assets.Project:
        return assets.Project(self.root)

    def run_cli(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = assets.main(list(args))
        return code, json.loads(output.getvalue())


class AssetRegistryTests(AssetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile({"mode": "case-workspace", "case": {"root": "cases"}, "assets": {"roots": ["projects"]}})
        write(self.root / "projects/area/plan-v1.xlsx", b"x" * 20)
        write(self.root / "projects/area/README.md", f"见 `cases/{ALPHA}/docs/report.md`\n")
        case_record(self.root, ALPHA, "阿尔法专项", deliverables=("docs/report.md",),
                    evidence=("projects/area/plan-v1.xlsx",), legacy_ids=("20260901-alpha-task",),
                    files={"docs/report.md": "# 季度报告\n正文含有独特词汇 zebra-marker\n",
                           "docs/drafts/draft.md": "草稿\n", "refs/source.pdf": "pdf", "docs/orphan.md": "无人引用\n"})
        case_record(self.root, BETA, "贝塔专项",
                    files={"docs/notes.md": f"引用 [报告](../../{ALPHA}/docs/report.md)\n"
                                            "旧任务 tasks/20260901-alpha-task\n"})

    def test_find_by_name_title_case_metadata_and_content(self) -> None:
        project = self.project()
        assets.rebuild(project)
        by_name = assets.find(project, ["report"])
        self.assertEqual(by_name["results"][0]["path"], f"cases/{ALPHA}/docs/report.md")
        self.assertEqual(by_name["results"][0]["title"], "季度报告")
        self.assertEqual(assets.find(project, ["季度报告"])["results"][0]["name"], "report.md")
        by_case = assets.find(project, ["阿尔法"])
        self.assertIn("draft.md", {item["name"] for item in by_case["results"]})
        self.assertEqual(assets.find(project, ["zebra-marker"])["total"], 0)
        by_content = assets.find(project, ["zebra-marker"], content=True)
        self.assertEqual([item["name"] for item in by_content["results"]], ["report.md"])
        only_xlsx = assets.find(project, ["plan"], ext="xlsx")["results"][0]
        self.assertEqual((only_xlsx["container"], only_xlsx["container_kind"]), ("projects/area", "project"))
        self.assertEqual(assets.find(project, [], kind="draft")["results"][0]["name"], "draft.md")

    def test_refs_inbound_outbound_and_affected_containers(self) -> None:
        project = self.project()
        result = assets.refs(project, f"cases/{ALPHA}/docs/report.md")
        sources = {item["src"] for item in result["inbound"]}
        self.assertIn(f"cases/{ALPHA}/CASE.md", sources)
        self.assertIn(f"cases/{BETA}/docs/notes.md", sources)
        self.assertIn("projects/area/README.md", sources)
        self.assertEqual(result["affected_containers"], [BETA, "projects/area"])
        evidence = assets.refs(project, "projects/area/plan-v1.xlsx")
        self.assertIn(("cases/" + ALPHA + "/CASE.md", "declared:evidence"),
                      {(item["src"], item["type"]) for item in evidence["inbound"]})
        record = assets.refs(project, f"cases/{ALPHA}/CASE.md")
        self.assertIn("legacy-id", {item["type"] for item in record["inbound"]})
        with self.assertRaises(assets.AssetError) as raised:
            assets.refs(project, "cases/nowhere.md")
        self.assertEqual(raised.exception.code, "unknown-path")

    def test_check_reports_unclaimed_and_broken_links(self) -> None:
        write(self.root / f"cases/{BETA}/docs/bad-link.md", "[缺失](./missing-file.md)\n")
        report = assets.check(self.project())
        unclaimed = [path for paths in report["unclaimed"].values() for path in paths]
        self.assertIn(f"cases/{ALPHA}/docs/orphan.md", unclaimed)
        self.assertNotIn(f"cases/{ALPHA}/docs/report.md", unclaimed)
        self.assertTrue(any(item["raw"] == "./missing-file.md" for item in report["broken"]["real-broken"]))

    def test_version_family_groups_and_ranks_versions(self) -> None:
        docs = self.root / f"cases/{BETA}/docs"
        for name in ("weekly_0901_v1.html", "weekly_0901_v2.html", "weekly_0901_web.html", "weekly_0902_v1.html"):
            write(docs / name, "<html><head><title>Team weekly</title></head></html>")
        result = assets.find(self.project(), ["weekly", "9月1号"])
        self.assertEqual(result["query"]["dates"], ["*-0901"])
        head = result["results"][0]
        self.assertEqual(head["name"], "weekly_0901_v2.html")
        self.assertEqual(head["title"], "Team weekly")
        self.assertEqual({item["name"] for item in head["versions"]}, {"weekly_0901_v1.html", "weekly_0901_web.html"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(assets.find(self.project(), ["0902"])["results"][0]["name"], "weekly_0902_v1.html")
        self.assertEqual(assets.find(self.project(), ["2026-09-01"])["total"], 0)

    def test_synonyms_come_only_from_the_project_file(self) -> None:
        write(self.root / f"cases/{BETA}/docs/weekly_0901.html", "<title>Team weekly</title>")
        project = self.project()
        self.assertEqual(project.synonyms(), [])
        self.assertEqual(assets.find(project, ["周报"])["total"], 0)
        write(self.root / assets.SYNONYMS, json.dumps({"schema_version": "1.0", "groups": [["周报", "weekly"]]}))
        found = assets.find(project, ["团队周报", "9月1号"])
        self.assertEqual(found["total"], 0)  # “团队” is not a synonym and appears nowhere
        found = assets.find(project, ["周报", "9月1号"])
        self.assertEqual(found["results"][0]["name"], "weekly_0901.html")
        write(self.root / assets.SYNONYMS, "{not json")
        with self.assertRaises(assets.AssetError) as raised:
            assets.find(project, ["周报"])
        self.assertEqual(raised.exception.code, "synonyms-invalid")

    def test_external_checkout_and_cache_directories_are_not_registered(self) -> None:
        checkout = self.root / "projects/area/vendor-copy"
        write(checkout / ".svn/wc.db", "x")
        write(checkout / "copied-data.xlsx", b"1")
        write(self.root / f"cases/{ALPHA}/build_cache_v2/copied-cache.json", "{}")
        project = self.project()
        names = {item["name"] for item in assets.find(project, [], limit=500)["results"]}
        self.assertFalse({"copied-cache.json", "wc.db", "copied-data.xlsx"} & names)
        self.assertEqual(assets.find(project, ["copied"])["total"], 0)
        skipped = {(item["reason"], item["path"]) for item in assets.check(project)["skipped"]}
        self.assertIn(("external-checkout", "projects/area/vendor-copy"), skipped)

    def test_credential_files_are_never_registered(self) -> None:
        docs = self.root / f"cases/{ALPHA}/docs"
        for name in ("credentials-list.xlsx", "prod.pem", "token-dump.md", "api_password.csv", ".env.local"):
            write(docs / name, "x")
        project = self.project()
        result = assets.rebuild(project)
        self.assertEqual(result["denied"], 5)
        self.assertEqual(assets.find(project, ["credentials"])["total"], 0)
        self.assertEqual(assets.find(project, [], ext="pem")["total"], 0)
        self.assertEqual(assets.find(project, ["token"])["total"], 0)
        self.assertEqual(assets.check(project)["totals"]["denied"], 5)

    def test_gitignored_files_only_registered_when_business_documents(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        write(self.root / ".gitignore", f"cases/{ALPHA}/data/\n")
        data = self.root / f"cases/{ALPHA}/data"
        write(data / "evidence-table.xlsx", b"x")
        write(data / "dump.json", "{}")
        project = self.project()
        found = assets.find(project, ["evidence-table"])["results"]
        self.assertEqual((found[0]["name"], found[0]["git"]), ("evidence-table.xlsx", "ignored"))
        self.assertEqual(assets.find(project, ["dump"])["total"], 0)
        self.assertEqual(assets.find(project, ["report"])["results"][0]["git"], "untracked")

    def test_git_state_when_the_project_is_a_repository_subdirectory(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        repository = self.root.parent
        git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
        subprocess.run([*git, "init", "-q"], cwd=repository, check=True)
        subprocess.run([*git, "add", "project"], cwd=repository, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "seed"], cwd=repository, check=True)
        write(self.root / f"cases/{ALPHA}/docs/report.md", "# 季度报告\n改过\n")
        result = assets.find(self.project(), ["report"])["results"][0]
        self.assertEqual(result["git"], "tracked+dirty")
        self.assertEqual(assets.find(self.project(), ["orphan"])["results"][0]["git"], "tracked")

    def test_without_git_files_are_marked_none(self) -> None:
        with mock.patch.object(assets, "_git", return_value=None):
            project = self.project()
            assets.rebuild(project)
            self.assertEqual(assets.find(project, ["report"], refresh=False)["results"][0]["git"], "none")
            self.assertEqual(assets.check(project, refresh=False)["untracked_new"], [])

    def test_non_shared_case_is_indexed_and_marked(self) -> None:
        case_record(self.root, "CASE-20261003-001-private", "私密专项", sensitivity="private",
                    files={"docs/private-plan.md": "x", "docs/db-password.txt": "y"})
        result = assets.find(self.project(), ["private-plan"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["sensitivity"], "private")
        self.assertEqual(assets.find(self.project(), ["report"])["results"][0]["sensitivity"], "shared")
        self.assertEqual(assets.find(self.project(), ["db-password"])["total"], 0)
        self.assertIn("CASE-20261003-001-private", [item["id"] for item in assets.cases(self.project())["results"]])

    def test_refresh_status_and_no_refresh(self) -> None:
        project = self.project()
        with self.assertRaises(assets.AssetError) as raised:
            assets.find(project, ["report"], refresh=False)
        self.assertEqual(raised.exception.code, "index-missing")
        self.assertEqual(assets.status(project)["status"], "missing")
        assets.rebuild(project)
        self.assertEqual(assets.status(project)["status"], "fresh")
        write(self.root / f"cases/{ALPHA}/docs/new-unique.md", "n")
        self.assertEqual(assets.status(project)["status"], "stale")
        self.assertEqual(assets.find(project, ["new-unique"], refresh=False)["total"], 0)
        self.assertEqual(assets.find(project, ["new-unique"])["total"], 1)
        self.assertEqual(assets.rebuild(project)["action"], "no-op")
        self.assertEqual(assets.rebuild(project, force=True)["action"], "rebuilt")

    def test_state_lives_in_the_user_cache_and_the_project_is_untouched(self) -> None:
        before = sorted(str(path) for path in self.root.rglob("*"))
        project = self.project()
        result = assets.rebuild(project)
        assets.check(project)
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))
        state = Path(result["state_dir"])
        self.assertEqual(state.parent, self.cache / "agenthelm" / "assets")
        self.assertTrue((state / assets.DB_NAME).is_file())

    def test_cache_inside_the_project_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / ".cache")}):
            with self.assertRaises(assets.AssetError) as raised:
                assets.rebuild(self.project())
        self.assertEqual(raised.exception.code, "state-inside-project")
        self.assertFalse((self.root / ".cache").exists())

    def test_cases_lists_open_cases_with_file_counts(self) -> None:
        result = assets.cases(self.project(), "阿尔法")
        self.assertEqual([(item["id"], item["files"]) for item in result["results"]], [(ALPHA, 5)])

    def test_cli_accepts_project_and_output_on_either_side(self) -> None:
        code, before = self.run_cli("--project", str(self.root), "--output", "json", "find", "report")
        self.assertEqual(code, 0, before)
        self.assertEqual(before["results"][0]["name"], "report.md")
        code, after = self.run_cli("find", "report", "--project", str(self.root), "--output", "json")
        self.assertEqual((code, after["results"][0]["name"]), (0, "report.md"))
        code, error = self.run_cli("refs", "nothing.md", "--project", str(self.root), "--output", "json")
        self.assertEqual((code, error["code"]), (2, "unknown-path"))


class AssetProfileTests(AssetTestCase):
    def test_missing_profile_scans_only_the_default_case_root(self) -> None:
        case_record(self.root, ALPHA, "阿尔法专项", files={"docs/report.md": "# 报告\n"})
        write(self.root / "projects/area/plan.xlsx", b"x")
        project = self.project()
        self.assertFalse(project.profile_found)
        self.assertEqual(project.roots, ["cases"])
        names = {item["name"] for item in assets.find(project, [], limit=50)["results"]}
        self.assertEqual(names, {"CASE.md", "report.md"})

    def test_profile_without_assets_section_uses_the_declared_case_root(self) -> None:
        self.profile({"mode": "case-workspace", "case": {"root": "work/cases"}})
        case_record(self.root, ALPHA, "阿尔法专项", case_root="work/cases", deliverables=("docs/report.md",),
                    files={"docs/report.md": "# 报告\n"})
        write(self.root / "cases/stray.md", "not scanned")
        project = self.project()
        self.assertEqual(project.roots, ["work/cases"])
        result = assets.find(project, ["report"])["results"][0]
        self.assertEqual((result["container"], result["role"]), (ALPHA, "deliverable"))
        self.assertEqual(assets.find(project, ["stray"])["total"], 0)
        inbound = assets.refs(project, f"work/cases/{ALPHA}/docs/report.md")["inbound"]
        self.assertIn("declared:deliverables", {item["type"] for item in inbound})

    def test_invalid_roots_are_rejected(self) -> None:
        for roots in (["/abs"], ["../outside"], "projects", ["cases/inner"]):
            self.profile({"case": {"root": "cases"}, "assets": {"roots": roots}})
            with self.assertRaises(assets.AssetError) as raised:
                self.project()
            self.assertEqual(raised.exception.code, "profile-invalid", roots)

    def test_discover_walks_up_to_the_nearest_profile(self) -> None:
        self.profile(None)
        nested = self.root / "cases" / ALPHA / "docs"
        nested.mkdir(parents=True)
        self.assertEqual(assets.Project.discover(nested, explicit=False).root, self.root.resolve())
        self.assertEqual(assets.Project.discover(nested, explicit=True).root, nested.resolve())


class PlatformTests(unittest.TestCase):
    def test_dataless_flag_is_only_honoured_on_macos(self) -> None:
        stat = SimpleNamespace(st_flags=assets.SF_DATALESS)
        with mock.patch.object(assets.sys, "platform", "darwin"):
            self.assertTrue(assets._is_dataless(stat))
        with mock.patch.object(assets.sys, "platform", "linux"):
            self.assertFalse(assets._is_dataless(stat))
        with mock.patch.object(assets.sys, "platform", "darwin"):
            self.assertFalse(assets._is_dataless(SimpleNamespace()))

    def test_dates_and_families(self) -> None:
        self.assertEqual(assets.extract_dates("report_20260901.md"), "0901 20260901")
        self.assertEqual(assets.extract_dates("9月1号 周报"), "0901")
        self.assertEqual(assets.family_key("c", "plan_v2_终稿.docx"), assets.family_key("c", "plan-v1.docx"))
        self.assertGreater(assets.version_rank("plan-v10.docx"), assets.version_rank("plan-v9.docx"))


if __name__ == "__main__":
    unittest.main()
