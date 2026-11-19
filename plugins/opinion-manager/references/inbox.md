# 候选收件箱（inbox）

[English](inbox.en.md) · **简体中文**

收件箱保存 Agent 在工作中发现的、尚待用户裁决的观点。它不是规则：除 `adopt --apply` 外，不写入 `OPINION.md`、Profile 或锁，也不改变任何已确认内容。暂存无需确认，所有状态变化都需要用户明确表态。

## 位置与隐私

- 文件：项目内的 `.opinion/inbox.json`（与 `.opinion/profiles` 同属项目私有目录）。
- **含用户原话，可能含敏感信息。**启用本功能的项目建议保持私有（至少不要放进公开渠道）。若要公开仓库、同步到共享目录或让他人看到，是否排除 `.opinion/` 及其中内容由你决定；工具只提醒，不会替你决定，也不会自动修改 `.gitignore`、删除或改写任何内容。
- 提醒的时机：第一次 `add` 创建收件箱时，返回里带 `notice`（Agent 会转告你）；会话开始时每个项目提醒一次（`nudge` 的 hook）；`inbox exposure` 或 `agenthelm nudge status` 随时查看当前状态（已被 Git 忽略 / 已被跟踪 / 未被忽略 / 不是 Git 仓库）。已被 Git 忽略时不再提醒。
- 拒绝符号链接；读改写持有 `.opinion/.inbox.lock`，多个会话同时暂存不会丢失。

## 状态

- `pending`：待确认，尚未生效。
- `trial`：用户同意在本项目试行。hook 会把试行候选的全文随协议注入会话，Agent 按其工作；超过 `nudge.trial_expire_days`（默认 30）未转正回到 `pending`。
- `formal`：用户已批准转为正式规则。**「已批准」不等于「已生效」**：`decide --to formal` 只记录状态，不写入 `OPINION.md`，后续会话不会加载它；输出里 `in_opinion: false`，并给出下一步 `inbox adopt …`。只有 `adopt --apply` 成功写入并读回后，候选才既是 `formal` 又已生效。存储状态名 `formal` 不变（改名会破坏已有收件箱数据）。
- `held`：暂缓，不再提示。
- `rejected`：丢弃。

## 命令

以下命令都在 `opinion_manager.py inbox <动作>` 下，`--project` 必填；`--lang zh|en` 指定提示语言（缺省按 `AGENTHELM_LANG`、系统 locale 决定，最后用英文）；输出为 JSON，`list` 可用 `--output markdown` 得到给用户看的编号列表。

- `add --title --text --scope --trigger --purpose [--exclude] [--relation] [--quote] [--client] [--session]`：暂存。标题 ≤120、正文 ≤1200、原话 ≤600、各字段 ≤300 个字符；至少一个 `--scope`。内容相同（忽略空白与大小写）的候选合并，新原话追加为证据。
  - `--trigger`（何时适用）与 `--purpose`（为什么）由 Agent 暂存时填写，用户在集中裁决时一次看到。本版为**软必填**：缺失时仍暂存成功，返回 `warnings` 且 `complete: false`，列表按「不完整」展示；严格拒绝缺失项留给之后的 major（代码里集中开关 `STRICT_REQUIRED`）。
  - 传入了但无效的值一律拒绝：空白，以及「所有」「全部」「相关」「一切」「any」「all」「everything」「relevant」「n/a」等占位词。
  - 全局声明：用户明确要求全局时，`--trigger` 必须用固定标准值「全部场景，用户要求全局」/ "All scenarios; the user asked for this to apply globally"，并同时带非空 `--quote`；列表标注「全局（请用户确认）」。**工具只做形式校验，无法验证是谁提出的全局**，需由用户在裁决时确认。
  - `--exclude` 写不适用情形；`--relation` 是 Agent 对与现有规则关系的备注，工具原样保存和展示，不解释。
  - 采纳写入时，自定义行为 `- **标题**（适用：…；条件：…；目的：…；不适用：…）：正文`（没有这些字段的旧候选与此前渲染逐字节相同）。行内标签与 `OPINION.md` 的框架文字随**项目文件语言**（Project Profile 的 `language`，缺省 zh），不随界面语言；`en` 项目的文件里不出现中文（用户自己写的内容除外）。`language` 非 zh 的项目，`opinion.lock.json` 带可选键 `"language": "en"` 供 `verify` 复现渲染；zh 项目的锁格式不变。旧版运行时读不了带 `language` 的锁，需先升级。
- `list [--status a,b] [--session ID] [--unpresented]`：列出候选。json 里每项带 `relations`（机械关系提示，没有则为空列表），markdown 里以「可能相关」行展示。
  - 关系提示的对象：其他候选（不含已丢弃）、`OPINION.md` 自定义行（按本工具自己的两种渲染格式解析）、Profile 里已选的公开规则文本。三种提示：规范化文本相同（`same_text`）、字符二元组 Jaccard 重合度 ≥ 0.5（`text_overlap`，两边规范化后都至少 6 个字符）、触发条件重合度 ≥ 0.4（`condition_overlap`，不含全局声明）。每条带证据片段与「机械比对，仅供参考」，最多 5 条，永不阻断；阈值保守，宁可漏报。冲突、合并、放大缩小、拆分等语义判断由 Agent 做，写入 `--relation` 备注，工具原样保存和展示，不解释。
