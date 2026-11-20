"""check 对已有 opinion 的项目给出迁移提示（advisory）：只在显式 check 里运行只读 `migrate` 预览，其余情况静默。

opinion-manager 只经子进程调用（桩脚本或仓库里真实的那份），HOME 是临时目录；夹具先复制到临时目录，从不原地写入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agenthelm  # noqa: E402
import test_agenthelm_reporting as base  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[2] / "opinion-manager" / "tests" / "fixtures" / "migrate-v6.13.1"
CJK = re.compile(r"[　-〿一-鿿＀-￯]")
PREVIEW = {
    "ok": True, "status": "preview", "reason": None, "message": None,
    "target": {"profile": "private.my-opinion@1.0.1", "schema_version": "2.2", "path": "/x/1.0.1.json"},
    "updates": [{"kind": "templates", "id": "scenario/reviewed-project", "current": "a@1.0.0", "target": "a@1.1.0",
                 "action": "upgrade", "extra": "dropped"}],
    "loading": {"before": None, "after": {"kind": "tags"}}, "custom": {"lines": []},
    "inbox": {"readable": True, "incomplete": ["i1", "i2"], "sha256": None},
    "confirmation_sha256": "0" * 64, "next": ["APPLY-COMMAND-MUST-NOT-LEAK"],
}
STUB = """import json, pathlib, sys, time
here = pathlib.Path(__file__).parent
with open(here / "calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
cfg = json.loads((here / "cfg.json").read_text())
if sys.argv[1] == "updates":
    print(json.dumps({"ok": True, "updates": []}))
    sys.exit(0)
time.sleep(cfg.get("sleep", 0))
sys.stdout.write(cfg["stdout"])
sys.exit(cfg.get("code", 0))
"""


def digest(root: Path) -> str:
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result.update(str(path.relative_to(root)).encode() + path.read_bytes())
    return result.hexdigest()


class Fixture(base.Base):
    lang = "zh"
    root_name = "project"

    def setUp(self) -> None:
        super().setUp()
        os.environ["AGENTHELM_LANG"] = self.lang
        self.project = self.root / self.root_name
        base.write(self.project / "opinion.lock.json", {"profile": {}})

    def stub_manager(self, payload: dict | str, *, code: int = 0, sleep: float = 0, name: str = "opinion-manager") -> Path:
        manager = self.root / name
        base.write(manager / "plugin.json", {"name": "opinion-manager", "version": "9.9.9"})
        (manager / "runtime").mkdir(parents=True, exist_ok=True)
        (manager / "runtime" / "opinion_manager.py").write_text(STUB, encoding="utf-8")
        (manager / "runtime" / "cfg.json").write_text(json.dumps(
            {"stdout": payload if isinstance(payload, str) else json.dumps(payload), "code": code, "sleep": sleep}), encoding="utf-8")
        patch = mock.patch.object(agenthelm, "locate_companion", return_value=manager)
        patch.start()
        self.addCleanup(patch.stop)
        return manager

    def migrate_calls(self, manager: Path) -> list[str]:
        log = manager / "runtime" / "calls.log"
        return [line for line in log.read_text().splitlines() if line.startswith("migrate")] if log.exists() else []

    def check_text(self, *extra: str) -> tuple[int, str]:
        return self.run_text("check", "--project", str(self.project), *extra)

    def check_json(self, *extra: str) -> tuple[int, dict]:
        return self.run_json("check", "--project", str(self.project), *extra)

    def baseline(self, *extra: str, fmt: str = "text") -> tuple[int, str]:
        with mock.patch.object(agenthelm, "opinion_migrate_findings", return_value=[]):
            code, out, err = self.run_cli("check", "--project", str(self.project), *extra, fmt=fmt)
        return code, out + err


class PreviewTests(Fixture):
    def test_preview_shows_hint_with_summary_and_copyable_command(self) -> None:
        manager = self.stub_manager(PREVIEW)
        code, text = self.check_text()
        self.assertEqual(code, 0)
        self.assertIn("[Opinion·可迁移]", text)
        self.assertIn("private.my-opinion@1.0.1", text)
        self.assertIn("scenario/reviewed-project", text)
        self.assertIn("2 条收件箱记录未完成", text)
        self.assertIn("不迁移则保持原样，不影响现有行为", text)
        self.assertNotIn("APPLY-COMMAND-MUST-NOT-LEAK", text)
        self.assertNotIn("--apply", text)
        command = re.search(r"查看迁移预览（只读）：(.+)", text).group(1)
        self.assertEqual(shlex.split(command), ["python3", str(manager / "runtime" / "opinion_manager.py"),
                                                "migrate", "--project", str(self.project)])
        self.assertEqual(len(self.migrate_calls(manager)), 1)
        self.assertEqual(self.migrate_calls(manager)[0], f"migrate --project {self.project} --output json")

    def test_hint_does_not_change_exit_code_or_strict(self) -> None:
        self.stub_manager(PREVIEW)
        code_strict, _text = self.check_text("--strict")
        baseline_strict, _ = self.baseline("--strict")
        self.assertEqual(code_strict, baseline_strict)

    def test_json_field(self) -> None:
        self.stub_manager(PREVIEW)
        code, payload = self.check_json()
        self.assertEqual(code, 0)
        finding, = payload["opinion_migrate"]
        self.assertEqual(finding["kind"], "opinion-migrate")
        self.assertEqual(finding["status"], "preview")
        self.assertEqual(finding["target"], {"profile": "private.my-opinion@1.0.1", "schema_version": "2.2"})
        self.assertEqual(finding["updates"], [{"kind": "templates", "id": "scenario/reviewed-project",
                                               "current": "a@1.0.0", "target": "a@1.1.0", "action": "upgrade"}])
        self.assertEqual(finding["inbox_incomplete"], 2)
        self.assertIn("migrate --project", finding["command"])
        self.assertNotIn("--apply", finding["command"])
        _code, baseline = self.baseline(fmt="json")
        self.assertEqual({key: value for key, value in payload.items() if key != "opinion_migrate"}, json.loads(baseline))
        self.assertEqual(payload["summary"], json.loads(baseline)["summary"])

    def test_offline_and_client_do_not_matter(self) -> None:
        manager = self.stub_manager(PREVIEW)
        _code, text = self.check_text("--offline", "--client", "claude")
        self.assertIn("[Opinion·可迁移]", text)
        self.assertEqual(len(self.migrate_calls(manager)), 1)

    def test_path_with_spaces_quotes_and_long_length(self) -> None:
        self.root_name = "long " * 30 + "it's \"q\" $HOME"
        self.project = self.root / self.root_name
        base.write(self.project / "opinion.lock.json", {"profile": {}})
        self.assertGreaterEqual(len(str(self.project)), 138)
        manager = self.stub_manager(PREVIEW, name="plugins dir it's")
        _code, payload = self.check_json()
        command = payload["opinion_migrate"][0]["command"]
        self.assertEqual(shlex.split(command), ["python3", str(manager / "runtime" / "opinion_manager.py"),
                                                "migrate", "--project", str(self.project)])
        if shutil.which("bash"):  # the printed command really runs in a shell
            done = subprocess.run(["bash", "-c", command.replace("python3", shlex.quote(sys.executable), 1)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(json.loads(done.stdout)["status"], "preview")


class SilentTests(Fixture):
    def assert_silent(self, **stub) -> None:
        self.stub_manager(**({"payload": ""} | stub))
        for fmt, extra in (("text", ()), ("json", ())):
            code, out, err = self.run_cli("check", "--project", str(self.project), *extra, fmt=fmt)
            base_code, base_out = self.baseline(*extra, fmt=fmt)
            self.assertEqual((code, out + err), (base_code, base_out))
            self.assertNotIn("opinion_migrate", out)
            self.assertNotIn("可迁移", out)

    def test_unchanged_and_nothing_to_migrate(self) -> None:
        for status, reason in (("unchanged", "bundles-kept"), ("nothing-to-migrate", "blank"), ("migrated", None),
                               ("rolled-back", None)):
            with self.subTest(status=status):
                self.assert_silent(payload={"ok": True, "status": status, "reason": reason})

    def test_unknown_status_and_other_reasons(self) -> None:
        self.assert_silent(payload={"ok": True, "status": "something-new"})
        for reason in ("no-project", "no-lock", "lock-invalid", "opinion-missing", "opinion-unreadable", "draft-failed",
                       "tag-conflict", "compose-failed"):
            with self.subTest(reason=reason):
                self.assert_silent(payload={"ok": False, "status": "cannot-migrate", "reason": reason, "message": "m"}, code=3)
        self.assert_silent(payload={"ok": False, "status": "refused"}, code=4)
        self.assert_silent(payload={"ok": False, "status": "failed"}, code=5)

    def test_non_json_failure_and_non_object(self) -> None:
        self.assert_silent(payload="not json at all")
        self.assert_silent(payload="[1, 2]")
        self.assert_silent(payload="", code=2)

    def test_preview_with_nonzero_exit_is_not_trusted(self) -> None:
        self.assert_silent(payload=PREVIEW, code=2)

    def test_timeout_is_silent_and_bounded(self) -> None:
        manager = self.stub_manager(PREVIEW, sleep=3)
        with mock.patch.object(agenthelm, "OPINION_MIGRATE_TIMEOUT", 0.5):
            started = __import__("time").monotonic()
            code, text = self.check_text()
            elapsed = __import__("time").monotonic() - started
        self.assertEqual(code, 0)
        self.assertNotIn("可迁移", text)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(self.migrate_calls(manager)), 1)

    def test_no_lock_means_no_call(self) -> None:
        manager = self.stub_manager(PREVIEW)
        (self.project / "opinion.lock.json").unlink()
        _code, text = self.check_text()
        self.assertNotIn("可迁移", text)
        self.assertEqual(self.migrate_calls(manager), [])

    def test_no_opinion_manager_installed(self) -> None:
        with mock.patch.object(agenthelm, "locate_companion", return_value=None):
            code, out, err = self.run_cli("check", "--project", str(self.project), fmt="text")
            self.assertEqual(code, 0)
            self.assertNotIn("可迁移", out + err)
            self.assertEqual(agenthelm.opinion_migrate_findings(self.project, []), [])

    def test_no_project_means_no_call(self) -> None:
        manager = self.stub_manager(PREVIEW)
        self.run_text("check", "--no-project")
        self.assertEqual(self.migrate_calls(manager), [])


class ConflictTests(Fixture):
    def test_manual_edit_and_catalog_conflict(self) -> None:
        for reason, words in (("manual-edit-conflict", "手工改动与迁移冲突"), ("catalog-conflict", "公开 catalog 与 Profile 记录的来源不一致")):
            with self.subTest(reason=reason):
                manager = self.stub_manager({"ok": False, "status": "cannot-migrate", "reason": reason, "message": "RAW 原文",
                                             "next": ["N0"]}, code=3)
                code, text = self.check_text()
                self.assertEqual(code, 0)
                self.assertIn(words, text)
                self.assertIn("migrate --project", text)
                _c, payload = self.check_json()
                finding, = payload["opinion_migrate"]
                self.assertEqual((finding["kind"], finding["status"], finding["reason"], finding["message"]),
                                 ("opinion-migrate", "cannot-migrate", reason, "RAW 原文"))
                self.assertEqual(payload["summary"]["up_to_date"], self.baseline(fmt="json") and json.loads(self.baseline(fmt="json")[1])["summary"]["up_to_date"])
                self.assertGreaterEqual(len(self.migrate_calls(manager)), 2)


class HookTests(Fixture):
    def test_hook_never_starts_migrate(self) -> None:
        manager = self.stub_manager(PREVIEW)
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = agenthelm.main(["check", "--hook", "--project", str(self.project), "--home", str(self.home),
                                   "--latest", str(self.latest_file)])
        self.assertEqual(code, 0)
        self.assertEqual(self.migrate_calls(manager), [])
        self.assertNotIn("可迁移", "".join(out))

    def test_run_check_defaults_and_hook_mode_skip_migrate(self) -> None:
        manager = self.stub_manager(PREVIEW)
        env = agenthelm.Env(home=self.home, variables=dict(os.environ))
        for kwargs in ({}, {"migrate_hint": True, "hook_mode": True}):
            agenthelm.run_check(env, self.project, latest_source=str(self.latest_file), offline=True, **kwargs)
        self.assertEqual(self.migrate_calls(manager), [])

    def test_nudge_and_session_start_sources_do_not_mention_migrate(self) -> None:
        runtime = Path(agenthelm.__file__).parent
        self.assertNotIn("migrate", (runtime / "agent_pack_nudge.py").read_text(encoding="utf-8").replace("migrate_", ""))


class EnglishTests(Fixture):
    lang = "en"

    def test_en_output_has_no_chinese(self) -> None:
        for payload, code in ((PREVIEW, 0),
                              ({"ok": False, "status": "cannot-migrate", "reason": "manual-edit-conflict", "message": "中文原文"}, 3),
                              ({"ok": False, "status": "cannot-migrate", "reason": "catalog-conflict", "message": "中文原文"}, 3)):
            with self.subTest(status=payload["status"], reason=payload.get("reason")):
                self.stub_manager(payload, code=code)
                _code, text = self.check_text()
                self.assertIn("[Opinion migration]", text)
                self.assertIsNone(CJK.search(text), text)
        self.stub_manager(PREVIEW)
        _code, text = self.check_text()
        self.assertIn("nothing changes and current behaviour is unaffected", text)
        self.assertIn("unfinished inbox records: 2", text)


class RealFixtureTests(Fixture):
    """The repository's real opinion-manager against copies of the v6.13.1 data (no stub, no mock)."""

    def copy(self, name: str) -> Path:
        shutil.copytree(FIXTURES / name, self.project, dirs_exist_ok=True)
        return self.project

    def test_real_preview_and_silent_fixtures(self) -> None:
        shutil.rmtree(self.project)
        for name, shown in (("reviewed-20", True), ("adopted-20", True), ("edited-20", True), ("blank", False),
                            ("bundles-21", False), ("atomic-20", False)):
            with self.subTest(fixture=name):
                if self.project.exists():
                    shutil.rmtree(self.project)
                self.copy(name)
                before = digest(self.project)
                code, payload = self.check_json()
                self.assertEqual(code, 0)
                self.assertEqual("opinion_migrate" in payload, shown, payload.get("opinion_migrate"))
                if shown:
                    self.assertEqual(payload["opinion_migrate"][0]["status"], "preview")
                    runtime = shlex.split(payload["opinion_migrate"][0]["command"])[1]
                    self.assertTrue(Path(runtime).is_file())
                self.assertEqual(digest(self.project), before, "check must not write to the project")


if __name__ == "__main__":
    unittest.main()
