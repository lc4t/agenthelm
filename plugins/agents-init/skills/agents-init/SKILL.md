---
name: agents-init
description: Initialize a repository, or migrate an existing one, to the agents-init v6 project skeleton. Use when the user asks to set up Agent collaboration, create AGENTS.md, add a Project Profile, add a root launcher for the project menu, or migrate an older agents-init layout (including the v5 project-runtime Profile). This Skill only handles structure; project-orchestrator owns the project lifecycle, agent-pack owns Plugins/Skills/MCP, and opinion-manager owns user-confirmed Agent behavior rules. 中文：初始化仓库或将既有仓库迁移到 agents-init v6 项目骨架。用户要求初始化 Agent 协作、创建 AGENTS.md、增加 Project Profile（项目配置）或迁移旧版 agents-init 结构（含 v5 的 project-runtime Profile）时使用。本 Skill 只负责结构；project-orchestrator 负责项目生命周期，agent-pack 负责 Plugin/Skill/MCP，opinion-manager 负责用户确认的 Agent 行为规则。
---

# Agents Init（项目初始化）

## 定位

创建一份精简、可移植、可被兼容 Agent 自动发现的项目契约。本 Skill **只负责初始化与有版本的骨架迁移**。

严格保持以下职责边界：

- `agents-init`：创建项目骨架与 Project Profile；
- `project-orchestrator`：管理持续工作、Task/Case 状态、路由、验证、进度与 Git 交付；
- `agent-pack`：盘点、同步、迁移与对账 Plugin、Skill、MCP；
- `opinion-manager`：通过用户说明、已批准模板或逐条引导生成并维护 Opinion；
- 领域 Skill：创建或检查具体交付物。

禁止把用户的个人规则、凭据、私有路径、MCP 密钥或私有 Skill 内容写入这个公开模板。公开 Opinion 模板目录可以为空；新增模板需要用户审阅完整内容并明确同意。

## 必读材料

初始化或迁移项目前，完整读取 [AGENT.template.md](AGENT.template.md)。它是 v6.6 骨架契约、中文生成模板与迁移指南，也是初始化器生成 Markdown 的唯一权威源。

本 Skill 需要同一市场的 `project-orchestrator`、`agent-pack` 与 `opinion-manager` 一并安装。初始化器在 agents-init 旁边查找它们：仓库与 Cursor 本地插件为同级目录，Claude Code 与 Codex 为插件缓存中的同市场目录。找不到时停止初始化并报告 `runtime-required`，引导用户从 agenthelm 市场安装缺少的 Plugin；禁止生成一个无法执行的项目契约。

## 工作流程

1. 检查仓库根目录、现有 Agent 入口文件、Git 状态、项目清单以及可能的构建/测试命令。
2. 只推断有直接证据的字段；标记不确定值。只有缺失选择会实质改变生成契约时，才集中询问一次。
3. 展示拟创建的文件计划与碰撞项。
4. 初始化器先验证同包 `project-orchestrator`、`agent-pack` 与 `opinion-manager`，用户确认后再使用 `--apply`；脚本无法运行时，严格按中文模板创建同一组文件，并再次确认三者可用。
5. 初始化模式下，任一碰撞都会阻止全部写入。保留所有既有文件；迁移模式只修改用户审阅差异后明确选择的文件。
6. 解析生成的 JSON，检查入口文件路由，并报告剩余占位符。
7. 初始化完成后调用 `opinion-manager`，只展示直接用模板、选择模板的部分条目、从空白开始迭代；另可选择基于已授权记忆提出候选规则。选择后再展开完整内容，集中确认一次；从空白开始时保持占位文件，不建立空 Profile。目录为空时说明模板不可用，允许个人说明导入或跳过。

预演示例：

```bash
python3 scripts/init_project.py --project /path/to/project \
  --name example --project-type code --vcs github \
  --stack python --runtime local --agent-cli codex
```

审阅计划后再应用：

```bash
python3 scripts/init_project.py --project /path/to/project \
  --name example --project-type code --vcs github \
  --stack python --runtime local --agent-cli codex --apply
```

**项目文件语言**：初始化前先问用户项目里的文件用中文还是英文（用户没有特别要求时取当前对话的语言），用 `--language zh|en` 传入；不传时取当前界面语言（`AGENTHELM_LANG`、系统 locale，最后英文）。它写进 Profile 的 `language`，决定新建 Case 的小节名与默认文字、README 追加的使用说明用哪种语言，随项目同步，与各人的命令行界面语言无关；缺省的旧项目保持中文。`--mode migrate` 缺省保持中文，要改传 `--language en`。

已有项目没有 `language` 时按中文生成。要改，用 `--mode language --language zh|en`（或 `agenthelm config project-language zh|en`）：默认只预览，`--apply` 只把 Profile 的 `language` 写成所选值（Profile 先备份），不改写任何已有文件；`check` 的 `project_language` 字段给出项目是否声明了它。

