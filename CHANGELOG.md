# 更新记录

## 2026-10-29 — Agents Init 6.8.0：接管已有内容的目录、受管区块、发布声明（属于 6.11.0 发布窗口）

在已有内容的目录里初始化，或者升级时新模板与你自己的内容冲突，现在有统一的处理原则：**控制新增，不追溯历史**。与骨架冲突的旧文件可以长期并存，不是阻塞项；只有你显式选择，工具才改动已有文件，并且先备份到项目外。

### agents-init 6.8.0

- 新增 `--mode scan`（只读）：扫描已有路径与骨架的碰撞，以及目录含义是否相同（语义碰撞）。每项给出选项和推荐，推荐只是建议：
  - `AGENTS.md` 已有用户内容：`keep`（不动）、`append`（原文不动，末尾追加带标记的项目契约区块，推荐）、`split`（按章节决定去向）、`side-by-side`（原文整体移到 `AGENT.USER.md`）；
  - `CLAUDE.md` 已有内容但没有导入 `AGENTS.md`：Claude Code 两者并存时只读 `CLAUDE.md`，项目契约不会加载，推荐追加 `@AGENTS.md` 导入区块；
  - `cases/`、`memory/` 已有别的内容：并存，或把新 Case / 记忆放到另一个目录（`--case-root` / `--memory-root`）；名字像工作容器的目录可用 `--register-root` 登记为资料根（只读登记）；
  - 启动器名称已被你的文件占用、符号链接父目录、其他客户端的指令文件（不触碰）。
- 新增 `--mode adopt`（默认预览，`--apply` 才写入）：已有文件原样保留，只补缺失的骨架；保留下来且与模板不同的文件记入决定文件，`check` 不再追问。改动已有文件（`--agents-policy append|split|side-by-side`、`--claude-policy add-import`）必须带项目外的 `--recovery-dir`；写入失败会还原已改动的文件。`split` 用 `--route <章节序号>=<agents|rules|user|opinion|sop>` 指定去向；`opinion` / `sop` 先放进 `AGENT.USER.md` 并标注待交接。章节去向建议来自关键词启发式，最终由你决定。
- 新增受管区块（`scripts/regions.py`）：工具只拥有带标记的区块，区块之外永远不动；区块内部被你改过则升级不覆盖。`check` 新增 `region:<文件>#<ID>` 发现（`region-outdated` / `region-modified` / `region-missing`），`--mode region` 逐个 `--keep` 或 `--accept`（替换前备份）。
- 内容锁新增 `regions`（受管区块的指纹）与 `render_inputs`（模板渲染时用的输入），后者使“用指定版本重新渲染基线”成为可能；`refresh` / `entry` 写锁时保留这些键。渲染对路径、时间、时区和输入组合都是确定的，已有测试覆盖。
- `CLAUDE.md` 模板改为一行 `@AGENTS.md` 的导入桩（Claude Code 在没有任何 `CLAUDE.md` 时才直接读取 `AGENTS.md`，v2.1.277 起；旧版本与部分会话读不到，导入桩两边都稳）；`AGENT.md` 仍是给其他客户端的文字路由。`AGENTS.md` 模板增加一行：若存在 `AGENT.USER.md`，与本文件一并遵循。项目契约模板内容包 1.2.0。
- `INITIALIZER_VERSION` 为 6.8.0。

### project-orchestrator

- `health` / 菜单遇到 `update-region` 发现时给出处理说明，不再因未知处理项出错（不另升版本，随 1.11.1）。

### 发布声明与 CI 门禁

- `release/migrations.json` 的条目新增声明（`"declaration": 2`）：`actions`、`user_action_required`（安装新版后需要你参与的事项，`session` 标明当前还是新开 session 处理）、`touches`（触碰的路径与字段）、`default_for_existing`（对已有项目的默认处理 `off` / `advisory` / `on`）、`template_changes`（模板文件的变更等级 `cosmetic` / `semantic`）。
- `scripts/release_meta.py --since <tag>` 现在还会失败于：升版本的条目没有声明；`breaking` 却没写 `actions`；语义级模板变更对已有项目生效却没有 `user_action_required`；内容包变了却没列 `template_changes`；`skeleton.json` / `project.schema.json` 变了却没写 `touches`；`CHANGELOG.md` 没提到新版本。旧条目不受影响。

