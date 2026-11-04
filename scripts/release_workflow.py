"""Git/filesystem adapter for release_model; publishing and promotion stay manual."""
from __future__ import annotations

import argparse
import json
import hashlib
import stat
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import release_model as model

FRAGMENTS = 'release/changes'
HISTORY = 'release/history'
CANDIDATE = 'release/candidate.json'


def bom_at(meta, ref):
    latest = meta.json_at(ref, 'latest.json')
    index = meta.json_at(ref, 'index.json')
    if not latest or not index:
        raise ValueError(f'{ref}: missing release BOM')
    return {'tag': ref, 'suite': index['plugin']['version'], 'plugins': latest['plugins']}


def official_base(meta, exclude=None):
    # A publish-then-promote merge tag can have the same tree as dev without being
    # its ancestor yet. RC tags never move the baseline; this project has one stable line.
    tags = (meta.git('tag', '--list', 'v*') or '').splitlines()
    official = [tag for tag in tags if tag != exclude and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag)]
    if not official:
        raise ValueError('no official release tag available; fetch release tags first')
    tag = max(official, key=lambda t: model.version_key(t[1:]))
    if meta.git('merge-base', '--is-ancestor', tag, 'HEAD') is not None:
        return tag
    tagged_tree = meta.git('rev-parse', tag + '^{tree}')
    parents = (meta.git('rev-list', '--parents', '-n', '1', tag) or '').split()[1:]
    if any(meta.git('merge-base', '--is-ancestor', parent, 'HEAD') is not None
           and meta.git('rev-parse', parent + '^{tree}') == tagged_tree for parent in parents):
        return tag
    raise ValueError(f'latest official release {tag} is not integrated; merge main into dev first')


def source_files(meta):
    """Bind non-generated distribution bytes; normalize only fields the release writer owns."""
    paths = (meta.git('ls-files', '--cached', '--others', '--exclude-standard', '-z') or '').split('\0')
    result = {}
    excluded = {meta.MARKETPLACE_FILE, meta.LATEST_FILE, meta.MIGRATIONS_FILE, CANDIDATE, 'CHANGELOG.md'}
    for relative in sorted(set(paths) - {''}):
        if relative in excluded or relative.startswith((HISTORY + '/', FRAGMENTS + '/')):
            continue
        path = meta.ROOT / relative
        if not path.exists() and not path.is_symlink():
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            data, kind = path.readlink().as_posix().encode('utf-8'), 'symlink'
        elif stat.S_ISREG(mode):
            data, kind = path.read_bytes(), 'executable' if mode & 0o111 else 'file'
            if frozen_json(relative):
                value = json.loads(data)
                if relative == 'index.json':
                    value['plugin'].pop('version', None)
                    value['site'].pop('updated_at', None)
                    for plugin in value['skills']:
                        plugin.pop('version', None)
                elif relative.endswith('/content.json'):
                    for pack in value['packs']:
                        pack.pop('version', None)
                        pack.pop('files', None)
                else:
                    value.pop('version', None)
                data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
            elif relative == 'index.html':
                text = re.sub(r'Release [0-9]+\.[0-9]+\.[0-9]+', 'Release @VERSION@', data.decode('utf-8'))
                text = re.sub(r'(更新日期 )[0-9]{4}-[0-9]{2}-[0-9]{2}', r'\g<1>@DATE@', text)
                data = text.encode('utf-8')
        else:
            raise ValueError(f'{relative}: unsupported distribution file type')
        result[relative] = {'sha256': hashlib.sha256(data).hexdigest(), 'kind': kind}
    return result


def current_bom(meta):
    plugins = {}
    for item in meta.load_plugins():
        plugins[item['manifest']['name']] = {
            'version': item['manifest']['version'], 'source': item['source'],
            'content': {pack['id']: {**pack, 'paths': pack.get('sources', []) + pack.get('materializes', [])}
                        for pack in (item['content'] or {}).get('packs', [])}}
    return {'plugins': plugins, 'suite': meta.read_json(meta.ROOT / 'index.json')['plugin']['version'],
            'source_files': source_files(meta)}