- `check`：只读，不写任何文件。列出缺 trigger / purpose 的候选（`pending`、`trial`）与 `OPINION.md` 自定义行；认不出格式的行标 `unstructured`（未结构化），只报告。输出带每个候选的 `revise_command`，供 Agent 逐条提出建议值。`OPINION.md` 里的行补全走 `adopt` 的完整预览流程，由用户一次批量确认，不新增写入命令。
- `revise <编号> [--title --text --scope --trigger --purpose --exclude --relation --quote]`：修订候选。状态为 `pending` 且从未展示（`presented` 为 0）时无需 `--confirmed`；其他状态必须带 `--confirmed`。`history` 追加 `{to: 原状态, note: revised, previous: 被改字段的旧值}`，状态不变；重算摘要，与其他候选撞摘要时报错；`--scope` 整体替换，`--quote` 追加。`formal` 候选的修订不改 `OPINION.md`（返回警告）。
- `decide <编号…> --to pending|trial|formal|held|rejected --confirmed [--note]`：裁决。没有 `--confirmed` 一律拒绝；多个编号要么全部成功，要么都不改。
- `adopt <编号…> [--apply --confirm SHA256]`：从采纳到写入的一步确认入口。用户在对话里明确说「采纳」时用它，不再单独 `decide`。
  - 不带 `--apply`：只预览，零写入。把候选正文作为个人规则追加到项目现有 Profile 的自定义段（没有 Profile 时新建 `private.my-opinion@1.0.0`），经 `profile` 与 `compose` 同一套预览，返回完整 `content`（将成为 `OPINION.md` 的全文）、`blocked`（已有人工内容等需先处理的情况）和 `confirmation_sha256`。
  - 预览还返回 `added_lines`（采纳后将追加到自定义段的行，与 `content` 里的一致）和 `shown_matches`：全部候选都有展示快照、快照与此刻渲染行一致、没有 `blocked`、没有冲突时为 `true`；`shown` 逐个候选列出是否一致。它只是事实记录，不改变保护：写入仍必须带预览指纹，不因 `shown_matches` 放宽。
  - 带 `--apply --confirm <预览的 confirmation_sha256>`：用户确认后一次写入新 Profile 版本、`OPINION.md` 与 `opinion.lock.json`，用 `verify` 读回核对，最后才把候选置为 `formal`。预览之后候选、Profile 或 `OPINION.md` 有变化则指纹不符，拒绝并零写入。不使用 `--confirmed`：确认凭据是预览指纹。
  - 多个编号一次预览、一次确认，全部成功或都不改；不存在的编号、已是 `formal` 的编号整体报错并点名。写入或读回失败时，`OPINION.md`、锁、本次新建的 Profile 文件恢复原状，候选状态不变。
  - 已被 `decide --to formal` 记录、但尚未写入的候选：先 `decide --to pending --confirmed` 退回，再 `adopt`。
- `present <编号…>`：记录已向用户展示过（hook 在发出确认请求时调用，避免同一候选反复提示），同时记录**展示快照**：此刻候选将被渲染成的规则行的摘要（`shown`：`at`、`language`、`line_sha256`）。之后候选被修订或项目语言改变，摘要就对不上。输出带这些候选（含 `relations`）。
- `gate [--session ID] [--cooldown-hours N] [--max-per-session N] [--peek]`：询问节流，只记账，不决定要不要问。同项目 N 小时内（默认 24）最多放行一次，同会话最多放行 `--max-per-session` 次（默认 1）；返回 `allowed`、`reason`（`ok` / `cooldown` / `session_limit`）、`next_allowed_at`。放行且没有 `--peek` 时才记一次；`--peek` 只读，不创建任何文件。没有 `--session` 时只看冷却。状态在项目私有的 `.opinion/ask-state.json`（保留最近 50 个会话），拒绝符号链接，读改写持有 `.opinion/.ask.lock`；状态文件格式不对时报错，不当作放行。本命令只提供计数；何时调用、询问的措辞由协议和 hook 决定。
- `expire --days N`：把试行超过 N 天的候选退回 `pending`。退回保留 `last_action_at`，因此仍计入“久未处理”。
- `pending [--session ID]`：待确认项提供者，见下。
- `exposure`：只读，报告 `.opinion/inbox.json` 在 Git 里的状态与提醒文字。
- `brief [--session ID] [--max-chars N] [--mode ask|digest|quiet] [--cooldown-hours N] [--max-per-session N]`：生成会话开始时注入的协议与试行候选全文，默认上限 6000 字符。**确认时机的单一来源是这份协议**（SKILL.md 只指向它）。`--mode`：`ask`（默认）要求任务收尾前先运行 `gate`，放行才询问，询问时同步 `present`；`digest` 不单独询问，只在回复末尾顺带一句；`quiet` 只靠会话开始的汇总。`--cooldown-hours` 默认 24，`--max-per-session` 默认 1，写入 `ask` 协议里的 `gate` 命令行。三种模式的协议都规定：`adopt` 预览的 `shown_matches` 为真、无 `blocked`、无冲突时，Agent 可视为用户已确认展示过的内容，直接 `--apply --confirm`（指纹保护不变），并告知结果与回退方式（上一个 Profile 版本仍在 `.opinion/profiles/` 下，没有一步撤销命令）；否则展示预览差异后再问一次。用户之后更新需求本身不触发确认。
  - **限制**：hook 路径（agent-pack 的 `nudge`）有硬限制——`stop` 每会话最多一次，并先问 `gate`。模型自判路径（无 hook 的客户端，或模型自己决定何时问）没有任何硬限制，只能靠协议措辞，本工具不保证。各客户端真实行为（Cursor 的 sessionStart 注入、Codex `/hooks` 信任、Claude Stop 的 `additionalContext` 在各版本的表现）未经实机验证。

