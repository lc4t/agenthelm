# 公开规则的渐进式加载设计（Issue #64，6.14.0）

- 状态：设计草案，待维护者决策（第 8 节）。本文只新增文档与一个发布片段，不改任何运行时代码、版本、catalog 内容或发布载体。
- 基线：`origin/dev` 的 `e50424a`；数据来自分支 `claude/catalog-content-614`（PR #151，`1e41ae5`）里 catalog 1.1.0 的真实规则与模板，在独立 worktree 中用 `versioned.render_rule`、`context.core_text`、`context.bundle_text` 的真实函数计算。
- 读者：仓库维护者。术语首次出现标注中英对照，文末有术语对照表。
- 与 `docs/design/progressive-loading.md` 的分工：那篇处理项目入口文件与 Skill 的体积分层；本文只处理 `OPINION.md` 里公开规则的读取分层。

---

## 0. 结论摘要

1. **采用“单一 `OPINION.md` + 读取时切片”（方案 a）加“规则级成员、规则组读取”（方案 c 的收敛版）。** 同意维护者的倾向，只有一处细化：数据模型的成员关系精确到规则编号，读取单元取“规则组”。逐条规则各占一条索引（纯 c）时，索引在 zh 下为 3,270–3,715 B，规则组的索引目标值为 600 B（4 组，每组 150 B），逐条索引抵消了大部分收益。
2. **否决分文件存储（方案 b）。** 它把锁、人工修改检测、`--accept-current`、原子写、`inbox` 的行号引用、所有指向 `OPINION.md` 的读取说明一并改写；旧 Agent 读到只剩索引的 `OPINION.md` 会静默欠读，`fallback` 也不再等于完整读取。
3. **否决拆分场景模板为多个系列（方案 d）。** 它改变模板身份，使 `scenario/reviewed-project@1.0.0` 等既有引用无法一对一迁移，并把“每个系列只选一个变体”的规则放大为组合问题。
4. **声明来源：模板可选携带 `groups`，选择模板时物化为 Profile 的 `loading.groups`。** Profile 仍是用户确认的唯一声明处，`--loading-file` 仍可整体替换。物化后的 Profile 使用 `schema_version` 2.2；2.0、2.1 的指纹、渲染字节、锁格式保持不变。旧工具读 2.2 Profile 或含 `groups` 的模板会报错（实测报错文字见 5.2）。
5. **体积（真实数据，zh，`scenario/reviewed-project`）：** 完整 `OPINION.md` 21,225 B。推荐分组（10 条常驻 + 4 个按需组）下，`core` 输出（含索引）目标值 8,248 B，典型任务读取量（`core` + 一个按需组的平均值）11,682 B，较现状 **减少 45%**；最轻任务 9,921 B（−53%），最重单组任务 12,760 B（−40%）。读满 4 个组时总读取量比现状多 761 B（框架与索引的固定开销）。收益有上限：常驻集合是下限，不能降到 5 KB 以下。
6. **可靠性：** 锁校验、`fallback`、切片与完整正文逐字一致、`context check` 对“已声明信号”的缺失检测可以硬保证；Agent 是否在命中触发条件时去读、是否如实声明信号，只能靠指令与事后记账（`routing_miss`），属于软约束。会话开始注入 `context core` 本版不做，原因与替代见 4.4。
7. **实施拆为 4 个子任务（第 6 节）。** 最小可发布集合 M1 是 T1（schema 与切分引擎）、T2（模板 `groups` 与预览物化）、T3（记账与文档）和 T4 中的体积守卫。时间紧时先落 M0：T1 加守卫框架，用户用 `--loading-file` 手写规则组，catalog 暂不带默认声明。
8. **发布等级（实施 PR，非本 PR）：** `opinion-manager` minor；`agents-init` 与 `project-orchestrator` 不需要改模板正文（现有措辞“规则束”已覆盖规则组），不产生 `template_changes`；catalog 内容包按 `template_changes` 判定。本设计 PR 只有 `suite` patch。

---

## 1. 领域概述

业界对“常驻指令、按需加载、查证层”的做法已形成三层结构，一手来源如下。**常驻层**只放短小且几乎每轮适用的内容：Agent Skills 规范规定 `name` 与 `description`（约 100 token）在启动时加载，`SKILL.md` 正文（建议 5000 token 以内）在任务触发时加载，其余资源按需读取（来源：agentskills.io/specification 的 Progressive disclosure 一节；Anthropic 的 Agent Skills overview 的 Level 1/2/3 表格）；Claude Code 文档建议每个 `CLAUDE.md` 控制在 200 行内，仅与部分代码相关的指令迁入带 `paths:` frontmatter 的路径规则，匹配文件时才加载（来源：code.claude.com/docs/en/memory，“Keep your files short, organized, and consistent”与“Path-specific rules”两节）。**按需层**的路由全靠自然语言触发条件，因此是软约束：同一文档写明 `CLAUDE.md` 内容“作为用户消息交付”、Claude“尝试遵循，但不保证严格遵从”，并称其为“context, not enforced configuration”。**查证层**放在模型之外：同一文档建议需要无条件阻止的动作改用 `PreToolUse` hook；hook 注入的 `additionalContext` 单串上限 10,000 字符，超出则写入文件并只传 2,000 字符预览（来源：code.claude.com/docs/en/hooks）。本项目的对应关系是：常驻层为 `context core`，按需层为 `context bundle`，查证层为锁校验与 `context check`。以上文档均于 2026-11-20 抓取原文核对；Codex、Cursor 等其他客户端的等价机制本文**未确认**。

---

## 2. 现状核实（读过源码）