def fragments(meta):
    return [(path.name, meta.read_json(path)) for path in sorted((meta.ROOT / FRAGMENTS).glob('*.json'))]


def validate_fragments(meta, items, scopes):
    return [p for name, fragment in items for p in model.fragment_problems(name, fragment, scopes, meta.declaration_problems)]


def changed_paths(meta, base):
    tracked = meta.git('diff', '--name-only', base, '--')
    if tracked is None:
        raise ValueError(f'{base}: not a git revision')
    untracked = meta.git('ls-files', '--others', '--exclude-standard') or ''
    return sorted(set(tracked.splitlines() + untracked.splitlines()))


def owned_scope(meta, path, names):
    for name in names:
        if path.startswith(f'plugins/{name}/'):
            return None if meta.is_unshipped(Path(path).parts) else name
    if path.startswith(('scripts/', 'packages/cli/')) or path in {'index.html', 'index.json', 'INSTALL.md', 'README.md'}:
        return 'suite'
    return None


def version_fields(path, data):
    """The small version registry. Non-version metadata remains editable in feature PRs."""
    if path == 'index.json':
        return {'suite': data['plugin']['version'], 'date': data['site']['updated_at'],
                'plugins': {p['id']: p['version'] for p in data['skills']}}
    if path.endswith('/content.json'):
        return {p['id']: {'version': p['version'], 'files': p['files']} for p in data['packs']}
    return {'version': data.get('version')}


def frozen_json(path):
    return path in {'index.json', 'packages/cli/package.json'} or path.startswith('plugins/') and (
        path.endswith(('/plugin.json', '/content.json', '/skill.json')))


def check_changes(meta, base):
    current = current_bom(meta)
    before = meta.json_at(base, 'latest.json') or {'plugins': {}}
    names = set(current['plugins']) | set(before['plugins'])
    paths = changed_paths(meta, base)
    fresh = [(name, f) for name, f in fragments(meta) if f'{FRAGMENTS}/{name}' in paths
             and meta.git('show', f'{base}:{FRAGMENTS}/{name}') is None]
    problems = validate_fragments(meta, fragments(meta), names)
    declarations = [change for _, f in fresh if isinstance(f, dict) for change in f.get('changes', []) if isinstance(change, dict)]
    covered = {c.get('scope') for c in declarations}
    for path in paths:
        scope = owned_scope(meta, path, names)
        if scope and scope not in covered:
            problems.append(f'{path}: shipped changes require a new {scope} fragment')
        inspect = (path in {meta.MARKETPLACE_FILE, meta.LATEST_FILE, meta.MIGRATIONS_FILE, CANDIDATE, 'index.html'}
                   or frozen_json(path) or path.startswith((HISTORY + '/', FRAGMENTS + '/')))
        if not inspect:
            continue  # Binary plugin assets still need scope coverage, never UTF-8 decoding.
        before_text = meta.git('show', f'{base}:{path}')
        target = meta.ROOT / path
        text = target.read_text(encoding='utf-8') if target.is_file() else None
        if path in {meta.MARKETPLACE_FILE, meta.LATEST_FILE, meta.MIGRATIONS_FILE, CANDIDATE}:
            if text != before_text:
                problems.append(f'{path}: derived release metadata is frozen in feature PRs')
        if frozen_json(path) and text and before_text:
            now, before = version_fields(path, json.loads(text)), version_fields(path, json.loads(before_text))
            if path == 'index.json':
                now['plugins'] = {k: v for k, v in now['plugins'].items() if k in before['plugins']}
                before['plugins'] = {k: v for k, v in before['plugins'].items() if k in now['plugins']}
            elif path.endswith('/content.json'):
                now = {k: v for k, v in now.items() if k in before}
                before = {k: v for k, v in before.items() if k in now}
            if now != before:
                problems.append(f'{path}: versions and content digests belong to the release command')
        if path == 'index.html' and before_text and text:
            if re.findall(r'Release [0-9]+\.[0-9]+\.[0-9]+', text) != re.findall(r'Release [0-9]+\.[0-9]+\.[0-9]+', before_text):
                problems.append('index.html: release badges belong to the release command')
        if path.startswith(HISTORY + '/') and before_text != text:
            # The first adoption imports historical declarations without changing their bytes.
            adoption = meta.git('show', f'{base}:{HISTORY}/legacy/migrations.json') is None
            imported = adoption and path == f'{HISTORY}/legacy/migrations.json' and before_text is None
            imported = imported and text == meta.git('show', f'{base}:{meta.MIGRATIONS_FILE}')
            if not imported:
                problems.append(f'{path}: release history is immutable in feature PRs')
        if path.startswith(FRAGMENTS + '/') and before_text is not None and text != before_text:
            problems.append(f'{path}: add a new fragment; pending fragments are immutable')
    # Require declarations for actual template/skeleton changes, not only a scope label.
    for name in names:
        prefix = f'plugins/{name}/'
        plugin = next((p for p in meta.load_plugins() if p['manifest']['name'] == name), None)
        if not plugin:
            continue
        relevant = [c for c in declarations if c.get('scope') == name]
        for pack in (plugin['content'] or {}).get('packs', []):
            sources = [prefix + source for source in pack['sources']]
            if any(any(path == source or path.startswith(source.rstrip('/') + '/') for source in sources) for path in paths):
                pack_paths = {'paths': pack.get('sources', []) + pack.get('materializes', [])}
                if not any(model.matching_pack(t.get('path', ''), pack_paths)
                           for c in relevant for t in c.get('template_changes', []) if isinstance(t, dict)):
                    problems.append(f'{pack["id"]}: changed content requires matching template_changes')
        if any(prefix + expected in paths for expected in meta.EXPECTATION_FILES) and not any(c.get('touches') for c in relevant):
            problems.append(f'{name}: expectation/schema changes require touches')
    return problems


