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

## 6.7.0：Claude Code 端的项目能力同步

以前用 `bootstrap --plugin ~/.agents/plugins/plugins/<项目包> --client claude` 把项目能力装进 Claude 的，Claude 依附在 Codex 的包上。Codex 端删除或排除某个 MCP 后，Claude 的条目会失效，Claude 自己的 `mcp_client_policy` 也不生效。改为：

1. 预览：`python3 <agent-pack>/runtime/agent_pack_config.py --output json sync --project <项目> --client claude`。会列出要新建的 Claude 专属包、要改指向的安装记录、要更新的 Skill 链接和 MCP 条目。本机认证值（如 `headers.Authorization`）默认保留，结果里只出现键名。
2. 确认后加 `--apply`。旧内容进入 `~/.agent-pack/recovery/<时间>-claude-<包名>/`，其中 `record.json` 记着旧记录原来指向哪里。
3. 核对：`doctor --project <项目> --client claude`，`clients.claude.in_sync` 应为 true。
4. 以后项目 MCP 有增减，重新运行第 1、2 步即可，不再需要手工从恢复目录还原认证头。

## 6.8.0：脚手架一致性与多机使用

不用改任何东西也能继续使用；升级后在项目里运行 `agenthelm check` 会多出“脚手架”项（建议处理，不改变 `health` 的退出码）：

1. 看有哪些差异：`agenthelm check`。每项带 ID（`skel:<路径>`、`content:<路径>`、`profile:<字段>`、`lock:<包>`、`case-naming`）。
2. 逐项处理或跳过：
   - 缺失的骨架文件：`agenthelm fix add-skeleton`（预览）→ `--apply`。只新建，已有文件不动。
   - 与新模板不同的文件（旧项目没有内容锁基线）：`agenthelm fix update-content`，逐个 `--keep <文件>` 或 `--accept <文件>`；`--keep` 会被记住，同样的模板内容不再追问。
   - Profile 缺字段：`agenthelm fix profile`（预览里有 diff，替换前备份）。你自己的字段不会被改动。
   - 内容锁里的 `0.0.0`：`agenthelm fix repair-lock`。如果基线与当前模板不同，会提示改用 `update-content` 逐文件选择。
   - 不想要的：`agenthelm fix --skip <ID> --reason "…" --apply`。决定写入 `.agents/moe.sakanano.agent-pack/check-decisions.json`，随项目同步，换一台机器不会再提示；`--unskip <ID>` 撤销。
3. 项目用自己的 Case 目录命名（例如 `YYYYMMDD-主题`）时，在 Profile 声明 `work.case.directory_naming`，否则 `check` 会按默认的 `CASE-日期-序号-主题` 报告不符：

   ```json
   "work": {"case": {"directory_naming": {"style": "date-topic"}}}
   ```

   `fix case-naming` 只给改名计划和受影响的引用，不会移动目录。
4. 两台机器的脚手架是否一致：各自运行 `agenthelm check --fingerprint`，指纹相同即一致；不同时在一台保存 `--output json`，在另一台用 `--explain <文件>` 看差在哪里。

不兼容点：

- `--mode entry` 之后内容锁里的版本是当前包版本，不再是 `0.0.0`。已有的 `0.0.0` 需要 `fix repair-lock`，不会被自动改写。
- `agenthelm check --strict` 与会话开始的一行提示会把未解决的脚手架差异算作未处理。
- Profile 的 `runtime.source_ref`、`plugin_version`、`source_commit`、`capability_manager_version`、`opinion.tool_version` 现在在 schema 里声明为审计字段：只作记录，运行时不读取，也不参与比较。以前 MIGRATION 里让你改的 `source_ref` 等仍可保留或更新，两者都不影响 `check`。

多机与同步盘的说明见 [README「多机与同步盘」](README.md#多机与同步盘)。

## 6.9.0：CLAUDE.md 导入桩、接管已有内容的目录、受管区块

不用改任何东西也能继续使用。升级后 `agenthelm update`（或 `fix update-content`）会列出 `CLAUDE.md` 与 `AGENTS.md` 的变化：

1. **CLAUDE.md 变成一行 `@AGENTS.md` 的导入桩。** Claude Code 只在工作目录及其上级目录都没有 `CLAUDE.md` 时才直接读取 `AGENTS.md`（v2.1.277 起，[官方文档](https://code.claude.com/docs/en/memory#agents-md)），两者并存时只读 `CLAUDE.md`；旧版本、禁用内置插件以及部分会话读不到 `AGENTS.md`，所以保留导入桩。没改过的 `CLAUDE.md` 先备份再替换；改过的逐个 `--keep` / `--accept`。读取 `CLAUDE.md` 里旧路由文字的外部脚本需要改读 `AGENTS.md`。
2. **`AGENTS.md` 模板多了一行“用户规则”**，指向 `AGENT.USER.md`（只有接管时从原 `AGENTS.md` 移出内容才会有这个文件）。同样在 `update-content` 里逐文件选择。
3. **你自己的 `CLAUDE.md` 没导入 `AGENTS.md`？** `init_project.py --mode scan` 会提示项目契约不会被 Claude 加载；`--mode adopt` 不适用于已初始化的项目，这种情况直接在你的 `CLAUDE.md` 里加一行 `@AGENTS.md`，或手动追加受管区块。
4. 新的发现 `region:<文件>#<ID>` 只在你用 `adopt` 追加过受管区块后出现，处理方式见 `init_project.py --mode region`。

对下游的说明：发布条目（`release/migrations.json`）现在带 `user_action_required`、`touches`、`default_for_existing`、`template_changes`，可据此判断一次升级对你的项目意味着什么；`CHANGELOG.md` 的“不兼容点与注意”仍是完整清单。
