# agenthelm

跨 Agent 的项目管理插件集。把你在不同 Agent（Claude Code、Codex、Cursor 等）里的工作方式、项目状态与行为规则固化到项目本地，任一客户端打开同一项目都能接着干。Skill 主体均为中文。

不开 AI Agent 也能用：初始化后的项目根目录有一个启动器（缺省 `./menu`），在终端运行它就能找资料、看进展、读项目指南、检查与维护项目、记录事项。它不调用模型，也不需要登录模型账号，只需要 Python 3.11+。见下文[不需要 AI Agent 的项目入口](#不需要-ai-agent-的项目入口)。

| 插件 | 职责 |
|---|---|
| `agents-init` | 创建或迁移项目骨架与 Project Profile |
| `project-orchestrator` | 管理 Case、能力路由、验证、进度、记忆与受控 Git 交付；附默认 Case 工具与不需要 AI Agent 的项目入口 |
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
./menu                      # 交互菜单：找资料 / 看进展 / 项目指南 / 检查项目与环境 / 修复与更新 / 记录事项 / 项目工具
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
- 插件更新调用 `claude plugin update`、`codex plugin marketplace upgrade`；Cursor 个人安装从带摘要的发布包更新。
- 项目里由模板生成的文件：未改动的先备份再替换，你改过的逐个选择 `--accept <文件>`（用新模板）或 `--keep <文件>`（保留本地）。
- 会话开始时 agent-pack 每天至多联网检查一次，有更新才提示一行，离线不报错。关闭：设置 `AGENTHELM_NO_UPDATE_CHECK=1`，或在 `~/.config/agenthelm/config.json` 写 `{"update_check": false}`。
- Claude Code 的第三方市场默认不自动更新；要让客户端自动更新，可在 `/plugin` → Marketplaces 里为 `agenthelm` 打开 Enable auto-update。

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
