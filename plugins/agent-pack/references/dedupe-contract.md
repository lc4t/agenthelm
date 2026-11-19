# `agenthelm dedupe` 契约（schema_version 1）

English: [dedupe-contract.en.md](dedupe-contract.en.md)

```
agenthelm dedupe --list [--output json]
agenthelm dedupe <分组id> (--keep <ref> --retire-others | --keep-all --note "…" | --ignore | --unignore)
                 [--project 目录] [--output json|text] [--lang zh|en|auto] [--apply --expect-plan <plan_digest>]
agenthelm dedupe <分组id> --keep <mcp ref> --desktop-snippet [--output json|text]
```

分组来自 `agenthelm inventory` 的 `duplicates`。分组 id 稳定：`g-` + SHA-256(`kind|客户端|名字或指纹`) 前 10 位，不随其他条目出现而变。默认预览、零写入；`--apply` 必须带预览给出的 `--expect-plan`，不一致就拒绝（退出码 1）。退出码：0 成功或预览无阻止项；1 阻止项、指纹不一致或失败；2 参数错误。只支持 macOS 与 Linux。

## 处置

- **`--keep <ref> --retire-others`**（只对 Skill 副本生效）：其余副本移动到 `~/.agent-pack/recovery/<时间戳>-dedupe-<分组id>/skills/<名>`（同一恢复目录布局，多个副本时目录名带存储后缀）。归档是移动，不是删除；失败逆序移回。结果给出 `recovery_dir` 与手工恢复的单行 `mv`，本版没有 `restore` 命令。保留项指向要退役目录内部时 `keep-depends-on-retired`。
- **证据为 `unknown` 的副本只读**：任何一份为 `unknown` 就 `blocked: evidence-unknown`，不能退役。
- **MCP 副本**：工具**不编辑**任何配置（`~/.claude.json` 可能含本地凭据）。`plan.manual[]` 给出官方命令 `claude mcp remove --scope <user|local|project> <名>` 与 `cp ~/.claude.json ~/.claude.json.bak` 备份提示，每项 `not_archivable_by_tool: true`、`promise: cannot-promise`；非 Claude 客户端只给「手工删除某文件里的某条目」的说明。`--apply` 的结果为 `manual-only`，零写入。其他客户端有同指纹条目时 `warnings` 含 `cross-client-keep-recommended`（跨客户端相同 MCP 不冲突，建议保留）。
- **`--keep-all --note`** / **`--ignore`** / **`--unignore`**：只写机器级决定文件 `~/.config/agenthelm/ext-decisions.json`（0600，原子写）；内容只有 `{decision, note, kind, client, key, members_digest, at}`，不含任何值。备注最长 200 个字符，疑似令牌的备注拒绝。分组成员变化后 `--list` 把决定标为 `stale`。`--ignore` 可用 `--unignore` 撤销。这三个动作不改任何客户端配置。

## Desktop 可粘贴片段（只读）

`--desktop-snippet`：输出 `claude_desktop_config.json` 的路径（macOS `~/Library/Application Support/Claude/`，Linux `~/.config/Claude/`）、要合并进 `mcpServers` 的 JSON 片段（只含 `command`/`args`/`url` 结构，环境变量只列名字并以 `<在此填入你的 NAME>` 占位）与「完全退出并重启 Desktop」提示。含内联凭据的条目 `blocked: inline-secret`，不生成片段。远程 `url`、请求头、SSE 是否被 Desktop 加载官方文档未说明，片段带 `notes` 标「未确认」；`promise` 恒为 `cannot-promise`。不读不写 Desktop 配置文件。

## JSON

`{schema_version, command: "dedupe", mode: "list"|"preview"|"apply"|"desktop-snippet", ...}`。`plan` 的键：`group`、`kind`、`disposition`、`status`（`ready|blocked|manual-only`）、`members[]`、`steps[{id,action,promise}]`、`blocked[{code,message}]`、`warnings[{code,message}]`、`manual[]`、`retire[]`、`plan_digest`、`next.apply`。`plan_digest` 与界面语言无关。键只追加；输出不含 env 值、headers 值或令牌。
