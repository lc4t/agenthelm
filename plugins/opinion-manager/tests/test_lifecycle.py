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
import lifecycle  # noqa: E402
import versioned  # noqa: E402


class LifecycleReducerTests(unittest.TestCase):
    """合成样例只验证状态机，不代表真实使用或成熟度证据。"""

    def setUp(self) -> None:
        self.content = {"title": "TITLE_FIXTURE", "text": "TEXT_FIXTURE", "scopes": ["SCOPE_FIXTURE"]}
        self.state = lifecycle.reduce(lifecycle.EMPTY_STATE, {"kind": "capture", "id": "fixture", "content": self.content})
        rule = {"id": "fixture", "version": "1.0.0", "slot": "fixture", "title": "T", "level": "required", "scopes": ["S"], "text": "X"}
        self.lock = {"profile": {"id": "fixture", "version": "1.0.0", "overrides": {},
                                 "sources": {"rules": [{"ref": "fixture@1.0.0", "value": rule}], "templates": []}},
                     "profile_sha256": "profile", "opinion_sha256": "opinion"}

    def observe(self, path: str, *, fixture: bool = False, validation: str = "passed") -> None:
        self.state = lifecycle.reduce(self.state, {"kind": "observe", "id": "fixture", "occurrence": path, "evidence": path,
                                                   "evidence_sha256": "snapshot", "fixture": fixture, "validation": validation})

    def adopt(self) -> None:
        self.state = lifecycle.reduce(self.state, {"kind": "adopt", "human_confirmed": True, "profile_sha256": "profile",
                                                   "evidence": "approval"}, self.lock)

    def view(self) -> dict:
        return lifecycle.candidate_view(self.state["candidates"]["fixture"])

    def test_task_phases_deduplicate_and_fixtures_do_not_mature(self) -> None:
        self.observe("cases/one/analysis.md")
        self.observe("cases/one/report.md")
        self.observe("cases/two/report.md", fixture=True)
        self.assertEqual(self.view()["distinct_occurrences"], 1)

    def test_frequency_raises_recommendation_and_presentation_consumes_it(self) -> None:
        self.observe("tasks/one/result.md")
        self.assertFalse(self.view()["recommendation_due"])
        self.observe("tasks/two/result.md")
        self.assertTrue(self.view()["recommendation_due"])
        self.state = lifecycle.reduce(self.state, {"kind": "presented", "id": "fixture", "content_sha256": versioned.digest(self.content)})
        self.assertFalse(self.view()["recommendation_due"])

    def test_counterevidence_blocks_readiness(self) -> None:
        self.observe("tasks/one/result.md", validation="failed")
        self.assertEqual(self.view()["status"], "needs-resolution")

    def test_occurrence_roots_are_configurable(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            self.observe("notes/one/result.md")
        event = {"kind": "observe", "id": "fixture", "occurrence": "notes/one/result.md", "evidence": "e",
                 "evidence_sha256": "s", "validation": "passed"}
        state = lifecycle.reduce(self.state, event, roots=("notes",))
        self.assertEqual(lifecycle.candidate_view(state["candidates"]["fixture"])["distinct_occurrences"], 1)

    def test_decision_checks_exact_content_and_never_publishes(self) -> None:
        event = {"kind": "decision", "id": "fixture", "decision": "accepted", "human_confirmed": True,
                 "content_sha256": "wrong", "evidence": "approval"}
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.reduce(self.state, event)
        accepted = lifecycle.reduce(self.state, {**event, "content_sha256": versioned.digest(self.content)})
        self.assertEqual(accepted["candidates"]["fixture"]["decision"], "accepted")
        self.assertEqual(accepted["rollouts"], {})
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.reduce(self.state, {**event, "content_sha256": versioned.digest(self.content), "human_confirmed": False})

    def test_candidate_content_is_immutable_and_replay_is_idempotent(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.reduce(self.state, {"kind": "capture", "id": "fixture", "content": {**self.content, "text": "CHANGED"}})
        self.assertEqual(lifecycle.reduce(self.state, {"kind": "capture", "id": "fixture", "content": self.content}), self.state)

    def test_adoption_needs_confirmation_and_failure_blocks_formalization(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.reduce(self.state, {"kind": "adopt", "human_confirmed": False, "profile_sha256": "profile"}, self.lock)
        self.adopt()
        self.assertEqual(self.state["rollouts"]["fixture@1.0.0"]["state"], "gray")
        use = {"kind": "use", "occurrence": "tasks/one/result.md", "evidence": "proof", "evidence_sha256": "snapshot",
               "rules": ["fixture@1.0.0"], "passed": False, "user_changed": False}
        self.state = lifecycle.reduce(self.state, use, self.lock)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.reduce(self.state, {"kind": "formalize", "human_confirmed": True, "profile_sha256": "profile", "evidence": "review"}, self.lock)

    def test_use_validates_results_and_rejects_bundles_without_loading(self) -> None:
        self.adopt()
        base = {"kind": "use", "occurrence": "tasks/one/result.md", "evidence": "proof", "evidence_sha256": "snapshot",
                "rules": ["fixture@1.0.0"], "passed": True, "user_changed": False}
        for extra in ({"results": {"fixture@1.0.0": "violated"}}, {"results": {"fixture@1.0.0": "unknown"}}, {"results": {}},
                      {"loaded_bundles": []}, {"routing_miss": []}, {"date": "2026/01/01"}, {"rules": ["missing@1.0.0"]}):
            with self.subTest(extra=extra), self.assertRaises(lifecycle.LifecycleError):
                lifecycle.reduce(self.state, {**base, **extra}, self.lock)
        state = lifecycle.reduce(self.state, {**base, "results": {"fixture@1.0.0": "passed"}, "date": "2026-01-01"}, self.lock)
        self.assertEqual(state["rollouts"]["fixture@1.0.0"]["blockers"], [])


class LifecycleCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.project = self.root / "project"
        (self.project / "tasks" / "one").mkdir(parents=True)
        (self.project / "tasks" / "two").mkdir(parents=True)
        self.sequence = 0

    def command(self, *arguments: object, code: int = 0) -> dict:
        result = subprocess.run([sys.executable, str(SCRIPT), *(str(item) for item in arguments), "--output", "json"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def confirmed(self, *arguments: object) -> dict:
        preview = self.command(*arguments)
        return self.command(*arguments, "--apply", "--confirm", preview["confirmation_sha256"])

    def file(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def lifecycle(self, action: str, *arguments: object, code: int = 0) -> dict:
        return self.command("lifecycle", action, "--project", self.project, "--store", ".state", *arguments, code=code)

    def event(self, value: dict, *, code: int = 0) -> dict:
        self.sequence += 1
        evidence = self.project / f"evidence-{self.sequence}.md"
        evidence.write_text(f"EVIDENCE_{self.sequence}", encoding="utf-8")
        path = self.project / f"event-{self.sequence}.json"
        path.write_text(json.dumps({**value, "evidence": evidence.name}), encoding="utf-8")
        preview = self.lifecycle("event", "--file", path, code=code)
        if code:
            return preview
        return self.lifecycle("event", "--file", path, "--apply", "--confirm", preview["confirmation_sha256"])

    def seed(self) -> None:
        for identity in ("alpha", "beta"):
            self.confirmed("publish", "--catalog", self.catalog, "--file", self.file("release.json", {
                "id": f"fixture.{identity}", "version": "1.0.0", "slot": f"fixture.{identity}", "title": f"TITLE_{identity}",
                "text": f"RULE_{identity}", "level": "required", "scopes": ["SCOPE_FIXTURE"]}))
        for family, rule in (("base", "alpha"), ("scene", "beta")):
            self.confirmed("publish", "--catalog", self.catalog, "--file", self.file("release.json", {
                "family": family, "variant": "default", "version": "1.0.0", "name": f"NAME_{family}", "description": "D",
                "scopes": ["SCOPE_FIXTURE"], "rules": [f"fixture.{rule}@1.0.0"]}))

    def adopt(self, release: str, *extra: object) -> dict:
        loading = {"core": ["base"], "signals": {"business": ["scene", "none"]},
                   "bundles": [{"family": "scene", "trigger": "TRIGGER", "exclude": "EXCLUDE", "routes": [{"business": ["scene"]}]}]}
        arguments = ["profile", "--catalog", self.catalog, "--project", self.project, "--id", "private.fixture", "--version", release, *extra]
        if not extra:
            arguments += ["--template", "base/default@1.0.0", "--template", "scene/default@1.0.0",
                          "--loading-file", self.file("loading.json", loading)]
        saved = self.confirmed(*arguments)
        self.confirmed("compose", "--project", self.project, "--profile", saved["path"])
        lock = json.loads((self.project / "opinion.lock.json").read_text(encoding="utf-8"))
        self.event({"kind": "adopt", "human_confirmed": True, "profile_sha256": lock["profile_sha256"]})
        return saved

    def use(self, occurrence: str, rules: list[str], **extra: object) -> dict:
        return self.event({"kind": "use", "occurrence": occurrence, "rules": rules, "passed": True, "user_changed": False, **extra})

    def test_preview_writes_nothing_and_snapshots_are_immutable(self) -> None:
        evidence = self.project / "evidence.md"
        evidence.write_text("EVIDENCE", encoding="utf-8")
        path = self.project / "event.json"
        path.write_text(json.dumps({"kind": "capture", "id": "fixture", "evidence": "evidence.md",
                                    "content": {"title": "TITLE", "text": "TEXT", "scopes": ["SCOPE"]}}), encoding="utf-8")
        preview = self.lifecycle("event", "--file", path)
        self.assertFalse((self.project / ".state").exists())
        self.lifecycle("event", "--file", path, "--apply", "--confirm", "wrong", code=2)
        self.assertFalse((self.project / ".state").exists())
        applied = self.lifecycle("event", "--file", path, "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertFalse(applied["writes_stable_rules"])
        self.assertFalse((self.project / "OPINION.md").exists())
        evidence.write_text("LATER_APPEND", encoding="utf-8")
        titles = self.lifecycle("queue", "--titles")["value"]
        self.assertEqual(titles, [{"id": "fixture", "title": "TITLE", "scopes": ["SCOPE"], "status": "waiting-verification", "distinct_occurrences": 0}])
        (self.project / applied["event"]["evidence"]).write_text("TAMPERED", encoding="utf-8")
        self.lifecycle("queue", code=2)

    def test_paths_outside_the_project_are_rejected(self) -> None:
        self.command("lifecycle", "queue", "--project", self.project, "--store", "../escaped", code=2)
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(self.command("lifecycle", "queue", "--project", other, "--store", ".state")["value"], [])
        self.assertFalse((other / ".state").exists())

    def test_use_records_bundles_and_routing_miss_blocks_review(self) -> None:
        self.seed()
        self.adopt("1.0.0")
        self.use("tasks/one/result.md", ["fixture.alpha@1.0.0"], results={"fixture.alpha@1.0.0": "passed"}, date="2026-01-02")
        status = self.lifecycle("status")
        coverage = status["coverage"]
        self.assertEqual(coverage["unused_rules"], ["fixture.beta@1.0.0"])
        self.assertEqual(coverage["unused_bundles"], ["scene"])
        self.assertEqual(coverage["rules"][0]["last_used"], "2026-01-02")
        self.use("tasks/two/result.md", ["fixture.alpha@1.0.0", "fixture.beta@1.0.0"], loaded_bundles=["scene"])
        status = self.lifecycle("status")
        self.assertTrue(status["value"]["private.fixture@1.0.0"]["ready_for_review"])
        self.assertEqual(status["coverage"]["bundles"], [{"family": "scene", "loaded_in": 1, "exercised_in": 1, "routing_misses": 0}])
        self.event({"kind": "use", "occurrence": "tasks/one/again.md", "rules": ["fixture.alpha@1.0.0"], "passed": True,
                    "user_changed": False, "loaded_bundles": ["missing"]}, code=2)
        self.use("tasks/one/later.md", ["fixture.alpha@1.0.0"], routing_miss=["scene"])
        status = self.lifecycle("status")
        rollout = status["value"]["private.fixture@1.0.0"]
        self.assertEqual(len(rollout["blockers"]), 1)
        self.assertFalse(rollout["ready_for_review"])
        self.assertEqual(status["coverage"]["bundles"][0]["routing_misses"], 1)
        lock = json.loads((self.project / "opinion.lock.json").read_text(encoding="utf-8"))
        self.event({"kind": "formalize", "human_confirmed": True, "profile_sha256": lock["profile_sha256"]}, code=2)

    def test_unchanged_rules_carry_evidence_into_a_new_profile_version(self) -> None:
        self.seed()
        first = self.adopt("1.0.0")
        self.use("tasks/one/result.md", ["fixture.alpha@1.0.0"])
        self.use("tasks/two/result.md", ["fixture.alpha@1.0.0", "fixture.beta@1.0.0"], loaded_bundles=["scene"])
        custom = self.root / "custom.md"
        custom.write_text("CUSTOM_FIXTURE", encoding="utf-8")
        second = self.adopt("1.1.0", "--from-profile", first["path"], "--custom-file", custom)
        status = self.lifecycle("status")
        rollout = status["value"]["private.fixture@1.1.0"]
        self.assertEqual(rollout["uses"], [])
        self.assertEqual(rollout["carried_uses"], 2)
        self.assertEqual(rollout["distinct_successful_uses"], 2)
        self.assertTrue(rollout["ready_for_review"])
        self.assertEqual(status["coverage"]["unused_rules"], [])
        changed = {"fixture.beta": {"title": "TITLE_beta", "level": "required", "scopes": ["SCOPE_FIXTURE"], "text": "RULE_beta_CHANGED"}}
        self.adopt("1.2.0", "--from-profile", second["path"], "--overrides-file", self.file("overrides.json", changed))
        status = self.lifecycle("status")
        rollout = status["value"]["private.fixture@1.2.0"]
        self.assertEqual(rollout["carried_uses"], 1)
        self.assertFalse(rollout["ready_for_review"])
        self.assertEqual(status["coverage"]["unused_rules"], ["fixture.beta@1.0.0"])
        (self.project / ".opinion" / "profiles" / "private.fixture" / "versions" / "1.0.0.json").unlink()
        status = self.lifecycle("status")
        self.assertEqual(status["value"]["private.fixture@1.2.0"]["carried_uses"], 0)
        self.assertEqual([item["profile"] for item in status["coverage"]["not_carried"]], ["private.fixture@1.0.0"])

    def test_status_stays_readable_when_the_current_text_cannot_be_verified(self) -> None:
        self.seed()
        self.adopt("1.0.0")
        opinion = self.project / "OPINION.md"
        opinion.write_text(opinion.read_text(encoding="utf-8") + "MANUAL_EDIT\n", encoding="utf-8")
        status = self.lifecycle("status")
        self.assertIsNone(status["coverage"])
        self.assertIn("private.fixture@1.0.0", status["value"])
        self.event({"kind": "use", "occurrence": "tasks/one/result.md", "rules": ["fixture.alpha@1.0.0"], "passed": True, "user_changed": False}, code=2)


if __name__ == "__main__":
    unittest.main()
