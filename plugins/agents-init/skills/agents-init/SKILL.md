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

新项目默认在根目录生成启动器 `menu`（人不经过 Agent 打开项目菜单），并在 Profile 写入 `entry` 段；用户想用别的名字时传 `--entry-name <名称>`，不要启动器时传 `--no-entry`。README.md 不存在时新建并写入「怎样使用」，已存在的不改动。

为已初始化的项目补上入口只用 `--mode entry`，它只涉及 Profile 的 `entry` 段、启动器与内容锁；根目录已有同名文件时视为用户的文件，必须由用户选择 `--keep` 或 `--accept`（替换前备份到 `--recovery-dir`）：

```bash
python3 scripts/init_project.py --project /path/to/project --mode entry --entry-name menu
python3 scripts/init_project.py --project /path/to/project --mode entry --entry-name menu --readme-entry --apply
```

迁移既有项目时，保留所有碰撞文件，只显式替换已审阅的路径。替换或退役旧 Profile 必须指定项目外恢复目录：

```bash
python3 scripts/init_project.py --mode migrate --project /path/to/project \
  --name example --project-type code --vcs github \
  --stack python --runtime local --agent-cli codex \
  --replace AGENTS.md --recovery-dir /safe/recovery/example-v6 --apply
```

目录里已有自己的文件时（没有 Project Profile）先扫描，再接管。原则是控制新增、不追溯历史：与骨架冲突的旧文件可以长期并存，不是阻塞项；只有用户显式选择，才改动已有文件，且先备份到 `--recovery-dir`（项目外）：

```bash
python3 scripts/init_project.py --project /path/to/project --mode scan            # 只读：冲突、选项与推荐
python3 scripts/init_project.py --project /path/to/project --mode adopt \
  --name example --project-type code --vcs github --stack python --runtime local --agent-cli codex   # 预览
python3 scripts/init_project.py ... --mode adopt --agents-policy append --claude-policy add-import \
  --recovery-dir /safe/recovery/example --apply                                    # 改动已有文件须显式选择
```

- `AGENTS.md` 已有用户内容：`keep`（缺省，不动）、`append`（推荐：原文不动，追加带标记的项目契约区块）、`split`（`--route <章节序号>=<agents|rules|user|opinion|sop>`，未指定的章节留下）、`side-by-side`（原文整体移到 `AGENT.USER.md`）。章节去向建议只来自关键词启发式，逐段判断放在 session 里与用户确认，再把结果交给 `--route`；写入始终经由本命令。
- `CLAUDE.md` 已有内容但没有导入 `AGENTS.md`：Claude Code 两者并存时只读 `CLAUDE.md`，项目契约不会加载，推荐 `--claude-policy add-import`。
- `cases/`、`memory/` 被别的内容占用：并存，或 `--case-root` / `--memory-root` 另设位置；已有工作容器用 `--register-root` 只读登记。
- 追加进用户文件的是受管区块（`scripts/regions.py`）：区块之外不动，区块内部被改过时升级不覆盖，用 `--mode region` 逐个 `--keep <文件>#<ID>` / `--accept <文件>#<ID>`。

已初始化的项目对照最新脚手架用 `--mode check`（只读）；其余模式默认只预览，`--apply` 才写入，已有文件不被覆盖，替换前备份到 `--recovery-dir`：

```bash
python3 scripts/init_project.py --project /path/to/project --mode check
python3 scripts/init_project.py --project /path/to/project --mode add-skeleton [--target docs/refs/README.md]
python3 scripts/init_project.py --project /path/to/project --mode profile [--target runtime.commit_policy] --recovery-dir /safe/recovery/example
python3 scripts/init_project.py --project /path/to/project --mode repair-lock
python3 scripts/init_project.py --project /path/to/project --mode decide --target skel:docs/drafts/.gitkeep --decision skip --reason "…" --apply
python3 scripts/init_project.py --project /path/to/project --mode case-naming   # 只给改名计划，不移动目录
```

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
docs/
├── refs/README.md
└── drafts/.gitkeep
```

`AGENTS.md` 是项目执行入口；`CLAUDE.md` 与 `AGENT.md` 是短路由文件；初始化产生的 `OPINION.md` 保持为空白配置入口。用户确认后由 `opinion-manager` 写入当前项目；用户内容禁止复制到公开模板、示例、测试或文档。

新项目默认 `work.mode=case-workspace`。`--work-mode legacy` 可保留旧分类；既有项目迁移时未显式选择模式就保持现有模式。Case 数据迁移由 `project-orchestrator` 的已审阅映射工具执行，初始化器只更新骨架与 Profile。

## 迁移规则

- 只根据明确版本标记识别旧版本；禁止把陌生结构推断为 v4 或 v5。
- 使用 `--mode migrate`；未选择的碰撞文件保持原样。写入新骨架文件前，每个 `--replace` 路径都必须备份到项目外。
- 检测到 v5 Profile（`.agents/moe.sakanano.project-runtime/project.json`）时，初始化模式以 `migrate-required` 停止；迁移模式保留其项目自定义字段并升级到 `.agents/moe.sakanano.agent-pack/project.json`，旧文件移入 `--recovery-dir`。
- 保留项目事实与本地约束。入口文件中的 `project-runtime` 引用由用户逐项选择 `--replace` 或手动精简。
- 只有在 `project-orchestrator`、`agent-pack` 与 `opinion-manager` 可用后，才从 Agent 入口文件移除重复的职责说明。
- 只有得到明确授权，才能把个人/全局规则移入用户的私有权威源；禁止提交到本公开模板。
- 禁止删除旧 Skill、MCP 条目、Task 数据或进度记录。初始化后由 `agent-pack` 盘点并迁移能力；旧 `project-runtime` Plugin 用 `agent-pack reconcile --retire-plugin project-runtime` 归档。
- 本 Skill 禁止执行项目工作、创建 Task/Case、演化 Opinion 或 commit/push。

## 完成报告

报告分发包与同包 `project-orchestrator`、`agent-pack`、`opinion-manager` 的验证结果、已生成或升级的文件、推断出的 Profile 值及证据、保留的碰撞项和验证结果，并记录用户选择的 Opinion 配置方式。
