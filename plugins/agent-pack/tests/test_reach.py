"""Impact and reachability (#184 step 1): the pure computation in ``agent_pack_reach`` on hand-made facts (no file system),
the driver declarations it reads (``compat_reads``, ``approval_required``) and the guards that keep client names out of it."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_reach as reach  # noqa: E402

NAME = "pdf-notes"
P = "/p"   # the project root of the hand-made paths


def hop(shown: str, phys: str | None = None, real: str | None = None) -> dict:
    absolute = phys or shown.replace("<project>", P).replace("~", "/h")
    return {"phys": absolute, "real": real or absolute, "shown": shown}


def skill(client: str, scope: str, store: str, digest: str = "d1", hops: list[dict] | None = None, **extra) -> dict:
    path = f"{store}/{NAME}"
    ref = f"skill:{NAME}@{client}:{scope}" + ("" if extra.pop("primary", True) else "#agents")
    return {"ref": ref, "client": client, "name": NAME, "scope": scope, "store": store, "path": path, "evidence": "file",
            "digest": digest, "gate": None, "problem": None, "resolved": None, "hops": hops or [hop(path)], **extra}


def reads(client: str, kind: str = "skills", **gates: str) -> dict:
    return {"id": client, "detected": True, "undetermined": False, "compat": [],
            "reads": [{"scope": s["scope"], "path": s["path"], "gate": gates.get(s["path"]) or (
                "approval" if s.get("approval_required") else "trust" if s.get("trust_required") else None)}
                      for s in registry.stores(client, kind)]}


def retire(path: str, scope: str, *, link: bool = False, real: str | None = None, phys: str | None = None) -> dict:
    """``phys`` is the physical path (parent links resolved); by default the spelling of ``path`` is already physical."""
    phys = phys or path.replace("<project>", P).replace("~", "/h")
    return {"phys": phys, "is_link": link, "real": None if link else (real or phys), "scope": scope}


def dispose(facts: dict, retired: list[dict], add: list[dict] | None = None, kind: str = "skill", **extra) -> dict:
    return reach.impact({"kind": kind, "name": NAME, "retire": retired, "add": add or [], **extra}, facts)


class SkillVerdictTests(unittest.TestCase):
    def facts_1_2(self) -> dict:
        """The #184 reproduction: ``<project>/skills/pdf-notes`` is the real directory, ``<project>/.agents/skills`` is a link to
        ``../skills`` and ``<project>/.claude/skills/pdf-notes`` links to ``../../.agents/skills/pdf-notes``."""
        real = f"{P}/skills/{NAME}"
        link_hops = [hop(f"<project>/.claude/skills/{NAME}", phys=f"{P}/.claude/skills/{NAME}", real=real),
                     hop(f"<project>/.agents/skills/{NAME}", phys=real, real=real)]
        return {"clients": [reads("claude"), reads("codex"), reads("cursor")], "entries": [
            skill("claude", "project", "<project>/.claude/skills", hops=link_hops, resolved=f"<project>/skills/{NAME}"),
            skill("codex", "user", "~/.agents/skills", primary=False),
            skill("codex", "project", "<project>/.agents/skills", hops=[hop(f"<project>/.agents/skills/{NAME}", phys=real, real=real)]),
            skill("cursor", "user", "~/.agents/skills", primary=False),
            skill("cursor", "project", "<project>/.agents/skills", primary=False,
                  hops=[hop(f"<project>/.agents/skills/{NAME}", phys=real, real=real)])]}

    def test_retiring_the_real_directory_leaves_the_claude_link_dangling(self) -> None:
        result = dispose(self.facts_1_2(), [retire(f"<project>/.agents/skills/{NAME}", "project", phys=f"{P}/skills/{NAME}")])
        # the archived entry is ``<project>/.agents/skills/..``; its physical path is where ``.agents/skills`` points to
        self.assertEqual(result["lost_clients"], ["claude"])
        claude = result["clients"]["claude"]
        self.assertEqual((claude["verdict"], claude["reasons"], claude["after"]), ("lost", ["link-target-retired"], []))
        self.assertEqual(claude["via_before"][0]["chain"], [f"<project>/.agents/skills/{NAME}"])
        self.assertEqual(result["dangling_after"], [f"<project>/.claude/skills/{NAME}"])
        for other in ("codex", "cursor"):
            self.assertEqual(result["clients"][other]["verdict"], "unchanged", other)
            self.assertEqual(result["clients"][other]["after_stores"], ["~/.agents/skills"])
        self.assertEqual((result["basis"], result["schema"]), ("file", 1))

    def test_retiring_only_the_link_leaves_the_target_and_the_other_links_alone(self) -> None:
        result = dispose(self.facts_1_2(), [retire(f"<project>/.claude/skills/{NAME}", "project", link=True)])
        self.assertEqual(result["clients"]["claude"]["verdict"], "lost")
        self.assertEqual(result["clients"]["claude"]["reasons"], ["entry-retired", "no-other-copy"])
        self.assertEqual(result["dangling_after"], [], "an archived link is gone, nothing dangles")
        self.assertEqual({c: row["verdict"] for c, row in result["clients"].items() if c != "claude"},
                         {"codex": "unchanged", "cursor": "unchanged"})

    def test_a_user_level_copy_keeps_claude_whole_and_claude_does_not_read_the_shared_store(self) -> None:
        facts = self.facts_1_2()
        facts["entries"].append(skill("claude", "user", "~/.claude/skills"))
        result = dispose(facts, [retire(f"<project>/.agents/skills/{NAME}", "project", phys=f"{P}/skills/{NAME}")])
        self.assertEqual(result["clients"]["claude"]["verdict"], "unchanged")
        self.assertEqual(result["lost_clients"], [])
        self.assertEqual(result["dangling_after"], [f"<project>/.claude/skills/{NAME}"], "the link still dangles")
        self.assertNotIn("~/.agents/skills", result["clients"]["claude"]["reads"])
        self.assertEqual(result["clients"]["claude"]["reads"], ["~/.claude/skills", "<project>/.claude/skills"])

    def test_a_copy_in_a_compat_location_downgrades_lost_to_uncertain(self) -> None:
        facts = {"clients": [{**reads("cursor"), "compat": [{"name": NAME, "client": "cursor", "path": f"~/.claude/skills/{NAME}",
                                                           "hops": [hop(f"~/.claude/skills/{NAME}")], "problem": None}]}],
                 "entries": [skill("cursor", "user", "~/.cursor/skills")]}
        result = dispose(facts, [retire(f"~/.cursor/skills/{NAME}", "user")])
        row = result["clients"]["cursor"]
        self.assertEqual((row["verdict"], row["reasons"]), ("uncertain", ["compat-copy-present"]))
        self.assertEqual(result["lost_clients"], [])
        self.assertEqual(result["uncertain_clients"], ["cursor"])
        self.assertEqual(result["scope_notes"], ["affects-all-projects"])
        # without the compat copy the same disposition is a plain loss
        facts["clients"][0]["compat"] = []
        self.assertEqual(dispose(facts, [retire(f"~/.cursor/skills/{NAME}", "user")])["clients"]["cursor"]["verdict"], "lost")

    def test_unreadable_or_unresolvable_copies_make_the_verdict_unknown(self) -> None:
        for label, change in (("broken", {"evidence": "unknown", "digest": None, "problem": "dangling"}),
                              ("too-deep", {"problem": "too-deep"}), ("loop", {"problem": "loop"})):
            facts = {"clients": [reads("claude")], "entries": [skill("claude", "user", "~/.claude/skills"),
                                                                skill("claude", "project", "<project>/.claude/skills", **change)]}
            result = dispose(facts, [retire(f"~/.claude/skills/{NAME}", "user")])
            row = result["clients"]["claude"]
            self.assertEqual(row["verdict"], "unknown", label)
            self.assertEqual(row["evidence"], "unknown")
            self.assertEqual(result["unknown_clients"], ["claude"])
            self.assertEqual(row["reasons"], ["link-chain-too-deep"] if label != "broken" else ["evidence-unknown"], label)
            self.assertNotIn("lost", [r["verdict"] for r in result["clients"].values()])

    def test_a_chain_of_nine_links_is_too_deep_and_a_loop_is_detected_by_the_reader(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            (base / "real").mkdir()
            previous = "real"
            for index in range(9):
                os.symlink(previous, base / f"l{index}")
                previous = f"l{index}"
            hops, problem, resolved = reach.chain(base / "l8", str)
            self.assertEqual((problem, resolved, len(hops) > reach.MAX_HOPS), ("too-deep", None, True))
            os.symlink("b", base / "a")
            os.symlink("a", base / "b")
            self.assertEqual(reach.chain(base / "a", str)[1], "loop")
            os.symlink("real", base / "ok")
            hops, problem, resolved = reach.chain(base / "ok", str)
            self.assertEqual((problem, resolved, len(hops)), (None, str(base / "real"), 2))
            os.symlink("missing", base / "gone")
            self.assertEqual(reach.chain(base / "gone", str)[1], "dangling")

    def test_a_different_copy_is_replaced_and_a_new_client_gains_it(self) -> None:
        facts = {"clients": [reads("claude"), reads("codex")],
                 "entries": [skill("claude", "user", "~/.claude/skills", digest="a"),
                             skill("claude", "project", "<project>/.claude/skills", digest="b")]}
        result = dispose(facts, [retire(f"~/.claude/skills/{NAME}", "user")])
        self.assertEqual(result["clients"]["claude"]["verdict"], "replaced")
        self.assertEqual(result["clients"]["claude"]["after_stores"], ["<project>/.claude/skills"])
        self.assertNotIn("codex", result["clients"], "no copy before and none after: not listed")
        moved = dispose(facts, [retire(f"<project>/.claude/skills/{NAME}", "project"), retire(f"~/.claude/skills/{NAME}", "user")],
                        [reach.add_item("skill", NAME, "codex", "project", "<project>/.agents/skills", "a")], other_machines=True)
        self.assertEqual(moved["clients"]["codex"]["verdict"], "gained")
        self.assertEqual(moved["clients"]["codex"]["after"], [f"skill:{NAME}@codex:project"])
        self.assertEqual(moved["clients"]["claude"]["verdict"], "lost")
        self.assertEqual(moved["scope_notes"], ["affects-all-projects", "other-machines-lose-it"])

    def test_a_synthetic_client_gets_a_row_and_the_module_names_no_client(self) -> None:
        synthetic = {"id": "zz-synthetic-client", "detected": True, "undetermined": False, "compat": [],
                     "reads": [{"scope": "user", "path": "~/.zz/skills", "gate": None}]}
        entry = skill("zz-synthetic-client", "user", "~/.zz/skills")
        result = dispose({"clients": [synthetic], "entries": [entry]}, [retire(f"~/.zz/skills/{NAME}", "user")])
        self.assertEqual(result["clients"]["zz-synthetic-client"]["verdict"], "lost")
        warnings = reach.warnings(result)
        self.assertEqual([w["code"] for w in warnings], ["impact-lost-client"])
        self.assertEqual(warnings[0]["client"], "zz-synthetic-client")
        text = (RUNTIME / "agent_pack_reach.py").read_text(encoding="utf-8")
        for client in registry.all_ids():
            self.assertNotIn(f'"{client}"', text, client)
            self.assertNotIn(f"'{client}'", text, client)

    def test_an_undetermined_store_is_unknown(self) -> None:
        facts = {"clients": [{**reads("claude"), "undetermined": True}], "entries": [skill("claude", "user", "~/.claude/skills")]}
        row = dispose(facts, [retire(f"~/.claude/skills/{NAME}", "user")])["clients"]["claude"]
        self.assertEqual((row["verdict"], row["reasons"]), ("unknown", ["store-undetermined"]))


class McpVerdictTests(unittest.TestCase):
    def mcp(self, client: str, scope: str, store: str, digest: str = "f1") -> dict:
        return {"ref": f"mcp:{NAME}@{client}:{scope}", "client": client, "name": NAME, "scope": scope, "store": store, "path": store,
                "evidence": "file", "digest": digest, "gate": reads(client, "mcp")["reads"][
                    [r["path"] for r in reads(client, "mcp")["reads"]].index(store)]["gate"], "problem": None, "resolved": None,
                "hops": [{"phys": f"{store}#{NAME}", "real": None, "shown": store}]}

    def mcp_retire(self, store: str, scope: str) -> dict:
        return reach.retire_mcp_item(store, NAME, scope)

    def test_local_retired_with_the_same_server_at_user_level_is_unchanged(self) -> None:
        facts = {"clients": [reads("claude", "mcp")], "entries": [self.mcp("claude", "user", "~/.claude.json"),
                                                                 self.mcp("claude", "local", "~/.claude.json#projects")]}
        result = dispose(facts, [self.mcp_retire("~/.claude.json#projects", "local")], kind="mcp")
        self.assertEqual(result["clients"]["claude"]["verdict"], "unchanged")
        self.assertEqual(result["scope_notes"], [])

    def test_the_only_user_level_copy_is_lost_and_affects_every_project(self) -> None:
        facts = {"clients": [reads("claude", "mcp")], "entries": [self.mcp("claude", "user", "~/.claude.json")]}
        result = dispose(facts, [self.mcp_retire("~/.claude.json", "user")], kind="mcp")
        self.assertEqual((result["lost_clients"], result["scope_notes"]), (["claude"], ["affects-all-projects"]))

    def test_only_the_project_file_left_needs_an_approval_that_is_not_read(self) -> None:
        facts = {"clients": [reads("claude", "mcp")], "entries": [self.mcp("claude", "user", "~/.claude.json"),
                                                                 self.mcp("claude", "project", "<project>/.mcp.json")]}
        row = dispose(facts, [self.mcp_retire("~/.claude.json", "user")], kind="mcp")["clients"]["claude"]
        self.assertEqual((row["verdict"], row["reasons"]), ("uncertain", ["project-approval-unknown"]))
        gone = dispose(facts, [self.mcp_retire("<project>/.mcp.json", "project")], kind="mcp")["clients"]["claude"]
        self.assertEqual(gone["verdict"], "unchanged", "the user-level copy needs no approval")

    def test_a_trust_gated_project_file_is_uncertain_for_the_client_that_declares_it(self) -> None:
        facts = {"clients": [reads("codex", "mcp")], "entries": [self.mcp("codex", "user", "~/.codex/config.toml"),
                                                                self.mcp("codex", "project", "<project>/.codex/config.toml")]}
        row = dispose(facts, [self.mcp_retire("~/.codex/config.toml", "user")], kind="mcp")["clients"]["codex"]
        self.assertEqual((row["verdict"], row["reasons"]), ("uncertain", ["project-trust-unknown"]))

    def test_the_only_copy_behind_an_approval_is_not_reported_as_lost(self) -> None:
        facts = {"clients": [reads("claude", "mcp")], "entries": [self.mcp("claude", "project", "<project>/.mcp.json")]}
        row = dispose(facts, [self.mcp_retire("<project>/.mcp.json", "project")], kind="mcp")["clients"]["claude"]
        self.assertEqual((row["verdict"], row["reasons"]), ("uncertain", ["project-approval-unknown"]))

    def test_moving_to_a_user_level_scope_is_gained_for_the_client_and_notes_the_other_machines(self) -> None:
        facts = {"clients": [reads("claude", "mcp")], "entries": [self.mcp("claude", "project", "<project>/.mcp.json")]}
        result = dispose(facts, [self.mcp_retire("<project>/.mcp.json", "project")],
                         [reach.add_item("mcp", NAME, "claude", "user", None, "f1")], kind="mcp", other_machines=True)
        self.assertEqual(result["clients"]["claude"]["verdict"], "unchanged")
        self.assertEqual(result["scope_notes"], ["other-machines-lose-it"])
        self.assertEqual([w["code"] for w in reach.warnings(result)], ["impact-other-machines"])


class WarningTests(unittest.TestCase):
    def test_codes_and_keys(self) -> None:
        facts = {"clients": [reads("claude")], "entries": [skill(
            "claude", "project", "<project>/.claude/skills", hops=[
                hop(f"<project>/.claude/skills/{NAME}", phys=f"{P}/.claude/skills/{NAME}", real=f"{P}/skills/{NAME}"),
                hop(f"<project>/skills/{NAME}", phys=f"{P}/skills/{NAME}", real=f"{P}/skills/{NAME}")])]}
        result = dispose(facts, [retire(f"<project>/skills/{NAME}", "project")])
        self.assertEqual([w["code"] for w in reach.warnings(result)], ["impact-lost-client", "impact-dangling-link"])
        self.assertEqual(reach.warnings(result)[0]["ref"], f"skill:{NAME}@claude:project")
        json.dumps(result)    # JSON-safe
        self.assertFalse({"message", "text"} & set(json.dumps(result).replace('"', " ").split()), "no localized text in the object")


class DriverDeclarationTests(unittest.TestCase):
    def base(self) -> dict:
        return copy.deepcopy(next(d for d in registry.BUILTIN if d["id"] == "cursor"))

    def test_compat_reads_are_declared_for_cursor_only_and_inventory_does_not_see_them(self) -> None:
        self.assertEqual([place["path"] for place in registry.compat_reads("cursor", "skills")],
                         ["~/.claude/skills", "~/.codex/skills", "<project>/.claude/skills", "<project>/.codex/skills"])
        self.assertEqual([c for c in registry.all_ids() if registry.compat_reads(c, "skills")], ["cursor"])
        self.assertEqual(registry.compat_reads("cursor", "mcp"), ())
        self.assertEqual(registry.compat_reads("no-such-client", "skills"), ())
        paths = {path for _scope, path in registry.store_table("skills")["cursor"]}
        self.assertFalse(paths & {place["path"] for place in registry.compat_reads("cursor", "skills")})
        self.assertEqual([s["path"] for s in registry.stores("cursor", "skills")],
                         ["~/.cursor/skills", "~/.agents/skills", "<project>/.cursor/skills", "<project>/.agents/skills"])

    def test_approval_required_is_declared_for_the_project_mcp_file_only(self) -> None:
        gated = [(c, s["scope"]) for c in registry.all_ids() for s in registry.stores(c, "mcp") if s.get("approval_required")]
        self.assertEqual(gated, [("claude", "project")])

    def test_valid_and_invalid_compat_reads(self) -> None:
        good = self.base()
        good["compat_reads"] = {"skills": [{"scope": "user", "path": "~/.x/skills"}, {"scope": "project", "path": "<project>/.x/skills",
                                                                                      "status": "observed", "source_url": None}]}
        registry.validate_builtin(good, "cursor.json")
        for label, value in (("not-an-object", []), ("unknown-kind", {"hooks": []}), ("not-a-list", {"skills": {}}),
                             ("missing-path", {"skills": [{"scope": "user"}]}), ("bad-scope", {"skills": [{"scope": "all", "path": "~/x"}]}),
                             ("absolute", {"skills": [{"scope": "user", "path": "/etc/skills"}]}),
                             ("bad-status", {"skills": [{"scope": "user", "path": "~/x", "status": "undetermined"}]}),
                             ("extra-key", {"skills": [{"scope": "user", "path": "~/x", "format": "dir"}]})):
            bad = self.base()
            bad["compat_reads"] = value
            with self.assertRaises(registry.DriverDataError, msg=label):
                registry.validate_builtin(bad, "cursor.json")

    def test_approval_required_must_be_a_boolean_on_an_mcp_store(self) -> None:
        for kind, value in (("mcp", "yes"), ("skills", True)):
            bad = self.base()
            bad["stores"][kind][0]["approval_required"] = value
            with self.assertRaises(registry.DriverDataError, msg=f"{kind} {value}"):
                registry.validate_builtin(bad, "cursor.json")
        good = self.base()
        good["stores"]["mcp"][0]["approval_required"] = True
        registry.validate_builtin(good, "cursor.json")


if __name__ == "__main__":
    unittest.main()
