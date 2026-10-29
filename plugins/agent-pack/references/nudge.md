# nudge：自动采集观点候选与未决事项提示

`agenthelm nudge` 由三个客户端的 hook 调用，让 opinion 机制在用户不主动提起时也能工作：会话开始注入协议与试行规则，工作中发现长期偏好时提醒 Agent 暂存，任务完成时请用户集中确认，开新会话时按阈值提示久未处理的候选。它只读写 opinion-manager 的收件箱和本机状态，从不写 `OPINION.md`。收件箱与状态流转见 opinion-manager 的 `references/inbox.md`。

## 各客户端的行为

- **Claude Code**：插件随附 `hooks/hooks.json`，安装后自动生效。SessionStart（`startup|resume|clear|compact`）注入协议；UserPromptSubmit 在命中关键词时提醒；Stop 在满足条件时用 `additionalContext` 让 Agent 向用户确认。
- **Codex**：读取同一个 `hooks/hooks.json`（Codex 同时提供 `CLAUDE_PLUGIN_ROOT`）。按 Codex 的规则，插件 hook 需要用户在 `/hooks` 中审阅并信任后才会运行。Stop 使用 `decision: "block"` 加 `reason`，原因文本成为下一轮的续写提示。
- **Cursor**：没有 prompt 级注入（`beforeSubmitPrompt` 只能放行或拦截）。运行 `agenthelm nudge install-hooks --client cursor` 预览，加 `--apply` 把 sessionStart 与 stop 写入 `~/.cursor/hooks.json`（`--scope project` 写项目的 `.cursor/hooks.json`；`--remove` 撤销，只删除 agenthelm 写入的条目）。Cursor 的 `stop` 提示以“用户消息”的形式出现，默认关闭。社区有 sessionStart 的 `additional_context` 未到达模型的反馈（见 Cursor 论坛），所以协议同时写在 opinion-manager 的 Skill 里，不依赖 hook 也能按需触发。

三个客户端的共同点：hook 永远退出 0，任何异常都静默；只在项目启用了 opinion-manager（Project Profile 的 `opinion.provider` 为 `opinion-manager`，或存在 `.opinion/`、`opinion.lock.json`）时才有输出，其他目录完全无感。

## 触发条件

- **会话开始**：总是注入协议与试行候选全文（上限 6000 字符）；`compact` 后只重新注入。待确认候选数 ≥ `pending_threshold`，或最久一条超过 `stale_days`，且距上次提示超过 `reminder_repeat_hours` 时，另给用户一行可见提示。
- **用户消息**：消息 ≤1500 字符且包含 `keywords` 中任一词（不区分大小写），每个会话最多提醒 `capture_hints_per_session` 次。关键词只是提高命中率的辅助，语义判断仍由 Agent 做；长消息多为粘贴内容，跳过。
- **回合结束**：本会话暂存且没展示过的候选存在、`stop_prompt` 允许、未超过 `max_prompts_per_session`，且（`on-completion` 时）最后一条回复含 `completion_keywords` 之一。续写回合（`stop_hook_active` 或 Cursor 的 `loop_count`）不再触发。发出请求时把这些候选标记为已展示。

## 配置

位置（后者覆盖前者）：内置默认 → Project Profile 的 `opinion.nudge`（随项目同步，团队共享）→ 用户配置 `~/.config/agenthelm/config.json` 的 `nudge`（本人覆盖）。缺省即默认值，不要求在 Profile 里声明。非法值被忽略并在 `agenthelm nudge status` 里报告。

- `enabled`：默认 `true`。
- `pending_threshold`：待确认候选 ≥ 此数就提示，默认 `5`。
- `stale_days`：最久一条待确认候选超过此天数就提示，默认 `7`。
- `max_prompts_per_session`：每会话回合结束的确认请求上限，默认 `1`。
- `capture_hints_per_session`：每会话关键词提醒上限，默认 `3`。
- `trial_expire_days`：试行超过此天数未转正退回待确认，默认 `30`。
- `reminder_repeat_hours`：会话开始的可见提示最短间隔，默认 `24`。
- `stop_prompt`：`off` / `on-completion` / `always`，或按客户端设置，默认 `{"claude": "on-completion", "codex": "on-completion", "cursor": "off"}`。
- `keywords`、`completion_keywords`：字符串数组，替换内置列表。

环境变量 `AGENTHELM_NO_NUDGE=1` 临时关闭全部输出；`AGENTHELM_NUDGE_DEBUG=1` 把 hook 内部异常打印到 stderr。`agenthelm nudge status [--project P]` 显示生效配置、当前汇总以及开新会话是否会提示。

## 本机状态

会话计数与提示时间保存在 `$XDG_STATE_HOME/agenthelm/nudge.json`（缺省 `~/.local/state`），只含会话编号和时间戳，保留 14 天、最多 200 个会话。候选本身在项目内。

## 验证状态

单元测试覆盖各客户端的输出格式、阈值、限频、关键词、配置合并和 Cursor hooks.json 合并。以下依据官方文档编写，尚未在真实客户端里逐一验证：Codex 对插件 hook 的信任流程与 `systemMessage` 字段；Cursor 的 sessionStart 注入、`~/.cursor/hooks.json` 的实际加载；Claude Code `Stop` 的 `additionalContext` 在各版本的表现。

## 隐私：收件箱含用户原话

候选的证据是用户的原话，保存在项目内的 `.opinion/inbox.json`，可能含敏感信息（客户名、内部系统、人名、业务细节）。启用本功能的项目建议保持私有，至少不要放进公开渠道。若要公开仓库、放进同步盘或共享目录，是否排除 `.opinion/` 由用户决定。

nudge 只提醒，不替用户决定：第一次 `inbox add` 的返回带 `notice`，每个项目第一次开会话时给一行可见提示（已被 Git 忽略则不提醒），`agenthelm nudge status` 随时显示当前状态。它不会修改 `.gitignore`，也不会删除、改写或拒绝保存任何内容。
