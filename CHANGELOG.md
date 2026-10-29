# 更新记录

## 2026-10-28 — agenthelm 6.9.0 / Agents Init 6.8.0 / Project Orchestrator 1.10.0：流程（SOP）管理

把用户常用的流程固化成 SOP（Standard Operating Procedure，标准作业流程），之后让 Agent 按它执行或由场景触发。典型例子：处理一个视频（下载 → 转音频 → 本地或云端转录）、每周复盘。SOP 里工具怎么选、出错怎么办、中间产物放哪，都是用户自己的习惯，建立时逐项谈清，不替用户决定。

### project-orchestrator 1.10.0

- 新增 `runtime/sop.py`（标准库；也可用 `agenthelm project sop <子命令>`）。一个 SOP 一个目录 `sops/<id>/SOP.md`，用户级 SOP 在 `~/.config/agenthelm/sops/`（`AGENTHELM_SOP_HOME` 可改），同 id 时项目级覆盖用户级。
  - **创建**：`new` 从样例、现成文件或已完成的 Case 生成草稿；草稿不会被触发。
  - **谈清约定**：`interview` 给出编号问题清单（触发、前置条件、出错、缺工具、中间产物、失败后恢复、花钱步骤、范围与公开边界）；`defaults --set` 把项目习惯一次谈好，新 SOP 沿用；`decide` 记录用户已确认的事项；有未确认的事项时 `activate` 拒绝启用。
  - **工具检查**：前置条件按种类声明（命令、Python 模块、环境变量、路径、技能、MCP、手动确认），可写安装方式、替代方案与缺失时的处理；`preflight` 检查就绪 / 缺失 / 待核对，环境变量只报告有无、不显示值；步骤里用到但没声明的工具在校验中报错；SOP 中疑似密钥会被拒绝。
  - **触发**：`index`（每个已启用的 SOP 一行）、`match`（关键词预筛）、`due`（周期到期，依据执行 Case 的创建日期）、`events`；默认命中后先问用户，到期只提醒，不自动执行。
  - **事件触发**：SOP 用 `on_events`（`case.created` / `case.closed`）声明监听，`event_tags` 限定只对带某些标签的 Case 触发。默认 Case 工具新建 Case、把 Case 关闭时，`case.py` 与项目入口的 `record` 在结果里带 `sop_events` 和一段提示，列出命中的 SOP 与可直接运行的命令；`sop events <事件> --case <Case>` 供自有 Case 工具的项目查询；`sop run --event … --event-case …` 把触发它的 Case 记进执行记录。SOP 自己的执行 Case 不再触发事件，避免循环；草稿与已退役的 SOP 不监听；SOP 目录有问题时不影响 Case 的写入。
  - **范围与私有目录**：创建时必须谈清第 8 项 `scope`——读取哪些事项、是否用到私有目录、产物放进哪个项目、那个项目会不会公开。会读取多个事项的流程（复盘）必须先由用户指定范围，不默认纳入全部。私有目录（`privacy.forbidden_default_reads`，缺省约定 `private/`）三条约定：读取前须经用户确认（`sop confirm-read` 记入 Case 的决策记录）；内容默认不进入 git（目录里的 `.gitignore`，`sop private --apply` 补上，`health` 提示未忽略或已被跟踪的文件）；用到它的 Case 标为 `sensitivity: private`（SOP 引用私有路径时 `run` 自动标，`case.py update` 登记私有路径的交付物或证据时自动标，也可 `--sensitivity`）。这是协议级约束，工具层挡不住 Agent 直接读文件。公开样例只含流程结构，不含任何个人进度或数据（有测试检查）。
  - **条件性前置**：前置条件可写 `仅当：<条件>`，例如 B 站视频用 yutto、其他站点用 yt-dlp。条件项缺失不影响“就绪”判断，`preflight` 单独列出，由 Agent 对照本次输入判断是否成立。
  - **执行**：`run` 建立一个 Case，固化执行约定与步骤清单；`step` 记录每步完成 / 跳过 / 失败、登记中间产物并更新下一动作；`status` 核对进度。一次执行的状态只记在 Case 里。
  - **版本**：`revise` 升版本号、写修订记录、保存快照；内容变了但没升版本时 `validate` 警告，`run` 拒绝执行。
  - 写入类命令默认只预览，`--apply` 才写入，写后读回；`--expect-sha256` 防止覆盖别人刚改的内容。
- `agenthelm project sop …` 与菜单第 8 项「流程（SOP）」；`health` 汇总流程的校验问题与到期提醒（建议处理，不改变退出码）。
- 演示模式（`--demo`）也支持流程：直接运行真实的 `sop.py`，但项目、SOP 与 Case 都在用完即删的临时目录里，不碰任何真实文件；非空场景预置 `weekly-review` 与 `video-to-transcript` 两个已启用的流程。唯一读取真实环境的是“执行前检查”（看这台机器上有没有对应命令与环境变量）。
- Case Schema 新增可选字段 `sop`（`<id>@<版本>`）；`case.py new --sop`。内容包 `project-orchestrator/case-template` 1.1.0，新增样例内容包 `project-orchestrator/sop-samples` 1.0.0（`blank`、`video-to-transcript`、`weekly-review`、`knowledge-digest`，创建的都是草稿；视频样例按 yutto 官方文档写了 B 站下载：`yutto <链接>`，`--audio-only` 只取音频，`-d` 指定目录，依赖 FFmpeg，登录信息用其 `--auth` / `yutto auth login`，凭据不进 SOP）。
- 新增 `references/sop.md`（契约与执行协议）与 `references/sop-interview.md`（与用户建立 SOP 的谈话流程）；`SKILL.md` 增加 3.1「流程（SOP）」。Agent 协议要点：缺工具时一次性列给用户并等待，不自行安装软件；SOP 不放宽任何授权；只清理登记过的中间产物。

