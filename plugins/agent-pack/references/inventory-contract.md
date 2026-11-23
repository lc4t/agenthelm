# `agenthelm inventory` 输出契约（schema_version 1）

English: [inventory-contract.en.md](inventory-contract.en.md)

只读命令，回答「各客户端现在能看到哪些 Skill 与 MCP、装在哪个存储与作用域、哪些重复、哪些来源可识别」。输出供人阅读，也供后续写入命令与菜单界面读取，所以 JSON 是契约：**键只追加，不删除、不改含义**；破坏性变化必须升 `schema_version`。

```
agenthelm inventory [--output json|text] [--client claude|codex|cursor|claude-desktop|all]… [--type skills|mcp|all]
                    [--scope project|user|all] [--project 目录] [--include-description] [--lang zh|en|auto]
```

- 默认读全部三个客户端、全部类型与作用域；`--project` 缺省为当前目录。`--scope project` 同时包含 Claude 的 `local` 级。
- 不写任何文件、不执行客户端命令行、不读设置键、不读令牌存储。退出码 0；参数错误 2。文件缺失视为没有该存储；文件存在但损坏则写入 `unknown[]`，不崩溃。
- `--output text` 随界面语言（`AGENTHELM_LANG` 或 `--lang`）；JSON 的键与取值（含原因代码）固定为英文标识。
- 默认不输出 description 文本；`--include-description` 才在 `frontmatter.description` 给出（截断到 1,536 个字符）。
- 路径用 `~`（家目录）与 `<project>`（项目根）缩写；符号链接目标位于这两处之外时保持绝对路径。

## 证据等级 `evidence`

`file`（读文件得到）、`setting`（读设置键得到）、`cli`（问客户端命令行得到）、`unknown`（官方文档没有给出判定手段，或文件损坏）。**本版本只产生 `file` 与 `unknown`**，`setting`、`cli` 为后续版本保留。证据为 `unknown` 的条目不参与任何「生效」结论；契约里没有「可删除」类字段，处置建议由后续命令在证据充分时另行给出。

## 顶层

`schema_version`、`generated_by`、`project`、`clients`、`filters{type,scope}`、`skills[]`、`mcps[]`、`summary.per_client`、`duplicates{skills[],mcps[]}`、`overlap_hints[]`、`unknown[]`。

## Skill 条目

`ref`、`name`、`client`、`scope`（`user|project`）、`store`、`path`、`frontmatter{name[,description]}|null`、`description_bytes`、`description_chars`、`when_to_use_chars`、`when_to_use_bytes`（frontmatter 的 `when_to_use`，没有则 0）、`skill_md_bytes`、`skill_md_lines`、`dir_bytes`、`file_count`（技能目录内文件总字节数与个数，目录内符号链接只计不跟随；超过 20000 个文件时为 `null` 并带 `dir-too-large`）、`content_sha256`（SKILL.md 全文）、`name_matches_dir`（frontmatter 无 name 时为 `null`）、`frontmatter_ok`（有 name 与 description）、`is_symlink`、`link_target_kind`（`relative|absolute|outside-home|null`，`absolute` 指家目录内的绝对路径）、`symlink{is_symlink,target}`、`origin`、`version`、`stale`、`loaded`、`precedence`、`shadowed_by`、`evidence`、`issues[]`。