新项目默认在根目录生成启动器 `menu`（人不经过 Agent 打开项目菜单），并在 Profile 写入 `entry` 段；用户想用别的名字时传 `--entry-name <名称>`，不要启动器时传 `--no-entry`。README.md 不存在时新建并写入使用说明小节（中文项目叫「怎样使用」，英文项目叫 How to use），已存在的不改动。

为已初始化的项目补上入口只用 `--mode entry`，它只涉及 Profile 的 `entry` 段、启动器与内容锁；根目录已有同名文件时视为用户的文件，必须由用户选择 `--keep` 或 `--accept`（替换前自动备份，见下文“备份与还原”）：

```bash
python3 scripts/init_project.py --project /path/to/project --mode entry --entry-name menu
python3 scripts/init_project.py --project /path/to/project --mode entry --entry-name menu --readme-entry --apply
```

迁移既有项目时，保留所有碰撞文件，只显式替换已审阅的路径。替换或退役旧 Profile 前，原件会先备份（缺省 `~/.agent-pack/recovery/`，也可用 `--recovery-dir` 指定项目外的目录）：

```bash
python3 scripts/init_project.py --mode migrate --project /path/to/project \
  --name example --project-type code --vcs github \
  --stack python --runtime local --agent-cli codex \
  --replace AGENTS.md --recovery-dir /safe/recovery/example-v6 --apply
```

目录里已有自己的文件时（没有 Project Profile）先扫描，再接管。原则是控制新增、不追溯历史：与骨架冲突的旧文件可以长期并存，不是阻塞项；只有用户显式选择，才改动已有文件，且先自动备份到项目外：

```bash
python3 scripts/init_project.py --project /path/to/project --mode scan            # 只读：冲突、选项与推荐
python3 scripts/init_project.py --project /path/to/project --mode adopt \
  --name example --project-type code --vcs github --stack python --runtime local --agent-cli codex   # 预览
python3 scripts/init_project.py ... --mode adopt --agents-policy append --claude-policy add-import \
  --apply                                                                          # 改动已有文件须显式选择；原件自动备份
```

- `AGENTS.md` 已有用户内容：`keep`（缺省，不动）、`append`（推荐：原文不动，追加带标记的项目契约区块）、`split`（`--route <章节序号>=<agents|rules|user|opinion|sop>`，未指定的章节留下）、`side-by-side`（原文整体移到 `AGENT.USER.md`）。章节去向建议只来自关键词启发式，逐段判断放在 session 里与用户确认，再把结果交给 `--route`；写入始终经由本命令。
- `CLAUDE.md` 已有内容但没有导入 `AGENTS.md`：Claude Code 两者并存时只读 `CLAUDE.md`，项目契约不会加载，推荐 `--claude-policy add-import`。
- `cases/`、`memory/` 被别的内容占用：并存，或 `--case-root` / `--memory-root` 另设位置；已有工作容器用 `--register-root` 只读登记。
- 追加进用户文件的是受管区块（`scripts/regions.py`）：区块之外不动，区块内部被改过时升级不覆盖，用 `--mode region` 逐个 `--keep <文件>#<ID>` / `--accept <文件>#<ID>`。

已初始化的项目对照最新脚手架用 `--mode check`（只读）；其余模式默认只预览，`--apply` 才写入，已有文件不被覆盖，替换前自动备份：

```bash
python3 scripts/init_project.py --project /path/to/project --mode check
python3 scripts/init_project.py --project /path/to/project --mode add-skeleton [--target docs/refs/README.md]
python3 scripts/init_project.py --project /path/to/project --mode profile [--target runtime.commit_policy]
python3 scripts/init_project.py --project /path/to/project --mode repair-lock
python3 scripts/init_project.py --project /path/to/project --mode decide --target skel:docs/drafts/.gitkeep --decision skip --reason "…" --apply
python3 scripts/init_project.py --project /path/to/project --mode case-naming   # 只给改名计划，不移动目录
```

**内容刷新（`--mode refresh`）遇到你改过的文件**：模板更新后，未改动的文件先备份再替换；你改过的文件必须逐个选择，不会被静默覆盖。没有内容锁基线的项目（早期版本初始化）里，所有与新模板不同的文件都算“你改过”，预览会写明这个原因和每个文件的差异摘要（`local_lines`、`template_lines`、`lines_added`、`lines_lost`）。选择：`--keep <文件>` 保留（建议；会被记住，之后不再追问）；`--append <文件>`（只限 `AGENTS.md`、`CLAUDE.md`）保留原文、只在末尾追加带标记的受管区块；`--accept <文件>` 换成新模板，会丢弃 `lines_lost` 行本地内容，所以须再加 `--confirm-discard <文件>` 才会执行；`--diff <文件>` 只看完整差异，不写入。

