# opinion 作为 skill 路由触发器：设计

维护者的新需求（暂无 issue 编号）：opinion 承担「触发器」，用户做某类事时默认启用某个 skill 并路由过去，例如「所有文字工作最终都用一个去 AI 味的 skill 处理」。依赖 O 组的四项字段（[#121](https://github.com/lc4t/agenthelm/issues/121)）与 M1 的只读 `agenthelm inventory`。基线 `dev`（6.13.1-rc.1 之后），核对日期 2026-11-18。**本文只设计，不实施；不改任何代码、版本、片段或发布载体。**

**领域概述。** 让 Agent 在合适的时机调用合适的能力，业界目前有两种机制：能力自带的触发描述（Agent Skills 把 `name` 与 `description` 常驻上下文，由模型按匹配度决定是否加载正文）与外部的指令层（项目规则文件、Hook 等）。通行的好做法是：触发描述写清「做什么、何时用」，指令层只写约束与优先级，并用可观测的记录验证模型是否照做。「好」的判据来源有两类：Agent Skills 官方文档对渐进式加载与描述触发的说明，以及可复现的行为观测（本仓库的 lifecycle `use` 事件记账）。两者都**无法保证**模型一定照做，所以设计必须把「软约束」写在前面。

---

## 0. 结论摘要

1. **可以建模为「带 skill 引用的规则」，并建议采用。** 规则的适用条件与不适用于复用 O 组的 `--trigger` / `--exclude`，目的复用 `--purpose`；新增的只有对 skill 名的引用、时机（`before` / `after` / `instead`）和一个「最终」标记。规则仍是 `OPINION.md` 自定义段里的一行渲染文字，**不改 Profile schema**；行内带一个纯 ASCII 的机器可读标记 `[route skill=… when=… on=…]`，供检查命令解析。
2. **与 skill 自带的 description 触发叠加，不互斥。** 描述触发由模型按匹配度判断；opinion 规则是每次会话读到的额外约束。真正的冲突来源有五类（第 3 节），其中**机械可检**的是：目标不存在 / 未启用、同一 `on` 标签下同一时机路由到不同 skill、重复路由、直接自指循环（由生成方式杜绝）；**只能由 Agent 或用户判断**的是：多个 skill 描述语义重叠、不同标签的触发范围交叠、间接循环、路由目标是否真的适合该任务。
3. **存在性交叉检查由 agenthelm 的 CLI 层用管道完成：** `agenthelm routes check` 子进程调用 `agenthelm inventory --json`，把裁剪后的 skills 子集通过 stdin 交给 `opinion_manager.py routes check --inventory -`。opinion-manager 拥有路由标记的解析，agent-pack 拥有 inventory；两个插件只经子进程互调，互不 import。输出契约见第 4 节。
4. **对模型的约束力是软的。** 规则能提高调用概率，不能保证。可观测验证：用 lifecycle 的 `use` 事件记录每次适用路由规则是否被执行（本版用现有 `results` 字段，下一版加 `routes` 字段），按「路由遵从率」评估，数值没有经过验证。
5. **体积预算：** 每条路由规则行 ≤500 字节（zh 样例实测 469 字节，en 477 字节），每个项目 ≤6 条，`OPINION.md` 增量 ≤3 KB；会话开始注入的 inbox brief（现测 zh 1,937 字节，空收件箱，本分支实测；维护者给出的 1,950 字节同量级）**对正式规则增量为 0**，仅试行中的路由候选全文进入，受 brief 既有 6000 字符上限约束。
6. **拆分：** 本版只做设计文档加「V0 文字约定」（零代码）；字段与渲染（V1）、检查命令与 inventory 管道（V2）放下一版；lifecycle `routes` 字段、环检测、Hook 型强制放再下一版。依赖：V1 依赖 O 组字段合入；V2 依赖 M1 inventory 契约稳定。
7. **既有项目：** `default_for_existing: off`，没有路由标记的规则一律不受影响。

决策项见第 9 节，未核实清单见第 10 节。

---

## 1. 现状（已读源码与文档）

- 规则载体：正式规则是 `OPINION.md` 自定义段的一行渲染文字，由 `inbox.py` 的渲染函数输出：`- **标题**（适用：…）：正文`（`plugins/opinion-manager/runtime/inbox.py` 约第 243 行）。O 组计划把「适用条件 / 目的 / 不适用于 / 关系备注」并入这一行。Profile schema 不变。
- lifecycle：`use` 事件有 `occurrence`、`rules`、`passed`、`user_changed`、`fixture`，可选 `results`（每条规则 `passed` / `not-applicable` / `violated`）、`loaded_bundles`、`routing_miss`、`date`（`references/versioning.md`「候选、灰度与使用记录」）。`routing_miss` 已用于「应读取却未读取的规则束」，语义相近但对象是规则束。
- 会话开始注入：`inbox brief` 输出协议与试行候选全文，默认上限 6000 字符；空收件箱实测 zh 1,937 字节（本分支运行 `inbox brief --lang zh` 得到的 `text` 字段）。
- 两个插件边界：opinion-manager 不 import agent-pack，只通过子进程互调（任务约束）。
- 官方文档对 skill 触发的说明（读取日期 2026-11-18）：`description` 是 Claude 匹配请求以决定是否触发 skill 的依据；元数据常驻上下文，约 100 token 每个 skill；正文在触发时才加载（[Agent Skills 概览](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)）。规范要求 `name` 仅含小写字母、数字、连字符，≤64 字符（[规范](https://agentskills.io/specification)）。

## 2. 问题 (a)：建模为「带 skill 引用的规则」是否合适

### 2.1 备选方案穷举

| 方案 | 做法 | 评价 |
| --- | --- | --- |
| **A1 带路由标记的规则（推荐）** | 规则一行里含自然语言约束与 `[route skill=… when=… on=…]` 标记；字段复用 O 组 | 可见（模型每次都读到）、可审计、可由工具机械解析标记；代价是占用 `OPINION.md` 体积 |
| A2 纯文字规则（V0） | 在 `--text` 里自然语言写「用 skill X」，无标记 | 零代码、本版即可用；工具无法检查存在性与冲突；作为过渡 |
| A3 Profile 新增 `routes` 字段 | schema 增加结构化数组 | 排除：改 schema 影响所有既有项目的兼容与校验，且 O 组已决定不改 Profile schema |
| A4 独立文件 `.opinion/routes.json` | 路由表与规则分开存放 | 排除：形成第二个事实来源，模型读的是 `OPINION.md`，两份会漂移 |
| A5 自写「编排 skill」 | 用户自己写一个 skill，description 触发「文字工作」，正文里调用去 AI 味 skill | 是真正的竞争方案：用原生触发机制与渐进式加载，不占 `OPINION.md`。缺点：触发同样是概率的，维护由用户承担，不进入 opinion 的候选 / 灰度 / 覆盖记账。可作为 A1 预算不够时的后备 |
| A6 Hook 型确定性注入 | agent-pack 的 hook 在用户提示或工具事件上用规则匹配后注入提醒 | 强制力最高，但匹配用关键词 / 正则，对「所有文字工作」这类语义条件误判多，且依赖客户端 hook 能力（Cursor / Codex / Claude Code 各不同，**未确认**）。放下一版以后评估 |
| A7 修改第三方 skill 的 description | 把触发扩大到所有文字工作 | 排除：改动他人文件，升级即丢失，且改变 skill 自身行为 |

### 2.2 为什么 A1 合适

- 路由本质是「用户和 Agent 之间的行为规则」：在什么情景（`trigger`）、为什么（`purpose`）、哪些情景不用（`exclude`）、怎么做（调用某 skill，时机如何）。与 O 组字段一一对应，没有新增概念，只多一个引用对象。
- `purpose` 解决环境耦合问题：规则引用的 skill 是环境相关的（某台机器没装、某个项目未启用），规则渲染里固定带一句降级条款：「该 skill 不可用时，告知用户并按目的自行完成」，Agent 有明确的回退行为，而不是静默失败（这一句由渲染器生成，用户不必每次写）。
- 与官方「叠加」模型一致：规则不替代 description 触发，只补充用户的偏好与优先级。

### 2.3 规则字段与渲染

新增三个路由字段（`inbox add` 的新选项，名称待定，D2-2）：

- `--route-skill NAME`：skill 名，校验 `^[a-z0-9]+(-[a-z0-9]+)*$`、≤64（按规范）；若允许带插件命名空间（`plugin:skill`），**未确认**客户端的命名形态，先只接受纯名称（D2-2）。
- `--route-when before|after|instead`：
  - `before`：在开始主体工作前使用（风格指南、检查清单类）；
  - `after`：主体完成后、交付前，把成果交给该 skill 处理（去 AI 味类）；
  - `instead`：用该 skill 取代默认做法。
- `--route-final`：只与 `after` 合用，表示「在交付前最后一步执行，其后不再改动该成果的文字」。这是对「最终」的表达：它是时机的一个强度修饰，不是第四种时机。
- `--route-on TAG`：可选，用户自定义的短标签（`^[a-z0-9-]{1,32}$`），例如 `text-deliverable`。**标签是用户自己的受控词汇**，只用于机械比较；不给标签时，机械冲突检查只能按触发文字规整后完全相同来比较，覆盖面更小。

渲染（zh 样例，469 字节，实测）：

```
- **文字成果最后统一去 AI 味**（适用：产出面向人的文字成果，如报告、回复草稿、文档；目的：减少模板化表达；不适用：代码、配置、命令输出、该 skill 自身的执行过程、用户要求保持原文）：主体完成后、交付前最后一步使用 skill humanizer-zh 处理文字；该 skill 不可用时告知用户并按目的自行完成。[route skill=humanizer-zh when=after final=1 on=text-deliverable]
```

- 标记使用 ASCII 键值，与项目语言无关，`en` 项目不会出现中文（写进项目文件的文字按 Project Profile 的 `language`，标记之外的文字由渲染器按语言生成）。
- 标记使用空格分隔、固定键序，便于一个正则解析；解析只在 `OPINION.md` 的自定义段内、以 `- ` 开头的行末匹配，避免误伤用户手写正文。
- 渲染器对每条路由规则自动在 `exclude` 中加入「该 skill 自身的执行过程」。**这就是「所有文字工作最终都用去 AI 味 skill」会自我递归的解法：** 去 AI 味 skill 的输出本身也是文字工作，如果不排除，规则会在它自己的输出上再次触发。由构造杜绝直接自指，不依赖用户记得写。
- 渲染器同时加入「被路由的 skill 执行期间不再触发其他路由规则」，限制路由深度为一层（软约束，写在规则文字里）。

「所有文字工作」的建议写法：`trigger` = 产出面向人的文字成果；`exclude` = 代码 / 配置 / 命令输出 / 保持原文 / 引用原话；`when=after`，`final`。避免写成「所有场景」：O 组的软必填会提示范围过宽，全局只能走标准值并带 `--quote`（用户原话）。

## 3. 问题 (b)：与 skill 自带描述触发的关系与冲突

### 3.1 关系：叠加

官方文档：`description` 常驻，模型按匹配度决定是否加载 skill 正文。opinion 规则是 `OPINION.md` 的额外文字，Agent 实施前按指令读取。两条通道各自作用，对同一任务可能都指向同一个 skill（一致，无冲突）、都不指向（无路由）、指向不同 skill（冲突）。

### 3.2 冲突来源与可检性

| # | 冲突来源 | 机械可检？ | 检查方式 / 判断者 |
| --- | --- | --- | --- |
| 1 | **多个 skill 的 description 争同一触发** | 否（语义） | 无法由工具判定。inventory 的「重复」能发现同名 / 同内容重复，不能发现语义重叠。可给词汇重叠度作提示，**仅提示，不作结论**（误报率未测）。判断者：Agent / 用户。路由规则反而是解决手段：用 `instead` 指定谁优先 |
| 2 | **规则路由到的 skill 不存在，或在本项目作用域 / 当前客户端未启用** | 是（依赖 inventory） | `routes check` 与 inventory 比对：状态 `found` / `found-other-scope` / `found-other-client` / `missing` / `unknown`（未提供 inventory 或证据等级不足） |
| 3 | **多条规则把同一触发路由到不同 skill** | 部分 | 机械：同一 `on` 标签、同一 `when`（`instead` 与 `before`/`after` 视为不同时机）下目标不同；同一标签下多条 `final`；同一标签下多条 `instead`。不可机械：未设标签、标签不同但触发范围交叠（语义）。判断者：Agent / 用户 |
| 4 | **规则互相触发形成循环** | 部分 | 机械：直接自指由渲染器排除；规则文字声明的路由深度为 1。间接循环（A 的输出触发 B，B 的输出触发 A）需要知道每个 skill 的产出类型，目前不存在这类声明；下一版可选加 `--route-emits TAG` 并做图检测（D2-6）。判断者：Agent / 用户 |
| 5 | **同一 skill 被多条规则重复引用、时机冲突** | 是 | 同 skill 同 `on` 下 `before` 与 `instead` 并存等组合报警 |
| 6 | **路由目标的行为与规则目的不符**（skill 升级后功能变化） | 否 | 规则按名称引用，不固定版本；`routes check` 不判断。若 inventory 提供内容哈希，可在采纳时记录并在漂移时提示（是否提供**未确认**，D2-5） |

**不承诺工具能判定语义冲突。** 机械检查只处理标记、标签与 inventory 里的存在性；其余在展示时交给 Agent 做初判、用户裁决，沿用 O 组的 `--relation`。

## 4. 问题 (c)：存在性交叉检查由谁做，输出契约

### 4.1 备选与选择

- **C1 opinion-manager import agent-pack**：违反边界，排除。
- **C2 agenthelm CLI / 菜单层做管道（推荐）**：`agenthelm routes check [--project P]`：
  1. 子进程运行 `agenthelm inventory --json`（M1，只读）；
  2. **适配层在 CLI 内**把 inventory 的完整输出裁剪、改名为 opinion 自己的最小输入格式（见 4.2），丢弃 MCP 部分与一切含密钥的字段；
  3. 子进程运行 `opinion_manager.py routes check --project P --inventory -`，stdin 传入裁剪后的 JSON；
  4. 原样转出检查结果。
  好处：opinion 的输入契约与 M1 的 schema 解耦，M1 字段改名只需改适配层；opinion 完全不见 MCP 与密钥。
- **C3 反过来：agent-pack 向 opinion 要 `routes list`，自己做比对**：路由标记的解析会泄露到 agent-pack，由两个插件共同维护同一语法；排除。（`routes list` 仍保留，供 Agent 和 CLI 读取路由清单。）
- **C4 Agent 手工比对（V0）**：Agent 读 `OPINION.md` 与 `agenthelm inventory` 输出自行比对。可用作过渡，但不可复现，且依赖模型自觉。

### 4.2 契约

输入（opinion 自己的最小子集，`--inventory FILE|-`）：

```json
{"schema_version": 1, "skills": [
  {"name": "humanizer-zh", "scope": "user|project|plugin", "client": "claude|codex|cursor|…",
   "enabled": true, "evidence": "declared|observed|…", "project": "<项目标识，可空>"}]}
```

- `schema_version` 不支持时拒绝并给出明确错误，不猜测。
- 只读取以上字段；其他字段忽略且不回显。**输出中任何位置不出现 inventory 的未知字段值，也不出现路径之外的 inventory 内容。** 不输出密钥值（M1 同样承诺；opinion 侧再做一次字段白名单作为第二道防线）。
- M1 inventory 的实际字段名、作用域枚举与证据等级取值**未确认**，上表为 opinion 侧需求；适配层负责映射，映射不到的字段按 `unknown` 处理。

输出（`routes check --output json`）：

```json
{"ok": true, "schema_version": 1, "checked_at": "…Z",
 "inventory": {"provided": true, "schema_version": 1, "skills_seen": 14},
 "routes": [
   {"rule": "cand-20261118-003", "line": 27, "skill": "humanizer-zh", "when": "after", "final": true,
    "on": "text-deliverable",
    "target": {"status": "found|found-other-scope|found-other-client|missing|unknown",
               "matches": [{"scope": "user", "client": "claude", "enabled": true, "evidence": "…"}]},
    "issues": [{"code": "target_missing|duplicate_route|same_tag_different_skill|multiple_final|…",
                "severity": "warn|error", "detail": "…", "related": ["cand-…"]}]}],
 "conflicts": [{"code": "same_tag_different_skill", "on": "text-deliverable", "rules": ["cand-…", "cand-…"]}],
 "unchecked": ["semantic_overlap", "indirect_cycle", "skill_behavior_drift"],
 "summary": {"routes": 1, "errors": 0, "warnings": 0}}
```

- `unchecked` 如实列出本命令不检查的类别，避免用户把「无冲突」读成「已全面验证」。
- 严重级别：解析失败（标记损坏、skill 名非法）为 `error`；其余为 `warn`。**退出码默认 0，`--strict` 时有 `error` 才非 0。** 路由是软约束，不应阻断 `verify` 或发布。
- 无 inventory 时：`routes check` 仍执行规则侧检查（#3、#5、标记合法性），目标状态全部为 `unknown`，`inventory.provided = false`。
- 无写入：整个命令只读。

## 5. 问题 (d)：约束力与可观测验证

**如实结论：路由规则对模型的约束是软的。** 模型是否调用该 skill，取决于它是否读到并遵从规则、规则与 description 是否一致、上下文里其他指令是否压过。本设计没有任何机制能保证调用发生；**不要在产品文案或 Skill 协议里写成「保证」「强制」**。

可观测验证（由弱到强）：

1. **自报（本版即可，零代码）：** Agent 在任务结束后提交 lifecycle `use` 事件，对每条适用的路由规则在 `results` 记 `passed`（确实调用并采用）/ `not-applicable` / `violated`（应调用而未调用）。`violated` 按现有规则使该版本不能转正。局限：自报依赖 Agent 诚实与记忆。
2. **事件字段（下一版）：** `use` 增加可选 `routes: [{rule, skill, invoked: true|false, evidence}]`，`evidence` 指向可核对的工作记录（例如会话记录中的 Skill 调用）。这一步是对 lifecycle 的小改动，不属于本版。
3. **客户端侧记录（未确认）：** 若 agent-pack 的 hook 能取得「Skill 被调用」的事件，可独立于 Agent 自报核验。各客户端是否提供、是否可靠，**未确认**；只在确认后设计。
4. **用户抽查：** 在复盘视图里列出最近 N 次适用路由规则的任务，用户对照会话记录看是否真的路由。

指标与判据：

- **路由遵从率** = `invoked` 的适用次数 / 适用次数（排除 `fixture`），每条路由规则单独计。
- 样本不足时不下结论：建议每条规则至少 10 个独立真实适用后再看；沿用 lifecycle 现有「至少两个独立真实成功使用才 `ready_for_review`」的下限作为**最低门槛**，路由遵从率是附加指标。10 次与具体阈值（例如遵从率 ≥80% 才算「有效」）**没有经过验证**，是默认参数。
- 对照实验（可选）：同一类任务，分别在有 / 无该规则下运行若干次，比较调用率，估计规则的净效果。其中无规则组仍可能因 description 触发而调用，这正是「叠加」的度量。

## 6. 问题 (e)：对 OPINION.md 与会话注入体积的影响

| 项 | 数值 | 来源 |
| --- | --- | --- |
| 单条路由规则行（zh） | 469 字节（229 字符） | 本文样例，`python3 len(s.encode())` 实测 |
| 单条路由规则行（en） | 477 字节 | 同上 |
| 当前 inbox brief（zh，空收件箱） | 1,937 字节 | 本分支 `inbox brief --lang zh` 的 `text` 字段；维护者给出 1,950 字节，量级一致 |
| brief 上限 | 6000 字符 | `inbox.py` `--max-chars` 默认值 |

预算（均为建议值，D2-4）：

- **单条 ≤500 字节**（含标记 65 字节，含降级条款与自我排除）。超过时校验给 `warn`。
- **每个项目 ≤6 条路由规则**；`OPINION.md` 增量 ≤ 6 × 500 = 3,000 字节。公式：`Δ = Σ 每条路由规则行字节数`。
- **会话开始注入：** 正式规则不由 brief 注入（`OPINION.md` 由指令要求实施前读取，既有路径），所以 brief 增量为 0。试行中的路由候选全文会随 brief 注入，增量同样按 ≤500 字节每条计，总量受 6000 字符上限保护；如需更紧，路由候选在 brief 中只注入「标记 + 标题」（约 120 字节），全文留在收件箱（D2-4）。
- **与分层加载的关系：** Profile 声明分层加载时，路由规则应归入某个规则束并设置触发条件，避免核心规则变大（`versioning.md` 分层加载一节）。路由规则的触发条件本来就是「某类任务」，天然适合放进对应规则束。
- **token 换算：** 字节到 token 的换算随模型与语言变化，**未确认**，不在本文给出；预算按字节表述，实施时用客户端的实测补充。
- skill 本身的元数据成本（官方文档：约 100 token 每个 skill，常驻）属于 skill 一侧，不计入 opinion 预算，但 inventory 的「description 加载成本」可帮用户看到合计。

## 7. 问题 (f)：实现拆分与等级建议

| 阶段 | 内容 | 本版 / 下一版 | 依赖 | 预计等级 |
| --- | --- | --- | --- | --- |
| V0 | 文字约定：在 opinion-manager 的 `references` 增加「路由规则写法」一节，Agent 用 O 组字段写 `--trigger / --purpose / --exclude`，正文里自然语言引用 skill，不含机器标记；`use` 事件用现有 `results` 记账 | **本版可做（仅文字，零代码）** | O 组字段合入（文字引用其选项名） | 文档与 Skill 文字；是否需变更片段以门禁实测为准，未确认 |
| V1 | `inbox add --route-skill/--route-when/--route-final/--route-on`、渲染器（降级条款、自我排除、标记）、`routes list`（只读，仅规则侧检查）、体积校验 | 下一版（O 组字段与渲染合入后；若 O 组本版较早合入，可评估并入本版末期，见 D2-6） | O 组 `--trigger/--purpose/--exclude` 与渲染函数 | opinion-manager `minor`，`default_for_existing: off` |
| V2 | `routes check --inventory`、`agenthelm routes check` 管道与适配层、输出契约、测试 | 下一版 | M1 `agenthelm inventory` 输出契约稳定（含 `schema_version`） | opinion-manager `minor` + agent-pack `minor` |
| V3 | lifecycle `use.routes` 字段与复盘视图；`--route-emits` 与环检测；hook 型验证 / 强制（A6）评估 | 再下一版 | V1、V2、hook 能力调研 | 各 `minor` |

不在本版做 V1 的主要理由：O 组正在同一份 `inbox.py` 与渲染函数上增加字段，两条线同时修改同一函数的合并风险高于收益；V0 已足够让维护者在文字层试用路由并积累 `results` 数据。若维护者希望本版就有工具支持，V1 的前置是 O 组合入，且单独成 PR。

## 8. 问题 (g)：对既有项目的影响与 `default_for_existing`

- **`default_for_existing: off`。** 没有路由标记的 `OPINION.md` 行为不变；`routes list` / `routes check` 在没有路由规则时输出空列表。
- 解析器只认以 `- ` 开头、行末含完整 `[route skill=… …]` 标记的行，既有手写正文即使含「skill」字样也不受影响；标记损坏只报该行 `error`，不影响其他规则、`verify` 与锁。
- 既有锁与 Profile 指纹不变（路由规则属于自定义段文字，随 O 组已有流程更新指纹）。
- 路由规则带环境耦合：同一 `OPINION.md` 在另一台机器 / 另一个项目未安装该 skill 时，规则内的降级条款生效（告知用户并按目的自行完成），`routes check` 报 `missing` 或 `found-other-scope`。多项目多机场景（#82）下，建议把路由规则放项目级而非全局，避免规则跟随到未启用该 skill 的项目。
- 公开 catalog：路由规则引用的 skill 名属于环境信息；默认**不得发布**带 `[route …]` 的规则到公开 catalog，除非引用的是公开可得的 skill 且逐文件审批（D2-8）。公开规则的中英双语与三项字段按 O 组方案。
- 隐私：路由规则文字本身不含个人内容，但 skill 名可能暴露用户的工具选择；与其他规则一样保留在用户私有环境。

## 9. 决策项（需要维护者拍板）

- **D2-1 建模方案。** 倾向：A1（带标记的规则），预算不足时回退 A5（自写编排 skill）。替代视角：A5 作为主方案（不占 `OPINION.md`，用原生触发），代价是不进入 opinion 的灰度 / 覆盖记账且触发同样概率化；A6（hook 强制）强制力更强但对语义条件误判多、依赖客户端能力，推迟评估。
- **D2-2 选项与标记语法。** 倾向：`--route-skill / --route-when / --route-final / --route-on`；标记 `[route skill=NAME when=after final=1 on=TAG]`，纯 ASCII；skill 名先只接受纯名称，不接受 `plugin:skill`（官方文档未读到该命名形态，未确认）。替代：把标记放在规则行首；或用 YAML 风格 `{route: …}`（与 Markdown 冲突概率更高）。
- **D2-3 「最终」的表达。** 倾向：`after` 加 `final` 修饰，每个 `on` 标签最多一条 `final`。替代：新增时机值 `last`（语义更直白，但让时机枚举变成不对称）。
- **D2-4 预算。** 倾向：单条 ≤500 字节、每项目 ≤6 条、增量 ≤3 KB；试行候选在 brief 中先只注入短形式。替代：不设上限只告警（体积会无声增长）。数值**没有经过大量使用验证**。
- **D2-5 交叉检查所在层与 inventory 内容哈希。** 倾向：C2（CLI 层管道、适配层裁剪）。替代：C4 仅 Agent 手工比对（V0 的过渡）。是否要求 M1 提供 skill 内容哈希以检测漂移：倾向「有则记录，无则不要求」，需要与 M1 对齐。
- **D2-6 拆分与是否把 V1 提到本版。** 倾向：本版 V0，V1 与 V2 下一版。替代：O 组较早合入后，V1 在本版末期并入（需要单独 PR 且不与 O 组同时改渲染函数）。环检测（`--route-emits`）放 V3 的倾向与替代：现在就做（成本高、收益小，不推荐）。
- **D2-7 违规的处理强度。** 倾向：路由检查默认只警告，`--strict` 才非 0 退出；`use` 的 `violated` 沿用现有「阻止转正」。替代：对 `missing` 目标阻断采纳（会使无该 skill 的机器无法采纳共享规则，不推荐）。
- **D2-8 公开 catalog。** 倾向：默认禁止带路由标记的规则公开。替代：允许引用公开 skill，需逐文件审批。
- **D2-9 验证强度。** 倾向：本版自报 + 用户抽查；下一版加 `use.routes`；hook 级核验待确认客户端能力后再定。替代：现在就要求自报带证据指针。

**我擅长：** 标记语法与数据契约、冲突分类、预算测算、测试设计。**需要维护者拍板：** D2-1（产品形态与取舍）、D2-4（体积偏好）、D2-7（强度）、D2-8（公开策略）；「所有文字工作都走去 AI 味 skill」是否真的符合预期效果，属于使用体验判断，需要用 V0 的遵从率数据与实际成果来评价。

## 10. 未核实清单

1. M1 `agenthelm inventory` 的 JSON 字段（名称、作用域枚举、客户端取值、证据等级取值、是否含内容哈希）。
2. O 组渲染函数的最终形态（规则行是否把四项字段写进同一行、分隔方式）与 `--relation` 的结构。
3. 客户端是否提供「Skill 被调用」事件供 hook 取得，各客户端差异。
4. 路由规则对模型的实际约束力：未测，没有任何遵从率数据。
5. skill 的插件命名空间在各客户端中的写法与解析（`plugin:skill` 之类）。
6. 字节到 token 的换算，以及体积对任务质量的影响。
7. 词汇重叠度提示的误报率（若实施）。
8. `check-changes` 对 V0 文字变更是否要求变更片段（以 `ci_gate` 实测为准）。
9. description 触发与 opinion 规则同时命中时模型的实际优先关系：官方文档只说明 description 用于匹配触发，未规定与外部指令的优先级。

## 11. 引用与读取日期（读取日期均为 2026-11-18）

- Anthropic，Agent Skills 概览（描述触发、元数据常驻约 100 token、渐进式加载、安全说明）：https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- Agent Skills 规范（`name` / `description` 约束与渐进式加载）：https://agentskills.io/specification
- 本仓库：`plugins/opinion-manager/runtime/inbox.py`、`plugins/opinion-manager/references/versioning.md`（lifecycle `use` 事件）、`plugins/opinion-manager/skills/opinion-manager/SKILL.md`、`docs/design/` 其余四篇（体例）。
- issue：#121（正文已读）；姊妹设计 `docs/design/opinion-skill-import.md`（#119）。

## 12. 术语对照表

- 路由：routing（opinion 规则指定「某类任务使用某个 skill」）
- 触发器：trigger
- 渐进式加载：progressive disclosure
- 路由遵从率：route adherence rate
- 适配层：adapter layer
- 软必填：soft-required（缺失只警告）
- 降级条款：fallback clause
- 机械检查：mechanical check（不判定语义）
