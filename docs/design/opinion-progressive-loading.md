# 公开规则的标签选择加载设计（Issue #64，6.14.0）

- 状态：已定稿（6.14.0）。引擎与数据结构随本文所在的实施 PR 落地（任务 T1）；模板声明、预览物化、catalog 1.1.0 内容、迁移命令、记账、Skill 与 AGENTS 文案、体积守卫是后续任务（第 11 节）。
- 基线：`origin/dev` 的 `5c6d28e`。体积数据来自 PR #151（`1e41ae5`）里 catalog 1.1.0 的真实规则与模板，用 `versioned.render_rule`、`context.core_text`、`context.bundle_text` 的真实函数在独立 worktree 中计算（该设计的首版为 PR #152）。
- 读者：仓库维护者与贡献者。术语首次出现标注中英对照，文末有术语对照表。
- 与 `docs/design/progressive-loading.md` 的分工：那篇处理项目入口文件与 Skill 的体积分层；本文只处理 `OPINION.md` 里公开规则的读取分层。

---

## 0. 决定摘要

1. **保持单一 `OPINION.md`，读取时切片。** 锁、人工修改检测、`fallback`、`inbox` 的行号引用语义不变；物理分文件不在本版。
2. **用标签选择器（tag selector）做按需加载，取代“每条规则只属于一个组”。** 一条规则可带多个标签（多对多）；程序按标签集合运算返回规则文本；规则组等同于具名的标签集合。多个 `--tag` 取并集并去重；常驻规则由 `core` 输出，不受标签选择影响；没有标签的规则视为常驻，新增规则不会因漏登记被跳过。
3. **标签词表封闭。** 未知标签直接报错并列出有效标签。标签 id、规则 id 一律 ASCII，中文只作显示名（`name`、`trigger`、`exclude` 的 zh/en 文字）。
4. **`context check` 由标签派生信号。** Agent 声明本次交付涉及的标签，程序算出应读取的规则并核对已读取；缺失退出码 1，沿用现有 `check` 的行为与退出码。
5. **用户 `custom` 段的规则也能带标签。** 行尾固定写法为 ` [tags a b]`，带有效标签的行按标签按需加载；没有标记、认不出的行永远常驻（沿用“认不出的行从不猜测”）。
6. **本版不在会话开始注入 `context core`。**
7. **体积守卫（数值已定，测试在 T4）：** reviewed 项目 zh 的 `core` 输出 ≤ 9,000 B、en ≤ 11,000 B；按需标签 ≤ 6 个；索引每个按需标签 ≤ 160 B（zh）、200 B（en）。引擎的 `context index` 输出逐标签给出这些数值（第 4.6 节）。
8. **常驻集合由 catalog 重做时声明。** 本版只提供机制与测试夹具。
9. **Profile 版本：** 带标签声明时写 `schema_version` 2.2，其余情形保持 2.0 与 2.1。2.0、2.1 的 Profile、`OPINION.md`、锁一字节不变（黄金测试固定了六个 SHA-256）。
10. **迁移由后续任务的 `opinion_manager.py migrate` 完成。** 契约见第 7 节：默认预览，`--apply --confirm <指纹>` 才写入，写入前备份且可回滚。
11. **发布等级：** 本 PR 的 `opinion-manager` 为 minor，`default_for_existing` 为 `advisory`（既有项目不自动变化）；`agents-init`、`project-orchestrator` 不改模板正文。

---

## 1. 领域概述

业界对“常驻指令、按需加载、查证层”的做法已形成三层结构，一手来源如下。**常驻层**只放短小且几乎每轮适用的内容：Agent Skills 规范规定 `name` 与 `description`（约 100 token）在启动时加载，`SKILL.md` 正文（建议 5000 token 以内）在任务触发时加载，其余资源按需读取（来源：agentskills.io/specification 的 Progressive disclosure 一节；Anthropic 的 Agent Skills overview 的 Level 1/2/3 表格）；Claude Code 文档建议每个 `CLAUDE.md` 控制在 200 行内，仅与部分代码相关的指令迁入带 `paths:` frontmatter 的路径规则，匹配文件时才加载（来源：code.claude.com/docs/en/memory，“Keep your files short, organized, and consistent”与“Path-specific rules”两节）。**按需层**的路由全靠自然语言触发条件，因此是软约束：同一文档写明 `CLAUDE.md` 内容“作为用户消息交付”、Claude“尝试遵循，但不保证严格遵从”，并称其为“context, not enforced configuration”。**查证层**放在模型之外：同一文档建议需要无条件阻止的动作改用 `PreToolUse` hook；hook 注入的 `additionalContext` 单串上限 10,000 字符，超出则写入文件并只传 2,000 字符预览（来源：code.claude.com/docs/en/hooks）。本项目的对应关系是：常驻层为 `context core`，按需层为 `context select`（2.1 声明下为 `context bundle`），查证层为锁校验与 `context check`。以上文档均于 2026-11-20 抓取原文核对；Codex、Cursor 等其他客户端的等价机制**未确认**。

---

## 2. 现状核实（dev `5c6d28e`，读过源码）

