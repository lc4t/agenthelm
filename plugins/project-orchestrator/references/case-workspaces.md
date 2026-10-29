# Case 工作目录契约与工具

`work.mode=case-workspace` 将 Case 作为唯一持久工作容器。项目自身的 Schema、创建和状态校验 Tool 保持权威，本包不假设某个项目的状态字段。缺失模式的旧项目保留原工作流，显式迁移后才切换。

## 目录与引用

```text
cases/CASE-ID/
├── CASE.md
├── docs/（最终交付；drafts/ 保存草稿）
├── refs/（本 Case 的输入与来源快照）
├── data/
├── images/
├── scripts/
├── work-items/（复杂子任务；可选）
└── .agent-doc/（计划、决策、验证与迁移凭证）
```

所有目录按需创建，简单 Case 只需 CASE.md。自有文档使用相对链接并保留完整附件；项目工程、正式规则和跨 Case 的版本化公共资产继续使用原权威位置。来源包中的 AGENTS.md 和 OPINION.md 作为历史材料，不成为当前项目指令。

### 文件语言

Case 正文的结构按项目文件语言生成，由 Project Profile 的 `language`（`zh` / `en`，缺省 `zh`）决定，随项目同步，不随各人命令行界面的语言（`AGENTHELM_LANG`）变化：

- 新建 Case 用 `templates/CASE.md`（中文）或 `templates/CASE.en.md`（英文）；字段占位的默认文字也随语言（“刚创建 / 待明确” 对应 “Just created / To be defined”）。
- 更新时认两种小节名：当前可信状态 / Current trusted state、下一动作 / Next action、决策记录 / Decision log、工作记录 / Work log、结果 / Result。文件里已有哪种写法就用哪种，找不到小节时按项目语言新建，所以旧 Case、换过语言的项目与手写的 Case 都能照常追加。
- 初始化时用 `init_project.py --language zh|en` 设定；它只影响之后生成的文件，已有 Case 不会被改写。

### 最小记录与项目特化

已有项目使用自己的 Case Schema 和状态工具。未声明 Case 创建工具的新项目可由 Agent 创建目录和 CASE.md，最小 Frontmatter 必须包含与目录相同的 `id`、可理解的 `title` 和 `status`；同时保存目标、下一动作、证据与决策，避免仅存文件而丢失恢复上下文。日期序号从当日现有目录取最大值加一，子工作项不取得第二套顶层生命周期。

```yaml
id: "CASE-20260101-001-example"
title: "示例工作"
status: "active"
sensitivity: "shared"
goal: "可核验的目标"
next_action: "下一项明确动作"
aliases: []
legacy_ids: []
tags: []
deliverables: []
evidence: []
```

示例只定义发现工具的通用输入，生产项目仍以自己的 Schema 为准。closed / done 等状态的语义、完成条件与审批由项目决定，工具不得据文件存在自行认定业务完成。

### 默认 Case 工具（官方样例）

没有自己 Case 工具的项目可直接使用随包的 `runtime/case.py`。它读取 Project Profile 的 `work.case.root`，按 [默认 Schema](case.schema.json) 校验，新建时使用 [CASE.md 模板](../templates/CASE.md)：

```bash
python3 runtime/case.py --project <项目> new --title "季度报告" --slug quarterly-report \
  --goal "提交经评审的季度报告" --next-action "收集各组数据"
python3 runtime/case.py --project <项目> list            # 未关闭的 Case，进行中在前；--all 含已关闭
python3 runtime/case.py --project <项目> show quarterly   # 按 ID 或 ID 片段
python3 runtime/case.py --project <项目> update quarterly --log "完成初稿" \
  --next-action "提交评审" --add-deliverable docs/report.md
python3 runtime/case.py --project <项目> validate
```

