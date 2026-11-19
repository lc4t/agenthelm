# 技能与 MCP 管理菜单：交互、去重与迁移的设计文档（CB 组）

状态：设计稿，待维护者审阅。基线 `v6.13.1`（dev 分支）。本文只描述概念、界面、命令形态、写入语义与拆分，不含代码，不改任何运行时、版本、变更片段或发布载体。
上游设计：[#82 `docs/design/mcp-skill-multi-project.md`](./mcp-skill-multi-project.md)（下称 **#82**，决策项 D1–D15、拆分 I-1…I-12）。数据来源：M1 组并行实现的只读 `agenthelm inventory`（下称 **inventory**）。同版本的姊妹设计：`docs/design/opinion-skill-routing.md`（CA 组撰写，本文只预留接口位置，见 §7）。

## 0. 结论摘要

1. **入口**：在项目菜单（`./menu` = `agenthelm project`）新增第 9 屏「技能与 MCP」，对应非交互命令组 `agenthelm project extensions …`；「能力状态」屏只增加一行可用性，「检查项目与环境」屏对「发现重复」增加一个跳转动作。不改动现有 1–8 号的序号。（§3、D-CB1）
2. **职责切分**：菜单与 `extensions` 命令只做展示、选择、预览确认与读回，全部数据与写入经子进程调用 agent-pack 的 `agenthelm` 命令并解析 JSON。写入由 agent-pack 新增的两个命令 `agenthelm relocate`（作用域或客户端之间移动、复制）与 `agenthelm dedupe`（重复处置）承担，均「默认预览、`--apply` 才写」，预览带 `plan_digest`，应用时必须回传同一摘要，保证预览等于应用。（§3.3、§4、§5）
3. **「生效范围」的表达**：菜单里的三个选项（全局 / 仅此项目 / 仅某客户端）落到 #82 I-4 的 `mcp_scope` / `skill_scope` 声明与 `sync` 的存储映射，不新增第二套声明。**只收窄不放宽**：收窄是默认动作；放宽（项目→用户级）是单独动词，需要 `--allow-widen` 与键入条目名确认。（§2）
4. **重复 MCP**：判据按「同名」「同指纹不同名」「同名不同指纹」三类；「哪份生效」只在官方文档写明优先级时才下结论，其余一律 `优先级未文档化`。处置四选一：保留一份并退役其余 / 都保留并标注 / 改作用域 / 忽略。（§4）
5. **关键发现（影响实施）**：agent-pack 现有 `reconcile --retire-*` 只支持 **Skill 与插件**（`agent_pack_config.py:2108-2109`，`agent_pack_claude.py:389-402`），对 MCP 条目只产出手工残留提示（`:250-268`，因为 `~/.claude.json` 可能含本地凭据，故刻意不自动编辑）。「MCP 退役经归档、可恢复」目前没有现成路径，须新增（拆分项 CB-3，决策 D-CB6）。另外 `reconcile` 当前没有「从归档恢复」命令（只有失败时的事务回滚，`:590-593`），恢复目前是手工移回。（§4.4、§9）
6. **技能**：工具只做机械判断（缺 description、体积、每会话 description 加载成本、同名漂移、可识别来源时的过期），**明确不做质量打分**。迁移用「复制 + 读回校验 + 归档源」，跨作用域不用指向 home 的符号链接；项目内相对符号链接沿用 #82 D3，Claude Code 官方文档确认支持符号链接，同步盘行为仍未核实。（§5）
7. **拆分**：9 个 issue 草稿（CB-1…CB-9），分三个波次。波次一只含 orchestrator 的只读屏与 inventory 字段请求，波次二、三才碰 agent-pack 写入路径，并与 #82 的 I-4 / I-7 在 `agent_pack_claude.py` 上串行。无 major。（§8）

**这个领域的标准流程**：包与配置管理的通用做法是「盘点（inventory）→ 解析（算出每份条目的归属与优先级）→ 预览（diff，不写）→ 应用（事务、先备份）→ 读回校验 → 退役（归档、可恢复）」，关键环节是作用域与优先级显式化、证据分级与不可逆动作前的确认。**当前行业内「好」的判据来源**：[OpenSSF Scorecard Pinned-Dependencies](https://github.com/ossf/scorecard/blob/main/docs/checks.md) 与 [MCP Registry server.schema.json](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json)（#82 §0 已采用，本文不重述）；Skill 的格式与成本判据来自 [Agent Skills 规范](https://agentskills.io/specification)（`name` ≤64 字符、`description` ≤1024 字符、元数据约 100 token 在启动时常驻、`SKILL.md` 建议 <500 行 / <5000 token，2026-11-18 读取）与 [Claude Code Skills 文档](https://code.claude.com/docs/en/skills)（2026-11-18 读取，见 §5.3）。这些是社区或厂商约定，不是对本仓库的强制要求；本文把它们当作「机械提示」的阈值来源，不当作质量结论。

---

## 1. 前提与边界

### 1.1 已读到的事实（仓库）

- 菜单：`project_menu.py` 的 `MENU` 为 8 项（找资料、看进展、项目指南、检查项目与环境、修复与更新、记录事项、项目工具、流程（SOP）），`SCREENS` 按序号映射；`health_screen` 通过 `finding["next"][0]["action"]` 的动词（`fix` / `open`）跳转；`confirm()` 要求输入 `yes`；各屏 `ask()` 回车返回、`q` 退出。
- 非交互：`project_cli.py` 的 `COMMANDS` 字典与 `parser()`；全部子命令继承 `--project`、`--output text|json`、`--no-input`、`--demo`、`--offline`；写命令带 `--apply`；JSON 外壳为 `{ok, attention, exit_code, command, demo, data}`。
- 预览/应用模式：`project_maintain.plan()` 生成 `{steps:[{id, title, blocked, custom, payload}]}`，`apply()` 逐步执行；预览与应用共用同一规划函数，`--apply` 前后不换逻辑；每个写入步骤有恢复目录（`_recovery_dir`，缺省 `~/.agent-pack/recovery/<时间戳>-agenthelm-<label>`）。
- 跨插件调用：project-orchestrator 通过 `subprocess.run([...], capture_output=True, text=True)` 调用外部脚本并 `json.loads` 其 stdout（`_run_refresh`、`_run_init`），不 import 其他插件；失败时转为 `EntryError`。本设计沿用该模式，调用对象是 `agenthelm` 命令。
- agent-pack 写入事务：`agent_pack_claude._apply` 在 `recovery_base/<时间戳>-claude-<名>` 下按 bucket 归档被替换物（`os.replace`），失败时按 `moved` 逆序回滚。
- 命令登记：`docs/write-commands.md` 的盘点表 + `tests/test_i18n_commands.py` 的 `READ_ONLY` / `PREVIEW` / `HELP_ONLY` / `DIRECT_WRITE` 字典与 `CASES`；新增子命令漏登会让测试失败。
- i18n 规模：`plugins/project-orchestrator/runtime/locale/en.json` 现有 1467 条，`plugins/agent-pack/runtime/locale/en.json` 现有 640 条。

### 1.2 数据契约：inventory

按统筹提供的口径：JSON 带 `schema_version`；技能条目含 `name / client / scope / store / description_bytes / content_sha256 / origin`；MCP 条目含 `name / client / scope / store / transport / 条目指纹`，**永不输出密钥值**；证据等级 `file / setting / cli / unknown`；技能「过期」只对能识别来源与版本者给出，其余 `unknown`；`claude-desktop` 为独立只读目标；只支持 macOS 与 Linux。本文把它当作唯一数据源，需要补的字段集中在 §10「对 inventory 契约的请求」。

### 1.3 不在本文范围

- #82 已回答的内容（存储映射表、同名优先级的官方出处、观测记录、`doctor` 就绪、目录、凭据文件）。
- opinion 路由（CA 组）。
- 任何运行时代码、版本号、变更片段、发布载体。

---

## 2. 范围模型：作用域选项与「生效范围」

### 2.1 各组件的作用域选项

存储与优先级的完整映射见 #82 §2.3 与 §3「存储映射表」，不重抄。下表只列菜单要呈现的**选项集合**（每一项对应一个真实存储，菜单里一律显示「存储 = 路径类别」，不只显示抽象的作用域名）：

- **MCP**
  - Claude Code：`项目文件`（`.mcp.json`，随项目分发，需批准）/ `本机项目级`（local，`~/.claude.json` 内按项目路径）/ `用户级`（`~/.claude.json` 顶层）。
  - Codex：`项目`（`.codex/config.toml`，需受信任项目）/ `用户`（`~/.codex/config.toml`）。
  - Cursor：`项目`（`.cursor/mcp.json`）/ `用户`（`~/.cursor/mcp.json`）。
  - **Claude Desktop 应用级**：`claude_desktop_config.json`，只对 Desktop 应用生效；本版只读（#82 I-5），写入属 I-6。
- **Skill**
  - Claude Code：`项目`（`.claude/skills`）/ `用户`（`~/.claude/skills`）。
  - Codex：`项目`（`.agents/skills`）/ `用户`（`~/.agents/skills`）。
  - Cursor：`项目`（`.agents/skills` 或 `.cursor/skills`）/ `用户`（`~/.agents/skills` 或 `~/.cursor/skills`），并兼容读取 `.claude/skills`、`.codex/skills` 与 `~/.claude/skills`、`~/.codex/skills`（#82 §2.3 事实 c）。
  - Desktop 没有 Skill 目录。

### 2.2 用户的「生效范围」选择与落盘

菜单把选择压缩为 4 个选项，每个选项映射到已有（#82 I-4）或已有命令的落盘动作，**本文不新增声明键**：

- **全局（所有项目）**：Profile 里该条目不出现在 `mcp_scope` / `skill_scope`（缺省 `default_scope: user`）；存储在用户级。
- **仅此项目**：`mcp_scope.<名>: "project"` 或 `skill_scope.<名>: "project"`；存储按 #82 映射表落到 Claude `local` / `.mcp.json` / `.claude/skills`，Codex 与 Cursor 的 Skill 为 `project-native`（无写入）。
- **仅某个客户端**：不属于 I-4 的作用域轴，属于既有 `mcp_client_policy`（`include` / `exclude`）与 `sync --client`。菜单只在「迁移到其他客户端」流程里使用，不引入新声明。
- **Desktop 应用级**：只读展示；要迁入时输出「可粘贴的片段与手工步骤」（§4.5），不写文件。

「在哪些项目生效」的语义：**声明是逐项目的**（每个项目的 Profile 各自声明），所以菜单在项目 X 里只改 X 的声明。选「全局」改「仅 X」意味着其他项目失去该条目，菜单必须先列出受影响的项目（来自 `~/.claude/agent-pack/state/*.json` 的归属记录，#82 §8 的局限同样适用：只能看到在本机同步过的项目）。同时在多个项目生效 = 每个项目各做一次「仅此项目」，菜单提供「也加入其他项目」时只给出命令行（`agenthelm project extensions scope … --project <路径>`），不在一个菜单会话里跨项目写入（决策 D-CB5）。

**未受管（loose）条目**：用户手工装的 MCP 或 Skill 不在任何项目声明里。把它们「变成项目级」需要先纳入项目声明：MCP 复用既有 `agent_pack_config.py transfer --from client:<X> --to-project P`（#82 §1），再写 `mcp_scope`、`sync`、最后退役用户级原条目；这条组合是一个事务计划，在预览里分步显示。含内联密钥值的条目无法通过 `_portable_mcp` 校验，计划标 `blocked: 含内联密钥值，改为 ${VAR} 引用后再迁移`，不尝试自动改写（改写会读取并搬运密钥值）。

### 2.3 「只收窄不放宽」在菜单里的呈现

- 每个目标作用域用「覆盖面」标注：`对所有项目生效` > `仅此项目` > `仅此项目且不入版本库`（Claude `local`）。
- **收窄**（全局→项目、项目文件→local）是默认动作，预览里标 `收窄：N 个项目将不再看到它`，N 与项目名来自归属记录；未知项目标 `未知`。
- **放宽**（项目→全局、local→用户级）只在用户主动选择时出现，菜单里用单独的动作键，标红 `放宽：此后所有项目都会看到它`，必须键入条目名确认，CLI 需要 `--allow-widen`。
- 任何**回退路径**（目标存储不可写、证据不足、客户端不支持项目级）一律「跳过并警告」，不改写到更宽的作用域（#82 §9 条 6）。例如声明为 `project` 的 Codex MCP 在 I-12 之前一律跳过。

---

## 3. 菜单与 CLI 的流程设计

### 3.1 屏幕清单与进入路径

- **新增第 9 屏「技能与 MCP」**，放在「维护」分隔线之下、SOP 之后（现有 1–8 的序号与 `SCREENS` 映射不变，不破坏肌肉记忆与既有测试）。能力 id 为 `extensions`。
- **并入点（不新增屏）**：
  - 「能力状态」（`agenthelm project capabilities` 的输出）增加一项 `extensions`：`available` 当且仅当 `agenthelm inventory` 可调用；否则 `unavailable` 并写原因（`未找到 agent-pack 的 inventory`），与现有 `record` / `tools` 的 `state/reason` 形态一致。
  - 「检查项目与环境」屏：`finding["next"]` 增加动词 `ext:<分组id>`，指向第 9 屏的重复详情。`health` 的发现来自 inventory 的重复归类（#82 I-1），默认 advisory，不改变 `ok`。
- 备选与排除：
  - 并入「能力状态」做成第二个功能页。排除：那是「项目能做什么」的只读说明，不是任务入口，且它在 CLI 里是 `capabilities` 子命令，不进菜单序号。
  - 并入「修复与更新」（fix）。排除：`fix` 面向项目脚手架与运行时的「不一致 → 修复」，范围单一；技能与 MCP 的整理是长期、可浏览、含用户判断的操作，不是检查结果驱动的修复。保留的衔接：`fix` 不处理本屏的任何事。
  - 在「项目工具」里挂一个工具。排除：该屏只运行项目自己声明的工具，语义不同。

### 3.2 第 9 屏：结构与 ASCII 草图

首页（只读，进入即调用 `agenthelm inventory --output json`，一次子进程，不联网）：

```
技能与 MCP                                  项目：/path/to/proj
────────────────────────────────────────────────────────────────
  技能 41 个 · MCP 9 个 · 客户端 claude / codex / cursor / desktop(只读)
  每会话 description 常驻：claude ≈ 38 KB（证据：file）
  重复 6 组（MCP 3 · 技能 3）  未知 4 项（证据不足，不做结论）

  1  按客户端看        客户端 → 作用域 → 条目
  2  按项目看          本项目 / 已同步过的项目（只列在本机同步过的）
  3  按重复看          同名 · 同指纹 · 同名不同内容
  4  技能体检          缺 description · 体积 · 加载成本 · 过期
  5  移动或复制        在作用域、客户端之间迁移（先预览）
  6  设置生效范围      全局 / 仅此项目（先预览）

  输入序号；或输入条目名直接看详情。 ? 帮助 · 回车返回
```

「按客户端看」（分组展示，条目行列固定，同一组内样式一致）：

```
claude  ·  证据 file
  用户级  ~/.claude/skills            23 个技能
    ● code-review           1.2 KB  desc 312 B   来源 plugin:xxx@1.4   [重复 2]
    ● pdf-notes             0.9 KB  desc 104 B   来源 未知             [无 description]
  项目级  .claude/skills               2 个技能
  MCP 用户级  ~/.claude.json           4 个
    ● notes-server   stdio  指纹 a1b2…   [重复 2：与 codex 用户级同指纹]
  MCP 本机项目级  ~/.claude.json[proj]  1 个
  MCP 项目文件  .mcp.json              2 个   批准状态 未知(unknown)
desktop(只读)  ·  claude_desktop_config.json   3 个 MCP   是否加载 未知(unknown)
```

约定：
- **证据等级**以单字徽标显示在行尾或组头：`file` 不标（默认可信），`setting` 标 `⚙`，`cli` 标 `⌘`，`unknown` 标 `?` 并灰显。「未知」永远显示为「未知」，不折叠、不省略、不用「正常」替代。
- **每行末尾的方括号标签**是机械发现（`重复 N`、`无 description`、`体积大`、`过期`），无发现不显示任何标签。
- 横向放不下时缩列顺序：来源 → 体积 → desc 字节；条目名与作用域永不省略。

「按重复看」：

```
重复 MCP（3 组）
 1  notes-server  同指纹 · 名称相同
      claude 用户级   ~/.claude.json            指纹 a1b2…
      codex  用户级   ~/.codex/config.toml      指纹 a1b2…
      生效：各客户端独立，彼此不冲突 → 这是「跨客户端重复」，可保留
 2  search        同名 · 指纹不同（内容漂移）
      claude 本机项目级  指纹 c3d4…   优先级：local > project > user（官方）
      claude 用户级      指纹 e5f6…   → 本项目里生效的是 local 那份
 3  fetch         同名 · 证据不足
      cursor 项目 / cursor 用户   优先级未文档化 → 不下结论
  序号看处置选项 · 回车返回
```

重复条目详情与处置见 §4。

### 3.3 交互流：看清单 → 选条目 → 选目标 → 预览 → 确认 → 执行 → 读回

```
 ┌────────────┐   选条目      ┌──────────────┐  选动作          ┌────────────────┐
 │ 清单(只读) │──────────────▶│ 条目详情     │─────────────────▶│ 选目标         │
 │ inventory  │               │ 证据/来源/   │ 移动·复制·       │ 客户端×作用域  │
 └────────────┘               │ 重复/被引用  │ 设范围·处置重复  │ (收窄默认,放宽 │
        ▲                     └──────────────┘                  │  需显式动词)   │
        │ 读回: 重新                                            └───────┬────────┘
        │ inventory 并与                                                ▼
        │ 计划逐项比对                                          ┌────────────────┐
 ┌──────┴─────┐  yes     ┌────────────┐   展示 diff 与受影响项 │ 预览(plan)     │
 │ 执行       │◀─────────│ 确认       │◀───────────────────────│ 含 plan_digest │
 │ --apply +  │          │ 输入 yes   │                        │ 只读,零写入    │
 │ digest     │          └────────────┘                        └────────────────┘
 └────────────┘   失败 → 事务回滚 + 恢复目录路径
```

每一步的约束：
1. **清单**：来自一次 `inventory` 调用（缓存在本屏会话内，写入后重新调用）。
2. **详情**：显示证据等级、来源（`origin`，可识别时）、`content_sha256` 前 8 位、`description_bytes`、重复归类、`referenced_by` 位（§7）、可用动作；「证据 unknown 的条目」只给只读信息与手工步骤，不给写动作（§4.6）。
3. **目标**：只列「对该客户端真实存在的存储」；不存在的组合不出现（例如 Cursor Skill 的项目级显示 `project-native：已由 .agents/skills 加载，无需写入`）。
4. **预览**：展示成对操作（如 `copy-skill` + `verify` + `archive-source`）、每个涉及路径、受影响项目、将进入恢复目录的物品、哪些操作**不能承诺**（§4.6）。预览零写入。
5. **确认**：沿用 `confirm()`（输入 `yes`）；放宽类额外键入条目名。
6. **执行**：子进程 `agenthelm relocate|dedupe … --apply --expect-plan <digest>`；digest 不一致（预览之后磁盘变化）则拒绝并提示重新预览。
7. **读回**：执行后菜单再次调用 `inventory`，对计划中的每个条目比较（存在性、作用域、`content_sha256` 或指纹）；不一致显示 `读回失败` 与恢复目录，退出码按 `EXIT_ATTENTION`。

### 3.4 非交互命令形态

两层命令，菜单只调用 agent-pack 层：

**agent-pack 层（`agenthelm …`，M1 与本版 CB 组实现）**
```
agenthelm inventory  [--source client:X|project:P …] [--kind skill|mcp] [--include-description]   # M1，只读
agenthelm relocate   <ref> --to <client>:<scope> [--mode copy|link] [--project P] [--allow-widen]
                     [--output json] [--apply --expect-plan <digest>]                              # 默认预览
agenthelm dedupe     <group-id> (--keep <ref> --retire-others | --keep-all --note "…" | --ignore)
                     [--output json] [--apply --expect-plan <digest>]                              # 默认预览
```

**project-orchestrator 层（`agenthelm project extensions …`，在项目上下文里转发并补充）**
```
agenthelm project extensions list    [--kind skill|mcp] [--client X] [--scope S] [--group client|scope|dup] [--output json]
agenthelm project extensions dups    [--kind mcp|skill] [--output json]
agenthelm project extensions show    <ref> [--output json]
agenthelm project extensions check   [--output json]          # 技能体检：机械发现，只读
agenthelm project extensions move    <ref> --to <client>:<scope> [--mode copy|link] [--apply]
agenthelm project extensions dedupe  <group-id> --keep <ref> --retire-others | --keep-all | --ignore [--apply]
agenthelm project extensions scope   <ref> --effective project|user [--allow-widen] [--apply]
```

约定（沿用 `fix` 的模式，参考 `project_cli.cmd_fix` 与 `project_maintain.plan/apply`）：
- **`<ref>`** 是稳定的条目引用：`skill:<名>@<客户端>:<作用域>` 或 `mcp:<名>@<客户端>:<作用域>`，例如 `skill:pdf-notes@claude:user`、`mcp:search@claude:local`。作用域取值与存储名见 §2.1，歧义时命令拒绝并列出候选。
- **默认预览，`--apply` 才写**；`--output json` 返回 `data.plan`（步骤、`blocked`、`plan_digest`、`undecidable`、`affected_projects`）与 `data.applied`；写命令的 JSON 外壳与 `fix` 一致。应用时必须带 `--expect-plan <digest>`（`opinion_manager.py inbox adopt` 的「预览指纹即确认」模式，见 `docs/write-commands.md`）；没有 digest 的 `--apply` 拒绝，保证预览等于应用。
- 退出码沿用 `EXIT_OK` / `EXIT_ATTENTION`：存在 `blocked` 或读回不一致为 `ATTENTION`。
- `--offline`（全局开关）下不做任何联网判断（过期、最新版本一律 `unknown`）；本版所有命令默认不联网。
- `scope` 是 `move` 的受限形态（目标客户端不变，只改作用域），单独成命令是为了让「设置生效范围」在脚本里语义更清楚，并用 Profile 声明表达（§2.2）。
- 不提供 `extensions delete` 或 `remove`；删除只通过 `dedupe --retire-others` 与退役归档（§4.4）。

### 3.5 菜单步骤细化（第 9 屏内部）

- 屏内导航沿用现有语法：序号进入、回车返回、`q` 退出；`?` 显示本屏帮助。
- 条目详情动作键：`m 移动或复制` · `s 设置生效范围` · `d 处理重复` · `o 打开其所在目录`（仅在本机文件管理器可用时，沿用 `provider.act("reveal", …)`）· `p 显示可粘贴片段`（Desktop 与手工步骤）。
- 向导里 `b` 返回上一步、不执行；任何一步回车取消，已写入为零（与 `confirm` 一致：`已取消，没有写入。`）。

---

## 4. 重复 MCP 的处置语义

### 4.1 判据

以 inventory 的**条目指纹**（规范化配置的摘要，不含值）为基础：

- **R1 同名同指纹**：在不同（客户端，作用域）下完全相同。分两种：
  - 跨客户端（claude 与 codex 各一份）：各客户端加载各自的，彼此不冲突。判定为「跨客户端重复」，默认建议级 `可保留`，**不推荐退役**（理由：它们是不同进程、不同客户端的各自配置；#82 §2.3 事实 c 对 Skill 说明了跨客户端要按客户端分别判断）。
  - 同客户端不同作用域：存在屏蔽关系（§4.2）。
- **R2 同名不同指纹**：内容漂移，优先显示差异的字段名（只列字段名与类别，如 `args 不同`，不显示值）。
- **R3 不同名同指纹**：同一服务以不同名字注册（常见于重复安装）；同客户端内是真重复（会启动两个进程），跨客户端属 R1 的跨客户端情形。
- **跨项目**：项目级条目在不同项目里重复（来自 `~/.claude/agent-pack/state/*.json`，#82 §8 的 `doctor --machine`）；本屏「按项目看」展示，不做合并建议。
- **Desktop**：`claude_desktop_config.json` 与 `~/.claude.json` / `.mcp.json` 同名，仅在 Desktop 应用的本地 Code 标签会话中前者优先（#82 §2.3，原文已引，不重述）；独立 CLI 不读它。展示时必须带这条限定语。

指纹不含「名字」与「证据」之外的元数据；两份指纹相同但证据均为 `unknown`（例如读取失败）时，归为 `unknown-effective`，不进入处置向导的写入分支。

### 4.2 「哪一份生效」：只在文档明确时下结论

| 场景 | 结论 | 依据 |
| --- | --- | --- |
| Claude CLI：local / project / user 同名 | local > project > user | Claude Code MCP 文档「Scope hierarchy and precedence」（引自 #82 §2.3） |
| Claude Desktop 本地 Code 标签：desktop 配置 vs `~/.claude.json` / `.mcp.json` | desktop 配置那份生效；仅该场景成立 | Claude Code Desktop 文档（引自 #82 §2.3） |
| Codex：项目层 vs 用户层 | 项目层优先（最近的项目文件胜出） | Codex MCP 文档（引自 #82 §2.3） |
| Cursor：项目 vs 全局 | **文档未说明** → 显示 `优先级未文档化`，不下结论 | #82 §13 条 6 |
| 跨客户端 | 各客户端独立加载，无「谁覆盖谁」 | 各客户端存储互不读取（Cursor 读 Skill 的兼容目录不适用于 MCP） |

未列出的组合、证据为 `unknown` 的副本，一律输出 `生效：未知`，并附「下一步」（例如在该客户端里用 `claude mcp list` 自行确认，这是 `cli` 证据，菜单**不**代为执行，沿用 #82 §4 对 `--probe` 的约束）。

### 4.3 处置选项（穷举与排除）

对一个重复组，向导依次给出以下选项，每项写明适用条件与被排除的理由：

- **A. 保留一份，退役其余**（需用户选保留项）。仅当组内全部副本的 `loaded` / 生效已确定（证据非 `unknown`）才可选；否则选项置灰并写原因。
- **B. 都保留并标注**：不改任何配置，只在 `~/.config/agenthelm/ext-decisions.json`（机器级，0600，不含值）记录 `{group-fingerprint, 决定:keep-all, 备注, 时间}`；之后该组在清单里显示为「已确认保留」而不再计入重复数。适用于跨客户端重复与有意的覆盖。
- **C. 改作用域**：把其中一份移到另一个作用域（走 §2.2 的 `scope` / `relocate`），常见用途是把「用户级 + 项目级各一份」收敛为仅项目级。
- **D. 忽略**：与 B 的区别是不声明「有意保留」，只在本机把这一组静默（可 `--unignore`）；用于证据不足、暂时无法判断时。
- 排除的做法：
  - 「自动选最新的」：无可靠版本信号，选错会改变实际加载的服务。
  - 「自动合并字段」：Claude 官方说明跨作用域不合并字段（「fields are not merged across scopes」，#82 §2.3）；合并产生任何客户端都不会按此执行的配置。
  - 「直接删除」：见 §4.4。
  - 把 R1 跨客户端重复当作错误：见上，它们不冲突。

### 4.4 退役：归档、可恢复，并且需要新增路径

**核对结论（影响实施）**：
- `reconcile --retire-skill`、`--retire-plugin`、`--retire-only` 已存在，归档到 `recovery_base/<时间戳>-claude-<名>/{skills,plugins,…}`，失败按 `moved` 逆序回滚（`agent_pack_claude.py:389-402`、`:497-593`）。它们只处理**用户级 Skill 链接与已安装的插件**。
- **没有** `--retire-mcp`。对指向被退役插件的 MCP 条目，代码只写 `manual` 提示（`claude mcp remove --scope user <名>`，`:250-268`），原因注释：`~/.claude.json` 可能含本地凭据，不自动编辑。
- 没有「从恢复目录恢复」命令；恢复靠手工把归档目录移回原位。

**设计**：
- **Skill 退役**：直接复用 `reconcile --retire-skill`。`dedupe --retire-others` 对用户级 Skill 副本生成的 plan 只是对该命令的封装；项目级 Skill 副本的退役（`.claude/skills/<名>` 符号链接或目录）由 `dedupe` 自己归档到同一恢复目录结构的 `skills` bucket，不新开路径。
- **MCP 退役（新增，CB-3）**：`dedupe --retire-others` 对 Claude 的 MCP 副本提供两级：
  1. **已受管的条目**（agent-pack 的 `state` 里有归属）：agent-pack 自己编辑 `~/.claude.json`，把被退役条目的 JSON 片段原样写入恢复目录（权限 0600，目录 0700，不打印），并原子改写原文件；失败回滚。
  2. **未受管的条目**（用户手工装的）：不编辑文件，只输出 `claude mcp remove --scope <user|local|project> <名>`（官方命令，#82 §2.3 引用）与「先自己备份 `~/.claude.json`」的提示；预览里显式标 `不可由工具归档`。
  - 选择原因：恢复片段里可能含内联的密钥值，写入恢复目录等于复制一份密钥到另一个位置；这是权衡，列为 D-CB6（替代：永远只给手工命令，彻底不碰 `~/.claude.json`）。
- **恢复**：本版不新增「恢复」命令（避免把恢复做成第二个会写入的路径），在退役结果里输出恢复目录与「手工移回」的单行步骤；是否新增 `restore` 列为 D-CB7。
- **不自动删除**：所有退役都是归档（移动），不是删除；恢复目录由用户自行清理，本版不做自动清理。

### 4.5 Desktop 配置（I-6 写入未做前）

- 仅只读与「可粘贴的片段 + 手工步骤」：当用户要把某 MCP「放到 Desktop」，`p` 动作输出：目标文件的路径（按平台，来自 #82 §2.3：macOS `~/Library/Application Support/Claude/claude_desktop_config.json`，Linux `~/.config/Claude/claude_desktop_config.json`）、需要粘贴进 `mcpServers` 的 JSON 片段（只含 `command` / `args` 与 `${VAR}` 之外的**结构**，凭据以 `<在此填入你的 VAR_NAME>` 占位）、以及「完全退出并重启 Desktop」的提示。
- 片段里**不含**任何密钥值，env 只列变量名；含内联值的条目不生成片段（标 `blocked`）。
- Desktop 是否加载：证据 `unknown`（#82 §4），菜单只显示「文件里有这一项」。
- 官方对 `claude_desktop_config.json` 是否支持 `${VAR}` 展开与远程 `url` 条目未说明（#82 §13 条 7），片段生成器对远程 `url` 条目标 `未确认是否被 Desktop 加载`，不承诺。

### 4.6 哪些写入不能在预览里承诺

预览的每一步带 `promise` 字段（`certain` / `best-effort` / `cannot-promise`）：

- `cannot-promise`：证据为 `unknown` 的任何结论，包括：某客户端「会不会加载」「哪份生效」「待批准/受信任状态」「Desktop 是否读取」「同步盘是否保持符号链接」。预览写 `执行后无法保证效果，工具只保证文件改动，不保证客户端行为`。
- `best-effort`：移动后「客户端是否出现该条目」需要重启或重新进入项目才能确认（所有客户端的加载时机文档不一）。预览输出「下一步：在客户端里确认」，但**不执行**任何客户端命令（沿用 #82 的 `--probe` 约束，本版菜单不调用 `claude mcp list`）。
- `certain`：文件级事实（目标存在、内容摘要一致、源已归档）。
- 证据为 `unknown` 的副本：**只读**，不出现 `A 保留一份退役其余` 与「可删除」字样（#82 §9 条 7）。

---

## 5. 技能管理语义

### 5.1 清单

条目字段（来自 inventory）：`name / client / scope / store / description_bytes / content_sha256 / origin`，加 §10 的请求字段。清单可按 客户端、作用域、来源、体积、description 字节排序。内置能力（`overridden-by-builtin`，#82 §4）在清单里作为只读注释行出现，不是可操作条目。

### 5.2 重复

- **同名、内容相同**（`content_sha256` 相同）：纯冗余。同客户端内按官方优先级有屏蔽关系（Claude：企业 > 个人 > 项目；2026-11-18 读取 [Claude Code Skills 文档](https://code.claude.com/docs/en/skills)「Enterprise over personal, and personal over project」）；Codex 官方说明「both can appear in skill selectors」（#82 §2.3），故 Codex 下同名两份都会出现，不存在屏蔽；Cursor 同名优先级未文档化。
- **同名、内容不同**：`漂移`。显示差异的类别（`SKILL.md` 不同 / 资源文件不同 / 仅 description 不同），**不显示内容**；是否同一来源的不同版本由 `origin` 判断，无法识别则标 `来源未知`，不推荐「保留较新者」。
- **description 相似（机械重叠提示）**：对 `description` 文本分词（拉丁词小写 + CJK 二元组），取 Jaccard 相似度 ≥ 0.6 的条目对，标 `description 相似（机械判断，不代表功能重复）`。阈值 0.6 是本文的初始值，没有外部依据，列为 D-CB8；输出用于排序，从不触发写动作。需要 `--include-description`（§10 请求），文本只在本机内存里用于比较，不写入日志与 JSON 的 `plan`。
- 重复处置沿用 §4.3 的四选一；Skill 的退役走 `reconcile --retire-skill` 或 §4.4 的项目级归档。

### 5.3 每会话 description 加载成本

事实（官方）：
- Claude Code：「Every skill in the skill listing adds to your context on every turn」；`description` 与 `when_to_use` 合并后在 skill 列表中**截断于 1,536 字符**；`skillOverrides` 可把条目设为 `on` / `name-only` / `user-invocable-only` / `off`（2026-11-18，[Claude Code Skills 文档](https://code.claude.com/docs/en/skills)；该页文本约 11.9 万字符，仅读到前 10 万字符，列表总预算与 `name` 长度上限我未读到，**未确认**）。
- Agent Skills 规范：`description` ≤1024 字符；元数据启动时常驻约 100 token（2026-11-18，[规范](https://agentskills.io/specification)）。

设计：
- **成本指标**：`cost_bytes(skill) = min(description_bytes + when_to_use_bytes, 1536 字符对应的字节上限)`。需要 inventory 提供 `description_chars`、`when_to_use_chars`（§10）；若只有 `description_bytes`，则成本按字节显示并标 `仅 description`。
- **每客户端汇总**：`sum(cost_bytes)`，只对该客户端**实际会加载**的条目求和（先按 §5.2 的屏蔽关系去重；Claude 的 `skillOverrides` 为 `off` / `user-invocable-only` 的不计入，读 `setting` 证据；其余证据 `unknown` 的并入「未计入」行并单独显示条数）。
- **展示**：字节为主，附「粗估 token」，公式 `≈ 字节 ÷ 4`（拉丁文）至 `÷ 2`（中文 UTF-8 每字 3 字节，约 1–2 token）的区间，标 `估算，仅用于排序与对比`，没有一手依据，列入未核实。
- **不计入**：MCP 工具定义的上下文成本（需要连接服务器，属 `cli` 证据，默认不取）；MCP 行显示 `工具定义成本：未知`。

### 5.4 过期

只对 `origin` 能识别「来源 + 已安装版本」的条目给出，且仅用本机已有信息（本机已缓存的最新版本）：
- 条目带 `origin.version` 与 `origin.latest_known`（inventory 提供，离线时 `unknown`）→ 显示 `有新版本 X`；
- 其余一律 `过期：未知`，**不用「很久没改」推断过期**（无来源信息时 mtime 不是可靠信号）。
- 升级本身不在本版范围：菜单只提示并给出该来源的官方升级命令（若 `origin` 提供），不代执行。

### 5.5 质量：能判断与不能判断

**机械可判断（输出为事实与阈值，不是分数）**
- 缺 `description`（Agent Skills 规范必填）→ `缺 description`；
- `description` 超 1024 字符（规范）或合并超 1536 字符（Claude 截断）→ `描述会被截断`；
- `SKILL.md` 行数 >500 或体积异常大（规范建议 <500 行 / <5000 token）→ `体积大`（阈值来自规范的建议，不是强制）；
- 同名漂移（§5.2）、目录名与 `name` 不一致（规范要求匹配）、`name` 含官方禁用字符（Claude 官方说明含 `<` `>`、零宽或控制字符会被拒绝）；
- 每会话加载成本（§5.3）。

**不能判断（交回用户）**：好坏、是否适合你的工作、与其他技能是否真的功能重复、description 写得是否能让模型正确触发、内容是否正确或安全。工具**不输出评分、不排名、不输出「建议删除」**。这些判断属于用户的领域经验。

### 5.6 迁移的写入语义

涉及的迁移方向：全局→项目、项目→全局（放宽，§2.3）、客户端之间。统一流程 `plan → apply`：

1. **规划**：解析源条目（必须证据为 `file`，且为一个目录、含 `SKILL.md`），计算目标路径，检查目标是否存在。
2. **绝不覆盖用户已有的同名目录**：目标存在时 plan 标 `blocked: 目标已存在同名条目（内容相同 / 不同）`，并给出 §5.2 的处置入口；工具**不**自动改名、不合并、不覆盖。
3. **形态：复制还是符号链接**：
   - **跨作用域迁移（home ↔ 项目）一律复制**。指向 home 的符号链接是机器相关的绝对路径，写入随项目同步的目录后在另一台机器上失效（#82 §3 选项 F 的同一理由）。
   - **同一项目内的投递**（`.agents/skills/<名>` → `.claude/skills/<名>`）沿用 #82 D3 倾向：相对符号链接 `../../.agents/skills/<名>`。Claude Code 官方文档说明个人/项目位置的条目可以是指向其他目录的符号链接，并且「loads the skill once even if several locations point at the same target」（2026-11-18，[Skills 文档](https://code.claude.com/docs/en/skills)）；插件内的符号链接处理不同，本文不涉及。**同步盘（iCloud、Dropbox、OneDrive）对相对符号链接的行为仍未核实**（#82 §13 条 10），`--mode link` 只在非同步盘位置默认允许，检测手段「路径含已知同步盘目录名」是启发式，列为 D-CB3 的一部分。
   - 同一客户端的用户级内部（`~/.claude/skills` ↔ `~/.agents/skills`）：两个用户级目录在同一台机器上、不同步，`--mode link` 可用，默认仍是复制（单一真相不是目标，隔离更安全）；链接模式的代价是「两处一改俱改」，预览里写明。
4. **备份与回滚**：写入前建恢复目录（沿用 `_recovery_dir`），源目录不移动直到目标校验通过；任何一步失败，逆序撤销（删除刚创建的目标副本；这是**自己创建的目标**，可以安全移除，不涉及用户已有物）并保留恢复目录。
5. **读回校验**：目标目录逐文件比较（相对路径与内容摘要），`SKILL.md` 的 `content_sha256` 与源一致才视为复制成功；符号链接模式校验链接目标与解析后路径。
6. **退役源**：校验通过后才归档源（用户级走 `reconcile --retire-skill`，其余同 §4.4），归档不是删除。「复制（保留源）」是独立选项，可选不退役源。
7. **清理项目**：若把用户级 Skill 复制进**项目仓库**，该内容将进入版本库与同步范围；预览在这一步显示 `将进入项目目录：<相对路径>（可能被提交到版本库）`，并要求用户确认其许可证与是否含私有内容（工具不能判断，交回用户）。公开仓库的隐私约束见 §6。
8. **Codex、Cursor 目标**：`--to codex:user` 的实际目标是 `~/.agents/skills`（#82 §2.3），`--to cursor:project` 是 `.agents/skills`，已由项目原生加载则 plan 记为 `project-native`（零写入）。

---

## 6. 安全与隐私

- **不打印密钥值、env、headers**。所有输出（文本、JSON、`plan`、恢复元数据、日志、错误信息）对 MCP 条目只输出：名字、客户端、作用域、存储类别、传输类型、指纹、环境变量**名**。测试用已知假令牌夹具，断言它不出现在任何输出与恢复目录元数据里（#82 §9 条 3）。恢复目录里归档的 `~/.claude.json` 片段是例外（§4.4），属于「写入」不是「输出」，权限 0600，路径以外不显示内容。
- **不读令牌存储**（Claude OAuth 令牌位置文档不公开，#82 §13 条 3）。
- **扫描边界**：
  - machine 级只读**已知存储**：#82 §3 与 §5 列出的用户级路径（`~/.claude/skills`、`~/.agents/skills`、`~/.codex/skills`、`~/.cursor/skills`、`~/.claude.json`、`~/.codex/config.toml`、`~/.cursor/mcp.json`、`claude_desktop_config.json`）。不遍历 home，不扫描任意目录寻找项目。
  - 项目级：当前项目，加用户以 `--project P` **显式指定**的项目，加 `~/.claude/agent-pack/state/*.json` 归属记录里出现过的项目（#82 §8 的局限）。没同步过的项目不可见，输出里写明这条局限。
  - 其他项目的目录是否存在、读取其项目文件：仅限上述已知项目，且只读已知存储位置（如 `<项目>/.mcp.json`、`<项目>/.claude/skills`）。
- **证据不足不给「可删除」结论**；`unknown` 的副本无写动作（§4.6）。
- **不自动删除任何东西**：退役 = 归档；本版不做恢复目录自动清理。
- **公开仓库隐私**：文档、测试夹具、示例输出里不得出现个人路径、内部主机名、令牌形字符串；沿用 `tests/test_public_privacy.py`。第 9 屏的示例路径一律用占位符。
- **子进程**：菜单只调用 `agenthelm`（不经 shell，参数列表形式）；命令与参数中不含用户可控的未转义字符串；输出只解析 JSON，不回显 stderr 全文（沿用 `_run_refresh` 的 `stderr[:200]` 截断）。
- **平台**：只支持 macOS 与 Linux（Windows 不处理，与 #82 D14 倾向一致）。

---

## 7. 与 opinion 的衔接（预留接口位置，不展开）

- 条目详情与清单行预留一个**可选**字段 `referenced_by`：`[{"rule_id": "…", "source": "…"}]`，没有该字段或为空时整列不显示。
- 取数方式（待 CA 组的设计确定后对齐）：菜单通过子进程调用 opinion 侧的只读命令（暂记 `agenthelm opinion refs --skill <名> --output json`），project-orchestrator 与 opinion-manager 同样互不 import。命令不存在或失败时降级为不显示，不报错。
- 本文不规定 opinion 规则的格式、路由语义与引用方向；「删除或退役一个被 opinion 规则引用的 Skill」的警告属于 CA 组的设计，本文只保证：详情页在有 `referenced_by` 时显示 `被 N 条规则引用`，并在退役预览里把它列为 `affected`（不阻塞，由 CA 的语义决定是否升级为阻塞）。

---

## 8. 实施拆分与顺序

等级沿用 #82 §11（`release/VERSIONING.md`）：patch 兼容修复或小改进，minor 值得发布说明的新能力。**本版无 major**。以下与 #82 的 I-1…I-12 互相引用，I-x 指 #82 的拆分项。

**CB-1 inventory 契约补字段**（minor 或并入 M1 的 patch；agent-pack）
- 范围：§10 的请求字段；与 M1 同一 PR 或 M1 的后续小改。
- 影响既有项目：输出追加字段，既有键不变。用户动作：无。默认：on。
- 依赖：M1。验收：新字段在夹具上存在；不输出任何值；`schema_version` 不变或小版本递增并注明。测试：临时 HOME 夹具，含含假令牌的 MCP 条目。

**CB-2 技能移动与复制 `agenthelm relocate`（Skill）**（minor；agent-pack）
- 范围：§5.6。Skill 的 copy / link、跨作用域、跨客户端；plan_digest；读回；归档源（复用 `reconcile --retire-skill`）。**新模块**（建议 `agent_pack_relocate.py`），在 `agenthelm.py` 只加子命令注册，在 `locale/en.json` 追加文案，以降低与 I-1/I-4/I-7 在 `agent_pack_claude.py` 与 `agent_pack_config.py` 上的冲突。
- 影响既有项目：新命令，不影响既有 `sync`。用户动作：无。默认：`off`（须显式调用）。
- 依赖：M1 inventory、CB-1。验收：目标已存在则 `blocked`；预览 = 应用（文件集合逐项相等）；失败回滚；复制后 `content_sha256` 一致。测试：目标冲突、中途失败、符号链接模式、`project-native`、回滚。

**CB-3 MCP 移动、作用域设置与退役（Claude 先行）**（minor；agent-pack）
- 范围：§2.2、§4.4。`relocate` 的 MCP 分支（用户级↔local↔项目文件）、`scope` 声明写入（I-4 的 `mcp_scope`）、**新增 MCP 退役**（受管条目归档编辑、未受管条目只给手工命令）、`transfer` 组合计划、含内联密钥的 `blocked`。
- 影响既有项目：新命令；对 `~/.claude.json` 的编辑仅限受管条目，且先建 0600 恢复目录。用户动作：无。默认：`off`。
- 依赖：I-1、I-4（作用域声明与存储映射）、CB-2（共用 plan 框架）。与 I-4 同改 `agent_pack_claude.py` 的 MCP 写入路径，**必须在 I-4 合并后串行**。
- 验收：声明为 `project` 的条目不回退写用户级；预览 = 应用；假令牌不入输出、不入恢复元数据以外的任何位置；退役后条目从原位置消失、恢复目录有片段（0600）。测试：#82 §10「作用域」「密钥安全」。

**CB-4 重复处置 `agenthelm dedupe` 与机器级决定文件**（minor；agent-pack）
- 范围：§4.3、§4.5。`--keep/--retire-others`（调用 CB-2/CB-3 的退役）、`--keep-all`、`--ignore`；`~/.config/agenthelm/ext-decisions.json`；Desktop 片段生成（`p` 动作的后端，只输出片段）。
- 影响既有项目：新命令；决定文件为新增。用户动作：无。默认：`off`。
- 依赖：CB-2、CB-3、I-5（Desktop 只读）。验收：`unknown` 证据的组不可 `--retire-others`；`ignore` 可撤销；决定文件无值。测试：判据 R1–R3、跨客户端不报冲突。

**CB-5 技能体检与成本核算（共享库函数 + inventory 衍生）**（patch/minor；agent-pack 或 orchestrator）
- 范围：§5.2–§5.5 的机械判断与 `description` 相似度。**建议放在 orchestrator 的只读渲染层**（只消费 inventory JSON，不读文件系统），避免与 agent-pack 写入路径重叠；若 inventory 将来内置这些字段则退化为展示。
- 影响：只读，advisory。依赖：CB-1。验收：不输出评分；阈值与来源可追溯；离线不联网。

**CB-6 菜单只读屏 + `extensions list/dups/show/check`**（minor；project-orchestrator）
- 范围：§3.1–§3.2。第 9 屏、`MENU` 与 `SCREENS`、`provider` 的 `extensions_*` 方法（真实项目与演示项目都要实现，演示项目返回固定夹具）、`cmd_extensions`、`parser()`、`capabilities` 一项。文件：`project_menu.py`、`project_cli.py`、`project_real.py`、`project_demo.py`、`locale/en.json`、`i18n_orchestrator`。
- 影响既有项目：菜单新增第 9 项，序号 1–8 不变。用户动作：无。默认：`on`（只读，无副作用）；`inventory` 不可用时该项显示 `不可用：…`。
- 依赖：M1 inventory（可用夹具 JSON 先行开发，在 inventory 落地前用桩）。验收：全程零写入；`unknown` 显示为「未知」；JSON 稳定；演示项目可走通。测试：`test_project_cli.py` 的新增用例；`tests/test_i18n_commands.py` 的 READ_ONLY 登记。

**CB-7 菜单写入动作与 `extensions move/dedupe/scope`**（minor；project-orchestrator）
- 范围：§3.3–§3.5。向导屏、`confirm` 流、`--expect-plan`、读回；全部调用 CB-2/3/4 的命令。
- 影响：新写命令，默认预览。用户动作：无。默认：`off`（用户走到该动作才发生）。
- 依赖：CB-6（同改 `project_menu.py` / `project_cli.py`，串行）、CB-2、CB-3、CB-4。验收：预览=应用；digest 不一致拒绝；`blocked` 不可执行；读回失败退出码 `ATTENTION`。

**CB-8 健康屏跳转与 opinion 引用位**（patch；project-orchestrator）
- 范围：`health_screen` 的 `ext:` 动词；`referenced_by` 的渲染位（字段缺失时不显示）。
- 依赖：CB-6；opinion 侧命令名待 CA 组确认。

**CB-9 文档与登记表**（patch；docs）
- 范围：`docs/write-commands.md` 盘点表加 `agenthelm relocate/dedupe` 与 `agenthelm project extensions move/dedupe/scope`；`tests/test_i18n_commands.py` 登记；用户文档（README 或项目指南）一段。若修改 `README.md` / `INSTALL.md` / `plugins/*/references` 须遵守 `tests/i18n-policy.json`。
- 依赖：随各项同步完成，不单列最终波次。

### 8.1 文件重叠与波次

重叠热点：
- agent-pack：`agent_pack_claude.py`、`agent_pack_config.py`（#82 的 I-1/I-3/I-4/I-7 同改）、`agenthelm.py`（子命令注册）、`locale/en.json`。
- project-orchestrator：`project_menu.py`、`project_cli.py`、`project_real.py`、`project_demo.py`、`locale/en.json`。
- 两个插件之间无文件重叠（只通过子进程与 JSON 耦合），因此波次一可与 agent-pack 侧的 M1 / I-1 / I-4 并行推进。

```
波次 1（与 M1/I-1 并行，不碰 agent-pack 写入路径）
  CB-6 只读屏 + extensions list/dups/show/check   (orchestrator)
  CB-1 inventory 补字段                           (M1 内或紧随)
  CB-5 体检与成本（orchestrator 只读层，随 CB-6 同 PR 或相邻 PR）
        │
波次 2（新模块，减少与 I-4/I-7 的冲突）
  CB-2 relocate（Skill）                          (agent-pack 新模块)
  CB-4 dedupe + 决定文件 + Desktop 片段           (agent-pack 新模块；依赖 CB-2 的 plan 框架，可与 CB-2 同 PR)
        │
波次 3（等 I-4 合并后）
  CB-3 MCP 移动/作用域/退役                       (agent-pack，改 agent_pack_claude.py)
  CB-7 菜单写入动作                               (orchestrator，依赖 CB-6 与 CB-2/3/4)
  CB-8 健康屏跳转 + opinion 引用位                 (orchestrator，小，随 CB-7)
```

与 #82 的排序关系：I-1 → CB-6、CB-1；I-4 → CB-3；I-7 与 CB 组无依赖；I-5 → CB-4 的 Desktop 片段；I-6（Desktop 写入）本组不做。

### 8.2 CLI 与菜单的 i18n 文案量级与「写命令登记表」

- **i18n（量级估算，非精确）**：orchestrator 侧第 9 屏与 `extensions` 命令约 **70–100 条**新增 msgid（标题、列头、徽标、动作提示、向导提示、错误与 `blocked` 原因、帮助与 `parser()` 帮助文本），相对现有 1467 条约 5–7%；agent-pack 侧 `relocate` / `dedupe` 的帮助、计划步骤、错误约 **40–60 条**，相对 640 条约 6–9%。每条都要同时有英文，`tests/test_i18n*.py` 的相关检查会要求（具体检查项以实施时测试为准）。该估算按 `project_menu.py` 单屏约 10–20 条的体量推出，没有逐字统计。
- **`docs/write-commands.md` 盘点表**需新增：
  - `agenthelm relocate` ——默认预览，`--apply`（须 `--expect-plan`）；
  - `agenthelm dedupe` ——默认预览，`--apply`；
  - `agenthelm project extensions move/dedupe/scope` ——默认预览；菜单显示计划并确认；
  - `agenthelm project extensions list/dups/show/check` 与 `agenthelm inventory` ——只读，不入盘点表（只读命令不写）。
- **`tests/test_i18n_commands.py`** 需登记：
  - `READ_ONLY`：`agenthelm inventory`（M1 登记，若未登记则 CB-1 补）、`agenthelm project extensions list|dups|show|check`（`CASES` 里补 `[*P, "--no-input", …]` 用例）；
  - `PREVIEW`：`agenthelm relocate`、`agenthelm dedupe`、`agenthelm project extensions move|dedupe|scope`（预览用例，运行前后磁盘不变）。
- `--expect-plan` 与 `--apply` 的组合已有先例（`opinion_manager.py inbox adopt` 的 `--apply --confirm <预览指纹>`）；测试对账要求 PREVIEW 命令的 `--help` 含 `--apply`，满足。

---

## 9. 决策项（需维护者拍板）

「倾向」是我的建议，「替代视角」是另一种合理立场。

### 9.1 我擅长的（读文档与代码、结构化比较、机械规则）——给出倾向，维护者复核

1. **D-CB1 入口形态**。倾向：新增第 9 屏，加 `capabilities` 一项与 `health` 跳转；序号 1–8 不动。替代：把它塞进「修复与更新」（体验统一，但语义混淆）。
2. **D-CB2 命令分层**。倾向：写入在 agent-pack（`relocate` / `dedupe`），orchestrator 只做展示与调用。替代：把写入也放进 orchestrator（少一层，但违反「两插件互不 import」下的职责边界，且 agent-pack 的恢复目录与归属状态不在 orchestrator 的上下文里）。
3. **D-CB3 技能复制还是符号链接**。倾向：跨作用域一律复制；项目内投递沿用 #82 D3 的相对符号链接；同步盘位置自动降级为复制。替代：全部复制（最简单，双份真相）。同步盘对相对符号链接的行为**未核实**。
4. **D-CB4 重复判据中的「跨客户端同指纹」**。倾向：视为可保留，不推荐退役。替代：视为冗余，推荐只保留一份（会让另一个客户端失去该 MCP）。
5. **D-CB6 MCP 退役是否由工具编辑 `~/.claude.json`**。倾向：仅对已受管条目编辑并把片段存入 0600 恢复目录；未受管条目只给官方 `claude mcp remove` 命令。替代：永远只给手工命令（零密钥搬运，但「可恢复」不成立）；或对全部条目都编辑（覆盖最广，风险最大，现有代码刻意没有这样做）。
6. **D-CB8 description 相似度阈值**。倾向：Jaccard ≥ 0.6，仅提示。没有外部依据；替代：不做相似度，仅做同名与同内容判断。

### 9.2 需要维护者判断的（产品取向、风险偏好、价值判断）

7. **D-CB5 跨项目写入**。倾向：菜单只改当前项目声明；多项目生效用命令行 `--project`。替代：菜单内多选项目批量写（体验好，但一次事务跨多个项目目录，回滚与权限边界复杂）。
8. **D-CB7 是否新增 `restore`（从恢复目录恢复）命令**。倾向：本版不新增，输出手工移回步骤。替代：新增只读预览 + `--apply` 的 `restore`（可恢复性闭环，但多一个写入路径需要完整的冲突处理）。
9. **D-CB9 「放宽」是否进菜单**。倾向：菜单里提供但需键入条目名确认并 `--allow-widen`；替代：菜单完全不提供放宽，只有 CLI。
10. **D-CB10 机器级决定文件位置与「忽略」的语义**。倾向：`~/.config/agenthelm/ext-decisions.json`（机器级）；替代：写进项目的决定文件（随项目同步，别的机器也看得到，但重复是机器事实，换机器未必成立）。
11. **D-CB11 用户级 Skill 迁入项目仓库时的默认目的地**。倾向：`.agents/skills`（Codex、Cursor 原生加载；Claude 经相对链接投递）。替代：`.claude/skills`（仅 Claude，不污染跨客户端目录）。选择取决于团队是否多客户端并用，属产品判断。
12. **D-CB12 清单中展示「粗估 token」**。倾向：展示，标「估算」；替代：只显示字节，避免没有一手依据的数字被当成事实。
13. **D-CB13 本版是否把 CB-3（MCP 写入）纳入**。倾向：纳入但依赖 I-4 先合并；替代：本版只做只读屏 + Skill 迁移 + MCP 的手工命令（风险最低，MCP 写入延后一版）。

---

## 10. 对 inventory 契约的请求

请 M1 组评估补充（均为新增键，不改既有键；不得包含任何密钥值）：

1. **`skills[].description_chars`、`when_to_use_chars`**（字符数，另给字节），用于对齐 Claude 的 1,536 字符截断规则（§5.3）。
2. **`skills[].skill_md_lines`、`dir_bytes`、`file_count`**：体积判断（规范建议 <500 行）。
3. **`skills[].is_symlink`、`link_target_kind`**（`relative` / `absolute` / `outside-home`，不输出完整路径时可只给类别）：判断迁移与同步盘风险。
4. **`skills[].name_matches_dir`、`frontmatter_ok`**：规范要求 `name` 与目录名一致、必填字段齐全；不输出内容。
5. **`--include-description`（可选开关）**：输出 `description` 文本（截断到 1,536 字符），仅用于相似度提示，默认不输出。
6. **`skills[].origin`**：明确结构 `{type: plugin|marketplace|git|manual|unknown, id, version, latest_known, evidence}`；`latest_known` 离线时为 `unknown`，**不联网**除非显式 `--refresh`。
7. **`entries[].loaded` 与 `shadowed_by`**（#82 I-1 已规划）：必须随证据等级给出，菜单据此决定哪些重复能进入写动作。
8. **MCP 条目**：`env_var_names[]`（仅变量名）、`has_inline_secret: bool`（只给布尔，**不给值与键名**）、`fingerprint_fields[]`（参与指纹的字段名，用于「哪些字段不同」）、`url_host`（仅远程条目，主机名，不含 query）。
9. **`projects[]`**（已知项目列表）：来自 `~/.claude/agent-pack/state/*.json` 的 `{name, path, last_synced}`；当前代码我只确认状态里有 `skills` 与 `mcps`（`agent_pack_claude.py:546-549`），**是否记录项目路径未确认**，如没有请求补充。
10. **`desktop`**：`claude_desktop_config.json` 路径与 `exists`、`parse_ok`；`entries[]` 带 `store: "desktop-config"`。
11. **每个条目的稳定 `ref`**（§3.4 的 `<kind>:<名>@<客户端>:<作用域>`）与 `plan` 里引用同一个字符串；若 M1 已有其他稳定 id 请告知，本文适配。
12. **`schema_version`** 的兼容约定：新增键不递增主版本；菜单对未知 `schema_version` 主版本降级为「不可用」并显示原因，而不是猜测。

---

## 11. 未核实清单

标「未确认」表示我未能从一手来源或仓库内容证实，设计里已按保守方式处理。#82 §13 的全部条目继续有效，下面只列本文新增或加重的。

1. **`reconcile` 的恢复**：仓库里没有从恢复目录恢复的命令（只看到失败时的事务回滚）；是否在其他插件里存在，**未确认**。
2. **MCP 退役的可恢复性**依赖「编辑 `~/.claude.json` 并存片段」，该文件的完整结构与 Claude Code 对它的并发写入行为文档未说明，**未确认**；设计用「原子改写 + 恢复片段」，未实测。
3. **Claude Code Skills 文档**：我只读到页面前 10 万字符（共约 11.9 万），skill 列表的**总预算**与 `name` 的长度上限**未读到、未确认**；1,536 字符截断、符号链接支持、`skillOverrides` 取值、优先级均来自已读部分，读取日期 2026-11-18。
4. **「粗估 token」**的字节换算比例没有一手来源，仅作排序，**未确认**。
5. **同步盘**（iCloud、Dropbox、OneDrive）对相对符号链接的行为未实测（沿用 #82 §13 条 10）；「路径含同步盘目录名」的识别是启发式，**未确认**其完整度。
6. **`description` 相似度阈值 0.6**无外部依据；CJK 二元组分词对短描述的稳定性未评估。
7. **Cursor Skill 同名优先级、Cursor 与 Codex 的 MCP 项目/用户同名优先级**文档未说明（#82 §13 条 6），本文照搬为「未文档化」。
8. **opinion 侧命令名与 `referenced_by` 的结构**取决于 CA 组设计，**未确认**；本文只预留位置。
9. **`~/.claude/agent-pack/state/*.json` 是否记录项目路径**未确认（§10 条 9）。
10. **#82 的引用**（§2.3 的各客户端官方文档）本文**未重新读取**，沿用 #82 记录的 2026-11-11 读取结果；Agent Skills 规范与 Claude Code Skills 文档为本文 2026-11-18 新读取。
11. **I-4（作用域声明）尚未实现**：本文的 `scope` 动词依赖其 `mcp_scope` / `skill_scope` 键，I-4 实现时若键名或存储映射变化，本文需同步调整。
12. **inventory（M1）尚在实现**：本文按统筹提供的字段口径设计，字段名与枚举以 M1 实际输出为准。

---

## 12. 术语对照表

- extensions（本文泛指 Skill 与 MCP）：扩展能力（技能与 MCP）
- Skill：技能（Agent Skills 规范的目录单元，含 `SKILL.md`）
- MCP（Model Context Protocol）：模型上下文协议
- inventory：盘点（`agenthelm inventory`，只读清单）
- scope：作用域（用户级 / 项目级 / 本机项目级）
- store：存储位置（作用域与客户端共同决定的具体文件）
- evidence：证据等级（`file` / `setting` / `cli` / `unknown`）
- fingerprint：条目指纹（规范化配置的摘要，不含值）
- shadowed / 屏蔽：同名条目中被优先级更高者覆盖
- drift / 漂移：同名条目内容不一致
- plan / plan_digest：计划 / 计划摘要（预览与应用的一致性凭证）
- preview / apply：预览 / 应用
- recovery directory：恢复目录（归档被替换或退役物品，可手工移回）
- retire：退役（归档，不是删除）
- loose entry：未受管条目（用户手工安装，不在任何项目声明中）
- project-native：项目原生加载（无需写入）
- narrowing / widening：收窄 / 放宽（作用域）
- OAuth（Open Authorization）：开放授权
- Jaccard similarity：杰卡德相似度（集合交并比）
- progressive disclosure：渐进式披露（先加载元数据，按需加载正文）
- Scorecard：OpenSSF 计分卡（依赖安全检查）
- Desktop：Claude 桌面应用（`claude_desktop_config.json` 的作用对象）