- **读取切片是只读的，来源是 Profile。** `context.py` 的 `load()`：没有 `loading`、`opinion.loading_mode=full`、锁校验失败，分别以 `FullReading` 退出码 3、3、2 返回 `fallback`（完整读取 `OPINION.md`）。
- **划分粒度是模板系列。** `partition()` 把每条规则归到“第一个包含它的模板系列”，常驻系列优先；`loading.bundles[].family` 只能写系列名（`versioned.validate_loading`）。`scenario/reviewed-project` 的 27 条规则全在 `scenario` 一个系列里，现状下只能整体常驻或整体按需。
- **声明的存储与指纹。** `loading` 属于 Profile，带声明时 `schema_version` 为 2.1，不带为 2.0。`OPINION.md` 元数据里的 `sha256` 是 Profile 指纹，锁校验基于 `OPINION.md` 全文 SHA-256（`verify_project`）。
- **模板字段严格。** `TEMPLATE_FIELDS` 固定为 `family, variant, version, name, description, scopes, rules`，目前无处声明标签；本版也不改它（模板声明属后续任务）。
- **用户采纳的规则落在 `custom` 段。** `inbox adopt` 把 `candidate_rule()` 生成的行追加到 Profile 的 `custom` 文本，并以 `from_profile` 继承原 Profile，`loading_file=None` 时原声明被沿用。`rule_lines.parse_line` 只认本工具渲染的两种格式，认不出的行报告为“未结构化”。
- **使用记账以“规则束”为键。** `lifecycle.py` 校验 `loaded_bundles`、`routing_miss` 取自 `loading.bundles[].family`；`routing_miss` 使该次使用记为阻断，阻断的版本不能转正式。标签声明没有 `bundles`，本版对 2.2 的处理是“不记账、不崩溃”（`loaded_bundles` 与 `routing_miss` 只接受空列表），完整记账属 T3。
- **会话开始钩子已存在。** `plugins/agent-pack/hooks/hooks.json` 在 `SessionStart`（含 `compact`）调用 `nudge hook`，通过子进程调用 opinion-manager，用 `additionalContext` 注入文字。

---

## 3. 方案比较与决定

### 3.1 存储与读取方式

评价维度：锁、人工修改检测、回退、升级、兼容旧 Profile、改动面、测试面。

- **(a) 单文件 + 读取时切片（采用）。** 锁与人工修改检测基于 `OPINION.md` 全文，不变；`fallback` 已完整实现；既有 Profile 零变化。
- **(b) 编译期拆文件（索引在 `OPINION.md`，正文在 `.opinion/rules/*.md`）：排除。** 理由：锁需升到 3.0 覆盖多个文件；`--accept-current` 与预览指纹要改为文件清单；中断状态组合从 2 个文件增加到 N+2 个；`OPINION.md` 不再是完整事实来源，旧 Agent 只读它会**静默欠读**，是所有方案中最危险的失败形态；`inbox` 的 `OPINION.md:行号` 引用要重定义。估计改动 12–20 个文件。
- **(d) 把场景模板拆成多个系列：排除。** 理由：模板身份 `family/variant@version` 变化，`scenario/reviewed-project@1.0.0` 等既有引用无法一对一迁移；`reviewed` 与 `concise` 仅 4 条规则不同，拆分后出现 5 个系列 × 2 变体的组合，放大“每个系列只选一个变体”的 slot 冲突面。
- **(e1) 只发布示例 `--loading-file`：排除。** 系列级声明切不开单一系列 `scenario`。
- **(e2) 在 `OPINION.md` 里加切片标记：排除。** 2.0、2.1 的输出字节要变，完整读取的 Agent 得不到节省，读取时仍需工具切片。
- **(e3) 把每个标签集生成为 Agent Skill：排除（6.15 之后备选）。** 路由仍是软约束，产生第二个事实来源（生成的 `SKILL.md` 与 `OPINION.md` 并存），依赖 `agent-pack` 的 Skill 安装链路，各客户端覆盖未确认。

### 3.2 读取单元：标签选择，不用“每条规则只属于一个组”

- **采用：多对多标签。** 一条规则可属于多个标签，读取单元是标签集合的并集。理由：真实规则常横跨场景（`versioned-files` 与汇报类规则都涉及“制作交付材料”）；单归属模型迫使作者在“重复写规则”与“漏读”之间选一个。
- **被排除：每条规则只属于一个组（#152 的首版设计）。** 单归属模型下组即分区，无法表达横跨；并集语义与去重（第 4.3 节）把重叠的代价压到零。
- **被排除：纯规则级索引（每条按需规则一条索引）。** zh 下 reviewed 模板的 17–19 条按需规则，索引 3,270–3,715 B；Agent 面对 17–19 个候选做路由，判断次数多、漏读点多，抵消大部分收益。标签集把索引压到每个标签一行。

---

## 4. 标签选择模型（schema 2.2，冻结接口）

### 4.1 Profile 的 `loading`

`loading` 只含三项，与 2.1 的 `core` / `signals` / `bundles` 互斥（一个 Profile 只用一种声明）。完整示例：

```json
{
  "schema_version": "2.2",
  "...": "其余字段与 2.0 / 2.1 相同",
  "loading": {
    "always": ["om.chinese-response", "om.conclusion-first"],
    "tags": [
      {"id": "decisions",
       "name":    {"zh": "方案与论证", "en": "Options and reasoning"},
       "trigger": {"zh": "给出方案、比较、数字或依赖假设的结论时", "en": "When proposing options, comparing, giving numbers or relying on assumptions"},
       "exclude": {"zh": "纯执行、不涉及方案选择的回复", "en": "Pure execution that involves no option choice"}},
      {"id": "reports",
       "name":    {"zh": "汇报材料", "en": "Report materials"},
       "trigger": {"zh": "制作或审查汇报材料时", "en": "When producing or reviewing report materials"},
       "exclude": {"zh": "闲聊与纯代码任务", "en": "Chat and pure coding tasks"}}
    ],
    "members": {
      "om.calculations":      ["decisions"],
      "om.complete-options":  ["decisions"],
      "om.versioned-files":   ["decisions", "reports"],
      "om.report-context":    ["reports"]
    }
  }
}
```

