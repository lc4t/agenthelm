# 渐进式加载设计（Issue #64）

- 状态：设计草案，待维护者决策（见第 6 节）。本文不改任何运行时代码、版本、片段或发布载体。
- 基线：`v6.13.0`（`dev` 与 `main` 同在 `bb84a08`）；目标周期 6.13.1（全 patch）。
- 读者：仓库维护者。术语首次出现标注中英对照，文末有术语表。

## 0. 结论

1. **第一层（分层约定）只新增文档，不改 `AGENT.template.md`。** 新增 `plugins/agents-init/references/layering.md`（及 `layering.en.md`），由 `agents-init` 的 `SKILL.md` 与 `AGENT.RULES.md` 之外的入口引用。按 `release/VERSIONING.md` 规则，这不匹配任何内容包来源，不产生 `template_changes`，既有项目无用户动作；插件等级 patch（可论证为 minor，见决策 D1）。模板现状已经是指针式入口（渲染后 `AGENTS.md` 中文 2,810 字节），不需要为遵守分层而改。
2. **第二层（检查工具指导处置）是本议题的核心价值。** 在 `memory.py check` 的 `startup-too-large` 问题上附加 `detail`（每文件字节数与占比、最大章节、可迁出候选），并让 `health` 的记忆项给出下一步。`project-orchestrator` minor，`default_for_existing=advisory`，无用户动作，`touches` 为空。字段为增量，现有消费者不受影响。
3. **第三层（Skill 瘦身）不进 6.13.1。** 只在本文给出候选拆分与测试不变量，另立 issue。理由：每个新增 `references/*.md` 都要登记中英配对或待翻译清单（`tests/test_i18n_policy.py:30-37`），工作量与风险和第一、二层不在一个量级。
4. **一处需要纠偏的描述：** #64 写“`check_budgets` 约 198–210 行”，当前为 `plugins/project-orchestrator/runtime/memory.py:209-221`；#64 的 opinion-manager 体积 11,914 是 `v6.11.0` 的值，当前为 12,554（见 1.1）。其余数字复核无误。

---

## 1. 现状核实

### 1.1 `SKILL.md` 体积（已执行并看到结果）

命令：`wc -c plugins/*/skills/*/SKILL.md`（工作树 `bb84a08`）。

- project-orchestrator：**20,335**（与 #64 一致）
- agents-init：**15,313**（一致）
- opinion-manager：**12,554**（#64 为 11,914；`git show v6.11.0:plugins/opinion-manager/skills/opinion-manager/SKILL.md | wc -c` = 11,914，6.11.0 之后由 `56b7bde`（#74）增长）
- agent-pack：**7,426**（一致）

最大章节（按 `## ` 切分，同一脚本统计）：

- project-orchestrator：§3 初始化或恢复状态 4,645；§2 分类当前请求 4,256；§1 发现项目契约 1,964。
- agents-init：「工作流程」9,535，占整篇 62%。
- opinion-manager：「候选收件箱与自动采集」2,954；「首次入口」2,646。
- agent-pack：最大章节「入口」1,744，已足够短。

### 1.2 `check_budgets` 现在报什么

`plugins/project-orchestrator/runtime/memory.py:209-221`：

- 触发条件：参数文件（policy）里 `startup_bundle.max_bytes` 与 `startup_bundle.files` 同时存在。
- 计算：`total = Σ stat().st_size（files 中存在的普通文件）+ 记忆索引字节数`；缺失文件**静默跳过**。
- 报告：一条 `{"code": "startup-too-large", "entry": "", "message": "启动层合计 N 字节，超过 M 字节"}`。没有文件、章节、占比、建议。
- `memory.py check` **只有 JSON 输出**（`run()` 中 `action == "check"` 分支直接 `json.dumps`，`--output` 只影响 `index`，见 `memory.py:359-361`）。文本形态只出现在 `health`（`project_real.py:437-458`），且只取第一条问题的 `message[:160]`。
- 相关约束：`confined()`（`memory.py:37-43`）拒绝符号链接与 `..`，路径违规直接 `fail()` 退出；第二层必须沿用同一解析，不能引入新的失败模式。
- 现有测试 `plugins/project-orchestrator/tests/test_memory.py:146-151` 只断言 `code` 集合与 `message`，增加 `detail` 键不会破坏它。

### 1.3 预算数值来源

- 工具侧**没有默认值**：`memory.py:328` 的帮助写明“未提供时不做体积与间隔检查”；`settings()`（`memory.py:60-72`）读取 Profile `memory.policy` 指向的项目文件，缺省为 `{}`。
- 模板侧也未设定：`AGENT.template.md:269`「参数（体积预算、复核间隔）：未设定」。
- #64 中接管项目的 `policy.json`“初始估计，没有历史数据支撑”：该项目文件不在本仓库，**未确认**（以 issue 转述为准）。
- 结论：数值归属项目，与 #64 验收第 4 条一致；工具只能提供依据与提示。