- **读取切片是只读的，来源是 Profile。** `context.py:34` 的 `load()`：没有 `loading`、`opinion.loading_mode=full`、锁校验失败，分别以 `FullReading` 退出码 3、3、2 返回 `fallback`（完整读取 `OPINION.md`）。
- **划分粒度是模板系列。** `context.py:52` 的 `partition()` 把每条规则归到“第一个包含它的模板系列”，常驻系列优先；`loading.bundles[].family` 只能写系列名（`versioned.py:257` 的 `validate_loading`）。`scenario/reviewed-project` 的 27 条规则全在 `scenario` 一个系列里，所以现状下只能整体常驻或整体按需，无法分层。
- **声明的存储与指纹。** `loading` 属于 Profile（`versioned.py:26` 的 `PROFILE_OPTIONAL_FIELDS`），带声明时 `schema_version` 为 2.1（`versioned.py:454`），不带为 2.0。`OPINION.md` 元数据里的 `sha256` 是 Profile 指纹（`versioned.py:602` 的 `render`），锁校验基于 `OPINION.md` 全文 SHA-256（`versioned.py:705` 的 `verify_project`）。
- **模板字段严格。** `TEMPLATE_FIELDS`（`versioned.py:24`）固定为 `family, variant, version, name, description, scopes, rules`，目前无处声明分组。
- **用户采纳的规则落在 `custom` 段。** `inbox adopt` 把 `candidate_rule()`（`rule_lines.py:127`）生成的行追加到 Profile 的 `custom` 文本（`inbox.py:458`），并以 `from_profile` 继承原 Profile，`loading_file=None` 时原声明被沿用（`versioned.py:452`）。`context core` 永远原样输出 `custom` 段（`context.py:85` 的 `core_text`）。
- **使用记账以“规则束”为键。** `lifecycle.py:103` 校验 `loaded_bundles`、`routing_miss` 必须取自 `loading.bundles[].family`；`routing_miss` 使该次使用记为阻断，阻断的版本不能转正式（`lifecycle.py:184`）。
- **会话开始钩子已存在。** `plugins/agent-pack/hooks/hooks.json` 在 `SessionStart`（含 `compact`）调用 `nudge hook`；`agent_pack_nudge.py` 通过子进程 `run_manager()`（超时 5 s，`agent_pack_nudge.py:37`）调用 opinion-manager，并用 `additionalContext` 注入文字（`render()`，`agent_pack_nudge.py:300`；`session_start()` 在 `:322`）。
- **AGENTS 模板已有措辞。** `AGENT.template.md:118` 与 `:146` 写明“Profile 声明了分层加载时，先读取核心规则与规则束索引，任务命中时读取对应规则束；工具返回 `fallback` 时完整读取”。

---

## 3. 问题一：分文件还是单一 `OPINION.md` 加读取时切片

### 3.1 评价维度与方案

七个维度：锁、人工修改检测、回退、升级、兼容旧 Profile、改动面、测试面。改动面与测试面的文件数为实测计数，工作量为估计并标注依据。

#### (a) 现状：单文件 + 系列级读取时切片

- 锁与人工修改检测：不变，仍基于 `OPINION.md` 全文。
- 回退：`fallback` 已完整实现。
- 升级与兼容：不变。
- 问题：系列级粒度，场景模板无法分层；catalog 不随模板提供声明。
- 单独采用 (a) 不解决本问题。

#### (b) 编译期拆文件（索引在 `OPINION.md`，正文在 `.opinion/rules/*.md`）

- 锁：`lock_value()`（`versioned.py:622`）只含 `opinion_sha256`；需要升级锁到 3.0，覆盖索引与每个规则文件的指纹。旧运行时读 3.0 锁会报“锁定文件版本不受支持”，旧项目保持 2.0 锁。
- 人工修改检测：`compose_command()`（`versioned.py:659`）以单个 SHA 做 `--accept-current`；改为文件清单后，确认参数、预览指纹、`blocked` 文案全部重写。
- 原子性：现状两文件各自原子写，中断后由 `verify` 报告（`versioned.py` 的注释）；变成 N+2 个文件，中断状态组合增加。
- 回退：`OPINION.md` 不再是完整事实来源，违反本任务的硬约束；“没有声明就完整读取”的行为不成立。旧 Agent 与旧 AGENTS 模板只读 `OPINION.md` 就只能看到索引，没有任何报错，**静默欠读**，这是五个方案中最危险的失败形态。
- 其他调用方：`inbox_relations.py:61`、`inbox_routes.py:44` 把自定义规则定位为 `OPINION.md:行号`，拆分后行号语义要重定义。
- 改动面：`versioned.py`、`inbox*.py`、`context.py`、AGENTS 模板、3 个 Skill；实测有 15 个测试文件提到 `OPINION.md`，14 个运行时、Skill 与参考文档文件提到 `opinion.lock.json`，估计需改 12–20 个文件。
- 结论：排除。

#### (c) 规则级切片（Profile 内声明，读取时切）

- 锁与回退：与 (a) 相同，`OPINION.md` 不动。
- 粒度：成员关系精确到规则编号。读取单元有两种取法：
  - 纯规则级：每条按需规则一条索引。zh 下 reviewed 模板的 17–19 条按需规则，索引 3,270–3,715 B（`标题｜trigger｜exclude` 的实测）；Agent 面对 17–19 个候选做路由，判断次数多、漏读点多。
  - 规则组：成员精确到规则，读取单元是组。索引目标 4 × 150 B = 600 B。
- 兼容：新增 `loading.groups` 需要 `schema_version` 2.2（见 5.2）。
- 结论：采用“规则级成员 + 规则组读取单元”。单条规则成组也允许，用于个别高频或高风险规则。

#### (d) 把场景模板拆成多个系列，沿用系列级声明

- 锁与回退：不动。
- 兼容：模板 ref 是 `family/variant@version`，现有 Profile 引用 `scenario/reviewed-project@1.0.0`；拆分后同一场景变成 5 个系列的组合，既有 Profile 无法自动映射，需要用户逐个确认迁移。`reviewed` 与 `concise` 仅 4 条规则不同，按系列拆分会出现 5 个系列 × 2 变体的组合，并放大“每个系列只选一个变体”的 slot 冲突面。
- 改动面：catalog 10 个模板全部重写，`updates` 不能把 1 个旧模板更新为 5 个新模板。
- 结论：排除。

#### (e) 其他可行方案

- (e1) 只发布示例 `--loading-file`：系列级声明无法切开 `scenario` 单一系列，不解决问题。排除。
- (e2) 在 `OPINION.md` 里加切片标记（如 HTML 注释）：2.0、2.1 的输出字节要变，或只对新 Profile 变；完整读取的 Agent 得不到任何节省，读取时仍需工具切片，与 (a)+(c) 相比没有增益。排除。
- (e3) 把每个规则组生成为 Agent Skill（`description` 常驻，正文按需）：借用客户端原生的渐进式加载，路由仍是软约束，且产生第二个事实来源（生成的 `SKILL.md` 与 `OPINION.md` 并存，漂移风险），依赖 `agent-pack` 的 Skill 安装与去重链路，各客户端覆盖未确认。列为 6.15 之后的备选，本版不做。