### agents-init 6.8.0

- 新项目的 Profile 声明 `privacy.forbidden_default_reads: ["private/"]` 并建立 `private/.gitignore`；骨架清单对声明了 `private` 的项目才检查它，已有项目不多出差异。
- 新项目的 Profile 带 `sop: {"root": "sops"}` 并建立 `sops/`；`project.schema.json` 声明 `sop` 段。
- 骨架清单（内容包 `agents-init/project-skeleton` 1.1.0）新增 `{sop_root}/.gitkeep`，条件为 `sop`：只有 Profile 声明了 `sop` 段的项目才检查它，没有声明的已有项目 `check` / `health` 不会多出任何差异（两台机器的期望清单版本不同时，指纹仍会因版本不同而不同，这是既有行为）。

### 不兼容点与注意

- **行为变化：资料索引不再过滤 `sensitivity` 非 `shared` 的 Case**（`find` / `refs` / `check` 照常登记并能找到它们的文件）。索引只是用户缓存里的本机索引，不对外公开；读取私有内容前是否需要确认，由私有目录的约定管。此前依赖“private 的 Case 搜不到”的项目，需要改用私有目录（`privacy.forbidden_default_reads`）把内容放在资料根之外。
- 无不兼容变更。没有 `sops/` 目录和用户级 SOP 时，SOP 相关功能静默跳过。
- 自定义了 `work.case.schema` 的项目：想让 Case 记录 SOP 来源，需要在自己的 Schema 增加可选字符串字段 `sop`。
- `agenthelm project` 的子命令后面的 `--output json` 现在由入口统一处理（此前各子命令在解析阶段就取值）。
- 用户级 SOP 与项目习惯里的路径由用户自己确认；写入项目外目录的中间产物需要用户先确认该路径。
- 只在 macOS 与 Linux 上验证过；`preflight` 对技能、MCP、其他 Python 环境只能检查常见位置，查不到时标为「待核对」，由 Agent 或用户确认。

### 版本

agenthelm 6.9.0、agents-init 6.8.0、project-orchestrator 1.10.0；agent-pack 1.9.0、opinion-manager 0.4.2 不变。

## 2026-10-27 — agenthelm 6.8.0 / Agents Init 6.7.0 / Agent Pack 1.9.0 / Project Orchestrator 1.9.0：脚手架一致性

在多台机器、多个客户端上使用同一个项目（例如放在同步盘里）时，只要装好 agenthelm，在任意机器运行 `agenthelm check`（或项目里的 `./menu health`）就能知道：这台机器和这个项目的脚手架是否与最新版一致，两台机器得出同样的结论；有差异时有配套工具修复。不同项目的 Opinion、Profile 里的自有字段与审计字段不参与比较，也不要求统一。

### agents-init 6.7.0

- 新增内容包 `agents-init/project-skeleton` 1.0.0：期望骨架清单（`skills/agents-init/skeleton.json`）声明项目应有的文件（是否必需、比较方式 `exists` / `dir` / `lock` / 不比较 Opinion）、Profile 受检字段，以及不参与比较的审计字段与 Opinion 设置。它不写进项目内容锁，所以不会让已有项目出现新的“可更新”。
- 新增 `scripts/scaffold.py`，`init_project.py` 新增模式：
  - `--mode check`：只读，列出缺失的骨架文件、与当前模板不同且没有基线的模板文件、缺失或不符的 Profile 字段、内容锁版本未知、Case 目录命名不符，附脚手架指纹；
  - `--mode add-skeleton [--target <路径>]`：按模板创建缺失文件，已存在的不动；
  - `--mode profile [--target <字段>]`：只改所选字段，输出 diff，替换前备份；
  - `--mode repair-lock`：把内容锁里的 `0.0.0` 换成真实版本（只在记录的基线都等于当前模板时）；
  - `--mode decide`：记录 / 撤销对某个发现的决定；
  - `--mode case-naming`：只输出 Case 目录改名计划和受影响的引用，不移动任何目录。
  以上默认只预览，`--apply` 才写入。
- **修复**：`--mode entry --apply` 在项目还没有该内容包的锁条目时，不再写入 `0.0.0`（这会让 `check` 立刻提示“0.0.0 → x.y.z 可更新”），而是写当前包版本。已存在的 `0.0.0` 用 `repair-lock` 修复。
- `refresh` 的 `--keep` 记入决定文件：同样的模板内容以后不再逐个追问，模板对该文件的渲染结果实质变化时才再问。
- `project.schema.json`：声明 `runtime.source_*` / `plugin_version` / `capability_manager_version` 与 `opinion.tool_version` 为审计字段（运行时不读取、不比较）；声明 `work.case.directory_naming`（`style`: `case-id` / `date-topic` / `custom`，`pattern`）与 `work.case.command`。其余自定义字段照常保留。

### agent-pack 1.9.0

