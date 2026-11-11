# Case 状态权威设计（Issue #65）

- 状态：设计草案，待维护者决策（见第 8 节）。本文不改任何运行时代码、Schema、版本、片段或发布载体。
- 基线：`v6.13.0`（`dev` 与 `main` 同在 `bb84a08`）；目标周期 6.13.1（全 patch）。
- 读者：仓库维护者。术语首次出现标注中英对照，文末有术语表。

> **#65 正文的一处表述与代码不符。** #65 写“AgentHelm 只消费 `work.case.command` 的 `list --all --output json`”。代码里执行 `list --all --output json` 的是 **`entry.items.command`**（`plugins/project-orchestrator/runtime/project_real.py:219-221`）；`work.case.command` 只用于让官方 `case.py` 在写入时让位（`runtime/case.py:183,200`；`references/case-workspaces.md:64`、`:133`）。两者是 Profile 里分开的字段，下文均按代码实际行为写。

## 0. 结论

1. **契约先行，检测随后。** 在 `plugins/project-orchestrator/references/case-workspaces.md` 增写“状态权威与索引”一节（草稿见第 2 节）：项目声明的 Case 状态权威必须是能随项目同步的文本记录，数据库只能是可从权威重建的索引。仅此一项就是 project-orchestrator **patch**、无 Schema 变化、无用户动作。
2. **Profile 字段要改 Schema，不能只靠文档约定。** `work.case` 虽是 `additionalProperties: true`，但 `tests/test_profile_reads_guard.py` 会拒绝运行时代码读取 Schema 里比已声明节点更深一层的 Profile 键（`work.case` 声明了 `properties`，所以 `work.case.state` 属于未声明的更深一层）。“不改 Schema”的唯一出路是把键登记进 `scaffold.READ_BY_OTHER_PLUGINS`，同样要改 agents-init 的代码，且丢掉 Schema 的字段说明与形状检查，因此无收益（第 3 节）。推荐：agents-init patch（声明 `work.case.state`，`touches: ["work.case.state"]`）+ project-orchestrator minor（检测），`default_for_existing: advisory`，不构成语义模板变化。
3. **检测落在 `health`，只提示、不改文件。** 权威路径在项目外 / 已被 Git 忽略且未跟踪 / 符号链接指向项目外时给出提示；算法复用 `private_paths.clean/relative`，Git 判断复用 `private_paths.git_state` 的子进程与超时约定，但需要一个对“文件本身”做判断的新辅助函数（第 4 节）。
4. **`state verify` 接口建议“声明才调用”。** `work.case.command` 现在从不被 AgentHelm 执行（只用于禁止官方 `case.py` 写入），对未声明的工具直接探测未知子命令可能触发副作用。默认：Profile 显式声明 `work.case.state.verify: true` 才运行；缺省只在“可选能力”清单里提示一行，不报错。这与 #65 原文“缺省时只提示”一致，但把“怎样算缺省”收紧了（决策 D4）。
5. **一处需要纠偏：** #65 写“AgentHelm 只消费 `list --all --output json`”针对 `work.case.command`。代码里被消费的是 **`entry.items.command`**（`plugins/project-orchestrator/runtime/project_real.py:220-221`）；`work.case.command` 只用于禁写（`runtime/case.py:183,200`，`case-workspaces.md:64`、`:133`）。两者在 Profile 中是分开的字段。

---

## 1. 现状核实

均已在 `bb84a08` 的工作树核对（读文件，未改任何内容）。

- **官方 Case 工具以 `CASE.md` 文本为权威。** `runtime/case.py` 直接读写 `cases/<ID>/CASE.md`，写入为原子替换（`write_atomic`，`case.py:226`）；没有数据库。
- **项目声明自有工具时，官方 `case.py` 只做只读。** `Project.__init__` 读取 `work.case.command` 到 `project_command`（`case.py:183`），`require_writable()` 在其非空时抛 `project-tool`（`case.py:199-201`）；`case-workspaces.md:64` 同。
- **被消费的是 `entry.items.command`。** `_run_items_tool` 执行 `[*entry.items.command, "list", "--all", "--output", "json"]`，`cwd` 为项目根，不经过 shell，超时 `TOOL_TIMEOUT_SECONDS = 30`（`project_real.py:36,220-228`）；返回 `{"items": [...]}`，约定写在 `case-workspaces.md:114-135`。只有 `work.case.command` 而没有 `entry.items` 时，进展仍按 `CASE.md` 读取（`case-workspaces.md:133`）。因此 #65 描述的“项目自有 Case 工具状态割裂”场景下，若项目同时声明了 `entry.items`，AgentHelm 展示的进展来自那个工具的本机库，而不是同步过来的 `CASE.md`——**这正是用户“等了几轮同步”的诊断盲区**。
- **资产台账是缓存，而且是正确的设计。** `runtime/assets.py` 把 SQLite 放在 `$XDG_CACHE_HOME/agenthelm/assets/<sha256(项目路径)[:16]>/`（`assets.py:170-177`），文件清单未变时重建为 no-op，查找结果“以文件本身与 CASE.md 为准”（`case-workspaces.md:82,84`）；缓存目录落在项目内则拒绝运行（`state-inside-project`）。目录键由**本机项目绝对路径**哈希而来，天然不可同步，也不应同步——它是派生物，不是权威。该约束只管 AgentHelm 自己的缓存，不约束项目自有工具。
- **原则已写但未落地。** `SKILL.md:239`「生成索引属于投影，源记录保持权威地位」；没有 Profile 字段，也没有检查。
- **Profile Schema 现状。** `plugins/agents-init/skills/agents-init/project.schema.json` 中 `work.case` 为 `type: [object, null]`、`additionalProperties: true`，已声明 `root`、`artifact_mode`、`command`、`schema`、`directory_naming`；`relativePath` 定义为“非空字符串，且不以 `/`、`~`、盘符开头”。顶层 Schema 的 `additionalProperties` 为 `false`，`work` 为 `true`。
- **相关运行时机制。** 私有路径工具 `runtime/private_paths.py`：`clean()`（拒绝绝对路径、`~`、`..`）、`relative()`（路径解析后判定是否在项目内，处理符号链接）、`git_state()`（`git check-ignore -q` + `git ls-files -z`，`GIT_TIMEOUT = 10`，无 Git 时 `ignored=None`）；`project_real.py:490` 起的 `_check_private` 是同类“路径 + Git 忽略”检查的现成模板。
- **会话开始并不运行 health。** `plugins/agent-pack/hooks/hooks.json` 的 SessionStart 只有更新/本机专属提示（`agenthelm.py check --hook`）与 nudge，均 `timeout: 10`；#65 的“health 开会话时调用”需要新增接线（第 5.4 节评估）。

