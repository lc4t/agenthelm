# AGENT.template.md v6.6

> 适用范围：可移植的项目初始化与迁移。运行期项目生命周期由同一分发包内的 `project-orchestrator` 负责，Agent 扩展能力由 `agent-pack` 负责，Opinion 由 `opinion-manager` 负责。

## 1. 设计契约

生成的项目严格分离五类职责：

1. **项目契约**——`AGENTS.md` 与机器可读的 Project Profile 描述仓库。
2. **生命周期编排**——唯一的 `project-orchestrator` Skill 管理工作容器、能力路由、文件落位、验证、进度和 Git。
3. **扩展能力管理**——`agent-pack` 盘点、校验、安装、同步、迁移与对账 Plugin、Skill 和 MCP。
4. **Opinion 管理**——`opinion-manager` 生成、组合、读取和审查用户确认的 Agent 行为规则。
5. **交付能力**——领域 Skill 与确定性 Tool 创建或检查交付物。

模板必须保持通用。禁止发布个人偏好、组织数据、凭据、私有文件系统路径或复制的私有 Skill。

## 2. 标准目录结构

```text
<project>/
├── README.md        # 不存在时新建，含「怎样使用」；已有的不改动
├── menu             # 人用入口（启动器），文件名可由 --entry-name 改
├── AGENTS.md
├── AGENT.RULES.md
├── OPINION.md
├── CLAUDE.md
├── AGENT.md
├── .agents/
│   ├── plugin.json
│   ├── mcp.json
│   ├── skills/
│   └── moe.sakanano.agent-pack/
│       └── project.json
├── .agent-doc/
│   ├── plan.md
│   ├── progress.md
│   └── chat-summary.md
├── cases/
│   └── .gitkeep
├── memory/
│   └── .gitkeep
└── docs/
    ├── refs/README.md
    └── drafts/.gitkeep
```

根目录启动器（缺省名 `menu`）让人不经过 AI Agent 也能打开项目菜单：找资料、看进展、项目指南、检查与维护。它按顺序查找 `AGENTHELM_BIN`、用户级运行时（`agenthelm runtime install` 安装）、PATH 上的 `agenthelm` 与已安装客户端里的 agent-pack，从不通过 npx 临时下载代码；都找不到时说明原因与下一步并以退出码 3 结束。启动器内容与文件名无关，由内容包跟踪更新。

Agent Plugins 1.0 可移植文件直接位于 `.agents/`：`plugin.json`、`skills/` 和 `mcp.json`。反向域名目录保存客户端扩展 Project Profile，避免与未来标准组件碰撞；`agent-pack` 从这里读取能力契约。

## 3. Project Profile

生成 `.agents/moe.sakanano.agent-pack/project.json`：

```json
{
  "$schema": "https://skill.sakanano.moe/skills/agents-init/project.schema.json",
  "schema_version": "1.0",
  "initializer_version": "6.6.0",
  "name": "PROJECT_NAME",
  "profile": {
    "project_type": ["PROJECT_TYPE"],
    "vcs": "VCS",
    "stack": ["STACK"],
    "runtime": "RUNTIME",
    "agent_cli": ["AGENT_CLI"]
  },
  "runtime": {
    "skill": "project-orchestrator",
    "plugin": "project-orchestrator",
    "capability_manager": "agent-pack",
    "distribution": "marketplace",
    "required": true,
    "commit_policy": "explicit",
    "push_policy": "explicit"
  },
  "opinion": {
    "provider": "opinion-manager",
    "project_overlay": "OPINION.md",
    "strict_mode": "smart",
    "loading_mode": "tiered"
  },
  "memory": {
    "provider": "project-orchestrator",
    "root": "memory",
    "index_command": null,
    "check_command": null,
    "policy": null
  },
  "capabilities": {
    "plugin_roots": [".agents"],
    "plugin_dirs": [],
    "skill_roots": [".agents/skills"],
    "mcp_sources": [".agents/mcp.json"],
    "native_mcp_sources": [],
    "credential_env_file": null,
    "mcp_client_policy": {},
    "destination_skill_root": ".agents/skills",
    "destination_mcp": ".agents/mcp.json"
  },
  "work": {
    "mode": "case-workspace",
    "task": null,
    "case": {"root": "cases", "artifact_mode": "case-local"}
  },
  "privacy": {
    "forbidden_default_reads": [],
    "generated_outputs": []
  },
  "entry": {"api": 1, "launcher": "menu"}
}
```

