# 客户端协议声明 schema v2（#134 Phase 2、3）

状态：设计稿，待维护者审阅。基线：`v6.14.0`（`fdcb701`）。本文只描述数据模型、边界、迁移批次和测试策略，不改任何运行时代码、版本、变更片段或发布载体。前置设计：[client-drivers.md](./client-drivers.md)（Phase 1，注册表与 `agenthelm clients`）。

## 0. 结论摘要

1. **原则：按协议识别客户端，不按产品名新增支线。** 新增客户端等于新增一份声明（数据）加一份夹具；格式与分发类型的差异由**按格式、按分发类型命名**的适配器吸收，适配器代码里不出现客户端 id。
2. **built-in 驱动改为数据文件**：`plugins/agent-pack/runtime/drivers/<id>.json`，注册表模块只做加载与校验。这使「新增客户端只加声明与夹具」可以按字面检验（§7）。
3. **schema v2 增加六个协议维度**（§4）：检测探针与证据、分发步骤、内容协议（清单加伴随文件）、存储（技能、MCP、hooks、上下文）、MCP 格式适配、hook 协议（事件映射加输出格式）；另有生命周期与「不支持项」。
4. **本版接入 Antigravity CLI（`agy`）与 Gemini CLI 作为验证**；官方文档称 Gemini CLI 已被 Antigravity CLI 取代，声明标 `lifecycle: deprecated`，支持范围收窄（§6）。两者都不进默认客户端集合。
5. **一次清零**：既有三个客户端的按名字分支全部迁到协议维度，守卫的白名单目标为 0，唯一例外是 `launcher.sh`（内容包来源文件，改动会触发模板变化，见 §9.3）。迁移分五批、同一批文件不并行（§8）。
6. **Phase 3**：运行时查找已经是 XDG 优先（`launcher.sh`），缺口在三处：客户端私有缓存的硬编码、hook 与 MCP 命令经客户端注入变量启动、同级插件查找散在多处。本版统一为一个解析函数（§9）。
7. **已实机核对**（agy 1.5.14、Gemini CLI 0.70.0，macOS，2026-11-22）：agy 插件落点、`plugin list --json`、`validate` 的行为、`hooks.json` 与 `mcp_config.json` 的识别条件、Gemini 的技能与扩展落点；**仍未验证**项集中列在 §12。

---

## 1. 前提与已决策事项

维护者原则不变：客户端是无穷的，核心安装流程不为某个客户端新增分支。本周期的决策记录如下，均来自统筹会话与维护者的往返：

- **守卫定义（D-K1）**：新增客户端等于声明加夹具，加上引用已有的格式适配器与分发类型。需要新格式或新分发类型时，允许新增按格式命名的适配器，但该 PR 里不得出现客户端 id 字面量。
- **一次清零（D-K2）**：既有三家的名字分支全部迁到协议维度，不用棘轮渐进。
- **launcher 例外（D-K3）**：`launcher.sh` 的第 4 级降级（客户端私有缓存）不改，守卫为这一个文件登记带理由的例外。本项在设计评审时可以推翻（代价见 §9.3）。
- **默认集合（D-K4）**：`agy` 与 `gemini` 的 `default_set` 为 false，须 `--client agy`、`--client gemini` 或 `--client all`；现有用户升级后行为不变。
- **支持范围（D-K5）**：见 §6。`sync` 写用户级技能与 MCP、agents-init 的 `agent_cli`、`nudge install-hooks` 对新客户端明确标「不支持」，不静默忽略。agy 的 MCP 与 Codex、Cursor 同等支持：`sync` 生成含 `mcp.json` 与 `mcp_config.json` 的可移植包，再由 `agy plugin install <目录>` 安装（§4.6）；用户级配置文件的落点与格式已实测（§2.2），项目级不支持（与 Codex、Cursor 相同）。
- **Gemini 安装的信任交互（D-K6）**：预览只打印命令；`--apply` 运行 `gemini extensions install` 时把终端交给用户，工具不代答信任提示，也不向其标准输入写入应答。
- **Windows（D-K7）**：沿用 `packages/cli/README.md` 的口径，运行时与启动器仅在 macOS 与 Linux 上验证；本版不改变这一范围（#82 的 D14 仍待拍板）。

## 2. 资料与证据等级

证据来源标记：**实测**（作者在实机上运行命令得到，agy 1.5.14、Gemini CLI 0.70.0）、**官方文档**（读取日期 2026-11-21 至 22）、**未验证**。「实测」只来自三轮只读探测与隔离 HOME 里的安装演练，真实用户目录前后快照差异为 0。

### 2.1 Agent Plugins 1.0.0

