"""Dry-run promotion checks for a release tag (design: docs/design/release-hardening.md, section 1.4).

The pure functions take facts that were already collected; `collect` and `main` hold all git,
GitHub CLI and npm calls. Nothing here pushes, tags or dispatches anything.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = '@sakanano-moe/agenthelm'
TAG_RE = re.compile(r'v(\d+\.\d+\.\d+)')


@dataclass
class Facts:
    """Everything the checks need; None means the value could not be obtained."""
    tag: str
    suite_version: str | None = None
    tag_commit: str | None = None
    branch_tip: str | None = None
    main_commit: str | None = None
    main_is_ancestor: bool | None = None
    tests_conclusion: str | None = None
    release_commit: str | None = None
    npm_version: str | None = None


@dataclass
class Result:
    key: str
    title: str
    ok: bool
    detail: str
    noop: bool = False


def _compare(key, title, actual, expected, label):
    ok = actual is not None and actual == expected
    shown = 'unavailable' if actual is None else actual
    return Result(key, title, ok, f'{label}: actual={shown} expected={expected}')


def check_tag(facts):
    title = 'tag is a final vX.Y.Z equal to the suite version'
    if not TAG_RE.fullmatch(facts.tag):
        return Result('a', title, False, f'tag {facts.tag!r}: expected vX.Y.Z (no -rc or other suffix)')
    expected = f'v{facts.suite_version}' if facts.suite_version else None
    return _compare('a', title, facts.tag if facts.suite_version else None, expected, 'tag vs index.json suite version')


def check_branch_tip(facts):
    return _compare('b', 'tag commit is the tip of origin/release/<tag>', facts.branch_tip,
                    facts.tag_commit, f'origin/release/{facts.tag} tip vs tag commit')


def check_main(facts):
    title = 'main can fast-forward to the tag commit'
    if facts.main_commit is not None and facts.main_commit == facts.tag_commit:
        return Result('c', title, True, 'main already equals the tag commit: no-op', noop=True)
    ok = facts.main_is_ancestor is True
    return Result('c', title, ok, f'main {facts.main_commit or "unavailable"} is'
                  f'{"" if ok else " not"} an ancestor of {facts.tag_commit or "unavailable"}')


def check_tests(facts):
    return _compare('d', 'tests.yml push run on the commit succeeded', facts.tests_conclusion,
                    'success', 'conclusion')


def check_release(facts):
    return _compare('e', 'GitHub Release exists and release.json commit matches', facts.release_commit,
                    facts.tag_commit, 'release.json commit vs tag commit')


def check_npm(facts):
    expected = facts.tag[1:] if TAG_RE.fullmatch(facts.tag) else None
    return _compare('f', f'{PACKAGE} version is on npm', facts.npm_version, expected, 'npm view version')


def evaluate(facts):
    first = check_tag(facts)
    if not first.ok:
        # Later checks would run git and npm with an unvalidated tag; report them as skipped.
        rest = [Result(k, t, False, 'skipped: check a failed') for k, t in (
            ('b', 'tag commit is the tip of origin/release/<tag>'),
            ('c', 'main can fast-forward to the tag commit'),
            ('d', 'tests.yml push run on the commit succeeded'),
            ('e', 'GitHub Release exists and release.json commit matches'),
            ('f', f'{PACKAGE} version is on npm'))]
        return [first, *rest]
    return [first, check_branch_tip(facts), check_main(facts), check_tests(facts),
            check_release(facts), check_npm(facts)]


def push_command(tag):
    return f'git push origin {tag}:main'


def render(facts, results):
    lines = [f'promote dry run for {facts.tag} (nothing is pushed, tagged or dispatched)', '']
    for r in results:
        lines.append(f'[{"PASS" if r.ok else "FAIL"}] {r.key}. {r.title}')
        lines.append(f'       {r.detail}')
    lines.append('')
    ok = all(r.ok for r in results)
    noop = any(r.noop for r in results)
    if not ok:
        lines.append('RESULT: FAIL; failed checks: ' + ', '.join(r.key for r in results if not r.ok))
    elif noop:
        lines.append('RESULT: PASS; no-op, main already equals the tag commit')
    else:
        lines.append('RESULT: PASS')
        lines.append('Command a maintainer would run (this workflow does not run it):')
        lines.append(f'  {push_command(facts.tag)}')
    return '\n'.join(lines) + '\n'


# ---- collection: the only code that touches git, gh and npm -------------------------------

def _run(args, root):
    done = subprocess.run(args, cwd=root, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def collect(tag, repository, root):
    facts = Facts(tag=tag)
    if not TAG_RE.fullmatch(tag):
        return facts
    git = lambda *a: _run(['git', *a], root)
    facts.tag_commit = git('rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}')
    facts.branch_tip = git('rev-parse', '--verify', f'refs/remotes/origin/release/{tag}^{{commit}}')
    facts.main_commit = git('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
    if not facts.tag_commit:
        return facts
    index = git('show', f'{facts.tag_commit}:index.json')
    try:
        facts.suite_version = json.loads(index)['plugin']['version']
    except (TypeError, ValueError, KeyError):
        pass
    if facts.main_commit:
        facts.main_is_ancestor = subprocess.run(
            ['git', 'merge-base', '--is-ancestor', facts.main_commit, facts.tag_commit],
            cwd=root, capture_output=True).returncode == 0
    runs = _run(['gh', 'api', '-X', 'GET', f'repos/{repository}/actions/workflows/tests.yml/runs',
                 '-f', f'head_sha={facts.tag_commit}', '-f', 'event=push', '-f', 'per_page=100'], root)
    try:
        pushes = [r for r in json.loads(runs)['workflow_runs']
                  if str(r.get('head_branch', '')).startswith('release/') and r.get('status') == 'completed']
        if pushes:
            facts.tests_conclusion = max(pushes, key=lambda r: r['created_at']).get('conclusion')
    except (TypeError, ValueError, KeyError):
        pass
    manifest = _run(['gh', 'release', 'download', tag, '--repo', repository, '--pattern', 'release.json',
                     '--output', '-'], root)
    try:
        facts.release_commit = json.loads(manifest).get('commit')
    except (TypeError, ValueError, AttributeError):
        pass
    if facts.suite_version:
        facts.npm_version = _run(['npm', 'view', f'{PACKAGE}@{facts.suite_version}', 'version'], root) or None
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    facts = collect(args.tag, args.repository, args.root)
    results = evaluate(facts)
    sys.stdout.write(render(facts, results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