---

## 2. 契约

### 2.1 写入 `case-workspaces.md` 的正式措辞（草稿）

建议放在「Case 工作目录工具」章节的「默认 Case 工具（官方样例）」与「交付物台账与 wf 查找（官方样例）」之间，作为二级小节「状态权威与索引」。

> **状态权威与索引**
>
> 一个 Case 工具管理的状态分为两类：**权威**与**索引**。
>
> - **权威**是生命周期状态的唯一来源。项目声明自己的 Case 工具（`work.case.command`）时，它的权威必须是**能随项目同步的文本记录**：位于项目目录内，被 Git 跟踪，或由项目声明的同步方式（同步盘等）覆盖；格式为人可读、可逐行比较、可由版本控制合并的文本（`CASE.md`、Markdown、JSON / JSONL 等）。
> - **索引**是为加速查询而从权威派生的数据，可以是 SQLite 等数据库。索引必须能够**仅凭权威重建**，丢失后不损失任何信息；它可以放在项目外或被 Git 忽略的位置，不要求同步。
> - 数据库**不得**作为权威。把权威放在不随项目同步的位置（项目外、Git 忽略且未跟踪、经符号链接指向项目外）会使多台机器的状态各自为政：一台机器新建的 Case，另一台机器上的 `CASE.md` 已同步，项目工具却报告不存在。
> - 项目可以在 Project Profile 的 `work.case.state` 声明权威与索引的位置：`authority`（项目相对路径，文件或目录）、`index`（可选，索引位置，可在项目外）、`rebuild`（可选，从权威重建索引的命令，数组或字符串）。AgentHelm 只读取这些声明用于检查与提示，**不会执行 `rebuild`，也不会修改权威或索引**。
> - 冲突合并（例如字段级“后写入者胜出”）由项目自己的工具决定，AgentHelm 不提供通用账本实现，也不迁移任何项目的 Schema。
> - 官方样例工具（`runtime/case.py`）与交付物台账（`runtime/assets.py`）遵守本约定：前者以 `CASE.md` 为权威，后者的 SQLite 是位于用户缓存目录的可重建索引。

英文对应稿随 `case-workspaces.md` 的整体英文化处理（该文件已登记在 `tests/i18n-policy.json` 的 `deferred`，本节不单独配对）。

### 2.2 论证与反例

- **为什么是“文本”而不是“可同步的数据库文件”：** 数据库文件放进 Git 或同步盘后，两台机器并发修改会产生无法按行合并的二进制冲突；同步盘还可能在写入中途同步半个文件。#65 报告的项目自己的补救（按机器分文件的追加式 JSONL 账本进 Git，SQLite 降为可重建索引）正是这一契约的实例。
- **为什么不限定 JSONL：** 项目的 Schema 各异，通用约束只到“文本、可比较、可合并”；格式留给项目。
- **允许的例外：** 项目显式声明用同步盘而不是 Git 同步时，Git 忽略的权威不应被判为问题（见 4.4 的误报边界）；契约措辞已用“项目声明的同步方式”覆盖。

---

## 3. Profile 字段：是否需要改 Schema

### 3.1 字段定义

`work.case.state`（全部可选，`additionalProperties: true` 以便项目附加自己的键）：

