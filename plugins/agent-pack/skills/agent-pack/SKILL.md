---
name: agent-pack
description: Deterministically inventory, verify, install, sync, migrate and reconcile Agent Plugins, Skills and MCP servers across projects, plugin directories and clients such as Codex, Cursor and Claude. Use when discovering, importing, exporting, syncing or verifying Agent capabilities, or diagnosing extension drift on a machine. The project lifecycle (Task/Case/routing/verification/Git) is not part of this Skill; that is project-orchestrator. 中文：确定性地盘点、校验、安装、同步、迁移与对账 Agent Plugin、Skill 与 MCP server——在项目、插件目录与 Codex / Cursor / Claude 等客户端之间。当需要发现、导入、导出、同步或验证 Agent 能力，或诊断某台机器的扩展安装漂移时使用。项目生命周期（Task/Case/路由/验证/Git）不在本 Skill 内——那是 project-orchestrator。
---

# Agent Pack（Agent 扩展能力管理）

## 定位

`agent-pack` 是**确定性工具**，不做生命周期决策。它只在项目、插件目录与已支持客户端之间 inventory / doctor / bootstrap / sync / transfer / reconcile。所有写操作默认 dry-run；重名、无效 manifest、外部 symlink、含疑似密钥的 MCP 条目一律失败关闭。

- 项目编排（选 Task/Case、路由、验证、Git 交付）归 **project-orchestrator**，不在本 Skill 内。
- 跨机整机恢复的**单一用户入口**是 `python3 tools/agent-bootstrap`（`doctor` / `install <client>` / `list`），它读取 `00-system/capabilities.json` 的 `agent_extensions` 契约并在内部调用本工具。日常先用它。
- 只有需要单组件级操作（transfer 某个 skill、reconcile 退役某个旧包、向另一个项目导出）时才直接用本工具。

## 入口

优先使用随附的 `agent-pack` MCP Tool（`agent_pack_inventory` / `agent_pack_doctor` / `agent_pack_bootstrap` / `agent_pack_sync` / `agent_pack_transfer` / `agent_pack_reconcile` / `agent_pack_cloud_sync_safety_check`）；不可用时运行标准库 CLI `runtime/agent_pack_config.py`。执行能力管理前读取 [references/capability-config.md](../../references/capability-config.md)。

`runtime/agenthelm.py` 另有两个面向人的入口：`project` 转交 project-orchestrator 的项目入口（不需要 AI Agent）；`runtime install|update|use|status` 管理用户级运行时（`~/.local/share/agenthelm/`，项目根目录的启动器优先使用它），写操作默认预览、`--apply` 才执行，新版本装在旁边、旧版本保留。

`agenthelm inventory`（只读）列出各客户端能看到的 Skill 与 MCP：存放位置、重复、description 字节数、可识别的来源；输出带 `schema_version` 的稳定 JSON，契约见 [references/inventory-contract.md](../../references/inventory-contract.md)。

## 技能与 MCP 管理（命令清单）

读命令零写入；写命令默认预览，`--apply` 必须带预览给出的 `--expect-plan <plan_digest>`（指纹不一致拒绝、退出码 1），本版只支持 macOS 与 Linux。Agent 先向用户展示预览，经同意才加 `--apply`；只有证据等级为 `file` 的条目才可能被处置，`unknown` 一律只读，不下结论。

- `agenthelm inventory [--client claude|codex|cursor|claude-desktop|all] [--scope project|user|all]`：技能与 MCP 的作用域、重复、description 加载成本、证据等级。`claude-desktop` 只读，不在默认客户端集合内，须点名或用 `--client all`。契约：[inventory-contract.md](../../references/inventory-contract.md)。
- `agenthelm clients`：只读，列出客户端驱动（内置与用户声明）及其能力。用户声明文件放在 `~/.config/agenthelm/clients.d/*.json`，本版**只校验、不执行**，不支持用新声明安装新客户端（#134 Phase 1）。
- `agenthelm relocate <ref> --to <客户端>:<作用域>`：迁移 Skill（复制或同作用域内相对符号链接，默认归档源、不删除）与 Claude 的 MCP 条目（`user|local|project`）。绝不覆盖同名目标；放宽须 `--allow-widen`，进入项目目录须 `--confirm-project-content`。契约：[relocate-contract.md](../../references/relocate-contract.md)。
- `agenthelm dedupe <分组id> …`：重复分组的处置（`--keep <ref> --retire-others`、`--keep-all --note`、`--ignore|--unignore`，决定写入 `~/.config/agenthelm/ext-decisions.json`），`--desktop-snippet` 只生成可粘贴到 Claude Desktop 配置的片段，不读写 Desktop 配置。契约：[dedupe-contract.md](../../references/dedupe-contract.md)。
- 受管与手工的区别（MCP）：state 里记录了本项目包归属的条目由工具退役（原片段与整份配置先备份、失败回滚）；手工装的或属于别的包的条目工具**不编辑**，只给 `claude mcp remove --scope <作用域> <名>` 与备份提示。Skill 副本只归档、不删除，本版没有 `restore` 命令，结果给出手工恢复的 `mv`。
- 作用域：Project Profile 的 `capabilities.default_scope` / `mcp_scope` / `skill_scope`（可选，不写则行为不变）与 `sync` / `reconcile --scope user|project|all`、`--project-store local|file`，细节与迁移预览、屏蔽检测见 [capability-config.md](../../references/capability-config.md)。
- 项目里的人用入口：`agenthelm project extensions list|dups|show|check|move|dedupe|scope`（菜单第 9 屏「技能与 MCP」），属于 project-orchestrator，写入类子命令转发到上面的命令。
- **未确认，须真机验证**：Claude local 级 MCP 条目是否弹批准；Claude Desktop 是否加载某条目（远程 url、请求头、SSE 官方文档未说明）；同步盘里相对符号链接是否可用（同步盘路径下改为复制）。这些工具都以 `promise: cannot-promise` 或 `best-effort` 标注，不写成已验证。

