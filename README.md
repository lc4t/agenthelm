# agenthelm

跨 Agent 的项目管理插件集。把你在不同 Agent（Claude Code、Codex、Cursor 等）里的工作方式、项目状态与行为规则固化到项目本地，任一客户端打开同一项目都能接着干。Skill 主体均为中文。

不开 AI Agent 也能用：初始化后的项目根目录有一个启动器（缺省 `./menu`），在终端运行它就能找资料、看进展、读项目指南、检查与维护项目、记录事项。它不调用模型，也不需要登录模型账号，只需要 Python 3.11+。见下文[不需要 AI Agent 的项目入口](#不需要-ai-agent-的项目入口)。

| 插件 | 职责 |
|---|---|
| `agents-init` | 创建或迁移项目骨架与 Project Profile |
| `project-orchestrator` | 管理 Case、能力路由、验证、进度、记忆、用户自己的流程（SOP）与受控 Git 交付；附默认 Case 工具与不需要 AI Agent 的项目入口 |
| `agent-pack` | 在项目与各客户端之间盘点、安装、同步、迁移 Plugin / Skill / MCP；附 `agenthelm` 命令行与用户级运行时 |
| `opinion-manager` | 管理用户确认的 Agent 行为规则：模板、平行变体、精确版本与锁定 |

四个插件在 `plugins/` 下各有 `plugin.json`（Agent Plugins 1.0），由市场清单 [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) 统一列出，市场名为 `agenthelm`。Claude Code、Codex 与 Cursor 都读取这份清单。

## 安装

四个插件需一并安装：`agents-init` 初始化项目时会查找另外三个。

**任意客户端（推荐）**：先预览，确认后执行。需要 Node 18+ 与 Python 3.11+。

```bash
npx @sakanano-moe/agenthelm install
npx @sakanano-moe/agenthelm install --apply
```

它检测本机的 Claude Code、Codex 与 Cursor，分别调用各自的市场命令；`--client claude|codex|cursor` 可只装某一个。

**Claude Code**：一条命令添加市场并安装；`agents-init` 声明了另外三个插件为依赖，会一并装上（需要 Claude Code 2.1.292 及以上）。

```bash
claude plugin install agents-init --marketplace lc4t/agenthelm
```

**Codex**

```bash
codex plugin marketplace add lc4t/agenthelm
codex plugin add agents-init@agenthelm
codex plugin add project-orchestrator@agenthelm
codex plugin add agent-pack@agenthelm
codex plugin add opinion-manager@agenthelm
```

添加市场后，Codex 桌面端也可打开 `codex://plugins/install/agents-init?marketplace=agenthelm` 查看并安装。

**Cursor**：个人使用 `npx @sakanano-moe/agenthelm install --client cursor --apply`，插件装入 `~/.cursor/plugins/local/`，重启 Cursor 生效。Teams / Enterprise 可在 Dashboard → Plugins & MCPs 用 Import from Repo 导入本仓库，并开启 Auto Refresh。

**团队项目**：在项目里运行 `claude plugin marketplace add lc4t/agenthelm --scope project` 并提交 `.claude/settings.json`，成员信任该目录后即可看到市场，各自安装。

## 不需要 AI Agent 的项目入口

```bash
./menu                      # 交互菜单：找资料 / 看进展 / 项目指南 / 检查项目与环境 / 修复与更新 / 记录事项 / 项目工具 / 流程（SOP）
./menu find 读后感 2026-10    # 不进菜单：直接查；--output json 给脚本用
./menu progress             # 按 进行中 / 等待 / 待澄清 / 挂起 分组看事项
./menu health               # 检查依赖、插件、模板、索引、事项记录与运行时，每条都写原因、影响和下一步
./menu --help
```

- **在哪运行**：项目根目录的启动器（`agents-init` 生成，缺省名 `menu`，可用 `--entry-name` 改）等同于 `agenthelm project --project <项目>`；没有启动器的项目直接运行 `agenthelm project`。
- **需要什么**：Python 3.11+ 和一次性安装的用户级运行时，不需要任何 AI 客户端。启动器依次查找 `AGENTHELM_BIN`、用户运行时、PATH 上的 `agenthelm`、已装客户端里的插件，从不通过 npx 临时下载代码；都找不到时会说明原因和下一步。
- **安装运行时**（只需一次）：`npx @sakanano-moe/agenthelm runtime install --apply`；离线时解压发布包后运行 `python3 <解压目录>/plugins/agent-pack/runtime/agenthelm.py runtime install --apply`。运行时装在 `~/.local/share/agenthelm/`，新版本装在旁边、旧版本保留，`agenthelm runtime use <版本> --apply` 可切回。加 `--link-bin ~/.local/bin` 可以直接用 `agenthelm` 命令。
- **断网**：找资料、看进展、项目指南、本地检查和重建索引都能用；检查更新使用缓存并标出缓存时间，需要联网的更新只预览。
- **写入**：修复、更新与记录事项都先显示影响，确认（菜单里输入 `yes`，命令行加 `--apply`）后才执行，执行后回读验证并给出恢复位置。你改过的模板文件逐个选择保留或采用；别人刚改过的事项拒绝覆盖。个人规则（Opinion）的新版本只提示，不会被自动采纳。
- **先看看**：`agenthelm project --demo study`（另有 `personal`、`research`、`blank`）用内置的虚构项目体验，不读写真实文件。
- **项目适配**：项目可在 Profile 的 `entry` 段声明简介、资料位置、自有事项工具与项目专用工具，约定见 [case-workspaces.md「项目入口」](plugins/project-orchestrator/references/case-workspaces.md#项目入口不需要-ai-agent)。
- 只在 macOS 与 Linux 上验证过。

## 流程（SOP）

把常用流程固化成 SOP（Standard Operating Procedure，标准作业流程），之后让 Agent 按它执行，或由场景触发。例子：把视频下载下来、转成音频、再用本地或云端模型转录；每周复盘。

```bash
./menu sop new --slug video-to-transcript --template video-to-transcript   # 预览；加 --apply 创建草稿
./menu sop interview video-to-transcript    # 还要和你谈清哪些事（编号问题清单，含范围与公开边界）
./menu sop preflight video-to-transcript    # 执行前检查所需的程序、模型、环境变量是否就绪
./menu sop activate video-to-transcript     # 谈清并确认后启用；加 --apply 写入
./menu sop run video-to-transcript          # 按它建立一次执行（一个 Case），加 --apply 写入
./menu sop due                              # 哪些有周期的流程到期了
```

- **每个人的习惯由你定**：触发场景、用到哪些工具及替代方案、缺工具怎么办、出错怎么办、中间产物放哪里和完成后是否清理、失败后从哪里重来、花钱或耗时的步骤要不要先问你。创建时逐项谈清；项目级的习惯（`sop defaults`）只需谈一次，新流程沿用。有没谈清的事项，`activate` 拒绝启用。
- **工具检查**：前置条件按种类声明（命令、Python 模块、环境变量、路径、技能、MCP、手动确认），写明安装方式与替代方案；因输入而异的工具写 `仅当`（例如 B 站视频用 yutto、其他站点用 yt-dlp）。执行前 `preflight` 列出就绪 / 缺失 / 待核对；缺失项一次性交给你处理，Agent 不会自行安装软件。API 密钥只写环境变量名。
- **触发**：用户明确指定、Case 事件、周期到期、对话意图，命中后默认先问你；到期只是提醒。草稿不会被触发。事件指新建或关闭 Case，可限定只对带某些标签的 Case 生效；流程自己的执行记录不会再触发事件。
- **执行记录**：一次执行是一个 Case，里面有步骤清单、固化的执行约定、每步结果和登记过的中间产物；SOP 目录里不记执行状态。SOP 不放宽任何授权：写入、外发、删除、花钱类步骤仍需你当次同意。
- **私有目录**：私有信息放在项目根的 `private/`（新项目自动建好，也可在 Profile 的 `privacy.forbidden_default_reads` 声明别的路径）。三条约定，其余照常：Agent 读取前须经你确认；内容默认不进入 git（目录里的 `.gitignore`，云同步盘不受影响）；用到它的 Case 自动标为 `sensitivity: private`。`./menu sop private` 查看状态，`./menu health` 提示未被忽略的私有目录。这是协议级约束，不是访问控制。
- **范围**：会读取多个事项的流程（复盘、汇总）先由你指定范围，不默认读取全部。你自己的流程不要提交到公开仓库，公开的只有通用样例。
- **位置**：项目级 `sops/<id>/SOP.md`（随项目同步），用户级 `~/.config/agenthelm/sops/`（每台机器各一份）。改过内容用 `sop revise` 登记版本，每个执行 Case 记着它用的版本。
- 契约与命令见 [sop.md](plugins/project-orchestrator/references/sop.md)，与用户一起建立的流程见 [sop-interview.md](plugins/project-orchestrator/references/sop-interview.md)；随包样例 `blank`、`video-to-transcript`、`weekly-review`、`knowledge-digest`（样例只含流程结构，不含任何个人进度或数据）。

## 初始化项目

安装后在新会话中让 Agent 使用 `agents-init` 初始化当前项目；它先预览要创建的文件，确认后才写入。也可以直接运行初始化器（路径以实际安装位置为准，在仓库里为 `plugins/agents-init/skills/agents-init/scripts/init_project.py`）：

```bash
python3 plugins/agents-init/skills/agents-init/scripts/init_project.py \
  --project /path/to/project --name example \
  --project-type code --vcs github --stack python \
  --runtime local --agent-cli claude
```

审阅后追加 `--apply`。新项目会生成根目录启动器 `menu`（`--entry-name` 改名，`--no-entry` 不生成）；README.md 不存在时新建并写入「怎样使用」，已有的不改动。已初始化的项目用 `--mode entry --entry-name <名称>` 补上入口（加 `--readme-entry` 在 README 末尾追加说明）。已有 v5 项目使用 `--mode migrate --recovery-dir <项目外目录>`。新项目默认 `work.mode=case-workspace`：持久工作、资料与交付物归入 Case，见 [Case 工作目录](plugins/project-orchestrator/references/case-workspaces.md)。

## 检查与更新

```bash
npx @sakanano-moe/agenthelm check            # 只读：插件、项目模板/样例与 Opinion 是否落后
npx @sakanano-moe/agenthelm update           # 预览要执行的更新
npx @sakanano-moe/agenthelm update --apply   # 执行
```

- 云端版本源是 [`latest.json`](latest.json)。`check` 分别标出插件代码更新、只改模板或样例的更新，以及含不兼容变更的版本和需要做的动作。
- 在项目目录里运行时，`check`（或项目里的 `./menu health`）还会对比项目的**脚手架**和最新版：缺失的骨架文件、模板文件与新版不同、Profile 受检字段缺失、内容锁版本未知、Case 目录命名与声明不符。Opinion、Profile 里的自有字段和审计字段（`source_ref`、`plugin_version` 等）不参与比较。`check` 始终只读，修复用 `fix`（默认预览，`--apply` 才写入）。
- 插件更新调用 `claude plugin update`、`codex plugin marketplace upgrade`；Cursor 个人安装从带摘要的发布包更新。
- 项目里由模板生成的文件：未改动的先备份再替换，你改过的逐个选择 `--accept <文件>`（用新模板）或 `--keep <文件>`（保留本地）。
- 会话开始时 agent-pack 每天至多联网检查一次，有更新才提示一行，离线不报错。关闭：设置 `AGENTHELM_NO_UPDATE_CHECK=1`，或在 `~/.config/agenthelm/config.json` 写 `{"update_check": false}`。
- Claude Code 的第三方市场默认不自动更新；要让客户端自动更新，可在 `/plugin` → Marketplaces 里为 `agenthelm` 打开 Enable auto-update。

### 脚手架差异：修复与跳过

```bash
npx @sakanano-moe/agenthelm check                      # 每项差异带一个 ID，例如 skel:docs/refs/README.md
npx @sakanano-moe/agenthelm fix add-skeleton           # 按模板补上缺失的骨架文件（已有文件不动）；--apply 写入
npx @sakanano-moe/agenthelm fix profile                # 只改所选 Profile 字段，预览里有 diff，替换前备份
npx @sakanano-moe/agenthelm fix repair-lock            # 内容锁里的 0.0.0 换成真实版本
npx @sakanano-moe/agenthelm fix case-naming            # 只给改名计划和受影响的引用，不移动目录
npx @sakanano-moe/agenthelm fix --skip <ID> --reason "…" --apply   # 永久跳过；--unskip <ID> 撤销
npx @sakanano-moe/agenthelm check --show-skipped       # 列出被跳过的项和理由
```

- 决定记在 `.agents/moe.sakanano.agent-pack/check-decisions.json`，随项目同步：一次决定，所有机器生效。默认只有“期望内容”实质变化（指纹不同）时才再次提示；`--reprompt` 强制重新评估。
- 只和这台机器有关的发现（例如“本机没装运行时”“项目在同步盘里”，ID 以 `local:` 开头）的决定存在用户状态目录，不进项目。
- `check --fingerprint` 输出脚手架指纹：两台机器的脚手架一致时相同。要知道差在哪里：在另一台机器运行 `check --fingerprint --output json > other.json`，在这台运行 `check --fingerprint --explain other.json`。
- 项目有自己的 Case 目录命名时，在 Profile 声明：`"work": {"case": {"directory_naming": {"style": "date-topic"}}}`（`style` 为 `case-id`（缺省）/ `date-topic` / `custom` + `pattern` 正则），`check` 按声明检查。

## 界面语言

命令行输出、菜单、`--help` 与错误信息有中文和英文两种，**默认英文**。按这个顺序决定：环境变量 `AGENTHELM_LANG`（`zh` / `en`）→ `~/.config/agenthelm/config.json` 的 `{"language": "zh"}` → 系统 locale（中文 locale 用中文）→ 英文。要继续看中文：`export AGENTHELM_LANG=zh`。

- 机器可读输出（`--output json` 的字段与结构、退出码）不随语言变化；解析文字输出的脚本请改用 `--output json`。
- 界面语言不改动项目文件。Case 的小节名与 README 追加的“怎样使用”由项目文件语言决定：Profile 顶层 `language`（`zh` | `en`，缺省 `zh`），`agents-init --language {auto,zh,en}` 在初始化时设置，`auto` 取当时的界面语言。SOP 的小节名与字段名也按它生成（`sop new --language` 可单独指定）。OPINION.md 与锁、记忆索引等其余内容仍为中文，所以两种语言下指纹与决定文件一致。
- 观点提示的语言另可在 `nudge.language` 单独设置，见 [nudge 说明](plugins/agent-pack/references/nudge.md)。

## 多机与同步盘

项目放在 iCloud、Dropbox、OneDrive 等同步盘里、在多台机器上使用时，哪些东西跟着项目走，哪些每台机器各一份：

| 内容 | 位置 | 随项目同步？ |
| --- | --- | --- |
| 项目文件、Profile（`project.json`）、内容锁（`content.lock.json`） | 项目里 `.agents/moe.sakanano.agent-pack/` 等 | 是 |
| 脚手架差异的决定（跳过、保留） | `.agents/moe.sakanano.agent-pack/check-decisions.json` | 是：一次决定，所有机器生效 |
| 本机决定（运行时未装、项目在同步盘） | `$XDG_STATE_HOME` 或 `~/.local/state` 下的 `agenthelm/decisions/` | 否 |
| 用户级运行时 | `~/.local/share/agenthelm/`（`runtime install`） | 否：每台机器各装一次 |
| AI 客户端里的插件 | 各客户端自己的插件目录 | 否 |
| 版本缓存 | `$XDG_CACHE_HOME` 或 `~/.cache` 下的 `agenthelm/latest.json` | 否 |
| `wf` / 找资料的交付物台账 | `~/.cache/agenthelm/assets/<项目绝对路径的摘要>/` | 否：路径不同就是两份台账，各自重建 |

- **`wf` 的结果为什么可能不同**：台账按项目的绝对路径各存一份；查找行为还取决于这台机器装的 project-orchestrator 版本，以及项目里可选的 `asset-synonyms.json`（同义词，随项目同步）。要排查，先用 `check --fingerprint` 确认两台机器的脚手架一致，再用 `fix rebuild-index` 重建本机台账。
- **`which wf` / `which agenthelm` 为空**：运行时没装时 `./menu` 仍通过客户端插件运行；`wf` 是单独的查找命令，运行时不提供它，用 `./menu find 词` 或 `agenthelm project find 词` 代替。`agenthelm runtime install --link-bin ~/.local/bin` 只建 `agenthelm` 的命令链接。
- **同步盘提示**：`health` 检测到项目位于同步盘时给出一条本机提示（不改任何文件）；`fix sync-folder` 列出项目里的可重建目录和处理方案，**不会替你移动或删除任何内容**。要把 `.venv`、`node_modules` 等移出同步盘，用下一节的 `agenthelm local-only`。
- 同步服务可能留下 `check-decisions 2.json` 之类的冲突副本；`check` 会提示，合并到 `check-decisions.json` 后删除副本。
- 日期 Case 目录、macOS 的 iCloud 未下载占位文件：台账不读取这些文件的内容，不触发下载。

### 同步盘里的 `.venv`、`node_modules`：`agenthelm local-only`

依赖和缓存不该进同步盘。`agenthelm local-only` 把它们放到每台机器自己的 `~/.local/share/agenthelm/local-only/<project_id>/` 下，项目里留一个软链接，并检查各设备之间的差异：

```bash
agenthelm local-only check                 # 云同步、外置链接、HOME、体积、包管理器（只读；--output json、--strict）
agenthelm local-only fix                   # 预览；--apply 才移动目录并建链接，失败整体撤销
agenthelm local-only fix --apply --rebuild # 新设备：建本机目录，并按 uv.lock / yarn.lock 等重新安装
agenthelm local-only check --fingerprint   # 本机环境指纹；--explain 对方.json 指出两台设备差在哪里
```

- **各设备的 HOME 绝对路径必须一致**（链接文本随项目同步，所有设备要相同）；不一致时 `check` 报告，`fix` 拒绝写入。
- 实测：uv、普通 venv、yarn 经典版、bun 可以外置；**npm 会把链接换回真实目录，pnpm 会安装失败**——同步目录里的 Node 项目请尽量用 yarn 或 bun（不开云同步时随意）。缓存类用 `PYTHONPYCACHEPREFIX`、`RUFF_CACHE_DIR`、`MYPY_CACHE_DIR` 等环境变量外置。
- 构建产物（`dist`、`build`）和本机工具状态（`.claude`、`.cursor`、`.idea`）默认保持同步。目录体积接近阈值（默认 200 MB 或 2 万个文件的 80%）时给出选项由你决定；阈值与例外在 `.agents/moe.sakanano.agent-pack/local-only.json` 或 `~/.config/agenthelm/config.json` 的 `local_only` 段配置。
- AI Agent 通过只读 MCP Tool `agent_pack_cloud_sync_safety_check` 获取同样的检查结果。
- 详见 [`plugins/agent-pack/references/local-only.md`](plugins/agent-pack/references/local-only.md)。

## 从旧安装方式迁移

早期版本把插件复制到 `~/.agents/plugins/plugins/<id>`，并在 `~/.claude/skills/<id>` 建软链。与市场安装并存会出现重复的 Skill 与 MCP：

```bash
npx @sakanano-moe/agenthelm migrate --install           # 预览
npx @sakanano-moe/agenthelm migrate --install --apply
```

旧文件整体移入 `~/.agent-pack/recovery/<时间>-agenthelm-migrate/`，并写入 `migration.json` 清单。下游工具的不兼容点见 [`CHANGELOG.md`](CHANGELOG.md)。

## 目录结构

```text
/
├── .claude-plugin/marketplace.json # agenthelm 市场清单（scripts/release_meta.py 生成）
├── latest.json                    # 云端版本源（同上）
├── release/migrations.json        # 版本间差异：变更类型、是否不兼容、需要的动作
├── plugins/
│   ├── agents-init/               # 初始化器、模板与 Profile schema
│   ├── project-orchestrator/      # 生命周期编排、默认 Case 工具、条目式记忆与项目入口
│   ├── agent-pack/                # 扩展能力管理、agenthelm 命令行、用户级运行时与 MCP
│   └── opinion-manager/           # Opinion 配置、组合与审查；版本化规则与模板
├── packages/cli/                  # @sakanano-moe/agenthelm（npx 入口；携带四个插件的发布内容，与插件同一份代码）
├── scripts/                       # release_meta.py、attach_release.py、bootstrap_and_init.py
└── tests/
```

## 维护与发布

- 许可证：[MIT](LICENSE)。git tag、站点（`index.json` 的 `plugin.version`）与 npm 包 `@sakanano-moe/agenthelm` 使用同一个发布版本号，由 `release_meta.py` 校验；各插件版本独立，只在自身改动时升级。
- 各插件的 `plugin.json` 是唯一手写版本；`content.json` 声明模板与样例内容包。改动后运行 `python3 scripts/release_meta.py --write` 生成市场清单、`latest.json` 与内容摘要。
- 版本门禁 `python3 scripts/release_meta.py --since <上一个 tag>`：插件或内容包改动未升版本即失败；升版本须在 `release/migrations.json` 写明 `change_kind`（code / content / both）、`breaking` 与 `actions`。
- 分支：功能分支 → `dev`（集成与测试）→ `main`（发布基线）。客户端市场跟随 `main`：合进 `main` 且版本号上升，市场用户就能更新到，所以未发布的改动只进 `dev`。在 `main` 上打 `v*` tag 触发发布：构建发布包、上传摘要、按需发布 `@sakanano-moe/agenthelm`、刷新站点。
- 上线前试用 `dev` 的内容：在本地检出 `dev`，用 `python3 plugins/agent-pack/runtime/agenthelm.py runtime install --apply` 装成运行时，或用 `install --source <检出目录>`、`--marketplace-source <检出目录>` 装进客户端；用隔离的 `HOME` 时不影响日常环境。
- 对外文字英文优先，并为以后的语言预留：
  - 文档每种语言一个文件（例如 npm 包的 `README.md` 与 `README.zh-CN.md`），开头互相链接；
  - 只有一个字段可用的长描述与 Skill 触发描述用 `English … 中文：…` 分段；
  - 由 `tests/test_public_metadata.py` 检查。用户自己的内容（例如 Opinion 规则正文）不做翻译。
- `tests/test_public_privacy.py` 扫描全部已跟踪文件，拦截本机路径、私钥与私有项目标识。公开 Opinion 模板须经内容检查、测试和用户逐文件明确同意；个人规则不得进入公开模板、示例、测试与文档。
- push 前完成单元测试、Plugin 校验、端到端初始化与公开敏感信息审计；版本、结构或入口变化记录到 [`CHANGELOG.md`](CHANGELOG.md)。

## Opinion 首次配置

只提供三个主入口：直接用模板、选择模板的部分条目、从空白开始迭代；另可在已授权范围由 Agent 提出记忆候选。规则来源、个人修改与完整正文集中确认一次，再保存、读回与 verify。详见 [首次使用说明](plugins/opinion-manager/references/onboarding.md) 与 [版本契约](plugins/opinion-manager/references/versioning.md)。

### 自动采集观点与未决事项提示

装好 agent-pack 后，启用了 opinion-manager 的项目（Project Profile 的 `opinion.provider` 为 `opinion-manager`，或存在 `.opinion/`、`opinion.lock.json`）里，三个客户端的 hook 会让 Opinion 自己转起来：

- 会话开始：注入采集协议和你已同意试行的候选；待确认候选 ≥ 5 条或最久 ≥ 7 天未处理时，给你一行提示（每 24 小时至多一次）。
- 工作中：你说“以后都……”“不要再……”“记住……”这类话时，提醒 Agent 静默暂存为候选，不打断任务。
- 任务完成时：Agent 把本次暂存的候选编号列出，一次请你裁决：正式采纳 / 项目内试行 / 暂缓 / 丢弃。
- 采集是自动的，生效不是：候选未经你确认不会进入 `OPINION.md`；`formal` 也只记录意愿，写入仍走完整预览与确认。

Claude Code 与 Codex 随插件生效（Codex 需在 `/hooks` 中信任）；Cursor 运行 `agenthelm nudge install-hooks --client cursor` 预览，`--apply` 写入。提示按你的语言显示（中文 / 英文；`nudge.language`、`AGENTHELM_LANG`、系统 locale，最后用英文），协议也同步，要求 Agent 用你正在使用的语言转告和确认。阈值在 Project Profile 的 `opinion.nudge` 或 `~/.config/agenthelm/config.json` 的 `nudge` 调整，`agenthelm nudge status` 查看生效配置，`AGENTHELM_NO_NUDGE=1` 关闭。候选保存在项目内的 `.opinion/inbox.json`，**其中有你的原话，可能含敏感信息**：启用这个功能的项目建议保持私有，至少不要放进公开渠道。若要公开仓库或让他人看到，是否排除 `.opinion/` 由你决定；工具会在第一次暂存和第一次开会话时各提醒一次，`agenthelm nudge status` 随时可查，但不会替你决定，也不会自动改 `.gitignore`。详见 [nudge 说明](plugins/agent-pack/references/nudge.md)（[English](plugins/agent-pack/references/nudge.en.md)） 与 [候选收件箱](plugins/opinion-manager/references/inbox.md)（[English](plugins/opinion-manager/references/inbox.en.md)）。