`$schema` 是公开的 Project Profile 契约。Tool 必须分别验证 `schema_version` 与 `initializer_version`。多值字段使用 JSON 数组；缺失的集成使用 `null` 或空数组，禁止杜撰命令。`runtime` 可追加项目自己的 `runtime_entry`、`session_bootstrap` 等字段，由 `project-orchestrator` 读取。

`work.mode` 新项目默认 `case-workspace`，Case 是唯一持久工作目录，子任务在 Case 内管理。既有项目字段缺失或为 `legacy` 时继续使用已有模式，升级 Plugin 不会自动迁移业务文件。`work.case.artifact_mode=case-local` 声明专属材料放入 Case；公共模板、长期项目源码和正式规则继续保留各自权威位置。Case 子目录按需创建，不为简单工作堆空目录。

`opinion.loading_mode` 为 `tiered` 时，按 Opinion Profile 中用户确认的加载声明读取核心规则与规则束；Profile 没有声明时仍然完整读取 `OPINION.md`。设为 `full` 可以让所有 Session 立即回到完整读取。

`entry` 声明人用入口：`api` 是项目入口的接口版本（运行时不支持时启动器提示更新，不改项目文件），`launcher` 是根目录启动器的文件名。可选字段：`about`（一句话简介，缺省取 README 开头）、`guide`（项目首页的相对路径）、`where` 与 `start`（指南里的“资料在哪”“怎样开始”）、`tools`（项目工具声明文件，缺省 `.agents/moe.sakanano.agent-pack/tools.json`）、`items`（项目自己的事项工具，约定见 project-orchestrator 的 case-workspaces.md「项目入口适配」）。没有 `entry` 段的项目仍可用 `agenthelm project` 打开菜单。

`memory` 声明项目记忆的提供方。`provider` 为 `project-orchestrator` 时使用随包的条目式工具，条目保存在 `root` 目录。项目已有自己的记忆实现时把 `provider` 设为 `project`，在 `index_command` 与 `check_command` 填写生成索引和执行检查的命令，`policy` 指向项目自己的参数文件。体积预算、保留窗口、条数和复核间隔等参数由各项目设定，不随模板分发。

## 4. AGENTS.md 模板

<!-- agents-init:template AGENTS.md -->
```markdown
# PROJECT_NAME — Agent 执行入口

## Project Profile

- 类型：PROJECT_TYPE
- VCS: VCS
- 技术栈：STACK
- Runtime：RUNTIME
- Agent 客户端：AGENT_CLI

机器可读权威源：`.agents/moe.sakanano.agent-pack/project.json`。

## Session 入口

1. 加载 `project-orchestrator`，作为唯一的项目生命周期 Skill。
2. 由它读取 Project Profile、当前 Git 状态，以及当前任务必需的入口文件。
3. 使用领域 Skill 处理具体交付物，使用确定性 Tool 执行状态变更。
4. Plugin、Skill 与 MCP 的盘点、同步或迁移交给 `agent-pack`。
5. `OPINION.md` 含用户确认的规则时，实施前由 `opinion-manager` 提供指导，交付前执行检查。Profile 声明了分层加载时，先读取核心规则与规则束索引，任务命中时读取对应规则束；工具返回 `fallback` 时完整读取 `OPINION.md`。
6. 读取 Project Profile 的 `memory` 段声明的记忆索引。依据某条记忆行动前，按 `AGENT.RULES.md` 的记忆约定核实。

## 职责边界

- `agents-init` 只创建或迁移本骨架。
- `project-orchestrator` 按 `work.mode` 选择工作容器、组织 Case 目录、管理进度、验证编排和 Git 交接。
- `agent-pack` 负责项目与客户端之间的 Plugin/Skill/MCP 能力管理。
- Opinion 指导并审查交付物，不管理项目生命周期。
- 领域 Skill 禁止创建无关 Task、编辑根进度、自我修改或自主提交。
- 保留无关改动。push、发布、merge、删除与破坏性迁移都需要明确授权。

## 人用入口

不需要 Agent 也能运行 `ENTRY_COMMAND` 打开项目菜单：找资料、看进展、项目指南、检查与维护。Agent 需要同样的信息时调用它的子命令并加 `--output json`（例如 `ENTRY_COMMAND find <词> --output json`），不另写一套查找逻辑；写入类子命令默认只预览。

## 项目专属入口

- 命令：TODO
- 测试：TODO
- 构建：TODO
- 架构/文档：TODO
- 隐私或禁止路径：未声明
- 用户规则：若存在 `AGENT.USER.md`（接管项目时从原 `AGENTS.md` 移出的用户内容），与本文件一并遵循。
```
<!-- /agents-init:template -->

