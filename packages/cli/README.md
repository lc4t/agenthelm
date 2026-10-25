# @sakanano-moe/agenthelm

**English** · [简体中文](https://github.com/lc4t/agenthelm/blob/main/packages/cli/README.zh-CN.md)

The command line for [agenthelm](https://github.com/lc4t/agenthelm), a set of cross-agent project management plugins for Claude Code, Codex and Cursor: **agents-init** (project contract), **project-orchestrator** (project lifecycle), **agent-pack** (Plugin / Skill / MCP management) and **opinion-manager** (Agent behavior rules).

> The interface and the Skills are currently written in Chinese. An English interface is planned for a later release.

## What it does

- **Install, check and update the plugins** through each client's own marketplace. Every write previews first; add `--apply` to run it.
- **User-level runtime**: install the deterministic tools once into `~/.local/share/agenthelm/`. After that, project launchers work without npx, without network access and without any AI client.
- **Project menu without an AI agent**: `agenthelm project` (or the project's root launcher, `./menu` by default) finds files, shows progress, explains the project, checks and maintains it, and records work items. It never calls a model.

## Requirements

- Python 3.11 or later
- Node.js 18 or later (only for `npx`)
- Verified on macOS and Linux. The runtime commands do not support Windows.

## Quick start

```bash
npx @sakanano-moe/agenthelm install            # preview installing the plugins into detected clients
npx @sakanano-moe/agenthelm install --apply
npx @sakanano-moe/agenthelm runtime install --apply   # once: the user-level runtime
npx @sakanano-moe/agenthelm project --demo study      # try the project menu on a fictional project
```

In an initialized project:

```bash
./menu                       # interactive menu
./menu find notes 2026-10    # find files by keyword and date
./menu progress              # items grouped by status
./menu health                # checks, each with its reason, impact and next step
./menu --help
```

Add `--output json` to any subcommand for scripts and agents. Commands never wait for input when they are not in a terminal. Exit codes: 0 success, 1 nothing found or something must be handled, 2 usage error, 3 runtime missing or incompatible, 4 capability unavailable, 5 write conflict, 6 execution failed.

## Offline

Download `agenthelm-v<version>.tar.gz` from [GitHub Releases](https://github.com/lc4t/agenthelm/releases), extract it, then run:

```bash
python3 <extracted>/plugins/agent-pack/runtime/agenthelm.py runtime install --apply
```

Without network access you can still find files, see progress, read the guide and run local checks. Update checks use the cached version information and show when it was fetched.

## Check and update

```bash
npx @sakanano-moe/agenthelm check          # read-only: plugins, project templates and Opinion
npx @sakanano-moe/agenthelm update         # preview
npx @sakanano-moe/agenthelm update --apply
agenthelm runtime update --apply           # new runtime beside the old one
agenthelm runtime use <version> --apply    # switch back
```

## Links

- Repository and full documentation: https://github.com/lc4t/agenthelm
- Site and version source: https://agenthelm.sakanano.moe/
- Changes: [CHANGELOG.md](https://github.com/lc4t/agenthelm/blob/main/CHANGELOG.md) · Migration: [MIGRATION.md](https://github.com/lc4t/agenthelm/blob/main/MIGRATION.md)

License: MIT