### 不兼容点与注意

- 已初始化项目的 `CLAUDE.md` 模板变了：没改过的，`agenthelm update` 会先备份再替换为导入桩；改过的逐个选择。读取 `CLAUDE.md` 里路由文字的外部脚本需改读 `AGENTS.md`。
- 新增的检查项都是“建议处理”，不改变退出码；`--strict` 会把未解决的 `region:` 发现算作未处理。
- 已知限制：升级时仍是整文件“保留 / 采用”二选一，真正的三方合并（用旧版本包重新渲染基线）尚未实现，见后续计划；受管区块已避免了接管场景下的大多数合并需求。

### 版本

不另升版本：随 6.11.0 一起发布（agents-init 6.8.0、project-orchestrator 1.11.1）；内容包 `agents-init/project-contract` 升到 1.2.0。

## 2026-10-29 — 命令行输出多语言（中文 / 英文），默认英文

此前所有命令的输出与提示只有中文。现在 agenthelm 的命令行输出、菜单、`--help` 与错误信息按语言显示，**默认英文**；中文原样保留。属于 6.11.0 发布窗口，与“观点自动采集与未决事项提示”同批发布。

### 语言怎么决定

顺序：调用方指定（如 `nudge.language`）→ 环境变量 `AGENTHELM_LANG` → 用户配置 `~/.config/agenthelm/config.json` 的 `language` → 系统 locale（`LC_ALL`、`LC_MESSAGES`、`LANG`）→ **英文**。取值 `zh`、`en`；`auto`、空值和 C/POSIX 视为未指定；中文 locale 用中文，其他语言的 locale 暂用英文。要继续看中文：`export AGENTHELM_LANG=zh`，或在用户配置里写 `{"language": "zh"}`。

### 范围

- 翻译：agenthelm（`check` / `install` / `update` / `doctor` / `runtime` / `nudge`）、项目入口与菜单（`project` / `./menu`，含演示项目）、`wf`、`assets.py`、`case.py`、`memory.py`、`case_workspace.py`、`opinion_manager.py`（含 `context` / `lifecycle` / `inbox`）、`init_project.py` 与 `scaffold.py` 的输出、提示、错误与 `--help`；用户级运行时启动器的“需要 Python 3.11”提示按 `AGENTHELM_LANG` / `LANG` 选择语言。
- 项目文件语言（新增）：Profile 顶层 `language`（`zh` | `en`，缺省 `zh`）决定写进项目的 Case 与 README。`en` 的项目里，新建的 CASE.md 使用英文小节名（Current trusted state / Next action / Decision log / Work log / Result）与英文默认文字，README 追加 “How to use”。它独立于界面语言：界面语言只影响命令输出，不会改动已有文件。`agents-init --language {auto,zh,en}`：`auto` 在 init 时取当前界面语言，migrate 时保持中文；agents-init 的 Skill 要求先问用户想用哪种文件语言。解析器同时认中英文小节名，已有文件沿用自己的写法，缺失的小节按项目语言补建。新增 `templates/CASE.en.md`，内容包 `project-orchestrator/case-template` 升到 1.1.0。
- SOP 同样跟项目文件语言走（`sop_structure.py`）：`language: en` 的项目里，`sop new` 从英文样例（`SOP.en.md`，附带模板 `*.en.md`）创建草稿，小节名为 Purpose / Prerequisites / Steps / Completion criteria / Exceptions and fallback / Revision log，步骤子项与前置条件字段也是英文（占位标记 `[TO FILL IN]`）；`sop run` 写进执行 Case 的“SOP checklist”小节、步骤记录与决策记录用英文。解析器两套词表都认，同一文件混用也能解析；追加内容沿用文件自己的写法，执行 Case 沿用 Case 自己的语言。`sop new --language zh|en` 可单独指定某个 SOP 的语言。样例 SOP 与参考文档 [`references/sop.md`](plugins/project-orchestrator/references/sop.md) 的“文件语言”一节给出对照表。
- 不翻译：OPINION.md 与锁的渲染、记忆索引、AGENT.template.md 等其余模板、骨架文件模板（这几项仍为中文，是后续工作，见 issue #24）、用户自己写的内容（候选、规则、原因、CASE.md 正文）、公开 Opinion 模板的规则正文、Skill 正文与 CHANGELOG / README 等文档。两种界面语言下 OPINION.md、锁、脚手架指纹与决定文件完全一致，并有测试守住。
- 机器可读输出（`--output json` 的字段名与结构、退出码）不变；JSON 里给人看的 `message` / `error` 文字随语言变化。解析文字输出的脚本请改用 `--output json`。

