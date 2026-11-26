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
- **MCP 副本**：仅**已受管**的条目（本项目包名的 state 记录了归属：user 级看 `mcps`，local/project 看 `scopes.mcps`）由工具退役：`plan.retire[]` 列出（`managed: true`、16 位 `entry_digest`，无值），原 JSON 片段存入恢复目录 `mcp/<名>@<作用域>.json`，整个配置文件先逐字节备份（`claude.json.before` / `project-mcp.json.before`，均 0600，目录 0700，不打印），`~/.claude.json` 原子改写，state 去掉该归属，任一步失败按字节回滚；预览 = 应用（`plan_digest` 含条目摘要，不一致拒绝且零写入）。恢复沿用手工方式：结果的 `restore[]` 给出放回片段或整份还原备份的步骤，没有 `restore` 命令。**未受管**的副本（手工装的、别的包名的）工具**不编辑**：`plan.manual[]` 给出官方命令 `claude mcp remove --scope <user|local|project> <名>` 与 `cp ~/.claude.json ~/.claude.json.bak` 备份提示，每项 `not_archivable_by_tool: true`、`promise: cannot-promise`；非 Claude 客户端只给「手工删除某文件里的某条目」的说明。只有未受管副本时 `--apply` 的结果为 `manual-only`，零写入。证据 `unknown` 的成员仍 `blocked: evidence-unknown`。其他客户端有同指纹条目时 `warnings` 含 `cross-client-keep-recommended`（跨客户端相同 MCP 不冲突，建议保留，不提供退役）。
- **`--keep-all --note`** / **`--ignore`** / **`--unignore`**：只写机器级决定文件 `~/.config/agenthelm/ext-decisions.json`（0600，原子写）；内容只有 `{decision, note, kind, client, key, members_digest, at}`，不含任何值。备注最长 200 个字符，疑似令牌的备注拒绝。分组成员变化后 `--list` 把决定标为 `stale`。`--ignore` 可用 `--unignore` 撤销。这三个动作不改任何客户端配置。

## 目标判定（#184）

指纹相同不等于目标相同（见 inventory 契约「目标判定」）。`dedupe` 据此做三处改动，键只追加，`schema_version` 保持 1，分组 id 与决定文件的键不变：

- `--list` 不再列出「指纹相同但目标不同或无法确认」的同指纹组（它们在 inventory 的 `mcp_hints[]` 里）。用这类组的旧 id 调用 `dedupe` 得到 `blocked: not-a-duplicate-group`，并带 `target_relation`、`differs_in[]`、`reasons[]`、`refs[]`，不再是 `group-not-found`。此前对这类组记录的 `keep-all` / `ignore` 仍留在决定文件里，不改写，`--unignore` 同样得到 `not-a-duplicate-group`；决定文件的清理不在本版。
- 跨客户端守卫按 `target_class` 比较：别的客户端里有同 `target_class` 的条目时仍发 `cross-client-keep-recommended`（`refs[]` 只列同目标的条目）；只有同指纹但不同 `target_class` 的条目不发警告；对方或本组含 `opaque` 目标而无法确认时发 `cross-client-target-unconfirmed`（不据此建议保留）。本命令仍不为跨客户端组提供退役。
- 同名跨存储组（`group_kind: same-name`）：`relation: identical` 而 `target_relation` 为 `different` 或 `unknown` 时，`warnings` 追加 `fingerprint-collision`（指纹相同但内容可能不是同一个服务，先确认目标再处置）。

## Desktop 可粘贴片段（只读）

`--desktop-snippet`：输出 `claude_desktop_config.json` 的路径（macOS `~/Library/Application Support/Claude/`，Linux `~/.config/Claude/`）、要合并进 `mcpServers` 的 JSON 片段（只含 `command`/`args`/`url` 结构，环境变量只列名字并以 `<在此填入你的 NAME>` 占位）与「完全退出并重启 Desktop」提示。含内联凭据的条目 `blocked: inline-secret`，不生成片段。远程 `url`、请求头、SSE 是否被 Desktop 加载官方文档未说明，片段带 `notes` 标「未确认」；`promise` 恒为 `cannot-promise`。不读不写 Desktop 配置文件。

## JSON

`{schema_version, command: "dedupe", mode: "list"|"preview"|"apply"|"desktop-snippet", ...}`。`plan` 的键：`group`、`kind`、`disposition`、`status`（`ready|blocked|manual-only`）、`members[]`、`steps[{id,action,promise}]`、`blocked[{code,message}]`、`warnings[{code,message}]`、`manual[]`、`retire[]`、`plan_digest`、`next.apply`。`plan_digest` 与界面语言无关。键只追加；输出不含 env 值、headers 值或令牌。