- `authority`：字符串，项目相对路径（文件或目录）。出现即触发第 4 节检测。
- `index`：字符串，索引位置；**允许项目外**（例如用户缓存目录），因此不用 `relativePath`，只要求非空字符串。
- `rebuild`：数组或字符串命令；仅用于提示文案，AgentHelm 不执行。
- `verify`：布尔，默认 `false`；`true` 表示项目工具实现了 `state verify`（第 5 节）。该字段不在 #65 的原始提案中，由第 5 节的“声明才调用”引入（决策 D4）。

### 3.2 加字段需要改 Schema 吗

结论：**需要。** 依据：

- `tests/test_profile_reads_guard.py` 的判定（文件头部注释）：读到的路径“自身或某个祖先在 Schema 里声明且该节点没有 `properties`（自由形态）视为已声明”；“路径比 Schema 更深一层未声明的键会失败”。`work.case` 已声明 `properties`，因此运行时代码读取 `work.case.state` 会使守卫失败。守卫的静态扫描范围含 `plugins/*/runtime/*.py`，`project_real.py`、`private_paths.py` 都在其中。
- 守卫的第二条出路是 `scaffold.READ_BY_OTHER_PLUGINS`（`plugins/agents-init/skills/agents-init/scripts/scaffold.py:374-376` 附近，现仅有 `opinion.nudge`）；其注释写明“首选在 Schema 声明，该列表只是为了让 Schema 副本较旧的项目不把它当成项目自己的字段”。
- 第三条出路是用守卫盲区（动态键、`dict.items()` 迭代等）绕开扫描，那样等于规避守卫，**排除**。

### 3.3 两种做法的发布声明对比

依据 `release/VERSIONING.md`、`scripts/release_workflow.py:185-200`（PR 门禁）、`scripts/release_model.py:150-153`、`scripts/release_meta.py:70,381-385`。

**做法 S（推荐）：改 `project.schema.json` 声明字段 + project-orchestrator 实现检测**

- 片段（一个文件，两条 `changes`）：
  - `agents-init`：`level: patch`，`touches: ["work.case.state"]`，`default_for_existing: "off"`，`user_action_required: []`，`template_changes: []`，`actions: []`。理由：仅声明一个可选字段，不改变任何既有项目的文件或默认行为。
  - `project-orchestrator`：`level: minor`（新增检测能力；若坚持 6.13.1 全 patch，则 patch，见 D3），`touches: ["work.case.state"]`，`default_for_existing: "advisory"`，`user_action_required: []`，`template_changes: []`。
- 为什么 `touches` 必须声明：`project.schema.json` 在 `EXPECTATION_FILES`（`release_meta.py:70`），PR 门禁检查“改了期望文件而相关片段没有非空 `touches`”（`release_workflow.py:198`），发布门禁再检一次（`release_meta.py:381-385`）。`touches` 的值写 Profile 字段 `work.case.state`（`VERSIONING.md`：`touches` 为用户项目路径或 Profile 字段）。
- 不构成语义模板变化：内容包 `agents-init/project-contract` 的 `sources` 只有 `AGENT.template.md` 与 `launcher.sh`；`agents-init/project-skeleton` 的 `sources` 只有 `skeleton.json`（`content.json` 核对）。`project.schema.json` 不是任何内容包的来源，因此不产生 `template_changes`、内容包版本不变（保持 1.4.0 与 1.1.0）。**不应**在 `skeleton.json` 里把 `work.case.state` 加成受检字段：它是可选字段，缺失不是缺陷；若加入就变成内容包 `project-skeleton` 的变化，需 `template_changes`。
- 对既有项目：无用户动作；既有 Profile 没有这个键，行为不变；声明了该键的项目升级后自动出现 `advisory` 级提示。
- 形状检查副作用：Schema 一旦声明 `state` 为对象、`authority` 为字符串，`agents-init` 的 Profile 形状检查（`scaffold.py` 的 `_shape_problem`，以 `profile-invalid`、级别 `must` 报告）会对**类型错误**的值报错（例如 `authority: 5`）。这对“写错了”是合理的；缺失与合法值不报。是否应把该级别降到提示，作为 D6 请维护者确认。

**做法 D：只写文档约定 + `check` 读取，不改 Schema**

- 必须登记 `scaffold.READ_BY_OTHER_PLUGINS["work.case.state"]`（agents-init 脚本代码变更，仍是 agents-init 的发布内容，等级 patch）。`project.schema.json` 未变，因此**不触发** `touches` 的强制要求（`release_workflow.py:198`），但声明应照写。
- 损失：编辑器与文档拿不到字段说明；没有类型形状检查；与守卫注释的“首选 Schema 声明”相悖；Schema 副本较旧的项目会把它报成“read-by-plugin”类别的额外字段（`_extras_summary`），提示反而更嘈杂（该行为细节**未确认**，需实现时核对）。
- 收益：无。仍然要发布 agents-init，且 `touches` 实际上同样要写。

**为什么 S 优于 D：** 发布成本相同（都改 agents-init），S 多得到字段说明与形状检查，且不违背守卫的设计意图。

---

## 4. check（health）检测

### 4.1 位置与形态

