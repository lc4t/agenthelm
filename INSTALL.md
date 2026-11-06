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

给了 `--client` 时，旧方式安装仍会列出（它是待迁移的风险），但只有它的路径属于所选客户端时才计入“是否最新”（`up_to_date`）与 `--strict` 退出码：`~/.claude/…` 归 Claude Code，`~/.agents/…`（Codex 个人市场）归 Codex；路径无法判断归属时仍列出、不计入。不带 `--client` 时所有旧方式安装都计入。JSON 里每个旧方式条目带 `owner`，汇总里 `legacy_installs` 是计入的个数，`legacy_other_clients` 是列出但未计入的个数。

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
- 试用候选版（版本号带 `-rc.N`，npm 标签 `next`）：用 `npx @sakanano-moe/agenthelm@<版本> …` 运行（不带版本的 `npx` 解析到 npm `latest`，即稳定版）；候选版运行时给出的提示命令会自带 `@<版本>`。`runtime install` 装候选版时**默认不切换 `current`**（项目启动器、hook、终端里的 `agenthelm` 继续用原来的运行时），要启用加 `--use`，或之后运行 `agenthelm runtime use <版本> --apply`；第一次安装（没有 `current`）仍会设为当前。稳定版的行为不变（装完即切换）。
- Codex 候选版验收：把安装预览中的市场添加命令替换为 `codex plugin marketplace add lc4t/agenthelm --ref <候选版 tag>`；不带 `--ref` 会跟随 `main`，不代表安装了候选版。完整步骤、来源未知时的处理与实机待验项目见 [候选版验收](release/ACCEPTANCE.md)。
- Agent 自己需要查资料或看进展时，调用 `./<启动器> find <词> --output json`、`progress --output json` 等子命令，不另写查找逻辑；写入类子命令默认只预览。

## 4. 检查与更新

- `npx @sakanano-moe/agenthelm check`：只读报告插件、项目模板/样例与 Opinion 是否落后，并区分代码更新、仅模板/样例更新与不兼容变更。
- `npx @sakanano-moe/agenthelm update`：预览更新；经用户同意后追加 `--apply`。本地改过的模板生成文件须用户逐个选择 `--keep`、`--append` 或 `--accept`（后者丢弃本地内容，须再加 `--confirm-discard <文件>`；`--diff <文件>` 先看差异）。
- 会话开始的静默检查由 agent-pack 完成，不需要 Agent 额外调用。
- Claude Code 升级插件后，`~/.claude/plugins/cache/agenthelm/<插件>/` 下会留着旧版本目录（例如 agent-pack 的 1.6.3、1.8.0）。这是客户端的缓存行为：`~/.claude/plugins/installed_plugins.json` 指向的是新版本，旧目录不影响使用。想省磁盘可以手动删除不再被 `installed_plugins.json` 引用的版本目录；agenthelm 不会替你清理，也不读取这些旧目录。
- 命令行输出默认英文；要中文：`export AGENTHELM_LANG=zh`，或 `agenthelm config language zh --apply`（等同在 `~/.config/agenthelm/config.json` 写 `{"language": "zh"}`；中文系统 locale 本来就是中文）。界面语言只影响输出；写进项目文件的语言是 Profile 的 `language`，用 `agenthelm config project-language zh|en` 查看或设置。界面显示为英文且没有设置界面语言时 `check` 会提示（中文界面不提示），`agenthelm config language-hint off` 关闭提示。
- 观点候选的自动采集与未决事项提示也由 agent-pack 的 hook 完成：Claude Code 随插件生效；Codex 需在 `/hooks` 中审阅并信任；Cursor 运行 `npx @sakanano-moe/agenthelm nudge install-hooks --client cursor` 预览，确认后加 `--apply`。`nudge status` 查看生效配置，`AGENTHELM_NO_NUDGE=1` 关闭，详见 `plugins/agent-pack/references/nudge.md`。

### Cursor 从发布包更新

本机无需保留 agenthelm 仓库：运行 `agenthelm update --client cursor` 预览，确认后追加 `--apply`。版本来源的 `release` 块提供 tag、tarball URL 与 SHA-256；若版本来源没有该块，会从 GitHub 的最新正式 Release 读取 `release.json`，只接受插件版本组合与当前版本来源一致的发布。

预览会下载并校验发布包，但不改客户端安装；应用前先核对包内所有插件的版本，再替换 `~/.cursor/plugins/local/<插件>` 并读回版本。摘要或版本不匹配时不会替换任何插件。已有目录备份在 `~/.agent-pack/recovery/<时间>-cursor-<插件>/`；替换后重启 Cursor，文件读回成功只说明安装完成，不代表当前会话已加载。

没有匹配发布包时，`check` 把 Cursor 单列为“不可用”，不计入“可更新”；可以指定本地源，例如 `agenthelm update --client cursor --source '/path/to/agenthelm checkout'`（先预览，再加 `--apply`）。部分安装时，`check` 的 `missing` 与 `next_command` 会列出缺失插件和补装命令 `agenthelm install --client cursor`。

### 从任意状态升级

`agenthelm check` 开头的“安装状态”按客户端给出结论、下一条命令（预览形式）、是否改动已有安装与原件恢复位置；`--output json` 的 `install_state.clients.<客户端>` 提供同样的稳定字段（`state`、`base_state`、`sub_status`、`installed`、`missing`、`installs`、`conflicts`、`market_conflict`、`untouched`、`summary`、`next_command`、`modifies_existing`、`modifies_note`、`recovery`，另有 `market_pin`：settings 把市场固定在某个 ref 时的 `{scope, file, ref, target, at_target}`，否则 `null`）。

