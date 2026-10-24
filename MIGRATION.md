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

输出 JSON 的 `path` 是插件根目录（含 `plugin.json` 与 `runtime/`），不传 `--version` 时取最新；未找到时退出码为 1。默认不返回旧方式安装（agent-pack 1.6.2 起），清理前后结果一致；需要排查时加 `--include-legacy`。不想依赖命令时，按以下顺序自行查找：

| 客户端 | 位置 |
|---|---|
| Claude Code | `~/.claude/plugins/installed_plugins.json` 中 `<插件>@agenthelm` 条目的 `installPath`（旧版本目录可能残留，以记录为准） |
| Codex | `~/.codex/plugins/cache/agenthelm/<插件>/<版本>/` |
| Cursor | `~/.cursor/plugins/local/<插件>/` |

`agent_pack_config.py` 的 `inventory` / `bootstrap --plugin --client --replace` / `reconcile --retire-plugin --retire-only` / `doctor --project`、`mcp_env_launcher.py` 的 `--env-file` / `--config`、opinion-manager 的 `verify` / `publish` / `profile` / `compose` / `lifecycle` 参数与状态文件格式均未改变。

## 下游脚本：版本与提交钉定

- 版本：继续比较插件 `plugin.json` 的 `version`。当前各插件版本以 [`latest.json`](https://agenthelm.sakanano.moe/latest.json) 为准（6.5.2 发布时为 agents-init 6.5.0、agent-pack 1.6.2、project-orchestrator 1.7.0、opinion-manager 0.4.1）。
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

## 6.6.0：给已初始化的项目接入人用入口

不改也能继续使用：没有 `entry` 段的项目可以直接运行 `agenthelm project`。要在项目根目录放一个稳定入口：

1. 更新插件（`npx @sakanano-moe/agenthelm update` 预览 → `--apply`），或安装运行时（`npx @sakanano-moe/agenthelm runtime install --apply`）。
2. 预览补入口：`agenthelm project fix add-entry --entry-name <名称>`，或 `python3 <agents-init>/skills/agents-init/scripts/init_project.py --project <项目> --mode entry --entry-name <名称>`。只涉及 Profile 的 `entry` 段、启动器与内容锁。
3. 项目根目录已有同名脚本（例如自己写过的菜单）时视为你的文件：`--keep <名称>` 保留它（检查会提示它是否调用了 agenthelm），`--accept <名称>` 换成模板并先备份。自己的脚本要接入公共菜单，在脚本里调用 `agenthelm project --project "<项目根目录>" "$@"` 即可。
4. 需要时加 `--readme-entry`（或在菜单里对 README.md 选择采用），在 README 末尾追加带标记的「怎样使用」。
5. 确认后 `--apply`，再运行 `./<名称> health` 验证。

模板内容包 `agents-init/project-contract` 升到 1.1.0（`AGENTS.md` 增加「人用入口」，并开始跟踪启动器）。`agenthelm update` 或菜单「修复与更新」会列出受影响的文件：未改动的先备份再替换；你改过的逐个选择保留（`--keep`）或采用（`--accept`）。

项目自己管理事项（不用默认 Case 工具）时，按 [case-workspaces.md「项目入口适配」](plugins/project-orchestrator/references/case-workspaces.md#项目入口适配) 在 `entry.items` 声明只读接口；没有声明写接口时，菜单里的记录事项会显示为不可用，不会绕过项目工具改文件。项目专用操作写进 `.agents/moe.sakanano.agent-pack/tools.json`。