- 位置：`plugins/project-orchestrator/runtime/project_real.py` 新增 `_check_case_state(findings, ok)`，在 `health()` 中紧随 `_check_private` 调用（`project_real.py:337-343` 的列表）。`agenthelm check`（agent-pack）管插件更新，`agenthelm doctor`（`agent_pack_config.py`）管客户端能力，都不是项目数据检查的位置；仓库里没有独立的 `doctor` 项目检查。
- 级别：`suggest`（与 `_check_private` 一致）；只读；不修改任何文件，不执行 `rebuild`。
- 触发：`work.mode == "case-workspace"` 且 `work.case.state.authority` 为非空字符串。其余情况**不产生问题项**，仅在 `ok` 清单里留一行“未声明状态权威（work.case.state），已跳过状态位置检查”，避免对绝大多数项目增加噪音。

### 4.2 判定算法

输入：项目根 `root`、Profile、`authority` 字符串 `A`。依次判断，命中即产生对应问题项（可叠加）：

1. **项目外路径。** `rel = private_paths.clean(A)`；若为 `None`（绝对路径、`~`、含 `..`、盘符）：
   - 若 `private_paths.relative(root, A)` 解析后仍在项目内（绝对路径指回项目内），不报问题，建议改写为相对路径（`info`）；
   - 否则报 **`case-state:outside`**：权威在项目外，必然不随项目同步。
2. **符号链接指向项目外。** 对 `rel` 的每一层前缀，用 `os.lstat`（从**未解析**的 `root` 逐级向下，不含 `root` 自身及其父级）检查是否为符号链接；遇到则取 `target = current.resolve()`，与 `root.resolve()` 比较：不在其中则报 **`case-state:symlink-outside`**。注意：比较的是两个**都已 `resolve()`** 的路径，因此项目根自身或其祖先是符号链接（macOS 的 `/var → /private/var`、`/tmp → /private/tmp`）时不会误报，而且“项目内的符号链接指向项目内”不报。
3. **路径不存在。** 不进入 Git 判断，`info`：“权威路径当前不存在：新项目、或另一台机器尚未同步到本机”。若同目录存在 iCloud 占位文件 `.<名>.icloud`，则文案改为“尚未下载到本机（iCloud 占位文件）”（占位文件模式同 `agent-pack/runtime/local_only.py:67` 的 `ICLOUD_PLACEHOLDER_RE`；两个插件互不导入，实现时在 project-orchestrator 内复制该正则并加测试比对，做法同 `tests/test_private_paths_sync.py`）。
4. **Git 忽略且未跟踪。** 新增辅助函数 `private_paths.git_tracking(project, rel)`（命名待定），返回 `{"ignored": bool|None, "tracked": [...]}`：
   - `git -C <project> check-ignore -q -- <rel>`（退出码 0 = 忽略，1 = 不忽略，其他或超时 = 未知 → `None`）；`private_paths.git_state` 是对**目录**探测 `<dir>/.agenthelm-probe`，对文件型权威（如 `ledger.jsonl`）会把探测路径放到“文件之下”，Git 对这类路径的行为**未确认**，因此文件权威要直接检查路径本身，目录权威沿用探测路径；
   - `git ls-files -z -- <rel>` 非空表示**已被跟踪**——即便它匹配 `.gitignore` 规则（强制添加过）也会随 Git 同步，**不报**；
   - 仅当 `ignored is True` 且 `tracked` 为空时报 **`case-state:ignored`**；无 Git 仓库（`ignored is None`）时**不报**，因为项目可能用同步盘同步。
5. **二进制数据库作权威。** 路径扩展名为 `.db`、`.sqlite`、`.sqlite3`、`.db3`，或（文件存在、非私有路径）前 16 字节为 `SQLite format 3\0`：报 **`case-state:binary`**，级别 `info`（即便被 Git 跟踪，二进制库也难以合并，违反契约第一条，但不一定造成割裂）。路径落在 `private_paths.declared()` 内时**不读取文件内容**，只按扩展名判断（遵守 `privacy.forbidden_default_reads`）。
6. **索引与权威冲突。** `index` 与 `authority` 相同、或其中一个位于另一个之内：报 **`case-state:index-overlap`**（`suggest`）——索引应当可丢弃，不能放进权威目录。

### 4.3 复用与新增

- 复用：`private_paths.clean`、`private_paths.relative`、`private_paths.declared`（决定是否读取内容）、`GIT_TIMEOUT`、`subprocess` + 超时的错误处理方式，以及 `_check_private` 的 `findings` 结构（`id`、`level`、`area`、`title`、`reason`、`impact`、`next`）。
- 新增：对任意路径（文件或目录）的 Git 跟踪判断函数；符号链接逐级检查函数。`private_paths.py` 文件头声明“只依赖标准库”，新增函数保持该约束。
- **不复用 `agent-pack` 的 `local_only.py`**：它在另一个插件内，`project-orchestrator` 不应导入；同步盘识别（`detect_service`、`SYNC_SERVICES`）因此不在本版实现，转为文案里的条件说明与“记录决定”（4.5）。是否值得做跨插件调用见 D5。

### 4.4 误报边界