### 实现

- 每个插件带一份相同的语言模块（`i18n_agent_pack.py`、`i18n_opinion.py`、`i18n_orchestrator.py`、`i18n_init.py`；模块名不同，同一进程里不会互相覆盖）：源语言是中文，`_("中文原文")` 在中文下原样返回，英文下查 `locale/en.json`，没有译文时退回原文；模块级常量里的文案用 `N_` 标记，使用处再 `_()`。约 1550 条文案，演示项目的数据在读取时整棵翻译。
- 测试：`tests/test_i18n.py` 守住四份语言模块行为一致、源码里每个 `_("…")` 都有英文译文、译文的占位符与原文一致且没有漏翻的中文、`_` 没有被局部变量遮蔽、演示数据完整，并在英文下实际运行演示项目、菜单（伪终端）、各命令的 `--help`、真实项目的 Case / 台账命令，要求输出里没有漏翻的中文。既有测试固定 `AGENTHELM_LANG=zh`。
- 英文文案由人工翻译，未经母语者审校；新增语言只需补一份 `locale/<语言>.json` 并在 `LANGUAGES` 里加上。

### 版本

与 SOP（6.9.0）、local-only（6.10.0）、观点自动采集同在 6.11.0 发布，不另升版本：agents-init 6.8.0、agent-pack 1.11.0、opinion-manager 0.5.0、project-orchestrator 1.11.1，本批把它们标为含不兼容变更（默认输出语言改变）。SOP（`sop.py`、`private_paths.py`）与 local-only（`local_only.py`）的命令输出也已纳入。

## 2026-10-29 — agenthelm 6.11.0 / Opinion Manager 0.5.0 / Agent Pack 1.11.0 / Project Orchestrator 1.11.1：观点自动采集与未决事项提示

6.11.0 同时包含下面 2026-10-29（6.10.0，local-only）和 2026-10-28（6.9.0，SOP）两个条目的全部变化：6.9.0、6.10.0 都没有单独发布，从 6.8.0 升级的用户一次得到三部分。

Opinion 之前只在用户明确提出时才工作：没有 hook，没有低成本的暂存入口，也没有任何东西提醒用户回头确认。这个版本让它在日常工作中自己转起来：会话开始注入规则和协议，工作中发现长期偏好就暂存，任务完成时集中请用户确认，开新会话时按阈值提示久未处理的候选。采集是自动的，生效与转正式仍然只来自用户的明确确认。

### opinion-manager 0.5.0