`check --output json` 的 `summary.scaffold_info` 只统计 info 级脚手架发现；文本里“脚手架·信息”行还包含 Profile 形态冲突与 schema 未声明字段，这两项合计为 `summary.profile_notes`（整数，不改变 `scaffold_info` 的含义）。文本输出中，同一插件同一版本的不兼容说明只在第一个出现的客户端下完整打印，其余客户端写“同上”；JSON 的逐客户端明细不变。

| 状态（`state`） | 含义 | 下一条命令（先预览） | 会改动已有安装？ | 原件恢复位置 |
|---|---|---|---|---|
| `none` | 客户端存在，没有任何 agenthelm 插件 | `agenthelm install --client <客户端>` | 否 | 无 |
| `partial` | 只装了一部分（`agents-init` 依赖其余插件） | `agenthelm install --client <客户端>`，只补缺的 | 否（Cursor 会整体重写，见右） | Cursor：`~/.agent-pack/recovery/<时间>-cursor-<插件>/` |
| `marketplace` · `behind` | 市场安装，落后于云端 | `agenthelm update --client <客户端>` | 是，经客户端升级 | Claude Code 在 `~/.claude/plugins/cache/agenthelm/<插件>/` 保留旧版本目录；Codex 由客户端管理，不备份 |
| `marketplace` · `current` / `ahead` | 已是最新 / 高于云端 | 无需操作 | — | — |
| `dev` | 来自其他市场名、本地目录市场、指向别处的软链：显示路径与版本 | `agenthelm update --client <客户端>`（预览里标“不会改动”）；缺的插件用 `install` 补装 | 开发版/本地安装**不会**被 `update` / `migrate` 改动 | 无（不动它们）；想纳入市场管理，自行卸载这处安装后运行 `install` |
| `legacy` | 只有旧方式安装（复制 + 软链） | `agenthelm migrate --install` | 是，旧安装移走 | `~/.agent-pack/recovery/<时间>-agenthelm-migrate/`（含 `migration.json`） |
| `mixed` | 同一插件有多处不同来源的安装（市场 + 开发版 / 市场 + 旧方式） | 含旧方式：`agenthelm migrate`；否则 `agenthelm doctor` 查看后自行保留一处 | `migrate` 会移走旧安装；`doctor` 只读 | 同 `legacy` |
| `market-conflict` | `settings.json` 的 `extraKnownMarketplaces.agenthelm` 指向别处，或固定在**旧于目标版本**的 ref（`update` 只会跟随该 ref，版本可能不变；固定的 ref 等于目标 tag 时不再算冲突，`market_conflict` 为 `null`，`market_pin.at_target` 为 `true`） | `agenthelm install --client claude --marketplace-source "<与声明一致的来源，含 #ref>"` | 否 | 无；agenthelm 从不修改 `settings.json`，要么自己改那处声明，要么让 `--marketplace-source` 与它一致 |

`update` 预览与 `--apply` 在升级前还会核对 Claude Code 自己登记的市场（`~/.claude/plugins/known_marketplaces.json`）与 `settings.json` 的声明：

| 现象 | 含义 | 处理 |
|---|---|---|
| `update` 把 Claude 的步骤标 **[阻塞]**，原因为「已登记的是 … / 还没有登记 / 无法解析」 | 你改了 `extraKnownMarketplaces.agenthelm`（例如把 ref 换成新 tag）但没重启 Claude Code，它还没按新声明登记市场；直接升级会在第一步报 `Marketplace 'agenthelm' not found` | 重启 Claude Code；或 `claude plugin marketplace add <owner/repo>#<ref>`（来源取自 settings.json 声明）。带 `--apply` 时 Claude 的步骤不执行，命令退出码 1；其他客户端照常执行，汇总里分开列出。JSON：`market_sync`（无问题为 `null`）与步骤里的 `blocker`、`next` |
| settings 固定在旧 tag | 更新只会跟随该 ref | 把声明里的 ref 改成目标 tag（agenthelm 不改 `settings.json`），再重启 Claude Code |
| Codex 步骤「完成，无变化」，原因为市场没固定到此版本 | Codex 市场在跟随默认分支 | `codex plugin marketplace add <owner/repo> --ref <tag>`（`--ref` 与 `owner/repo@ref` 见 [Codex 插件文档](https://developers.openai.com/codex/plugins/build)；若提示市场已存在，先 `codex plugin marketplace remove agenthelm`，移除对已装插件的影响**未确认**）。步骤的 JSON 里有 `next` |

要点：识别不等于处理，开发版与本地安装只告知；同一插件有多处安装时，哪一处生效由客户端决定，目前**未确认**，建议只保留一处。指定版本来源测试升级路径：`check --latest <latest.json 的 URL 或路径>`、`update --latest …`；固定到某个 tag 安装：`install --client claude --marketplace-source "owner/repo#<tag>"`（若 `settings.json` 已声明该市场，来源须与声明一致，Claude Code 否则拒绝）。

## 无客户端命令时的兜底

无法使用客户端市场时，可下载完整仓库，在仓库根目录运行 `python3 scripts/bootstrap_and_init.py --client <codex|cursor|claude> --project <绝对路径> ...`，默认只预演，确认后追加 `--apply`。这种方式不经市场，之后需用 `npx @sakanano-moe/agenthelm migrate --install` 改为市场安装才能自动获得更新。

## 安全语义

- 安装、更新、迁移与初始化默认预览；没有 `--apply` 时不写入用户目录和项目。
- 初始化碰撞在写入前阻断；禁止自动覆盖项目文件。更新替换前保留恢复副本。
- 升级须通过客户端的安装/启用机制或 `agenthelm`，禁止手改受客户端管理的缓存。