- **macOS `/var → /private/var`、`/tmp → /private/tmp`：** 两端都 `resolve()` 后比较，不会误报；测试用“经符号链接路径访问项目”的夹具覆盖（第 7 节）。逐级检查只从 `root` 之下开始，不把祖先目录的符号链接当作问题。
- **iCloud / Dropbox / OneDrive 同步盘：** 同步盘会同步被 Git 忽略的文件，因此“Git 忽略 + 项目只靠同步盘同步”并不割裂。本版对 `case-state:ignored` 的文案用条件句（“如果本项目靠 Git 在多台机器之间同步……”），并支持用户记录决定后不再提示（4.5）。iCloud 对 `.nosync` 后缀目录不同步，这一点若要用于“确定不同步”的升级文案，需要官方文档依据，**未确认**，本版不据此判定。
- **新建未提交：** 权威文件已存在、未被忽略、未被跟踪（工作树里的新文件）**不报**——这是正常的提交前状态。
- **无 Git 仓库或 Git 不可用：** 不报 `ignored`，只做路径与符号链接判断。
- **子模块 / 嵌套仓库：** 权威位于子模块内时 `git ls-files` 的行为**未确认**；实现时用夹具验证，行为不明时按 `ignored=None` 处理（不报）。
- **权威是目录且部分被跟踪：** 只要 `ls-files` 非空即视为受 Git 管理，不报。
- **大小写不敏感文件系统：** `.gitignore` 匹配遵循 Git 配置，本检查不自行比较路径大小写。
- **超时：** `git` 调用沿用 10 秒超时（`GIT_TIMEOUT`），超时按未知处理，不产生问题项。

### 4.5 不重复打扰

沿用 health 的“已记录决定”机制（`project_real.py` 的 `_skipped` 与 `--reprompt`，决定经 `agents-init` 的 `--mode decide` 记录）：用户对 `case-state:ignored` 选择跳过后不再提示。该机制逐行接口**未确认**，实现 issue 里需先核对问题项 `id` 与指纹的约定。

### 4.6 提示文案（草稿）

`area`：中文“Case 状态”/英文 “Case state”。所有文案只陈述事实与可选处置，不写“应当立即”，不自动执行。

- **`case-state:ignored`**
  - 中文：标题「Case 状态权威被 Git 忽略：`{path}`」；原因「该路径被 `.gitignore` 忽略且没有被跟踪，换一台机器克隆或拉取后不会包含它」；影响「如果本项目靠 Git 在多台机器之间同步，Case 状态会按机器割裂：一台新建的 Case，另一台上 `CASE.md` 已同步而项目工具报告找不到」；下一步「把权威改成可同步的文本记录并保留数据库作为可重建索引（见 `case-workspaces.md`「状态权威与索引」）；如果本项目靠同步盘而不是 Git 同步，可以记录决定后不再提示。本检查不会修改任何文件。」
  - English: title “Case state authority is ignored by Git: `{path}`”; reason “The path is matched by `.gitignore` and not tracked, so a clone or pull on another machine will not contain it”; impact “If this project syncs across machines through Git, Case state splits per machine: a Case created on one machine syncs its `CASE.md` to another, but the project tool reports it missing”; next “Make the authority a text record that syncs with the project and keep any database as a rebuildable index (see ‘State authority and index’ in `case-workspaces.md`). If this project syncs through a synced folder instead of Git, record a decision to stop this notice. This check never modifies files.”
- **`case-state:outside`**：标题「Case 状态权威在项目之外：`{path}`」；原因「Profile 的 `work.case.state.authority` 不是项目内的相对路径」；影响「项目外的文件不会随项目同步，多台机器各有一份」；下一步同上。English：“Case state authority is outside the project: `{path}`” / “`work.case.state.authority` is not a project-relative path” / “Files outside the project do not sync with it; each machine ends up with its own copy”.
- **`case-state:symlink-outside`**：标题「Case 状态权威经符号链接指向项目外：`{path}` → `{target}`」；原因「路径的某一级是符号链接，目标不在项目目录内」；影响同 outside。English：“Case state authority resolves outside the project through a symlink: `{path}` → `{target}`”.
- **`case-state:binary`**（info）：标题「Case 状态权威是二进制数据库：`{path}`」；原因「SQLite 等二进制文件无法逐行比较与合并」；下一步「建议改为文本记录，数据库只作可重建索引」。English：“Case state authority is a binary database: `{path}`” / “Binary files such as SQLite cannot be diffed or merged line by line” / “Prefer a text record as the authority and keep the database as a rebuildable index”.
- **`case-state:index-overlap`**：标题「Case 状态索引与权威路径重叠」；原因「`index` 位于 `authority` 之内，或两者相同」；下一步「索引应能被丢弃并重建，请放到权威之外」。

---

## 5. 可选 `verify` 接口

### 5.1 调用约定

- 命令：`<work.case.command> state verify --output json`，`cwd` 为项目根，**不经过 shell**（数组形式；字符串形式用 `shlex.split` 解析，Windows 引号规则**未确认**，决策 D7），环境变量继承。
- **仅当 `work.case.state.verify` 为 `true` 时调用。** 理由：`work.case.command` 目前从未被 AgentHelm 执行，贸然对未声明能力的项目工具探测一个未知子命令，可能遇到“忽略未知子命令而执行默认动作（同步、写库）”的实现。`entry.items.command` 的 `list` 调用之所以安全，是因为项目通过声明 `entry.items` 自己授权了这一调用；`verify` 套用同样的“自己声明才执行”。
- 只读契约：`verify` **不得**写权威、不得重建索引、不得联网（文档中以“必须”写明，AgentHelm 无法强制，只能写入契约与测试夹具）。