- `origin`：`{kind, detail}` 为第一版键；结构化形式 `{type, id, version, latest_known, evidence}`：`type` 为 `plugin`（可识别的 agenthelm 插件）、`marketplace`（位于客户端市场缓存目录的插件）、`git`（符号链接目标位于某个 git 仓库内）、`unknown`；`latest_known` 是本机各客户端插件目录里找到的同名插件最高版本，**不联网**，找不到为 `"unknown"`。没有 `manual` 值：无法区分「手工放置」与「来源不明」，一律 `unknown`。`kind`：`agenthelm-plugin`（解析符号链接后位于 `plugin.json` 声明为 agenthelm 仓库的插件内）、`state-record`（Claude 的同步状态记录 `~/.claude/agent-pack/state/*.json` 登记了它）、`unknown`。
- `stale` 只对 `agenthelm-plugin` 给 `true|false`：版本低于本机各客户端插件目录里找到的同名插件最高版本为 `true`；`false` 只表示本机没有更新的副本，不代表云端最新。其余一律 `"unknown"`，不猜。
- `loaded`：本版本恒为 `"unknown"`（要判定须读 `skillOverrides`、`[[skills.config]]` 等设置键，本版不读，证据等级为 `setting`，保留）。`precedence`：`unique`（无同名）、`winner`、`shadowed`、`both-load`、`unknown`；`shadowed_by{store,rule,evidence,source_url}` 只在官方文档明确优先级且本条目落败时给出。
- `issues[]`：`broken-symlink`、`frontmatter-missing`、`description-missing`、`name-mismatch`、`skill-md-too-large`、`dir-too-large`。
- 读取的存储：Claude `~/.claude/skills`、`<project>/.claude/skills`；Codex `~/.codex/skills`、`~/.agents/skills`、`<project>/.agents/skills`；Cursor `~/.cursor/skills`、`~/.agents/skills`、`<project>/.cursor/skills`、`<project>/.agents/skills`。以点开头的条目（如 `.system`）跳过。未覆盖：企业/管理员级目录、从子目录向上逐级扫描、Cursor 对 `.claude/skills` 的兼容加载。

## MCP 条目

`ref`、`name`、`client`、`scope`（`user|local|project`）、`store`、`transport`（`stdio|http|sse|unknown`）、`command`、`args[]`、`url`、`url_host`（仅远程，只有主机名，不含端口与 query）、`env_names[]`（`env` 的键名）、`env_var_names[]`（涉及的环境变量名：`env` 键名加 `${VAR}` 引用，仅名字）、`header_names[]`、`has_inline_secret`（布尔：是否存在内联的凭据值；不给键名、不给值）、`fingerprint`、`fingerprint_fields[]`（参与指纹的字段名：`["command","args"]` 或 `["url"]`）、`origin`、`loaded`（`"unknown"`，文件自己关闭的条目为 `"no"`）、`precedence`、`shadowed_by`、`evidence`、`notes[]`。

- **绝不输出 `env` 值、`headers` 值、令牌**：只给键名（`env_names`、`header_names`）。`args` 与 `url` 里疑似凭据的值替换为 `<redacted>`，URL 的用户信息与查询值同理；`${VAR:-默认值}` 只保留 `${VAR}`。
- `fingerprint`：对**已脱敏**的 `command+args` 或 `url` 取 SHA-256，用于判重，不含 `env` 与 `headers`。
- 读取的存储：Claude `~/.claude.json`（用户级 `mcpServers`）、`~/.claude.json` 内 `projects[<项目绝对路径>].mcpServers`（local 级，按官方文档示例的结构读取，结构不符记 `unknown`）、`<project>/.mcp.json`；Codex `~/.codex/config.toml`（`mcp_servers`）、`<project>/.codex/config.toml`（带 `notes: ["codex-project-requires-trust"]`，信任记录读不到）；Cursor `~/.cursor/mcp.json`、`<project>/.cursor/mcp.json`。

## 条目引用 `ref` 与版本兼容约定