- `update` 只改指定字段，并向「工作记录」「决策记录」追加带时间的条目，其余正文原样保留；`--expect-updated-at` 防止覆盖并发修改。`active` 需要 `next_action`，`waiting` 需要 `waiting_for`；其余完成语义仍由项目决定。
- Profile 的 `work.case.command` 声明了项目自己的工具时，本工具只做 `list` / `show` / `validate`，写操作返回 `project-tool` 并提示改用项目工具；`work.case.schema` 可替换默认 Schema。
- 模板与 Schema 属于内容包 `project-orchestrator/case-template`，随插件版本更新，只影响之后新建的 Case，不改写已有 Case。

### 交付物台账与 wf 查找（官方样例）

`runtime/assets.py` 把 Case 与额外扫描根下的文件登记成一份只读台账，回答“文件在哪、属于哪个 Case、有哪些版本、谁引用它、改动会波及谁”。`runtime/wf.py` 是同一台账的人用入口，不需要 Agent。

```bash
python3 runtime/assets.py --project <项目> find 笔记 9月1号      # 标题、文件名、日期与同义词；同系列版本归为一组
python3 runtime/assets.py --project <项目> find 方案 --ext xlsx --kind deliverable --in alpha
python3 runtime/assets.py --project <项目> refs cases/<CASE-ID>/docs/report.md --depth 2
python3 runtime/assets.py --project <项目> check                 # 无人认领的交付物、断链、跨容器重复、上游更新
python3 runtime/wf.py 笔记 9月1号                                 # 在项目内任意目录运行；o1 打开、v1 看版本、r1 看引用
```

- 扫描范围：Case 根取 `work.case.root`（缺省 `cases`）；`work.assets.roots` 可追加项目内的额外根（例如 `["projects"]`），每个一级子目录视为一个容器；未声明时只扫 Case 根，没有 Project Profile 时按缺省值运行。Case 内文件按 `docs/`、`docs/drafts/`、`refs/`、`data/` 等目录推断角色；`CASE.md` 的 `deliverables` / `evidence` 视为声明引用，正文中的 Markdown 链接、wikilink、反引号路径、Case 编号与旧 Task 路径（`legacy_ids`）都会建立引用边。开头带 `<!-- derived:` 标记的生成视图不作为引用来源。
- 同义词：可选文件 `.agents/moe.sakanano.agent-pack/asset-synonyms.json`，格式见 [示例](asset-synonyms.example.json)（`groups` 中每组互为同义）；缺省为空，不内置任何词表。
- 不登记：文件名含凭据类词语（credential、password、secret、token、cookie、.env、私钥等）或扩展名为证书/密钥的文件；`.git`、`node_modules` 等工具目录与 `cache` 类缓存目录；含 `.svn` / `.git` 的嵌套外部检出（被 Git 忽略时上溯到最外层被忽略目录）。Git 忽略的文件只在属于文档、表格或压缩包时登记元数据。不需要安装 SVN；项目不是 Git 工作区或没有 git 时，Git 状态记为 `none`。
- 私密 Case：`sensitivity` 不是 `shared` 的 Case 也登记，`find` 能找到它们（结果带 `sensitivity` 字段），台账只是索引，不做可见性过滤；其中的凭据类文件名仍不登记。台账位于用户缓存目录，不进项目。
- 状态与写入：台账为 SQLite，位于 `$XDG_CACHE_HOME/agenthelm/assets/<项目摘要>/`（缺省 `~/.cache`），文件清单未变时重建为 no-op；`wf` 的“上一次结果”也存于此。两者都不写项目，缓存目录落在项目内时拒绝运行。
- 平台：macOS 上 iCloud 未下载的文件（dataless）只登记元数据，不读取、不算哈希；`wf` 的打开、在 Finder 中显示、复制路径调用 `open` / `pbcopy`，其他平台或命令缺失时打印绝对路径。
- 两个命令都接受 `--project` 与 `--output json`，写在子命令前后均可；查找结果以文件本身与 CASE.md 为准，台账只是可重建的投影。

## 项目入口（不需要 AI Agent）