### 5.2 JSON 契约

```json
{
  "schema_version": "1.0",
  "ok": false,
  "authority": {"path": "ledger/", "records": 128},
  "index": {"path": ".cache/cases.sqlite", "records": 127},
  "diff": {"missing_in_index": 1, "missing_in_authority": 0, "stale": 0, "conflicts": 0},
  "rebuild_hint": "cases ledger-rebuild",
  "checked_at": "2026-11-11T08:00:00Z"
}
```

- 必需：`ok`（布尔）、`diff`（对象，值为非负整数）。其余可选，未知键忽略。
- 退出码：`0` = 一致（`ok: true`，`diff` 全为 0）；`1` = 有差异（`ok: false`，stdout 仍为合法 JSON）；其他 = 工具错误。AgentHelm 以 stdout JSON 为准，不依赖退出码区分“差异”与“一致”，仅在 JSON 无法解析时才看退出码。
- 计数语义：`missing_in_index` = 权威有而本机索引没有；`missing_in_authority` = 索引有而权威没有；`stale` = 两边都有但内容不一致；`conflicts` = 权威内部相互冲突的记录数（由项目的合并策略定义）。总差异 `Σ diff`。
- `rebuild_hint` 仅用于展示；AgentHelm 优先显示 Profile 的 `state.rebuild`，其次此字段，两者都不执行。

### 5.3 解释与缺省行为

- `ok: true`：`ok` 清单写“状态索引与权威一致”。
- `ok: false`：问题项 **`case-state:drift`**（`suggest`）：「本机索引与权威相差 {n} 项（缺失于索引 {a}，缺失于权威 {b}，过期 {c}，冲突 {d}）」，下一步显示重建命令（不执行）。
- 缺省（`verify` 未声明或为 `false`）：**不调用、不报错**，只在 `ok` 清单留一行“可选：项目工具实现 `state verify` 后，在 `work.case.state.verify` 声明为 `true` 即可由 health 核对索引与权威”。
- 已声明但失败：命令无法启动、非 JSON、缺必需字段、超时 → **不报错、不阻断 health**，写一条 `info`“状态校验未能完成：{原因}”，使用与 `_run_items_tool` 相同的错误包装（`EntryError` 风格），原因只含命令名与截断后的 stderr（≤ 200 字符，同 `project_real.py:231`）。

### 5.4 在开会话时调用的成本与超时

- 单次成本上界：`verify` 超时 `T_v`。建议常量 `STATE_VERIFY_TIMEOUT_SECONDS = 10`（与 `GIT_TIMEOUT`、`hooks.json` 的 hook `timeout: 10` 同量级；`items` 的 30 秒是读全量事项，`verify` 应更轻）。stdout 读取上限 64 KiB。
- `health` 内的成本：`health` 顺序执行各 `_check_*`，最坏增加 `T_v = 10 s`；`_check_memory` 已有 60 秒上限（`project_real.py:446`），整体仍可接受。
- 会话开始：`hooks.json` 里两个 SessionStart 命令各自 `timeout: 10`，且**目前都不运行 health**。若把 `verify` 放进 SessionStart，最坏会占满 hook 的全部预算，并在每个会话重复执行；两条 hook 是否并行**未确认**。因此本周期默认**不接入 SessionStart**；用户或 Agent 在会话中运行 `health` 时才调用（Skill 文本已有“进展/检查”入口）。若日后要接入，建议只读取上一次 `health` 缓存的结果（带时间戳、过期则丢弃），不在 hook 里实时执行——作为独立议题（D8）。

---

## 6. 与现有机制的关系

- **不改变** `entry.items` 的 `list --all --output json` 契约与超时。
- **不改变** 资产台账的缓存位置与“拒绝写进项目目录”的规则；本契约把“权威 / 派生”区分推广到项目自有工具。
- **不触碰** 项目的冲突合并策略；不提供 JSONL 账本实现（与 #65“不在范围内”一致）。
- 术语边界：此处“索引”指 Case 状态索引，与“资产台账索引”“记忆索引”（`memory.py index`）不同；文档中首次出现处加限定词。

---

## 7. 验收夹具方案

位置：`plugins/project-orchestrator/tests/test_case_state.py`（新增）；夹具函数在临时目录里 `git init`、写 Profile、按需写 `.gitignore`、`git add`，不依赖网络与用户 HOME（测试按 CI 方式在临时 HOME 下跑）。

**核心三例（对应 #65 验收建议）**