- 新增候选收件箱 `opinion_manager.py inbox`（数据在项目私有的 `.opinion/inbox.json`）：
  - `add` 暂存（标题、完整规则、适用范围、用户原话），无需确认；内容相同的候选合并并追加证据；
  - 状态 `pending`（待确认）/ `trial`（项目内试行）/ `formal`（同意转正式）/ `held` / `rejected`；`decide` 必须带 `--confirmed`，批量要么全成功要么全不改；
  - `trial` 的候选全文随会话开始注入，Agent 按其工作；`expire --days N` 把试行超期未转正的退回待确认；
  - 提示与协议有中文、英文两种（`--lang zh|en`，缺省按 `AGENTHELM_LANG`、系统 locale 决定，最后用英文）；候选内容是用户自己的话，不翻译；历史记录用与语言无关的编码；
  - `exposure` 只读报告收件箱在 Git 里的状态（已忽略 / 已跟踪 / 未忽略 / 非 Git 仓库）；
  - `pending` 是“待确认项提供者”，返回统一结构，供 agent-pack 汇总（SOP 等其他来源按同一结构接入）；`brief` 生成会话开始注入的协议与试行候选。
- `formal` 只记录用户意愿，写入 `OPINION.md` 仍走完整预览与确认；`inbox` 不写 `OPINION.md`、Profile 或锁。
- `SKILL.md`：触发描述增加“工作中出现长期偏好或纠正”“任务完成时确认”“候选堆积时裁决”；新增候选收件箱与自动采集一节。新增 [`references/inbox.md`](plugins/opinion-manager/references/inbox.md)。

### agent-pack 1.11.0

- 新增 `agenthelm nudge` 与随附 hook（`hooks/hooks.json`）：
  - **会话开始**（`startup|resume|clear|compact`）：在启用了 opinion-manager 的项目里注入采集协议与试行候选；待确认 ≥ `pending_threshold`（默认 5）条或最久 ≥ `stale_days`（默认 7）天时给用户一行提示，同一项目每 24 小时至多一次；
  - **用户消息**：命中长期偏好关键词（可配置）时提醒 Agent 静默暂存，每会话至多 3 次；
  - **回合结束**：本会话暂存且没展示过的候选存在、最后一条回复像是完成时，让 Agent 用编号列表一次性请用户裁决，每会话至多 1 次。
- 各客户端：Claude Code 与 Codex 读取同一个 `hooks/hooks.json`（Codex 需在 `/hooks` 中信任）；Cursor 没有 prompt 级注入，用 `agenthelm nudge install-hooks --client cursor` 预览并写入 `hooks.json`，stop 提示默认关闭。其他目录完全无输出，hook 永远退出 0。
- 提示语言：给用户看的提示和注入给模型的协议使用同一种语言（中文 / 英文）。顺序：`nudge.language`（非 `auto` 时，用户配置优先于项目 Profile）→ `AGENTHELM_LANG` → 系统 locale → 英文；解析结果传给 opinion-manager 的 `inbox --lang`。协议要求 Agent 向用户转告和确认时使用用户正在使用的语言。文案集中在 `nudge_text.py` / `inbox_text.py`，测试守住两种语言的键与占位符一致、英文不含中文。这是运行时多语言的第一步，仓库里其他命令的输出暂时仍为中文。新增的 `inbox` / `nudge` 命令行帮助为英文，两份 references 各有中文与英文版（`inbox.md` / `inbox.en.md`、`nudge.md` / `nudge.en.md`）。
- 配置：Project Profile 的 `opinion.nudge`（随项目）与用户配置的 `nudge`（个人，优先）；缺省即默认值，不进脚手架受检字段。`agenthelm nudge status` 显示生效配置与是否会提示；`AGENTHELM_NO_NUDGE=1` 关闭。详见 [`references/nudge.md`](plugins/agent-pack/references/nudge.md)。
- 尚未在真实客户端逐一验证：Codex 插件 hook 的信任流程、Cursor 的 sessionStart 注入与 `~/.cursor/hooks.json` 加载、Claude Code `Stop` 的 `additionalContext`；依据官方文档编写，单元测试覆盖输出格式与逻辑。

### project-orchestrator 1.11.1

- 在 1.10.0（SOP 合并）已取消 `sensitivity` 过滤的基础上补两点：`find` 等结果带每个 Case 的 `sensitivity` 字段，菜单里标注“私密”；台账结构版本升到 2，使 1.10.0 之前建立、指纹没变的旧台账下次使用时自动重建（否则旧缓存里仍没有私密 Case）。凭据类文件名仍不登记，台账仍在用户缓存目录、不进项目。