### 1.3.1 已有的“分层”相关表述（避免重复造概念）

- 指针原则已存在：`AGENT.template.md:264`「每类信息只有一个权威位置，其他位置只留指针」；`AGENT.template.md:234`「保持入口简短……禁止在任一入口文件复制完整生命周期工作流」（该句位于模板文件的说明部分，不随 `AGENTS.md` 渲染）。
- 术语冲突：仓库里已有“分层加载”指 **OPINION.md 规则**的按需读取（`AGENT.template.md:146`、`:483-491`，`opinion.loading_mode`，由 opinion-manager 负责）。本设计的三层是**项目文件与 Skill 的体积分层**，文档里统一称“渐进式加载（progressive disclosure）三层”，不使用“分层加载”，避免与 Opinion 机制混淆。
- SOP 已是范例：先读索引、命中后只读一个正文（`project-orchestrator/skills/project-orchestrator/SKILL.md` §3.1；`references/sop.md`）。

### 1.4 `agents-init` 模板体积与结构

命令：在临时 HOME 下 `init_project.py --project <空目录> --name Demo --project-type software --vcs git --stack python --runtime python3 --agent-cli claude --language zh|en --apply`，随后 `wc -c`。

- 渲染后文件：`AGENTS.md` zh **2,810** / en **3,117**；`AGENT.RULES.md` zh 1,643 / en 1,843；`CLAUDE.md` 128 / 137；`.agent-doc/chat-summary.md` 332 / 400。启动层合计（四个文件）zh 4,913 / en 5,497，远低于任何合理预算。
- `AGENTS.md` 章节（zh）：Project Profile、Session 入口、职责边界、人用入口、隐私约定、项目专属入口（共 6 个 `##`），全部已是指针式内容。
- 受管区块（managed region，`plugins/agents-init/skills/agents-init/scripts/regions.py` 的 `REGION_RE`）：**不在新项目渲染结果里**；只在“接管已有 `AGENTS.md` / `CLAUDE.md`”时由 `--append` / `--claude-policy add-import` 追加（`SKILL.md:86-89`）。区块正文来自模板的 `region/agents-contract`、`region/privacy-contract` 等，区块内被用户改过时升级不覆盖。
- 内容包：`plugins/agents-init/content.json` 的 `agents-init/project-contract`（1.4.0）`sources` 为 `skills/agents-init/AGENT.template.md` 与 `skills/agents-init/launcher.sh`，按**整个文件**计 SHA-256（`files` 字段）。PR 门禁同样强制：改动内容包来源而没有匹配的 `template_changes` 会失败（`scripts/release_workflow.py:190-196`）。因此**哪怕只改模板文件里不渲染的说明段落**，`check-release` 也会报 `content pack(s) … changed; list the files in template_changes`（`scripts/release_meta.py:376-380` 的 `changed_packs` 检查）。

### 1.5 会话开始注入的协议文本

两条独立的 SessionStart 通道（`plugins/agent-pack/hooks/hooks.json`）：

1. `agenthelm.py check --hook --hook-format claude-hook`：更新与本机专属目录提示，静默优先（`agenthelm.py:1552-1570`）；**不运行 health，也不运行 memory check**。
2. `agenthelm.py nudge hook --event session-start`：注入 opinion-manager 的收件箱 `brief`（候选采集协议）与提醒。

实测（临时 HOME + 新建项目，无 `.opinion/inbox.json`）：向模型注入的 `additionalContext` 在 zh **1,956 字节**、en **2,224 字节**（命令：向 `nudge hook --event session-start` 的 stdin 送 `{"source":"startup","session_id":"s1","cwd":"<项目>"}`，`AGENTHELM_LANG=zh|en`）。含已有候选的项目会更长，**该上限未实测**，标「未确认」。结论：会话开始的注入已经很小（≈2 KB），#64 第 3 条中“只注入索引与指针”无收益空间，可从范围中撤出（决策 D6）。

---

## 2. 第一层：分层约定（文档）

### 2.1 三层定义

- **常驻层（resident）**：每个会话开始都读。内容只限身份、硬性边界（隐私、授权）、指针与索引。带字节预算，预算数值由项目决定。典型文件：`AGENTS.md`、`AGENT.RULES.md`、记忆索引、`.agent-doc/chat-summary.md` 的固定小节。
- **触发层（triggered）**：命中某个场景才读，**一个场景一个文件**，文件头部写触发条件。典型：SOP、运行手册、专题规则束、子系统说明。
- **查证层（verify-on-demand）**：历史、证据、归档、原始资料，只在核实某个结论时读；不得被常驻层的文字要求“每次读取”。

