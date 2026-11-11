"""Cursor updates use verified release archives without a local source checkout (#47)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import agenthelm  # noqa: E402

SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class CursorReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.latest_file = self.root / "latest.json"
        self.latest = {"schema": 1, "repository": "https://github.com/example/agenthelm", "migrations": [],
                       "plugins": {"agent-pack": {"version": "2.0.0"}, "opinion-manager": {"version": "3.0.0"}}}
        write(self.latest_file, self.latest)
        self.release = {"tag": "v9.0.0", "commit": "a" * 40,
                        "tarball_url": "https://github.com/example/agenthelm/releases/download/v9.0.0/agenthelm-v9.0.0.tar.gz"}
        self.archive = self.make_archive()
        self.release["tarball_sha256"] = hashlib.sha256(self.archive).hexdigest()
        for name in self.latest["plugins"]:
            write(self.home / ".cursor/plugins/local" / name / "plugin.json",
                  {"$schema": SCHEMA, "name": name, "version": "1.0.0"})
        patch = mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en", "HOME": str(self.home)}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.fetch = mock.patch.object(agenthelm, "_fetch_json", side_effect=self.fetch_json).start()
        self.addCleanup(mock.patch.stopall)
        self.download = mock.patch.object(agenthelm.urllib.request, "urlopen", side_effect=self.urlopen).start()
        mock.patch.object(agenthelm, "known_cli_locations", return_value=[]).start()

    def make_archive(self, wrong_version: bool = False, wrong_identity: bool = False) -> bytes:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as bundle:
            for name, entry in self.latest["plugins"].items():
                manifest = json.dumps({"$schema": SCHEMA,
                                       "name": "unexpected-plugin" if wrong_identity and name == "opinion-manager" else name,
                                       "version": "0.0.0" if wrong_version and name == "opinion-manager" else entry["version"]}).encode()
                info = tarfile.TarInfo(f"agenthelm-v9.0.0/plugins/{name}/plugin.json")
                info.size = len(manifest)
                bundle.addfile(info, io.BytesIO(manifest))
        return data.getvalue()

    def fetch_json(self, url: str, timeout: float = 0) -> dict:
        if url.endswith("/releases/latest/download/release.json"):
            return self.release.copy()
        if url == "https://raw.githubusercontent.com/example/agenthelm/v9.0.0/latest.json":
            return self.latest.copy()
        raise AssertionError(f"Unexpected network access: {url}")

    def urlopen(self, request, timeout: float = 0):
        self.assertEqual(request.full_url, self.release["tarball_url"])
        return io.BytesIO(self.archive)

    def cli(self, command: str, *flags: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = agenthelm.main([command, "--home", str(self.home), "--latest", str(self.latest_file),
                                   "--client", "cursor", "--no-project", "--output", "json", *flags])
        return code, json.loads(output.getvalue())

    def installed(self) -> dict[str, bytes]:
        return {p.relative_to(self.home).as_posix(): p.read_bytes()
                for p in (self.home / ".cursor").rglob("*") if p.is_file()}

    def test_remote_fallback_previews_without_writes_and_applies_with_version_readback(self) -> None:
        code, check = self.cli("check")
        self.assertEqual((code, check["summary"]["plugins_behind"], check["summary"]["plugins_unavailable"]), (0, 2, 0))
        before = self.installed()
        code, preview = self.cli("update")
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["steps"][0]["status"], "preview")
        self.assertEqual(self.installed(), before)
        # Only the check + update each resolve the raw fallback once; the preview uses the pinned result.
        self.assertEqual(self.fetch.call_count, 4)
        code, applied = self.cli("update", "--apply")
        self.assertEqual(code, 0, applied)
        step = applied["steps"][0]
        self.assertEqual(step["status"], "applied")
        self.assertEqual({c["plugin"]: c["after"] for c in step["changes"]}, {"agent-pack": "2.0.0", "opinion-manager": "3.0.0"})
        self.assertTrue(all(c["changed"] for c in step["changes"]))
        self.assertEqual(self.cli("check")[1]["summary"]["plugins_behind"], 0)

    def test_bad_digest_and_wrong_manifest_write_neither_plugin(self) -> None:
        before = self.installed()
        self.release["tarball_sha256"] = "0" * 64
        code, payload = self.cli("update", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("digest", payload["steps"][0]["error"].lower())
        self.assertEqual(self.installed(), before)
        self.archive = self.make_archive(wrong_version=True)
        self.release["tarball_sha256"] = hashlib.sha256(self.archive).hexdigest()
        code, payload = self.cli("update", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("opinion-manager", payload["steps"][0]["error"])
        self.assertEqual(self.installed(), before)

    def test_wrong_plugin_identity_is_rejected_before_any_client_write(self) -> None:
        # The second plugin's identity is wrong despite its correct version.
        # No earlier plugin may be installed, nor may the wrong name choose a destination.
        before = self.installed()
        self.archive = self.make_archive(wrong_identity=True)
        self.release["tarball_sha256"] = hashlib.sha256(self.archive).hexdigest()
        code, payload = self.cli("update", "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(payload["steps"][0]["status"], "failed")
        self.assertIn("identifies as unexpected-plugin", payload["steps"][0]["error"])
        self.assertEqual(self.installed(), before)
        self.assertFalse((self.home / ".cursor/plugins/local/unexpected-plugin").exists())

    def test_local_source_with_spaces_is_available_in_the_summary_and_read_back(self) -> None:
        source = self.root / "local checkout with spaces"
        for name, entry in self.latest["plugins"].items():
            write(source / "plugins" / name / "plugin.json", {"$schema": SCHEMA, "name": name, "version": entry["version"]})
        self.fetch.side_effect = OSError("offline")
        code, payload = self.cli("update", "--source", str(source), "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"]["plugins_unavailable"], 0)
        self.assertEqual(payload["summary"]["plugins_behind"], 2)
        self.assertEqual(payload["steps"][0]["status"], "applied")
        self.download.assert_not_called()
        self.fetch.assert_not_called()

    def test_partial_install_names_missing_plugins_and_the_completion_command(self) -> None:
        import shutil
        shutil.rmtree(self.home / ".cursor/plugins/local/opinion-manager")
        code, payload = self.cli("check")
        self.assertEqual(code, 0)
        cursor = payload["install_state"]["clients"]["cursor"]
        self.assertEqual(cursor["state"], "partial")
        self.assertEqual(cursor["missing"], ["opinion-manager"])
        self.assertIn("opinion-manager", cursor["summary"])
        self.assertIn("install --client cursor", cursor["next_command"])

    def text_cli(self, *flags: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = agenthelm.main(["update", "--home", str(self.home), "--latest", str(self.latest_file),
                                   "--client", "cursor", "--no-project", "--output", "text", *flags])
        return code, output.getvalue()

    def test_update_says_where_the_replaced_plugin_directories_are_backed_up(self) -> None:
        """#80：预览写将备份到哪里，apply 写实际路径；--recovery-dir 对 Cursor 步骤同样生效。"""
        default = self.home / ".agent-pack" / "recovery"
        code, preview = self.text_cli()
        self.assertEqual(code, 0, preview)
        self.assertIn(f"{default}/<timestamp>-cursor-agent-pack/replaced-plugin/agent-pack", preview)
        self.assertIn("the timestamp is fixed when it runs", preview)
        self.assertFalse(default.exists())
        chosen = self.root / "chosen"
        code, preview = self.text_cli("--recovery-dir", str(chosen))
        self.assertIn(f"{chosen}/<timestamp>-cursor-opinion-manager/replaced-plugin/opinion-manager", preview)
        code, applied = self.text_cli("--recovery-dir", str(chosen), "--apply")
        self.assertEqual(code, 0, applied)
        for name in self.latest["plugins"]:
            (line,) = [text for text in applied.splitlines() if f"replaced {name} directory" in text]
            where = Path(line.split("to: ", 1)[1])
            self.assertEqual(where.parent.name, "replaced-plugin")
            self.assertTrue(where.is_relative_to(chosen), where)
            self.assertEqual(json.loads((where / "plugin.json").read_text())["version"], "1.0.0")  # the old copy is really there
        self.assertFalse(default.exists(), "--recovery-dir keeps the default recovery folder untouched")
        code, again = self.text_cli("--apply")  # same versions now: nothing to replace, so nothing to back up
        self.assertNotIn("replaced", again)

    def test_preview_without_runnable_steps_does_not_tell_the_user_to_apply(self) -> None:
        """#97：全部不可用/失败的预览不提示追加 --apply；部分可执行时只说明 --apply 跳过的步骤。"""
        self.fetch.side_effect = OSError("offline")  # no release block reachable: the Cursor step is unavailable
        code, text = self.text_cli()
        self.assertIn("The preview has no runnable steps: 0 blocked, 1 unavailable, 0 failed", text)
        self.assertNotIn("add --apply to run it", text)
        self.fetch.side_effect = self.fetch_json
        self.release["tarball_sha256"] = "0" * 64  # digest mismatch: the action raises, so the step is failed
        code, text = self.text_cli()
        self.assertEqual(code, 1)
        self.assertIn("The preview has no runnable steps: 0 blocked, 0 unavailable, 1 failed", text)
        self.assertNotIn("add --apply to run it", text)

    def test_preview_summary_wording_for_mixed_and_zh_results(self) -> None:
        def preview(*statuses: str, lang: str = "en") -> str:
            records = [{"status": status, "description": f"step {index}"} for index, status in enumerate(statuses)]
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang}), redirect_stdout(output):
                agenthelm._print_steps(records, False)
            return output.getvalue().splitlines()[-1]
        self.assertIn("some steps are unavailable or failed", preview("preview", "unavailable"))
        self.assertIn("--apply runs only the remaining runnable steps", preview("preview", "failed", "unavailable"))
        self.assertIn("0 blocked, 2 unavailable, 1 failed", preview("unavailable", "unavailable", "failed"))
        self.assertIn("1 blocked, 1 unavailable, 0 failed", preview("blocked", "unavailable"))
        self.assertEqual(preview("preview", "preview"), "The above is a preview; add --apply to run it.")
        self.assertIn("没有可以执行的步骤：0 个步骤阻塞、2 个不可用、0 个失败", preview("unavailable", "unavailable", lang="zh"))
        self.assertIn("其中有步骤不可用或失败", preview("preview", "failed", lang="zh"))
        self.assertIn("确认后追加 --apply 执行", preview("preview", lang="zh"))


if __name__ == "__main__":
    unittest.main()
