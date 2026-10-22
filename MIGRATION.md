# 迁移到 agenthelm 市场分发

适用于：用旧方式（agent-pack bootstrap 复制 + `~/.claude/skills` 软链）安装过 lc4t/skill 的机器，以及把插件版本、提交或安装路径写进自己脚本与 Project Profile 的下游项目。版本差异与不兼容点的完整清单在 [`CHANGELOG.md`](CHANGELOG.md) 与 [`release/migrations.json`](release/migrations.json)。

## 顺序很重要

1. **先装新的**：`npx @sakanano-moe/agenthelm install`（预览）→ 追加 `--apply`。新旧安装此时并存，旧路径仍可用。
2. **再改下游**：按下文更新查找路径、版本钉定与 Profile 字段，并在新会话中验证。
3. **最后清旧的**：`npx @sakanano-moe/agenthelm migrate`（预览）→ 追加 `--apply`。旧文件移入 `~/.agent-pack/recovery/<时间>-agenthelm-migrate/`，`migration.json` 列出每一项的原位置，可按清单移回。

先执行第 3 步会让仍按旧路径查找插件的下游脚本立刻失效。

## 下游脚本：查找插件的位置

旧查找顺序（`~/.agents/plugins/plugins/<id>`、`~/.claude/agent-pack/plugins/<id>`、`~/.codex/plugins/cache/personal/<id>/<版本>`）在迁移后找不到插件。改为：

```bash
npx @sakanano-moe/agenthelm locate opinion-manager --version 0.4.1
python3 <agent-pack 目录>/runtime/agenthelm.py locate opinion-manager --version 0.4.1
```

输出 JSON 的 `path` 是插件根目录（含 `plugin.json` 与 `runtime/`），不传 `--version` 时取最新；未找到时退出码为 1。不想依赖命令时，按以下顺序自行查找：

| 客户端 | 位置 |
|---|---|
| Claude Code | `~/.claude/plugins/installed_plugins.json` 中 `<插件>@agenthelm` 条目的 `installPath`（旧版本目录可能残留，以记录为准） |
| Codex | `~/.codex/plugins/cache/agenthelm/<插件>/<版本>/` |
| Cursor | `~/.cursor/plugins/local/<插件>/` |

`agent_pack_config.py` 的 `inventory` / `bootstrap --plugin --client --replace` / `reconcile --retire-plugin --retire-only` / `doctor --project`、`mcp_env_launcher.py` 的 `--env-file` / `--config`、opinion-manager 的 `verify` / `publish` / `profile` / `compose` / `lifecycle` 参数与状态文件格式均未改变。

## 下游脚本：版本与提交钉定

- 版本：继续比较插件 `plugin.json` 的 `version`。本次各插件版本为 agents-init 6.5.0、agent-pack 1.6.0、project-orchestrator 1.7.0、opinion-manager 0.4.1。
- 提交：市场安装没有本地 Git 工作区，“HEAD 等于钉定提交”无法再检查。改为钉定 tag（`v6.5.0` 起每次发布都有 tag 与带 SHA-256 的发布包），或只钉版本号。
- 需要固定到某次发布时，添加市场时带上 ref：`claude plugin marketplace add lc4t/agenthelm#v6.5.0`、`codex plugin marketplace add lc4t/agenthelm --ref v6.5.0`。钉定后不会收到更新提示之外的自动升级。

## Project Profile

`.agents/moe.sakanano.agent-pack/project.json` 的 `runtime` 段：

| 字段 | 旧值 | 新值 |
|---|---|---|
| `distribution` | `bundled` | `marketplace` |
| `source_repository` | `lc4t/skill` | `lc4t/agenthelm`（仓库已改名；旧地址由 GitHub 重定向，钉定的旧提交仍可取到） |
| `source_ref` | `v6.4.0` | 采用的发布 tag，如 `v6.5.0` |
| `source_commit` | 提交 SHA | 可保留作审计记录；不再用于“本地 HEAD 一致”检查 |
| `plugin_version` / `capability_manager_version` | 1.6.0 / 1.5.1 | 1.7.0 / 1.6.0 |

`opinion.tool_version` 改为 0.4.1。Schema 仍接受 `bundled` 与 `external`，旧 Profile 不改也能通过校验；改为 `marketplace` 后 `agenthelm check` 的提示与实际安装方式一致。

## 项目里的模板文件

v6.5.0 之前初始化的项目没有 `.agents/moe.sakanano.agent-pack/content.lock.json`。之后模板更新时，`npx @sakanano-moe/agenthelm update` 会把与新模板不同的文件都视为本地改动，逐个要求 `--accept`（用新模板，先备份）或 `--keep`（保留本地）；选择后即建立基线。
