# `agenthelm dedupe` contract (schema_version 1)

中文：[dedupe-contract.md](dedupe-contract.md)

```
agenthelm dedupe --list [--output json]
agenthelm dedupe <group-id> (--keep <ref> --retire-others | --keep-all --note "..." | --ignore | --unignore)
                 [--project DIR] [--output json|text] [--lang zh|en|auto] [--apply --expect-plan <plan_digest>]
agenthelm dedupe <group-id> --keep <mcp ref> --desktop-snippet [--output json|text]
```

Groups come from the `duplicates` of `agenthelm inventory`. A group id is stable: `g-` + the first 10 hex digits of SHA-256(`kind|client|name-or-fingerprint`); it does not change when other entries appear. Preview by default, zero writes; `--apply` must carry the `--expect-plan` a preview printed and refuses on a mismatch (exit 1). Exit codes: 0 success or a preview without blockers; 1 blockers, digest mismatch or failure; 2 usage error. macOS and Linux only.

## Dispositions

- **`--keep <ref> --retire-others`** (Skill copies only): the other copies are moved to `~/.agent-pack/recovery/<timestamp>-dedupe-<group id>/skills/<name>` (the same recovery layout; several copies get a store suffix). Archiving is a move, not a delete; failures move everything back. The result gives `recovery_dir` and a one-line manual `mv`; this version has no `restore` command. A kept entry that points inside a directory to be retired -> `keep-depends-on-retired`.
- **A copy with `unknown` evidence is read-only**: if any member is `unknown` the plan is `blocked: evidence-unknown` and nothing is retired.
- **MCP copies**: the tool **never edits** any configuration (`~/.claude.json` may hold local credentials). `plan.manual[]` carries the official command `claude mcp remove --scope <user|local|project> <name>` and the backup reminder `cp ~/.claude.json ~/.claude.json.bak`, each marked `not_archivable_by_tool: true` and `promise: cannot-promise`; other clients only get an instruction to delete the entry in a file by hand. `--apply` ends as `manual-only` with zero writes. When another client has an entry with the same fingerprint, `warnings` has `cross-client-keep-recommended` (the same MCP across clients does not conflict; keeping is recommended).
- **`--keep-all --note`** / **`--ignore`** / **`--unignore`**: write only the machine-level decision file `~/.config/agenthelm/ext-decisions.json` (0600, atomic); its content is just `{decision, note, kind, client, key, members_digest, at}` with no values. A note is at most 200 characters and a token-like note is refused. After the group's members change, `--list` marks the decision `stale`. `--ignore` is undone with `--unignore`. None of the three changes any client configuration.

## Paste-ready Desktop snippet (read-only)

`--desktop-snippet` prints the path of `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`, Linux `~/.config/Claude/`), a JSON fragment to merge into `mcpServers` (structure `command`/`args`/`url` only; environment variables are listed by name with a `<fill in your NAME>` placeholder) and the hint to fully quit and restart Desktop. An entry with an inline credential is `blocked: inline-secret` and yields no snippet. Whether Desktop loads remote `url` entries, headers or SSE is not documented; the snippet carries `notes` marked unconfirmed, and `promise` is always `cannot-promise`. The Desktop config file is neither read nor written.

## JSON

`{schema_version, command: "dedupe", mode: "list"|"preview"|"apply"|"desktop-snippet", ...}`. `plan` keys: `group`, `kind`, `disposition`, `status` (`ready|blocked|manual-only`), `members[]`, `steps[{id,action,promise}]`, `blocked[{code,message}]`, `warnings[{code,message}]`, `manual[]`, `retire[]`, `plan_digest`, `next.apply`. `plan_digest` does not depend on the interface language. Keys are only appended; output contains no env values, header values or tokens.
