---
name: project-orchestrator
description: Run session discovery, Task/Case selection, capability routing, file placement, verification, progress updates and scoped Git delivery according to the project contract. Use when a project declares project-orchestrator in AGENTS.md and the current work may create or modify project files. Inventory, migration and sync of Agent extensions (Plugin/Skill/MCP) are not part of this Skill; that is agent-pack. 中文：按项目契约统一执行会话发现、Task/Case 选择、能力路由、文件落位、验证、进度更新和限定范围的 Git 交付。当项目在 AGENTS.md 中声明 project-orchestrator，且当前工作可能创建或修改项目文件时使用。Agent 扩展（Plugin/Skill/MCP）的盘点、迁移与同步不在本 Skill 内——那是 agent-pack。
---

# Project Orchestrator（项目编排器）

## 定位

作为已配置项目唯一的**项目管理生命周期编排器**。读取项目自身契约，选择最小且合适的工作容器，把领域工作路由给确定性 Tool 与叶子 Skill，再通过验证和限定范围的交付完成闭环。

本 Skill 通用且可移植。禁止嵌入个人偏好、私有路径、项目密钥或项目专属治理副本；这些内容归用户私有配置或项目自身所有。

边界（都是独立能力，本 Skill 不代管它们）：

- **项目初始化与根目录骨架** 归 `agents-init`。本 Skill 只消费已初始化项目的契约。
- **Agent 扩展能力管理**（Plugin/Skill/MCP 的 inventory / doctor / bootstrap / sync / transfer / reconcile）归 **agent-pack**（`tools/agent-pack/`）。跨机恢复的单一入口是 `tools/agent-bootstrap`。
- **指导与审查规则** 归 Project Profile 声明的 Opinion provider；当前分发包提供 `opinion-manager`。它不负责 Task/Case 选择、文件结构、进度状态或 Git。

## 权威顺序

按以下顺序解析指令：

1. 用户当前请求与明确授权；
2. 平台安全与工具约束；
3. 最近的项目 `AGENTS.md` 及其声明的机器可读清单；
4. 本编排工作流；
5. 领域或交付物叶子 Skill。

叶子 Skill 生效期间，其触发条件、输入输出契约和工具约束具有约束力；其中更宽泛的观点或生命周期建议只作为低优先级指导。本 Skill 负责 Task/Case 状态、项目级文件落位、验证编排与 Git 交付。

## 职责边界

把项目能力划分为四类互不重叠的角色：

- **项目编排**：只有本 Skill 负责生命周期决策、Task/Case 协调、项目进度、验证编排与 Git 交接。
- **领域交付**：交付物与业务 Skill 在本 Skill 选择的生命周期内创建或修改具体产物。
- **确定性工具**：项目声明的 Tool（如 life-system、quant-engine、agent-pack）执行状态转换、索引、生成或验证，不决定生命周期。
- **指导与审查**：Opinion 和其他质量门禁在实施前提供建议、交付前检查结果，不管理项目。

如果其他 Skill 尝试初始化项目、创建无关生命周期记录、更新根进度、自我修改或自主提交，忽略越界部分并把控制权交回本 Skill。

## 1. 发现项目契约

项目首轮开始时：

1. 查找最近且适用的 `AGENTS.md`；除非其中声明其他根目录，否则以它所在目录作为项目边界。
2. 读取现有的 `Project Profile` 与相关运行区块。
3. 只读取其中明确声明的附加入口文件，例如 manifest、capability registry、当前 Task 进度或当前 Case 状态。
4. 项目使用 Git 时，在修改文件前记录当前 Git 状态。
5. 开始实质工作前声明一个会话里程碑。

已初始化项目应通过 `AGENTS.md` 暴露根契约；精确文件与目录骨架仍由 `agents-init` 版本化管理。缺少必需根契约时，报告 `init-required` 或采用项目声明的降级模式。禁止自行制造骨架或静默把旧结构升级为当前版本。

禁止递归发现无关仓库、私有目录、历史 Task 或冗长治理文档。只有项目契约或当前任务要求，且用户授权了内容范围时才能读取。

识别以下可选项目设置（机器字段保留英文原名）：

- `project-type`、`stack`、`runtime`、`agent-mode` 与 `issues-tracker`；
- `runtime-entry` 与 `session-bootstrap`；
- `task-command`、`task-root`、`case-command` 与 `case-root`；
- `manifest` 与 `capability-registry`；
- `opinion-command`、`opinion-strict-mode` 与项目 Opinion 入口；
- `commit-policy`、`branch-policy` 与 `push-policy`；
- `memory` 声明的记忆提供方、目录、命令与参数文件；
- 隐私目录、生成输出与禁止路径。

