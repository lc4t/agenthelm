"""Pure change-fragment validation and BOM planning; no files, clock or Git access."""
from __future__ import annotations

import copy
import fnmatch

LEVELS = {None: 0, 'patch': 1, 'minor': 2, 'major': 3}
FIELDS = ('actions', 'user_action_required', 'touches', 'default_for_existing', 'template_changes', 'contracts')


def version_key(value: str) -> tuple[int, int, int]:
    parts = value.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f'invalid version: {value!r}')
    return tuple(int(p) for p in parts)


def bump(version: str, level: str | None) -> str:
    parts = list(version_key(version))
    if level:
        index = 3 - LEVELS[level]
        parts[index] += 1
        parts[index + 1:] = [0] * (2 - index)
    return '.'.join(map(str, parts))


def highest(*levels: str | None) -> str | None:
    return max(levels, key=LEVELS.__getitem__, default=None)


def summary_text(value, language='zh') -> str:
    return value if isinstance(value, str) else value.get(language, value.get('en', ''))


def fragment_problems(name: str, fragment: dict, scopes: set[str], declaration_validator) -> list[str]:
    problems = []
    if not isinstance(fragment, dict):
        return [f'{name}: fragment must be an object']
    for key in {'schema', 'summary', 'changes', 'suite'} - set(fragment):
        problems.append(f'{name}: missing {key}')
    if set(fragment) - {'schema', 'summary', 'changes', 'suite'}:
        problems.append(f'{name}: unknown fragment fields (versions belong to the release command)')
    if fragment.get('schema') != 1:
        problems.append(f'{name}: schema must be 1')
    summary = fragment.get('summary')
    if not (isinstance(summary, str) and summary.strip() or isinstance(summary, dict)
            and set(summary) == {'zh', 'en'} and all(isinstance(v, str) and v.strip() for v in summary.values())):
        problems.append(f'{name}: summary must be a non-empty string or {{zh, en}}')
    if not isinstance(fragment.get('suite'), (str, type(None))) or fragment.get('suite') not in LEVELS:
        problems.append(f'{name}: invalid suite level')
    changes = fragment.get('changes')
    if not isinstance(changes, list) or not changes:
        return problems + [f'{name}: changes must be a non-empty list']
    seen = set()
    for position, change in enumerate(changes):
        label = f'{name}.changes[{position}]'
        if not isinstance(change, dict):
            problems.append(f'{label}: change must be an object')
            continue
        if set(change) - {'scope', 'level', *FIELDS}:
            problems.append(f'{label}: unknown change fields (do not assign versions)')
        scope, level = change.get('scope'), change.get('level')
        if not isinstance(scope, str) or scope not in scopes | {'suite'}:
            problems.append(f'{label}: unknown scope {scope!r}')
        if isinstance(scope, str) and scope in seen:
            problems.append(f'{label}: duplicate scope {scope!r}')
        if isinstance(scope, str):
            seen.add(scope)
        if not isinstance(level, str) or level not in {'major', 'minor', 'patch'}:
            problems.append(f'{label}: invalid level')
        problems += declaration_validator(label, {**change, 'breaking': level == 'major'})
        if level == 'major' and not change.get('user_action_required'):
            problems.append(f'{label}: major changes need user_action_required')
        contracts = change.get('contracts')
        if not isinstance(contracts, list) or not all(isinstance(v, str) and v.strip() for v in contracts):
            problems.append(f'{label}: contracts must be a list of non-empty interface names')
        if contracts and level != 'major':
            problems.append(f'{label}: incompatible contract changes require major')
    return problems


def unique(items):
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def merge_declarations(changes: list[dict]) -> dict:
    result = {key: unique([value for c in changes for value in c.get(key, [])])
              for key in FIELDS if key != 'default_for_existing'}
    result['default_for_existing'] = max((c.get('default_for_existing', 'off') for c in changes),
                                          key={'off': 0, 'advisory': 1, 'on': 2}.__getitem__)
    return result


def matching_pack(path: str, pack: dict) -> bool:
    return any(path == p or path.startswith(p.rstrip('/') + '/') or fnmatch.fnmatchcase(path, p)
               for p in pack.get('paths', []))