- `agenthelm check` 纳入项目脚手架（仍然只读，只写版本缓存）：
  - 发现按 ID 列出（`skel:<路径>`、`content:<路径>`、`profile:<字段>`、`lock:<包>`、`case-naming`）；
  - `--show-skipped` 列出被跳过的项和理由；`--reprompt` 忽略已记录的决定重新评估；`--strict` 在有未解决的脚手架差异时也返回 1；
  - `--fingerprint` 输出脚手架指纹与逐项明细（骨架状态 + 受检 Profile 字段 + 内容锁 + 命名规则；不含 Opinion、项目自有字段、已跳过项）；`--explain` 逐项列出明细，带另一台机器 `check --fingerprint --output json` 的输出文件时指出两台差在哪里（有差异退出码 1）。
- 新增 `agenthelm fix`，等同 `agenthelm project fix`。
- 本机状态：运行时未安装的提示现在说明 `agenthelm` 与 `wf` 命令在 PATH 上是否可用（运行时的 `--link-bin` 只链接 `agenthelm`，不提供 `wf`；`./menu` 通过客户端插件兜底）；检测项目是否位于 iCloud / Dropbox / OneDrive / Google Drive 目录（按路径判断，不读取、不改动任何文件）。本机类发现的决定存在用户状态目录（`$XDG_STATE_HOME` 或 `~/.local/state` 下的 `agenthelm/decisions/`），不进项目。

### project-orchestrator 1.9.0

- `health` 汇总脚手架差异，级别为“建议处理”，不改变退出码；每项给出 ID 和 `fix --skip` 的写法；`--show-skipped` / `--reprompt`。
- `fix` 与发现一一对应：

  | 发现 | 工具 |
  | --- | --- |
  | 骨架文件缺失 | `fix add-skeleton [路径…]` |
  | Profile 缺字段或不符 | `fix profile [字段…]` |
  | 模板文件与新版不同 | `fix update-content`（`--keep` / `--accept`；没有内容锁或模板包版本未变时也能处理；`--keep` 持久化） |
  | 内容锁版本不对 | `fix repair-lock` |
  | Case 命名与声明不符 | `fix case-naming`（只给计划，不移动目录） |
  | 运行时未安装 | `fix install-runtime`（提示说明命令可用性与 `--link-bin`） |
  | 位于同步盘 | `fix sync-folder`（只给指引与 `.gitignore` / `.nosync` 方案，不删除、不移动任何内容） |

- 决定：`fix --skip <ID> --reason <文本>` / `--unskip <ID>` 写入 `.agents/moe.sakanano.agent-pack/check-decisions.json`（随项目同步，一次决定对所有机器生效）；默认只有期望内容的指纹变化才再次提示。交互菜单里每项可选 采用 / 永久跳过 / 稍后 / 查看差异。
- 退出码沿用原约定：`--skip` 了现在不存在的发现为 2；决定文件损坏为 4；决定文件刚被另一台机器同步更新为 5。

### 不兼容点与注意

- 没有决定文件、没有脚手架差异的项目，`check` / `health` 的输出与退出码不变。有差异的旧项目会多出“建议处理”的脚手架项；`agenthelm check --strict` 与会话开始的一行提示会把它们算作未处理。
- `--mode entry` 之后写入的内容锁版本从 `0.0.0` 变为当前包版本。依赖 `0.0.0` 的脚本需要调整（已有的 `0.0.0` 不会被自动改写，需 `fix repair-lock`）。
- `wf` 仍是单独的查找命令，运行时不提供它。

### 版本

agenthelm 6.8.0、agents-init 6.7.0、agent-pack 1.9.0、project-orchestrator 1.9.0；opinion-manager 0.4.2 不变。

## 2026-10-25 — agenthelm 6.7.0 / Agent Pack 1.8.0：Claude Code 端补齐、对外元数据英文优先

### agent-pack 1.8.0：Claude Code 端

- `sync` / `reconcile --client claude`：
  - 按项目和 Claude 的 `mcp_client_policy` 单独生成包，放在 `~/.claude/agent-pack/packages/<包名>/`，安装记录改为指向它，不再借用 Codex 的包目录。
  - `~/.claude/agent-pack/state/<包名>.json` 记下本包装进 Claude 的 Skill 与 MCP。项目增删改会跟着同步，被策略排除的条目会移除；用户自己配置的条目从不改动。
  - 与用户同名条目冲突时需要 `--replace`。默认只预览；`--apply` 时旧内容进恢复目录，失败整体回滚。
  - 旧的安装记录指向别的目录（例如用 bootstrap 指向 Codex 包）时，第一次同步自动迁移。此后 Codex 端的包怎么变、是否删除，都不影响 Claude。
- **本机认证保留**：MCP 条目里本机才有的认证值（`headers` / `env` 中项目没有的键、占位符或凭据类键）在同步、bootstrap 与替换时默认保留；`--reset-overrides` 才丢弃。结果只列出键名，不输出值。替换下来的条目存入 `replaced-mcp.json`，权限 0600。
- **`bootstrap --client claude` 按组件安装**：新增 `--skill` / `--mcp`（可重复），只处理选中的组件。
- **`doctor --client codex|cursor|claude`**（可重复）：对比客户端与项目权威，报告缺失、内容不同、本包装过但项目已没有、悬空链接、安装记录依附在别的客户端包上，计入 `issues`；只差本机认证的条目计入 `warnings`；用户自己的条目和被策略排除的条目只列出。不加 `--client` 时行为不变。
- MCP Tool 同步更新：`agent_pack_sync` / `agent_pack_reconcile` 支持 `claude`，新增 `replace` / `resetOverrides`；`agent_pack_doctor` 新增 `clients`；`agent_pack_bootstrap` 新增 `skills` / `mcps` / `resetOverrides`。
- 核实：启动器配置文件名 `runtime/<哈希>.json` 按 MCP 名字计算，只改配置不会换名字。真正的风险是 Claude 依附在 Codex 的包上：Codex 端删除某个 MCP 或把它排除后，Claude 的条目会失效，Claude 自己的策略也不生效。这次用 Claude 专属包一并解决。
- 文档示例里两个出自具体项目的 Skill 名换成通用示例。