- `always`：常驻集合，写规则编号（不带版本，规则升 patch 或 minor 时不受影响）。
- `tags`：封闭词表，每项恰好四个字段。`id` 为 ASCII（小写字母、数字、连字符，1 至 32 字符，不能取保留名 `core`、`none`、`all`）；`name`、`trigger`、`exclude` 是只含 `zh`、`en` 两个键的对象，两种语言都必须是非空字符串。显示文字由 `pick()` 随项目语言选择，因此 2.2 的词表没有“缺译文”状态。
- `members`：键是规则编号，值是该规则的标签列表（非空、不重复、只能用词表里的标签）。没有出现在 `members` 里、也不在 `always` 里的规则是常驻的。
- 规则编号沿用现有 `IDENTIFIER`（ASCII 小写字母、数字、`.`、`-`），暂不允许中文 id。

### 4.2 校验规则（`validate_tag_loading`，`profile` 预览阶段执行）

- 字段集合不符（缺项、多项、与 2.1 字段混用）：拒绝，提示 `loading 字段必须为：['always', 'members', 'tags']`。
- **悬空规则编号：** `always` 或 `members` 引用 Profile 里不存在的规则：拒绝，指出编号。
- **未知标签：** `members` 使用词表外的标签：拒绝，列出有效标签。
- **重复：** 标签 id 重复、`always` 内重复、同一规则的标签列表内重复：拒绝。
- **标签 id 形式：** 含中文、大写、超长或取保留名：拒绝，说明 ASCII 规则。
- **词表孤儿：** 没有任何成员规则的标签：拒绝。
- **本地化文字：** `name`、`trigger`、`exclude` 缺 `zh` 或 `en`、为空或多出键：拒绝。
- **版本一致：** `schema_version` 为 2.1 而 `loading` 含 `tags`，提示“需要 schema_version 2.2”；2.2 而没有 `loading`，因字段集合不符被拒。
- **读取时的两种非错误状态（Profile 有效）：**
  - 停用规则（`overrides` 置 `null`）使某标签的成员全部失效：标签为空集，`select` 报“没有有效规则（成员规则已停用）”，`index` 标 `state: "empty"`，`core` 索引不列出。沿用 2.1 的“组内无有效规则时不可用”语义。
  - 规则同时在 `always` 与某标签里：**归常驻**。该标签的按需成员不含此规则；标签的成员全部常驻时 `state: "resident"`，`select` 报“规则全部常驻，已随 context core 输出”，索引不列出。

### 4.3 选择语义

- 每条有效规则恰好归一处：常驻（`always`，或没有标签）；或按需（有至少一个标签且不在 `always` 里）。测试断言 `core` 输出加全部标签的 `select` 输出，每条规则恰好出现一次，且与 `OPINION.md` 里的规则块逐字一致。
- `select --tag A --tag B` 返回 A、B 成员规则的**并集**，按 `OPINION.md` 里的顺序各输出一次；常驻规则不在其中。同一标签重复书写等价于一次。
- `check` 的“已读取”也是并集：读取了标签 B 即读取了 B 的全部成员，因此声明涉及 A、已读取 B 且 B ⊇ A 时通过；只覆盖一部分时，缺失的规则编号逐条列出（`missing_rules`）。

### 4.4 命令（冻结的签名）

所有命令只读，不写文件；错误退出码 2 并附 `fallback`（完整读取 `OPINION.md`）。

```text
context core   --project P                      常驻规则 + 标签索引
context select --project P --tag A [--tag B …]  标签并集的规则文本（不含常驻规则）
context index  --project P                      JSON：常驻与逐标签的规则数、字节数
context check  --project P (--tag A | --signal touches=A)… [--loaded A …]
context bundle --project P <系列>               2.1 声明的旧行为，不变；2.2 下报错并指向 select
```

- `core` 输出：标题、Profile 行、说明、常驻规则、常驻自定义行、`## 标签索引`；索引每行 ``- `标签id`（显示名，N 条）：触发条件。不适用：排除条件。``，N 为规则数加自定义行数，只列可选的标签。
- `select` 输出：`# 按需规则：`A`、`B``，随后是规则块；命中的自定义行归在 `## 用户自定义规则` 下。
- `index` 的 JSON 字段：`resident`（规则编号与自定义行数）、`tags[]`（`id`、`name`、`state`、`rules`、`rule_ids`、`custom_lines`、`trigger`、`exclude`、`bytes`、`index_bytes`）、`on_demand_tags`、`rejected_custom_lines`、`resident_bytes`、`core_bytes`、`complete_bytes`、`profile_sha256`、`opinion_sha256`。`bytes` 是该标签单独 `select` 的输出字节，`index_bytes` 是它在 `core` 里的索引行字节（含换行），体积守卫据此逐标签断言。
- `check` 的 JSON 字段：`ok`、`signals`、`required`、`required_rules`、`loaded`、`missing`（缺失的标签）、`missing_rules`、`next`（可直接执行的 `context select …`）。派生词表：`touches` ∈ 标签 id ∪ {`none`}；`none` 只能单独声明，表示本次不涉及按需标签。
- 错误信息（均列出有效标签）：未知标签、空选择、`core`（常驻集合不可选）、全部常驻的标签、成员已停用的标签、`select` 带位置参数、`--tag` 用于 `core` / `index` / `bundle`。
- 文字遵循现有 i18n：框架文字随界面语言，规则正文与标签显示名随项目语言（en 项目的输出不含中文，测试覆盖）。

### 4.5 `check` 的限度

派生信号由 Agent 在交付时自报，只能发现“声明涉及某标签却没读该标签”的自相矛盾；识别任务类别的责任仍在 Agent。2.2 暂不支持手写信号词表；需要更细的产物类别时用更多标签表达。被排除：仅做存在性提示（只比较 `--loaded` 与索引），信息量更少，派生的实现成本接近零。

### 4.6 守卫数值如何测

T4 的守卫测试读取 `context index` 的 JSON：`core_bytes` ≤ 9,000（zh）/ 11,000（en）；`on_demand_tags` ≤ 6；每个可选标签的 `index_bytes` ≤ 160（zh）/ 200（en）。`index_bytes` 含行框架（约 40 B，zh）和换行，因此 trigger 与 exclude 的文字合计约 120 B 以内才能达标；该约束对 catalog 作者的实际可行性**未在真实 catalog 上验证**，由 T4 在 catalog 重做后用实测确认。