来源：[规范](https://agent-plugins.org/specification)，状态 Published。

- `plugin.json` 只要求 `$schema` 与 `name`；技能在 `skills/<名>/SKILL.md`；MCP 在 `mcp.json`，每个服务器必须有 `type`（`stdio`、`streamable-http`、`sse`）。
- **hooks 不是 v1 的可移植组件**，由客户端用反向域名扩展目录自行定义；1.1.0 草案写「adds portable hooks」（草案正文本文未读）。
- 规范不定义安装位置，也不定义市场或 git 分发。
- `mcp.json` 只展开 `${PLUGIN_ROOT}` 与 `${PLUGIN_DATA}`，不展开宿主环境变量；`url` 与请求头里不展开任何占位符。

### 2.2 Antigravity CLI（`agy`）

| 事项 | 事实 | 证据 |
| --- | --- | --- |
| 可执行文件与版本 | 单文件可执行，`agy --version` 输出 `1.5.14`；帮助文本写 stderr | 实测 |
| 顶层子命令 | `acp agent agents changelog help install mcp mic-serve models plugin remote-control update`；没有 `skills`、`hooks`、`version`、`extensions` | 实测 |
| `agy plugin` | `list [--json]`、`import [gemini\|claude]`、`install <目标>`、`uninstall`、`enable`、`disable`、`validate [目标] [--strict] [--json]`、`link <市场> <目标>`；没有 `update` | 实测 |
| `agy mcp` | `add [flags] <name> <commandOrUrl> [args...]`（`-H/--header "Key: Value"` 可重复、`-e/--env KEY=value` 可重复、`-t/--type stdio\|http` 默认 stdio；flag 必须写在 `<name>` 之前，http 与 https 地址自动识别，命令或参数以 `-` 开头时用 `--`）、`remove`、`enable`、`disable`（均取 `<name>`）、`list`（无参数，表格 `NAME TYPE STATUS COMMAND/URL`，写 stdout）。官方文档称没有该命令，与实测不一致，以实测为准。写入全局 `~/.gemini/config/mcp_config.json`；`disable` 在同一文件里把条目的 `disabled` 改为 `true`，不产生其他状态文件；`remove` 删除条目 | 实测（隔离 HOME，真实 HOME 前后差异 0） |
| 插件落点（用户） | `~/.gemini/config/plugins/<name>/`；`~/.gemini/antigravity-cli/plugins/` 不存在 | 实测，官方文档一致 |
| 插件落点（项目） | `.agents/plugins/` | 官方文档，**未验证** |
| 安装方式 | `agy plugin install <目录>` 原样复制目录并写 `~/.gemini/config/import_manifest.json`（`imports[]`：`name`、`source`、`importedAt`、`components`）；市场形式只文档化了 `antigravity-plugins-official`，GitHub 地址形式未试 | 目录形式实测 |
| 状态 | `enable`、`disable` 把状态写入 `~/.gemini/config/config.json` 的 `plugins.<name>.enabled`（只记录被切换过的插件）；`uninstall` 删除插件目录与清单条目 | 实测 |
| `plugin list --json` | 键 `name enabled version path skills[]`，有则 `mcpServers[]`（服务器名）、`hooks[]`（分组名）、`agents[]`、`rules[]`；经 agy 安装或导入的多一个 `source`（`antigravity` 或 `gemini-cli`）。可作为 `cli` 级证据 | 实测 |
| 清单 | `plugin.json`；识别 Agent Plugins 1.0.0 的 `$schema`（`validate` 报 `agentPluginsSpec:true`）；`displayName`、`logo`、`suggestedPrompts` 等为 agy 自有键 | 实测 |
| MCP（插件内） | 只有根目录 `mcp_config.json` 被 `install` 计入；Agent Plugins 的 `mcp.json` 仅在 `enable-agent-plugins-spec` 开关开启时使用，`validate` 对它只给 note，`install` 与 `list` 不计入（开关的存放位置**未找到**，本机状态推断为关） | 实测，开关状态为推断 |
| MCP 文件格式 | 全局与插件内同一格式 `mcpServers.<名>`：stdio 条目键 `command`、`args`（有参数时）、`env`（对象）、`disabled`（`agy mcp` 写入的全局文件里出现）；http 条目键 `serverUrl`（不写 `type`，不是 `url`）、`disabled`；`--env 'K=${K}'` 的字面量**原样保存、不展开**；`add` 后条目按名字排序。全局文件在实测机上起初是 0 字节空文件，首次 `add` 在其上写入 | 实测；启动 MCP 时是否展开 `${VAR}`、`disabled: true` 是否真的不启动**未验证** |
| hooks | 只读取插件**根目录**的 `hooks.json`；`hooks/hooks.json` 被 `validate` 警告「never loaded」。格式 `<分组名> → <事件名> → [{type, command}]`，另有嵌套形式 `PreToolUse[]{matcher, hooks[]{command,timeout,type}}` | 实测（识别与警告），事件是否触发**未验证** |
| hook 事件名 | `SessionStart PreInvocation PostInvocation PreToolUse PostToolUse Stop`；注入上下文用 `injectSteps` | 官方文档（真实 `hooks.json` 里见到 `PreInvocation`、`Stop`） |
| 技能目录（用户） | 文档列出 `~/.gemini/skills/` 与 `~/.gemini/antigravity-cli/skills/` 两个候选；实测机上两者都不存在，技能只见于插件内 `skills/` | **未确定** |
| 技能目录（项目） | `.agents/skills/` | 官方文档，**未验证** |
| 配置目录覆盖 | 把 `HOME` 指向别的目录即可整体重定位（隔离 HOME 里 `install` 落在其下） | 实测 |

### 2.3 Gemini CLI

注意：[官方文档](https://geminicli.com/docs/extensions/reference/)每页顶部写明 Gemini CLI 已于 2026-06-18 被 Antigravity CLI 取代。

| 事项 | 事实 | 证据 |
| --- | --- | --- |
| 可执行文件与版本 | `gemini --version` 输出 `0.70.0` | 实测 |
| 顶层子命令 | `mcp extensions skills hooks gemma models`；`hooks` 只有 `migrate`（从 Claude Code 迁移） | 实测 |
| 清单 | 只认 `gemini-extension.json`；对 Agent Plugins 夹具报 `Configuration file not found at …/gemini-extension.json` | 实测 |
| 分发 | `gemini extensions install <目录或 GitHub 地址> [--ref --auto-update --pre-release --consent --skip-settings]`；**必须由用户在终端确认文件夹信任**，标准输入为空时中止；确认后落在 `~/.gemini/extensions/<name>/`，并生成 `extension-enablement.json` 与 `~/.gemini/extension_integrity.json`（签名与哈希），所以不能由 agenthelm 直接写扩展目录 | 实测 |
| 技能 | `gemini skills link <技能目录> --scope user\|workspace` 建绝对路径符号链接：用户 `~/.gemini/skills/<名>`，工作区 `<cwd>/.gemini/skills/<名>`；文档另列 `~/.agents/skills/` 与 `.agents/skills/` 别名（未观察） | 链接实测，别名官方文档 |
| 信任 | 未信任的工作区跳过项目级技能与 hooks（stderr 提示） | 实测 |
| `extensions list -o json` | 输出在 stderr；键 `name version path contextFiles[] installMetadata{} mcpServers{} isActive id resolvedSettings[] skills[] agents[]` | 实测 |
| hooks | 配置在 `settings.json` 的 `hooks` 对象，扩展内在 `hooks/hooks.json`；事件 `SessionStart SessionEnd BeforeTool AfterTool BeforeAgent AfterAgent BeforeModel AfterModel BeforeToolSelection Notification PreCompress`；`SessionStart` 输出 `hookSpecificOutput.additionalContext`；stdin 字段 `session_id transcript_path cwd hook_event_name timestamp` | 官方文档 |
| MCP | 在 `settings.json` 的 `mcpServers`，键 `command url httpUrl args env cwd headers trust …`；`$VAR` 展开只在 `env` 对象里有文字依据 | 官方文档，**未验证** |

### 2.4 一个会影响设计的对照

- Gemini 的 hook 输入字段与 Claude 的几乎相同，**按载荷嗅探客户端会误判**（现有 `agent_pack_nudge.detect_client` 靠载荷字段区分 Cursor、Codex、Claude）。设计结论：hook 命令一律显式带 `--client <id>`，嗅探只保留给已有安装。
- 同一命令输出可能写在 stdout 或 stderr（agy 帮助、Gemini `extensions list -o json`），`cli` 探针要声明读取哪个流。

---

## 3. 现状盘点：按名字分支的位置

下列位置按协议维度分类，是「一次清零」的迁移清单。行号为 `v6.14.0`。

- **存储表重复四份**：`agent_pack_clients.py`（注册表）、`agent_pack_inventory.py:48-77`、`agent_pack_relocate.py:32-34`、`agent_pack_config.py:611-613`；Phase 1 只用测试对账。→ 单一来源（§4.5）。
- **枚举仍是字面量**：`agent_pack_config.py:2167-2208` 的 `--client` 选项共四处，`mcp_server.py` 的 `enum` 共四处。→ 读注册表。
- **已装插件状态读取**：`agenthelm.py:312-380`，三种布局（Claude 缓存目录、Codex 缓存目录、Cursor 目录解包）。→ 分发类型的 `installed_state` 读取器，由声明选择（§4.3）。
- **安装与更新命令**：`agenthelm.py:1230-1232`、`1888-1903`、`1943-2003`；Claude 与 Codex 用不同的动词（`plugin install` 与 `plugin add`），Cursor 用目录解包。→ 声明里的步骤模板（§4.3）。
- **每客户端文案**：`agenthelm.py:493-495`、`2130-2132` 的恢复提示与「版本没变」解释。→ 声明里的文案键，文案本身保留在 `locale`。
- **能力差异**：`agenthelm.py:568`、`2084`、`2471`（只对 Claude、Codex 做 legacy 清理与更新）、`632`（Cursor 重装会重写所有插件）、`704-705`（Claude 的市场固定）。→ 能力标志（§4.8）。
- **优先级规则**：`agent_pack_inventory.py:758-801`（Claude 的 local > project > user，Codex 同名都加载，其余未文档化）。→ 声明里的 `precedence` 条目与出处（§4.5）。
- **Codex 的 TOML 写入与残留清理**：`agent_pack_config.py:1300-1384`、`1636-1789`。→ `toml` MCP 格式适配器与分发类型的 `cleanup` 步骤。
- **Cursor 的 `headers` 限制**：`agent_pack_config.py:1143`。→ MCP 适配器的 `remote_headers` 能力。
- **nudge**：`agent_pack_nudge.py:62`（停止提示的默认值）、`188-191`（载荷嗅探）、`305-405`（各客户端输出格式与事件差异）。→ hook 协议（§4.7）。
- **整模块按客户端命名**：`agent_pack_claude.py`、`agent_pack_codex_mcp.py`、`agent_pack_mcp_write.py`（只写 Claude）、`agent_pack_scope.py`。→ 重命名为按格式或按分发类型的模块（§8）。

---

## 4. schema v2

一份声明是一个 JSON 文件，built-in 在 `runtime/drivers/`，用户级声明沿用 `~/.config/agenthelm/clients.d/`（只校验，见 §4.9）。下面按协议问题组织字段。字段名为设计建议，评审可调。

### 4.1 顶层

- `schema_version`：2。读取 `schema_version: 1` 的声明时按 v1 子集处理，不报错。
- `id`、`display`：同 v1。
- `lifecycle`：`active`（默认）或 `deprecated`；`deprecated` 在 `agenthelm clients` 里显示，不改变其他行为。
- `capabilities`：布尔标志，见 §4.8。
- `unsupported`：`[{feature, reason}]`，`feature` 取 §6 矩阵里的功能名。**写出「不支持」比静默忽略好**：对应命令遇到它时给出这条理由。

### 4.2 检测

- `detect.executables`、`detect.dirs`：同 v1（只查找，不运行）。
- `detect.cli`（可选）：`{argv, stream, parse}`。`argv` 是固定的只读命令（如 `["plugin","list","--json"]`），`stream` 为 `stdout` 或 `stderr`，`parse` 为 `json` 或 `text`。用于取得 `cli` 级证据；**检测本身不运行它**，只有 inventory 与 doctor 在用户已请求时运行。
  - **批 B 已实现**：`agent_pack_clients.probe_problem` 校验声明（`argv` 为 1–16 个只含字母、数字与 `. _ : = @ / + -` 的参数，可执行文件取自 `detect.executables`，因此声明 `cli` 必须同时声明 `executables`）；`agent_pack_probe.run_probe(probe, executable, runner, timeout)` 是纯函数运行器，子进程函数由参数注入。默认运行器用列表形式、不经 shell、标准输入为空、有超时，只读不写。失败原因取 `invalid-probe`、`not-found`、`timeout`、`unreadable`、`nonzero-exit`、`too-large`（输出超过 1 MiB）、`unexpected-format`；失败的探针不构成任何证据。非零退出码按失败处理（假设：本版没有已知客户端在成功时返回非零，**未验证**）。本版没有内置驱动声明 `detect.cli`，也没有命令行开关调用它（接线在批 E）。用户级声明里的 `detect.cli` 与 `precedence` 一律拒绝。
- 证据等级沿用 inventory 的 `file / setting / cli / unknown`。agy 的插件清单用 `cli` 证据（`plugin list --json`），文件读取作为 `file` 证据对照。

### 4.3 分发

`distribution.type` 仍为 `marketplace-cli`、`directory-dropin`、`none`，新增字段描述步骤：

- `steps`：`add_source`、`install`、`update`、`uninstall`、`verify`，每步是 argv 模板，占位符限 `{cli} {plugin} {marketplace} {source_dir} {version}`。
  - Claude：`add_source = plugin marketplace add {slug}`、`install = plugin install {plugin}@{marketplace}`；Codex：`install = plugin add {plugin}@{marketplace}`；agy：没有 `add_source`，`install = plugin install {source_dir}`（取目录形式，GitHub 与市场形式未验证），`uninstall = plugin uninstall {plugin}`。
  - 「没有这一步」用缺省表示，不用空串。
- `interactive`：`none` 或 `trust-prompt`。`trust-prompt` 表示命令会向用户索取确认（Gemini 的文件夹信任），`--apply` 时继承用户终端，**工具不应答**（D-K6）。
- `source`：`marketplace`（客户端自行取源）或 `bundle-dir`（由 agenthelm 把运行时里的插件目录交给命令，agy 与 Gemini 属此类）。
- `installed_state`：已装插件状态的读取器，取值 `cache-dir-versioned`（Claude、Codex）、`dropin-dir`（Cursor）、`cli-list-json`（agy）、`extensions-dir`（Gemini，目录带签名，**只读**）。
- `reinstall_rewrites_all`：见 §4.8。

**用户级声明里的 `steps` 一律拒绝**（沿用 Phase 1 的 D-R1：让用户文件决定运行什么命令，是任意命令执行面）。built-in 的 `steps` 是随包数据，经测试与代码审查。

**批 C 已实现**（`agent_pack_clients.py` 校验，`agenthelm.py` 读取；与上文的差异如下，均为落地时的必要调整）：

- `distribution` 在 built-in 里是对象 `{type, ...}`；v1 的写法（只写类型名的字符串）仍被接受，等价于 `{type}`，用户声明只能用字符串。`agenthelm clients` 的输出不变（仍报告类型名与原有三个能力标志）。
- 每步是以 `{cli}` 开头的 argv 模板，参数只含 `[A-Za-z0-9._:=@/+-]` 与占位符；步骤缺省不写、不用空列表。`marketplace-cli` 必须有 `install`，且需要 `detect.executables`（`{cli}` 指它）。
- **占位符补 `{slug}`**：Claude 与 Codex 的 `add_source` 需要市场来源（`owner/repo`、镜像或本地路径），而 `{marketplace}` 是市场名 `agenthelm`，两者不同。§4.3 的示例本来就写了 `{slug}`，占位符清单漏列。
- **步骤名补 `update_source`**：Claude 的更新是「先刷新市场、再逐个插件更新」（`update_source` 加 `update`），Codex 一条 `plugin marketplace upgrade` 同时升级市场与已装插件（只有 `update_source`）。只声明 `update_source` 时，该命令的读回验证覆盖所有落后的插件。
- **`installed_state` 取值按布局命名**：Claude 读 `installed_plugins.json` 里的安装记录（`records-json`），不是缓存目录；Codex 读 `plugins/cache/<市场>/<插件>/<版本>/`（`cache-dir-versioned`）；Cursor 读 `plugins/local/<插件>/`（`dropin-dir`）。§4.3 原把 Claude 与 Codex 都归入 `cache-dir-versioned`，与 Claude 的实际布局不符。`cli-list-json`、`extensions-dir` 随 agy、Gemini 在批 E 加入。
- 新增字段：`config_dir`（读取器读的客户端目录，可由环境变量覆盖）、`owned_dirs`（旧安装路径归属判断用的目录）、`cli_locations`（命令不在 PATH 时的查找位置）、`scope_flag`（更新时指定安装范围的选项名）、`messages`（文案槽到文案键的映射；文案本身在运行时与 `locale`，声明不带文字）。
- 能力标志 `legacy_cleanup`、`marketplace_update`、`reinstall_rewrites_all`、`market_pin` 落在 `capabilities`，built-in 可选、缺省为假；`reinstall_rewrites_all` 只放能力标志，不在 `distribution` 重复。
- `interactive: trust-prompt` 与 `source` 取值已校验；`trust-prompt` 的运行时行为（继承终端）随 Gemini 在批 E 实现，批 C 没有声明它的驱动。

### 4.4 内容协议

- `content.manifest`：清单协议 id，取值 `agent-plugins-1.0`、`claude-plugin`、`codex-plugin`、`cursor-plugin`、`gemini-extension`、`antigravity-plugin`。一个客户端可识别多种（agy 识别 `antigravity-plugin` 与 `agent-plugins-1.0`）。
- `content.companions`：该客户端要求出现在插件根目录的伴随文件，`[{path, role, required_when}]`。agy：`mcp_config.json`（`role: mcp`，`required_when: agent-plugins-spec-off`，实测开关疑似关，故按「总是需要」处理）、`hooks.json`（`role: hooks`）。Gemini：`gemini-extension.json`（`role: manifest`）。
- 伴随文件有两种来源。**agent-pack 自己的插件目录**里是随包的静态数据文件（不依赖运行时状态，§10）。**`sync` 生成的可移植包**里，`mcp_config.json` 由 MCP 格式适配器与 `mcp.json` 在同一个函数里从同一组条目生成，两份不会漂移；`hooks.json` 只出现在 agent-pack 自己的目录。一致性由测试保证（§7.4）。
- `content.skills`：`agent-skills`（`SKILL.md`）；`content.context`：上下文文件协议，`agents-md`（原生）、`pointer-file`（需要指针文件）、`unsupported`。本版 agy 与 Gemini 均按 `unsupported` 处理 agents-init 的入口（§6）。

### 4.5 存储与优先级

沿用 v1 的 `stores`，扩展：

- `stores.skills[]`、`stores.mcp[]`、`stores.hooks[]`、`stores.context[]`：每项 `{scope, path, format, status}`，`status` 取 `observed`、`documented`、`undetermined`。**`undetermined` 的存储不被写入，也不参与 `inventory` 的结论**，只在 `agenthelm clients` 里显示。
- `format`：`dir`、`json`、`toml`，另加 `json-hooks`（hook 配置）。
- `precedence[]`：`{kind, rule, evidence, source_url}`，把 `agent_pack_inventory.py:758-801` 的规则搬进声明；`rule` 取 `local-over-project-over-user`、`personal-over-project`、`both-load`、`precedence-undocumented`、`unknown`。
  - **批 B 已实现**：`kind` 取 `skills` 或 `mcp`；条目按声明顺序求值，第一条适用的生效，没有适用的条目即 `unknown`（`store` 为空）。可选的 `when: {scopes, match}` 写条件：同名分组的作用域集合等于（`exact`）或包含（`includes`）所列作用域；可选的 `winner` 指出哪个作用域的存储是生效的那一份（取分组里该作用域的第一个存储，要求有 `when`，且 `winner` 属于 `when.scopes`）。不带 `when` 的条目总是适用，其后不得再有同一 `kind` 的条目。
  - `rule` 的取值在上述五个之外增加两个，因为既有输出已经使用它们：`project-approval-unknown`（Claude 的 MCP 在没有 local 条目时，项目级条目是否已批准未读取）与 `project-trust-unknown`（Codex 的 MCP，项目是否受信任未读取）；二者都是 `unknown` 的细分，`evidence` 为 `unknown`。官方文档链接作为 `source_url` 放在声明里，代码里不再有 `DOC_*` 常量。
  - 求值器是 `agent_pack_inventory._effective`，不含客户端 id；`_skill_effective` 与 `_mcp_effective` 保留为薄包装。
- 存储表的**唯一来源**是声明；inventory、relocate、config 读同一份，测试不再对账三份表。
- **读取属性（批 B 已实现）**：`stores.mcp[]` 的 `json` 与 `toml` 存储必须带 `root_key`（`mcpServers`、`mcp_servers`），其余存储不得带。路径带 `#节名` 表示服务器按项目存放在同一文件内：`data[节名][<项目绝对路径>][root_key]`（Claude 的 local 作用域 `~/.claude.json#projects`），节的形状不符时报 `unknown`，不猜；该约定只适用于 `json` MCP 存储。`managed_by: "agent-pack-state"` 表示该存储里的条目可由 agent-pack 的同步状态记录认出来源（Claude 用户级 Skill 与 MCP）；`trust_required: true` 表示客户端只在受信任的项目里读取该存储，输出带 `codex-project-requires-trust` 注记（Codex 项目级 MCP）。三者都是存储属性，不是能力标志：能力标志会出现在 `agenthelm clients` 的输出里，加它们会改变既有输出。`inventory` 的跨存储判断、`doctor` 读取原生 MCP、Desktop 的路径解析（声明为 `<per-platform>/…` 的存储走平台表，`DESKTOP` 常量由此派生）都读这些声明。读取侧不再有按客户端名字的分支。
- **写入目标标记（批 A 已实现）**：`stores.skills[]` 的条目可带 `write_target: true`，表示 `relocate` 把技能写到这个存储；同一客户端同一作用域至多一项，其余条目只供读取。取这个形状的证据：现有 `relocate` 的目标表与 inventory 的读取集合确实不同（Codex 用户级 `relocate` 写 `~/.agents/skills`，inventory 读 `~/.codex/skills` 与 `~/.agents/skills`；Cursor 项目级写 `.agents/skills`），写入目标总是读取集合里的一项，所以用条目上的布尔标记，不另建一张表。按声明顺序的第一项仍是该作用域的「主存储」（inventory 的引用里，其余存储带 `#agents` 后缀；config 的用户级技能根目录也取它），不需要另加标记。`stores[].status` 缺省表示尚未归类（v1 的既有条目），不等同于 `observed`；批 A 的内置条目不写 `status`。`lifecycle` 缺省为 `active`。
- 共享存储：同一路径可出现在多个驱动里（`~/.agents/skills` 同时是 Codex、Cursor、Gemini 的存储），inventory 去重时按真实路径合并。**批 B 核实的行为**：条目与重复分组都按客户端计，共享路径对每个声明它的客户端各扫描一次，不跨客户端合并；同一客户端的两个存储指向同一真实路径时（符号链接或共享目录本身）关系为 `same-file`，不产生优先级结论；摘要里的 `description_bytes_distinct_files` 对同一真实文件只计一次。以上由合成的第三个客户端的测试覆盖，代码无需改动。

### 4.6 MCP 格式适配

适配器按**格式**命名，选项由声明提供：

- `mcp.adapter`：`json-mcpServers`（Claude、Cursor、agy、Gemini 的 `settings.json`）、`toml-mcp_servers`（Codex）。
- `mcp.options`：`root_key`、`remote_url_key`（`url`、`serverUrl`、`httpUrl`）、`remote_headers`、`env_interpolation`（`none`、`plugin-root-only`、`declared-variables`、`shell-env`）、`type_field_required`。agy 的 `remote_url_key` 为 `serverUrl`，`env_interpolation` 为 `declared-variables`（官方文档，未验证）。
- `mcp.scopes`：声明到存储的映射（沿用 #82 的 `mcp_scope` 语义）。
- 适配器必须容忍**空文件**（agy 全局 `mcp_config.json` 起初为 0 字节）：空文件按空对象处理，写入时不把它判为损坏。**批 B 已实现读取侧**：`agent_pack_store_read` 按格式命名读取适配器（`json`、`toml`），0 字节文件读作空对象；只含空白的文件仍是损坏。改动前 0 字节的 JSON 配置报 `invalid-json`（inventory 里是「无法判定」，`doctor --client claude-desktop` 里是 `desktop-config` 警告），空 TOML 本来就读作空表。写入侧留待后续批次。
- `mcp.write`：`sync` 写入的方式，取 `package`（生成可移植包，由分发步骤安装）或 `user-file`（直接改用户级配置文件）。Claude、Codex、Cursor 沿用现有实现，本文不改。**agy 取 `package`**：生成含 `mcp.json` 与 `mcp_config.json` 的包，再 `plugin install <目录>`；不直接写全局 `mcp_config.json`，也不用 `agy mcp add`（`--env` 的值会出现在进程参数里，flag 顺序有约束）。全局文件只用于 `inventory` 读取（文件证据；`disabled: true` 的条目记为未加载）；`agy mcp list` 是否会启动服务器未知，`doctor` 默认不运行它。

### 4.7 hook 协议

- `hooks.events`：规范事件到客户端原生事件名的映射。规范事件取现有的 `session-start`、`prompt-submit`、`stop`：

  | 规范事件 | claude | codex | cursor | agy | gemini |
  | --- | --- | --- | --- | --- | --- |
  | `session-start` | `SessionStart` | `SessionStart` | `sessionStart` | `SessionStart` | `SessionStart` |
  | `prompt-submit` | `UserPromptSubmit` | `UserPromptSubmit` | 沿用现状 | **不支持**（未验证） | **不支持**（未验证） |
  | `stop` | `Stop` | `Stop` | 沿用现状 | `Stop` | **不支持**（未验证） |

  agy 的 `PreInvocation` 与 Gemini 的 `BeforeAgent`、`AfterAgent` 与规范事件的对应关系没有验证，不猜，标 `unsupported`，需要时由后续议题补充。Claude、Codex、Cursor 的既有映射以现有代码为准，本文不改。
- `hooks.config`：`{location, format, root_file}`。agy：插件根目录的 `hooks.json`，格式 `group-event-handlers`；Gemini：扩展内 `hooks/hooks.json`；Claude：`hooks/hooks.json`。
- `hooks.output`：输出格式适配器名。现有三种保持不变（`hookSpecificOutput` 系、Cursor 的 `additional_context` 系、Codex 的 `decision: block`），**新增 `injectSteps`**（agy：`{"injectSteps":[{"systemMessage":{"systemMessage":"…"}}]}`，来自官方文档，**输出是否被接受未验证**）。Gemini 的 `SessionStart` 与 Claude 同为 `hookSpecificOutput.additionalContext`，复用现有适配器。
- 载荷：hook 命令一律带 `--client <id>`；`detect_client` 的嗅探只保留给已有安装（§2.4）。

### 4.8 能力标志

沿用 `default_set`、`read_only`、`project_mcp_write`，新增：

- `user_mcp_write`、`project_skill_write`、`user_skill_write`：是否支持对应的 `sync` 写入。
- `plugin_hooks`、`plugin_mcp`：随插件交付 hooks 与 MCP 的能力。
- `reinstall_rewrites_all`：重装会重写所有插件（Cursor，对应 `agenthelm.py:632`）。
- `market_pin`：支持市场固定（Claude，对应 `704-705`）。
- `legacy_cleanup`、`marketplace_update`：对应 `568`、`2084`、`2471` 里按名字取的集合。
- `project_trust_required`：项目级内容在未信任的工作区不加载（Gemini）。
- 后续新增标志只追加，不改既有语义。

### 4.9 用户级声明

保持 Phase 1 的行为：只校验，非法的警告并忽略，合法的标「已声明，本版未支持安装」，不执行任何内容，不进入 `--client` 可用集合。v2 的新字段在用户声明里同样只作数据校验，`steps` 与任何形如命令的字段整份拒绝。

---

## 5. 各客户端声明概览

只列与既有驱动的差异和两个新驱动；既有驱动的存储与能力以 `agent_pack_clients.py` 为准，迁移为数据文件时逐字节保持。

- **claude / codex / cursor / claude-desktop**：字段由现有代码与 Phase 1 注册表直接导出，不新增行为。
- **agy**：`lifecycle: active`；`detect.executables: [agy]`，`detect.dirs: [~/.gemini/config]`，`detect.cli: plugin list --json`（stdout，json）；`distribution`：`marketplace-cli`，`source: bundle-dir`，`install = plugin install {source_dir}`，`installed_state: cli-list-json`；`content.manifest: [antigravity-plugin, agent-plugins-1.0]`，`companions: [mcp_config.json, hooks.json]`；`stores.skills.user: undetermined`，`stores.skills.project: .agents/skills（documented）`；`stores.mcp.user: ~/.gemini/config/mcp_config.json（observed，json，只读取）`；`mcp.adapter: json-mcpServers`，`remote_url_key: serverUrl`，`write: package`；`hooks.events`：`session-start`、`stop`；`hooks.output: injectSteps`；`unsupported`：见 §6。
- **gemini**：`lifecycle: deprecated`；`detect.executables: [gemini]`，`detect.dirs: [~/.gemini]`；`distribution`：`marketplace-cli`，`source: bundle-dir`，`interactive: trust-prompt`，`installed_state: extensions-dir`（只读）；`content.manifest: [gemini-extension]`，`companions: [gemini-extension.json]`；`stores.skills.user: ~/.gemini/skills（observed）`、`.agents/skills` 别名（documented）；`capabilities.project_trust_required: true`；`hooks.events`：`session-start`；`hooks.output: hookSpecificOutput`。

---

## 6. 本版支持矩阵（agy 与 Gemini）

| 功能 | agy | gemini |
| --- | --- | --- |
| `agenthelm clients` 检测与能力显示 | 支持 | 支持，标 `deprecated` |
| `install`、`update` | 支持（`plugin install <目录>`；`update` 为卸载后重装，因为没有 `plugin update`） | 支持，预览打印命令，`--apply` 把终端交给用户（D-K6） |
| `inventory` 读 | 支持：`plugin list --json`（`cli`）加文件读取（`file`）；用户级技能目录 `undetermined`，不下结论 | 支持：技能目录与扩展目录，扩展目录只读 |
| `relocate`、`dedupe` | 不支持：技能存储未确定，插件内容不可移动 | 支持：仅限技能目录（`dir` 存储），不处理扩展目录 |
| `doctor` | 支持：插件启用状态、`mcp_config.json` 与 `hooks.json` 是否随包、`enable-agent-plugins-spec` 状态未知时显示「未知」 | 支持：显示文件夹信任对项目级内容的影响 |
| `sync` 写技能 | 用户级随包交付（包形态，`install` 的技能计数已实测）；不写独立的技能目录（用户级目录未确定）；项目级不支持 | 不支持 |
| `sync` 写 MCP | 用户级：生成含 `mcp.json` 与 `mcp_config.json` 的包再 `plugin install <目录>`；项目级不支持（与 Codex、Cursor 相同，项目级插件目录 `.agents/plugins/` 未验证） | 不支持 |
| agents-init 的 `agent_cli` 与 `check`（#115） | 不支持（需要模板变化，影响既有项目） | 不支持 |
| `nudge install-hooks` | 不适用：hooks 随插件交付 | 不适用 |
| `prompt-submit` 事件 | 不支持（未验证） | 不支持（未验证） |
| `stop` 事件 | 支持（输出格式未验证） | 不支持（未验证） |
| Windows | 沿用 §1 的范围 | 同左 |

「不支持」写进声明的 `unsupported`，对应命令遇到时输出该条理由和替代做法，退出码沿用「需要注意」，不静默跳过。

---

## 7. 「零改动」守卫

### 7.1 数据文件化

built-in 驱动从 `agent_pack_clients.py` 里的 Python 字面量移到 `runtime/drivers/<id>.json`；`agent_pack_clients.py` 只保留加载、校验、查询函数。`sync-runtime.mjs` 与发布源码绑定要包含这些 JSON（`release_meta.py` 的 `UNSHIPPED_PARTS` 只排除 `tests/`，已满足，需测试确认）。

### 7.2 守卫一：字面量扫描

`plugins/agent-pack/tests/test_client_protocol_guard.py`：用 `ast` 扫描 `runtime/*.py`（排除 `agent_pack_clients.py`、`i18n_agent_pack.py`），收集所有字符串常量，断言**没有一个等于已注册驱动的 id**；同时扫描 `runtime/locale/*.json` 之外的 `plugins/agent-pack/hooks`、`skills` 目录里的代码文件。唯一的豁免是 `plugins/agents-init/skills/agents-init/launcher.sh` 的第 4 级降级，登记在测试内的豁免表里，每条带 `reason` 与 `until`。**白名单的目标值是除这条豁免之外为 0。**

字面量扫描不覆盖字符串拼接与外部文案；`locale/en.json` 里的客户端名属于文案，由「文案键」引用，不在扫描范围内，但守卫二会检查文案键存在。

### 7.3 守卫二：合成客户端

测试在临时目录里写一份合成驱动（id 取自「未注册」的随机名）和一套夹具（桩可执行文件、配置树、插件包），通过 `AGENTHELM_DRIVERS_DIR`（仅测试可用的加载参数）加载，然后依次运行：`clients`、`install` 预览、`inventory`、`doctor`、`relocate` 预览，断言这些命令对它产生合理输出且**无需任何代码改动**。这一条把「新增客户端只加声明与夹具」从口号变成回归测试。`agy` 与 `gemini` 各自的夹具位于 `tests/fixtures/clients/<id>/`，由参数化测试覆盖。

### 7.4 守卫三：声明自洽

参数化遍历 `drivers/*.json`：词汇合法；存储声明的路径与格式匹配；`unsupported[].feature` 属于 §6 的功能名；每个驱动有对应夹具目录；`companions` 里列出的文件在 `plugins/agent-pack` 内真实存在（agy 的 `hooks.json` 与 `mcp_config.json`）；`mcp_config.json` 与 `mcp.json` 的服务器集合一致（防止两份随包数据漂移）。

---

## 8. 一次清零的迁移批次

同一批文件不并行；并行不超过两个 session。每批的验收是「对照基线逐字节核对既有输出」（沿用 Phase 1 的做法），并在路径长度不少于 138 字符的工作树里跑全套测试。

- **批 A（注册表 v2 与单一存储来源）**：`drivers/*.json`、加载与校验、`agent_pack_inventory.py`、`agent_pack_relocate.py`、`agent_pack_config.py` 的存储表改读注册表，`--client` 选项与 `mcp_server.py` 的 `enum` 改读注册表。风险低，其余批的前置。
- **批 B（检测、inventory、relocate、dedupe、doctor）**（读取侧已完成，见 §4.2、§4.5、§4.6；过渡版守卫 `tests/test_client_protocol_guard.py` 带一张逐文件上限表，上限必须恰好等于当前计数，后续批次减少字面量时同步下调）：`precedence` 与存储进声明，`cli` 探针适配，空文件容忍，共享存储去重。
- **批 C（install、update、legacy）**：`distribution.steps` 与 `installed_state` 读取器取代 `agenthelm.py:312-380`、`1230-1232`、`1888-2003` 的按名字分支，文案走文案键。风险最高，要求既有三家输出逐字节不变，Cursor 的目录解包与 Codex 的残留清理各有回归用例。**已完成**：`agenthelm.py` 的客户端 id 字面量清零（守卫上限表移除该文件）；`agent_pack_claude.py`、`agent_pack_scope.py`、`agent_pack_mcp_write.py`、`agent_pack_codex_mcp.py` 不在 install、update、legacy 的调用路径上（只被 `sync` 与 `agent_pack_config` 的写入侧使用），改名与字面量清零留给批 D；旧安装清理里 `legacy_artifacts` 与 `retire_legacy` 描述的是预发布时代引导脚本留下的一组固定文件，不按客户端 id 分支，未改。
- **批 D（nudge 与 hooks）**：事件映射与输出适配器取代 `agent_pack_nudge.py` 的名字分支，hook 命令带 `--client`，保留嗅探给已有安装，新增 `injectSteps` 适配器与 agent-pack 根目录的 `hooks.json`、`mcp_config.json`。
- **批 E（agy 与 gemini 声明、夹具、守卫收尾）**：两份声明加夹具，守卫一的豁免表只剩 `launcher.sh`，支持矩阵的 `unsupported` 条目与命令输出落地。
- **并行轨 F（Phase 3，§9）**：运行时解析函数与同级插件查找；与批 A、B 文件不交叠时可并行，与批 C 交叠（`agenthelm.py`）时串行。

`agent_pack_claude.py`、`agent_pack_codex_mcp.py`、`agent_pack_mcp_write.py`、`agent_pack_scope.py` 的文件名含客户端名，批 C 与批 D 中按职责重命名（例如 `agent_pack_claude.py` 里的 Claude 插件包构造按「native-package」职责归类），重命名随对应批进行，不单独成批。

## 9. Phase 3：运行时查找

### 9.1 现状（已读到源码）

- `launcher.sh` 的顺序：`AGENTHELM_BIN`、`${XDG_DATA_HOME:-~/.local/share}/agenthelm/current/bin/agenthelm`、PATH 上的 `agenthelm`、客户端私有缓存（第 4 级，三家路径写死在一段内嵌 Python 里）。XDG 优先在这里已经成立。
- `agenthelm.py:773`、`872`、`2814`：同级插件查找先看 `PLUGIN_ROOT.parent / <名>`，再看各客户端已装副本，不先看 XDG 的 `current/plugins/<名>`。
- `agenthelm_runtime.py:97`：按路径片段 `/plugins/cache/` 与 `/.cursor/plugins/` 判断「在客户端里运行」。
- `hooks.json` 的四条命令用 `${CLAUDE_PLUGIN_ROOT}`，`mcp.json` 的 `args` 用 `${PLUGIN_ROOT}`。

### 9.2 方案

- **最终实现（#134 Phase 3）**：`agenthelm.py` 的 `resolve_plugin(name)` 是唯一的同级插件查找，`locate_companion`、`scaffold_module`、`run_project` 都走它。三层按固定顺序，**层与层之间严格按顺序**，一个靠前的层里有可用副本就用它，即使靠后的层版本更高：
  1. 同级目录 `PLUGIN_ROOT.parent/<名>`，只在当前进程不是从客户端缓存运行时计入（`running_from` 不是 `client-plugin`）。
  2. 用户级运行时 `${XDG_DATA_HOME:-~/.local/share}/agenthelm/current/plugins/<名>`。
  3. 客户端私有缓存：各驱动 `runtime_cache` 声明的通配（`env` 覆盖配置目录，默认目录也保留），加上客户端记录里的安装路径。
  同一层内部才比较版本（清单 `plugin.json` 的 `version`，按数字比较），取最高。副本须有 `plugin.json` 并通过调用方给的 `accept` 判断（例如 `project_cli.py` 是否存在）。找不到返回 `Resolution(None, "not-found")`。需要「不能低于某版本」的调用方（内容刷新，#48）传 `minimum`：低于它的副本不计入，过旧的早层副本不会否决靠后层里够新的副本；都不够时仍返回版本最高的那份，由调用方报告「过旧」。`clients` 参数把第 3 层限制在调用方选定的客户端内。
  与草案的差异：显式覆盖环境变量与 PATH 两层未实现（运行时之外没有对应的插件目录）；`locate_companion` 原先把同级与各客户端副本混在一起取最高版本，现改为严格分层。
- 「在客户端里运行」的判断（`running_from`）读驱动 `runtime_cache` 声明的缓存路径模式，取代路径片段 `/plugins/cache/` 与 `/.cursor/plugins/`。
- hook 与 MCP 命令不改为「运行时优先」：插件目录是随版本固定的，运行时 `current` 可能是别的版本，混用会造成版本错配。agy 的根 `hooks.json` 用相对路径还是 `${PLUGIN_ROOT}`，取决于 agy 对 hook 命令的工作目录与变量展开，**未验证**，列入 §12。

### 9.3 `launcher.sh` 的例外（D-K3）

`launcher.sh` 是内容包 `agents-init/project-contract` 的来源文件，改动会触发 `template_changes`，语义变化对既有项目需要用户动作（`refresh`）。第 4 级降级只在运行时、PATH 都找不到时才用，新客户端的用户装的是运行时，走前三级即可。因此本版不改它，守卫登记一条豁免。**代价**：`launcher.sh` 里仍有三个客户端名。**替代**：改造第 4 级，接受内容包 minor 加 `refresh`；可在设计评审时选择。

### 9.4 启动器的适用范围

`launcher.sh` 是 POSIX `sh` 脚本，是 agents-init 为项目根目录生成的「人用入口」（`./menu`），让人不经过 AI Agent 就能打开项目菜单。Windows 原生 shell 不能运行它，仓库口径是「运行时命令不支持 Windows」，本版不改。

---

## 10. 对既有功能的影响

- `inventory`：新增 `cli` 证据来源与空文件容忍；JSON 追加字段，既有键不变。
- `relocate`、`dedupe`：存储表读注册表；对 `undetermined` 存储拒绝并说明。
- `doctor`：新增「开关未知」「项目未被信任」等只读提示，默认 advisory，不改 `ok` 与退出码。
- `nudge`：hook 命令带 `--client`；既有三家的输出逐字节不变。
- agent-pack 插件新增两个静态文件：根目录 `hooks.json`（agy 格式，内容与 `hooks/hooks.json` 等价的子集，命令用 `--client agy`）与 `mcp_config.json`（与 `mcp.json` 同一服务器集合，agy 格式）；其他客户端忽略它们。`sync` 生成的包里的 `mcp_config.json` 由适配器生成，不属于这两个静态文件。
- 会话开始注入的文本总量不变（`docs/design/progressive-loading.md` §1.5 的约 2 KB）。
- SKILL.md 体积：相对 `v6.13.1` 的净增长预算只剩 2 字节，本版不改 `SKILL.md`，说明放在 `references`。

## 11. 版本与发布声明（建议）

- `agent-pack`：minor；`default_for_existing: off`（新客户端不在默认集合）；无用户动作；`touches: []`。
- 批 A 到 D 不改变既有行为，可并入同一插件版本；每个 PR 一个片段。
- `agents-init`：本版不改，内容包不变；`launcher.sh` 不动。
- 套件：随 `agent-pack` minor；`latest.json` 迁移窗口（#28）另按 `release-hardening.md` 实施。

## 12. 未验证与需要的后续补测

1. **agy MCP 的运行时行为**：`agy mcp` 的落点、格式、键名、`disable` 的落点已实测（§2.2）；`${VAR}` 在 agy 启动 MCP 时是否展开、`disabled: true` 是否真的不启动、包内 `mcp_config.json` 里 `${PLUGIN_ROOT}` 是否展开（文档称展开），需要模型会话。`sync` 的包形态因此要沿用现有的 `mcp_env_launcher` 包装，不依赖客户端展开。
2. **`enable-agent-plugins-spec` 开关的存放位置与切换方式**：在 `config.json` 里没有找到；`mcp.json` 是否被运行时使用只有推断。本设计按「总是随包带 `mcp_config.json`」处理。
3. **根目录 `hooks.json` 对 Claude 格式文件的接受程度**：`hooks/hooks.json` 被警告未加载；根目录 `hooks.json` 的接受格式只验证了 agy 自有格式。需要一次 `validate` 与 `install` 演练（夹具为 agent-pack 的 hook 文件放到根目录）。
4. **agy hook 命令的工作目录与变量展开**（文档称插件 hook 的 cwd 是插件根目录）、`injectSteps` 输出是否被接受、各事件是否触发：需要模型会话。
5. **MCP 是否真的加载、`${VAR}` 是否展开**：需要模型会话。
6. **agy 的用户级与项目级技能目录**：文档列出两个候选，实测机上都不存在，没有 CLI 命令可以创建技能；声明为 `undetermined`。
7. **Gemini 的 MCP、`$VAR` 展开、`.agents/skills` 别名、AGENTS.md 处理**：只有官方文档依据。
8. **agy 的 GitHub 地址与市场形式安装**：只文档化，未试。
9. **Agent Plugins 1.1.0 草案的可移植 hooks**：草案正文未读；它若定稿，`hooks.events` 的映射可以简化。

## 13. 决策记录

以下各项已由维护者在 2026-11-22 确认「按倾向」，D-K11 在此前已单独确认；括号里的替代做法保留，作为将来推翻时的参照。

- **D-K3（已决）** `launcher.sh` 的例外保留（替代见 §9.3）。
- **D-K8（已决）** `hooks.output` 的 `injectSteps` 适配器本版随 agy 一起发（标「输出是否被接受未验证」，验收用模型会话冒烟）。
- **D-K9（已决）** agent-pack 自己的插件目录里的 `hooks.json` 与 `mcp_config.json` 静态提交，加一致性测试（替代：安装时生成，会使安装结果依赖运行时状态，且 `agy plugin install` 复制的是目录）。`sync` 生成的包不受此影响，始终由适配器生成（§4.4）。
- **D-K11（已决）** agy 的 `sync` 顺带支持用户级技能（包形态已含技能，`install` 的技能计数已实测，不需要新存储；替代：在声明里写 `unsupported`，与最初的支持范围一致）。
- **D-K10（已决）** `agy` 的 `update` 用「卸载后重装」实现（因为没有 `plugin update`；风险是失去已有的启用状态，需要在预览里写明，并在重装后按读回的 `enabled` 恢复）。

## 14. 术语对照

- 驱动（driver）：以数据声明一个客户端如何被识别、如何接收插件、存储在哪里、有哪些能力。
- 清单协议（manifest protocol）：插件或扩展的元数据文件格式，如 `plugin.json`、`gemini-extension.json`。
- 伴随文件（companion file）：某客户端要求出现在插件根目录的额外文件，如 agy 的 `mcp_config.json` 与 `hooks.json`。
- 棘轮（ratchet）：只允许下降、不允许回升的约束；本版守卫的白名单目标是 0，除一条登记的豁免。
- `cli` 证据：由客户端命令输出得到的事实，区别于 `file`（读文件）与 `setting`（读设置）。