### 对外元数据英文优先（i18n 第一步）

注册表、市场与客户端插件列表里显示的文字改为英文在前，并为以后的语言预留扩展。功能、模板与内容包不变；界面多语言在后续版本。

- **npm 包**：描述改英文；README 每种语言一个文件，`README.md`（英文）与 `README.zh-CN.md`（简体中文）开头互相链接，以后加语言只需加文件（此前 npm 页面没有 README）。
- **市场与插件描述**：市场总描述改英文（由 `scripts/release_meta.py` 生成）；agents-init 插件描述改英文。
- **Codex 插件界面**：四个插件的简介与默认提示改英文；长描述改为 `English … 中文：…` 分段。opinion-manager 与 project-orchestrator 的 `agents/openai.yaml` 同步改英文。
- **Skill 触发描述**：四个 SKILL.md 改为 `English … 中文：…` 分段，中英文请求都能匹配；以后的语言按同样格式追加。Skill 正文仍为中文。
- **llms.txt**：改为英文正文，末尾按“名称 (语言标签): 链接”列出其他语言。站点 `index.json` 的描述改为英文。
- **测试**：新增 `tests/test_public_metadata.py`，守住以上格式；Skill 描述不得含会破坏 YAML 的 `: `，长度不超过 1024。

### 版本

agent-pack 1.8.0、agents-init 6.6.1、opinion-manager 0.4.2、project-orchestrator 1.8.1；后三者只改描述。不兼容点：无。

## 2026-10-25 — agenthelm 6.6.0 / Project Orchestrator 1.8.0 / Agent Pack 1.7.0 / Agents Init 6.6.0

不开 AI Agent 也能用：项目根目录的启动器（缺省 `./menu`）打开项目菜单，用于找资料、看进展、读项目指南、检查与维护、记录事项。它不调用模型，不需要 AI 客户端，只需要 Python 3.11+。

### project-orchestrator 1.8.0

- 新增项目入口 `runtime/project_cli.py`（`agenthelm project`）：交互菜单与非交互子命令 `find` / `open` / `refs` / `progress` / `show` / `guide` / `health` / `fix` / `record` / `tools` / `capabilities`。
  - 非交互时从不等待输入，`--output json` 输出稳定的结构。
  - 退出码：0 成功；1 没有结果或有必须处理的项；2 用法错误；3 运行时缺失或不兼容；4 能力不可用；5 写入冲突；6 执行失败。
- 找资料复用交付物台账，看进展读 `CASE.md` 或项目在 `entry.items` 声明的事项工具。结果显示来源与读取时间，不推断优先级或是否完成。
- 检查汇总以下各项，按“必须处理 / 建议处理 / 可选更新 / 当前不可用”分级，每条写明原因、影响与下一步：
  - 运行环境、入口、资料索引、事项记录、记忆、项目工具；
  - 经 agent-pack 检查的插件、模板、个人规则版本与运行时。
- 修复与更新按“检查 → 选择 → 看影响 → 确认 → 验证”执行，处理项包括：重建索引（只写用户缓存）、更新客户端插件、更新项目模板（改过的文件逐个保留或采用，替换前备份）、安装或更新运行时、补上入口。执行前重新预览，写入范围与预览不一致就不执行。个人规则的新版本只提示，不采纳。
- 记录事项：新建、追加记录、更新下一动作与等待对象、关闭与恢复、登记交付物。写入前核对内容摘要与 `updated_at`，别人刚改过就拒绝写入；`--expect-updated-at` / `--expect-sha256` 支持跨命令核对。项目工具没有写接口时说明缺口，不绕过它改文件。
- 项目工具：`.agents/moe.sakanano.agent-pack/tools.json` 声明的操作只在被选中并确认后运行，参数列表不经过 shell；缺命令、缺环境变量或离线时显示为不可用。
- `--demo study|personal|research|blank` 内置虚构项目，用来体验与测试，结果标注「演示」。
- `case.py` 新增 `plan_create` / `plan_update` 与 `--expect-sha256`；`update` 在替换前再次核对文件内容。
- 内容包 `asset-synonyms` 1.1.0：示例改为“笔记 / 报告 / 照片”等场景中立的词组；`wf` 帮助示例同步。

### agent-pack 1.7.0

- 新增 `agenthelm runtime install | update | use | status`：把四个插件装进用户数据目录 `~/.local/share/agenthelm/runtimes/<版本>/`，并维护 `current` 指针。
  - 同一版本不改写；新版本装在旁边，旧版本保留，可切回。
  - 来源可以是随附内容、联网下载并核对摘要的发布包，或离线的本地目录与 `.tar.gz`。
  - `--link-bin` 建命令链接，不覆盖别人的文件。
- 新增 `agenthelm project`，转交 project-orchestrator 的项目入口。
- npm 包 `@sakanano-moe/agenthelm` 改为携带完整发布内容（`index.json` 与四个插件，不含测试），`npx … runtime install` 由此安装完整运行时。

### agents-init 6.6.0

