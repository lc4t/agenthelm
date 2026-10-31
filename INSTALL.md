# Agent 安装与初始化说明

用户要求“用 agenthelm 初始化当前项目”、“安装 agenthelm”，或给出 `https://agenthelm.sakanano.moe/` 并要求初始化时，按本文件执行。

## 目标

经 agenthelm 市场安装四个插件，再用 `agents-init` 创建项目契约：

- `agents-init`：创建或迁移项目骨架与 Project Profile；
- `project-orchestrator`：初始化后管理 Case、路由、验证、进度、记忆与 Git 交付；
- `agent-pack`：管理项目与客户端之间的 Plugin / Skill / MCP，并提供 `agenthelm` 检查、更新与用户级运行时；
- `opinion-manager`：生成和维护用户确认的 Agent 行为规则。

## 前提

- 本机有 Python 3.11 及以上；使用 `npx @sakanano-moe/agenthelm` 时另需 Node 18 及以上。
- 安装会写入用户级客户端目录，初始化会写入项目；两者都需用户确认。
- 禁止读取或上传项目 `.env`、个人 Opinion 规则、凭据及私有目录内容。

## 1. 安装插件

先预览，向用户展示将执行的命令，取得同意后再追加 `--apply`：

```bash
npx @sakanano-moe/agenthelm install --client <claude|codex|cursor>
npx @sakanano-moe/agenthelm install --client <claude|codex|cursor> --apply
```

没有 Node 时，直接执行当前客户端的市场命令（同样先取得同意）：

- Claude Code：`claude plugin install agents-init --marketplace lc4t/agenthelm`（会一并安装另外三个依赖插件）。
- Codex：`codex plugin marketplace add lc4t/agenthelm`，再对四个插件分别执行 `codex plugin add <插件>@agenthelm`。

若 `npx @sakanano-moe/agenthelm check` 报告旧方式安装（`~/.agents/plugins/plugins/<id>` 或 `~/.claude/skills/<id>` 软链），先预览 `npx @sakanano-moe/agenthelm migrate --install`，经同意后追加 `--apply`。

安装完成后需要新会话（Claude Code 可用 `/reload-plugins`，Codex 与 Cursor 需重启）才能加载插件。文件安装完成不能证明当前会话已加载插件。

## 2. 初始化项目

在新会话中使用 `agents-init` Skill：

1. 确认目标项目目录已存在；全新项目先 `mkdir -p /absolute/path/to/project`。
2. 从项目现有文件确认名称、项目类型、VCS、技术栈、runtime 与 Agent 客户端。项目名不能派生 ASCII slug 时显式传 `--slug`。无法确认且会改变生成契约时，一次性询问用户。
3. 运行已安装 agents-init 的 `skills/agents-init/scripts/init_project.py`，默认只预演；向用户展示拟创建文件与碰撞项，得到确认后追加 `--apply`。
4. 运行 `agent-pack` 的 `doctor --project <项目>` 确认 `ok=true`，报告已创建文件与仍需填写的 `TODO`。
   初始化会生成根目录启动器 `menu`（用户选了别的名字时用 `--entry-name`）；向用户说明：不开 Agent 时运行 `./menu` 即可找资料、看进展与检查项目。
5. 完整读取 opinion-manager 的 Skill、[版本契约](plugins/opinion-manager/references/versioning.md) 与 [首次使用说明](plugins/opinion-manager/references/onboarding.md)。只提供三个主选项：直接用模板、选择模板的部分条目、从空白开始迭代；另可选择基于已授权记忆提出候选规则。正式保存前预览 Profile 与完整正文，用户一次确认；个人来源、Profile、`OPINION.md` 与 `opinion.lock.json` 只保存在目标项目，禁止复制到公开位置。

已有 v5 项目（存在 `.agents/moe.sakanano.project-runtime/project.json`）使用 `--mode migrate` 升级 Profile（旧 Profile 自动备份到 `~/.agent-pack/recovery/`，也可用 `--recovery-dir <项目外目录>` 指定），详见 `AGENT.template.md` 的“从 v5 迁移”。

## 3. 不需要 AI Agent 的入口

- 已初始化但没有启动器的项目：`init_project.py --project <项目> --mode entry --entry-name <名称>` 预览，经同意后 `--apply`。根目录已有同名文件时让用户选择 `--keep`（保留）或 `--accept`（替换，先自动备份到 `~/.agent-pack/recovery/`，输出会写出位置）。
- 让项目脱离 AI 客户端也能打开菜单：预览 `npx @sakanano-moe/agenthelm runtime install`，经同意后 `--apply`；运行时写入用户数据目录 `~/.local/share/agenthelm/`。
- 离线安装：解压发布包 `agenthelm-v<版本>.tar.gz`，运行 `python3 <解压目录>/plugins/agent-pack/runtime/agenthelm.py runtime install --apply`。
- Agent 自己需要查资料或看进展时，调用 `./<启动器> find <词> --output json`、`progress --output json` 等子命令，不另写查找逻辑；写入类子命令默认只预览。

## 4. 检查与更新

- `npx @sakanano-moe/agenthelm check`：只读报告插件、项目模板/样例与 Opinion 是否落后，并区分代码更新、仅模板/样例更新与不兼容变更。
- `npx @sakanano-moe/agenthelm update`：预览更新；经用户同意后追加 `--apply`。本地改过的模板生成文件须用户逐个选择 `--accept` 或 `--keep`。
- 会话开始的静默检查由 agent-pack 完成，不需要 Agent 额外调用。
- 命令行输出默认英文；要中文：`export AGENTHELM_LANG=zh`，或在 `~/.config/agenthelm/config.json` 写 `{"language": "zh"}`（中文系统 locale 本来就是中文）。
- 观点候选的自动采集与未决事项提示也由 agent-pack 的 hook 完成：Claude Code 随插件生效；Codex 需在 `/hooks` 中审阅并信任；Cursor 运行 `npx @sakanano-moe/agenthelm nudge install-hooks --client cursor` 预览，确认后加 `--apply`。`nudge status` 查看生效配置，`AGENTHELM_NO_NUDGE=1` 关闭，详见 `plugins/agent-pack/references/nudge.md`。

## 无客户端命令时的兜底

无法使用客户端市场时，可下载完整仓库，在仓库根目录运行 `python3 scripts/bootstrap_and_init.py --client <codex|cursor|claude> --project <绝对路径> ...`，默认只预演，确认后追加 `--apply`。这种方式不经市场，之后需用 `npx @sakanano-moe/agenthelm migrate --install` 改为市场安装才能自动获得更新。

## 安全语义

- 安装、更新、迁移与初始化默认预览；没有 `--apply` 时不写入用户目录和项目。
- 初始化碰撞在写入前阻断；禁止自动覆盖项目文件。更新替换前保留恢复副本。
- 升级须通过客户端的安装/启用机制或 `agenthelm`，禁止手改受客户端管理的缓存。
