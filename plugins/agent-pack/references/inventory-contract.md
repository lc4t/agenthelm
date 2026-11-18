# `agenthelm inventory` 输出契约（schema_version 1）

English: [inventory-contract.en.md](inventory-contract.en.md)

只读命令，回答「各客户端现在能看到哪些 Skill 与 MCP、装在哪个存储与作用域、哪些重复、哪些来源可识别」。输出供人阅读，也供后续写入命令与菜单界面读取，所以 JSON 是契约：**键只追加，不删除、不改含义**；破坏性变化必须升 `schema_version`。

```
agenthelm inventory [--output json|text] [--client claude|codex|cursor]… [--type skills|mcp|all]
                    [--scope project|user|all] [--project 目录] [--lang zh|en|auto]
```

- 默认读全部三个客户端、全部类型与作用域；`--project` 缺省为当前目录。`--scope project` 同时包含 Claude 的 `local` 级。
- 不写任何文件、不执行客户端命令行、不读设置键、不读令牌存储。退出码 0；参数错误 2。文件缺失视为没有该存储；文件存在但损坏则写入 `unknown[]`，不崩溃。
- `--output text` 随界面语言（`AGENTHELM_LANG` 或 `--lang`）；JSON 的键与取值（含原因代码）固定为英文标识。
- 路径用 `~`（家目录）与 `<project>`（项目根）缩写；符号链接目标位于这两处之外时保持绝对路径。

## 证据等级 `evidence`

`file`（读文件得到）、`setting`（读设置键得到）、`cli`（问客户端命令行得到）、`unknown`（官方文档没有给出判定手段，或文件损坏）。**本版本只产生 `file` 与 `unknown`**，`setting`、`cli` 为后续版本保留。证据为 `unknown` 的条目不参与任何「生效」结论；契约里没有「可删除」类字段，处置建议由后续命令在证据充分时另行给出。

## 顶层

`schema_version`、`generated_by`、`project`、`clients`、`filters{type,scope}`、`skills[]`、`mcps[]`、`summary.per_client`、`duplicates{skills[],mcps[]}`、`overlap_hints[]`、`unknown[]`。

## Skill 条目

`name`、`client`、`scope`（`user|project`）、`store`、`path`、`frontmatter{name,description}|null`、`description_bytes`（description 的 UTF-8 字节数）、`skill_md_bytes`、`content_sha256`（SKILL.md 全文）、`origin{kind,detail}`、`symlink{is_symlink,target}`、`version`、`stale`、`evidence`、`issues[]`。

- `origin.kind`：`agenthelm-plugin`（解析符号链接后位于 `plugin.json` 声明为 agenthelm 仓库的插件内）、`state-record`（Claude 的同步状态记录 `~/.claude/agent-pack/state/*.json` 登记了它）、`unknown`。
- `stale` 只对 `agenthelm-plugin` 给 `true|false`：版本低于本机各客户端插件目录里找到的同名插件最高版本为 `true`；`false` 只表示本机没有更新的副本，不代表云端最新。其余一律 `"unknown"`，不猜。
- `issues[]`：`broken-symlink`、`frontmatter-missing`、`description-missing`、`name-mismatch`、`skill-md-too-large`。
- 读取的存储：Claude `~/.claude/skills`、`<project>/.claude/skills`；Codex `~/.codex/skills`、`~/.agents/skills`、`<project>/.agents/skills`；Cursor `~/.cursor/skills`、`~/.agents/skills`、`<project>/.cursor/skills`、`<project>/.agents/skills`。以点开头的条目（如 `.system`）跳过。未覆盖：企业/管理员级目录、从子目录向上逐级扫描、Cursor 对 `.claude/skills` 的兼容加载。

## MCP 条目

`name`、`client`、`scope`（`user|local|project`）、`store`、`transport`（`stdio|http|sse|unknown`）、`command`、`args[]`、`url`、`env_names[]`、`header_names[]`、`fingerprint`、`origin`、`evidence`、`notes[]`。

- **绝不输出 `env` 值、`headers` 值、令牌**：只给键名（`env_names`、`header_names`）。`args` 与 `url` 里疑似凭据的值替换为 `<redacted>`，URL 的用户信息与查询值同理；`${VAR:-默认值}` 只保留 `${VAR}`。
- `fingerprint`：对**已脱敏**的 `command+args` 或 `url` 取 SHA-256，用于判重，不含 `env` 与 `headers`。
- 读取的存储：Claude `~/.claude.json`（用户级 `mcpServers`）、`~/.claude.json` 内 `projects[<项目绝对路径>].mcpServers`（local 级，按官方文档示例的结构读取，结构不符记 `unknown`）、`<project>/.mcp.json`；Codex `~/.codex/config.toml`（`mcp_servers`）、`<project>/.codex/config.toml`（带 `notes: ["codex-project-requires-trust"]`，信任记录读不到）；Cursor `~/.cursor/mcp.json`、`<project>/.cursor/mcp.json`。

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

## `unknown[]`

`{store, client, reason}`。`reason`：`invalid-json`、`invalid-toml`、`unreadable`、`too-large`、`unexpected-structure`、`claude-local-structure-unrecognized`。

## 平台

支持 macOS 与 Linux 的路径解析；Windows 路径只在文档说明，本命令不解析。