- 新项目默认生成根目录启动器 `menu`（`--entry-name` 改名，`--no-entry` 不生成）和 Profile 的 `entry` 段；README.md 不存在时新建并写入「怎样使用」，已存在的不改动。
- 启动器按以下顺序查找运行时，从不经过 npx 临时下载代码，都找不到时说明原因与下一步并以退出码 3 结束：
  1. `AGENTHELM_BIN`
  2. 用户级运行时
  3. PATH 上的 `agenthelm`
  4. 已安装客户端里的 agent-pack
- 新增 `--mode entry`，为已初始化的项目补上入口：只涉及 Profile、启动器与内容锁。已有同名文件须 `--keep` 或 `--accept`（替换前备份）；`--readme-entry` 在 README 末尾追加带标记的说明，只追加一次。
- Profile Schema 新增可选的 `entry` 段：`api`、`launcher`、`about`、`guide`、`where`、`start`、`tools`、`items`。这是增量兼容改动，旧 Profile 无需修改。
- 项目契约模板内容包 1.1.0：`AGENTS.md` 增加「人用入口」，内容锁开始跟踪启动器，`refresh` 写入时保持可执行权限。

### 不兼容点

无。没有 `entry` 段的项目仍可用 `agenthelm project`；更新模板时你改过的文件逐个选择保留或采用。接入步骤见 [MIGRATION.md](MIGRATION.md#660给已初始化的项目接入人用入口)。

### 验证范围

macOS（Python 3.14，bash 3.2 的 `/bin/sh`）上全量测试通过。Linux 由 CI（ubuntu，Python 3.11–3.13）覆盖。Windows 未验证，运行时命令在 Windows 上直接拒绝。Linux 桌面上的打开与复制（`xdg-open`、`wl-copy` / `xclip` / `xsel`）没有逐一验证，做不到时打印绝对路径。

## 2026-10-23 — agenthelm 6.5.3 / Agent Pack 1.6.3

- `agent-pack` 1.6.3：客户端命令行先查 PATH，再查应用自带位置（ChatGPT.app 内的 Codex、`~/.local/bin`、Homebrew），找不到时提示可用 `AGENTHELM_CODEX` / `AGENTHELM_CLAUDE` 指定；`migrate` 一并把 Codex 旧的 `plugins/cache/personal/<插件>` 缓存移入恢复目录（`CODEX_HOME` 在家目录之外也能处理）；`check` 把旧方式安装单独标为“旧方式安装，待迁移”，汇总不再把它们算作“可更新”。

## 2026-10-22 — agenthelm 6.5.2 / Agent Pack 1.6.2

- `agent-pack` 1.6.2：`inventory --source client:claude|codex` 纳入市场安装（Claude Code 安装记录与 Codex 插件缓存）；`agenthelm locate` 默认排除旧方式安装，`--include-legacy` 可列出；`check` 的 Opinion 检查在 macOS 不再因 `/var` 符号链接失败。交互命令联网超时从 3 秒放宽到 10 秒；站点 `latest.json` 取不到而改用 GitHub 原始文件时，从 GitHub Release 的 `release.json` 获取发布包信息（tag 所含插件版本一致才采用），Cursor 安装与更新不再失败；解包启用 Python 的 `data` 过滤。
- 发布流水线：同一 tag 只发布一次，Release 已存在时跳过创建。

## 2026-10-22 — agenthelm 6.5.1 / Agent Pack 1.6.1

- 发布版本号与 agents-init 版本解绑：git tag、站点与 npm 包共用独立的发布版本号，各插件只在自身改动时升级。
- `agent-pack` 1.6.1：未安装任何插件时，`check` 提示运行 `install`，不再显示“全部为最新”。
- 发布流水线：GitHub Actions 升级到 Node 24 运行时的版本；npm 暂存发布在未设置 `NPM_TOKEN` 时改用 Trusted Publishing（OIDC），配置可信发布者后即可删除令牌。

## 2026-10-21 — agenthelm 市场：Agents Init 6.5.0 / Agent Pack 1.6.0 / Project Orchestrator 1.7.0 / Opinion Manager 0.4.1

仓库由 lc4t/skill 改名为 lc4t/agenthelm（旧地址由 GitHub 重定向）。四个插件改为经 agenthelm 市场分发：一份 `.claude-plugin/marketplace.json` 同时供 Claude Code、Codex 与 Cursor 读取，`latest.json` 作为云端版本源，`npx @sakanano-moe/agenthelm` 负责安装、检查、更新与迁移。

### 发布与更新

- 以 MIT 许可证发布（新增 `LICENSE`；各插件 `plugin.json` 与市场条目声明 `license`）。
- 一次发布一个版本号：git tag、站点 `index.json` 与 npm 包 `@sakanano-moe/agenthelm` 均为 6.5.0，`release_meta.py` 与发布流水线校验三者一致。
- `scripts/release_meta.py` 从各插件 `plugin.json` 生成市场清单与 `latest.json`；Claude Code 只按市场条目的 `version` 判定更新，条目版本因此统一由脚本生成。
- 版本门禁：插件或内容包改动未升版本即失败；升版本必须在 `release/migrations.json` 写明 `change_kind`（code / content / both）、是否不兼容与需要的动作。
- 内容包：插件在 `content.json` 声明模板与样例（独立版本与文件摘要）。只改模板或样例的发布被单独标出，由用户选择是否更新。
- 打 tag 发布：构建发布包并上传摘要，Pages 只在 tag 与 main 的插件版本一致时把发布包写入线上 `latest.json`；`@sakanano-moe/agenthelm` 版本变化时发布到 npm。

### agent-pack 1.6.0

- 新增 `runtime/agenthelm.py`（`npx @sakanano-moe/agenthelm` 调用同一份代码）：`check` 只读比较 Claude Code、Codex、Cursor 与旧方式安装的版本、项目模板与 Opinion；`install` / `update` 调用各客户端原生市场命令，Cursor 从带摘要的发布包安装；`migrate` 把旧方式安装移入恢复目录并改为市场安装；`doctor` 报告重复安装与版本不一致。写操作默认预览，`--apply` 才执行。
- `hooks/hooks.json`：会话开始静默检查，每天至多联网一次、同一提示每天一次，离线不报错；`AGENTHELM_NO_UPDATE_CHECK=1` 或 `~/.config/agenthelm/config.json` 的 `"update_check": false` 关闭。
- Codex 适配器生成的 `.mcp.json` 改用 `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}`：Claude Code 与 Cursor 不展开 `${PLUGIN_ROOT}`；Codex 仍读取 `mcp.json`。

### agents-init 6.5.0

- 迁入 `plugins/agents-init/`。初始化器在三种布局中查找同级插件：仓库与 Cursor 本地为同级目录，Claude Code 与 Codex 为插件缓存（Claude Code 以 `installed_plugins.json` 为准）。`--runtime-plugin-root` 同时接受插件根与仓库根。
- 初始化与迁移写入 `.agents/moe.sakanano.agent-pack/content.lock.json`，记录模板渲染文件的安装时摘要；`--mode refresh` 按 Profile 用新模板重新渲染，未改动的文件先备份再替换，本地改动须 `--accept` 或 `--keep`。
- 新项目 Profile 的 `runtime.distribution` 为 `marketplace`；Schema 保留 `bundled` 与 `external`。

### project-orchestrator 1.7.0

- 默认 Case 工具 `runtime/case.py`（官方样例）：`new` / `list` / `show` / `update` / `validate`；项目声明 `work.case.command` 时写操作让位给项目工具。模板与默认 Schema 属于内容包 `project-orchestrator/case-template`。
- 交付物台账 `runtime/assets.py` 与人用查找入口 `runtime/wf.py`（官方样例，只读）：按标题、文件名、日期与同义词查找，同系列版本归组，追踪引用与改动影响面，巡检无人认领的交付物与断链。扫描 `work.case.root` 与新的可选字段 `work.assets.roots`（agents-init Profile Schema 同步加入）；同义词来自项目可选文件 `asset-synonyms.json`，示例属于内容包 `project-orchestrator/asset-synonyms`；台账与“上次结果”存于 `$XDG_CACHE_HOME/agenthelm/assets/`，不写项目；凭据类文件不登记；非 macOS 上打开与显示降级为打印路径。

### opinion-manager 0.4.1

- 声明公开目录的内容包清单；规则、模板与命令行为不变。

### 不兼容点（下游须处理）

- 安装布局：仓库根不再是安装单元，根 `plugin.json`、`.codex-plugin/` 与 `skills/agents-init/` 移到 `plugins/agents-init/`。按旧路径引用这些文件的脚本需要更新。
- 旧方式安装（agent-pack 复制到 `~/.agents/plugins/plugins/<id>`、`~/.claude/skills/<id>` 软链、`~/.claude.json` 中的 agent-pack MCP）与市场安装并存会重复；用 `npx @sakanano-moe/agenthelm migrate --install` 迁移，旧文件进入 `~/.agent-pack/recovery/` 并附清单。
- 按 `~/.agents/plugins/plugins/agent-pack` → `~/.claude/agent-pack/plugins/agent-pack` → `~/.cursor/plugins/local/agent-pack` 查找 agent-pack 的下游工具，需改为读取客户端安装记录（Claude Code `~/.claude/plugins/installed_plugins.json`、Codex `~/.codex/plugins/cache/agenthelm/`）或调用 `npx @sakanano-moe/agenthelm doctor`。
- `agent_pack_config.py` 的 `inventory` / `bootstrap` / `reconcile` / `doctor` 参数与 `mcp_env_launcher.py` 的参数不变；opinion-manager 的命令与状态文件格式不变。

## 2026-10-08 — Agents Init 6.4.0 / Project Orchestrator 1.6.0

- 新项目默认 Case 工作目录，专属资料与交付物就近保存，子工作项留在 Case 内。
- Project Profile 增加 work.mode 与 Case 文件落位声明；既有项目保持原模式，显式迁移保留原配置和 legacy_task 指针。
- 提供 Case 元数据和文件名检索、全量盘点及已审阅目录映射的无损迁移；冲突与路径风险失败关闭，应用前后哈希核验，失败撤回本次移动。
- 初始化模板、迁移器、元数据及兼容测试同步升级。


## 2026-10-08 — Opinion Manager 0.4.0 / Agents Init 6.3.0 / Project Orchestrator 1.5.0 / Agent Pack 1.5.1

- `project-orchestrator` 与 `agent-pack` 改为在本仓库直接维护，移除 `scripts/export_plugins.py`；原导出时的隐私扫描改为 `tests/test_public_privacy.py`，对全部已跟踪文件生效。
- `agent-pack` 1.5.1：文本输出中的操作目标在没有 `to` 时显示 `from`。

### opinion-manager

- 新增可选的分层加载：Profile 可声明常驻模板系列、按需规则束、交付信号与路由，声明随 Profile 一起预览、确认和锁定。带声明的 Profile 使用 `schema_version` 2.1；不带声明的 Profile 仍为 2.0，指纹、锁与正文渲染不变。
- 新增只读 `context` 命令：`core` 输出常驻规则与规则束索引，`bundle` 输出指定规则束，`index` 给出各部分规模，`check` 核对 Agent 声明的交付信号与已读取的规则束。核对失败、未声明或项目设为完整读取时返回 `fallback`。
- `profile` 新增 `--loading-file` 与 `--remove-loading`；`compare` 与修订预览显示声明的前后差异。
- 新增 `lifecycle` 命令，在项目私有目录记录候选、证据快照、人工裁决与灰度使用；所有写入先预览再凭指纹确认，证据保留不可变副本。工具只记账，不发布规则，也不自动晋升。
- `use` 事件可记录逐条规则结果、读取的规则束与漏读；漏读记为阻断。`status` 附带规则与规则束的覆盖视图。
- 同一 Profile 的新版本沿用旧版本中规则指纹未变的使用记录；`queue --titles` 输出未裁决候选的标题清单。

### agents-init

- 骨架增加 `memory/`；`AGENT.RULES.md` 模板增加「记忆」一节，规定写入去向、不写入的内容、召回前核实、客户端私有记忆边界与收尾检查。
- `AGENTS.md` 的 Session 入口增加分层加载与记忆索引两步；`chat-summary.md` 模板明确只保存未决事项与指针，三个固定小节名称不变。
- Project Profile 增加可选的 `memory` 段与 `opinion.loading_mode`；既有 Profile 无需修改即可继续使用。体积预算、保留窗口与复核间隔等参数由各项目设定，不随模板分发。

### project-orchestrator

- 新增只依赖标准库的条目式记忆工具 `runtime/memory.py`：一条记忆一个文件，索引实时生成；提供 `index`、`list`、`check`、`create`、`verify`、`retire`，写入类动作默认只预览。
- `check` 报告结构问题、失效的来源路径、已晋升仍在用的条目，以及项目参数文件声明的索引、启动层与会话摘要预算；到期条目只标注，不计为失败。
- Skill 增加记忆的读取、记录与收尾检查，以及 Opinion 分层加载时的读取与核对步骤。

## 2026-10-02 — Opinion Manager 0.3.0 / Agents Init 6.2.0

- 新增经内容批准的公开目录：五个系列、十个平行变体、31 条原子规则，规则与模板均精确为 1.0.0。
- 首次配置收敛为直接用模板、选择部分条目、从空白开始，另可在已授权范围由 Agent 提出记忆候选。
- Profile 与正文在正式保存前完整预览，用户一次确认，Agent 分别使用各自真实指纹；内容漂移与冲突继续阻断。
- 保持 0.2.0 Profile、锁和正文渲染兼容；登记新占位正文同时保留旧占位识别与人工编辑保护。
- 修正文档和界面旧空目录说明，明确实际 Skill 来源、桌面加载、部分选择的模板追踪及可见目录建议。
- 新增通用首次配置回归测试；公开目录依赖、安装、初始化、升级和修改保护继续核验。测试不代表长期学习效果。


## 2026-09-30

### opinion-manager 0.2.0

- 支持模板系列的平行变体，使用 `family/variant@major.minor.patch` 精确选择；规则与个人 Profile 同样维护独立版本。
- 新增 `publish`、`profile`、`updates`、`compare`、`verify` 命令和 `compose --profile`；发布版本不可覆盖，所有写入需确认完整预览指纹。
- 生成完整 `OPINION.md` 和包含全部来源快照的 `opinion.lock.json`；支持离线重建、内容指纹核验及人工修改保护。
- 提供个人 Profile 派生、三方升级冲突预览、明确的冲突处理和规则停用；查看更新不会改变项目内容。
- 新增版本契约、操作验收说明及真实文件命令测试。公开规则和模板目录保持空白。

## 2026-09-29

### 6.1.0

### Added

- 新增随包安装的 `opinion-manager`，支持从用户说明生成 Opinion、组合已批准模板、逐条选择规则、预览并写入统一的 `OPINION.md`。
- 新增模板版本、稳定规则编号和来源记录机制，为后续模板升级与差异检查提供依据。
- 新增公开模板准入流程。公开模板目录保持为空；只有经过内容检查、测试和用户逐文件明确同意的模板才能加入。

### Changed

- 完整分发包由三个 Plugin 增加为四个 Plugin。
- 新项目的 Project Profile 默认声明 `provider: opinion-manager`；生成的 `OPINION.md` 仍为空白配置入口。
- 初始化完成后询问用户选择自定义生成、已批准模板组合、逐条引导或跳过 Opinion 配置。

## 2026-09-18

### 6.0.0

破坏性变更：`project-runtime` 已拆分为 `project-orchestrator`（生命周期编排）与 `agent-pack`（扩展能力管理），本包不再分发 `project-runtime`。

### Changed

- Project Profile 路径改为 `.agents/moe.sakanano.agent-pack/project.json`，与 `agent-pack` 的读取路径一致。
- Profile `runtime` 区块改为 `skill/plugin = project-orchestrator`，新增 `capability_manager = agent-pack`；`distribution` 变为可选。
- `AGENT.template.md` 升级到 v6.0：五类职责分离，生成的 `AGENTS.md`、`CLAUDE.md`、`AGENT.md` 路由到 `project-orchestrator` 与 `agent-pack`。
- 初始化器写入前校验随包的 `plugins/project-orchestrator` 与 `plugins/agent-pack`。
- `bootstrap_and_init.py` 用 agent-pack 依次安装 `agents-init`、`project-orchestrator`、`agent-pack` 三个 Plugin，新增 `--client claude`。

### Added

- `plugins/project-orchestrator/` 与 `plugins/agent-pack/`：从可信源码白名单导出的独立 Plugin，名称与版本和源头一致，避免同机重复安装。
- `scripts/export_plugins.py`：导出时把 `plugin.json#repository` 改写为本公开仓库，并对改写后内容做隐私扫描。
- `--mode migrate` 检测 v5 Profile：保留项目自定义字段并升级到新路径，旧文件移入项目外恢复目录；初始化模式遇到 v5 项目以 `migrate-required` 停止。
- `--mode migrate` 遇到父目录为符号链接的骨架文件（如 `.agents/skills -> ../skills`）时跳过并在 `skipped_symlinked_parents` 报告，永不穿过链接写入；对这类路径使用 `--replace` 会被拒绝。
- `--skip <path>`（仅 migrate）：既有项目可显式不要某些骨架文件，例如不使用根级 `docs/` 的项目；Profile 不可跳过。

### Removed

- `skills/project-runtime/`、`runtime/`、根目录 `mcp.json` / `.mcp.json`、`scripts/export_project_runtime.py` 及其测试。

## 2026-08-13

### 5.2.2

- 首页移除正式域名与 Git 分支等发布运维信息，只保留 Plugin 版本和更新时间。
- 修复 GitHub Pages 部署清单，公开 `llms.txt`、`INSTALL.md`、Plugin manifest、runtime 与统一安装脚本。

### 5.2.1

- 新增 `llms.txt`、`INSTALL.md` 与 `bootstrap_and_init.py`，支持用户用一句自然语言请求触发完整 Plugin 安装与项目初始化。

### 5.2.0

- 仓库根目录升级为 Agent Plugins 1.0 安装单元，同时分发 `agents-init` 与 `project-runtime`。
- 新增统一 `plugin.json`、`mcp.json`、Codex adapter 与一键 bootstrap 安装入口。
- 初始化器改为从 `AGENT.template.md` 具名中文区块生成 Markdown，并在零写入阶段验证同包 runtime。
- 新增白名单导出脚本、project-runtime 测试及“安装完整 Plugin → 初始化项目 → runtime doctor”端到端验收。
- Opinion 继续保持独立指导/审查能力；公开包不含个人规则、凭据与私有路径。

### Added

- 新增显式 `--mode migrate`：保留未选择的碰撞文件，仅替换逐项指定路径，并要求把原文件备份到项目外恢复目录。
- Project Profile 新增 `credential_env_file` 与 `mcp_client_policy` 空配置，供 project-runtime 安全加载本地凭据和按客户端投影 MCP。

### Fixed

- 初始化 apply 改为全有或全无：任一冲突时零写入，写入中途失败只回滚本次已知文件。
- 使用 descriptor-bound 路径遍历拒绝父目录 symlink，避免骨架写出项目根目录。
- Project Profile 可声明 `native_mcp_sources`，供 project-runtime 纳管历史或 client-local MCP。

### Changed

- 发布 `agents-init v5`，将职责收口为项目骨架初始化与版本迁移。
- 运行期 Task/Case、进度、验证、Git 与 Skill/MCP 管理统一交给外部 `project-runtime`。
- Opinion 保持独立指导/审查能力，公开模板不再内置个人或通用 Opinion 内容。
- Project Profile 使用 `.agents/moe.sakanano.project-runtime/project.json`，便携能力采用 Agent Plugins 1.0 目录。

### Added

- 新增默认 dry-run、冲突不覆盖的 `scripts/init_project.py` 及单元测试。
- 新增空的项目 Opinion 覆盖入口、Agent Plugin/MCP 占位及最小过程文档。

## 2026-07-02

### Changed

- 将 `agents-init` 元数据对齐到 `AGENT.template.md v4.0`，skill 版本更新为 `4.0.0`。
- 按模板内容补充 v4 核心变更、命令入口、治理层级、T1/T2 运行时检查器和初始化产物清单。
- 将 `agents-init` 的 `SKILL.md` 精简为执行入口，完整治理规则保留在独立 `AGENT.template.md`。
- 升级 `skill.json` 元数据，补充 `schema_version`、`entrypoints`、`files` 和 `commands` 字段。
- 明确 `assets/` 非必需目录，仅在存在图片、脚本、示例数据等资源时创建。
- 移除 workflow 中的分支级 `CNAME` 写入，避免误导为单仓库可按分支自动切换自定义域名。
- 调整发布模型为仅 `main` 分支发布到 `skill.sakanano.moe`，`test` 分支仅用于本地预览。

### Added

- 新增 GitHub Pages 自动发布 workflow：`.github/workflows/pages.yml`。
- 初始化云端 Skills 静态站点结构。
- 新增顶层浏览器首页 `index.html`。
- 新增顶层机器索引 `index.json`。
- 新增示例 skill：`agents-init`。
- 新增 `agents-init` 的 AI 执行入口、浏览器页面、结构化元数据和独立 `AGENT.template.md` 模板资源。
- 新增 `.nojekyll`，避免 GitHub Pages 对以下划线开头的路径或文件执行 Jekyll 处理。
