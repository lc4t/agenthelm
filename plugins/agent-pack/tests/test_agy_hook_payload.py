"""Antigravity's documented hook payload (#134): ``workspacePaths`` and ``conversationId``, no ``cwd`` and no session id.

The documentation (antigravity.google/docs/hooks) gives every hook ``conversationId``, ``workspacePaths`` (absolute paths of the
mounted workspaces), ``transcriptPath``, ``artifactDirectoryPath``, ``modelName`` and ``lastUserInput``. The driver declares the two
keys under ``hooks.payload`` so the nudge code names no client: a declared key is read only after the keys every client is read
for, so what the other clients' payloads resolve to does not change. Whether a real ``agy`` sends exactly this is for the
release-candidate smoke test.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_nudge as nudge  # noqa: E402
import agenthelm  # noqa: E402

DOCUMENTED = {"conversationId": "c-1", "workspacePaths": [], "transcriptPath": "/t", "artifactDirectoryPath": "/a",
              "modelName": "m", "lastUserInput": "hello"}


class DirectoryTests(unittest.TestCase):
    def test_workspace_paths_name_the_project_for_the_declaring_client_only(self) -> None:
        payload = {**DOCUMENTED, "workspacePaths": ["/work/one", "/work/two"]}
        self.assertEqual(nudge.payload_directory(payload, "agy", {}), Path("/work/one"))
        self.assertIsNone(nudge.payload_directory(payload, "claude", {}))   # no other client reads that key
        self.assertIsNone(nudge.payload_directory(payload, "codex", {}))
        self.assertIsNone(nudge.payload_directory(payload, "cursor", {}))

    def test_the_keys_every_client_is_read_for_come_first(self) -> None:
        payload = {"cwd": "/from/cwd", "workspacePaths": ["/ws"], "workspace_roots": ["/roots"]}
        self.assertEqual(nudge.payload_directory(payload, "agy", {}), Path("/roots"))
        self.assertEqual(nudge.payload_directory({"cwd": "/from/cwd", "workspacePaths": ["/ws"]}, "agy", {}), Path("/from/cwd"))
        self.assertEqual(nudge.payload_directory({"workspacePaths": ["/ws"]}, "agy", {"CLAUDE_PROJECT_DIR": "/env"}), Path("/env"))

    def test_odd_values_are_not_a_directory(self) -> None:
        for value in ([], [None], [""], "", None, 5, [5]):
            self.assertIsNone(nudge.payload_directory({"workspacePaths": value}, "agy", {}), value)
        self.assertEqual(nudge.payload_directory({"workspacePaths": "/plain"}, "agy", {}), Path("/plain"))

    def test_existing_payload_shapes_resolve_as_before(self) -> None:
        cases = ({"workspace_roots": ["/a"]}, {"cwd": "/b"}, {}, {"workspace_roots": [], "cwd": "/c"})
        for payload in cases:
            for client in ("claude", "codex", "cursor", "auto"):
                expected = (Path(payload["workspace_roots"][0]) if payload.get("workspace_roots") else
                            Path(payload["cwd"]) if payload.get("cwd") else None)
                self.assertEqual(nudge.payload_directory(payload, client, {}), expected, (client, payload))


class SessionTests(unittest.TestCase):
    def test_conversation_id_is_the_session_for_the_declaring_client(self) -> None:
        self.assertEqual(nudge.payload_session(DOCUMENTED, "agy", "session_id", "conversation_id"), "c-1")
        self.assertEqual(nudge.payload_session(DOCUMENTED, "claude", "session_id", "conversation_id"), "")
        self.assertEqual(nudge.payload_session({"session_id": "s", "conversationId": "c"}, "agy", "session_id"), "s")
        self.assertEqual(nudge.payload_session({"conversation_id": "x"}, "cursor", "session_id", "conversation_id"), "x")
        self.assertEqual(nudge.payload_session({}, "agy", "session_id"), "")


class HookRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "proj"
        (self.project / ".opinion").mkdir(parents=True)
        (self.project / ".opinion" / "inbox.json").write_text("{}", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en", "HOME": str(self.home), "AGENTHELM_NO_NUDGE": ""})
        env.start()
        self.addCleanup(env.stop)

    def hook(self, event: str, payload: dict, client: str = "agy") -> dict | None:
        args = nudge.parser().parse_args(["hook", "--event", event, "--client", client, "--home", str(self.home)])
        return nudge.run_hook(args, agenthelm, payload)

    def documented(self, project: Path | None = None, **extra) -> dict:
        return {**DOCUMENTED, "workspacePaths": [str(project or self.project)], **extra}

    def test_session_start_injects_steps_for_the_documented_payload(self) -> None:
        output = self.hook("session-start", self.documented())
        self.assertIsNotNone(output)
        text = output["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Opinion", text)

    def test_a_project_without_opinion_is_silent(self) -> None:
        bare = self.root / "bare"
        bare.mkdir()
        self.assertIsNone(self.hook("session-start", self.documented(bare)))

    def test_the_conversation_is_deduplicated_by_conversation_id(self) -> None:
        first = self.hook("session-start", self.documented())
        second = self.hook("session-start", self.documented())
        self.assertIsNotNone(first)
        self.assertIsNone(second)   # the exposure notice is given once per project; the second call has nothing to add
        state = json.loads((self.home / ".local/state/agenthelm/nudge.json").read_text())
        self.assertIn(str(self.project), state["exposure_noted"])

    def test_a_payload_in_the_other_shape_still_works(self) -> None:
        self.assertIsNotNone(self.hook("session-start", {"cwd": str(self.project), "session_id": "s"}))

    def test_stop_asks_with_a_continue_decision_and_counts_the_conversation(self) -> None:
        import agent_pack_nudge as module
        calls = []
        pending = {"pending": 1, "trial": 0, "oldest_days": 0.0, "hint": None, "session_unpresented": ["id-1"],
                   "summaries": []}

        def fake_manager(manager, *arguments, lang=None):
            calls.append(arguments[:2])
            if arguments[:2] == ("inbox", "gate"):
                return {"allowed": True}
            if "markdown" in arguments:
                return "- an item\n"
            return {"ok": True}

        (self.project / ".opinion" / "inbox.json").write_text(json.dumps({"c-1": "x"}), encoding="utf-8")
        config = {"agy": "always"}
        user = self.home / ".config" / "agenthelm" / "config.json"
        user.parent.mkdir(parents=True)
        user.write_text(json.dumps({"nudge": {"stop_prompt": config}}), encoding="utf-8")
        with mock.patch.object(module, "find_manager", return_value=Path("/m.py")), \
                mock.patch.object(module, "collect", return_value=pending), \
                mock.patch.object(module, "run_manager", side_effect=fake_manager):
            first = self.hook("stop", self.documented(executionNum=1, terminationReason="done", fullyIdle=True))
            second = self.hook("stop", self.documented(executionNum=2, terminationReason="done", fullyIdle=True))
        self.assertEqual(first["decision"], "continue")
        self.assertIn("reason", first)
        self.assertIsNone(second)   # once per conversation (max_prompts_per_session): the count is keyed by conversationId


class DeclarationTests(unittest.TestCase):
    def good(self) -> dict:
        return json.loads((registry.DRIVERS_DIR / "agy.json").read_text(encoding="utf-8"))

    def test_the_payload_block_is_validated(self) -> None:
        registry.validate_builtin(self.good(), "agy.json")
        for bad in ({}, {"project_dir": []}, {"project_dir": "workspacePaths"}, {"cwd": ["x"]},
                    {"project_dir": ["a b"]}, {"session": ["ok", 5]}, {"session": ["a", "b", "c", "d", "e"]}, []):
            broken = copy.deepcopy(self.good())
            broken["hooks"]["payload"] = bad
            with self.subTest(bad=bad), self.assertRaises(registry.DriverDataError):
                registry.validate_builtin(broken, "agy.json")

    def test_no_other_builtin_driver_declares_payload_keys(self) -> None:
        self.assertEqual([c for c in registry.all_ids() if "payload" in registry.hooks(c)], ["agy"])


if __name__ == "__main__":
    unittest.main()
