"""Select the feature or release gate from the GitHub event, validating the real source commit."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import release_meta


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def revision(root, value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40,64}', value) or set(value) == {'0'}:
        raise ValueError(f'{label}: a non-zero commit SHA is required')
    git(root, 'cat-file', '-e', value + '^{commit}')
    return value


def prepared_release(root, before, after):
    paths = git(root, 'diff', '--name-only', before, after, '--').splitlines()
    return any(path == 'release/candidate.json' or re.fullmatch(
        r'release/history/[0-9]+\.[0-9]+\.[0-9]+/plan\.json', path) for path in paths)


def select_gate(root: Path, event_name: str, event: dict):
    if event_name == 'pull_request':
        pull = event['pull_request']
        base = revision(root, pull['base']['sha'], 'pull_request.base.sha')
        head = revision(root, pull['head']['sha'], 'pull_request.head.sha')
        common = git(root, 'merge-base', base, head)
        target, source = pull['base']['ref'], pull['head']['ref']
        if target == 'main' and common != base:
            raise ValueError('main promotion requires a fast-forward; integrate the current main base first')
        if target == 'main' or target.startswith('release/') or source.startswith('release/') and target == 'dev':
            return {'kind': 'release', 'head': head, 'base': common, 'target': target, 'source': source}
        return {'kind': 'feature', 'head': head, 'base': common, 'target': target, 'source': source}
    if event_name == 'push':
        head = revision(root, event['after'], 'push.after')
        ref = event['ref']
        if not isinstance(ref, str) or not ref.startswith('refs/heads/'):
            raise ValueError('push.ref must name a branch')
        branch = ref.removeprefix('refs/heads/')
        raw_before = event.get('before')
        creating = isinstance(raw_before, str) and re.fullmatch(r'0{40}|0{64}', raw_before)
        if branch == 'main' or branch.startswith('release/'):
            # Creating a release branch has no previous tip; its gate validates the
            # entire prepared release, so it does not need an incremental diff.
            before = None if creating else revision(root, raw_before, 'push.before')
            return {'kind': 'release', 'head': head, 'base': before, 'target': branch, 'source': branch}
        before = revision(root, raw_before, 'push.before')
        if branch == 'dev' and prepared_release(root, before, head):
            return {'kind': 'release', 'head': head, 'base': before, 'target': branch, 'source': branch}
        return {'kind': 'feature', 'head': head, 'base': before, 'target': branch, 'source': branch}
    raise ValueError(f'unsupported CI event: {event_name}')


@contextmanager
def source_tree(root: Path, head: str):
    # GitHub checks out a synthetic PR merge. Inspect the actual PR head so that
    # upstream-only changes after the fork point cannot be charged to this PR.
    with tempfile.TemporaryDirectory(prefix='agenthelm-ci-gate-') as temporary:
        checkout = Path(temporary) / 'source'
        git(root, 'worktree', 'add', '--detach', str(checkout), head)
        try:
            yield checkout
        finally:
            git(root, 'worktree', 'remove', '--force', str(checkout))


def run(root: Path, event_name: str, event: dict) -> int:
    selected = select_gate(root, event_name, event)
    print(f"{selected['kind']} gate: {selected['head']} against {selected['base']}")
    with source_tree(root, selected['head']) as checkout:
        previous = release_meta.ROOT
        release_meta.ROOT = checkout
        try:
            if selected['kind'] == 'feature':
                return release_meta.main(['check-changes', '--base', selected['base']])
            suite = release_meta.read_json(checkout / 'index.json')['plugin']['version']
            if event_name == 'pull_request' and selected['target'] == 'main' and selected['source'] != f'release/v{suite}':
                raise ValueError(f'PRs to main must come from release/v{suite}')
            if selected['source'].startswith('release/') and selected['source'] != f'release/v{suite}':
                raise ValueError(f'release branch must be named release/v{suite}')
            return release_meta.main(['check-release'])
        finally:
            release_meta.ROOT = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-name', default=os.environ.get('GITHUB_EVENT_NAME'))
    parser.add_argument('--event-path', type=Path, default=os.environ.get('GITHUB_EVENT_PATH'))
    args = parser.parse_args(argv)
    try:
        if not args.event_path:
            raise ValueError('GITHUB_EVENT_PATH or --event-path is required')
        return run(Path(__file__).resolve().parents[1], args.event_name, json.loads(args.event_path.read_text()))
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
