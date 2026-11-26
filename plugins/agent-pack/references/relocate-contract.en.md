# `agenthelm relocate` contract (schema_version 1)

中文：[relocate-contract.md](relocate-contract.md)

```
agenthelm relocate <ref> --to <client>:<scope> [--mode copy|link] [--project DIR] [--allow-widen] [--keep-source]
                   [--confirm-project-content] [--output json|text] [--lang zh|en|auto]
                   [--apply --expect-plan <plan_digest>]
```

Skills and MCP entries (below); for a Skill `<ref>` is `skills[].ref` from `agenthelm inventory` (`skill:<name>@<client>:<scope>`, with `#agents` outside the primary store). **Preview by default, zero writes.** `--apply` must carry the `--expect-plan` a preview printed; if the recomputed `plan_digest` differs the command refuses (exit 1, nothing written). Exit codes: 0 success or a preview without blockers; 1 blockers, digest mismatch or a failed write; 2 usage error. macOS and Linux only.

## Target stores

`claude:user` -> `~/.claude/skills`; `claude:project` -> `<project>/.claude/skills`; `codex:user` -> `~/.agents/skills`; `codex:project` and `cursor:project` -> `<project>/.agents/skills`; `cursor:user` -> `~/.cursor/skills`. When the source already sits in a store the target client reads natively, `plan.status` is `native` and nothing is written.

## Rules

- **Never overwrites**: an existing same-name target -> `blocked: target-exists-identical|target-exists-different`; no rename, no merge.
- **Copy vs link**: across scopes (home <-> project) always copy (`--mode link` is blocked with `link-cross-scope`). Within one scope `--mode link` creates a relative symbolic link and implies keeping the source. A path containing a synced-folder name (iCloud, Dropbox, OneDrive, ...; a heuristic) downgrades the link to a copy, `warnings` has `sync-folder-demoted`, and the last step's `promise` is `cannot-promise`.
- **The source**: by default archived after verification to `~/.agent-pack/recovery/<timestamp>-relocate-<name>/skills/<name>` (a move, not a delete; same layout as the recovery directory of `reconcile --retire-skill`: `<timestamp>-<label>/skills/<name>`, asserted equal by a test); `--keep-source` copies and keeps it. The result gives `recovery_dir` and a one-line manual `mv` restore step; this version has no `restore` command.
- **Write order and rollback**: recovery directory (0700) -> copy under a temporary name beside the target -> read back (relative path and content digest per file; the `SKILL.md` digest equals the source's) -> rename into place -> archive the source. Any failure removes only what this run created, moves archived sources back and keeps the recovery directory. Symbolic links inside the directory that point outside it -> `source-has-external-symlink`.
- **Widening** (project -> user level) needs `--allow-widen`, `direction: "widen"`, shown in red in a terminal's text preview.
- **Entering a project repository** (user -> project level): `confirmations` says the content will enter the project directory and may be committed; `--apply` additionally needs `--confirm-project-content`. License and private content are for the user to judge; the tool cannot.
- A source without `file` evidence (broken directory, oversized `SKILL.md`) is read-only: `source-not-file-evidence`. Other kinds: `unsupported-kind`.

## MCP entries (`mcp:<name>@claude:<user|local|project>`)

`<ref>` is `mcps[].ref`; `--to claude:user|local|project` (`project` is the project's `.mcp.json`). Same shell (preview, `plan_digest`, `--expect-plan`, `blocked`, `promise`); writes reuse the Claude sync's local and `.mcp.json` readers and helpers, implemented in `agent_pack_mcp_move.py`. `plan.kind` is `mcp`, `source.managed` says whether agent-pack's state records ownership, and `plan.manual[]` carries the manual command for an unmanaged source.

- **Copy only, never overwrite**: an existing target entry of that name → `target-exists-identical|different` (compares a hash of the whole entry, says only same or different, shows no value). `--mode link` → `link-unsupported`.
- **Direction**: `user` is wider than `local`/`project`; widening (→ `user`) needs `--allow-widen` and is marked red; `local` ↔ `project` is `same`. A `project` target also needs `--confirm-project-content` on `--apply` (the entry enters version control).
- **Limits for `.mcp.json`**: an inline secret value → `inline-secret-in-project-file` (turn it into a `${VAR}` reference first; the tool never rewrites entries); a machine path → `machine-specific-in-project-file`. A client whose `project_write_supported` is false → `project-write-unsupported`; across clients → `cross-client-unsupported`.
- **Disposition of the source**: an entry is **managed** only when the state of this project's package records ownership (`mcps` for user level, `scopes.mcps` for local/project). Managed and without `--keep-source`: the target write and the source removal are one atomic replace of `~/.claude.json` (`.mcp.json` is its own atomic write); the whole original file is first backed up byte for byte into the recovery directory (`claude.json.before` / `project-mcp.json.before`), the removed entry's original JSON fragment goes to `mcp/<name>@<scope>.json` (all 0600, directory 0700, never printed), and the state is updated (the `mcps` key still means user level only). On any failure every touched file is restored byte for byte. **Unmanaged** (hand-installed, or owned by another package name): only copied to the new place, the original is left alone, and `plan.manual[]` gives `claude mcp remove --scope <scope> <name>` plus a backup reminder, `not_archivable_by_tool: true`.
- Recovery: no `restore` command in this version; the result's `restore[]` lists the manual steps (put the fragment back by hand, or restore the whole backup, which loses later changes to that file).
- Output, `relocate.json` and the plan carry only names, scopes, paths and a 16-hex entry digest, never env values, headers or tokens; only `claude.json.before` and `mcp/*.json` hold the user's own values.
- When the project Profile's `mcp_scope` differs from the target scope, `warnings` has `declaration-differs` (the next sync follows the declaration).

## Impact (`plan.impact`)

The previews of Skills and MCP entries both gain the key `impact`: the source (archived when `retire_source` is true) and the target (the added copy) are computed together, and when several clients read the target store each reader gets a row (`gained`). With `--keep-source` nothing is archived, so only `gained` and `other-machines-lose-it` appear; a `native` (zero-write) plan and a blocked plan have no `impact`. When a project-level entry moves to user level, `scope_notes` contains `other-machines-lose-it` and the warning `impact-other-machines` says that if the entry is shared through the project repository, projects on other machines will no longer carry it; the tool does not check whether it is committed. The remaining keys, values and warning codes are those of the "Impact" section of the `agenthelm dedupe` contract (the authoritative field description). `plan_digest` includes `impact` and does not depend on the interface language; `status` is not changed by the impact.

## JSON

`{schema_version, command: "relocate", mode: "preview"|"apply", plan, applied}`. `plan`: `ref`, `kind`, `source`, `target`, `mode`, `retire_source`, `direction` (`narrow|same|widen`), `status` (`ready|blocked|native`), `steps[{id,action,promise,...}]`, `blocked[{code,message,...}]`, `warnings[{code,message}]`, `confirmations[{id,text,flag}]`, `recovery_base`, `plan_digest`, `next.apply`. `promise`: `certain` (file-level fact), `best-effort` (needs a client restart or re-entering the project to confirm it shows up), `cannot-promise` (the tool guarantees file changes, not client behavior). `plan_digest` does not depend on the interface language (`message` / `text` are excluded). `applied.status`: `applied|rolled-back|refused|native`. Keys are only appended; the output has no file contents, only names, paths (abbreviated with `~` and `<project>`) and digests.