保持入口简短。把稳定的项目专属约束放入 `AGENT.RULES.md`；禁止在任一入口文件复制完整生命周期工作流。

## 5. 其他生成文件

### AGENT.RULES.md

<!-- agents-init:template AGENT.RULES.md -->
```markdown
# 项目专属规则

本文件只保存当前仓库独有的稳定约束。通用生命周期由 `project-orchestrator` Skill 负责，扩展能力管理由 `agent-pack` 负责。

## 命令与验证

- 初始化：TODO
- 测试：TODO
- 构建：TODO

## 文件落位与生成输出

- 权威源：TODO
- 生成输出：TODO

## 隐私与外部系统

- 默认禁止读取：未声明
- 需要授权的外部写入：全部，除非另有明确配置

## 记忆

- 写入去向：每类信息只有一个权威位置，其他位置只留指针。稳定的项目事实与外部资料位置写入记忆条目；进行中的工作状态写入所选工作模式的权威记录；行为与表达偏好经 `opinion-manager` 的候选流程确认；`.agent-doc/chat-summary.md` 只保存未决事项与指针。
- 不写入记忆：能从文件或版本历史直接得到的内容；已在规则或本文件中的内容；已完成工作的流水；只对当前对话有用的内容。
- 召回：记忆反映写入当时的事实。依据某条记忆行动前，核对其中提到的文件、命令与外部对象仍然存在；发现过期时更新或退役该条目。
- 客户端私有记忆：客户端自带的记忆功能只保存该客户端自身的运行事实。项目事实、偏好与结论写入仓库，使所有客户端读到同一份内容。
- 收尾：结束一个里程碑前生成记忆索引并运行检查。
- 参数（体积预算、复核间隔）：未设定
```
<!-- /agents-init:template -->

### OPINION.md

<!-- agents-init:template OPINION.md -->
```markdown
# 项目 Opinion 覆盖层

尚未配置 Opinion。

使用 `opinion-manager` 选择直接用模板、选择部分条目或从空白开始；也可基于已授权记忆提出候选。完整预览经用户确认后生效，个人内容只写入当前项目。
```
<!-- /agents-init:template -->

### CLAUDE.md 与 AGENT.md

Claude Code 在工作目录及其上级目录没有任何 `CLAUDE.md` 时才直接读取 `AGENTS.md`（v2.1.277 起），两者并存时只读 `CLAUDE.md`；旧版本、禁用内置插件以及部分会话也读不到 `AGENTS.md`。因此 `CLAUDE.md` 保留为一行 `@AGENTS.md` 的导入桩，`AGENTS.md` 是唯一内容来源，Claude 专属指令写在导入行下面。`AGENT.md` 给其他客户端，用文字路由。

<!-- agents-init:template CLAUDE.md -->
```markdown
@AGENTS.md

## Claude Code

Claude Code 专属指令写在这里；项目通用规则放在 `AGENTS.md` 与 `AGENT.RULES.md`。
```
<!-- /agents-init:template -->

<!-- agents-init:template ROUTE.md -->
```markdown
# Agent 路由

读取 `AGENTS.md` 并遵循其中的 Project Profile。加载 `project-orchestrator` 管理项目生命周期；Plugin/Skill/MCP 能力管理交给 `agent-pack`；Opinion 的生成、读取与审查交给 `opinion-manager`。
```
<!-- /agents-init:template -->

### 受管区块模板

接管已有文件时，工具只向用户的文件追加带标记的区块（见 `scripts/regions.py`），区块之外的内容保持原样。区块正文来自下面的模板，随内容包更新；区块内部被改过时不会被覆盖，由用户选择。

<!-- agents-init:template region/claude-import -->
```markdown
@AGENTS.md
```
<!-- /agents-init:template -->

<!-- agents-init:template region/agents-contract -->
```markdown
## agenthelm 项目契约

本项目由 agenthelm 管理项目生命周期。开始实质工作前：

1. 加载 `project-orchestrator`，读取 `.agents/moe.sakanano.agent-pack/project.json`（Project Profile）。
2. 项目专属约束见 `AGENT.RULES.md`；Plugin/Skill/MCP 管理交给 `agent-pack`；Opinion 交给 `opinion-manager`。
3. 本文件其余部分是接管项目前就有的用户规则，与上述契约一并遵循。
```
<!-- /agents-init:template -->