1. **SQLite 在 Git 忽略目录 → 报出。** `work.case.state.authority = "state/cases.sqlite"`，`.gitignore` 含 `state/`，文件存在且未跟踪：断言 `health` 的 findings 含 `case-state:ignored`（且 `case-state:binary`）。
2. **改为 Profile 声明的文本权威 → 不报。** `authority = "ledger/"`，目录下有 `machine-a.jsonl`，被 `git add`（或未忽略的未跟踪文件）：断言无任何 `case-state:*` finding，`ok` 清单含状态位置通过项。
3. **`verify` 缺省 → 只提示。** `state` 已声明但没有 `verify`：断言不执行任何子进程（用一个会在被调用时写标记文件的桩命令，断言标记文件不存在），无 finding，`ok` 清单有“可选”提示行。

**边界与回归**

4. 项目外：`authority` 为绝对路径、`../x`、`~/x` → `case-state:outside`；绝对路径指回项目内 → 不报。
5. 符号链接：指向项目外 → `case-state:symlink-outside`；指向项目内 → 不报；**经符号链接路径访问整个项目**（模拟 `/var → /private/var`）且权威正常 → 不报。
6. 已被忽略规则匹配但强制 `git add -f` 的权威 → 不报；权威在无 Git 仓库的目录 → 不报 `ignored`。
7. 权威路径不存在 → `info`；存在 iCloud 占位文件名 `.cases.jsonl.icloud` → 文案为“尚未下载”。
8. 私有路径内的 `.sqlite` 权威：不读取文件内容（用不可读权限或桩断言未调用 `open`），按扩展名判 `binary`。
9. `index` 与 `authority` 重叠 → `case-state:index-overlap`。
10. **`verify` 声明为 `true`：** 桩脚本输出 `ok: true` → 通过；`ok: false` 带 `diff` → `case-state:drift`，文案含各计数；非 JSON / 退出码 2 / 超时（测试把超时常量改小）/ 命令不存在 → 只有 `info`，`health` 整体不失败；桩验证命令行**不经 shell**（参数中含空格与 `;` 不被解释）。
11. **守卫与 Schema：** 跑 `tests/test_profile_reads_guard.py`（Schema 声明字段后通过）；`state.authority: 5` 触发 `agents-init` 的 Profile 形状检查（`profile-invalid`）；缺失字段不触发。
12. **文案：** zh / en 两种界面语言的 `msgid` 都在 `locale/en.json`，`test_i18n*.py` 通过。

本地验证口径：`git fetch --tags origin` 后 `AGENTHELM_REQUIRE_TAGS=1 HOME=$(mktemp -d) python3 -m unittest discover -s tests`，以及 `plugins/project-orchestrator/tests`、`plugins/agents-init/skills/agents-init/scripts` 的对应套件（实现 PR 的验证清单，本设计 PR 不需要）。

---

## 8. 实施拆分（issue 草稿）

**S1 文档：状态权威与索引契约（#65 第 1 条）**

- 范围：`case-workspaces.md` 增写 2.1 节；`SKILL.md:239` 附近加一句指向该节。
- 等级：project-orchestrator patch；`touches: []`；`default_for_existing: off`；无用户动作。
- 影响既有项目：否。依赖：无。`case-workspaces.md` 不在任何内容包 `sources` 内（project-orchestrator 的包来源仅 `templates`、`references/case.schema.json`、`references/asset-synonyms.example.json`、`sop-templates`，`content.json` 核对），不产生 `template_changes`。
- 验收：措辞与 2.1 一致；`case-workspaces.md` 内部锚点有效；不要求英文配对（已登记 deferred）。

**S2 Schema：声明 `work.case.state`（可选字段）（#65 第 2 条）**

- 范围：`project.schema.json` 为 `work.case` 增加 `state` 对象（`authority`、`index`、`rebuild`、`verify`，`additionalProperties: true`）并写字段说明；不改 `skeleton.json`、模板、内容包。
- 等级：agents-init patch；`touches: ["work.case.state"]`；`default_for_existing: off`；`template_changes: []`。
- 影响既有项目：否（可选字段）。依赖：无（但先于 S3 合入，S3 才能通过 `test_profile_reads_guard`）。
- 验收：`test_profile_reads_guard` 通过；`check-changes` 通过（`touches` 已声明）；不改任何内容包版本；既有 Profile 不出现新的 `profile-invalid`。

**S3 检测：`health` 的 Case 状态位置检查（#65 第 3 条）**

- 范围：`private_paths.py` 增补 Git 跟踪与符号链接辅助函数；`project_real.py` 增 `_check_case_state`；zh/en 文案；第 7 节 1–9、11、12 的测试。
- 等级：project-orchestrator minor（D3 可降 patch）；`default_for_existing: advisory`；无用户动作；`touches: ["work.case.state"]`。
- 影响既有项目：仅声明了 `work.case.state.authority` 的项目出现提示；其余无变化。依赖：S2。
- 验收：#65 验收第 1 条；不写任何项目文件；无 Git / 超时场景不误报；macOS 符号链接夹具通过。

**S4 verify 接口（可选，#65 第 4 条）**

- 范围：`work.case.state.verify` 的声明解析、`state verify` 调用与 JSON 校验、`case-state:drift` 与失败降级、测试 10；`case-workspaces.md` 增写契约小节（5.2）。
- 等级：project-orchestrator minor（与 S3 同一发布可合并）；`default_for_existing: advisory`。依赖：S2、S3。
- 验收：#65 验收第 2 条；未声明时零子进程；失败路径只出 `info`。