def plan(base: dict, fragments: list[tuple[str, dict]], current: dict, date: str,
         overrides: dict | None = None) -> dict:
    """Plan from an official BOM plus all pending fragments. RC BOMs never become a baseline.

    """
    effective = copy.deepcopy(base)
    collected: dict[str, list[dict]] = {}
    levels = {}
    reasons = []
    suite_level = None
    for filename, fragment in sorted(fragments):
        suite_level = highest(suite_level, fragment.get('suite'))
        for change in fragment['changes']:
            scope = change['scope']
            levels[scope] = highest(levels.get(scope), change['level'])
            collected.setdefault(scope, []).append({**change, 'summary': fragment['summary']})
            reasons.append({'fragment': filename, 'scope': scope, 'level': change['level']})
            suite_level = highest(suite_level, change['level'])
    overrides = overrides or {}
    for scope, override in sorted(overrides.items()):
        if scope not in set(current['plugins']) | {'suite'}:
            raise ValueError(f'unknown override scope: {scope}')
        level = override.get('level')
        previous = suite_level if scope == 'suite' else levels.get(scope)
        if level not in {'major', 'minor', 'patch'} or LEVELS[level] < LEVELS[previous]:
            raise ValueError(f'{scope}: override may only raise a level')
        if not isinstance(override.get('reason'), str) or not override['reason'].strip():
            raise ValueError(f'{scope}: override requires a reason')
        if scope != 'suite' and scope not in collected:
            raise ValueError(f'{scope}: override needs a change fragment')
        if scope == 'suite':
            suite_level = level
        else:
            levels[scope] = level
            suite_level = highest(suite_level, level)
        reasons.append({'override': scope, **override})
    removed = set(base['plugins']) - set(current['plugins'])
    added = set(current['plugins']) - set(base['plugins'])
    suite_level = highest(suite_level, 'major' if removed else 'minor' if added else None)
    plugins = {}
    migrations = []
    for name, plugin in sorted(current['plugins'].items()):
        old = effective['plugins'].get(name)
        target = bump(old['version'], levels.get(name)) if old else '1.0.0'
        packs = {}
        changes = collected.get(name, [])
        for pack_id, pack in sorted(plugin.get('content', {}).items()):
            before = (old or {}).get('content', {}).get(pack_id)
            classes = [t['class'] for c in changes for t in c['template_changes'] if matching_pack(t['path'], pack)]
            level = 'minor' if 'semantic' in classes else 'patch' if classes else None
            packs[pack_id] = {'version': bump(before['version'], level) if before else '1.0.0', 'kind': pack['kind']}
        plugins[name] = {'version': target, 'source': plugin['source'], **({'content': packs} if packs else {})}
        original = base['plugins'].get(name)
        combined = changes
        if original and target != original['version'] and combined:
            declaration = merge_declarations(combined)
            migrations.append({'plugin': name, 'from': original['version'], 'to': target,
                               'change_kind': 'both' if declaration['template_changes'] else 'code',
                               'breaking': levels.get(name) == 'major' or any(c.get('breaking') for c in combined),
                               'date': date, 'summary': '; '.join(unique([summary_text(c['summary']) for c in combined])),
                               'summary_en': '; '.join(unique([summary_text(c['summary'], 'en') for c in combined])),
                               'declaration': 2, **{k: v for k, v in declaration.items() if k != 'contracts'}})
    suite = bump(effective['suite'], suite_level)
    lines = [f'## {date} — agenthelm {suite}', '']
    for name, plugin in plugins.items():
        if name in collected:
            lines.extend([f'### {name} {plugin["version"]}', ''])
            lines.extend(f'- {summary_text(c["summary"])}' for c in collected.get(name, []))
            lines.append('')
    if collected.get('suite'):
        lines.extend([f'### suite {suite}', ''])
        lines.extend(f'- {summary_text(c["summary"])}' for c in collected['suite'])
    return {'schema': 1, 'baseline': base.get('tag'), 'suite': suite, 'plugins': plugins,
            'migrations': migrations, 'changelog': '\n'.join(lines).rstrip() + '\n',
            'explain': reasons, 'removed': sorted(removed), 'overrides': overrides, 'date': date}