### 流程占位文件

`.agent-doc/plan.md`:

<!-- agents-init:template .agent-doc/plan.md -->
```markdown
# 计划

当前没有活动的多步骤计划。
```
<!-- /agents-init:template -->

`.agent-doc/progress.md`:

<!-- agents-init:template .agent-doc/progress.md -->
```markdown
# 进度

当前没有活动的工作记录。项目工作状态由 `project-orchestrator` 通过已配置的项目子系统管理。
```
<!-- /agents-init:template -->

`.agent-doc/chat-summary.md`:

<!-- agents-init:template .agent-doc/chat-summary.md -->
```markdown
# 会话摘要

本文件只保存尚未处理的事项与指向权威位置的指针。事项有了结论后写入对应位置，并从这里移除。

## 待确认假设

无。

## 未解决冲突

无。

## Opinion 演化候选

无。候选规则通过 `opinion-manager` 提交并由用户确认，禁止在此自动晋升。
```
<!-- /agents-init:template -->

三个固定小节的名称保持不变，便于工具按标题定位。项目可以按需追加小节，例如跨 Session 的恢复摘要或定时服务的状态指针；追加小节的写法与上限由项目自己的记忆参数规定。

`docs/refs/README.md`:

<!-- agents-init:template docs/refs/README.md -->
```markdown
# 参考资料

项目契约要求保留可复用参考资料时，将其放在这里。
```
<!-- /agents-init:template -->

## 6. Agent Plugin 占位文件

`.agents/plugin.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "PROJECT_SLUG",
  "version": "0.1.0",
  "description": "项目本地的可移植 Agent 能力包。"
}
```

`.agents/mcp.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {}
}
```

在 `agent-pack` 导入已选择的 Skill 前，保持 `.agents/skills/` 为空。禁止从公开模板初始化包含密钥的 MCP 配置。

## 7. 初始化与迁移

### 新项目

1. 使用只读命令检查事实。
2. 填写有证据支持的 Profile 值，未确定的命令保留为 `TODO`。
3. 预演初始化器并展示碰撞项。
4. 确认同一分发包内的 `project-orchestrator`、`agent-pack` 与 `opinion-manager` 可用，获得授权后应用。
5. 解析 JSON 并验证路由文件。
6. 调用 `opinion-manager`，只展示直接用模板、选择模板的部分条目、从空白开始迭代；另可选择基于已授权记忆提出候选规则。选定后再完整预览与确认；从空白开始时不创建空规则版本。

### 既有项目

1. 保留现有文件并识别其权威性。
2. 与 v6 比较职责，不以文件名是否相同作为判断依据。
3. 先创建机器可读 Profile。
4. 只有 `project-orchestrator`、`agent-pack` 与 `opinion-manager` 验证通过后，才精简重复的 Agent 入口说明。
5. 保持历史 Task/Case 与能力源完整，随后使用 `agent-pack` 盘点。
6. 只有得到明确授权，才能把个人 Opinion 内容移入私有权威源。

### 接管已有内容的目录

目录里已经有自己的文件时，原则是**控制新增，不追溯历史**：与骨架冲突的旧文件可以长期并存，不是阻塞项；只有用户显式选择，才改动已有文件，且先备份到项目外。

1. `--mode scan`（只读）：列出路径碰撞、目录含义是否相同、`AGENTS.md` / `CLAUDE.md` 的状况，每项给出选项和推荐。
2. `--mode adopt`（默认预览，`--apply` 才写入）：已有文件原样保留，只补缺失的骨架；保留下来且与模板不同的文件记入决定文件，`check` 不再追问。
3. 改动已有文件才需要显式选择，并带 `--recovery-dir`：`--agents-policy append|split|side-by-side`、`--claude-policy add-import`。`split` 用 `--route <章节序号>=<agents|rules|user|opinion|sop>` 指定每个章节的去向（建议来自关键词启发式，最终由用户决定；`opinion` / `sop` 先放进 `AGENT.USER.md` 并标注待交接）。
4. 目录含义冲突的用 `--case-root` / `--memory-root` 另设位置，已有的工作容器目录用 `--register-root` 只读登记。
5. 追加到用户文件里的内容是带标记的受管区块（见 `scripts/regions.py`），区块之外不动；区块内部被改过时升级不覆盖，用 `--mode region` 逐个 `--keep` / `--accept`。