`runtime/project_cli.py` 是面向人的项目菜单，经 `agenthelm project` 或项目根目录的启动器（`agents-init` 生成，缺省名 `menu`）打开。它不调用模型，不需要 AI 客户端，按想做的事组织：

| 菜单 | 子命令 | 来源 |
|---|---|---|
| 找资料 | `find <词…>`、`open <路径>`、`refs <路径>` | 交付物台账（上一节），首次查找时建立 |
| 看进展 | `progress [词]`、`show <事项>` | 各 `CASE.md`，或下文的项目事项工具 |
| 项目指南 | `guide` | Profile 的 `entry`、README 开头一段与目录结构 |
| 检查项目与环境 | `health` | 运行环境、入口、索引、事项记录、记忆、项目工具；经 agent-pack 检查插件、模板、规则与运行时 |
| 修复与更新 | `fix [项…]` | 重建索引、更新插件、更新模板（改过的文件逐个保留或采用）、安装或更新运行时、补上入口 |
| 记录事项 | `record <事项|new>` | 默认 Case 工具或项目事项工具的写接口 |
| 项目工具 | `tools [工具]` | 项目在 tools.json 声明的操作 |

约定：

- 写入类子命令默认只预览，`--apply` 才执行；菜单里输入 `yes` 才执行。执行后回读验证，并给出恢复位置。
- 记录事项写入前核对 `CASE.md` 的内容摘要与 `updated_at`，别人在预览之后改过就拒绝写入。未知字段与其他小节原样保留。
- 结果只呈现已记录的事实，不推断优先级、重要性或是否完成。
- 非交互时从不等待输入；`--output json` 输出 `{"ok", "attention", "exit_code", "command", "demo", "data"}`，出错时为 `error: {code, message, next}`。
- 退出码：0 成功；1 没有结果或有必须处理的项；2 用法错误；3 运行时缺失或与项目入口不兼容；4 能力当前不可用；5 写入冲突；6 执行失败；130 中断。
- `--offline` 不联网：版本检查只用缓存并标注缓存时间，需要联网的维护只预览。
- `--demo study|personal|research|blank` 使用内置的虚构项目，结果都标注「演示」，不读写真实文件。
- 打开、显示所在目录、复制路径：macOS 用 `open` / `pbcopy`；Linux 用 `xdg-open` 与 `wl-copy` / `xclip` / `xsel`（未在桌面环境逐一验证）；做不到时打印绝对路径。设置 `AGENTHELM_OPEN=print` 总是只打印路径。

### 项目入口适配

项目在 Profile 的 `entry` 段声明自己的情况，公共代码不写死任何项目：

```json
"entry": {
  "api": 1,
  "launcher": "menu",
  "about": "一句话说明项目做什么",
  "guide": "docs/START.md",
  "where": [{"label": "观察记录", "path": "observations/"}],
  "start": ["先看进行中的事项", "需要资料用「找资料」"],
  "tools": ".agents/moe.sakanano.agent-pack/tools.json",
  "items": {"command": ["python3", "tools/items.py"], "write": false,
            "status_map": {"进行中": "active", "待复核": "waiting", "归档": "done"}}
}
```

**项目事项工具（`entry.items`）**：项目用自己的工具管理事项时声明它，`command` 是参数列表，不经过 shell，在项目根目录运行。

- 只读接口（必需）：`<command> list --all --output json` 返回 `{"items": [...]}`。每项可含 `id`、`title`、`status`、`goal`、`next_action`、`waiting_for`、`resume_when`、`last_record: {at, text}`、`deliverables`、`aliases`、`legacy_ids`、`updated_at`、`path`（权威记录的项目相对路径）。`status_map` 把项目自己的状态名映射到 `active` / `waiting` / `captured` / `suspended` / `done` / `abandoned`，没映射的显示为“其他状态”。
- 写接口（可选，`write: true` 才启用）：`<command> new --title … --goal … [--next-action …]` 与 `<command> update <id> [--log …] [--next-action …] [--waiting-for …] [--status …] [--add-deliverable <路径>] [--expect-updated-at <值>]`，都追加 `--output json`。带 `--dry-run` 时只返回 `{"preview": [行…], "path": …}`，不写；`updated_at` 不一致时以非零退出并返回 `{"code": "conflict"}`。
- 没有声明写接口时，记录事项显示为不可用并说明缺口，入口不会绕过项目工具直接改文件。Profile 只有 `work.case.command`、没有 `entry.items` 时，进展仍按 `CASE.md` 读取，写入同样让位给项目工具。