`agenthelm config` 管理用户级设置（`~/.config/agenthelm/config.json`），默认只预览，`--apply` 才写入，只改要改的键，替换前备份原文件：`config show` 显示生效的设置；`config language zh|en|auto` 设置界面语言（命令行输出用哪种语言，不改动项目文件）；`config language-hint on|off` 开关“界面语言未设置”的提示；`config project-language zh|en [--project <目录>]` 设置当前项目的文件语言（Profile 的 `language`，写进项目的文件用哪种语言；只改 Profile）。只有界面实际显示英文、且没有任何地方指定界面语言时，`agenthelm check` 与会话开始的提示才会说明当前语言从哪来并给出命令（中文界面不提示）；界面显示英文、已有项目没有 `language` 时 `check` 也会说明。两种语言的区别要向用户讲清楚：界面语言只影响屏幕上的文字，项目文件语言随项目同步。

## 执行顺序

1. 盘点来源与目标（`inventory --source project:/path | plugin:/path | client:codex | client:cursor | client:claude`）；
2. 把每个组件分类为 Agent Plugin、Skill 或 MCP server；
3. 拒绝重名、无效 manifest、外部 symlink 以及包含密钥的 MCP 配置；
4. 通过 dry-run 预览导入、导出与客户端同步；
5. 计划写入其他项目或用户级客户端时申请授权；
6. 只应用明确选择的组件；
7. 对目标运行 `doctor`。

目标客户端尚未安装某包时，从可信本地插件根目录使用 `bootstrap`。`bootstrap` 支持 `codex` / `cursor` / `claude`；Claude 端写入 `~/.claude/skills` 符号链接与 `~/.claude.json` user-scope MCP，可用 `--skill` / `--mcp` 只装选中的组件，本机认证值默认保留（`--reset-overrides` 才丢弃），装完需重启 Claude Code。把项目能力同步到 Claude 用 `sync --client claude`：它为 Claude 单独生成包，只增删改本包装过的条目，不要用 `bootstrap` 指向 Codex 的包。核对客户端是否与项目一致用 `doctor --client <客户端>`。处理已有客户端时优先使用 `reconcile`：审阅组件状态，明确列出要退役的旧 Skill/plugin，再执行应用。只退役、不安装项目包时加 `--retire-only`。用户验收迁移前，已退役或替换的内容必须保留在报告的恢复目录（`~/.agent-pack/recovery/`）中。

## 打包与协议

优先使用 Agent Plugins 1.0 作为可移植包格式：根目录 `plugin.json`、直接子目录 `skills/<name>/SKILL.md` 和根目录 `mcp.json`。客户端专属投影（`.codex-plugin/`、`.mcp.json`）只作为 adapter 保留，且其 `name` / `version` 由 `plugin.json` 生成，禁止手改。以 MCP 2026-07-28 为主要 wire revision；已安装客户端仍要求旧版本时保留兼容投影。

## 版本完整性

`plugin.json#version` 是唯一权威。`doctor` 对每个被盘点插件比对仓库 `plugin.json` 版本与其 `.codex-plugin/plugin.json` 适配器版本，不一致即报 issue（fail，不是 warning）。`~/.codex/plugins/cache/personal/` 是 Codex 自身的版本化缓存，滞后于 marketplace 源，须由 Codex 下次激活或重启刷新——这不是 `agent-pack` 能写的。

## 同步盘与本机专属目录（local-only）

项目放在 iCloud、Dropbox、OneDrive 等同步目录里时，`.venv`、`node_modules`、工具缓存不该进同步盘。只读 MCP Tool `agent_pack_cloud_sync_safety_check` 返回结构化发现（含 `attention`、证据、可执行的命令）；改动由用户同意后运行 `agenthelm local-only fix … --apply`（默认预览，失败整体撤销）。Agent 不要自己 `mv` / `ln -s`；体积接近阈值的发现把选项交给用户。契约、实测兼容性（yarn / bun / uv 可外置，npm 会把链接换回真实目录，pnpm 会失败）、配置与多设备流程见 `references/local-only.md`。

## 边界

- 不初始化项目、不创建骨架（那是 `agents-init`）。
- 不选择 Task/Case、不写项目进度、不做 Git 提交（那是 project-orchestrator）。
- 不把原生客户端 MCP 配置中的凭据复制进可移植包；含密钥条目保持 `blocked` 并留在客户端本地。

## 观点候选采集与未决事项提示（nudge）

插件随附的 hook 调用 `runtime/agenthelm.py nudge hook`：在项目启用了 opinion-manager 时，会话开始注入候选采集协议与试行候选，工作中提醒暂存长期偏好，任务完成时请用户集中确认，并在待确认候选堆积或久未处理时提示一行。该工具只读写 opinion-manager 的收件箱，不写规则、不改项目文件。阈值、客户端差异与关闭方式见 [references/nudge.md](../../references/nudge.md)；`agenthelm nudge status` 显示生效配置，`agenthelm nudge install-hooks --client cursor` 预览 Cursor 的 hook 配置。