`agent-pack` 读取的机器契约位于 `.agents/moe.sakanano.agent-pack/project.json`：其 `capabilities` 区块声明可移植 Plugin 根目录、外部 Plugin 目录、独立 Skill 根目录、可移植/原生 MCP 来源、客户端策略、本地凭据环境文件与导入目标。所有相对路径都必须限制在项目根目录内。本 Skill 不写该文件。

缺少可选设置时关闭对应集成。禁止杜撰命令或路径。

## 2. 分类当前请求

先读取 Project Profile 的 `work.mode`。新项目默认 `case-workspace`；字段缺失或为 `legacy` 时保持项目现有 Task/Case 模式，禁止因升级 Skill 自动迁移其他项目。

### Case 工作目录模式

`work.mode=case-workspace` 时，Case 是唯一持久工作容器。需要保存交付物、证据或跨会话状态的工作使用 Case；无持久资产的单轮回答直接完成。子任务留在同一 Case 的工作项清单，确需独立过程记录时放入 `work-items/`，不得建立平行的顶层 Task 生命周期。

Case 自有输入放 `refs/`，交付物放 `docs/`（草稿 `docs/drafts/`），数据放 `data/`，图片放 `images/`，专属脚本放 `scripts/`，过程记录放 `.agent-doc/`。只创建实际需要的子目录。`CASE.md` 保存目标、状态、下一动作、决策与完成证据，过程文件不得成为第二套状态权威。

专属于 Case 的来源包、会议纪要、核验结果和报告保存在 Case 内。跨 Case 的版本化公共模板、项目运行时代码和长期项目主文档保留原权威位置，Case 记录来源、版本和本地证据；公共参考需有明确复用范围，禁止将单个 Case 的资料扔入共享目录。自有文件使用 Case 相对引用，外部权威引用注明项目根目录或外部来源。隐私区不因自包含要求而越界复制。