### 3.2 汇总

- 锁语义不变：(a)、(c)、(d)、(e3)；(b) 重写。
- `OPINION.md` 保持完整事实来源：(a)、(c)、(d)、(e1)；(b)、(e2) 不满足。
- 既有 2.0 与 2.1 Profile 零变化：(a)、(c)（2.2 仅新增）；(d) 需要迁移。
- 估计改动面（文件数）：(c) 约 8–10（见第 6 节），(b) 约 12–20，(d) 约 14（10 个模板加运行时与测试）。
- 推荐：**(c) 的收敛版 + (a) 的全部既有机制**。不同意见：无，只对粒度做了上述细化。

---

## 4. 问题二至四的设计细节

### 4.1 粒度与声明从哪来（问题二）

**模板侧：可选 `groups`（只作默认建议来源）。**

```json
"groups": [
  {"id": "baseline", "load": "always",
   "name": {"zh": "基础回复规范", "en": "Baseline reply conventions"},
   "rules": ["om.chinese-response", "om.conclusion-first"]},
  {"id": "decisions", "load": "on_demand",
   "name": {"zh": "方案与论证", "en": "Options and reasoning"},
   "trigger": {"zh": "给出方案、比较、数字或依赖假设的结论时", "en": "When proposing options, comparing, giving numbers or relying on assumptions"},
   "exclude": {"zh": "纯执行、不涉及方案选择的回复", "en": "Pure execution that involves no option choice"},
   "rules": ["om.calculations", "om.complete-options"]}
]
```

- `rules` 写规则编号不带版本，规则升 patch 或 minor 时成员不变。
- 模板内每条规则至多属于一个组；未入组的规则按现行语义视为常驻（“新增规则不会因漏登记被跳过”）。
- `trigger`、`exclude` 为本地化文本，与规则字段一致，由 `pick()` 随项目语言选择；缺译文走现有 `untranslated` 警告，范围扩到组的 `name`、`trigger`、`exclude`。
- 模板的 `groups` 是建议，不单独生效；它参与模板指纹，因此只能随模板新版本发布。

**Profile 侧：物化为 `loading.groups`（schema 2.2）。** 下例的 `signals` 与 `routes` 为显式写出的形态；两者缺省时由运行时在读取时派生同样的内容，不写入 Profile（见下文“`signals` 与 `check`”）。

```json
"loading": {
  "core": [],
  "signals": {"touches": ["scenario:decisions", "scenario:files", "scenario:reports", "scenario:maintenance", "none"]},
  "bundles": [],
  "groups": [
    {"id": "scenario:decisions", "load": "on_demand",
     "rules": ["om.calculations", "om.complete-options"],
     "name": {...}, "trigger": {...}, "exclude": {...},
     "routes": [{"touches": ["scenario:decisions"]}]}
  ]
}
```

- 组 id 加模板系列前缀 `<family>:<group>`，多个模板同时选择时不冲突。
- `partition()` 的优先级：(1) 规则在 `groups` 里按组归属；(2) 否则按现有系列规则；(3) 同时落在常驻与按需时归常驻（沿用现有语义）。每条有效规则恰好归一处，用测试断言。
- 规则被停用或被 `overrides` 修订：成员关系按规则编号，不受影响；组内无有效规则时该组不可用（沿用 `test_disabled_rule_leaves_the_bundle_empty_and_unavailable` 的语义）。
- `context core|bundle|index` 接受组 id；`bundle` 对常驻组报“已常驻”（沿用 `test_always_loaded_families_cannot_be_requested_as_bundles`）。

**默认与改写。**

- 选择带 `groups` 的公开模板时，`profile` 预览自动物化 `loading.groups`，`changes.loading_changed` 与 `loading.before/after`（`versioned.py:385` 已有）展示给用户，沿用“预览 → 确认指纹”。新增 `--no-groups` 开关保持完整读取。
- 用户随时用 `--loading-file` 整体替换，或 `--remove-loading` 回到完整读取；`opinion.loading_mode=full` 仍可让所有会话立即回到完整读取。
- 已有手写 `loading` 的 Profile：不自动合并模板 `groups`，预览里以建议形式列出，用户决定。

**`signals` 与 `check` 没有手写词表时。**

- 方案：2.2 的 `loading.groups` 存在而 `signals` 缺省时，派生词表 `touches = {所有按需组 id} ∪ {"none"}`，并为每个组生成路由 `{"touches": [组 id]}`。`check --signal touches=scenario:decisions --loaded scenario:decisions` 沿用现有比较逻辑。
- 限度：派生信号由 Agent 在交付时自报，只能发现“声明涉及某组却没读该组”的自相矛盾；识别任务类别的责任仍在 Agent。手写词表（如 `artifact=report`）能表达更具体的产物类别，派生词表不替代它。
- 被排除：仅做存在性提示（只比较 `--loaded` 与 `core` 索引）。理由：信息量更少，派生词表的实现成本接近零。

**用户 `custom` 段的新规则。**

- 本版默认不变：`custom` 段整段常驻，`inbox adopt` 的预览与确认流程完全不受影响（`adopt_plan()` 只改 `custom` 文本，`loading` 由 `from_profile` 继承）。
- 可选增强（T4，非最小集合）：`loading.custom: "by_trigger"`（需要 schema 2.2）。`context core` 对 `custom` 段逐行用 `rule_lines.parse_line` 解析：带 `trigger` 且不是全局范围（`is_global`）的行改为索引条目（标题 + trigger + exclude），正文通过 `context bundle custom:<行指纹前 8 位>` 读取；未结构化的行、全局行、路由行一律保持常驻，延续“认不出的行从不猜测”的原则。
- 收益公式：每个转为按需的行节省 `行字节 − 索引条目字节`。条目约 150–250 B（zh 实测同类索引行 100–200 B 量级），单行正文低于该值时没有收益，所以默认关闭，由维护者决定是否值得做（决策 D6）。

### 4.2 用真实数据量化（问题三）

#### 方法与公式

数据来自 PR #151 的 1.1.0 规则与模板，在长路径 worktree 中读取，不 checkout 到本分支。符号：