- `ref` = `<kind>:<name>@<客户端>:<作用域>`，`kind` 为 `skill` 或 `mcp`，例如 `skill:pdf-notes@claude:user`、`mcp:github@claude:local`。同一客户端与作用域下，Skill 位于「主存储」之外的条目加后缀 `#agents`（主存储：Claude `~/.claude/skills` 与 `<project>/.claude/skills`；Codex `~/.codex/skills` 与 `<project>/.agents/skills`；Cursor `~/.cursor/skills` 与 `<project>/.cursor/skills`；其余即 `~/.agents/skills`、Cursor 的 `<project>/.agents/skills`），所以 `ref` 在一份输出内唯一，且不随其他条目的出现而变化。
- 兼容约定：**新增键不升 `schema_version`**；删除键、改变键含义或取值集合的收窄才升主版本。消费方遇到不认识的 `schema_version` 应降级为「不可用」，不要猜测字段含义；遇到不认识的新键应忽略。

## 汇总与重复

- `summary.per_client.<客户端>`：`skill_count`、`description_bytes_total`（每个条目的 description 字节数之和，不含 name，不含客户端的截断或格式化）、`description_bytes_distinct_files`（指向同一文件的只算一次）、`mcp_count`。
- `duplicates.skills[]`：同一客户端内同名且位于不同存储。`relation`：`same-file`（解析后是同一文件）、`identical`（SKILL.md 内容相同）、`different`、`unknown`。跨客户端不算重复。
- `duplicates.mcps[]`：`kind: same-name`（同客户端同名跨存储，`relation` 按指纹）与 `kind: same-fingerprint`（同指纹不同名）。
- `effective`：「哪一份生效」，只在官方文档给出优先级时有 `store`，否则 `store: null` 并给 `rule` 与 `evidence: unknown`。
  - Claude Skill：企业 > 个人 > 项目（用户级胜出；企业级未读）。出处 <https://code.claude.com/docs/en/skills>（「Resolve skills that share a name」，2026-11-18 读取）。
  - Claude MCP：local > project > user，整条目取用不合并字段。出处 <https://code.claude.com/docs/en/mcp>（「Scope hierarchy and precedence」，2026-11-18 读取）。项目级 `.mcp.json` 需要逐项目批准而批准状态本版不读，所以只有 local 胜出时给结论，其余为 `project-approval-unknown`。
  - Codex Skill：同名不合并，两份都出现在选择器，`rule: both-load`。出处 <https://learn.chatgpt.com/docs/build-skills>（2026-11-18 读取）。
  - Codex MCP：官方页面（<https://learn.chatgpt.com/docs/extend/mcp?surface=cli>，2026-11-18 读取）只说项目级仅对受信任项目生效，没有给同名优先级，记 `project-trust-unknown`。
  - Cursor：同名优先级文档未说明（设计文档 #82 §2.3，**本次未重读，未确认**），记 `precedence-undocumented`。
- `overlap_hints[]`：不同名、同客户端、description 的词元集合 Jaccard 相似度 ≥ 0.8（每边至少 5 个词元；英文按单词，中文按相邻二字）。`hint_only: true`，只是机械提示，不声称语义相同。

## 已知取舍与局限

- **Cursor 的兼容加载未建模（有意）**：设计文档 #82 §2.3 记载 Cursor 还会兼容读取 `~/.claude/skills`、`~/.codex/skills`（及项目的 `.claude/skills`、`.codex/skills`）。本版本只列 Cursor 自己的存储（`~/.cursor/skills`、`~/.agents/skills`、`<project>/.cursor/skills`、`<project>/.agents/skills`），原因：该行为依据的是 #82 的一次读取，本次未重读，**未确认**；同名优先级与停用开关的落盘位置官方文档均未说明；建模会让同一个文件同时出现在 `claude` 与 `cursor` 两个客户端下，影响重复与字节汇总的口径。后续可作为新增键或新存储追加，不改既有键。因此 Cursor 的 Skill 清单与 description 字节汇总可能少于它实际加载的数量。
- **指纹基于脱敏后的参数**：`fingerprint` 取的是已替换为 `<redacted>` 的 `command+args` 或 `url`，所以仅在被脱敏的位置上取值不同（例如 `args` 里两个不同的密钥值，或 URL 查询里不同的 token）的两条会得到同一个指纹，被判为「内容相同」。这是为了让摘要不泄露凭据而接受的取舍；`has_inline_secret` 可提示该条目含内联凭据，判重结论对这类条目只作提示。