---

## 5. 自定义规则的标签

### 5.1 写法

行尾追加一个纯 ASCII 标记，标签按字母序、以单个空格分隔、不重复：

```text
- **标题**（适用：…；条件：…）：正文 [tags decisions files]
- **标题**（适用：…）：正文。 路由：…… [route skill=deck when=after] [tags reports]
```

- 与路由标记 ` [route …]` 并存时顺序固定为 `[route …] [tags …]`；`rule_lines.parse_line` 先剥离行尾标签标记，再按原有逻辑解析路由，因此路由行的往返还原不变。
- 标记与项目语言无关，zh、en 两种渲染格式都适用。

### 5.2 加载与归类

- 标记存在、行本身是 `parse_line` 认得的结构化格式、且所有标签都在词表内：该行属于这些标签，按需加载（`select` 的并集里出现，`core` 里不出现；并计入标签索引的条数）。
- **其余情形一律常驻：** 没有标记的行；手写行；格式认不出的行（即便末尾带 `[tags …]`）；标记损坏（大写、空格错乱、中文、空列表）；含词表外标签的行（词表后来缩小时旧行因此回到常驻，不会丢失）。`index` 的 `rejected_custom_lines` 统计后两类。
- 被分出去的行不出现在 `core` 的自定义段里；没有任何剩余行时该段不输出。
- `check` 把带声明标签的自定义行与规则一同计入“应读取”。

### 5.3 `inbox`

- `inbox add --tag A [--tag B]` 与 `inbox revise <id> --tag A …`：对照项目锁里的标签词表校验；没有词表（Profile 为 2.0、2.1）或含未知标签时报错并列出有效标签。`revise` 的 `--tag` 整体替换原标签集，`--tag-clear` 清空；二者不可同用。候选保存 `tags`（排序去重）。
- 候选行渲染：`candidate_rule` 在行末追加标记；没有标签的候选，渲染字节与旧版逐字节相同（测试以 `parse_line` → 渲染往返证明）。
- 采纳指纹：`fingerprint_input` 仅在候选带标签时把 `{"tags": [...]}` 并入摘要；没有标签的候选，指纹与旧版相同。因此预览后改标签会使确认指纹失效，需要重新预览。
- `inbox adopt` 的预览与确认流程不变：`adopt_plan` 只改 `custom` 文本，`loading` 由 `from_profile` 继承；它额外在预览阶段再校验一次标签仍在词表内。

---

## 6. 兼容

### 6.1 既有项目零变化

- 2.0（无 `loading`）与 2.1（规则束 `loading`）的 Profile：校验、指纹、`OPINION.md` 字节、锁格式、`context` 输出一律不变。
- 黄金测试：以固定夹具生成 2.0 与 2.1 项目，Profile 文件、`OPINION.md`、锁文件的 SHA-256 固定为常量；这六个常量先在 dev `5c6d28e` 上计算，再在改动后的代码上复核，两边一致。
- 2.2 只在 `--loading-file` 内容含 `tags` 时写入；`--remove-loading` 回到 2.0。
- `lifecycle` 读取 `loading.bundles` 处改为容忍缺失（2.2 没有该字段），既有 2.1 行为不变。

### 6.2 旧工具读 2.2（实测）

在 dev `5c6d28e` 的运行时上，对新工具生成的 2.2 项目运行，结果一致：旧工具**明确报错并回退为完整读取**，没有静默读取的路径。

- `context core --project P`、`context core --profile 文件`、`verify --project P`：退出码 2。
- 报错文字（zh 界面）：`Profile 字段必须为：['custom', 'derived_from', 'id', 'overrides', 'rules', 'schema_version', 'sources', 'templates', 'version']`；en 界面为 `Profile must have exactly these fields: [...]`。`context` 另附 `fallback`：完整读取项目根目录 `OPINION.md`。
- 这条报错说明字段集合不符，没有直接点出“版本过新”；旧工具的文案已发布，不能追改。新工具在 2.1 声明了 `tags` 时给出“需要 schema_version 2.2”的提示。
- `OPINION.md` 本身仍是完整、可直接阅读的正文，旧 Agent 退回完整读取不会欠读。测试 `test_old_tool_reading_a_22_profile_gets_an_explicit_error` 以旧校验的等价函数固定该行为；上述实测输出来自真实旧运行时。

---

## 7. 迁移（契约；`opinion_manager.py migrate` 由后续任务实现）

### 7.1 输入与目标

- **输入：** 既有项目，其 Profile 为 2.0 或 2.1，可能选用了 catalog 1.0.0 的模板（如 `scenario/reviewed-project@1.0.0`），`OPINION.md` 可能被人手工改过，`custom` 段可能含手写行或 `inbox adopt` 采纳的行，收件箱里可能有缺字段的旧候选。
- **目标：** 升级到带标签声明的 Profile（2.2）与对应的 catalog 版本，用户的规则与手工修改一条不丢，读取方式可回滚。
- **入口：** `opinion_manager.py migrate --project P`。

### 7.2 流程