def make_plan(meta, base, date, overrides=None):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', base):
        raise ValueError('--base must name an official vX.Y.Z release tag')
    old, current = bom_at(meta, base), current_bom(meta)
    items = fragments(meta)
    problems = validate_fragments(meta, items, set(old['plugins']) | set(current['plugins']))
    if problems:
        raise ValueError('\n'.join(problems))
    result = model.plan(old, items, current, date, overrides)
    if not items:
        raise ValueError('no pending change fragments')
    problems = [p for entry in result['migrations'] for p in meta.declaration_problems(entry['plugin'], entry)]
    if problems:
        raise ValueError('\n'.join(problems))
    # Every changed pack must have an actual matching template path declaration.
    for item in meta.load_plugins():
        name = item['manifest']['name']
        prior = old['plugins'].get(name, {}).get('content', {})
        for pack in (meta.refreshed_content(item) or {}).get('packs', []):
            previous = meta.json_at(base, f'{item["source"][2:]}/content.json') or {'packs': []}
            old_pack = next((p for p in previous['packs'] if p['id'] == pack['id']), None)
            version = result['plugins'][name].get('content', {}).get(pack['id'], {}).get('version')
            if old_pack and pack['files'] != old_pack['files'] and version == prior.get(pack['id'], {}).get('version'):
                raise ValueError(f'{pack["id"]}: changed content has no matching template_changes path')
    return result


def history_migrations(meta):
    paths = sorted((meta.ROOT / HISTORY).glob('**/migrations.json'))
    if not paths:
        return None
    entries = [entry for path in paths for entry in meta.read_json(path)]
    candidate = meta.ROOT / CANDIDATE
    if candidate.is_file():
        entries += meta.read_json(candidate)['migrations']
    return list({(e['plugin'], e['from'], e['to']): e for e in entries}.values())


