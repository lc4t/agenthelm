# 候选收件箱（inbox）

收件箱保存 Agent 在工作中发现的、尚待用户裁决的观点。它不是规则：不写入 `OPINION.md`、Profile 或锁，也不改变任何已确认内容。暂存无需确认，所有状态变化都需要用户明确表态。

## 位置与隐私

- 文件：项目内的 `.opinion/inbox.json`（与 `.opinion/profiles` 同属项目私有目录）。
- **含用户原话，可能含敏感信息。**启用本功能的项目建议保持私有（至少不要放进公开渠道）。若要公开仓库、同步到共享目录或让他人看到，是否排除 `.opinion/` 及其中内容由你决定；工具只提醒，不会替你决定，也不会自动修改 `.gitignore`、删除或改写任何内容。
- 提醒的时机：第一次 `add` 创建收件箱时，返回里带 `notice`（Agent 会转告你）；会话开始时每个项目提醒一次（`nudge` 的 hook）；`inbox exposure` 或 `agenthelm nudge status` 随时查看当前状态（已被 Git 忽略 / 已被跟踪 / 未被忽略 / 不是 Git 仓库）。已被 Git 忽略时不再提醒。
- 拒绝符号链接；读改写持有 `.opinion/.inbox.lock`，多个会话同时暂存不会丢失。

## 状态

- `pending`：待确认，尚未生效。
- `trial`：用户同意在本项目试行。hook 会把试行候选的全文随协议注入会话，Agent 按其工作；超过 `nudge.trial_expire_days`（默认 30）未转正回到 `pending`。
- `formal`：用户同意转为正式规则。这里只记录意愿，写入 `OPINION.md` 仍走完整预览与确认。
- `held`：暂缓，不再提示。
- `rejected`：丢弃。

## 命令

以下命令都在 `opinion_manager.py inbox <动作>` 下，`--project` 必填；`--lang zh|en` 指定提示语言（缺省按 `AGENTHELM_LANG`、系统 locale 决定，最后用中文）；输出为 JSON，`list` 可用 `--output markdown` 得到给用户看的编号列表。

- `add --title --text --scope [--quote] [--client] [--session]`：暂存。标题 ≤120、正文 ≤1200、原话 ≤600 个字符；至少一个 `--scope`。内容相同（忽略空白与大小写）的候选合并，新原话追加为证据。
- `list [--status a,b] [--session ID] [--unpresented]`：列出候选。
- `decide <编号…> --to pending|trial|formal|held|rejected --confirmed [--note]`：裁决。没有 `--confirmed` 一律拒绝；多个编号要么全部成功，要么都不改。
- `present <编号…>`：记录已向用户展示过（hook 在发出确认请求时调用，避免同一候选反复提示）。
- `expire --days N`：把试行超过 N 天的候选退回 `pending`。退回保留 `last_action_at`，因此仍计入“久未处理”。
- `pending [--session ID]`：待确认项提供者，见下。
- `exposure`：只读，报告 `.opinion/inbox.json` 在 Git 里的状态与提醒文字。
- `brief [--session ID] [--max-chars N]`：生成会话开始时注入的协议与试行候选全文，默认上限 6000 字符。

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