### 不兼容点与注意

- 无。没有 `.opinion/`、`opinion.lock.json` 或 `opinion.provider: opinion-manager` 的项目不受影响。
- 启用 Opinion 的项目，新会话会多出约 1500 字符的协议上下文；不需要时设置 `nudge.enabled: false` 或 `AGENTHELM_NO_NUDGE=1`。
- 收件箱 `.opinion/inbox.json` 保存用户原话，可能含敏感信息，建议启用本功能的项目保持私有。是否公开仓库、是否排除 `.opinion/` 由你决定：工具只在第一次暂存（`inbox add` 返回 `notice`）和每个项目第一次开会话时提醒一次，`inbox exposure` / `agenthelm nudge status` 随时可查；不会替你决定，也不会自动修改 `.gitignore`、删除或改写内容。

### 版本

agenthelm 6.11.0、opinion-manager 0.5.0、agent-pack 1.11.0、project-orchestrator 1.11.1；agents-init 6.8.0 不变。

## 2026-10-29 — agenthelm 6.10.0 / Agent Pack 1.10.0 / Project Orchestrator 1.11.0：同步盘里的本机专属目录

项目放在 iCloud、Dropbox、OneDrive 等同步目录、在多台设备上使用时，`.venv`、`node_modules`、工具缓存不该进同步盘。`agenthelm local-only` 把它们放到每台设备自己的 `~/.local/share/agenthelm/local-only/` 下并留软链接，同时检查各设备之间的差异（谁装好了依赖、谁没装、HOME 是否一致）。

6.10.0 同时包含下面 2026-10-28 条目（6.9.0，流程（SOP）管理）的全部变化：6.9.0 没有单独发布，从 6.8.0 升级的用户一次得到两部分。

### agent-pack 1.10.0：同步盘里的本机专属目录（local-only）

项目放在 iCloud、Dropbox、OneDrive 等同步目录里、在多台设备上使用时，`.venv`、`node_modules`、工具缓存不该进同步盘。新增 `runtime/local_only.py`（只用标准库）、命令 `agenthelm local-only`、只读 MCP Tool `agent_pack_cloud_sync_safety_check`，以及 `references/local-only.md`。