### 2.2 “内容放哪一层”对照

- 身份、一句话定位、硬性边界、授权规则 → 常驻层（`AGENTS.md` / `AGENT.RULES.md`）。
- 命令清单、测试/构建入口 → 常驻层，**每项一行**；多步骤的操作手册 → 触发层（运行手册，`AGENTS.md` 留一行指针与触发条件）。
- 子系统细则、模块约定 → 触发层（`docs/` 下按子系统一个文件）；`AGENTS.md` 只留“改动 X 子系统前读 Y”。
- 凭据位置与使用说明 → 触发层，且路径落在 `privacy.forbidden_default_reads` 声明内；`AGENTS.md` 不写凭据值，只写“凭据说明见 Z，读前需用户确认”。
- 迁移说明、版本升级说明 → 触发层（升级时才读）。
- 决策历史、会话流水、检查证据 → 查证层（`.agent-doc/`、Case 的决策与工作记录、`archive/`）。
- 具体数值、日期、版本号 → 放到唯一的权威位置（Profile、配置文件、Case），常驻层不复述；需要提及时用指针。

判定口诀（写进文档的单句规则）：**读完这一行，Agent 在“不命中任何场景”的会话里是否会改变行为？不会，就迁出到触发层或查证层，原处留一行指针。**

### 2.3 放置位置与引用方式

- 推荐路径：`plugins/agents-init/references/layering.md`，英文配对 `layering.en.md`（`agent-pack`、`opinion-manager` 的 `references` 均在插件根目录，`agents-init` 目前没有该目录，是新增）。
- 引用方式：在 `plugins/agents-init/skills/agents-init/SKILL.md`「输出契约」附近加一句相对链接 `[分层约定](../../references/layering.md)`，与 `project-orchestrator` 引用 `../../references/case-workspaces.md` 的写法一致（`project-orchestrator/skills/project-orchestrator/SKILL.md:84`）。
- 替代位置：`plugins/project-orchestrator/references/layering.md`。理由是“编辑 AGENTS.md 迁出细则”发生在项目运行期，而 `project-orchestrator` 是运行期的唯一生命周期 Skill，且第二层的检查输出也出自它。代价是 `project-orchestrator/SKILL.md` 已 20,335 字节，需再加一行链接。**倾向 `agents-init`**（与 issue 建议一致，约定属于“项目契约”职责，`AGENT.template.md:5-14`）；第二层的提示文案里直接写 `project-orchestrator` 的文档链接时也可跨插件引用路径文字，不引入代码依赖。**需要维护者拍板（D2）**。
- 门禁：该路径匹配 `tests/test_i18n_policy.py:32` 的 `plugins/*/references/**/*.md`，必须在 `tests/i18n-policy.json` 的 `pairs` 登记中英对，两文互相用相对 Markdown 链接指向（`docs/i18n-policy.md` “文档配对门禁”）。英文稿的人工审校仍待完成，文中不得声称已审校。

### 2.4 是否改动 `AGENTS.md` 模板或受管区块：逐条论证

依据 `release/VERSIONING.md` 的声明规则与实现 `scripts/release_model.py:150-153`、`scripts/release_meta.py:218-250`：

- 内容包等级只由匹配其 `sources` 的 `template_changes` 决定：`semantic → minor`，`cosmetic → patch`，无匹配保持不变。
- 声明校验：`template_changes` 非空时 `touches` 必须非空；**语义模板变化且 `default_for_existing != off` 时必须有 `user_action_required`**。

**方案 A（推荐）：只新增 `references/layering*.md`，改 `SKILL.md` 一行引用，不触及模板**

- `template_changes`：`[]`；内容包 `project-contract` 保持 1.4.0；`launcher.sh` 不动。
- 插件等级：`agents-init` patch（兼容的小改进；若维护者认为“新增一份对外约定”值得发布说明，可调 minor，决策 D1）。
- `touches`：`[]`（没有改动用户项目内的路径或 Profile 字段）；`user_action_required`：`[]`；`default_for_existing`：`off`。
- 对既有项目：**没有用户动作**，没有文件被重新渲染，`agenthelm refresh` 看不到差异。
- 代价：既有项目的 `AGENTS.md` 不会出现指向新文档的链接，发现途径只剩 Skill 加载与第二层的检查提示（第二层的 `suggestion` 文案会带文档路径）。

**方案 B：同时在 `AGENT.template.md` 加一行指针（例如在“项目专属入口”下写“分层约定见 …”）**