- `r_i`：规则 i 的渲染字节（`versioned.render_rule`，zh、en 分别计算）。
- `full`：完整 `OPINION.md` 字节（`versioned.render`）。`full = F0 + Σ r_i + 2·(n−1)`，其中 `F0` 为元数据、标题、导语的固定部分；`Σ r_i = 20,815`（reviewed，zh），`full = 21,225`，`F0` 约 410 B。维护者口径 21,227 与本表差 2 B，来自元数据里 Profile 编号的长度。
- `core_out = F + Σ_{i∈常驻} r_i + 2·(k−1) + I`：`F` 为切片框架（标题、Profile 行、说明、索引标题），实测 388 B（zh 界面）/ 419 B（英文界面），`k` 为常驻规则数，`I` 为索引总字节。本表框架取 419 B。
- 索引上界 `I_ub`：把组内成员的 trigger、exclude 直接用“；”拼接，是现有 `core_text` 索引行格式下的**上界**（实测 2,535 B，窄集合 2,863 B）。
- 索引目标 `I_tgt = 组数 × 150 B`（zh）或 `× 190 B`（en）。这是设计目标：每组由模板作者写一句 trigger 与一句 exclude，不拼接成员；由 T4 的守卫测试强制。**该值为目标，实测值以 T4 守卫测试为准。**
- `bundle_out(g)`：`context bundle` 的实测输出（含标题与换行），reviewed、zh 为 decisions 3,941、files 3,612、reports 4,512、maintenance 1,673。
- 典型任务读取量 `typical = core_out(I_tgt) + avg(bundle_out)`；节省 `1 − typical/full`。
- 字节不等于 token：zh 约 3 B/字，实际 token 数未测量，**未确认**。

#### 分组方案（由我提出，依据 trigger 字段）

判据（逐条规则的 trigger，见附录）：

- **常驻**：trigger 对“向用户回复、撰写文字、输出内容”这类几乎每轮发生的事件成立，或出错代价高且触发难以事先判断（不可逆操作前）。
- **按需**：trigger 只在特定产物或特定情形出现（汇报、PPT、HTML、规则维护、方案论证、版本命名）。

推荐方案（宽集合，reviewed 10 条常驻）：

- `baseline`（常驻，10 条）：`chinese-response`、`conclusion-first`、`response-dense`、`evidence-inference`、`bilingual-terms`、`markdown-structure`、`batch-questions`、`align-requirements`、`clean-deliverable`（“撰写叙述文字时”）、`tables-list-first`（“输出文字内容、表格数据或汇报材料时”）。
- `decisions`（按需，5 条）：`calculations`、`complete-options`、`solutions-three-level`、`assumptions-authority`、`test-evidence`。
- `files`（按需，4 条）：`versioned-files`、`concrete-titles`、`portable-files`、`consistent-items`。
- `reports`（按需，6 条）：`report-context`、`report-formats`、`report-review`、`device-readable`、`semantic-wrap`、`editable-diagrams`。
- `maintenance`（按需，2 条）：`rules-tools`、`maintenance-chinese`。

窄集合（常驻 8 条）：同上，但 `clean-deliverable`、`tables-list-first` 移入 `files`。`concise-project` 在 `tables-compact` 的 trigger 为“需要用表格呈现对比或数据时”，范围更窄，宽集合下它也归 `files`，常驻 9 条。分组是 catalog 作者按模板分别声明的，两个变体可以不同。

#### 结果：`scenario/reviewed-project@1.1.0`

- 现状：完整读取 zh 21,225 B，en 26,281 B。
- 宽集合，zh：
  - 常驻规则 7,211 B（10 条）；`core_out` 上界版（`I_ub` 2,535 B）10,183 B（4,177 字符）；目标版（`I_tgt` 600 B）8,248 B。
  - 典型任务读取量：最轻 9,921 B（−53%），平均 11,682 B（−45%），最重 12,760 B（−40%）。
  - 读满 4 组：21,986 B，比完整读取多 761 B。
  - 用上界索引时典型平均 13,618 B（−36%）。
- 窄集合，zh：
  - 常驻规则 5,101 B（8 条）；`core_out` 上界版 8,397 B；目标版 6,134 B。
  - 典型：最轻 7,807 B（−63%），平均 10,097 B（−52%），最重 11,860 B（−44%）。
- 宽集合，en：完整 26,281 B；`core_out` 上界版 12,501 B（12,445 字符），目标版 10,159 B；典型平均 14,412 B（−45%）；读满 4 组 27,173 B（多 892 B）。
- 窄集合，en：`core_out` 上界版 10,415 B，目标版 7,683 B；典型平均 12,556 B（−52%）。

#### 结果：`scenario/concise-project@1.1.0`

- 现状：zh 20,687 B，en 25,710 B。
- 宽集合（常驻 9 条），zh：`core_out` 目标版 7,203 B；典型最轻 8,838 B，平均 10,764 B（−48%），最重 11,715 B；读满 21,449 B（多 762 B）。
- 窄集合（常驻 8 条），zh：目标版 6,224 B；典型平均 10,030 B（−52%）。
- 宽集合，en：目标版 8,934 B；典型平均 13,351 B（−48%）；窄集合 en 典型平均 12,475 B（−51%）。

#### 读数

- 上界索引（现有 `core_text` 的拼接格式）把宽集合的典型节省压到 36%（窄集合 42%），目标版为 45%（窄集合 52%）；索引的书写质量影响约 9 个百分点的节省（宽集合 36% 对 45%），这是模板作者的任务，也是守卫测试要锁住的数字。
- 收益有硬下限：常驻集合 5–7 KB 加框架与索引，典型读取不会低于约 8 KB（zh）。
- 与 1.0.0 对照：维护者给出 1.0.0 完整 14,767 B。宽集合目标版的典型读取量（11,682 B）低于该值，即 1.1.0 加了三项字段后，典型任务读到的内容比 1.0.0 完整读取还少。
- 字符数与注入上限：zh 宽集合 `core_out` 仅 4,177 字符；en 宽集合上界版 12,445 字符，目标版约 10.1K 字符，均可能超过 Claude Code 的 `additionalContext` 10,000 字符上限（见 4.4）。

#### 待决策项（价值判断，维护者拍板）