def release_files(meta, release, candidate):
    generated = {}
    index = meta.read_json(meta.ROOT / 'index.json')
    index['plugin']['version'] = release['suite']
    index['site']['updated_at'] = release['date']
    for skill in index['skills']:
        if skill['id'] in release['plugins']:
            skill['version'] = release['plugins'][skill['id']]['version']
    generated['index.json'] = meta.dump(index)
    package = meta.read_json(meta.ROOT / meta.CLI_PACKAGE)
    package['version'] = release['suite']
    generated[meta.CLI_PACKAGE] = meta.dump(package)
    for item in meta.load_plugins():
        name, root = item['manifest']['name'], item['root']
        version = release['plugins'][name]['version']
        for path in [root / 'plugin.json', root / '.codex-plugin/plugin.json', *root.glob('skills/*/skill.json')]:
            if path.is_file():
                data = meta.read_json(path)
                data['version'] = version
                generated[path.relative_to(meta.ROOT).as_posix()] = meta.dump(data)
        if item['content']:
            content = meta.refreshed_content(item)
            for pack in content['packs']:
                pack['version'] = release['plugins'][name]['content'][pack['id']]['version']
            generated[f'{item["source"][2:]}/content.json'] = meta.dump(content)
    html = (meta.ROOT / 'index.html').read_text(encoding='utf-8')
    html = re.sub(r'Release [0-9]+\.[0-9]+\.[0-9]+', 'Release ' + release['suite'], html)
    html = re.sub(r'(更新日期 )[0-9]{4}-[0-9]{2}-[0-9]{2}', r'\g<1>' + release['date'], html)
    generated['index.html'] = html
    changelog = (meta.ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    # Replace the managed candidate section, so repeated RC plans do not duplicate notes.
    section = ('<!-- release-plan:start -->\n' + release['changelog'] + '<!-- release-plan:end -->\n') if candidate else release['changelog']
    if '<!-- release-plan:start -->' in changelog:
        changelog = re.sub(r'<!-- release-plan:start -->.*?<!-- release-plan:end -->\n?', lambda _: section,
                           changelog, flags=re.S)
    else:
        head, sep, body = changelog.partition('\n')
        changelog = head + sep + '\n' + section + '\n' + body
    generated['CHANGELOG.md'] = changelog
    generated[CANDIDATE if candidate else f'{HISTORY}/{release["suite"]}/plan.json'] = meta.dump(release)
    if not candidate:
        generated[f'{HISTORY}/{release["suite"]}/migrations.json'] = meta.dump(release['migrations'])
        for filename, fragment in fragments(meta):
            generated[f'{HISTORY}/{release["suite"]}/changes/{filename}'] = meta.dump(fragment)
    return generated


def apply_release(meta, release, candidate):
    if meta.git('status', '--porcelain').strip():
        raise ValueError('release --apply requires a clean work tree and index; commit the fragments first')
    generated = release_files(meta, release, candidate)
    if not candidate:
        if (meta.ROOT / HISTORY / release['suite']).exists():
            raise ValueError('this release is already archived; use a new version')
        for filename, _ in fragments(meta):
            generated[f'{FRAGMENTS}/{filename}'] = None
        if (meta.ROOT / CANDIDATE).exists():
            generated[CANDIDATE] = None
    # Save only paths this transaction owns. A failed validation or commit restores exact bytes and index.
    extra = [meta.MARKETPLACE_FILE, meta.LATEST_FILE, meta.MIGRATIONS_FILE]
    backups = {name: (meta.ROOT / name).read_bytes() if (meta.ROOT / name).exists() else None
               for name in {*generated, *extra}}
    try:
        for name, text in generated.items():
            path = meta.ROOT / name
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
        if meta.main(['--write']):
            raise ValueError('generated metadata failed consistency checks')
        problems = check_release(meta, release['baseline'])
        if problems:
            raise ValueError('generated release failed the release gate: ' + '; '.join(problems))
        subprocess.run(['git', 'add', '--', *backups], cwd=meta.ROOT, check=True, capture_output=True)
        if meta.git('diff', '--cached', '--quiet') is not None:
            return False  # An identical RC plan is already materialized.
        subprocess.run(['git', 'commit', '-m', f'chore: release {release["suite"]}' + (' candidate' if candidate else '')],
                       cwd=meta.ROOT, check=True, capture_output=True)
        return True
    except Exception:
        meta.git('reset', '--mixed', 'HEAD')
        for name, data in backups.items():
            path = meta.ROOT / name
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        raise


def check_release(meta, since, tag=None):
    problems = []
    result = meta.main(['--since', since])
    if result:
        problems.append('derived metadata or release declarations failed')
    suite = meta.read_json(meta.ROOT / 'index.json')['plugin']['version']
    if tag and not re.fullmatch(r'v' + re.escape(suite) + r'(?:-rc\.[1-9][0-9]*)?', tag):
        problems.append(f'tag {tag} must equal v{suite} or v{suite}-rc.N')
    path = meta.ROOT / CANDIDATE
    if tag and '-rc.' in tag and not path.is_file():
        problems.append('candidate tags require a candidate plan retaining change fragments; use release --candidate')
    if tag and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag) and path.is_file():
        problems.append('formal tags require an archived release plan; rerun release without --candidate')
    if not path.is_file():
        path = meta.ROOT / HISTORY / suite / 'plan.json'
    if not path.is_file():
        return problems + ['missing generated release plan; run release --apply']
    release = meta.read_json(path)
    if release['suite'] != suite or release['baseline'] != since:
        problems.append('release plan differs from the suite version or official baseline')
    current = current_bom(meta)
    if current['source_files'] != release.get('source_files'):
        changed = sorted(name for name in set(current['source_files']) | set(release.get('source_files', {}))
                         if current['source_files'].get(name) != release.get('source_files', {}).get(name))
        problems.append('release source changed since preparation: ' + ', '.join(changed[:8]))
    source = meta.ROOT / FRAGMENTS if path.name == 'candidate.json' else path.parent / 'changes'
    items = [(p.name, meta.read_json(p)) for p in sorted(source.glob('*.json'))]
    problems += validate_fragments(meta, items, set(current['plugins']) | set(bom_at(meta, since)['plugins']))
    if not problems:
        expected_plan = model.plan(bom_at(meta, since), items, current, release['date'], release.get('overrides'))
        if expected_plan != release:
            problems.append('saved release plan differs from its source fragments; regenerate the release')
    for name, plugin in current['plugins'].items():
        expected = release['plugins'].get(name)
        if not expected or plugin['version'] != expected['version']:
            problems.append(f'{name}: version differs from the release plan')
        for pack_id, pack in plugin.get('content', {}).items():
            if pack['version'] != (expected or {}).get('content', {}).get(pack_id, {}).get('version'):
                problems.append(f'{pack_id}: version differs from the release plan')
    return problems