- 影响面：该文件整体哈希变化 → 必须声明 `template_changes: [{"path":"skills/agents-init/AGENT.template.md","class":…}]`，`touches` 非空（如 `["AGENTS.md"]`）。
- 若判为 `semantic`（新增了 Agent 行为指引）：内容包 1.4.0 → **1.5.0**；`default_for_existing` 为 `advisory` 或 `on` 时必须写 `user_action_required`（例如“运行 `agenthelm refresh`，逐文件选择 `--keep` / `--append` / `--accept`”，`session: "new"`）；若 `default_for_existing: off` 则免用户动作，但等于新项目生效、既有项目不生效，与“让既有项目受益”的目的矛盾。
- 若判为 `cosmetic`：内容包 patch，仍须 `touches`，既有项目 `refresh` 时被用户改过的 `AGENTS.md` 仍要逐文件选择（`agents-init/SKILL.md:102`，没有基线时所有与新模板不同的文件都算“你改过”），实际用户成本不低于 semantic。
- 隐含风险：`AGENTS.md` 常驻层每个会话多读一行；受管区块（接管项目的 `agents-contract`）若同步改动，区块指纹变化，被用户改过的区块会触发“区块内被改过时升级不覆盖”的逐个确认流程。

**推荐 A 的论证与代价**

- 目的是“让工具和文档能帮项目守住界线”，而模板本身已经是指针式入口（1.4），改模板没有体积收益，只有发现性收益。
- 方案 A 在既有项目上没有用户动作，符合本周期“全 patch”的约束；方案 B 至少要内容包 minor 或带 `touches` 的 patch，与 6.13.1 目标冲突。
- 发现性缺口由第二层补：超限时 `suggestion` 直接指向文档；`agents-init` Skill 被触发时同样可见。若日后评估认为模板需要指针，再作为独立的 6.14 议题（minor + 用户动作），不与本周期绑定。
- 验收第 1 条“`AGENTS.md` 的模板与区块遵守它”：用**测试**证明，而非改模板——渲染 zh/en 的 `AGENTS.md`，断言字节数不超过上限（建议 4,096，当前 3,117 最大）且每个 `##` 小节行数不超过 N；测试不触发版本变更（“测试与 CI 配置本身不要求插件升版”）。

---

## 3. 第二层：检查工具指导处置

### 3.1 范围与边界

- 位置：`plugins/project-orchestrator/runtime/memory.py`，仅在 `startup-too-large` 触发（总量超过 `startup_bundle.max_bytes`）时计算 `detail`，未超限时不增加任何开销与输出。
- 只读：只读取 `startup_bundle.files` 列出的文件（已有行为），外加用于“重复出现”判断的项目内 Markdown（见 3.4，受私有路径约束）；**不写任何文件**，不自动改写（与 #64 “只提示”及现有预览口径一致）。
- 不决定预算数值；不做压缩、摘要等有损处理。

### 3.2 `detail` 结构（JSON，增量字段）

沿用原问题的 `code` / `entry` / `message`，新增可选键 `detail`。字段如下：

```json
{
  "code": "startup-too-large",
  "entry": "",
  "message": "启动层合计 73156 字节，超过 70000 字节",
  "detail": {
    "total_bytes": 73156,
    "max_bytes": 70000,
    "over_bytes": 3156,
    "over_percent": 4.5,
    "files": [
      {"path": "AGENTS.md", "bytes": 31200, "percent": 42.6},
      {"path": "AGENT.RULES.md", "bytes": 12100, "percent": 16.5},
      {"path": "(记忆索引，生成)", "bytes": 3900, "percent": 5.3, "splittable": false},
      {"path": "docs/gone.md", "bytes": 0, "percent": 0.0, "missing": true}
    ],
    "sections": [
      {"file": "AGENTS.md", "heading": "## 子系统 A 细则", "level": 2, "line": 120,
       "bytes": 6100, "percent": 8.3}
    ],
    "candidates": [
      {"file": "AGENTS.md", "heading": "## 子系统 A 细则", "line": 120, "bytes": 6100,
       "rule": "duplicated-elsewhere", "evidence": "docs/subsystem-a.md 含相同的 7 行连续文本",
       "reclaim_bytes": 5980,
       "suggestion": "把本节迁到 docs/subsystem-a.md 已有位置，原处保留一行指针（约 120 字节）"}
    ],
    "reclaim_covers_over": true,
    "layering_doc": "plugins/agents-init/references/layering.md"
  }
}
```

定义（可复算）：

