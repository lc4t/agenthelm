# 客户端驱动注册表（#134 Phase 1）

状态：设计稿 + Phase 1 实现。基线 dev `c45547a`。范围只含「解耦客户端枚举并预留扩展点」；接入新客户端（Phase 2）与改造运行时查找顺序（Phase 3）本版不做。

## 0. 结论摘要

1. 新增 `plugins/agent-pack/runtime/agent_pack_clients.py`：**声明式驱动注册表**（数据，不是按名字的 `if` 分支），内置 `claude` / `codex` / `cursor` 与只读的 `claude-desktop`。
2. 现有的客户端名单（`agenthelm.py` 的 `CLIENTS` / `CLIENT_KEYS` / `CLIENT_NAMES` / `--client` choices / `detect_clients`，`agent_pack_inventory.py`、`agent_pack_nudge.py`、`user_settings.py` 的枚举）改为读注册表；内容与顺序不变，既有输出逐字节不变。
3. 新增只读命令 `agenthelm clients`：列出每个驱动的检测结果（依据哪一项）、分发驱动类型、能力标志。
4. 预留用户级声明 `~/.config/agenthelm/clients.d/*.json`（遵循 `XDG_CONFIG_HOME`）：本版**只校验**，非法的警告并忽略，合法的标「已声明，本版未支持安装」，**不执行其中任何内容、不据此写任何文件、不进入 `--client` 可用集合**。
5. 存储与写入逻辑（`agent_pack_config.py`、`agent_pack_claude.py`、`agent_pack_inventory.py` 的存储表）本版不动；注册表里的存储声明与 inventory 现有表用测试对账，避免两份数据漂移。

## 1. 数据模型

一个驱动是一个 JSON 对象（内置驱动用同一形状，写在代码里）：

- `schema_version`：1。
- `id`：`^[a-z][a-z0-9-]{0,31}$`；不得与内置 id 或 `all` 冲突。
- `display`：显示名。
- `detect`：检测依据，只放事实：`executables`（可执行文件名，在 PATH 与已知位置查找，**只查找不运行**）、`dirs`（配置目录；内置驱动可带环境变量覆盖，如 `CLAUDE_CONFIG_DIR`、`CODEX_HOME`）。任一命中即为检测到，输出里记录命中的那一项。
- `distribution`：分发驱动类型，对应 #134 的两种驱动：
  - `marketplace-cli`：客户端自带插件市场命令（`claude plugin …`、`codex plugin …`）。
  - `directory-dropin`：把插件目录放进客户端目录（Cursor 的 `~/.cursor/plugins/local`）。
  - `none`：本工具不向它分发（`claude-desktop` 只读）。
- `stores.skills[]` / `stores.mcp[]`：`{scope: user|local|project, path: 模板, format: dir|json|toml}`，路径模板中 `~` 为家目录、`<project>` 为项目根。
- `capabilities`：布尔能力标志。本版定义：`default_set`（是否属于默认客户端集合）、`read_only`（只读目标）、`project_mcp_write`（是否支持项目级 MCP 写入）。后续新增标志只追加。

### 与 W-a「作用域能力」的衔接

`capabilities.project_mcp_write` 是「某客户端是否支持项目级写入」的**唯一来源**；注册表提供查询函数 `supports(client, capability)`。#135 引入的 `agent_pack_config.project_write_supported(client)` 现在读这个标志（取值 claude 为真，codex、cursor、claude-desktop 为假，与改前一致，并由测试对账），不再按客户端名字判断。

## 2. 选项与排除理由

- **A. 继续各模块各留一份元组**：排除，#134 要解决的正是这个。
- **B. 把声明做成可执行配置（声明里带安装命令）**：排除。让用户文件决定要运行什么命令，是任意命令执行面；本版声明只含数据字段，出现未知字段（如 `command`）整份拒绝。
- **C. 从 PATH 上枚举「看起来像客户端」的程序**：排除，不可靠且不是协议依据。
- **D（采用）**：内置驱动写成数据 + 用户声明只校验；安装实现留到 Phase 2，届时每种 `distribution` 类型一个实现，由类型而不是客户端名字选择。

## 3. 对既有项目的影响

- 无用户动作；`agent-pack` minor，`default_for_existing: off`。
- `agenthelm --help` 只多出 `clients` 一行（新命令必然的变化）；各子命令的 `--help` 与既有命令输出不变（对照基线逐字节核对）。
- `claude-desktop` 仍不在默认集合，须点名或 `--client all`。
- 未知 `--client` 沿用 argparse 的 `invalid choice` 并列出可选 id（默认集合；`inventory` 含 `claude-desktop`、`all`）。用户声明的 id 不在可选集合内。
- 其他插件（project-orchestrator 的 `CLIENT_LABEL` 等）不 import agent-pack，本版不动。

## 4. 测试策略

注册表派生的枚举与旧元组相等；驱动词汇合法；存储声明与 inventory 表对账；`agenthelm clients` 的文本与 JSON、检测依据；合法声明列出且不可安装；各类非法声明（非对象、损坏、含 `command`、内置 id、路径含 `..`、可执行名含参数、未知能力）警告并忽略；声明里的命令与 PATH 上同名脚本不被执行（标记文件不出现）；`clients` 前后磁盘不变；声明 id 不是可用的 `--client`；写命令登记表把 `agenthelm clients` 登记为 `READ_ONLY`。

## 5. 决策项

- **D-R1**：声明文件只允许数据字段，未知字段整份拒绝。默认：是。
- **D-R2**：用户声明与内置 id 冲突时拒绝并警告（不覆盖内置）。默认：是。
- **D-R3**：`nudge`、`user_settings` 的枚举也读注册表（只取默认集合）。默认：是，行为不变。
- **D-R4**：声明的 `dirs` 限定 `~/` 开头；`executables` 限定为不含路径的文件名。默认：是。

## 6. 将来如何接入新客户端（文字示例，不含路径）

以 Antigravity 为例：Phase 2 在确认其可执行文件名、配置目录、技能与 MCP 存储位置与格式、是否有插件市场命令之后，写一份驱动声明：`id`、`display`、`detect`（可执行文件名与配置目录）、`distribution`（有市场命令则 `marketplace-cli`，否则 `directory-dropin`）、`stores` 与能力标志，再给对应的 `distribution` 类型补实现（若已有实现则无需新代码）。这些取值都需要实机确认，**本版不写入代码与文档的路径**。

## 7. 未核实清单

- `codex` 与 `cursor` 的 `project_mcp_write` 取 `false` 只表示「agenthelm 目前不写」，不是客户端能力结论（inventory 能读到两者的项目级 MCP 文件）。
- `claude-desktop` 的检测目录取自 inventory 已引用的官方文档位置，Windows 未覆盖；未实机核对。
- 用户声明的 `stores` 本版只校验形状，不核对路径是否存在或格式是否真实。
- 其余客户端名单（`agenthelm.py` 的安装、更新、legacy 分支，`agent_pack_config.py` 的存储选择，nudge 的事件能力差异）属于「存储」与「能力差异」类，留给后续按能力标志逐步替换。