- 哪些规则常驻（D4）：我推荐宽集合。依据是 trigger 判据的一致应用；窄集合每任务少读约 0.7–1.9 KB（宽减窄的典型平均差：reviewed zh 1,585 B、en 1,856 B，concise zh 734 B、en 876 B），代价是 `clean-deliverable` 与 `tables-list-first` 在写作类回复中可能漏读。本文不替维护者决定规则内容。
- `versioned-files`（1,523 B，trigger “修改文件……”）在代码类项目中几乎每个任务都会触发；若项目以代码任务为主，建议单独上调为常驻，`core_out` 增加 1,523 B。

---

### 4.3 读取与遵从的可靠性（问题四）

#### 能硬保证的（由工具确定性执行）

1. **锁与正文核验**：每次 `context` 调用先 `verify_project`；失败退出码 2，返回 `fallback`。
2. **回退**：没有声明、`opinion.loading_mode=full`，退出码 3，返回 `fallback`；回退内容是完整 `OPINION.md`，行为与现状一致。
3. **切片与正文逐字一致**：切片由同一个 `render_rule` 生成；T1 增加完整性测试：每条有效规则恰好出现在一个切片里，所有切片规则文本拼接后与 `OPINION.md` 的规则段逐字相等。
4. **声明合法性**：悬空规则编号、重复归属、未知组 id、路由引用未声明信号，在 `profile` 预览阶段拒绝。
5. **`context check` 对已声明信号的缺失检测**：Agent 声明了信号而未读对应规则组，退出码 1，并列出缺失组。

#### 不能硬保证的

1. Agent 在会话开始是否运行 `context core`，在任务命中触发条件时是否读取规则组：全靠指令。Claude Code 文档称 `CLAUDE.md` 为“context, not enforced configuration”，“no guarantee of strict compliance”；AGENTS 文件的读取同理。
2. 交付信号由 Agent 自报；自报缺失或不实，`check` 无从发现。
3. 压缩后的保留：Claude Code 文档写明项目根 `CLAUDE.md` 在 `/compact` 后从磁盘重读，只存在于对话里的指令会丢失。`context core` 的输出属于对话内容，压缩后可能丢失；Claude Code 以外的客户端行为**未确认**。
4. 读取行为的真实命中率：当前没有任何客户端实测数据，**未确认**。

#### `routing_miss` 如何记账

- 记账者是写 `lifecycle event`（`use` 类型）的一方：Agent 自述，或复盘者发现应读未读后补记。它是灰度期的审计线索，运行时不据此阻断，只在转正式前阻断（`lifecycle.py:184`）。
- 变更：`lifecycle.py:103` 的合法集合改为“family 集合 ∪ group id 集合”；`status` 的 `bundles` 表继续用 `family` 字段承载组 id，并增加 `kind: "family" | "group"`；`exercised_in` 的成员映射（`rule_families`，`lifecycle.py:68`）扩展为组成员。
- 新增判据（T3）：从未被读取的按需组出现在 `unused_bundles`，提示该组触发条件写得过窄或该组可并入常驻。

### 4.4 是否在会话开始注入 `context core`（本版建议不做）

- 可复用路径：`agent_pack_nudge.session_start` 已经通过子进程 `run_manager()` 调 opinion-manager，并以 `additionalContext` 注入，`source == "compact"` 也会再次注入。`agent-pack` 与 `opinion-manager` 只能经子进程互调的约束已被遵守，新增调用无需新的依赖方向。
- 体积预算：Claude Code 对 `additionalContext` 单串上限 10,000 字符，超出则落盘、仅传 2,000 字符预览（hooks 文档）。zh 宽集合 `core_out` 4,177 字符，足够；en 宽集合上界版 12,445 字符、目标版约 10.1K 字符，超限；en 窄集合目标版 7,683 字符可行。预算建议：注入内容不超过 9,500 字符。
- 不做的理由：(1) `run_manager()` 把非零退出当失败，`fallback` 的退出码 3 要专门处理；(2) en 宽集合超限；(3) Codex 与 Cursor 的 `SessionStart` 注入语义**未确认**；(4) 没有 `routing_miss` 数据证明 AGENTS 指令不够。
- 替代与后续：本版先在 AGENTS 与 Skill 的现有措辞下运行，用灰度期 `routing_miss` 统计决定 6.15 是否注入；若注入，先在 zh 宽集合与 en 窄集合上开启。决策 D7。

---

## 5. 兼容与迁移（问题五）

### 5.1 既有项目零变化

- 2.0（无 `loading`）与 2.1（系列级 `loading`）的 Profile：校验、指纹、`OPINION.md` 字节、锁格式、`context` 输出一律不变。T1 的黄金测试：以当前 `dev` 的 2.0、2.1 夹具计算 Profile SHA-256 与 `OPINION.md` SHA-256 固定为常量，改动后必须相等。
- 新字段只在 2.2 出现；仅当 `loading.groups` 或 `loading.custom` 存在时才写 2.2。

### 5.2 旧工具读新数据（实测）

用当前 `dev` 运行时验证，旧工具遇到新数据时会明确报错，没有静默读取的路径：

- 读 `schema_version` 2.2 的 Profile：`Profile must have exactly these fields: ['custom', 'derived_from', 'id', 'overrides', 'rules', 'schema_version', 'sources', 'templates', 'version']`。这条报错说明字段集合不符，没有直接点出“版本过新”；旧工具的文案已发布，不能追改。新工具在 2.2 校验失败时给出明确的“需要新版运行时”提示。
- 读含 `groups` 的模板：`published content must have exactly these fields: ['description', 'family', 'name', 'rules', 'scopes', 'variant', 'version']`。
- 旧工具 `catalog` 命令指向含 `groups` 的 catalog 目录时，整个命令失败（实测）。catalog 随插件分发，正常情况下运行时与 catalog 同版本；跨版本混用（旧插件指向新 catalog 路径）会报错。

### 5.3 升级到 catalog 1.1.0 的路径

1. `opinion_manager.py updates` 列出模板与规则 1.1.0（PR #151 的现有行为）。
2. 若 1.1.0 模板携带 `groups`，`profile` 预览显示 `loading` 由无声明变为 `loading.groups`，Profile 2.0 或 2.1 升到 2.2；用户确认指纹后 `--apply`。
3. 手工修改保留：`OPINION.md` 被人工改过时，沿用 `compose_command` 的阻断与 `--accept-current` 流程（`versioned.py:659`）；`custom` 段内容原样保留。
4. 不想分层：`--no-groups`，或升级后设 `opinion.loading_mode=full`。
5. 已有手写 `loading`：不自动合并，预览里给出模板建议。
6. 前提：catalog 1.1.0 尚未发布，建议把 `groups` 直接并入 1.1.0（PR #151 重做时加），避免 1.1.0 之后紧接 1.2.0 迫使用户连升两次。决策 D3。

