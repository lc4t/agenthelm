# `agenthelm inventory` output contract (schema_version 1)

中文：[inventory-contract.md](inventory-contract.md)

A read-only command that answers: which Skills and MCP servers does each client see now, in which store and scope do they live, which are duplicated, and which have a recognizable origin. The output is for people and for later write commands and menus, so the JSON is a contract: **keys are only appended, never removed or redefined**; a breaking change must bump `schema_version`.

```
agenthelm inventory [--output json|text] [--client claude|codex|cursor]... [--type skills|mcp|all]
                    [--scope project|user|all] [--project DIR] [--lang zh|en|auto]
```

- By default it reads all three clients, all types and scopes; `--project` defaults to the current directory. `--scope project` also includes Claude's `local` level.
- It writes no file, runs no client command line, reads no settings key and no token store. Exit code 0; 2 for argument errors. A missing file means the store does not exist; a present but corrupt file is recorded in `unknown[]` and never crashes the command.
- `--output text` follows the interface language (`AGENTHELM_LANG` or `--lang`); JSON keys and values (including reason codes) are fixed English identifiers.
- Paths are abbreviated with `~` (home) and `<project>` (project root); a symlink target outside both stays absolute.

## Evidence level `evidence`

`file` (read from a file), `setting` (read from a settings key), `cli` (asked the client command line), `unknown` (the official documentation gives no way to decide, or the file is corrupt). **This version produces only `file` and `unknown`**; `setting` and `cli` are reserved. An `unknown` entry takes part in no "takes effect" conclusion. The contract has no "removable" field; later commands give disposal advice separately and only when the evidence is sufficient.

## Top level

`schema_version`, `generated_by`, `project`, `clients`, `filters{type,scope}`, `skills[]`, `mcps[]`, `summary.per_client`, `duplicates{skills[],mcps[]}`, `overlap_hints[]`, `unknown[]`.

## Skill entry

`name`, `client`, `scope` (`user|project`), `store`, `path`, `frontmatter{name,description}|null`, `description_bytes` (UTF-8 bytes of the description), `skill_md_bytes`, `content_sha256` (whole SKILL.md), `origin{kind,detail}`, `symlink{is_symlink,target}`, `version`, `stale`, `evidence`, `issues[]`.

- `origin.kind`: `agenthelm-plugin` (after resolving symlinks it lies inside a plugin whose `plugin.json` names the agenthelm repository), `state-record` (listed in Claude's sync state records `~/.claude/agent-pack/state/*.json`), `unknown`.
- `stale` is `true|false` only for `agenthelm-plugin`: `true` when the version is below the highest version of the same plugin found in this machine's client plugin directories; `false` only means no newer copy exists on this machine, not that it is the newest in the cloud. Everything else is `"unknown"`; nothing is guessed.
- `issues[]`: `broken-symlink`, `frontmatter-missing`, `description-missing`, `name-mismatch`, `skill-md-too-large`.
- Stores read: Claude `~/.claude/skills`, `<project>/.claude/skills`; Codex `~/.codex/skills`, `~/.agents/skills`, `<project>/.agents/skills`; Cursor `~/.cursor/skills`, `~/.agents/skills`, `<project>/.cursor/skills`, `<project>/.agents/skills`. Entries starting with a dot (such as `.system`) are skipped. Not covered: enterprise/admin directories, scanning from subdirectories up to the repository root, Cursor's compatibility loading of `.claude/skills`.

## MCP entry

`name`, `client`, `scope` (`user|local|project`), `store`, `transport` (`stdio|http|sse|unknown`), `command`, `args[]`, `url`, `env_names[]`, `header_names[]`, `fingerprint`, `origin`, `evidence`, `notes[]`.

- **`env` values, `headers` values and tokens are never printed**: only key names (`env_names`, `header_names`). Credential-looking values in `args` and `url` become `<redacted>`, as do URL userinfo and query values; `${VAR:-default}` keeps only `${VAR}`.
- `fingerprint`: SHA-256 of the **redacted** `command+args` or `url`, used to find duplicates; it excludes `env` and `headers`.
- Stores read: Claude `~/.claude.json` (user-level `mcpServers`), `projects[<absolute project path>].mcpServers` inside `~/.claude.json` (local level, read in the shape of the official example; any other shape is `unknown`), `<project>/.mcp.json`; Codex `~/.codex/config.toml` (`mcp_servers`), `<project>/.codex/config.toml` (with `notes: ["codex-project-requires-trust"]`, because the trust record cannot be read); Cursor `~/.cursor/mcp.json`, `<project>/.cursor/mcp.json`.

## Summary and duplicates

- `summary.per_client.<client>`: `skill_count`, `description_bytes_total` (sum of each entry's description bytes; excludes the name and any truncation or formatting by the client), `description_bytes_distinct_files` (entries pointing at one file count once), `mcp_count`.
- `duplicates.skills[]`: same name in different stores within one client. `relation`: `same-file` (one file after resolving), `identical` (same SKILL.md content), `different`, `unknown`. Across clients is not a duplicate.
- `duplicates.mcps[]`: `kind: same-name` (same client, same name, different stores; `relation` by fingerprint) and `kind: same-fingerprint` (same fingerprint, different names).
- `effective` ("which one takes effect"): carries a `store` only where the official documentation states a precedence; otherwise `store: null` with a `rule` and `evidence: unknown`.
  - Claude Skill: enterprise > personal > project (the user level wins; the enterprise level is not read). Source: <https://code.claude.com/docs/en/skills> ("Resolve skills that share a name", read 2026-11-18).
  - Claude MCP: local > project > user, the entry is used whole with no field merge. Source: <https://code.claude.com/docs/en/mcp> ("Scope hierarchy and precedence", read 2026-11-18). A project-level `.mcp.json` needs per-project approval and this version does not read the approval state, so a conclusion is given only when local wins; otherwise `project-approval-unknown`.
  - Codex Skill: same names are not merged and both appear in the selector, `rule: both-load`. Source: <https://learn.chatgpt.com/docs/build-skills> (read 2026-11-18).
  - Codex MCP: the official page (<https://learn.chatgpt.com/docs/extend/mcp?surface=cli>, read 2026-11-18) says only that project-level config applies to trusted projects and states no precedence, so `project-trust-unknown`.
  - Cursor: same-name precedence is not documented (design document #82 §2.3; **not re-read this time, unconfirmed**), recorded as `precedence-undocumented`.
- `overlap_hints[]`: different names, same client, token-set Jaccard similarity of the descriptions >= 0.8 (at least 5 tokens each side; English words, Chinese adjacent character pairs). `hint_only: true`; a mechanical hint that does not claim the two mean the same.

## `unknown[]`

`{store, client, reason}`. `reason`: `invalid-json`, `invalid-toml`, `unreadable`, `too-large`, `unexpected-structure`, `claude-local-structure-unrecognized`.

## Platforms

Path resolution supports macOS and Linux; Windows paths are described in documentation only and not resolved by this command.