**项目工具（tools.json）**：

```json
{"schema_version": 1, "tools": [
  {"id": "export-csv", "title": "导出观察表", "description": "把全部记录导出为 CSV",
   "command": ["python3", "tools/obs.py", "export"], "writes": ["exports/"],
   "network": false, "requires": {"commands": ["python3"], "env": []}}
]}
```

只列出声明且当前可用的工具；缺命令、缺环境变量（账号令牌等只写变量名）或离线时显示为不可用并写明原因。打开菜单时从不运行任何工具；执行前展示完整命令、是否联网、需要的账号与写入范围，由人确认。私有实现与配置留在项目里。

## 迁移工具

运行 `runtime/case_workspace.py migrate --project <项目> --map <映射.json>` 先预演。审阅映射后追加 `--apply`。映射必须有 `schema_version: "1.0"` 和 `moves` 数组，每项包含项目相对 `source`、`target`；目标必须处于 cases 的一个 Case 目录内。可选 `records` 数组包含 `path`（目标 CASE.md）、`content`（项目创建器预先生成的正文），工具只负责排他创建，不替项目认定状态。

```json
{
  "schema_version": "1.0",
  "moves": [{"source": "tasks/20260101-example", "target": "cases/CASE-20260101-001-example"}],
  "records": [{"path": "cases/CASE-20260101-001-example/CASE.md", "content": "项目工具生成的完整 Case 正文"}]
}
```

目标根目录读取 Project Profile 的 `work.case.root`，缺失时使用 `cases`。目录与单文件均可映射，目标需要位于一个明确的 Case 工作目录内。迁移器拒绝绝对路径、路径逃逸、symlink、非普通文件、源目标重叠和已有目标；预演严格零写入。应用使用同文件系统的原子排他 rename；迁移前后检查路径、文件数、字节数和 SHA-256。凭据文件及隐私目录中的文件只记录大小与文件身份，整体 rename 不读取内容，输出 `opaque_files` 明确核验边界。新 CASE.md 或迁移中途失败时撤回本次创建的记录与移动，不删除既有文件。跨文件系统或缺少原子禁止覆盖 rename 的平台失败关闭。

输出 JSON 的 `moves[].manifest` 可保存到 Case `.agent-doc/` 作为迁移证据。工具执行前确保没有其他客户端修改本次源目录；此工具不提供跨进程锁。项目规则决定 Git 提交与发布，工具不执行它们。

迁移后由项目 Tool 执行 Case 校验、链接检查、索引重建、Dashboard 刷新。旧 ID 记入 Case 的 `legacy_ids`，检索可使用本包 `search --project <项目> --term <词>`；只读取 CASE.md 元数据与文件名，不读取 Artifact 正文。`inspect` 输出全量 Case 文件数和体积，包含关闭状态，供项目 Dashboard 消费。它们不代替项目的完整 Schema 校验或质量门禁。

## 初始化器升级

新项目默认 Case 工作目录；既有项目的模式默认保留。显式使用 `init_project.py --mode migrate --work-mode case-workspace --replace .agents/moe.sakanano.agent-pack/project.json --recovery-dir <项目外目录>` 更新 Profile。原 task 配置保留为 `legacy_task`，初始化器不搬业务文件；原数据迁移由本工具的已审阅映射单独执行。私有路径、业务内容和映射只保存于目标项目，不进入公共分发包。
