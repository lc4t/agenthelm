# `agenthelm relocate` contract (schema_version 1)

中文：[relocate-contract.md](relocate-contract.md)

```
agenthelm relocate <ref> --to <client>:<scope> [--mode copy|link] [--project DIR] [--allow-widen] [--keep-source]
                   [--confirm-project-content] [--output json|text] [--lang zh|en|auto]
                   [--apply --expect-plan <plan_digest>]
```

Skills only; `<ref>` is `skills[].ref` from `agenthelm inventory` (`skill:<name>@<client>:<scope>`, with `#agents` outside the primary store). **Preview by default, zero writes.** `--apply` must carry the `--expect-plan` a preview printed; if the recomputed `plan_digest` differs the command refuses (exit 1, nothing written). Exit codes: 0 success or a preview without blockers; 1 blockers, digest mismatch or a failed write; 2 usage error. macOS and Linux only.

## Target stores

`claude:user` -> `~/.claude/skills`; `claude:project` -> `<project>/.claude/skills`; `codex:user` -> `~/.agents/skills`; `codex:project` and `cursor:project` -> `<project>/.agents/skills`; `cursor:user` -> `~/.cursor/skills`. When the source already sits in a store the target client reads natively, `plan.status` is `native` and nothing is written.

## Rules

- **Never overwrites**: an existing same-name target -> `blocked: target-exists-identical|target-exists-different`; no rename, no merge.
- **Copy vs link**: across scopes (home <-> project) always copy (`--mode link` is blocked with `link-cross-scope`). Within one scope `--mode link` creates a relative symbolic link and implies keeping the source. A path containing a synced-folder name (iCloud, Dropbox, OneDrive, ...; a heuristic) downgrades the link to a copy, `warnings` has `sync-folder-demoted`, and the last step's `promise` is `cannot-promise`.
- **The source**: by default archived after verification to `~/.agent-pack/recovery/<timestamp>-relocate-<name>/skills/<name>` (a move, not a delete; same layout as the recovery directory of `reconcile --retire-skill`: `<timestamp>-<label>/skills/<name>`, asserted equal by a test); `--keep-source` copies and keeps it. The result gives `recovery_dir` and a one-line manual `mv` restore step; this version has no `restore` command.
- **Write order and rollback**: recovery directory (0700) -> copy under a temporary name beside the target -> read back (relative path and content digest per file; the `SKILL.md` digest equals the source's) -> rename into place -> archive the source. Any failure removes only what this run created, moves archived sources back and keeps the recovery directory. Symbolic links inside the directory that point outside it -> `source-has-external-symlink`.
- **Widening** (project -> user level) needs `--allow-widen`, `direction: "widen"`, shown in red in a terminal's text preview.
- **Entering a project repository** (user -> project level): `confirmations` says the content will enter the project directory and may be committed; `--apply` additionally needs `--confirm-project-content`. License and private content are for the user to judge; the tool cannot.
- A source without `file` evidence (broken directory, oversized `SKILL.md`) is read-only: `source-not-file-evidence`. MCP entries: `unsupported-kind`.

## JSON

`{schema_version, command: "relocate", mode: "preview"|"apply", plan, applied}`. `plan`: `ref`, `kind`, `source`, `target`, `mode`, `retire_source`, `direction` (`narrow|same|widen`), `status` (`ready|blocked|native`), `steps[{id,action,promise,...}]`, `blocked[{code,message,...}]`, `warnings[{code,message}]`, `confirmations[{id,text,flag}]`, `recovery_base`, `plan_digest`, `next.apply`. `promise`: `certain` (file-level fact), `best-effort` (needs a client restart or re-entering the project to confirm it shows up), `cannot-promise` (the tool guarantees file changes, not client behavior). `plan_digest` does not depend on the interface language (`message` / `text` are excluded). `applied.status`: `applied|rolled-back|refused|native`. Keys are only appended; the output has no file contents, only names, paths (abbreviated with `~` and `<project>`) and digests.