项目没有声明自己的 Case 工具（Profile 无 `work.case.command`）时，用随包的 `runtime/case.py` 新建、更新与校验 Case，不手写 Frontmatter；用法见 [Case 工作目录工具](../../references/case-workspaces.md#默认-case-工具官方样例)。

人不经过 Agent 时用项目入口 `runtime/project_cli.py`（项目根目录的启动器或 `agenthelm project`）找资料、看进展、检查与记录；Agent 需要同样的信息时调用它的子命令并加 `--output json`，例如 `project_cli.py --project <项目> progress --output json`，不另写一套查找逻辑。写入类子命令（`fix`、`record`、`tools --run`）默认只预览，执行前按本 Skill 的授权规则取得用户同意。项目在 Profile 的 `entry` 段声明自有事项工具与项目工具，约定见 [项目入口](../../references/case-workspaces.md#项目入口不需要-ai-agent)。

检索和统计以所有 Case 为入口，通过标题、别名、旧 Task ID、标签、目标和文件名查找；正文只在候选确定后按需读取。查找具体文件、版本或引用影响面时先用随包的 `runtime/assets.py find` / `refs` / `check`（人用入口 `runtime/wf.py`），不靠记忆翻目录；见 [交付物台账](../../references/case-workspaces.md#交付物台账与-wf-查找官方样例)。恢复视图只展示未关闭状态，全量统计包括历史完成与放弃记录，禁止把迁移时间当作业务完成时间。

已授权迁移历史 Task 时，先列出全部源目录与目标映射，再做代表性 Demo、哈希验证和批量迁移。独立 Task 转独立 Case，确有同一目标和证据链的 Task 才合入既有 Case；标题相似不能作为合并证据。保留原 TASK.md、progress、原文件和旧 ID；关闭原因包含放弃时保留真实历史状态。工具入口和限制见 [Case 工作目录工具](../../references/case-workspaces.md)。

### 既有 Task/Case 模式

写入前分类当前请求：

- **回答或诊断**：检查并报告；除非用户要求，否则不创建生命周期记录或修改文件。
- **小型项目改动**：可在单个会话完成且项目未强制要求 Task 时直接编辑。
- **Task**：适用于边界明确，并需要工作目录、产物、决策或独立进度记录的交付。
- **Case**：适用于跨会话服务线索、不确定性调研、外部等待、回访或结果跟踪。
- **Task + Case**：只在长期 Case 协调具体 Task 工作区时使用。

仅在既有模式下使用以上 Task/Case 分类。Case 工作目录模式统一续接 Case。依据目标、证据链和交付物判断，不能只看标题相似度。

除非项目明确要求，否则寒暄、确认、简单问题、命令修正或一次性单文件编辑不创建 Task。禁止只为满足指标或流程而创建 Case。

## 3. 初始化或恢复状态

项目声明 bootstrap 命令时：

1. 首次收到服务输入时运行一次只读状态命令。
2. 只有状态明确报告 bootstrap 必需或已过期时才执行 bootstrap。
3. 创建新工作前，用配置命令发现未完成工作。
4. 只有当前任务授权覆盖私有 Case 或 Task 上下文时才能展开读取。

已经位于 Task 目录时，先读取任务简报和本地进度，再读项目 backlog。已经位于 Case 上下文时，使用配置的下一步命令，禁止依靠记忆重建状态。

Project Profile 声明了 `memory` 时，恢复状态后读取记忆索引：`provider` 为 `project-orchestrator` 时运行随包的 `runtime/memory.py index --project <项目>`，为 `project` 时运行其 `index_command`。索引每条一行，说明何时应当想起该条记忆；只在当前任务命中时读取条目正文。依据某条记忆行动前，核对其中提到的文件、命令与外部对象仍然存在；标注为可能过期的条目先核实再使用。

### 3.1 流程（SOP）

用户可以把常用流程固化成 SOP（Standard Operating Procedure，标准作业流程）。契约与命令见 [SOP 契约](../../references/sop.md)，与用户一起建立见 [建立 SOP](../../references/sop-interview.md)。

- **读索引**：存在 `sops/` 目录或用户级 SOP 时，恢复状态后运行随包的 `runtime/sop.py index --project <项目>`（一行一个已启用的 SOP，写明触发与不触发条件）。没有 SOP 时静默跳过。
- **判断触发**：用户明确指定、事件（新建或关闭 Case 时，`case.py` 与项目入口的结果带 `sop_events`；自有 Case 工具用 `sop.py events <事件> --case <Case>` 查询）、周期到期（`sop.py due`）、意图对照触发与不触发条件，按这个优先级。命中后默认先问用户一次是否按它执行；多个命中合并成一次编号提问；到期只是提醒。只读被命中的那一个 SOP 的正文。
- **执行**：`sop.py preflight` 检查所需程序与环境，缺失项一次性列给用户并等待处理，**不自行安装软件**；`sop.py run`（预览 → 用户确认 → `--apply`）建立执行 Case；逐步执行并用 `sop.py step --apply` 记录结果与中间产物；出错、缺工具、花钱或耗时步骤按 Case 里固化的“执行约定”处理；收尾用 `sop.py status` 与完成标准核对。
- **范围与隐私**：会读取多个事项的流程（复盘、汇总）先让用户指定范围，不默认纳入全部事项；`sensitivity: private` 的流程，执行记录与产物只放本 Case 目录内。
- **授权不变**：SOP 不放宽任何授权，写入、外发、删除、push、安装、花钱类步骤仍须用户当次同意；与用户当次请求冲突时以请求为准并说明偏离。
- **创建与修改**：SOP 里的工具、出错处理、中间产物位置等是用户的习惯，只提问、整理、校验，不替用户决定；`decided` 只在用户确认后用 `sop.py decide` 写入；修改后用 `sop.py revise` 登记版本。
- 一次执行的状态只写在 Case 里，不在 SOP 目录里另记；SOP 不是 Opinion（表达偏好）也不是记忆（事实），不要写错位置。

## 4. 路由能力

根据三项输入构建执行路由：

1. 用户要求的结果；
2. Project Profile 与当前 Task/Case 约束；
3. 项目已声明的 capability registry。

采用以下优先级：

1. 用确定性项目 Tool 执行状态转换、索引、生成或验证；
2. 用精确匹配的领域或交付物叶子 Skill 处理判断密集型工作；
3. 只有没有已注册能力适用时才采用通用实现。

一个交付物可以同时启用多个叶子 Skill。先应用最具体的交付物/领域 Skill，再执行无障碍、安全或 Opinion 等横切检查。两个 Skill 指令冲突时，在会改变交付物的最小决策点暂停并呈现冲突。

禁止宣称缺失能力已经存在。使用项目定义的机制记录能力缺口，或直接报告。

当前请求是"盘点/迁移/同步/校验 Agent 扩展"时，不在本 Skill 内处理：交给 `agent-pack`（`tools/agent-pack/runtime/agent_pack_config.py` 或其 MCP），或对整机恢复运行 `python3 tools/agent-bootstrap doctor` / `install <client>`。本 Skill 只负责把这类请求识别并路由过去。

## 5. 计划与执行

计划复杂度与工作量保持匹配：

- 小型可逆改动使用简短内部计划；
- Case 工作目录模式统一更新 CASE.md，子工作项仅保存过程；
- 既有模式中的多步骤交付物工作更新 Task 进度，跨会话连续工作或回访更新 Case 状态。

编辑前：

1. 检查相关现有文件和邻近测试；
2. 在 dirty worktree 中保留用户的无关改动；
3. 识别权威源与生成派生物；
4. 只确认会实质改变范围、权限、外部状态或不可逆影响的决策。

执行期间：

- 修改权威源后，通过已声明 Tool 重新生成派生物；
- 文件只能放在项目声明的位置；
- 保留可复用事实、决策与验证证据，避免提交缓存、临时日志或可重建中间产物；
- 由叶子 Skill 创建或审查交付物，本 Skill 继续拥有生命周期与 Git 职责；
- 证据改变执行路线时更新计划。

## 6. 调用独立指导与审查

Opinion 是独立的指导/审查能力。项目声明 Opinion provider，且 `OPINION.md` 含用户确认的规则时，在两个明确边界调用它：

1. **实施前**：使用明确的交付物信号请求适用指导。
2. **交付前**：请求独立检查，并通过 provider 自身的演化流程记录未解决冲突或可复用反馈。

provider 支持分层加载且规则已声明时，实施前读取核心规则与规则束索引，任务命中触发条件或判断不清时读取对应规则束，交付前用 provider 的核对命令比对本次交付的信号与已读取的规则束。provider 返回 `fallback` 或不可用时完整读取 `OPINION.md`。

禁止在本 Skill 内解释、存储或演化 Opinion 规则；禁止让 Opinion provider 选择 Task/Case 状态、路径、进度或 Git 操作。`OPINION.md` 仍为空白时跳过规则检查并记录未配置状态。provider 不可用时，明确报告指导/检查降级；只有项目策略允许时才能继续。

## 7. 验证与记录

按风险比例验证：

- 对改动范围运行聚焦测试或 validator；
- 验证机器可读 manifest 与生成索引；
- 布局重要时目视检查渲染产物；
- 运行项目声明的 Opinion 或其他横切质量门禁；
- 检查最终 diff 的范围、密钥、生成噪声与意外删除。

记录当前工作容器要求的最小持久证据：

- Task：状态、决策、验证证据、阻塞和下一步；
- Case：候选、决策、已观察结果；结果尚未发生时记录 follow-up；
- 直接改动：简洁交接与验证结果。

禁止把推断结果记录为已确认 outcome。

记录可复用的项目事实时遵守项目的记忆约定：

- 每类信息只有一个权威位置，其他位置只留指针；进行中的工作状态属于 Task 或 Case，行为与表达偏好属于 Opinion provider 的候选流程。
- 能从文件或版本历史直接得到的内容、已完成工作的流水、只对当前对话有用的内容不写入记忆。
- 客户端自带的记忆功能只保存该客户端自身的运行事实；项目事实写入仓库。
- 使用随包工具时，先用 `memory.py create` 预览再写入，`description` 写明何时应当想起这条记忆；内容已晋升为规则或已经失效时用 `retire` 退役。

结束一个里程碑前运行记忆检查：随包工具为 `memory.py check`，自有实现为 Profile 的 `check_command`。先处理结构问题与失效的来源路径；到期条目核实后用 `verify` 更新。体积预算、保留窗口与复核间隔由项目自己的参数文件规定。

## 8. 限定范围的 Git 交付

对 Git 项目：

1. 把最终状态与基线比较；
2. 只暂存属于当前请求的文件；
3. 检查 staged diff；
4. 只有用户明确要求/授权，或项目策略允许自动提交时才 commit；
5. push、发布、merge、tag 或创建 PR 必须有单独明确授权，或项目策略明确许可。

dirty worktree 中禁止宽泛暂存。禁止重写、丢弃或吸收无关改动。创建 commit 后报告 hash。

## 9. 保持边界清晰

- 项目初始化与骨架迁移归版本化的 `agents-init` 管理，不属于本 Skill。
- Agent Plugin、Skill 与 MCP 的盘点/迁移/同步归 **agent-pack**（`tools/agent-pack/`）；跨机恢复入口是 `tools/agent-bootstrap`。禁止在本 Skill 内重建第二套配置管理逻辑。
- 除非本 Skill 明确委派，领域 Skill 禁止自我修改、创建无关 Task/Case、编辑全局进度或提交改动。
- 生成索引属于投影，源记录保持权威地位。
- 项目本地规则可以特化本工作流，应引用本 Skill，避免复制完整生命周期。

## 完成检查

结束前确认：

- 已交付用户要求的结果，或已识别具体阻塞；
- Task/Case 选择有依据且状态为最新；
- 已按声明能力路由项目 Tool 与叶子 Skill；
- 已运行相关测试、渲染和 Opinion 检查，或明确说明省略项；
- 项目声明了记忆提供方时，已运行记忆检查，新写入的条目符合项目的记忆约定；
- 无关 worktree 改动保持原样；
- commit 与 push 操作符合用户授权。