- `percent = bytes / total_bytes × 100`，保留 1 位小数；`over_percent = over_bytes / max_bytes × 100`（#64 例：3,156 / 70,000 = 4.51%）。
- `bytes` 按 **UTF-8 字节**计，不是字符数：对每个文件读取 `read_bytes()`，不经过 `str` 换行归一，因此各章节 `bytes` 之和**恒等于**该文件 `stat().st_size`（测试不变量）。与既有总量口径一致（`memory.py:219` 用 `st_size`）。
- `reclaim_bytes = section.bytes − POINTER_BYTES`，`POINTER_BYTES` 为指针行的估计值（常量，初值 120，指针行形如“细则见 `docs/x.md`，改动该子系统前阅读”）；仅用于估算，写入输出说明其为估计。
- `reclaim_covers_over`：`Σ 候选.reclaim_bytes ≥ over_bytes`，回答“按候选迁完能否压回预算”。
- `files` 按 `bytes` 降序；`sections` 取全体启动文件中最大的 5 节；`candidates` 取至多 5 条，按 `reclaim_bytes` 降序。

### 3.3 按标题切分的规则（不误报的边界）

- 只识别 ATX 标题（行首 1–6 个 `#` 后接空白）。Setext 标题（下划线式）不识别，文档中注明（用于启动文件的项目极少使用）。
- **围栏代码块内的 `#` 行不是标题**：遇到以 ```` ``` ```` 或 `~~~` 开头的围栏行开始跟踪，遇到同字符且不短于开围栏长度的行结束；`#` 开头的 shell 注释、Markdown 示例因此不会被切开。未闭合围栏视为延续到文件末尾。
- 开头的 YAML Front Matter（首行 `---` 到下一个 `---`）整体并入“文件开头”小节；HTML 注释内的 `#` 行不作特殊处理（仅在注释以围栏方式出现时受围栏规则保护），此为已知误差，写入文档。
- 切分粒度：以 `level ≤ 2` 的标题开一节，`###` 及更深并入父节；若某个 `##` 节超过其文件的 50%，再把它的 `###` 子节单独列出（避免“一个大节”无法定位）。
- 无标题文件：整体一节 `(无标题)`，**不生成候选**，建议写为“该文件没有标题，无法按章节建议迁出，可手动拆分”。
- 文件开头到第一个标题之间的内容为 `(文件开头)` 节，不生成候选（身份与边界通常在此）。
- 非 UTF-8 文件：按字节计数，标题检测对每行 `decode("utf-8", errors="replace")`；字节和不变量不受影响。CRLF 保持原字节。

### 3.4 “可迁出候选”判定（保守优先，宁漏勿误）

只对 `AGENTS.md`/`AGENT.RULES.md`（及项目在 `startup_bundle.files` 里列出的其他 Markdown）生成候选，且**同时满足全部前置条件**：

1. 节字节数 ≥ 1,024（太小的节迁出收益低于指针成本）。
2. **不在受保护集合内**：标题匹配（中英）`Project Profile`、`Session 入口`/`Session entry`、`职责边界`/`Responsibilities`、`隐私约定`/`Privacy convention`、`项目专属入口`；节落在 `<!-- agenthelm:region … -->` 受管区块内（复用 `regions.py` 的 `REGION_RE` 判定；因 `project-orchestrator` 不应 import `agents-init` 脚本，实现时在 `memory.py` 内复制同一正则并在测试里与 `regions.py` 比对，参照 `tests/test_private_paths_sync.py` 的做法）。
3. 不在私有路径内、也不引用私有路径：节文本命中 `private_paths.mentions(text, private_paths.declared(project))` 的不生成“内容比对”证据，且比对时**不读取**私有路径下的任何文件（遵守 `privacy.forbidden_default_reads`）。

在此之上按规则出证据，每条候选必须带 `rule` 与 `evidence`：

- `duplicated-elsewhere`（高置信）：该节规范化文本（去空白、去标题行）中，存在 ≥ 3 行连续、每行 ≥ 20 字节的内容，**逐字**出现在另一个项目内的非启动、非私有、非忽略（`git ls-files` 或存在于工作树）Markdown 文件里。扫描范围限定 `docs/`、`.agent-doc/`、`cases/`、项目声明的 `entry.where` 路径，单文件 ≤ 256 KiB、总扫描文件 ≤ 200、总耗时 ≤ 2 秒，超过则**停止并标注** `scan_truncated`，不猜测。
- `already-pointed`（提示）：该节在别处已有同名文件（标题与某 `docs/*.md` 文件名/标题一致），建议合并为指针——比 `duplicated-elsewhere` 弱，只在无逐字重复时给出。
- `detail-dense`（低置信，默认不进 `candidates`，仅进 `sections` 的标注 `hint`）：#64 要求的“含具体数值或日期的细则”，用“日期（`\d{4}-\d{2}-\d{2}`）或带单位的数值”出现次数 ≥ 8 且占节字节 ≥ 15% 判定。该启发式在“版本表、端口表”上会误报，故默认关闭，是否启用见决策 D5。
- 不会成为候选：已经是纯指针的节（节字节 < 1,024 已排除）、无证据的大节（只在 `sections` 列出体积，让人自行判断）。