## 路由规则（把某类任务路由到某个 skill）

规则可以指定「做某类事时使用某个 skill」，例如「所有文字成果最终都用去 AI 味的 skill 处理」。**路由只是规则里的一段文字：它能提高模型调用该 skill 的概率，不能保证模型照做**，不要在对用户的说明里写成「保证」「强制」。它与 skill 自己的 `description` 触发叠加，不互斥。

- `add` / `revise` 的路由参数（都可选）：`--route-skill NAME`（skill 名：小写字母、数字、连字符，≤64；不接受 `plugin:skill`）、`--route-when before|after|instead`（缺省 `before`）、`--route-final`（收尾：只与 `after` 合用，不给 `--route-when` 时隐含 `after`）、`--route-on TAG`（短标签：小写字母、数字、连字符，≤32，供机械比较）。没有 `--route-skill` 时其余路由参数无效；`revise` 在候选已有路由上合并，`--route-clear` 清除整条路由（与其他 `--route-*` 互斥，旧路由记入 `history` 的 `previous`，确认规则同其他修订）。适用条件、目的、不适用仍用 `--trigger` / `--purpose` / `--exclude`；写「所有文字工作」时 `--trigger` 写成「产出面向人的文字成果」，`--exclude` 写代码、配置、命令输出、保持原文等，不要写成全局。
- 采纳写入的行末带纯 ASCII 标记 `[route skill=… when=… final=1 on=…]`（`final`、`on` 缺省则省略），与项目语言无关；渲染器按**项目文件语言**自动追加两条约束：不适用里加入「skill 自身的执行过程」，正文后加入「该 skill 执行期间不再触发本规则」与「该 skill 不可用时告知用户并按目的自行完成」。没有路由的规则行逐字节不变；`language=en` 的项目不出现中文。不改 Profile schema 与锁格式。
- 候选记录里的 `route` 对象含 `skill`、`when`、`final`、`on`；旧记录没有该键。`list`、`present`、`pending`、`brief` 的试行条目、`adopt` 预览都带出路由；采纳指纹与展示快照随渲染行一起变化。
- `routes list [--output markdown]`：只读。列出候选（`pending`、`trial`，以及已批准但尚未写入的 `formal`）与 `OPINION.md` 自定义段里的路由，认不出的行标 `unstructured`（未结构化，只报告）。附**机械检查，仅供参考**（只警告，不阻断）：同一 `--route-on` 标签、同一时机路由到不同 skill；重复路由（同 skill、同标签、同时机）；全项目多于一条 `--route-final`；超过 6 条；单条超过 500 字节。**不检查 skill 是否存在**（需要 `agenthelm inventory`，下一版提供），也不判断语义重叠与间接循环（输出的 `unchecked` 如实列出）。
- 验证办法（本版）：任务结束提交 lifecycle 的 `use` 事件时，对每条适用的路由规则在现有 `results` 字段自报 `passed`（确实调用并采用）、`not-applicable` 或 `violated`（应调用而未调用）。自报依赖 Agent 如实记录，路由遵从率的阈值没有经过验证；不改 lifecycle。

## 待确认项提供者契约

agent-pack 的会话开始检测按同一结构汇总各来源（opinion 候选，以及将来的 SOP 待办等）。一个提供者返回：

```json
{"ok": true, "source": "opinion",
 "items": [{"id": "cand-20261029-001", "source": "opinion", "kind": "candidate", "title": "…",
            "status": "pending", "created_at": "…Z", "last_action_at": "…Z", "age_days": 3.2,
            "hint_command": "处理这一项的命令"}],
 "counts": {"pending": 2, "trial": 1, "formal": 0, "held": 0, "rejected": 0},
 "oldest_pending_days": 3.2, "last_action_at": "…Z", "session_unpresented": ["cand-…"]}
```

`status` 为 `pending` 的条目计入提醒阈值；`age_days` 按 `last_action_at`（上一次用户动作，缺省为创建时间）计算。提供者只读，不在读取时改变状态。