### 5.4 en 项目

- 规则字段已有 zh/en；组的 `name`、`trigger`、`exclude` 同样本地化，`pick()` 按项目语言选择；缺译文回退 zh 并出现在 `untranslated` 警告。
- `context` 输出的切片框架按界面语言（`_()`），规则正文按项目语言；两者独立，沿用现状。
- en 体积约为 zh 的 1.24 倍（reviewed：26,281 / 21,225），预算要分语言设定。

### 5.5 其他组件需要同步的内容（只列，不改）

- `AGENT.template.md:118`、`:146`、`:483-491`：现有措辞“规则束”“核心规则与规则束索引”已覆盖规则组。可选增补一句“压缩后重新读取 `context core`”，非必需；若增补，属 `agents-init` 的语义模板变化，需要 `template_changes`。
- `project-orchestrator` 的 `SKILL.md:193`：同上，无必需改动。
- `opinion-manager` 的 `SKILL.md`（约 52 行）与 `references/versioning.md`「分层加载」一节：必须补充 `loading.groups`、2.2、`touches` 派生词表、`--no-groups`。
- `agenthelm check/fix`、`agents-init` 的 `init_project.py`：只处理 Project Profile（`schema_version` 1.0 与 `loading_mode`），不读 Opinion Profile，无需改动。
- `release/migrations.json`：无迁移项（既有项目零变化）。

### 5.6 版本与片段声明建议（实施 PR）

- `opinion-manager`：minor。`user_action_required`：空或“可选：`updates` 查看 1.1.0 默认规则组”；`touches`：`[".opinion/profiles", "opinion.lock.json"]`；`default_for_existing`：`advisory`（既有项目不自动变化，升级时才出现预览）。
- catalog 内容包（`opinion-manager/catalog`）：由 `template_changes` 判定；模板加入 `groups` 属语义变化（`"class": "semantic"`），内容包 minor，并需要用户动作（既有项目运行 `updates`）。路径必须匹配内容包来源（`catalog/` 下文件），不能用无关路径占位。
- `agents-init`、`project-orchestrator`、`agent-pack`：不改则无片段。
- 套件：随最高等级。
- 本设计 PR：`release/changes/64-opinion-loading-design-614.json`，仅 `suite` patch，无 `template_changes`。

---

## 6. 实施拆分（问题六）

拆为 4 个子任务。依赖：T1 先行并冻结接口；T2、T3 在 T1 接口冻结后并行；T4 的守卫部分可与 T1 同步开始，真实 catalog 夹具依赖 T2。

### T1 schema 与切分引擎（opinion-manager runtime）

- 范围：`loading.groups`、`schema_version` 2.2、`validate_loading` 扩展、`partition()` 的组优先级、`context core|bundle|index|check` 接受组 id、派生 `touches` 词表、2.2 校验失败的明确提示。
- 文件：`runtime/versioned.py`、`runtime/context.py`、`references/*.schema.json`（如有 Profile schema）、`tests/test_context.py`、`tests/test_versioned.py`。
- 依赖：无。
- 验收：
  - 2.0、2.1 黄金测试：Profile 指纹与 `OPINION.md` 字节与 `dev` 一致。
  - 完整性：每条有效规则恰好归一个切片；切片规则文本与 `OPINION.md` 逐字一致。
  - 失败路径：悬空编号、重复归属、未知组 id、停用规则后组为空、常驻组被 `bundle` 请求，均有用例。
  - 回退路径：无声明、`loading_mode=full`、锁校验失败仍返回 `fallback`，退出码 3、3、2 不变。
  - **长路径 worktree（≥138 字符）**：`context core|bundle|check` 经子进程 CLI 对长 `--project` 路径的端到端用例；Profile 保存到 `.opinion/profiles/<id>/versions/<ver>.json` 后再校验。

### T2 模板 `groups` 与预览物化（opinion-manager 运行时 + catalog 机制）

- 范围：`TEMPLATE_FIELDS` 增加可选 `groups`；`versioned-template.schema.json` 同步；`profile` 预览物化 `loading.groups`；`--no-groups`；`updates` 预览显示 `loading` 变化；`untranslated` 覆盖组文字；已有手写 `loading` 时只给建议。
- 文件：`runtime/versioned.py`、`references/versioned-template.schema.json`、`tests/test_versioned.py`、`tests/test_catalog_bilingual.py`；catalog 模板的 `groups` 内容由维护者在 PR #151 重做时定（D3、D4），T2 只交付机制与一份 `reviewed`、`concise` 的建议夹具。
- 依赖：T1。
- 验收：
  - 选择带 `groups` 的模板，预览含 `loading.groups`，确认指纹覆盖它；`--no-groups` 保持 2.0 与原指纹。
  - 模板无 `groups` 时输出与 `dev` 字节相同。
  - `updates` 从 1.0.0 升到 1.1.0：手工修改保留、`--accept-current` 流程不变。
  - en 项目：组文字缺译文进入 `untranslated`。
  - **长路径 worktree**：`profile` → `compose` → `verify` 往返；`updates` 预览写入临时目录再清理。

### T3 记账与文档同步（lifecycle、Skill、参考文档）

- 范围：`lifecycle.py` 接受组 id、`status` 的 `kind` 字段与 `unused_bundles`；`versioning.md`「分层加载」「候选、灰度与使用记录」两节；`opinion-manager` 的 `SKILL.md`；英文镜像（`i18n` 待翻译清单或配对，按 `tests/test_i18n_policy.py` 的要求）。
- 文件：`runtime/lifecycle.py`、`references/versioning.md`、`skills/opinion-manager/SKILL.md`、对应 en 文件、`tests/test_lifecycle.py`。
- 依赖：T1 语义（可与 T2 并行）。
- 验收：`routing_miss`、`loaded_bundles` 接受组 id 并拒绝未声明 id；`status` 对组输出 `loaded_in`、`exercised_in`、`routing_misses`；文档与 i18n 守卫测试通过。

### T4 体积守卫与可选增强