1. **预览（默认，零写入）：** 读取锁与 `OPINION.md`，输出分类表（7.3）、将写入的 Profile 新版本与 `OPINION.md` 的差异、`loading` 的前后对照（沿用 `changes.loading_changed` 与 `loading.before/after`）、`confirmation_sha256`。预览前后项目目录的文件树逐字节相同，测试断言。
2. **确认：** 向用户展示完整预览并取得明确确认。
3. **应用：** `migrate --project P --apply --confirm <指纹>`。指纹覆盖新 Profile、渲染后的 `OPINION.md`、分类结果；预览之后任何输入变化（Profile、`OPINION.md`、收件箱、catalog）都使指纹失效，必须重新预览。
4. **备份：** 写入前把将被改写的文件原样复制到 `.opinion/backups/<时间戳>-migrate/`（`OPINION.md`、`opinion.lock.json`、原 Profile 与迁移涉及的收件箱文件），并写出 `manifest.json`（路径与 SHA-256）。
5. **写入与读回：** 复用现有的 `profile` → `compose` → `verify` 原子写与读回；任一步失败，按备份逐字节还原并退出非零。
6. **回滚：** `migrate --project P --rollback <备份目录>`：核对备份 `manifest.json` 与当前锁的 SHA-256，只在当前状态等于迁移后状态时还原；当前文件已被后续修改则拒绝并说明。

### 7.3 分类（预览逐项列出）

- **Profile 2.0（无声明）：** 升级为带标签声明的 2.2；声明由 catalog 新版模板携带的标签物化（T2）；没有可物化声明的模板组合保持 2.0，不强加分层。
- **Profile 2.1（规则束声明）：** 默认保持 2.1 不动，`context bundle` 继续可用；仅当用户在预览里选择改用标签时，才改写为 2.2（新旧声明互斥，改写后不再有 `bundles`）。
- **catalog 1.0.0 模板：** 按 `updates` 的现有预览升级到 1.1.0；rule 与 template 的升级仍须逐项确认，冲突沿用 `keep-local` / `use-upstream`。
- **手工修改过的 `OPINION.md`：** 沿用 `compose` 的阻断与 `--accept-current`：被改动的正文不被静默覆盖；`--accept-current` 把当前内容保存为人工修改版本后再继续。迁移不改写用户手工添加的任何字节之外的内容，`custom` 段原样保留。
- **`custom` 段：**
  - 结构化且带有效标签的行：保持原样（已带标签）。
  - 没有标记的结构化行、手写行、认不出的行：**保持原样，永远常驻**。迁移不替用户猜标签；预览只在“建议”栏列出可手工加标签的行（有 `trigger` 的结构化行）。
- **含缺字段的旧候选：** 候选记录不改写（`inbox` 的存储格式不变）；预览列出缺 `trigger` / `purpose` 的候选，沿用现有“不完整”提示，不阻断迁移。已采纳的行不回溯修改。

### 7.4 不变量（测试逐条断言）

1. 预览与失败的应用零写入：项目文件树的哈希与执行前相同。
2. 用户手工修改的每个字节、`custom` 段的每一行在迁移后仍存在于读取结果里（`core` ∪ 全部 `select` 的并集）。
3. 认不出的行与没有标记的行保持常驻，往返字节不变。
4. 迁移后 `verify` 通过；2.0、2.1 项目未选择迁移时字节不变。
5. 回滚后 Profile、`OPINION.md`、锁与迁移前逐字节相同。
6. 迁移不触碰 `.agents/` 下的项目配置与收件箱的候选内容。

### 7.5 失败与回滚

- 备份写入失败：不进入写入步骤，零改动。
- 任一写入或读回失败：自动按备份还原，输出失败原因与还原结果；还原本身失败时保留备份目录并给出手工还原命令。
- 重复执行：已是目标状态时返回 `unchanged`，不产生新备份。

---

## 8. 体积（沿用 #152 的实测数据，注明来源）

数据来自 PR #151 的 1.1.0 规则与模板，在长路径 worktree 中读取，不 checkout 到本分支；符号与公式不变。**以下数值是分组为 4 个互不重叠标签时的结果**，重叠标签的并集读取只会更小（并集去重）。

- `r_i`：规则 i 的渲染字节（`versioned.render_rule`，zh、en 分别计算）。
- `full = F0 + Σ r_i + 2·(n−1)`：完整 `OPINION.md` 字节；`F0` 为元数据、标题、导语，约 410 B。reviewed zh：`Σ r_i = 20,815`，`full = 21,225`。
- `core_out = F + Σ_{i∈常驻} r_i + 2·(k−1) + I`：`F` 为切片框架，实测 388 B（zh 界面）/ 419 B（英文界面）；`k` 为常驻规则数；`I` 为索引总字节。
- 索引上界 `I_ub`：把成员规则的 trigger、exclude 直接用“；”拼接（实测 2,535 B，窄集合 2,863 B）。索引目标 `I_tgt = 标签数 × 150 B`（zh）、`× 190 B`（en）：每个标签由模板作者写一句 trigger 与一句 exclude。目标为设计值，实测值以 T4 守卫为准。
- `bundle_out(g)`：按需标签单独 `select` 的输出，reviewed zh 实测：decisions 3,941、files 3,612、reports 4,512、maintenance 1,673（#152 数据，标签边界相同）。
- 典型读取量 `typical = core_out(I_tgt) + avg(select_out)`；节省 `1 − typical/full`。字节不等于 token：zh 约 3 B/字，实际 token 数**未测量**。

### 8.1 reviewed-project@1.1.0（宽集合：常驻 10 条）

- zh：完整 21,225 B；常驻规则 7,211 B；`core_out` 上界版 10,183 B、目标版 8,248 B。
- 典型读取：最轻 9,921 B（−53%），平均 11,682 B（−45%），最重 12,760 B（−40%）；读满 4 个标签 21,986 B，比完整读取多 761 B。
- en：完整 26,281 B；`core_out` 上界版 12,501 B、目标版 10,159 B；典型平均 14,412 B（−45%）。

### 8.2 窄集合与 concise-project

- reviewed 窄集合（常驻 8 条）zh：`core_out` 目标版 6,134 B，典型平均 10,097 B（−52%）。
- concise-project 宽集合（常驻 9 条）zh：目标版 7,203 B，典型平均 10,764 B（−48%）；en 目标版 8,934 B，典型平均 13,351 B（−48%）。
- 常驻集合是价值判断，由 catalog 重做时按 trigger 判据声明（附录 A 给出一组示例）。