### 3.5 输出样例

**JSON**：见 3.2。`check` 的退出码不变（有问题为 1）；`ok`、`entries`、`problems[]` 的既有键位不动。

**文本**（`health` 的记忆项与新增的 `memory.py check --output text`，决策 D4）：

```
启动层合计 73,156 字节，超过预算 70,000 字节（超出 3,156，4.5%）
  文件体积
    AGENTS.md           31,200  42.6%
    AGENT.RULES.md      12,100  16.5%
    （记忆索引，生成）    3,900   5.3%
  最大章节
    AGENTS.md  第 120 行  ## 子系统 A 细则        6,100  8.3%
    AGENTS.md  第 310 行  ## 凭据与迁移说明        4,300  5.9%
  可迁出候选（按可回收体积排序，已迁完可回收 8,900，足以压回预算）
    1. AGENTS.md「## 子系统 A 细则」：docs/subsystem-a.md 已含相同的 7 行连续文本
       建议：迁到该文档，原处保留一行指针（约 120 字节）
  分层约定：plugins/agents-init/references/layering.md
  本命令不会修改任何文件。
```

`health` 现有结构把 `reason` 截为 160 字符（`project_real.py:454-456`）。方案：仍给出一条 `memory` 问题，`reason` 变为“启动层超出预算 3,156 字节（4.5%），最大的章节是 AGENTS.md「## 子系统 A 细则」6,100 字节”，`next` 给出“运行 `memory.py check` 查看每个文件与候选”；完整分解只在 `check` 输出。

### 3.6 与现有 `startup-too-large` 的兼容

- `code`/`entry`/`message` 文本不变，`detail` 为新增键；`problems` 数组顺序不变（该问题仍位于 `check_budgets` 返回的相同位置）。
- 现有 `test_memory.py` 仅比较 `code` 集合与 `message`，不需要修改（已核对 146-151 行）。
- `detail` 计算失败（读文件错误、扫描超时）时**不得改变原问题的存在性**：退回仅 `message`，并在 `detail` 写 `{"error": "…"}` 或省略；`check` 不因 `detail` 失败而崩溃。
- 文件缺失沿用“跳过”语义，但在 `files[]` 以 `missing: true, bytes: 0` 列出，便于发现策略里写错路径（现行行为是静默跳过，属于信息增量而非行为变化）。

### 3.7 等级与声明

- `project-orchestrator`：**minor**（新增能力，值得进发布说明；可归 patch 的“小改进”边界见 `VERSIONING.md` 与严格 SemVer 的差异说明，决策 D3）。
- `actions: []`，`user_action_required: []`，`touches: []`，`default_for_existing: "advisory"`，`template_changes: []`，`contracts: []`。理由：输出只增不减，不改变任何项目文件，不要求任何动作；`advisory` 表示既有项目升级后自动出现更详细的提示。
- 不涉及 `project.schema.json` 或 `skeleton.json`（不新增 Profile 读取：`startup_bundle` 在项目 policy 文件中，不在 Profile；候选扫描读取的 `privacy.forbidden_default_reads` 与 `entry.where` 已被 `private_paths`/`project_real` 读取，`tests/test_profile_reads_guard.py` 不受影响——实现时仍需跑该守卫）。

### 3.8 测试与夹具方案

位置：`plugins/project-orchestrator/tests/test_memory_startup_detail.py`（新增，沿用 `test_memory.py` 的 `run_tool` 夹具）。

