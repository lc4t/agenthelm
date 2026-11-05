from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_release", ROOT / "scripts/validate_release.py")
release_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_gate)


class ReleaseArtifactGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = Path(temporary.name) / "archive.tar.gz"
        self.latest = {"repository": "https://github.com/example/agenthelm",
                       "plugins": {"agent-pack": {"version": "2.0.0"}}}
        self.release = {"tag": "v9.0.0-rc.1", "commit": "a" * 40,
                        "tarball_url": "https://github.com/example/agenthelm/releases/download/v9.0.0-rc.1/agenthelm-v9.0.0-rc.1.tar.gz"}
        manifest = json.dumps({"name": "agent-pack", "version": "2.0.0"}).encode()
        with tarfile.open(self.archive, "w:gz") as bundle:
            root = tarfile.TarInfo("agenthelm-v9.0.0-rc.1")
            root.type = tarfile.DIRTYPE
            bundle.addfile(root)
            info = tarfile.TarInfo("agenthelm-v9.0.0-rc.1/plugins/agent-pack/plugin.json")
            info.size = len(manifest)
            bundle.addfile(info, io.BytesIO(manifest))
        self.release["tarball_sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def validate(self, release: dict | None = None, **options) -> None:
        release_gate.validate(self.latest, self.release if release is None else release, self.archive,
                              tag="v9.0.0-rc.1", commit="a" * 40, **options)

    def test_candidate_artifacts_pass_when_they_match_the_advertised_versions(self) -> None:
        self.validate()

    def test_every_cursor_manifest_field_is_required_and_checked(self) -> None:
        for field in ("tag", "commit", "tarball_url", "tarball_sha256"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate({key: value for key, value in self.release.items() if key != field})

    def test_digest_and_manifest_version_mismatch_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match the archive"):
            self.validate({**self.release, "tarball_sha256": "0" * 64})
        self.latest["plugins"]["agent-pack"]["version"] = "2.1.0"
        with self.assertRaisesRegex(ValueError, "advertised version"):
            self.validate()

    def test_forked_or_renamed_repository_uses_the_publishing_repository(self) -> None:
        fork = {**self.release, "tarball_url": "https://github.com/fork-owner/agenthelm-renamed/releases/download/v9.0.0-rc.1/agenthelm-v9.0.0-rc.1.tar.gz"}
        with self.assertRaisesRegex(ValueError, "tagged archive"):
            self.validate(fork)  # latest.json still names example/agenthelm
        self.validate(fork, repository="fork-owner/agenthelm-renamed")
        with self.assertRaisesRegex(ValueError, "tagged archive"):  # the URL must still name the publishing repository
            self.validate(self.release, repository="fork-owner/agenthelm-renamed")
        for bad in ("fork-owner", "a/b/c", "x y/z", "../x/y"):
            with self.subTest(repository=bad), self.assertRaisesRegex(ValueError, "owner/name"):
                self.validate(repository=bad)

    def test_cli_defaults_to_github_repository_from_the_environment(self) -> None:
        import os
        import subprocess
        import sys
        root = self.archive.parent
        (root / "latest.json").write_text(json.dumps(self.latest))
        fork = {**self.release, "tarball_url": "https://github.com/fork-owner/agenthelm/releases/download/v9.0.0-rc.1/agenthelm-v9.0.0-rc.1.tar.gz"}
        (root / "release.json").write_text(json.dumps(fork))
        command = [sys.executable, str(ROOT / "scripts/validate_release.py"), str(root / "latest.json"), str(root / "release.json"),
                   str(self.archive), "--tag", "v9.0.0-rc.1", "--commit", "a" * 40]
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_REPOSITORY"}
        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(command, env={**env, "GITHUB_REPOSITORY": "fork-owner/agenthelm"},
                                        capture_output=True).returncode, 0)

    def test_release_workflow_gates_the_artifacts_before_publication(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertLess(workflow.index("scripts/validate_release.py"), workflow.index("gh release create"))
        self.assertIn('--tag "$GITHUB_REF_NAME" --commit "$GITHUB_SHA" --repository "$GITHUB_REPOSITORY"', workflow)


if __name__ == "__main__":
    unittest.main()