- 范围（必选）：体积预算守卫测试；（可选）`loading.custom: "by_trigger"`；（可选）会话开始注入的预算与开关设计。
- 文件：`tests/test_loading_budget.py`（新增，读取 `plugins/opinion-manager/catalog` 固定路径）；可选部分涉及 `runtime/context.py`、`agent-pack/runtime/agent_pack_nudge.py`。
- 依赖：守卫的框架部分无依赖，真实夹具依赖 T2 与 catalog 重做；可选部分依赖 T1。
- 守卫用例（固定路径、固定模板，zh 与 en 各一遍）：
  - `scenario/reviewed-project` 与 `scenario/concise-project` 的 `core_out` 字节不超过预算；索引总字节不超过 `组数 × 160 B`（zh）、`组数 × 200 B`（en）。
  - 按需组数不超过 6；每组至少 1 条规则；常驻集合字节占完整正文的比例不超过 40%。
  - 切片总和（`core_out` + 全部 `bundle_out`）与完整正文之差不超过 1,200 B（目标版推算 761–893 B）。
  - 预算数值待 D8 决定；推荐 reviewed zh `core_out` ≤ 9,000 B、en ≤ 11,000 B；注入开启前再加 en `core_out` ≤ 9,500 字符。
- **长路径 worktree**：守卫测试读取仓库内 catalog 并在长路径下运行，确认不依赖相对路径。

### 最小可发布集合

- **M0（时间最紧）**：T1 + T4 的守卫框架。用户用 `--loading-file` 手写 2.2 声明（含 `groups`）；catalog 暂无默认声明；收益在于机制就绪与回归安全，PR #151 不会因为体积失控而被拒。
- **M1（推荐发布集合）**：T1 + T2 + T3 + T4 守卫。新选择公开模板即得默认分层，文档与记账完备。
- 排到 6.15：`loading.custom: "by_trigger"`、会话开始注入、基于 `routing_miss` 的组调整。

---

## 7. 术语与口径说明

- “常驻 / 按需”指读取时机：常驻随 `context core` 输出，按需须命中触发条件后读取。
- “规则组”是读取单元，成员以规则编号表示；“规则束”是现有文档对系列级读取单元的叫法。本文设计中组也称规则束，`context bundle` 命令名不变。
- 体积一律为 UTF-8 字节，另标字符数的地方明确写“字符”。

---

## 8. 决策项（一次打包）

1. **D1 采用方案。** 推荐：(c) 收敛版（规则级成员、规则组读取）+ (a)。被排除：(b) 分文件，理由是锁、修改检测、原子写、`inbox` 行号引用与读取说明全部重写，且旧 Agent 静默欠读；(d) 拆模板系列，理由是模板身份变化、既有引用无法迁移；(e1) 只发示例文件，理由是系列级声明切不开 `scenario`；(e2) 切片标记，理由是无节省且改输出字节；(e3) 规则组生成 Skill，理由是第二事实来源与客户端覆盖未确认，列为 6.15 后备选。
2. **D2 声明来源与默认行为。** 推荐：模板带 `groups`，`profile` 预览自动物化为 `loading.groups`，用户确认后生效；已有手写 `loading` 不自动合并；提供 `--no-groups`。被排除：运行时从模板直接派生、不进 Profile（既有 Profile 会在无确认下改变读取行为，违反“没有声明就完整读取”）。
3. **D3 `groups` 放进哪个 catalog 版本。** 推荐：并入尚未发布的 1.1.0（PR #151 重做时加），避免用户连升两次。被排除：1.2.0（多一次升级预览，catalog 1.1.0 先带来 +6 KB 的完整读取负担）。
4. **D4 常驻集合。** 推荐：宽集合（reviewed 10 条，concise 9 条）；`versioned-files` 是否上调由项目类型决定。窄集合每任务少读约 0.7–1.9 KB（宽减窄的典型平均差：reviewed zh 1,585 B、en 1,856 B，concise zh 734 B、en 876 B），代价是写作类回复漏读风险。此项为价值判断，维护者拍板。
5. **D5 `check` 无手写词表时。** 推荐：派生 `touches` 词表。被排除：仅存在性提示（信息量更少，实现成本相近）。
6. **D6 `custom` 段。** 推荐：本版整段常驻，`by_trigger` 作为后续可选（T4 可选项）。被排除：本版默认按 trigger 转按需（单行节省有限，且误路由会让用户亲自确认过的规则被漏读）。
7. **D7 会话开始注入。** 推荐：本版不注入，先用灰度期 `routing_miss` 数据决定；若注入，预算 9,500 字符内。被排除：本版注入（en 宽集合超过 10,000 字符上限、`fallback` 退出码需特判、Codex 与 Cursor 语义未确认）。
8. **D8 预算数值。** 推荐：reviewed zh `core_out` ≤ 9,000 B、en ≤ 11,000 B；索引每组 ≤ 160 B（zh）、200 B（en）；按需组 ≤ 6。被排除：不设预算（体积回退无人发现）。
9. **D9 Profile 版本。** 推荐：新增 2.2，仅在存在 `loading.groups` 或 `loading.custom` 时写入。被排除：复用 2.1 加可选字段（版本号与能力不符，旧工具同样报错但文案指向 `loading` 字段，更难诊断）。
10. **D10 发布等级。** 推荐：实施 PR 的 `opinion-manager` minor，catalog 内容包按 `template_changes` 判 minor，`agents-init`、`project-orchestrator` 不动；本设计 PR 仅 `suite` patch。

### 未确认清单

- Codex、Cursor 等客户端的会话开始注入与上下文压缩后的行为。
- 真实客户端下 Agent 对“命中触发条件时读取规则组”的遵从率（无实测数据）。
- 字节到 token 的换算（未测量）。
- 其他客户端对 `additionalContext` 的体积上限（仅核实 Claude Code，抓取日期 2026-11-20）。
- 灰度期 `routing_miss` 的真实分布。
- 英文界面与 zh 界面的框架长度差（实测 31 B，已计入）；其他语言未测。

### 能力分工

- 我擅长并已完成：源码核实、体积计算与公式、兼容性实验、方案穷举与量化、实施拆分。
- 需要维护者拍板：哪些规则常驻（D4）、`custom` 默认行为（D6）、是否注入（D7）、预算数值（D8）、规则组的名称与 trigger 文案（审美与措辞偏好）、catalog 1.1.0 是否并入 `groups`（D3，涉及发布节奏）。
- 倾向性提示：推荐 (c)+(a) 受“保持锁语义、最小破坏”这一目标影响，替代视角是 (e3) 借用客户端原生 Skill 渐进式加载，其收益取决于各客户端对 Skill 触发的支持，本文未验证。

