"""One resolver for sibling plugins (#134 Phase 3): three layers, driver-declared client caches, running_from."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
import agenthelm  # noqa: E402
import agenthelm_runtime  # noqa: E402


def plugin(path: Path, name: str, version: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "plugin.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    return path


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.release = self.root / "release/plugins"
        (self.release / "agent-pack").mkdir(parents=True)
        self.variables: dict[str, str] = {}
        self.env = agenthelm.Env(home=self.home, variables=self.variables)
        self.running = "checkout"
        for patcher in (mock.patch.object(agenthelm, "PLUGIN_ROOT", self.release / "agent-pack"),
                        mock.patch.object(agenthelm_runtime, "running_from", lambda env: self.running)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sibling(self, version: str = "1.0.0") -> Path:
        return plugin(self.release / "agents-init", "agents-init", version)

    def xdg(self, version: str = "1.0.0") -> Path:
        return plugin(self.home / ".local/share/agenthelm/current/plugins/agents-init", "agents-init", version)

    def claude_cache(self, version: str = "1.0.0") -> Path:
        return plugin(self.home / ".claude/plugins/cache/agenthelm/agents-init" / version, "agents-init", version)

    def resolve(self, **kwargs):
        return agenthelm.resolve_plugin("agents-init", self.env, **kwargs)

    # ------------------------------------------------------------ the three layers
    def test_each_layer_hits_alone(self) -> None:
        found = self.resolve()
        self.assertEqual((found.path, found.layer, found.found), (None, "not-found", False))
        cache = self.claude_cache()
        self.assertEqual((self.resolve().path, self.resolve().layer), (cache, "client-cache"))
        runtime = self.xdg()
        self.assertEqual((self.resolve().path, self.resolve().layer), (runtime, "runtime"))
        sibling = self.sibling()
        self.assertEqual((self.resolve().path, self.resolve().layer), (sibling, "sibling"))

    def test_layers_win_in_order_even_when_a_later_copy_is_newer(self) -> None:
        self.sibling("1.0.0"), self.xdg("2.0.0"), self.claude_cache("3.0.0")
        self.assertEqual(self.resolve().layer, "sibling")
        (self.release / "agents-init/plugin.json").unlink()
        self.assertEqual(self.resolve().layer, "runtime")

    def test_a_copy_without_plugin_json_or_failing_accept_is_skipped(self) -> None:
        (self.release / "agents-init").mkdir()
        self.assertFalse(self.resolve().found)
        self.sibling()
        cache = self.claude_cache("0.9.0")
        found = self.resolve(accept=lambda path: path == cache)
        self.assertEqual((found.path, found.layer), (cache, "client-cache"))

    def test_excluded_paths_are_skipped(self) -> None:
        sibling = self.sibling()
        cache = self.claude_cache()
        self.assertEqual(self.resolve(excluded={sibling}).path, cache)

    # ------------------------------------------------------------ client caches (declared in the drivers)
    def test_client_cache_takes_the_highest_version_across_clients(self) -> None:
        self.claude_cache("1.2.0")
        codex = plugin(self.home / ".codex/plugins/cache/agenthelm/agents-init/1.10.0", "agents-init", "1.10.0")
        plugin(self.home / ".cursor/plugins/local/agents-init", "agents-init", "1.3.0")
        self.assertEqual(self.resolve().path, codex)  # 1.10.0 > 1.3.0 > 1.2.0 numerically, not as text

    def test_config_directory_variables_move_the_cache(self) -> None:
        elsewhere = plugin(self.root / "cfg/claude/plugins/cache/agenthelm/agents-init/5.0.0", "agents-init", "5.0.0")
        self.assertFalse(self.resolve().found)
        self.variables["CLAUDE_CONFIG_DIR"] = str(self.root / "cfg/claude")
        self.assertEqual(self.resolve().path, elsewhere)
        other = plugin(self.root / "cfg/codex/plugins/cache/agenthelm/agents-init/6.0.0", "agents-init", "6.0.0")
        self.variables["CODEX_HOME"] = str(self.root / "cfg/codex")
        self.assertEqual(self.resolve().path, other)

    def test_installed_records_still_count_as_client_copies(self) -> None:
        recorded = plugin(self.root / "anywhere/agents-init", "agents-init", "4.0.0")
        other = plugin(self.root / "anywhere/opinion-manager", "opinion-manager", "9.0.0")
        installed = [{"client": "claude", "plugin": "agents-init", "path": str(recorded)},
                     {"client": "claude", "plugin": "opinion-manager", "path": str(other)}]
        self.assertEqual(self.resolve(installed=installed).path, recorded)

    def test_symlinked_copies_are_found(self) -> None:
        real = plugin(self.root / "working-copy/agents-init", "agents-init", "7.0.0")
        link = self.home / ".cursor/plugins/local/agents-init"
        link.parent.mkdir(parents=True)
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.resolve().path, link)
        shared = self.home / ".local/share/agenthelm/current"
        shared.parent.mkdir(parents=True)
        shared.symlink_to(self.release.parent, target_is_directory=True)
        self.assertFalse((shared / "plugins/agents-init/plugin.json").exists())

    # ------------------------------------------------------------ the intended change (design §9.2)
    def test_running_from_a_client_cache_prefers_the_user_runtime_over_client_copies(self) -> None:
        self.running = "client-plugin"
        self.sibling("1.0.0")  # e.g. Cursor's plugins/local neighbour: a client copy, not a release
        cache = self.claude_cache("1.0.0")
        self.assertEqual(self.resolve().path, cache)  # no user-level runtime installed: unchanged
        runtime = self.xdg("0.9.0")  # older than the client copy on purpose
        found = self.resolve()
        self.assertEqual((found.path, found.layer), (runtime, "runtime"))

    def test_the_newest_version_is_chosen_only_inside_a_layer(self) -> None:
        self.claude_cache("1.2.0")
        newest = self.claude_cache("1.10.0")
        plugin(self.home / ".cursor/plugins/local/agents-init", "agents-init", "1.3.0")
        self.assertEqual(self.resolve().path, newest)
        runtime = self.xdg("0.1.0")  # an earlier layer wins whatever the versions are
        self.assertEqual(self.resolve().path, runtime)
        self.assertEqual(agenthelm.locate_companion("agents-init", [], env=self.env), runtime)

    def test_minimum_skips_a_stale_early_copy_and_falls_back_to_the_best_one(self) -> None:
        sibling = self.sibling("1.0.0")
        cache = self.claude_cache("3.0.0")
        self.assertEqual(self.resolve(minimum="2.0.0").path, cache)  # the stale sibling does not veto
        self.assertEqual(self.resolve(minimum="1.0.0").path, sibling)  # a sufficient early copy still wins
        self.assertEqual(self.resolve(minimum="9.0.0").path, cache)  # nothing reaches it: the best copy, to be called too old

    def test_clients_limit_the_cache_layer(self) -> None:
        self.claude_cache("1.0.0")
        self.assertFalse(self.resolve(clients={"codex"}).found)
        self.assertTrue(self.resolve(clients={"claude"}).found)

    def test_locate_companion_and_resolve_plugin_agree(self) -> None:
        self.sibling("1.0.0"), self.xdg("2.0.0"), self.claude_cache("3.0.0")
        self.assertEqual(agenthelm.locate_companion("agents-init", [], env=self.env), self.resolve().path)


class RunningFromTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.env = agenthelm.Env(home=self.root / "home", variables={})

    def where(self, path: Path, env=None) -> str:
        with mock.patch.object(agenthelm_runtime, "__file__", str(path)):
            return agenthelm_runtime.running_from(env or self.env)

    def test_values_are_unchanged_for_each_kind_of_location(self) -> None:
        home = self.env.home
        script = "plugins/agent-pack/runtime/agenthelm_runtime.py"
        self.assertEqual(self.where(home / ".local/share/agenthelm/runtimes/6.14.0" / script), "runtime")
        self.assertEqual(self.where(home / ".claude/plugins/cache/agenthelm/agent-pack/6.14.0/runtime/x.py"), "client-plugin")
        self.assertEqual(self.where(home / ".codex/plugins/cache/agenthelm/agent-pack/6.14.0/runtime/x.py"), "client-plugin")
        self.assertEqual(self.where(home / ".cursor/plugins/local/agent-pack/runtime/x.py"), "client-plugin")
        self.assertEqual(self.where(home / "lib/node_modules/@scope/agenthelm/runtime/x.py"), "npm")
        self.assertEqual(self.where(home / ".npm/_npx/abc/node_modules/pkg/runtime/x.py"), "npm")
        self.assertEqual(self.where(home / "src/agenthelm" / script), "checkout")

    def test_the_config_directory_variables_are_honoured(self) -> None:
        elsewhere = self.root / "cfg/claude/plugins/cache/agenthelm/agent-pack/6.14.0/runtime/x.py"
        self.assertEqual(self.where(elsewhere), "checkout")
        env = agenthelm.Env(home=self.env.home, variables={"CLAUDE_CONFIG_DIR": str(self.root / "cfg/claude")})
        self.assertEqual(self.where(elsewhere, env), "client-plugin")

    def test_other_plugin_caches_and_lookalike_directories_are_not_client_plugins(self) -> None:
        home = self.env.home
        self.assertEqual(self.where(home / ".claude/plugins/cache/other-market/p/1/runtime/x.py"), "checkout")
        self.assertEqual(self.where(home / "projects/plugins/cache/agenthelm/p/1/x.py"), "checkout")


class DriverFieldTests(unittest.TestCase):
    def test_builtin_drivers_declare_their_caches(self) -> None:
        caches = {item["id"]: item["runtime_cache"] for item in registry.BUILTIN}
        self.assertEqual(caches["claude"], [{"base": "~/.claude", "env": "CLAUDE_CONFIG_DIR",
                                             "glob": "plugins/cache/agenthelm/<plugin>/*"}])
        self.assertEqual(caches["codex"], [{"base": "~/.codex", "env": "CODEX_HOME",
                                            "glob": "plugins/cache/agenthelm/<plugin>/*"}])
        self.assertEqual(caches["cursor"], [{"base": "~/.cursor", "glob": "plugins/local/<plugin>"}])
        self.assertEqual(caches["claude-desktop"], [])
        self.assertEqual(caches["gemini"], [])  # an extension directory is signed by the client: not a runtime to run from

    def test_validation_rejects_malformed_runtime_cache(self) -> None:
        base = json.loads(json.dumps(registry.get("cursor")))
        for bad in ("x", [{"base": "~/.x"}], [{"base": "~/.x", "glob": "../up", "env": None}],
                    [{"base": "rel", "glob": "a"}], [{"base": "~/.x", "glob": "/abs"}],
                    [{"base": "~/.x", "glob": "a", "extra": 1}]):
            with self.subTest(bad=bad), self.assertRaises(registry.DriverDataError):
                registry.validate_builtin({**base, "runtime_cache": bad}, "t.json")
        registry.validate_builtin({**base, "runtime_cache": []}, "t.json")
        registry.validate_builtin({key: value for key, value in base.items() if key != "runtime_cache"}, "t.json")


if __name__ == "__main__":
    unittest.main()