### 8.3 与守卫数值的关系

- 守卫 `core` ≤ 9,000 B（zh）/ 11,000 B（en）：目标版（8,248 / 10,159）达标，上界版（10,183 / 12,501）超标，因此索引的书写质量决定能否达标，约 9 个百分点的节省取决于它。
- 守卫“按需标签 ≤ 6”：#152 的方案是 4 个。
- 收益有硬下限：常驻集合 5–7 KB 加框架与索引，典型读取不会低于约 8 KB（zh）。
- 与 1.0.0 对照：1.0.0 完整 14,767 B；宽集合目标版的典型读取（11,682 B）低于该值。
- 字符数与注入上限：zh 宽集合 `core_out` 4,177 字符；en 宽集合上界版 12,445 字符，目标版约 10.1K 字符，可能超过 Claude Code `additionalContext` 的 10,000 字符上限（见第 9.3 节）。

---

## 9. 读取与遵从的可靠性

### 9.1 工具能硬保证的

1. **锁与正文核验：** 每次 `context` 调用先 `verify_project`；失败退出码 2，返回 `fallback`。
2. **回退：** 没有声明、`opinion.loading_mode=full`，退出码 3，返回 `fallback`；回退内容是完整 `OPINION.md`。
3. **切片与正文逐字一致：** 切片由同一个 `render_rule` 生成；测试断言每条有效规则恰好出现一次。
4. **声明合法性：** 第 4.2 节的全部校验在 `profile` 预览阶段拒绝。
5. **`check` 对已声明标签的缺失检测：** 声明涉及某标签而未读取其规则，退出码 1，并列出缺失的标签与规则。

### 9.2 不能硬保证的

1. Agent 在会话开始是否运行 `context core`、命中触发条件时是否读取标签：全靠指令。Claude Code 文档称 `CLAUDE.md` 为“context, not enforced configuration”，“no guarantee of strict compliance”；AGENTS 文件的读取同理。
2. 交付信号由 Agent 自报；自报缺失或不实，`check` 无从发现。
3. 压缩后的保留：Claude Code 文档写明项目根 `CLAUDE.md` 在 `/compact` 后从磁盘重读，只存在于对话里的指令会丢失。`context core` 的输出属于对话内容，压缩后可能丢失；其他客户端**未确认**。
4. 读取行为的真实命中率：没有任何客户端实测数据，**未确认**。

### 9.3 本版不在会话开始注入 `context core`

- 可复用路径：`agent_pack_nudge.session_start` 已通过子进程 `run_manager()` 调 opinion-manager，并以 `additionalContext` 注入，`source == "compact"` 也会再次注入。
- 体积：Claude Code 对 `additionalContext` 单串上限 10,000 字符，超出则落盘、仅传 2,000 字符预览。zh 宽集合 4,177 字符可行；en 宽集合超限。
- 不做的理由：(1) `run_manager()` 把非零退出当失败，`fallback` 的退出码 3 要专门处理；(2) en 宽集合超限；(3) Codex 与 Cursor 的 `SessionStart` 注入语义**未确认**；(4) 没有 `routing_miss` 数据证明 AGENTS 指令不够。
- 后续：用灰度期 `routing_miss` 统计决定 6.15 是否注入；若注入，预算 9,500 字符内。

### 9.4 `routing_miss` 记账（T3）

- 记账者是写 `lifecycle event`（`use` 类型）的一方：Agent 自述，或复盘者发现应读未读后补记。运行时不据此阻断，只在转正式前阻断。
- 2.2 的变更（T3）：`loaded_bundles`、`routing_miss` 的合法集合扩展为标签 id；`status` 增加 `kind: "family" | "tag"`；从未被读取的标签出现在 `unused_bundles`。本版对 2.2 只保证不崩溃。

---

## 10. 决定清单

1. **D1 采用方案：** 单一 `OPINION.md` + 读取时切片 + 多对多标签选择。理由与被排除项见第 3 节。
2. **D2 声明来源：** 本版 Profile 的 `loading` 由 `--loading-file` 写入；后续任务让模板可选携带 `tags` 建议，选择模板时在预览里物化为 `loading`，由用户确认指纹后生效；已有手写 `loading` 不自动合并。被排除：运行时从模板直接派生、不进 Profile，因为既有 Profile 会在无确认下改变读取行为，违反“没有声明就完整读取”。
3. **D3 catalog 版本：** 标签声明并入尚未发布的 1.1.0（PR #151 重做时加），避免 1.1.0 之后紧接 1.2.0 迫使用户连升两次。被排除：1.2.0。
4. **D4 常驻集合：** 由 catalog 重做时声明，附录 A 给出按 trigger 判据的示例（宽集合）。本版只提供机制与夹具。
5. **D5 `check` 无手写词表：** 由标签派生 `touches` 词表（第 4.4 节）。被排除：仅存在性提示。
6. **D6 `custom` 段：** 带有效标签的行按标签按需加载，其余永远常驻（第 5 节）。被排除：整段常驻（用户规则无法分层）；按 trigger 字段自动转按需（误路由会让用户亲自确认过的规则被漏读，违反“认不出的行从不猜测”）。
7. **D7 会话开始注入：** 本版不做（第 9.3 节）。
8. **D8 预算数值：** zh `core` ≤ 9,000 B、en ≤ 11,000 B；按需标签 ≤ 6；索引每标签 ≤ 160 B（zh）、200 B（en）。被排除：不设预算（体积回退无人发现）。
9. **D9 Profile 版本：** 带标签声明写 2.2，其余保持 2.0、2.1。被排除：复用 2.1 加可选字段（版本号与能力不符，旧工具同样报错但文案指向 `loading` 字段，更难诊断）。
10. **D10 标签词表与 id：** 封闭词表，id 与规则编号一律 ASCII，中文只作显示名。被排除：开放词表（拼写差异悄悄产生永不被读取的标签）；中文 id（跨平台文件名、shell 引号与 Unicode 归一化风险，且本版不需要）。
11. **D11 发布等级：** 本 PR `opinion-manager` minor，`default_for_existing` 为 `advisory`；`agents-init`、`project-orchestrator` 不改模板正文；catalog 内容包按 `template_changes` 判定，由后续任务声明。