---

## 附录 A：逐条规则的渲染字节与建议组

字节为 `versioned.render_rule` 的 UTF-8 字节：`zh / en`。组为推荐的宽集合。

**scenario/reviewed-project@1.1.0**

- `conclusion-first`：580 / 731｜baseline｜给出分析、说明或建议性回答时
- `chinese-response`：460 / 575｜baseline｜向用户回复时
- `bilingual-terms`：594 / 776｜baseline｜正文中首次出现专业术语时；术语密集时；引用外文资料时
- `markdown-structure`：409 / 504｜baseline｜输出多层次内容时
- `clean-deliverable`：977 / 1166｜baseline｜撰写叙述文字时；制作代码、文案、PPT、HTML 等最终交付材料时
- `concrete-titles`：507 / 589｜files｜为文档、章节或图表拟定标题与副标题时
- `response-dense`：557 / 707｜baseline｜回答复杂问题或需要分析推导的问题时
- `evidence-inference`：1085 / 1386｜baseline｜给出事实性结论、数据或技术方案时；表达不确定结论、推测、概率或置信度时
- `test-evidence`：833 / 1044｜decisions｜用户转述实测结果、官方答复或亲身经验时
- `calculations`：792 / 987｜decisions｜给出关键数字、比较方案，或进行较复杂、较长的计算时
- `complete-options`：412 / 487｜decisions｜要给出二选一的方案或建议之前
- `align-requirements`：816 / 1061｜baseline｜展开完整方案前；理解出现偏差、初始假设或关键前提有误时；执行不可逆操作前
- `batch-questions`：600 / 750｜baseline｜存在不确定内容或需要向用户收集信息时
- `assumptions-authority`：876 / 1143｜decisions｜回答依赖未经确认的假设时；任务涉及价值判断、审美偏好、领域经验或实时业务决策时；做技术选型或方法论推荐时
- `solutions-three-level`：992 / 1249｜decisions｜针对问题给出解决方案时
- `versioned-files`：1523 / 1942｜files｜修改文件、为文件命名版本、推进完整系统，或制作邮件、PPT、HTML 等汇报材料时
- `device-readable`：925 / 1089｜reports｜制作含图表、图内文字、长段文字或其他排版内容的材料时
- `semantic-wrap`：782 / 942｜reports｜设计 PPT、HTML 及其他排版内容的换行时
- `editable-diagrams`：734 / 868｜reports｜内容结构复杂或具有流程性时
- `consistent-items`：852 / 1060｜files｜同一组内有多个承担相同展示目的的同类条目时
- `report-context`：647 / 817｜reports｜制作汇报前
- `report-formats`：679 / 869｜reports｜同一汇报同时制作 PPT、HTML、Markdown 等多种载体时
- `report-review`：709 / 900｜reports｜汇报交付前
- `tables-list-first`：1133 / 1306｜baseline｜输出文字内容、表格数据或汇报材料时
- `portable-files`：700 / 839｜files｜设计或维护项目文档、档案及导航结构时
- `rules-tools`：858 / 1068｜maintenance｜判断表达、写作、工作方法、审美和交付规范该保存在哪里时；编写 Skill、规则或模板时
- `maintenance-chinese`：783 / 987｜maintenance｜编写或维护当前项目的第一方 Skill、模板与维护文档时

组合计（zh / en，条数）：baseline 7,211 / 8,962（10）；decisions 3,905 / 4,910（5）；files 3,582 / 4,430（4）；reports 4,476 / 5,485（6）；maintenance 1,641 / 2,055（2）。

**scenario/concise-project@1.1.0 与上表不同的规则**（其余 23 条相同）

- `response-brief`：647 / 790｜baseline｜回答问题，尤其是复杂问题时（取代 `response-dense`）
- `solutions-proportional`：763 / 954｜decisions｜解决问题并需要给出方案时（取代 `solutions-three-level`）
- `tables-compact`：773 / 951｜files｜需要用表格呈现对比或数据时（取代 `tables-list-first`，范围较窄，按需）
- `maintenance-project-language`：745 / 984｜maintenance｜编写、维护或翻译第一方 Skill、模板与维护文档时（取代 `maintenance-chinese`）

组合计（zh / en，条数）：baseline 6,168 / 7,739（9）；decisions 3,676 / 4,615（5）；files 4,355 / 5,381（5）；reports 4,476 / 5,485（6）；maintenance 1,603 / 2,052（2）。

## 附录 B：复算方法

在 PR #151 分支的 worktree 里，对每个模板：用 `versioned.load_catalog` 读取规则，用 `versioned.render_rule(profile, rule, lang)` 取每条字节；用 `context.partition` 的替身返回上述分组，调用真实的 `context.core_text`、`context.bundle_text` 得到 `core_out`、`bundle_out`；`I_ub` 为组内 trigger、exclude 以“；”拼接后的索引行字节之和，`I_tgt` 按 4 × 150 B（zh）、4 × 190 B（en）替换 `I_ub`。完整 `OPINION.md` 字节取 `versioned.render(profile, lang)`。所有数值可由这些函数在同一分支重现；脚本为一次性测量用，未提交。

## 术语对照表

- 渐进式加载（progressive disclosure / progressive loading）：按需分阶段把内容载入上下文。
- 常驻层 / 按需层 / 查证层：always-loaded / on-demand / verification layer。
- 规则组 / 规则束（rule group / bundle）：读取单元。
- 触发条件（trigger）、不适用（exclude）、目的（purpose）：规则的适用说明字段。
- 回退（fallback）：校验失败或无声明时完整读取 `OPINION.md`。
- 锁（lock，`opinion.lock.json`）：记录 Profile 快照与正文指纹的文件。
- 指纹（fingerprint）：SHA-256 摘要。
- 路由漏读（`routing_miss`）：本应读取却没有读取的规则束记录。
- 灰度（rollout）：候选规则在受控范围内的试用阶段。
- 内容包（content pack）：随插件分发、按 `template_changes` 判定版本的内容集合。
- 钩子（hook）：客户端在会话事件上调用的外部命令。
- `additionalContext`：hook 向模型上下文注入的文字字段。