历史内容什么时候迁移由用户决定：之后可以用 `--mode adopt` 以外的迁移工具逐步把旧内容整理进标准结构，不影响日常使用。

### 将既有项目整理为 Case 工作目录

1. 阅读同包 `project-orchestrator/references/case-workspaces.md`，明确 Case 的目标、子工作项与专属材料范围。
2. 使用 `--mode migrate --work-mode case-workspace --replace .agents/moe.sakanano.agent-pack/project.json --recovery-dir <项目外恢复目录>` 预演；旧 task 配置保存为 `legacy_task`，原业务文件保持在原位，等待独立的已授权数据迁移。
3. Profile 替换保留项目自定义的 runtime、opinion、memory、capabilities 和 privacy。未显式选择模式时保留现有 `work` 段。
4. 展示全部 Task、Case 专属输入和交付物的目标映射，Demo 通过后用 `case_workspace.py migrate` 实施；保留旧 TASK.md、progress、旧 ID 与真实完成或放弃证据。
5. 用项目 Tool 校验 Case、链接和索引，验证旧名称检索与全量统计；迁移完成后关闭生产态 Task 创建入口。其他项目单独授权并迁移。

### 为已初始化的项目补上人用入口

1. `--mode entry --entry-name <名称>` 预览：只涉及 Profile 的 `entry` 段、根目录启动器与内容锁；加 `--readme-entry` 时在 README 末尾追加带标记的「怎样使用」。
2. 根目录已有同名文件时视为用户自己的文件：`--keep <名称>` 保留（不跟踪更新），`--accept <名称>` 用模板替换并先备份到 `--recovery-dir`；不选则不写入。
3. 确认后加 `--apply`，再运行 `./<名称> health` 验证。也可以在菜单「检查项目与环境」里处理“项目根目录还没有人用入口”。

### 为既有 v6 项目补充记忆与分层加载

这两项都是增量，不改变已有文件的职责：

1. 在 Project Profile 增加 `memory` 段与 `opinion.loading_mode`；已有自己记忆实现的项目把 `provider` 设为 `project` 并填写命令。
2. 把 `AGENT.RULES.md` 模板中的「记忆」一节合并进项目现有文件，保留项目已有的约定与参数。
3. `.agent-doc/chat-summary.md` 保留三个固定小节；其中已有结论的条目移入对应的权威位置。
4. `AGENTS.md` 的 Session 入口由用户审阅差异后选择 `--replace` 或手动补充第 5、6 步。
5. 分层加载需要在 Opinion Profile 中增加加载声明并经用户确认，由 `opinion-manager` 处理；在此之前保持完整读取。

### 从 v5 迁移

v5 把生命周期与扩展管理合并在 `project-runtime` 中，Profile 位于 `.agents/moe.sakanano.project-runtime/project.json`。`--mode migrate` 检测到该文件时：

1. 保留 `profile`、`capabilities`、`work`、`privacy`、`opinion` 与 `runtime` 中的项目自定义字段；
2. 把 `runtime.skill` / `runtime.plugin` 改为 `project-orchestrator`，写入 `runtime.capability_manager = "agent-pack"`；
3. 写入新路径 `.agents/moe.sakanano.agent-pack/project.json`，并把旧文件移动到 `--recovery-dir`（必须位于项目外）；
4. 入口文件中的 `project-runtime` 引用不自动改写，由用户逐项选择 `--replace` 或手动精简。

迁移后在各客户端用 `agent-pack reconcile --retire-plugin project-runtime` 归档旧 Plugin。

## 8. 验收标准

- 所有生成的 JSON 文档均可解析。
- `AGENTS.md` 与路由文件指向同一个 Project Profile、`project-orchestrator`、`agent-pack` 与 `opinion-manager`。
- 只有一个 Skill 负责项目生命周期：`project-orchestrator`；只有一个工具负责扩展能力：`agent-pack`。
- Opinion provider 配置为 `opinion-manager`；`OPINION.md` 可以保持空白。
- Project Profile 声明记忆提供方；`AGENT.RULES.md` 含记忆的写入去向、召回核实与客户端私有记忆边界。
- 个人规则、凭据、token、私有路径或私有 Skill 内容均未进入公开模板。
- 除非用户逐项批准替换，否则保留现有文件。