**S5 会话开始接线（另立项，6.14 后）**

- 范围：health 结果缓存与 SessionStart 读取（5.4）。等级 minor。依赖：S4。验收：hook 总耗时不增加（实测前后对比）；缓存过期丢弃。

建议 6.13.1 交付 S1 + S2（+ 视决策 S3）；S4、S5 视真实项目使用后再定。

---

## 9. 决策项（给维护者）

- **D1 `verify` 挂在哪个命令上。** 倾向：`work.case.command`（Case 状态属于 Case 工具）。各自对既有项目的影响：
  - 挂 `work.case.command`：既有项目没有任何变化，因为 AgentHelm 目前从不执行它；只有项目自己声明 `work.case.state.verify: true` 之后才会产生第一次调用。代价是引入一个“被 AgentHelm 执行”的新命令入口，需要 `verify: true` 开关与文档说明。
  - 挂 `entry.items.command`：已声明 `entry.items` 的项目（它们本来就被执行 `list`）一旦工具升级加了 `state verify`，无需任何开关就会被调用，既有项目的行为会在没有用户动作的情况下变化；未声明 `entry.items` 的项目（只有 `work.case.command`）则完全用不上。优点是复用现成的 30 秒超时与错误包装；缺点是语义混杂（事项列表 vs 状态校验）。
- **D2 契约表述是否允许“项目声明的同步方式”（同步盘）作为 Git 之外的合法同步。** 倾向：允许（2.1 已写），否则同步盘用户会被永久提示。替代：只认 Git，同步盘用户靠记录决定跳过。
- **D3 S3 的等级。** 倾向：minor（新增检测能力，值得进发布说明）。替代：patch（只在声明字段时出现提示，影响面小），若坚持 6.13.1 全 patch。S2（agents-init）倾向 patch。
- **D4 `verify` 是否“声明才调用”。** 倾向：是（5.1，避免对未知子命令的副作用）。替代：只要声明了 `work.case.command` 就探测一次，未实现则降级为提示（更符合 #65 原文字面，但执行从未授权过的命令，风险由项目工具承担）。
- **D5 是否跨插件复用同步盘识别。** 倾向：本版不做（`project-orchestrator` 不导入 `agent-pack`，文案用条件句 + 记录决定）。替代：让 agent-pack 的 `local_only` 结果提供“项目在同步盘”事实，在 `ignored` 时区分文案（更准确，但引入跨插件契约，需要接口版本管理）。
- **D6 Schema 声明后的形状错误（如 `authority: 5`）的报告级别。** 倾向：沿用现有 `profile-invalid`（`must`），与其他字段一致。替代：对 `work.case.state` 专门降为提示（与“新能力仅提示”一致，但要给 `scaffold` 开例外）。
- **D7 `work.case.command` 为字符串时的拆分规则。** 倾向：`shlex.split`（POSIX 规则）并在文档写明 Windows 需用数组形式。替代：字符串形式一律拒绝执行 `verify`，只接受数组。
- **D8 是否排期会话开始接线（S5）。** 倾向：不排期，先观察 `health` 手动调用的使用情况。替代：在 6.14 做缓存读取。
- **D9 是否把 `case-state:binary` 保留为 `info` 级。** 倾向：保留（契约第一条的直接反例）。替代：删除，避免对“权威是被 Git 跟踪的 SQLite、没有割裂”的项目噪音。

---

## 10. 未确认清单

- Git 对“文件型路径之下的探测路径”的 `check-ignore` 行为（4.2 第 4 步，实现时夹具验证）。
- 权威位于 Git 子模块或嵌套仓库内时 `git ls-files` 的行为。
- `.nosync`（iCloud）与 Dropbox / OneDrive 忽略属性是否可作为“确定不同步”的依据，缺官方文档依据。
- health“已记录决定”机制对新问题项 `id` / 指纹的具体约定（4.5）。
- 声明字段后，Schema 副本较旧的项目在 `agents-init` 额外字段报告中的类别与提示强度（3.3 做法 D 的描述）。
- 两条 SessionStart hook 是否并行执行，以及 `verify` 接入后的真实耗时。
- Windows 下 `work.case.command` 字符串的引号规则。
- #65 的接管项目（本机 SQLite + 账本）的细节不在本仓库，本文仅按 issue 转述。

## 11. 术语对照

- 权威（authority）：生命周期状态的唯一来源。
- 索引（index）：从权威派生、可丢弃并可重建的数据。
- Profile（Project Profile，项目配置，`.agents/moe.sakanano.agent-pack/project.json`）。
- 内容包（content pack）：`content.json` 中带版本与来源哈希的模板资产。
- 守卫（guard）：`tests/test_profile_reads_guard.py`，核对运行时代码读取的 Profile 键是否已在 Schema 声明。
- `touches` / `default_for_existing` / `template_changes`：变更片段中的发布声明字段，见 `release/VERSIONING.md`。
- iCloud 占位文件：尚未下载到本机的 `.<名>.icloud` 文件。