- **字节和不变量**：多文件夹具，断言 `Σ sections.bytes == st_size`，`Σ files.bytes + index == total_bytes`。
- **多字节**：全中文标题与正文（如 100 个汉字 = 300 字节）、混合 emoji（4 字节）、组合字符；断言 `bytes` 等于 `len(text.encode("utf-8"))` 而非字符数；预算边界取 `max_bytes = total` 与 `total − 1`，验证“等于不报、多 1 字节才报”（现有比较为 `total > max`）。
- **标题缺失**：无任何 `#`、只有文件开头、仅围栏内 `#`；断言单节 `(无标题)`、无候选、建议文案含“没有标题”。
- **代码块内的 `#`**：围栏内 `# comment`、`## fake` 不切节；`~~~` 围栏；未闭合围栏；围栏嵌套长度（```` ```` ```` 内含 ```` ``` ````）。
- **CRLF 与非 UTF-8**：CRLF 文件字节和不变量；含 `\xff` 的文件不崩溃。
- **候选**：`duplicated-elsewhere` 命中（夹具 `docs/x.md` 含相同 4 行）；同样的文本但位于私有目录 → 不读、不出候选；受保护标题与受管区块 → 不出候选；节 < 1 KiB → 不出候选；`scan_truncated`（用小限额夹具触发）。
- **兼容**：既有 `test_project_policy_sets_budgets_and_summary_sections` 不修改仍通过；`detail` 缺失时 `health` 退回旧文案。
- **健康项**：`test_project_real.py` 增加一例：超限夹具的 `health` 记忆项 `reason` 含最大章节名。

---

## 4. 第三层：随包 Skill 瘦身（仅清单与不变量，建议另立项）

### 4.1 候选拆分清单（体积来自 1.1）

- `agents-init/SKILL.md` 15,313：「工作流程」9,535（62%）按子流程（初始化、接管既有、内容刷新/合并、Case 迁移）拆 3–4 个 `references/`，主文件保留触发条件、命令骨架与安全规则；预计主文件降至 ≈7 KB。
- `project-orchestrator/SKILL.md` 20,335：§3「初始化或恢复状态」4,645 中的 SOP（§3.1）与私有目录（§3.2）已有 `references/sop.md`，可再把详细条款迁出；§2「分类当前请求」4,256 中 Case 模式与既有 Task/Case 模式各自迁出；预计主文件 ≈13 KB。
- `opinion-manager/SKILL.md` 12,554：「候选收件箱与自动采集」2,954、「首次入口」2,646 迁出（`references/` 已有 `onboarding.md`、`inbox.md`）。
- `agent-pack/SKILL.md` 7,426：暂不拆。

这些估计是按章节体积的**推算**，未实际拆分验证，标「未确认」。

### 4.2 测试不变量

新增 `tests/test_skill_size_budget.py`，数据文件 `tests/skill-size-budget.json`（`{"<SKILL.md 相对路径>": <字节上限>}`）：

- **体积上限棘轮**：初值设为当前体积（20,335 / 15,313 / 12,554 / 7,426），之后只允许下调；增长必须同时修改清单并在 PR 说明，使“体积回升”可审阅。拆分 PR 把上限降到拆分后的值。
- **引用存在**：扫描每个 `SKILL.md` 的相对 Markdown 链接（`](../../references/x.md#anchor)`），目标文件必须存在，锚点必须在目标里能找到对应标题（按 GitHub slug 规则；复杂中文锚点生成规则未验证，先只检查文件存在，锚点检查标「未确认」）。目前 `grep` 未发现现成的“链接存在性”或“体积上限”测试。
- **i18n 配对联动**：每个新增 `references/*.md` 必须在 `tests/i18n-policy.json` 登记（配对或以具体路径和理由列入 `deferred`），否则 `test_public_docs_are_paired_or_explicitly_deferred` 失败。

### 4.3 评估：本周期之外单独立项

理由：(a) 拆分改变 Skill 触发后的加载行为，应有真实会话验证“拆分后任务完成度不下降”，仓库内没有此类评测，**未确认**可用的评测方法；(b) 每个拆分都伴随中英文 references 与门禁登记，等级至少 patch，涉及 SKILL.md 语义的重排建议 minor；(c) 第二层完成后才有“超限时指向分层约定”的闭环，先后顺序自然。建议 6.14 以后，由第 5 节 I4 承接。

---

## 5. 实施拆分（issue 草稿）

**I1 文档：渐进式加载分层约定（#64 第一层）**

- 范围：新增 `plugins/agents-init/references/layering.md` 与 `layering.en.md`；`agents-init/SKILL.md` 加一行链接；`tests/i18n-policy.json` 登记配对；新增 `AGENTS.md` 渲染体积与小节行数的守卫测试（见 2.4）。
- 等级：agents-init patch；`touches: []`；`default_for_existing: off`；无 `template_changes`。
- 影响既有项目：否。依赖：无。
- 验收：对照表与三层定义齐全；中英互链且通过 `test_i18n_policy`；守卫测试在 zh/en 渲染下通过；文档明示“分层加载（Opinion）”与本约定的区别；英文稿标注“未经母语者审校”。

**I2 检查工具：启动层超限时输出体积分解与迁出候选（#64 第二层）**

- 范围：`memory.py` 的 `startup-too-large` 增加 `detail`；`health` 记忆项文案；可选 `check --output text`（D4）；测试按 3.8；`references` 中补一小节（如 `references/case-workspaces.md` 旁的记忆说明，位置需确认，**未确认**现有记忆说明文档路径）。
- 等级：project-orchestrator minor；`default_for_existing: advisory`；无用户动作。
- 影响既有项目：仅在超限时多出提示，无写入。依赖：I1（`layering_doc` 指向的文档需先存在，或先输出占位路径并在 I1 合入后更新）。
- 验收：#64 验收第 2、4 条；3.8 全部用例通过；既有 `test_memory.py` 不改动仍通过；输出不含私有路径内容。

**I3 候选判定增强（可选）：`detail-dense` 与 `already-pointed`**

- 范围：低置信规则，默认关闭，开关位置见 D5。等级 patch。依赖：I2。验收：误报率在夹具与至少一个真实项目的试跑上有记录（试跑需维护者提供项目，**未确认**可行）。

**I4 Skill 瘦身（#64 第三层，6.14 后）**

- 范围：按 4.1 逐个 Skill 拆分一个 PR；`tests/test_skill_size_budget.py` 与 `tests/skill-size-budget.json`。
- 等级：每个被拆插件 patch/minor（见 D3）；`touches: []`；`default_for_existing: off`。依赖：I1。验收：#64 验收第 3 条；每个 PR 有“拆分前后行为对照”的人工核对记录。

**I5（可并入 I2）：把 `memory.py check` 的体积计算抽成可复用函数**，供第二层与将来的 `health` 复用，避免两处各算一次。等级随 I2。

---

## 6. 决策项（给维护者）

- **D1 第一层的发布等级。** 倾向：agents-init **patch**（`template_changes: []`，`default_for_existing: off`）。替代：minor，理由是新增对外约定文档值得进发布说明。影响：6.13.1 是否仍为“全 patch”。
- **D2 分层约定文档放哪个插件。** 倾向：`plugins/agents-init/references/`（与 issue 一致，属项目契约）。替代：`plugins/project-orchestrator/references/`（运行期更常被读到，且第二层同属该插件）。
- **D3 第二层等级：minor 还是 patch。** 倾向：project-orchestrator **minor**（新增“指导处置”能力）。替代：patch（输出增量、无新命令/字段，可归“小改进”）。若坚持 6.13.1 全 patch，则选 patch，I2 仍可在本周期交付。
- **D4 是否为 `memory.py check` 新增 `--output text`。** 倾向：新增（当前只有 JSON，#64 的“列出每个文件与章节”对人用不友好；`index` 子命令已有同名选项可参照）。替代：只在 `health` 输出文本，`check` 保持纯 JSON，不新增 CLI 面。
- **D5 `detail-dense`（含数值日期的细则）是否默认启用。** 倾向：默认关闭，只作 `sections` 上的 `hint`，因为在版本表、端口表上误报概率高；替代：默认作为低置信候选展示并标 `confidence: low`。
- **D6 #64 第三层中“会话开始注入只发索引与指针”是否撤出范围。** 倾向：撤出，实测注入仅约 1.9–2.2 KB（1.5）。替代：保留，待含大量候选的真实项目实测注入长度后再判（该数据**未确认**）。
- **D7 是否在 6.13.1 就启用 `memory.py check` 的候选扫描 `duplicated-elsewhere`。** 倾向：启用，限额与私有路径保护写死（3.4）；替代：6.13.1 只交付 `files` 与 `sections`（零扫描风险），候选放 I3。
- **D8 第一层是否同时给既有项目加指针（方案 B）。** 倾向：不加（2.4）；替代：作为 6.14 的 minor + 用户动作议题。

---

## 7. 未确认清单

- #64 中接管项目的 `policy.json` 内容与“初始估计”描述（项目不在本仓库）。
- 含大量候选的项目其会话开始注入的最大长度（只实测了无收件箱候选的新项目：zh 1,956 / en 2,224 字节）。
- 第三层各拆分后的主文件体积（按章节体积推算，未实际拆分）。
- 英文文档的人工审校状态（见 `docs/i18n-policy.md`，目前没有审校记录）。
- GitHub 风格锚点对中文标题的 slug 规则，及基于此的锚点检查可行性。
- 当前仓库是否存在记忆检查说明的现成 references 页面（实现 I2 时再定位文档落点）。

## 8. 术语对照

- 渐进式加载（progressive disclosure）：按需分层读取内容，常驻层最小。
- 受管区块（managed region）：工具拥有、带指纹标记的文本块，区块外归用户。
- SOP（Standard Operating Procedure，标准作业流程）。
- Profile（Project Profile，项目配置，`.agents/moe.sakanano.agent-pack/project.json`）。
- 内容包（content pack）：`content.json` 中带版本与来源文件哈希的模板资产。
- `template_changes` / `default_for_existing` / `touches`：变更片段中的发布声明字段，见 `release/VERSIONING.md`。