## `unknown[]`

`{store, client, reason}`。`reason`：`invalid-json`、`invalid-toml`、`unreadable`、`too-large`、`unexpected-structure`、`claude-local-structure-unrecognized`。

## Claude Desktop（`claude-desktop`，只读）

`--client claude-desktop` 读取 Claude Desktop 应用的 `claude_desktop_config.json`，作为**独立目标**，不并入 `claude`（独立 CLI 不读该文件）。**不在默认客户端集合内**：须点名 `--client claude-desktop` 或用 `--client all`；默认输出与此前一致。

- 位置（官方文档，<https://modelcontextprotocol.io/docs/develop/connect-local-servers>，2026-11-18 读取）：macOS `~/Library/Application Support/Claude/claude_desktop_config.json`，Linux `~/.config/Claude/claude_desktop_config.json`；Windows 为 `%APPDATA%\Claude\claude_desktop_config.json`，本命令不解析，只在 `unknown[]` 记 `platform-unsupported`。
- 条目：`client: claude-desktop`、`scope: user`、`store: "desktop-config"`（符号名；真实路径在顶层 `desktop.path`），字段同 MCP 条目；顶层追加 `desktop{path, exists, parse_ok}`（仅选择了 `claude-desktop` 时出现；`parse_ok` 在文件不存在时为 `null`）。`unknown[]` 里该文件的 `store` 同为 `desktop-config`。没有 Skill 目录，也没有项目级，所以 `--type skills`、`--scope project` 下为空。文件里含 `${...}` 的条目带 `notes: ["variable-expansion-unconfirmed"]`（官方文档没有说明该文件是否展开变量）。
- 文件不存在记 `unknown` 的 `not-found`；损坏记 `invalid-json`、`unexpected-structure`。
- `doctor --client claude-desktop` 只读、只给观测与警告，永不产生 `issues`，不改变 `ok` 与退出码。
- `doctor --client claude`：项目受管的 MCP 若在 `claude_desktop_config.json` 里也有同名条目，`clients.claude.shadowed_by[]` 追加一条建议级提示（键只在有发现时出现）：`{name, by, store, applies_to: "desktop-local-code-tab", cli_affected: false, severity: "advisory", evidence: "file", precedence_confirmed: false, source_url}`，并写入 `warnings`。该提示仅涉及 Desktop 本地 Code 标签；独立 CLI 不受影响。**优先级未确认**：设计文档 #82（2026-11-11 读取）引用过「同名时 Code 标签使用 `claude_desktop_config.json` 的定义」，2026-11-18 重读 <https://code.claude.com/docs/en/desktop#mcp-servers-from-the-claude-desktop-chat-app> 时该页只写「Desktop 把该文件的服务器与 `~/.claude.json`、`.mcp.json` 里的一起加载进本地 Code 标签」，没有同名优先级，所以 `precedence_confirmed` 为 `false`，措辞为「可能改用」。

## 平台

支持 macOS 与 Linux 的路径解析；Windows 路径只在文档说明，本命令不解析。

## `cli` 键（只在点名运行客户端命令时出现）

`--client agy`（或 `all` 且程序存在）时，顶层追加 `cli`：`{<客户端>: {evidence, ok, reason, plugins[]}}`。`evidence` 为 `cli`（客户端自己的 `plugin list --json` 读成功）或 `unknown`（读取失败，`reason` 是 `not-found`、`timeout`、`unreadable`、`nonzero-exit`、`too-large`、`unexpected-format` 之一，不构成证据）。`plugins[]` 的键：`name`、`enabled`、`version`、`path`、`skills[]`、`mcp_servers[]`、`hooks[]`（后三者是名字）。默认的 `inventory` 不运行任何客户端命令，也没有这个键。