**备份与还原**：`refresh`、`adopt`、`region`、`profile`、`entry`、`migrate` 这些会替换用户文件的模式，不必先想一个备份目录——`--apply` 时原件自动复制到 `~/.agent-pack/recovery/<UTC 时间>-agenthelm-<模式>/`（与 `agenthelm update`、`migrate` 的位置和命名一致），一次操作的所有被替换文件放在同一个目录。命令输出（文本和 `--output json` 的 `recovery`、`recovery_files`、`recovery_default` 字段）会说明备份位置；还原就是把备份里的文件按相同的相对路径复制回项目。显式传入的 `--recovery-dir` 仍然优先，但必须在项目外，项目内的目录会被拒绝。备份目录无法创建或写入时，命令在改动任何项目文件之前就失败并说明原因，不会跳过备份。预览（不带 `--apply`）不创建任何目录。

期望骨架清单是 [skeleton.json](skeleton.json)（内容包 `agents-init/project-skeleton`）；Opinion、Profile 里的项目自有字段与审计字段不参与比较。用户的跳过 / 保留决定写入 `.agents/moe.sakanano.agent-pack/check-decisions.json`，随项目同步。日常入口是 `agenthelm check` 与 `agenthelm fix`。

## 输出契约

v6 基线结构如下：

```text
README.md        # 不存在时新建
menu             # 根目录启动器（--entry-name 可改，--no-entry 不生成）
AGENTS.md
AGENT.RULES.md
OPINION.md
CLAUDE.md
AGENT.md
.agents/
├── plugin.json
├── mcp.json
├── skills/
└── moe.sakanano.agent-pack/project.json
.agent-doc/
├── plan.md
├── progress.md
└── chat-summary.md
cases/
└── .gitkeep
memory/
└── .gitkeep
sops/
└── .gitkeep
private/
└── .gitignore
docs/
├── refs/README.md
└── drafts/.gitkeep
```

`AGENTS.md` 是项目执行入口；`CLAUDE.md` 与 `AGENT.md` 是短路由文件；初始化产生的 `OPINION.md` 保持为空白配置入口。用户确认后由 `opinion-manager` 写入当前项目；用户内容禁止复制到公开模板、示例、测试或文档。

新项目的 Profile 声明 `privacy.forbidden_default_reads: ["private/"]` 并建立 `private/`（里面的 `.gitignore` 让内容默认不进入 git）：私有信息放在这里，Agent 默认不读，读之前须经用户确认；用到它的 Case 标为 `sensitivity: private`。已有项目没有声明时 `check` 不要求 `private/`，不会多出差异。

新项目的 Profile 带 `sop: {"root": "sops"}`，并建立 `sops/`：用户自己的流程（SOP）放在这里，由 `project-orchestrator` 的 `runtime/sop.py` 管理。已有项目没有 `sop` 段时 `check` 不要求 `sops/`，想启用就在 Profile 加这一段并运行 `fix add-skeleton`。

新项目默认 `work.mode=case-workspace`。`--work-mode legacy` 可保留旧分类；既有项目迁移时未显式选择模式就保持现有模式。Case 数据迁移由 `project-orchestrator` 的已审阅映射工具执行，初始化器只更新骨架与 Profile。

## 迁移规则

- 只根据明确版本标记识别旧版本；禁止把陌生结构推断为 v4 或 v5。
- 使用 `--mode migrate`；未选择的碰撞文件保持原样。写入新骨架文件前，每个 `--replace` 路径都会先备份到项目外。
- 检测到 v5 Profile（`.agents/moe.sakanano.project-runtime/project.json`）时，初始化模式以 `migrate-required` 停止；迁移模式保留其项目自定义字段并升级到 `.agents/moe.sakanano.agent-pack/project.json`，旧文件移入备份目录。
- 保留项目事实与本地约束。入口文件中的 `project-runtime` 引用由用户逐项选择 `--replace` 或手动精简。
- 只有在 `project-orchestrator`、`agent-pack` 与 `opinion-manager` 可用后，才从 Agent 入口文件移除重复的职责说明。
- 只有得到明确授权，才能把个人/全局规则移入用户的私有权威源；禁止提交到本公开模板。
- 禁止删除旧 Skill、MCP 条目、Task 数据或进度记录。初始化后由 `agent-pack` 盘点并迁移能力；旧 `project-runtime` Plugin 用 `agent-pack reconcile --retire-plugin project-runtime` 归档。
- 本 Skill 禁止执行项目工作、创建 Task/Case、演化 Opinion 或 commit/push。

## 完成报告

报告分发包与同包 `project-orchestrator`、`agent-pack`、`opinion-manager` 的验证结果、已生成或升级的文件、推断出的 Profile 值及证据、保留的碰撞项和验证结果，并记录用户选择的 Opinion 配置方式。