- **外置**：`local-only fix` 把虚拟环境（按 `pyvenv.cfg` 识别，不看名字）和可外置的 `node_modules` 移到 `~/.local/share/agenthelm/local-only/<project_id>/<相对路径>`，原位置建软链接。默认只预览，`--apply` 才写；跨设备移动先复制并核对文件数与字节数；任何一步失败整体撤销；旧配置先备份。`--rebuild` 在本机按锁文件重新安装（`uv sync`、`poetry install`、`yarn/bun install --frozen-lockfile`）。
- **硬约束**：链接文本随项目同步，必须各设备相同——目标路径只由项目里记录的 `home`、`project_id` 和相对路径拼成，不读 `XDG_DATA_HOME`；各设备 HOME 的绝对路径必须一致，不一致时 `check` 报告、`fix` 拒绝写入。
- **实测依据**（Linux 容器，2026-10-29）：uv 0.11.32、普通 venv、yarn 1.22.22、bun 1.4.4 可以外置；**npm 10.9.4 把链接换回真实目录，pnpm 10.28.0 安装失败并删掉链接目标**——同步目录里的 npm / pnpm 项目只报告并建议改用 yarn 或 bun（不开云同步时不提示）；缓存类用 `PYTHONPYCACHEPREFIX`、`RUFF_CACHE_DIR`、`MYPY_CACHE_DIR`、`pytest -o cache_dir` 外置，工具只给写法。
- **检查**：`local-only check`（`quick` / `standard` / `full` 三档时间预算，会话开始钩子只用 `quick`）发现真实的本机专属目录、已声明条目的状态（正常 / 本机未安装 / 尚未建立 / 又变回真实目录 / 指向别处）、包管理器、体积阈值、`-wal` / `-shm` / pid / socket 文件、冲突副本、iCloud 未下载占位文件（不读取内容）。“本机未安装”就是设备 B 没装好依赖的信号——此时直接运行 `uv sync` 会报 `File exists`，`fix --apply --rebuild` 修复。
- **默认保持同步**：构建产物（`dist`、`build`、`out`）和本机工具状态（`.claude`、`.cursor`、`.idea`、`.vscode`）；可在配置的 `keep_synced` 调整。带 `CACHEDIR.TAG` 的目录算工具缓存，不算构建产物。
- **体积阈值可配置**：每个顶层目录默认 200 MB 或 2 万个文件，达到 80% 开始提示；`.agents/moe.sakanano.agent-pack/local-only.json`（随项目同步，`fix` 创建）和 `~/.config/agenthelm/config.json` 的 `local_only` 段都能改，项目配置优先。接近阈值时只给选项由你决定——提高阈值（`fix --raise-limit 目录=MB/文件数`）、不再提示（`fix --skip`）、在同步服务里设为不同步——不提供“外置”（普通内容外置后其他设备看不到）。
- **跨设备对比**：`local-only check --fingerprint [--explain 对方.json]`；`agenthelm check --fingerprint` 在项目配置了 local-only 时也带“本机环境”段并一并比较。指纹只含 HOME、链接状态与链接文本，不含平台与体积；没有配置的项目输出与以前完全一样。
- **汇总**：`agenthelm check` / `health` / 会话开始提示汇总同步盘问题（`local:` 开头的本机发现，决定存用户状态目录）；“需要处理”的项让 `up_to_date` 变为 false、`check --strict` 退出码为 1，`health` 的退出码不变。不在同步盘里、也没有配置的项目不扫描，输出与退出码不变。
- **同步盘识别**：补上 macOS File Provider 的 `~/Library/CloudStorage/Dropbox-*`；iCloud“桌面与文稿”同步的路径上看不出来，用 `fix --service icloud` 声明。
- **未确认**：iCloud、Dropbox、OneDrive 是否同步软链接本身；yarn 2+；各服务冲突副本的命名（只识别 Dropbox 的 `conflicted copy` 与 iCloud 的 `.icloud` 占位文件）；macOS 上是否正在同步、账号状态没有找到可靠的检测办法，不报告。Windows 上只支持检查。

### project-orchestrator 1.11.0

- `health` 汇总同步盘里的本机专属目录问题（area 为“同步盘”，ID 以 `local:` 开头，决定只记在这台机器上）；`fix local-only` 展示 `agenthelm local-only fix` 的预览（不自己执行移动）；`fix sync-folder` 的指引改为推荐 `local-only`，不再建议对 npm 无效的 `.nosync` 做法。

### 不兼容点与注意

- 无不兼容变更。
- **同步盘里的项目**：`check` / `health` 会多出“同步目录里有本机专属目录”等发现；`agenthelm check --strict` 与会话开始的一行提示把需要处理的项算作未处理。不需要的用 `fix --skip <ID> --reason …` 跳过。不在同步盘里、也没有 local-only 配置的项目，输出与退出码不变。
- 只在 Linux 容器里做了 uv、yarn、bun、npm、pnpm 的实测；macOS 上跑过测试，但 iCloud、Dropbox、OneDrive 是否同步软链接本身没有在真机核对（见上文“未确认”）。

### 版本

agenthelm 6.10.0、agent-pack 1.10.0、project-orchestrator 1.11.0；agents-init 6.8.0 与 project-orchestrator 1.10.0 的变化（6.9.0 条目）首次随本版发布；opinion-manager 0.4.2 不变。

## 2026-10-28 — agenthelm 6.9.0 / Agents Init 6.8.0 / Project Orchestrator 1.10.0：流程（SOP）管理（未单独发布，随 6.10.0 一起发布）

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
