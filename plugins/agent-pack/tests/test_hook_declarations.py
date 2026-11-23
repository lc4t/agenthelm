"""Hook declarations (#134 batch D1): events, output adapters, legacy sniffing, validation."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_nudge as nudge  # noqa: E402

DRIVERS = RUNTIME / "drivers"


def driver(name: str = "claude") -> dict:
    return json.loads((DRIVERS / f"{name}.json").read_text(encoding="utf-8"))


def problem(data: dict) -> str:
    try:
        registry.validate_builtin(data, "synthetic.json")
    except registry.DriverDataError as error:
        return str(error)
    return ""


class DeclaredEventTests(unittest.TestCase):
    def test_builtin_event_maps_are_the_ones_the_code_used_before(self) -> None:
        native = {c: {e: registry.hook_event_name(c, e) for e in registry.HOOK_EVENTS} for c in ("claude", "codex", "cursor")}
        self.assertEqual(native["claude"], {"session-start": "SessionStart", "prompt-submit": "UserPromptSubmit", "stop": "Stop"})
        self.assertEqual(native["codex"], native["claude"])
        self.assertEqual(native["cursor"], {"session-start": "sessionStart", "prompt-submit": None, "stop": "stop"})

    def test_stop_prompt_defaults_come_from_the_declarations(self) -> None:
        self.assertEqual(nudge.DEFAULTS["stop_prompt"], {"claude": "on-completion", "codex": "on-completion", "cursor": "off"})

    def test_every_adapter_name_is_implemented_and_nothing_else(self) -> None:
        self.assertEqual(set(nudge.ADAPTERS), set(registry.HOOK_OUTPUTS))

    def test_builtin_drivers_name_adapters_by_format_not_by_client(self) -> None:
        for client in registry.all_ids():
            for name in registry.hooks(client).get("output", {}).values():
                self.assertNotIn(name, registry.all_ids())


class AdapterTests(unittest.TestCase):
    def test_hook_specific_output_shapes(self) -> None:
        adapter = nudge.ADAPTERS["hookSpecificOutput"]
        self.assertEqual(adapter("Stop", "c", "s"), {"systemMessage": "s", "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "c"}})
        self.assertEqual(adapter("Stop", None, "s"), {"systemMessage": "s"})
        self.assertEqual(adapter("Stop", "c", None), {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "c"}})

    def test_additional_context_and_followup_message(self) -> None:
        self.assertEqual(nudge.ADAPTERS["additional_context"]("x", "c", "s"), {"additional_context": "c"})
        self.assertIsNone(nudge.ADAPTERS["additional_context"]("x", None, "s"))
        self.assertEqual(nudge.ADAPTERS["followup_message"]("x", "c", None), {"followup_message": "c"})
        self.assertIsNone(nudge.ADAPTERS["followup_message"]("x", None, "s"))

    def test_decision_block(self) -> None:
        self.assertEqual(nudge.ADAPTERS["decision_block"]("Stop", "c", "s"), {"decision": "block", "reason": "c"})
        self.assertIsNone(nudge.ADAPTERS["decision_block"]("Stop", None, "s"))

    def test_inject_steps_shape_is_the_documented_one(self) -> None:
        adapter = nudge.ADAPTERS["injectSteps"]
        self.assertEqual(adapter("SessionStart", "hello", None), {"injectSteps": [{"systemMessage": {"systemMessage": "hello"}}]})
        self.assertEqual(adapter("Stop", "x", "ignored"), {"injectSteps": [{"systemMessage": {"systemMessage": "x"}}]})
        self.assertEqual(json.dumps(adapter("Stop", "é", None), ensure_ascii=False), '{"injectSteps": [{"systemMessage": {"systemMessage": "é"}}]}')
        self.assertIsNone(adapter("Stop", None, "user line only"))
        self.assertIsNone(adapter("Stop", "", None))

    def test_decision_continue_is_the_antigravity_stop_output(self) -> None:
        adapter = nudge.ADAPTERS["decision_continue"]
        self.assertEqual(adapter("Stop", "go on", "ignored"), {"decision": "continue", "reason": "go on"})
        self.assertIsNone(adapter("Stop", None, "only a user line"))
        self.assertIsNone(adapter("Stop", "", None))
        self.assertEqual(json.dumps(adapter("Stop", "é", None), ensure_ascii=False), '{"decision": "continue", "reason": "é"}')
        used = {client for client in registry.all_ids() if "decision_continue" in registry.hooks(client).get("output", {}).values()}
        self.assertEqual(used, {"agy"})

    def test_inject_steps_is_used_by_the_antigravity_driver_only(self) -> None:
        used = {client for client in registry.all_ids()
                if "injectSteps" in registry.hooks(client).get("output", {}).values()}
        self.assertEqual(used, {"agy"})


class LegacySniffTests(unittest.TestCase):
    """Old installs call ``nudge hook`` without --client; the payload decides, exactly as before."""

    def test_explicit_client_wins(self) -> None:
        self.assertEqual(nudge.detect_client("codex", {"workspace_roots": []}, {}), "codex")

    def test_old_command_without_client_is_recognised_by_its_payload(self) -> None:
        self.assertEqual(nudge.detect_client("auto", {"workspace_roots": ["/p"]}, {}), "cursor")
        self.assertEqual(nudge.detect_client("auto", {"conversation_id": "c"}, {}), "cursor")
        self.assertEqual(nudge.detect_client("auto", {"turn_id": "t", "workspace_roots": []}, {}), "cursor")  # cursor is checked first
        self.assertEqual(nudge.detect_client("auto", {"turn_id": "t"}, {}), "codex")
        self.assertEqual(nudge.detect_client("auto", {}, {"PLUGIN_ROOT": "/x"}), "codex")
        self.assertEqual(nudge.detect_client("auto", {}, {"PLUGIN_ROOT": ""}), "claude")
        self.assertEqual(nudge.detect_client("auto", {"session_id": "s", "cwd": "/p"}, {}), "claude")
        self.assertEqual(nudge.detect_client("auto", {}, {}), "claude")


class HookValidationTests(unittest.TestCase):
    def test_shipped_declarations_are_valid(self) -> None:
        for name in ("claude", "codex", "cursor"):
            self.assertEqual(problem(driver(name)), "", name)

    def test_unknown_event_name_is_refused(self) -> None:
        data = driver()
        data["hooks"]["events"]["session-end"] = "SessionEnd"
        self.assertIn("hooks.events", problem(data))

    def test_unknown_adapter_name_is_refused(self) -> None:
        data = driver()
        data["hooks"]["output"]["stop"] = "claude"
        self.assertIn("hooks.output", problem(data))

    def test_output_must_cover_exactly_the_mapped_events(self) -> None:
        data = driver("cursor")
        data["hooks"]["output"]["prompt-submit"] = "hookSpecificOutput"
        self.assertIn("hooks.output", problem(data))
        data = driver()
        del data["hooks"]["output"]["stop"]
        self.assertIn("hooks.output", problem(data))

    def test_unsupported_needs_the_declared_shape(self) -> None:
        for bad in ([{"feature": "hook-event:prompt-submit"}],
                    [{"feature": "prompt-submit", "reason": "x"}],
                    [{"feature": "hook-event:nope", "reason": "x"}],
                    [{"feature": "hook-event:prompt-submit", "reason": ""}],
                    [{"feature": "hook-event:prompt-submit", "reason": "x", "extra": 1}],
                    "hook-event:prompt-submit"):
            data = driver("cursor")
            data["unsupported"] = bad
            self.assertIn("unsupported", problem(data), bad)

    def test_an_event_is_mapped_or_unsupported_never_both_or_neither(self) -> None:
        both = driver("cursor")
        both["hooks"]["events"]["prompt-submit"] = "x"
        both["hooks"]["output"]["prompt-submit"] = "hookSpecificOutput"
        self.assertIn("exactly one", problem(both))
        neither = driver("cursor")
        neither["unsupported"] = []
        self.assertIn("exactly one", problem(neither))
        twice = driver("cursor")
        twice["unsupported"] = twice["unsupported"] * 2
        self.assertIn("twice", problem(twice))

    def test_unsupported_events_need_a_hooks_declaration(self) -> None:
        data = driver("cursor")
        del data["hooks"]
        self.assertIn("hooks declaration", problem(data))

    def test_bad_values_inside_hooks_are_refused(self) -> None:
        cases = [("stop_prompt", "sometimes"), ("shows_system_message", "yes"), ("config", {"format": "toml"}),
                 ("config", {"format": "flat-event-commands", "user": "~/x"}), ("legacy_sniff", {"payload_keys": ["a"]}),
                 ("legacy_sniff", {"priority": 1, "payload_keys": "a"}), ("extra", 1)]
        for key, value in cases:
            data = copy.deepcopy(driver("cursor"))
            data["hooks"][key] = value
            self.assertNotEqual(problem(data), "", (key, value))

    def test_user_declarations_may_not_hold_hooks(self) -> None:
        data = {"schema_version": 1, "id": "example-ide", "display": "Example IDE",
                "detect": {"executables": ["example-ide"]}, "distribution": "none", "stores": {}, "capabilities": {}}
        self.assertIsNone(registry.validate_declaration(data))
        for placement in (lambda d: d.update(hooks={"events": {}}), lambda d: d["stores"].update(hooks=[]),
                          lambda d: d.update(unsupported=[])):
            changed = copy.deepcopy(data)
            placement(changed)
            self.assertIsNotNone(registry.validate_declaration(changed))
        changed = copy.deepcopy(data)
        changed["hooks"] = {"events": {}}
        self.assertIn("hooks", registry.validate_declaration(changed))


if __name__ == "__main__":
    unittest.main()
