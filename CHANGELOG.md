# 更新记录

## 2026-10-21 — agenthelm 市场：Agents Init 6.5.0 / Agent Pack 1.6.0 / Project Orchestrator 1.7.0 / Opinion Manager 0.4.1

仓库由 lc4t/skill 改名为 lc4t/agenthelm（旧地址由 GitHub 重定向）。四个插件改为经 agenthelm 市场分发：一份 `.claude-plugin/marketplace.json` 同时供 Claude Code、Codex 与 Cursor 读取，`latest.json` 作为云端版本源，`npx @agenthelm/cli` 负责安装、检查、更新与迁移。

### 发布与更新

- `scripts/release_meta.py` 从各插件 `plugin.json` 生成市场清单与 `latest.json`；Claude Code 只按市场条目的 `version` 判定更新，条目版本因此统一由脚本生成。
- 版本门禁：插件或内容包改动未升版本即失败；升版本必须在 `release/migrations.json` 写明 `change_kind`（code / content / both）、是否不兼容与需要的动作。
- 内容包：插件在 `content.json` 声明模板与样例（独立版本与文件摘要）。只改模板或样例的发布被单独标出，由用户选择是否更新。
- 打 tag 发布：构建发布包并上传摘要，Pages 只在 tag 与 main 的插件版本一致时把发布包写入线上 `latest.json`；`@agenthelm/cli` 版本变化时发布到 npm。

### agent-pack 1.6.0

- 新增 `runtime/agenthelm.py`（`npx @agenthelm/cli` 调用同一份代码）：`check` 只读比较 Claude Code、Codex、Cursor 与旧方式安装的版本、项目模板与 Opinion；`install` / `update` 调用各客户端原生市场命令，Cursor 从带摘要的发布包安装；`migrate` 把旧方式安装移入恢复目录并改为市场安装；`doctor` 报告重复安装与版本不一致。写操作默认预览，`--apply` 才执行。
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
- 旧方式安装（agent-pack 复制到 `~/.agents/plugins/plugins/<id>`、`~/.claude/skills/<id>` 软链、`~/.claude.json` 中的 agent-pack MCP）与市场安装并存会重复；用 `npx @agenthelm/cli migrate --install` 迁移，旧文件进入 `~/.agent-pack/recovery/` 并附清单。
- 按 `~/.agents/plugins/plugins/agent-pack` → `~/.claude/agent-pack/plugins/agent-pack` → `~/.cursor/plugins/local/agent-pack` 查找 agent-pack 的下游工具，需改为读取客户端安装记录（Claude Code `~/.claude/plugins/installed_plugins.json`、Codex `~/.codex/plugins/cache/agenthelm/`）或调用 `npx @agenthelm/cli doctor`。
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