def main(meta, argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    changes = commands.add_parser('check-changes', help='feature PR gate; no version changes')
    changes.add_argument('--base', required=True)
    check = commands.add_parser('check-release', help='release commit gate')
    check.add_argument('--since')
    check.add_argument('--tag')
    release = commands.add_parser('release', help='plan, or create one local release commit')
    release.add_argument('mode', nargs='?', choices=['plan'])
    release.add_argument('--base', help='official baseline tag; defaults to the highest official tag, requiring integration')
    release.add_argument('--date', default=datetime.now(timezone.utc).date().isoformat())
    release.add_argument('--explain', action='store_true')
    release.add_argument('--apply', action='store_true')
    release.add_argument('--candidate', action='store_true', help='retain fragments for the next RC')
    release.add_argument('--overrides', type=Path, help='JSON {scope: {level, reason}}; levels may only rise')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check-changes':
            problems = check_changes(meta, args.base)
        elif args.command == 'check-release':
            suite = meta.read_json(meta.ROOT / 'index.json')['plugin']['version']
            problems = check_release(meta, args.since or official_base(meta, exclude='v' + suite), args.tag)
        else:
            if not meta.DATE_RE.fullmatch(args.date):
                raise ValueError('--date must be YYYY-MM-DD')
            if args.mode == 'plan' and args.apply:
                raise ValueError('release plan is read-only; omit plan to use --apply')
            result = make_plan(meta, args.base or official_base(meta), args.date,
                               meta.read_json(args.overrides) if args.overrides else None)
            printed = result if args.explain else {k: v for k, v in result.items() if k != 'explain'}
            print(meta.dump(printed), end='')
            if args.apply:
                created = apply_release(meta, result, args.candidate)
                print(('Local release commit created. ' if created else 'Release plan already materialized. ') + f'Review it, then run: git tag v{result["suite"]}' +
                      ('-rc.N' if args.candidate else ''))
            return 0
        for problem in problems:
            print(problem, file=sys.stderr)
        return int(bool(problems))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