### 未确认清单

- Codex、Cursor 等客户端的会话开始注入与上下文压缩后的行为。
- 真实客户端下 Agent 对“命中触发条件时读取标签”的遵从率（无实测数据）。
- 字节到 token 的换算（未测量）。
- 其他客户端对 `additionalContext` 的体积上限（仅核实 Claude Code，抓取日期 2026-11-20）。
- 灰度期 `routing_miss` 的真实分布。
- 索引每标签 ≤ 160 B（zh）在真实 catalog 上的可行性（第 4.6 节）。
- 英文界面与 zh 界面的框架长度差（实测 31 B，已计入）；其他语言未测。

---

## 11. 实施拆分

依赖：T1（本 PR）冻结接口；T2、T3 在 T1 之后并行；T4 的守卫框架可与 T1 同步，真实夹具依赖 catalog 重做。

### T1 标签选择引擎（本 PR）

- 内容：`loading` 的 2.2 数据模型与校验；`context core|select|index|check`；`rule_lines` 的标签标记；`inbox add|revise --tag`；`lifecycle` 对 2.2 的容错；本设计文档定稿；`profile.schema.json` 与 `versioning.md` 同步。
- 测试：`plugins/opinion-manager/tests/test_tags.py`（黄金、校验失败路径、并集与去重、常驻不重复、`check` 缺失与退出码、en 无中文、custom 往返、长路径 `--project`、旧工具读 2.2）。

### T2 模板声明与预览物化（已实现）

见第 13 节：模板可选 `loading`，`profile` 预览物化 Profile 的 2.2 `loading`，`--no-tags`，`updates` 显示声明变化，已有手写 `loading` 只给建议；catalog 1.1.0 的两个场景模板声明了常驻集合与标签。

### T3 记账与文档同步

- `lifecycle` 接受标签 id、`status` 的 `kind` 与 `unused_bundles`；`SKILL.md`、`AGENT.template.md` 措辞；英文镜像与 i18n 守卫。

### T4 体积守卫与 catalog 重做

- `tests/test_loading_budget.py`：读取 `plugins/opinion-manager/catalog` 固定路径，zh、en 各一遍，断言第 4.6 节的数值；catalog 1.1.0 声明常驻集合与标签。

### T5 迁移命令

- `opinion_manager.py migrate`，按第 7 节的契约；含零写入与回滚的反例测试。

### 排到 6.15

- 会话开始注入；基于 `routing_miss` 的标签调整；手写信号词表。

---

## 12. 术语与口径说明

- “常驻 / 按需”指读取时机：常驻随 `context core` 输出，按需须命中触发条件后读取。
- “标签（tag）”是读取单元的成员关系；“规则组”等同于具名的标签集合；“规则束（bundle）”是 2.1 声明下系列级读取单元的叫法，`context bundle` 命令名不变。
- 体积一律为 UTF-8 字节，另标字符数的地方明确写“字符”。

---

## 13. 模板声明与预览物化

### 13.1 模板的 `loading`

- 模板在 `TEMPLATE_FIELDS` 之外新增可选字段 `loading`，形态与 Profile 2.2 的 `loading`（第 4.1 节）一致：`always` 列规则编号（不带版本），`tags` 为封闭词表，`members` 把规则编号映射到标签。校验与 Profile 共用 `check_tag_loading`，区别只在可引用的规则：模板只能引用自己 `rules` 里的规则编号（悬空编号、未知标签、重复、孤儿标签、非 ASCII 标签 id、缺 zh 或 en 的文字都在 `publish` 与 `catalog` 加载时被拒绝）。
- 没有 `loading` 的模板：物化后它的规则全部常驻，读取行为与此前相同。
- `loading` 属于模板内容，参与模板指纹（`digest`），因此只能随模板新版本发布；patch 版本不能改变它（与「patch 不能改变有效规则、适用范围」同一类约束）。1.0.0 文件不变。
- `references/versioned-template.schema.json` 增加 `loading`，引用 `profile.schema.json` 的 `tagLoading`。

### 13.2 合并语义（`template_loading`）

选择了多个模板时，按模板引用排序后合并：

- 词表按 id 合并，同一 id 的 `name`、`trigger`、`exclude` 必须逐字相同，否则预览报「标签定义冲突」，并列出每个冲突标签的来源模板。
- `always` 取并集；`members` 按规则编号取并集；没有声明的模板，其规则一律加入 `always`（任一模板要求常驻则常驻）；同一规则同时在 `always` 与标签里时归常驻，成员全部常驻的标签不收录，没有任何按需标签时不生成声明。
- 直接选择的原子规则和 `overrides` 停用或修订的规则：成员关系按规则编号，不受影响；停用后标签为空集，沿用第 4.2 节的 `empty` 语义。

### 13.3 预览物化

