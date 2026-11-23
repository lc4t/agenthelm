"""The ``latest.json`` migration window, consumer side (#28): below its floor a plugin reads the full migrations file.

A real local HTTP server stands in for the Pages / raw sources. Nothing here talks to the network.
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"

import http.server
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import agenthelm  # noqa: E402

GAP = "窗口外的迁移说明暂不可用，请查看 CHANGELOG.md 或联网重试"


def entry(start: str, end: str, *, breaking: bool = False, plugin: str = "p") -> dict:
    return {"plugin": plugin, "from": start, "to": end, "change_kind": "code", "breaking": breaking, "date": "2026-01-01",
            "summary": f"full {start}->{end}", "actions": [f"act {end}"]}


def index_of(item: dict) -> dict:
    """The index entry a window leaves outside it (plugin/from/to/change_kind/breaking/date, short summary if breaking)."""
    result = {key: item[key] for key in ("plugin", "from", "to", "change_kind", "breaking", "date")}
    if item["breaking"]:
        result.update(summary="short note", actions=[])
    return result


FULL = [entry("1.0.0", "1.1.0", breaking=True), entry("1.1.0", "1.2.0"), entry("1.2.0", "1.3.0", breaking=True),
        entry("1.3.0", "1.4.0"), entry("1.4.0", "1.5.0")]
WINDOW = {"schema": 1, "floor": {"p": "1.3.0"}, "full": "release/migrations.json"}


def latest_doc(*, window: dict | None = WINDOW, migrations: list | None = None) -> dict:
    doc = {"schema": 1, "marketplace": "agenthelm", "released_at": "2026-02-01", "plugins": {"p": {"version": "1.5.0"}},
           "migrations": migrations if migrations is not None else
           [index_of(item) if item["to"] in ("1.1.0", "1.2.0", "1.3.0") else item for item in FULL]}
    if window is not None:
        doc["migrations_window"] = window
    return doc


class Server:
    """Serves ``routes`` (path -> (status, bytes)) and counts the paths it was asked for."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes, self.hits = routes, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits.append(self.path)
                status, body = outer.routes.get(self.path, (404, b"{}"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(data) -> tuple[int, bytes]:
    return 200, json.dumps(data).encode()


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.env = agenthelm.Env(home=self.home, variables={})
        self.servers: list[Server] = []
        patch = mock.patch.object(agenthelm, "DEFAULT_LATEST_URLS", ())  # never reach the real sources
        patch.start()
        self.addCleanup(patch.stop)

    def serve(self, routes) -> Server:
        server = Server(routes)
        self.servers.append(server)
        self.addCleanup(server.close)
        return server

    def full(self, server: Server, latest: dict | None = None, **kwargs):
        latest = latest or latest_doc()
        return agenthelm.load_full_migrations(self.env, latest, {"source": server.base + "/latest.json"}, None, **kwargs)

    def findings(self, server: Server, current: str, *, latest: dict | None = None, **kwargs) -> dict:
        latest = latest or latest_doc()
        item = [{"client": "claude", "plugin": "p", "version": current}]
        return agenthelm.plugin_findings(latest, item, lambda: self.full(server, latest, **kwargs))[0]

    # -- no window: today's behaviour
    def test_a_latest_without_a_window_is_read_exactly_as_before(self) -> None:
        latest = latest_doc(window=None, migrations=FULL)
        server = self.serve({})
        finding = self.findings(server, "1.0.0", latest=latest)
        self.assertEqual(finding["steps"], [{"to": item["to"], "change_kind": "code", "breaking": item["breaking"],
                                             "summary": item["summary"], "actions": item["actions"]} for item in FULL])
        self.assertTrue(finding["breaking"])
        self.assertEqual(server.hits, [])
        self.assertEqual(finding["steps"], agenthelm.plugin_findings(latest, [{"client": "claude", "plugin": "p",
                                                                              "version": "1.0.0"}])[0]["steps"])

    def test_a_malformed_or_newer_schema_window_is_ignored(self) -> None:
        for window in ({"schema": 2, "floor": {"p": "9.0.0"}, "full": "release/migrations.json"},
                       {"schema": 1, "floor": ["p"], "full": "release/migrations.json"},
                       {"schema": 1, "floor": {"p": "9.0.0"}, "full": "https://evil.example/m.json"},
                       {"schema": 1, "floor": {"p": "9.0.0"}, "full": "../up.json"},
                       {"schema": 1, "floor": {"p": "9.0.0"}, "full": "/abs.json"}, "text", 3):
            with self.subTest(window=window):
                latest = latest_doc(window=window, migrations=FULL)
                server = self.serve({})
                finding = self.findings(server, "1.0.0", latest=latest)
                self.assertEqual(len(finding["steps"]), 5)
                self.assertFalse(any(step.get("gap") for step in finding["steps"]))
                self.assertEqual(server.hits, [])

    # -- below the floor: the full file
    def test_below_the_floor_the_full_text_is_fetched_from_the_latest_json_prefix(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        finding = self.findings(server, "1.0.0")
        self.assertEqual([step["to"] for step in finding["steps"]], ["1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"])
        self.assertEqual(finding["steps"][0]["summary"], "full 1.0.0->1.1.0")
        self.assertEqual(finding["steps"][0]["actions"], ["act 1.1.0"])
        self.assertTrue(finding["breaking"])
        self.assertEqual(server.hits, ["/release/migrations.json"])

    def test_at_or_above_the_floor_nothing_is_fetched(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        for current in ("1.3.0", "1.4.0"):
            finding = self.findings(server, current)
            self.assertEqual([step["to"] for step in finding["steps"]], ["1.4.0", "1.5.0"] if current == "1.3.0" else ["1.5.0"])
        self.assertEqual(server.hits, [])

    def test_only_a_behind_plugin_is_fetched(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        for current in ("1.5.0", "1.6.0"):  # current, ahead
            self.assertEqual(self.findings(server, current)["steps"], [])
        self.assertEqual(server.hits, [])

    def test_a_second_run_the_same_day_uses_the_cache(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        self.findings(server, "1.0.0")
        self.findings(server, "1.0.0")
        self.assertEqual(server.hits, ["/release/migrations.json"])

    def test_a_cache_older_than_a_day_or_for_other_plugin_versions_is_refetched(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        self.full(server)
        cache = self.env.cache_dir / agenthelm.MIGRATIONS_FULL_CACHE
        data = json.loads(cache.read_text())
        data["fetched_at"] = time.time() - 2 * 24 * 3600
        cache.write_text(json.dumps(data))
        self.full(server)
        self.assertEqual(len(server.hits), 2)
        newer = latest_doc()
        newer["plugins"]["p"]["version"] = "1.6.0"
        self.full(server, newer)
        self.assertEqual(len(server.hits), 3)

    def test_the_second_source_is_tried_when_the_first_has_no_such_file(self) -> None:
        first, second = self.serve({}), self.serve({"/release/migrations.json": ok(FULL)})
        urls = (first.base + "/latest.json", second.base + "/latest.json")
        with mock.patch.object(agenthelm, "DEFAULT_LATEST_URLS", urls):
            entries = agenthelm.load_full_migrations(self.env, latest_doc(), {"source": urls[0]}, None)
        self.assertEqual(entries, FULL)
        self.assertEqual((first.hits, second.hits), (["/release/migrations.json"], ["/release/migrations.json"]))

    def test_a_local_latest_json_reads_the_sibling_file(self) -> None:
        (self.home / "release").mkdir()
        (self.home / "release/migrations.json").write_text(json.dumps(FULL))
        entries = agenthelm.load_full_migrations(self.env, latest_doc(), {"source": str(self.home / "latest.json")}, None)
        self.assertEqual(entries, FULL)

    # -- failures: a gap, never "no migration"
    def assert_gap(self, finding: dict) -> None:
        gaps = [step for step in finding["steps"] if step.get("gap")]
        self.assertEqual(len(gaps), 1, finding["steps"])
        self.assertEqual(gaps[0]["summary"], GAP)
        self.assertTrue(gaps[0]["breaking"])
        self.assertTrue(finding["breaking"])
        self.assertEqual(gaps[0]["actions"], [])

    def test_both_sources_404(self) -> None:
        server = self.serve({})
        finding = self.findings(server, "1.0.0")
        self.assert_gap(finding)
        self.assertIn("1.4.0", [step["to"] for step in finding["steps"]])  # the window's own steps are still reported

    def test_invalid_json_not_an_array_and_a_non_object_entry_are_gaps(self) -> None:
        for body in (b"{not json", b"[1, 2]", b'"text"', b"null", b'{"migrations": 3}', b"\xff\xfe", b""):
            with self.subTest(body=body):
                self.setUp()
                server = self.serve({"/release/migrations.json": (200, body)})
                self.assert_gap(self.findings(server, "1.0.0"))

    def test_an_object_carrying_the_list_is_accepted(self) -> None:
        server = self.serve({"/release/migrations.json": ok({"migrations": FULL})})
        self.assertEqual(self.full(server), FULL)

    def test_an_oversized_response_is_a_gap(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        with mock.patch.object(agenthelm, "MIGRATIONS_FULL_MAX_BYTES", 100):
            self.assert_gap(self.findings(server, "1.0.0"))

    def test_a_connection_error_is_a_gap(self) -> None:
        server = self.serve({})
        server.close()
        self.assert_gap(self.findings(server, "1.0.0"))

    def test_offline_without_a_cache_is_a_gap_and_does_not_connect(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        self.assert_gap(self.findings(server, "1.0.0", offline=True))
        self.assertEqual(server.hits, [])

    def test_offline_with_a_cache_uses_it_even_when_old(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        self.full(server)
        cache = self.env.cache_dir / agenthelm.MIGRATIONS_FULL_CACHE
        data = json.loads(cache.read_text())
        data["fetched_at"] = 1
        cache.write_text(json.dumps(data))
        finding = self.findings(server, "1.0.0", offline=True)
        self.assertEqual(finding["steps"][0]["summary"], "full 1.0.0->1.1.0")
        self.assertEqual(len(server.hits), 1)

    def test_a_failed_refetch_falls_back_to_the_stale_cache(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        self.full(server)
        server.routes.clear()
        cache = self.env.cache_dir / agenthelm.MIGRATIONS_FULL_CACHE
        data = json.loads(cache.read_text())
        data["fetched_at"] = 1
        cache.write_text(json.dumps(data))
        self.assertEqual(self.full(server), FULL)

    def test_a_corrupt_cache_is_ignored(self) -> None:
        server = self.serve({"/release/migrations.json": ok(FULL)})
        cache = self.env.cache_dir / agenthelm.MIGRATIONS_FULL_CACHE
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{broken")
        self.assertEqual(self.full(server), FULL)

    def test_a_cache_from_another_source_is_not_used_offline(self) -> None:
        other = self.serve({"/release/migrations.json": ok(FULL)})
        self.full(other)
        server = self.serve({})
        self.assertIsNone(self.full(server, offline=True))

    def test_a_recent_failure_backs_off_in_the_hook(self) -> None:
        server = self.serve({})
        self.full(server, backoff=True)
        self.full(server, backoff=True)
        self.assertEqual(len(server.hits), 1)  # the failed attempt is not repeated while the back-off lasts

    # -- index entries and rendering
    def test_index_entries_outside_the_window_report_without_none(self) -> None:
        server = self.serve({})
        finding = self.findings(server, "1.0.0")
        by_to = {step["to"]: step for step in finding["steps"]}
        self.assertIsNone(by_to["1.2.0"]["summary"])  # an index entry carries no summary unless it is breaking
        self.assertEqual(by_to["1.3.0"]["summary"], "short note")
        self.assertEqual(by_to["1.3.0"]["actions"], [])

    def check_text(self, routes: dict, latest: dict, *, offline: bool = False, server: Server | None = None) -> tuple[str, Server]:
        """``check`` end to end (run_check + render_text) for one installed ``p`` 1.0.0 against a served latest.json."""
        server = server or self.serve({"/latest.json": ok(latest), **routes})
        env = agenthelm.Env(home=self.home, variables={})
        installed = [{"client": "claude", "plugin": "p", "version": "1.0.0", "path": "x"}]
        with mock.patch.object(agenthelm, "installed_plugins", return_value=installed), \
                mock.patch.object(agenthelm, "github_release", return_value={}), \
                mock.patch.object(agenthelm, "known_cli_locations", return_value=[]):
            report = agenthelm.run_check(env, None, latest_source=server.base + "/latest.json", offline=offline)
            return agenthelm.render_text(report), server

    def test_check_below_the_floor_prints_the_full_notes(self) -> None:
        text, server = self.check_text({"/release/migrations.json": ok(FULL)}, latest_doc())
        self.assertIn("1.1.0 不兼容：full 1.0.0->1.1.0\n        - act 1.1.0", text)
        self.assertIn("1.3.0 不兼容：full 1.2.0->1.3.0\n        - act 1.3.0", text)
        self.assertNotIn("None", text)
        self.assertNotIn(GAP, text)
        self.assertEqual(server.hits, ["/latest.json", "/release/migrations.json"])

    def test_check_with_the_full_file_missing_names_the_gap_once_and_still_shows_the_window(self) -> None:
        text, _ = self.check_text({}, latest_doc())
        self.assertEqual(text.count(GAP), 1)
        self.assertIn("（含不兼容变更）", text)
        self.assertNotIn("None", text)
        self.assertNotIn("同上", text)

    def test_check_offline_without_a_cache_names_the_gap(self) -> None:
        # latest.json comes from the cache the first run wrote; the full file was never fetched
        _, server = self.check_text({}, latest_doc())
        hits = list(server.hits)
        text, _ = self.check_text({}, latest_doc(), offline=True, server=server)
        self.assertIn(GAP, text)
        self.assertEqual(server.hits, hits)

    def test_a_breaking_index_entry_without_a_summary_prints_no_none(self) -> None:
        bare = [{key: item[key] for key in ("plugin", "from", "to", "change_kind", "breaking", "date")} for item in FULL]
        text, _ = self.check_text({}, latest_doc(window=None, migrations=bare))
        self.assertIn("1.1.0 不兼容", text)
        self.assertNotIn("None", text)
        self.assertNotIn("不兼容：", text)


if __name__ == "__main__":
    unittest.main()