- `profile` 预览在新建 Profile 或模板选择变化时，由所选模板生成 Profile 的 `loading`（schema 2.2），结果出现在 `changes.loading_changed`、`changes.loading.before/after`，并通过 `loading_source` 标明来源。仍然是「预览 → 用户确认指纹 → 写入」，确认前不生效；`OPINION.md` 的正文不变，声明只进入 Profile 与锁。
- 模板选择不变的修订沿用原声明，不重新物化（不会因为一次无关修订改变读取行为）。
- 用户已有手写 `loading` 的 Profile：原声明与旧模板物化结果相同时随新模板更新；否则保持不变，只在预览的 `loading_suggestion` 与警告里给出建议，需要时用 `--loading-file` 整体写入。
- `--no-tags` 保持完整读取，不物化模板声明（取名与 Profile 的「标签」术语一致；设计首版曾拟用 `--no-groups`，规则组在本版只是具名标签集合，沿用「标签」避免两个名字指同一件事）。`--loading-file`、`--remove-loading` 与 `opinion.loading_mode=full` 的行为不变。
- `updates` 对每个可升级的模板给出各版本的声明摘要；`compare` 沿用 `loading_changed` 与 `loading.before/after`。从 1.0.0 升到 1.1.0 时，旧 Profile 为 2.0 无声明，预览物化出 2.2；手工修改过 `OPINION.md` 时 `compose` 阻断，纳入后用 `--accept-current` 确认，升级后 `verify` 通过。
- `untranslated` 的范围包含标签的 `name`、`trigger`、`exclude`；Profile 2.2 的词表要求 zh 与 en 俱全，本版 catalog 全部双语，缺译文只会出现在非 zh/en 完整的手写数据上，且只警告不阻断。

### 13.4 catalog 1.1.0 的声明

两个场景模板声明相同的四个按需标签与不同的常驻集合（规则编号以各自 1.1.0 引用的规则为准）：

- `reviewed-project` 常驻 10 条：`om.chinese-response`、`om.conclusion-first`、`om.response-dense`、`om.evidence-inference`、`om.bilingual-terms`、`om.markdown-structure`、`om.batch-questions`、`om.align-requirements`、`om.clean-deliverable`、`om.tables-list-first`。
- `concise-project` 常驻 9 条：把 `om.response-dense` 换为 `om.response-brief`，不含表格规则；`om.tables-compact` 归 `files`（与附录 A 一致）。
- 标签：`decisions`（5 条）、`files`（reviewed 4 条、concise 5 条）、`reports`（6 条）、`maintenance`（2 条）。规则文件不改，标签成员关系只在模板里。
- 实测（`context index` 的真实输出，字节）：reviewed zh 完整 21,225、`core_bytes` 8,208；reviewed en 完整 26,281、`core_bytes` 10,059；concise zh 完整 20,687、`core_bytes` 7,181；concise en 完整 25,710、`core_bytes` 8,853。四个标签的 `index_bytes` 为 zh 126–148、en 139–182，均在 160 / 200 的守卫内；按需标签数 4。

---

## 附录 A：逐条规则的渲染字节与示例标签

字节为 `versioned.render_rule` 的 UTF-8 字节：`zh / en`。分组为按 trigger 判据的一个示例（宽集合）；标签支持多对多，下列示例用互不重叠的归属，实际声明由 catalog 重做时决定。

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

标签合计（zh / en，条数）：baseline 7,211 / 8,962（10）；decisions 3,905 / 4,910（5）；files 3,582 / 4,430（4）；reports 4,476 / 5,485（6）；maintenance 1,641 / 2,055（2）。

**scenario/concise-project@1.1.0 与上表不同的规则**（其余 23 条相同）

- `response-brief`：647 / 790｜baseline｜回答问题，尤其是复杂问题时（取代 `response-dense`）
- `solutions-proportional`：763 / 954｜decisions｜解决问题并需要给出方案时（取代 `solutions-three-level`）
- `tables-compact`：773 / 951｜files｜需要用表格呈现对比或数据时（取代 `tables-list-first`，范围较窄，按需）
- `maintenance-project-language`：745 / 984｜maintenance｜编写、维护或翻译第一方 Skill、模板与维护文档时（取代 `maintenance-chinese`）

标签合计（zh / en，条数）：baseline 6,168 / 7,739（9）；decisions 3,676 / 4,615（5）；files 4,355 / 5,381（5）；reports 4,476 / 5,485（6）；maintenance 1,603 / 2,052（2）。

## 附录 B：复算方法

在 PR #151 分支的 worktree 里，对每个模板：用 `versioned.load_catalog` 读取规则，用 `versioned.render_rule(profile, rule, lang)` 取每条字节；用 `context.partition` 的替身返回上述分组，调用真实的 `context.core_text`、`context.bundle_text` 得到 `core_out`、`bundle_out`（互不重叠的标签下，它们与 `tags.core_text`、`tags.select_text` 的输出等价，仅框架与索引行格式不同）；`I_ub` 为组内 trigger、exclude 以“；”拼接后的索引行字节之和，`I_tgt` 按 4 × 150 B（zh）、4 × 190 B（en）替换 `I_ub`。完整 `OPINION.md` 字节取 `versioned.render(profile, lang)`。所有数值可由这些函数在同一分支重现；脚本为一次性测量用，未提交。

## 术语对照表

- 渐进式加载（progressive disclosure / progressive loading）：按需分阶段把内容载入上下文。
- 常驻层 / 按需层 / 查证层：always-loaded / on-demand / verification layer。
- 标签（tag）：规则的读取成员关系；规则组 / 规则束（rule group / bundle）：具名标签集合 / 2.1 的系列级读取单元。
- 标签选择器（tag selector）：按标签集合运算返回规则文本的读取接口。
- 触发条件（trigger）、不适用（exclude）、目的（purpose）：规则的适用说明字段。
- 回退（fallback）：校验失败或无声明时完整读取 `OPINION.md`。
- 锁（lock，`opinion.lock.json`）：记录 Profile 快照与正文指纹的文件。
- 指纹（fingerprint）：SHA-256 摘要。
- 路由漏读（`routing_miss`）：本应读取却没有读取的规则束记录。
- 灰度（rollout）：候选规则在受控范围内的试用阶段。
- 内容包（content pack）：随插件分发、按 `template_changes` 判定版本的内容集合。
- 钩子（hook）：客户端在会话事件上调用的外部命令。
- `additionalContext`：hook 向模型上下文注入的文字字段。
