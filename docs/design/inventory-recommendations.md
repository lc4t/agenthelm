# 清单推荐、影响面与 MCP 目标判定：设计文档（#184）

状态：设计稿，待维护者审阅。基线 `dev` 头提交 `59c8ddc`（6.15.0 已发布），目标周期 6.16.0。本文只描述概念、数据模型、JSON 字段、文案草稿、测试与拆分，不含产品代码，不改任何运行时、版本、变更片段或发布载体。
议题：[#184](https://github.com/lc4t/agenthelm/issues/184)（正文与补充评论）。上游设计：[`skills-mcp-menu.md`](./skills-mcp-menu.md)（CB 组，下称 **CB**，决策 D-CB1…D-CB13）与 [`mcp-skill-multi-project.md`](./mcp-skill-multi-project.md)（#82，下称 **#82**，决策 D1…D15）。客户端声明见 [`client-drivers.md`](./client-drivers.md) 与 [`client-protocols.md`](./client-protocols.md)。契约文件：`plugins/agent-pack/references/inventory-contract.md`、`dedupe-contract.md`、`relocate-contract.md`（各有 `.en.md`）。

## 0. 结论摘要

1. **#184 拆成五步，6.16.0 只实现前两步。** 步骤 1 影响面与可达性（Impact and Reachability）、步骤 2 MCP 误报（目标服务判定）进入 6.16.0；步骤 3 推荐引擎、步骤 4 批量预览与应用、步骤 5 传输方式只设计，排入下一版及以后。（§2）
2. **两个核心现象已在 6.15.0 上复现**（临时 HOME 夹具，只读预览加一次 `--apply`，见 §1.2、§1.3）：
   - 把 `~/.agents/skills/<名>` 保留、`<项目>/.agents/skills/<名>` 退役时，`dedupe` 的预览为 `ready`、`warnings` 为空；`--apply` 成功，项目里指向它的 `.claude/skills/<名>` 软链悬空，`inventory` 随后报 `broken-symlink`。实际被归档的是 `.agents/skills` 软链背后的真实目录。
   - MCP 指纹相同而目标不同有四种机制，其中三种在源码里可直接复现（§1.3）：长于 64 字符的路径参数被当成令牌脱敏；形似令牌的服务标识参数被脱敏；`env` 值与 Codex 的 `cwd` 不进指纹。维护者那 3 组的具体成因**未确认**（没有数据），设计按机制覆盖。
3. **步骤 1 的设计**：新增纯函数模块 `agent_pack_reach.py`，输入一次处置（保留谁、退役谁、迁到哪里）与文件事实，输出逐客户端判决 `unchanged | lost | replaced | gained | uncertain | unknown`。读取集合来自驱动声明，符号链按物理路径逐跳展开；Cursor 文档记载但 inventory 未建模的兼容读取位置作为新的驱动声明 `compat_reads`，只参与影响面，从不改变 inventory 与 relocate 的既有输出。结果写入 `dedupe` 与 `relocate` 计划的新键 `impact`，并生成 `warnings`，进入 `plan_digest`。（§3）
4. **步骤 2 的设计**：指纹（fingerprint）保持不变，避免改变分组 id 与决定文件的键；新增进程内比较「目标服务」的判据（目标类型、目标标识、其余参数、`env` 非密钥值、`cwd`），输出每条 MCP 的 `target` 与 `target_class`，把「指纹相同但目标不同或无法确认」的候选从 `duplicates.mcps[]` 移到新键 `mcp_hints[]`。比较用的原值只在进程内使用，输出只含类别名与序号。（§4）
5. **`schema_version` 保持 1。** 理由：键只追加；菜单对未知主版本降级为「不可用」（`project_extensions.py:121-126`），升版会让菜单整体失效。代价与守卫见 §4.9。
6. **推荐引擎（步骤 3）沿用 CB「不做质量打分」**，只输出由固定规则导出的处置建议，规则编号可复现；推荐 `retire` 时 CB 与 #82 的「`loaded` 已确定才给可清理候选」与现实（`loaded` 恒为 `unknown`）冲突，列为决策项 D-R4。（§6）
7. **重叠分析结论（§9）**：#69 的三项、#73、#72、#67、#64 与 #65 的残余项**全部判为独立、排除**，没有一项并入 6.16.0。只有两处词汇对齐（`requires_cli`、`auth` 的字段名沿用 #82 §7）记入步骤 3。
8. **实施拆分（§5）**：T1（影响面，agent-pack）与 T2（目标判定，agent-pack）可并行，冲突集中在 `agent_pack_dedupe.py` 的不同函数、两份契约文档和 `locale/en.json`；T3（project-orchestrator 消费 `target_class`）依赖 T2 的字段名，可对夹具并行开发。
9. 需要维护者拍板的取舍见 §10（D-R1…D-R10），未核实清单见 §11。

**这个领域的标准流程**：包与配置管理的通用做法是「盘点（inventory）→ 解析（算出每份条目对每个消费者的可见性）→ 预览（diff，不写）→ 应用（事务、先备份）→ 读回校验 → 退役（归档、可恢复）」，关键环节是把「改动会影响谁」在预览阶段算出来并显式列出，证据不足时降级为「未知」而不是给结论。**当前行业内「好」的判据来源**：影响面按消费者逐一列出，这一点本文参照 Claude Code 官方文档对重复服务的处理方式（同名按作用域优先级、插件与连接器按端点匹配，URL 仅在 scheme/host 大小写、默认端口、末尾斜杠上不同才算同一端点，[MCP 文档 Scope hierarchy and precedence](https://code.claude.com/docs/en/mcp)，2026-11-24 读取）；证据分级与「证据不足不给破坏性结论」沿用 #82 §0 条 4 与 §9 条 7。这些是厂商文档与本仓库既有设计的约定，不是对本仓库的强制要求。

---

## 1. 前提与已读到的事实

### 1.1 仓库现状（读源码得到）

- `inventory`（`agent_pack_inventory.py`）：Skill 条目含 `is_symlink`、`link_target_kind`、`symlink{is_symlink,target}`、`evidence`；`broken-symlink` 条目的 `evidence` 为 `unknown`。读取位置来自驱动声明（`client_registry.store_table`）。`loaded` 恒为 `"unknown"`（`inventory-contract.md` 的 `loaded` 条）。
- 重复分组：`skill_duplicates`、`mcp_duplicates`（`agent_pack_inventory.py:730`、`:758`）；MCP 的 `same-fingerprint` 组按「同客户端、同指纹、不同名」生成。
- `dedupe`（`agent_pack_dedupe.py`）：`list_groups` 把 `duplicates` 转成分组（`:58`）；`cross_client_hint` 是 D-CB4 的守卫（`:110-111`，警告码 `cross-client-keep-recommended`）；`_plan_retire`（`:184`）只检查「保留项依赖要退役的目录」（`keep-depends-on-retired`，且仅对非符号链接的退役项），不检查其他条目是否依赖被退役的目录。
- `relocate`（`agent_pack_relocate.py`、`agent_pack_mcp_move.py`）：计划外壳含 `warnings`、`confirmations[{id,text,flag}]`、`plan_digest`；`plan_digest` 排除 `message` 与 `text`，与界面语言无关（`agent_pack_ext_common.py` 的 `plan_digest`、`_without_text`）。
- 驱动声明：`runtime/drivers/*.json` 的 `stores.skills[]` / `stores.mcp[]` 声明各客户端读取的位置，条目键集合固定为 `STORE_KEYS`（`agent_pack_clients.py:79`）；`status: undetermined` 的位置被 `stores()` 排除。驱动快照测试 `test_data_files_equal_the_v6_14_0_builtin_snapshot` 对 `stores` 列表做相等比较，新增位置会破坏它（见 §3.5）。
- 菜单（project-orchestrator）：`project_extensions.py` 自己用 inventory 条目的 `fingerprint` 重新分组（`build_groups`，`diff-name-same-fp` 类），不读 `duplicates.mcps[]`；对未知 `schema_version` 主版本降级为「不可用」（`:121-126`）。`project_ext_actions.py:108` 转发的 `relocate` 标志只有 `--keep-source`、`--allow-widen`、`--confirm-project-content`。
- 守卫：`test_client_protocol_guard.py` 要求 `runtime/*.py` 不含任何已注册客户端 id 的字符串常量（`CAPS` 为空表）；`test_synthetic_client_guard.py` 用随机 id 的合成客户端验证命令无需改代码即可处理。新模块必须从驱动声明读取一切。
- `check` 的「项目声明与本机不一致」提示由 `capability_notice`（`agenthelm.py:1478-1482`）生成，只列客户端名；`client_drift` 返回的 `missing / different / stale / native` 列表（`agent_pack_config.py:2019` 起）已含具体条目，提示没有使用（步骤 5 用）。

### 1.2 在 6.15.0 上复现：退役使另一个客户端的软链悬空

夹具（临时 HOME，路径为占位）：`~/.agents/skills/pdf-notes/SKILL.md` 与 `<项目>/skills/pdf-notes/SKILL.md` 内容相同；`<项目>/.agents/skills` 是指向 `../skills` 的软链；`<项目>/.claude/skills/pdf-notes` 是指向 `../../.agents/skills/pdf-notes` 的相对软链。

- `dedupe --list`：Codex 与 Cursor 各一组（`same-name`，`relation: identical`），与 #184 描述的「15 个 Skill × 2 个客户端 = 30 组」同形。
- `dedupe <组id> --keep skill:pdf-notes@codex:user#agents --retire-others` 的预览：`status: ready`，`warnings: []`，`blocked: []`，步骤为 `create-recovery-dir`、`archive-copy`、`confirm-in-client`。
- 带摘要 `--apply`：`applied`。归档的是 `<项目>/skills/pdf-notes`（`<项目>/.agents/skills/pdf-notes` 经父目录软链解析到它）；`<项目>/skills` 变空，`.claude/skills/pdf-notes` 悬空，`inventory --client claude` 随后给出 `issues: ["broken-symlink"]`、`evidence: unknown`。
- 因此 Claude Code 在该项目里失去了这个 Skill；它读取的位置只有 `~/.claude/skills` 与 `<项目>/.claude/skills`，不含 `~/.agents/skills`（驱动 `claude.json` 的 `stores.skills`；Claude Code 官方位置表同样没有列出 `.agents/skills`，见 §11 条 1）。

### 1.3 在 6.15.0 上复现：MCP 指纹相同而目标不同的机制

指纹取已脱敏的 `command + args`（stdio）或脱敏后的 `url`（`agent_pack_inventory.py:513-515`），不含 `env`、`headers`、`cwd`。`inventory-contract.md`「已知取舍」已写明：只在被脱敏的位置上取值不同的两条会得到同一个指纹。本文用只读函数 `normalize_mcp` 在夹具上验证了以下机制（结果均为「指纹相同」）：

- **M1 长路径参数被当成令牌。** `_redact_args` 对非选项、非 `${VAR}` 的参数调用 `looks_like_token`，长度超过 64 的字符串直接判为令牌（`agent_pack_config.py:56-57`），路径形状（`/`、`~` 开头）没有豁免。两个 `python3 <长度>64 的不同脚本路径> --port 8001 --mode stdio` 脱敏后都是 `<redacted> --port 8001 --mode stdio`，指纹相同，并且 `has_inline_secret` 被置真（附带的误报）。
- **M2 形似令牌的服务标识。** 24 字符以上、含大小写与数字的参数（常见于服务 ID）同样被脱敏：`--target AbCd…123` 与 `--target ZyXw…987` 指纹相同。
- **M3 目标在 `env` 里。** `env` 值不进指纹：同一 `command + args`、`env.TARGET` 不同的两条指纹相同。
- **M4 目标在 `cwd` 里。** Codex 的 `cwd` 键（#82 §2.3 记载）不进指纹（`normalize_mcp` 不读它）。M4 由代码阅读得到，未单独实测。
- 对照：仅 `--service alpha-api` 与 `--service beta-api` 不同（可读标识）时指纹**不同**，现有逻辑正确。

#184 第 3 条写的是「`python3` 加 5 个参数的启动方式相同」。这与 M1（路径参数被脱敏，其余 5 个参数相同）一致，但没有该用户的数据，**未确认**是哪一种机制。设计按 M1–M4 全部覆盖，并要求夹具逐一复现（§4.8）。

### 1.4 #184 评论里的现象

评论记录了桌面端内置客户端 2.1.330 上 HTTP + OAuth 路径的超时、状态与实际不一致、刷新锁等现象，来自维护者本机日志。**客户端行为是否缺陷未确认**，本文只把它们作为「传输方式影响推荐」的输入（步骤 5），不下结论。

---

## 2. 版本拆分：6.16.0 与之后

- **6.16.0 本版实现**
  - 步骤 1 影响面与可达性（任务 T1）。
  - 步骤 2 MCP 目标判定（任务 T2）与菜单消费（任务 T3，project-orchestrator）。
  - 与步骤 1 同一函数、同一份数据的两处附带：Skill 与 MCP 都走同一个 `impact`；`relocate` 把项目级迁到用户级时附一条「其他机器上的该项目不再带有它」的静态提示（D-R7，推荐并入，约 30 行与 2 条文案）。
- **下一版及以后（只设计）**
  - 步骤 3 推荐引擎：依赖步骤 1 的 `impact` 与步骤 2 的 `target_class`，所以排在它们之后。
  - 步骤 4 批量预览与应用：依赖步骤 3 的「同一批」选择器，也依赖步骤 1 把同一物理路径的影响合并。
  - 步骤 5 传输方式：依赖步骤 2 的 `target`（远程类型）与 #73/#69 的认证状态（均未实现，见 §9）。
- 排序理由：步骤 1 修复的是一次真实的静默失去能力；步骤 2 修复的是误报，其修正依赖同一份「读取与比较」的基础设施（驱动声明、进程内比较）；步骤 3 起的功能都在这两者的输出上叠加。

---

## 3. 步骤 1：影响面与可达性（6.16.0 实现）

### 3.1 输入与输出

- **输入**
  - 一次处置：`{kind, name, retire[], add[]}`。`retire[]` 是被归档的条目路径（`dedupe --retire-others` 的退役项；`relocate` 且 `retire_source` 为真时的源）。`add[]` 是新增的副本（`relocate` 的目标，含客户端、存储、内容摘要或指纹）。
  - 文件事实（`FsFacts`）：由读取函数 `read_facts` 从 `inventory.observe` 的报告与文件系统得到，纯函数本身不碰文件系统，测试直接传入字典。
  - 读取集合 `reads[client]`：来自驱动声明（§3.2）。
- **输出**：`impact` 对象（§3.7）与由它派生的 `warnings`。不产生 `blocked`（D-R3）。

### 3.2 各客户端读取哪些位置

- 读取集合 = 驱动 `stores.<kind>[]` 中 `status` 不是 `undetermined` 的全部位置（不限于 `write_target`）。
- 兼容读取集合 `compat_reads`：驱动新增的顶层可选键，列出「官方文档记载客户端也会读取、但 inventory 刻意未建模」的位置（现仅 Cursor 的 Skill：`~/.claude/skills`、`~/.codex/skills`、`<项目>/.claude/skills`、`<项目>/.codex/skills`，来源 #82 §2.3 事实 c，**本稿未能重读 cursor.com，未核实**，见 §11 条 2）。
  - 格式：`"compat_reads": {"skills": [{"scope": "user", "path": "~/.claude/skills", "status": "documented", "source_url": "…"}]}`。
  - 它只被 `agent_pack_reach` 读取。`stores()`、`store_table()`、inventory、relocate 的目标表都不读它，所以既有输出逐字节不变（这正是 inventory 契约「Cursor 的兼容加载未建模（有意）」要求的口径）。
- 使用 `compat_reads` 的规则：由声明路径算出「无副本可达」时，如果某个兼容位置里存在同名副本，判决降为 `uncertain`（原因 `compat-copy-present`），不写 `lost`。
- 客户端范围：默认集合内的客户端始终列出；默认集合之外的已注册客户端（`gemini`、`agy` 等）只在 `detect` 命中时列出（`detected: true`），否则不出现，避免「未安装的客户端将失去它」的噪声。读取集合含 `undetermined` 位置的客户端判 `unknown`（原因 `store-undetermined`）。
- 模块里不出现任何客户端 id 常量，遍历 `registry.all_ids()`（`test_client_protocol_guard.py` 与 `test_synthetic_client_guard.py`）。

### 3.3 路径、链与退役的物理语义

设 `physical(p) = realpath(dirname(p)) / basename(p)`：父目录的软链全部展开，最后一级不展开。验证（§1.2）：对 `<项目>/.agents/skills/pdf-notes` 做归档，被移动的是 `<项目>/skills/pdf-notes`，因为 `.agents/skills` 是软链。

- **链** `chain(e)`：从条目路径 `e.path` 起，反复 `readlink` 直到不再是软链或深度超过 8（`link-chain-too-deep`，判 `unknown`）；记录每一跳的 `physical` 路径与最终目录的 `realpath`。
- **退役集合** `R`：被归档条目的 `physical(path)`。若被归档的是真实目录，则 `realpath` 落在该目录之内的一切都随之消失；若被归档的是软链本身，只有这条链接消失，目标目录不动。
- **幸存判定**：条目 `e` 幸存，当且仅当：`physical(e.path) ∉ R`；`chain(e)` 的每一跳都 `∉ R`；`realpath(e)` 不在 `R` 中任何真实目录之内；并且 `e` 的最终目录含 `SKILL.md`（`resolved: true`）。不满足者为「悬空」，记入 `dangling_after`。
- **MCP**：条目没有链，退役集合是 `(store, name)`；幸存即「该客户端的某个读取位置里仍有同名条目」。

### 3.4 逐客户端判决

对每个客户端 `C`（名字 `N`）：`before` 为处置前可达的副本集合，`after` 为处置后可达的集合（幸存者加 `add[]`）。副本以内容摘要（Skill 的 `content_sha256`，MCP 的 `fingerprint`）区分。

- `unchanged`：`before` 与 `after` 的摘要集合相同（含两者都为空，此时记 `none` 且不列入输出的 `lost`）。
- `lost`：`before` 非空、`after` 为空，且没有降级条件。
- `replaced`：`after` 非空，但摘要集合与 `before` 不同（例如保留的那份内容不同）。该客户端仍有同名条目，实际加载哪一份取决于优先级（驱动 `precedence`），本步只陈述集合变化。
- `gained`：`before` 为空、`after` 非空（`relocate` 的目标客户端）。
- `uncertain`：声明路径上得到 `lost` 或 `replaced`，但存在不确定因素：`compat-copy-present`、`project-approval-unknown`（幸存者只有 Claude 项目级 `.mcp.json`，需逐项目批准，批准状态未读，#82 §2.3）、`project-trust-unknown`（幸存者只有声明了 `trust_required` 的项目级位置，如 Codex `.codex/config.toml`）。
- `unknown`：任一相关条目 `evidence != file`（含悬空、损坏、过大）、读取集合含未确定位置、链过深或成环。

「降级写法」的规则（贯穿 T1）：**证据不足时只写「无法确定」与原因代码，不写 `lost` 也不写「不受影响」；任何文案都不使用「可安全删除 / 可清理」。** 所有判决都是文件级：客户端是否真正加载取决于 `skillOverrides`、`[[skills.config]] enabled = false` 等设置键，inventory 本版不读（`loaded: "unknown"`），影响面输出里固定带一条 `basis: "file"` 与对应说明。

### 3.5 驱动声明的改动

- 新增顶层可选键 `compat_reads`，校验沿用 `STORE_KEYS` 的形状检查（`scope`、`path`、`status`、`source_url`），键集合与 `precedence` 同级。
- 新增 store 键 `approval_required`（Claude 项目级 `.mcp.json`，依据 §11 条 3）：声明「该位置的条目需要逐项目批准」，读取方是影响面。与既有 `trust_required` 并列。
- 影响既有测试：`test_data_files_equal_the_v6_14_0_builtin_snapshot` 的 `comparable()` 需在顶层 `pop("compat_reads")`，在 store 键列表里加入 `approval_required`（与 `precedence`、`sync` 当初加入时同一做法）；这是该测试唯一的改动。`schema_version` 保持 2（可选键只追加）。

### 3.6 `dedupe` 与 `relocate` 的接入点

- `dedupe --retire-others`：在 `_plan_retire` 与 `_plan_mcp` 里，计划成形后调用 `reach.impact`，写入 `plan["impact"]`，再由 `impact` 生成 `warnings`。`keep-depends-on-retired`、`evidence-unknown` 等既有 `blocked` 逻辑不变。
- `relocate`：Skill 与 MCP 计划同样写入 `impact`。`retire_source` 为真时 `retire[]` 为源，`add[]` 为目标；`--keep-source` 时 `retire[]` 为空，只给 `gained` 与「其他机器」提示。`native`（零写入）计划不生成 `impact`。
- `keep-all`、`ignore`、`unignore` 不改任何配置，不生成 `impact`。
- `plan_digest`：`impact` 在计划体内，参与摘要；其中只放代码、路径与摘要，不放本地化文字（`message`/`text` 由 `warnings` 携带，已被摘要排除），摘要与界面语言无关。
- 预览之后文件变化（例如用户把 Claude 的用户级副本补上）使重新计算的 `impact` 改变，摘要不一致，`--apply` 拒绝并要求重新预览，沿用现有协议，不加新机制。

### 3.7 JSON 数据模型

`plan.impact`（键只追加；所有路径用 `~` 与 `<project>` 缩写，不含文件内容）：

```json
{
  "impact": {
    "schema": 1,
    "kind": "skill",
    "name": "pdf-notes",
    "basis": "file",
    "clients": {
      "claude": {
        "detected": true,
        "reads": ["~/.claude/skills", "<project>/.claude/skills"],
        "before": ["skill:pdf-notes@claude:project"],
        "after": [],
        "verdict": "lost",
        "evidence": "file",
        "reasons": ["link-target-retired"],
        "via_before": [{"ref": "skill:pdf-notes@claude:project", "path": "<project>/.claude/skills/pdf-notes",
                        "chain": ["<project>/.agents/skills/pdf-notes"], "resolved": "<project>/skills/pdf-notes"}]
      },
      "codex": {"detected": true, "verdict": "unchanged", "evidence": "file", "reasons": [],
                "before": ["skill:pdf-notes@codex:user#agents", "skill:pdf-notes@codex:project"],
                "after": ["skill:pdf-notes@codex:user#agents"]}
    },
    "lost_clients": ["claude"],
    "uncertain_clients": [],
    "unknown_clients": [],
    "dangling_after": ["<project>/.claude/skills/pdf-notes"],
    "scope_notes": []
  }
}
```

- `verdict` 取值见 §3.4；`reasons[]` 取值：`link-target-retired`、`no-other-copy`、`entry-retired`、`compat-copy-present`、`project-approval-unknown`、`project-trust-unknown`、`evidence-unknown`、`store-undetermined`、`link-chain-too-deep`。
- `scope_notes[]`：`affects-all-projects`（退役或移动的是用户级 MCP 或 Skill，所有项目的该客户端都受影响；项目清单「未提供」，需要 #82 I-8 的 `doctor --machine`）、`other-machines-lose-it`（D-R7）。
- `warnings` 新增码（`message` 随界面语言）：`impact-lost-client`（每个 `lost` 客户端一条，带 `client`、`ref`）、`impact-uncertain`、`impact-dangling-link`、`impact-other-machines`。`plan.status` 不因它们改变（D-R3）。
- 契约文档：`dedupe-contract.md` 与 `relocate-contract.md`（及 `.en.md`）各加一节「影响面」，列出上述键与码。键只追加，两份契约的 `schema_version` 保持 1。

### 3.8 预览文案草稿（中英对照）

文案走 `_()`，原文为中文，`locale/en.json` 给英文。示例（`{0}` 为客户端显示名，`{1}` 为 Skill/MCP，`{2}` 为名字）：

- `影响面：退役后 {0} 将失去 {1}「{2}」` → `Impact: after this change {0} will lose {1} "{2}"`
- `  原因：它的符号链接 {0} 指向将被归档的目录，归档后链接悬空` → `  Reason: its symlink {0} points into a directory that is about to be archived, so the link will dangle`
- `  原因：{0} 读取的位置里没有其他同名副本（读取位置：{1}）` → `  Reason: no other copy of that name exists in the places {0} reads ({1})`
- `影响面：{0} 不受影响（仍可从 {1} 读取）` → `Impact: {0} is not affected (it still reads it from {1})`
- `影响面：{0} 将改为读取内容不同的另一份（{1}）` → `Impact: {0} will read a different copy instead ({1})`
- `影响面：无法确定 {0} 的结果（{1}）；工具只保证文件改动，不保证客户端行为` → `Impact: the result for {0} cannot be determined ({1}); the tool guarantees file changes only, not client behaviour`
- 原因短语：`有副本没有文件级证据` / `a copy has no file-level evidence`；`该客户端的读取位置尚未确定` / `where this client reads is not determined yet`；`兼容读取位置里有同名副本，该客户端是否读取未核实` / `a copy sits in a compatibility location; whether the client reads it is unverified`；`项目级 .mcp.json 需要逐项目批准，批准状态未读取` / `a project-level .mcp.json needs per-project approval; the approval state is not read`；`项目级配置只对受信任项目生效，信任状态未读取` / `project-level configuration only applies to trusted projects; the trust state is not read`；`符号链接层数过多或成环` / `the symlink chain is too deep or loops`。
- `影响面：这些符号链接将悬空：{0}` → `Impact: these symlinks will dangle: {0}`
- `影响面按各客户端读取的文件位置计算；是否真正加载取决于客户端设置，本命令不读取` → `Impact is computed from the file locations each client reads; whether it is actually loaded depends on client settings, which this command does not read`
- `（本机未检测到该客户端，仅按其声明计算）` → `(this client was not detected on this machine; computed from its declaration only)`
- `用户级条目影响所有项目；本机项目清单未提供` → `A user-level entry affects every project; the list of projects on this machine is not provided`
- D-R7：`若该条目随项目仓库同步，迁到用户级后其他机器上的该项目不再带有它；工具不检查它是否已提交` → `If this entry is shared through the project repository, projects on other machines will no longer carry it after the move to user level; the tool does not check whether it is committed`

文本预览示例（节选，对应 §1.2 夹具）：

```
影响面：退役后 Claude Code 将失去技能「pdf-notes」
  原因：它的符号链接 <project>/.claude/skills/pdf-notes 指向将被归档的目录，归档后链接悬空
影响面：Codex 不受影响（仍可从 ~/.agents/skills 读取）
影响面：Cursor 不受影响（仍可从 ~/.agents/skills 读取）
影响面按各客户端读取的文件位置计算；是否真正加载取决于客户端设置，本命令不读取
```

### 3.9 边界

- 同步盘上相对软链的行为**未确认**：影响面只描述本机文件事实，不对同步后的另一台机器下结论；`relocate` 现有的 `sync-folder-demoted` 警告保持原样。
- 企业级、管理员级、子目录向上逐级扫描、`--add-dir` 附加目录不在读取集合内（inventory 契约「未覆盖」条）；影响面在 `basis` 说明里沿用这一声明，不为它们给出判决。
- 同一物理路径出现在多个客户端（Codex 与 Cursor 共读 `~/.agents/skills`）时，`impact` 为每个客户端各给一行，物理归档只发生一次（批量去重见步骤 4）。
- 内置能力（`overridden-by-builtin`）与 `skillOverrides` 不在本步骤（§9）。
- 不读设置键，不执行客户端命令（`cli` 证据不产生）。

### 3.10 测试与夹具（T1）

夹具沿用 `plugins/agent-pack/tests/` 的临时 HOME 构造函数，不提交静态目录。

- 纯函数表驱动（`test_reach.py`，无文件系统）：
  - 退役真实目录使项目软链悬空，Claude 判 `lost`（§1.2 复现）。
  - 只退役软链本身，目标目录与其他软链不受影响。
  - Claude 另有用户级副本时 `unchanged`；Claude 不读 `~/.agents/skills`（读取集合断言）。
  - `compat-copy-present` 判 `uncertain`；悬空或损坏条目判 `unknown`；链深 9 与成环判 `unknown`。
  - 内容摘要不同判 `replaced`；`relocate` 到新客户端判 `gained`。
  - 合成客户端（随机 id，只有 `stores` 声明）得到判决行，模块文本不含客户端 id（`test_client_protocol_guard.py`）。
  - MCP：Claude 退役 `local`、保留 `user` 且指纹相同为 `unchanged`；用户级无其他副本为 `lost`；仅剩 `.mcp.json` 为 `uncertain: project-approval-unknown`；Codex 仅剩项目级为 `uncertain: project-trust-unknown`。
- 计划层（`test_reach_plans.py`）：
  - §1.2 夹具：预览 `impact.lost_clients == ["claude"]`，`warnings` 含 `impact-lost-client`，`status` 仍为 `ready`；`--apply` 的实际文件结果与 6.15.0 相同（改变的是预览信息，不改写入语义）。
  - 预览与应用之间补一份 Claude 用户级副本：重新计算的摘要不同，`--apply` 拒绝且零写入。
  - 摘要与界面语言无关（zh 与 en 相同）。
  - 假令牌放进无关配置：不出现在任何 `impact` 输出里。
- 驱动层：`compat_reads` 的合法与非法声明、访问函数、`approval_required`；快照测试按 §3.5 修改。
- 基线对照（沿用 PR #161「旧运行时取自此前正式 tag」的做法，取 `v6.15.0` 的运行时）：同一夹具下，新版 `dedupe`、`relocate` 预览去掉 `impact`、`impact-*` 警告与 `plan_digest`、`next` 后，与 6.15.0 输出逐键相等；`inventory` 输出整体逐字节相等（T1 不触碰它）。
- 测试登记：`tests/test_i18n_commands.py` 的 PREVIEW 登记无新增命令，不需改。

### 3.11 迁移与兼容

- 既有项目无用户动作；`default_for_existing` 为 `on`（预览多出信息，不改写入）。
- **一处行为差异**：版本升级前后，同一个预览的 `plan_digest` 不同。用旧版预览、升级后用旧摘要 `--apply` 会被拒绝，需要重新预览。拒绝时零写入，属于安全失败。
- 新旧 agent-pack 与菜单的组合：新 agent-pack 加旧菜单，`impact` 随 `warnings` 以通用方式显示（菜单原样展示 plan，`project_ext_actions.py` 头注释），不依赖菜单升级；旧 agent-pack 加新菜单，没有 `impact` 键时不显示（缺键按无处理）。
- 等级：agent-pack minor（新增 `impact`、驱动键）。变更片段在实现 PR 里加。

### 3.12 i18n

约 18 条新增 msgid（§3.8 的句子与原因短语），全部 `_()`；运行 `python3 tests/i18n_catalog.py --write`；`test_i18n_catalog_order.py` 与 `tests/i18n_catalog.py` 必须通过。契约的两份 `.en.md` 同步更新（`tests/i18n-policy.json` 登记 `plugins/*/references` 的配对）。

---

## 4. 步骤 2：MCP 误报（目标服务判定，6.16.0 实现）

### 4.1 输入与输出

- 输入：`observe` 内部的原始 MCP 条目（未脱敏的 `command`、`args`、`url`、`env`、`cwd`），在 `normalize_mcp` 内取得。
- 输出：每个 MCP 条目新增 `target` 与 `target_class`；`duplicates.mcps[]` 新增 `target_relation`；候选组按判决分流；新顶层键 `mcp_hints[]`（仅有内容时出现）。

### 4.2 被排除的做法

- **A. 修正脱敏（长路径参数不再算令牌）。** 排除：改变 `fingerprint` 的取值，进而改变 `g-…` 分组 id（同指纹组的 id 取自指纹）与 `ext-decisions.json` 里按组 id 记录的决定，使已有的「保留」与「忽略」变为过期；并且只覆盖 M1、M2，M3、M4 仍在。附带的 `has_inline_secret` 误报（M1）可作为另议的修正。
- **B. 把 `env` 值与 `cwd` 的摘要并入指纹。** 排除：同样改变指纹取值；对 `env` 值取摘要等于输出可被离线猜测的低熵口令的哈希。
- **C（采用）：指纹不变，进程内比较目标。** 原值只在 `normalize_mcp` 与 `observe` 内存里比较，输出只有类别名、序号与可展示的标签。

### 4.3 目标服务的识别

`target = {kind, label, basis[]}`，`label` 只含可展示内容；形似令牌的值显示为 `<redacted>`，不进标签。

- `remote`（`url` 条目与 `command` 为空的条目）：标识为规范化后的 URL，规则类比 Claude Code 官方对端点相同的定义：scheme 与 host 的大小写、默认端口（文档写 `https` 的 `:443`，`http` 的 `:80` 为本文按同理补充）、末尾斜杠不算差异；路径、查询字符串、userinfo、非默认端口算差异（[MCP 文档](https://code.claude.com/docs/en/mcp)，2026-11-24 读取）。该规则在文档里用于插件与连接器的重复判定，对用户自己配置的服务按名字匹配，所以这里是类比采用，不是文档对用户级条目的规定。标签只显示 `host + path`，不含查询。凭据形状的查询值与 userinfo 不参与比较（掩码）。
- `package`（`npx`、`bunx`、`uvx`、`pipx run`、`pnpm dlx`、`yarn dlx` 等启动器）：跳过启动器自身的选项（`-y`、`--yes` 等，消耗下一个值的 `-p`、`--package`、`--from` 按表处理），第一个位置参数是包规格，标识为「规范化的包名 + 版本规格」。标签如 `@scope/pkg@1.2.3`。
- `script`（`python`、`python3`、`node`、`bash`、`sh`、`ruby`、`bun`、`deno` 等解释器）：第一个位置参数的路径；`-m <模块>` 记为 `module`；`-c`、`-e`、`--eval` 与带内联代码的形式记为 `opaque`。路径用 `~` 与 `<project>` 缩写，不展开软链。
- `binary`：`command` 本身不是上述启动器或解释器时，标识为 `command` 的缩写路径。
- `opaque`：解析有歧义（启动器选项表没有覆盖的带值选项、`docker run` 这类选项多的形式）一律记 `opaque`，不猜。选项表是模块内的数据表（`LAUNCHERS`），表外的启动器按 `binary` 处理。

比较的字段与判据，对同客户端内同指纹的候选集合：

1. **目标**：`kind` 与标识相同。
2. **服务参数**：目标之后的其余 `args`，规范化 `${VAR:-默认}` 为 `${VAR}` 后逐项相等，顺序敏感。
3. **环境**：`env` 的值，对键名命中密钥规则的值掩码，其余逐键比较（键集合相同、值相同）。
4. **`cwd`**：原值相等。
5. **凭据位置**：命中强密钥形状（`SECRET_VALUE_RE`、紧随密钥名选项的值、密钥名键的 `env` 值与 `headers`）的位置整体掩码；掩码位置的原值不同时，该候选记 `credential-differs`，关系为 `unknown`（同一服务的不同账号与不同服务无法区分）。

形似令牌但不命中强密钥形状的值（M1、M2 的长路径与服务标识）按原值参与 1–2 项比较，标签显示 `<redacted>`。

关系：
- `same`：全部候选在同一个等价类，没有 `opaque`，没有 `credential-differs`。
- `different`：存在多个等价类，且差异落在 1–4 项的非掩码字段；`differs_in[]` 记字段类别（`target`、`args`、`env`、`cwd`、`url`）。
- `unknown`：存在 `opaque`、`credential-differs` 或读取失败；`reasons[]` 记 `opaque-launcher`、`credential-differs`、`unreadable`。

### 4.4 数据模型

条目新键（每个有指纹的 MCP 条目都带；无指纹条目为 `null`）：

```json
{"target": {"kind": "script", "label": "<redacted>", "basis": ["command", "args[0]"]},
 "target_class": "t-3fa81c2d.1"}
```

- `target_class`：`t-<指纹前 8 位>.<序号>`；同一份输出内取值相同的条目目标等价。序号按（客户端、作用域、存储、名字）的既有排序依次分配，同一输入得到同一序号；只在本份输出内有意义，不跨运行比较，不入决定文件。
- `duplicates.mcps[]`：`kind: same-fingerprint` 与 `same-name` 的组各加 `target_relation`（`same|different|unknown`）。
  - `same-fingerprint` 组：`same` 留在 `duplicates.mcps[]`；`different` 与 `unknown` 移入 `mcp_hints[]`，不再是重复组。
  - `same-name` 组：不移动，也不改 `relation` 的取值；`relation: identical` 而 `target_relation` 为 `different` 或 `unknown` 时，`dedupe` 计划的 `warnings` 追加 `fingerprint-collision`。
- `mcp_hints[]`（顶层，仅在非空时出现，追加在末尾）：

```json
{"kind": "same-fingerprint-different-target", "id": "g-…", "client": "cursor", "names": ["svc-a", "svc-b"],
 "stores": ["~/.cursor/mcp.json"], "fingerprint": "…", "target_relation": "different",
 "differs_in": ["target"], "reasons": [], "refs": ["mcp:svc-a@cursor:user", "mcp:svc-b@cursor:user"]}
```

  - `kind` 取 `same-fingerprint-different-target` 或 `same-fingerprint-target-unconfirmed`。
  - `id` 与原重复组同一公式（`g-` 加 `mcp|客户端|指纹` 的 SHA-256 前 10 位），所以用旧 id 调用 `dedupe` 时返回 `blocked: not-a-duplicate-group`，并带上该提示的原因，不再是 `group-not-found`。
- `inventory --output text` 增加一节「MCP 提示（不算重复）」，每条一行（§4.6）。

### 4.5 与 D-CB4 的关系

D-CB4（`agent_pack_dedupe.py:110-111`）：同指纹跨客户端默认建议保留，不提供退役。现行守卫按指纹比较，也会在目标不同时误报。改为按 `target_class` 比较：

- 其他客户端里有同 `target_class` 的条目：保持警告 `cross-client-keep-recommended`，行为与 6.15.0 一致。
- 其他客户端里只有同指纹但不同 `target_class`：不发警告。
- 其他客户端里同指纹且目标关系为 `unknown`：发 `cross-client-target-unconfirmed`（不据此建议保留）。
- 本身仍然不为跨客户端组提供退役（`list_groups` 以客户端为界，跨客户端不成组，`test_dedupe_mcp.py:137` 的断言保持）。

### 4.6 文案草稿（中英对照）

- `MCP 提示（不算重复）` → `MCP hints (not duplicates)`
- `  {0} 与 {1}：指纹相同，目标不同（差异：{2}）` → `  {0} and {1}: same fingerprint, different targets (differs in: {2})`
- `  {0} 与 {1}：指纹相同，无法确认目标是否相同（{2}）` → `  {0} and {1}: same fingerprint, the target cannot be confirmed (reason: {2})`
- 字段类别：`目标` / `target`、`参数` / `arguments`、`环境变量` / `environment`、`工作目录` / `working directory`、`地址` / `URL`、`凭据位置` / `credential positions`。
- `这个分组不是重复：指纹相同但目标不同或无法确认（见 inventory 的 mcp_hints）` → `This is not a duplicate group: the fingerprints match but the targets differ or cannot be confirmed (see mcp_hints in inventory)`
- `指纹相同但内容可能不是同一个服务：先确认两份的目标再处置` → `The fingerprints match but these may not be the same service; confirm the targets before acting`
- `其他客户端里有同指纹的条目，但无法确认目标相同：不据此建议保留` → `Another client has an entry with the same fingerprint, but the target cannot be confirmed: this is not a reason to keep it`

### 4.7 边界

- 指纹值与既有 `ext-decisions.json` 不变；已记录的 `keep-all` / `ignore` 按组 id 继续生效。被移入 `mcp_hints[]` 的组，其决定记录留在文件里（`--list` 不再列出它，决定文件不会被改写），之后用户用旧 id 调 `--unignore` 得到 `not-a-duplicate-group`；决定文件的清理不在本版。
- 不比较 `headers` 值与令牌存储；`headers` 键名不进目标判据。
- OAuth、`mcp-remote` 等传输形态只在步骤 5 处理；本步骤只保证 `mcp-remote` 条目被识别为 `package` 目标，不据此合并「原生 HTTP」与「桥接 stdio」。
- 启动器选项表覆盖不全属于已知局限：表外形式一律 `opaque` 或 `binary`，向「降级为提示」方向失败，不向「误判为重复」方向失败。

### 4.8 测试与夹具（T2）

- 机制复现（每个都断言：旧版成组、新版不成组并进入 `mcp_hints[]`）：M1 两个长于 64 字符的不同路径；M2 两个形似令牌的不同服务标识；M3 同命令、`env.TARGET` 不同；M4 Codex TOML 里 `cwd` 不同。
- 仍然是重复（`target_relation: same`，组保留）：同目标同参数不同名；`${VAR}` 引用相同；URL 仅大小写、默认端口、末尾斜杠不同；URL 路径不同则 `different`；非默认端口不同则 `different`。
- `credential-differs`：同命令，仅 `--api-key` 的值不同 → `unknown`，进入 `mcp_hints[]`，输出里不含两个值。
- 回归：`test_same_fingerprint_under_different_names`（`test_inventory_observe.py:406`）的夹具里 `b` 带 `env.K = "v"` 而 `a` 没有，按本判据变为 `different`。该断言是唯一需要修改的既有断言：夹具改为 `b` 不带 `env`，另加 `env` 不同的新用例（预期不成组）。
- `test_json_keys_are_stable`（`:443`）：`NEW_MCP_KEYS` 加入 `target`、`target_class`；顶层键列表断言覆盖「无提示时不出现 `mcp_hints`」与「有提示时出现在末尾」两种。
- D-CB4：同目标跨客户端仍发警告；目标不同不发；目标未知发 `cross-client-target-unconfirmed`。
- 密钥：把假令牌放进 `args`、`env`、`url` 查询与 `headers`，断言整份 JSON 与文本输出里不出现它们，`target.label` 只含白名单内容。
- 基线对照：对没有碰撞的夹具，新版 `inventory` 输出去掉 `target`、`target_class` 后与 `v6.15.0` 的输出逐字节相等；`dedupe --list` 逐字节相等。
- 旧 id：`dedupe <旧同指纹组 id>` 返回 `blocked: not-a-duplicate-group`。
- 菜单（T3）：缺 `target_class` 的 inventory（旧 agent-pack）时 `diff-name-same-fp` 保持旧行为；有该键时只在 `target_class` 相同才成组；`mcp_hints[]` 显示为一行信息，不进重复计数。

### 4.9 迁移与兼容

- **`schema_version` 保持 1。** 依据：`inventory-contract.md` 的兼容约定（「新增键不升 `schema_version`；删除键、改变键含义或取值集合的收窄才升主版本」）；`duplicates.mcps[]` 的键含义不变，只是少列了此前错误成组的项。该契约已声明「判重结论对含内联凭据的条目只作提示」，本步骤把这一立场扩展到所有被脱敏掩盖了差异的条目。
- 升版的代价：菜单对未知主版本降级为「不可用」（`project_extensions.py:121-126`），旧菜单会因此整体停用；因此不升版（D-R2）。
- **有意的输出差异（须在变更说明里写明）**：`duplicates.mcps[]` 与 `dedupe --list` 对同指纹不同目标的条目不再成组；`inventory` 条目多两个键；顶层可能多 `mcp_hints`；`cross-client-keep-recommended` 只在目标相同时出现。
- 旧菜单加新 agent-pack：菜单仍按 `fingerprint` 自己分组（`build_groups`），误报在菜单里保留到菜单升级，不会崩溃。新菜单加旧 agent-pack：缺 `target_class`，保持旧行为。
- 等级：agent-pack minor；project-orchestrator（T3）patch，`default_for_existing` 为 `on`（减少误报）。

### 4.10 i18n

约 10 条新增 msgid（§4.6）；契约 `.md` 与 `.en.md` 成对更新；`inventory --output text` 的新一节随界面语言。

---

## 5. 两个实现任务的拆分

- **T1 影响面（agent-pack）**
  - 新增：`runtime/agent_pack_reach.py`、`tests/test_reach.py`、`tests/test_reach_plans.py`。
  - 修改：`runtime/agent_pack_dedupe.py`（`_plan_retire`、`_plan_mcp`、文本渲染）、`runtime/agent_pack_relocate.py`、`runtime/agent_pack_mcp_move.py`（计划成形后调用）、`runtime/agent_pack_clients.py`（`STORE_KEYS`、`compat_reads` 校验与访问函数）、`runtime/drivers/cursor.json`、`tests/test_client_drivers.py`（快照 `comparable()`）、`runtime/locale/en.json`、`references/dedupe-contract.md`、`relocate-contract.md` 及各自 `.en.md`、变更片段。
- **T2 目标判定（agent-pack）**
  - 新增：`runtime/agent_pack_mcp_target.py`（目标识别与关系）、`tests/test_mcp_target.py`。
  - 修改：`runtime/agent_pack_inventory.py`（`normalize_mcp` 的私有比较键、`mcp_duplicates`、`observe`、文本渲染）、`runtime/agent_pack_dedupe.py`（`list_groups`、`cross_client_hint`、`not-a-duplicate-group`）、`tests/test_inventory_observe.py`（§4.8 的两处）、`runtime/locale/en.json`、`references/inventory-contract.md`、`dedupe-contract.md` 及 `.en.md`、变更片段。
- **T3 菜单消费（project-orchestrator）**：`runtime/project_extensions.py`（`build_groups` 读 `target_class`、`mcp_hints` 一行信息）、`runtime/project_extensions_demo.py` 夹具、`tests/test_project_extensions_menu.py`、`runtime/locale/en.json`、变更片段（patch）。
- **并行性**：T1 与 T2 可并行，T3 对夹具并行、合并顺序在 T2 之后。
- **冲突点与规避**
  - `agent_pack_dedupe.py`：T1 改 `_plan_*`，T2 改 `list_groups` 与 `cross_client_hint`，函数不同，文本冲突概率低。
  - 两份契约文档：T1 追加「影响面」节，T2 追加「目标判定」节与 inventory 契约的键说明，各自新增小节而不改既有段落。
  - `locale/en.json`：已按键排序，并行新增不同键通常不冲突；冲突时保留两边并运行 `python3 tests/i18n_catalog.py --write`。
  - 测试：两者各用新测试文件；仅 T2 改 `test_inventory_observe.py`，仅 T1 改 `test_client_drivers.py`。
- **共同守卫**：不改任何 `SKILL.md` 与 `launcher.sh`；新模块不含客户端 id 常量（`test_client_protocol_guard.py` 的 `CAPS` 保持空表，`test_synthetic_client_guard.py` 通过）；`tests/test_i18n_commands.py` 无新命令登记。
- **体量估算**（无逐行统计，仅量级）：T1 新模块约 250–350 行加测试；T2 新模块约 200–300 行加测试；T3 约 30 行。

---

## 6. 步骤 3：推荐引擎（下一版及以后，只设计）

### 6.1 输入、输出、数据模型

- 输入：`dedupe` 的重复组（含成员、关系、证据）、步骤 1 的 `impact`、步骤 2 的 `target_class`、既有决定（`ext-decisions.json`）。
- 输出：`dedupe --list` 与预览里每组追加 `recommendation`：
  `{"disposition": "keep-one|keep-all|ignore|none", "keep": "<ref>|null", "rule": "R4", "reasons": ["…"], "impact": {…摘要…}, "candidates": [{"keep": "<ref>", "lost_clients": [], "rejected": "…|null"}], "basis": "file"}`。
- `candidates[]` 穷举每个「保留谁」的选项及其影响面，被排除的选项带 `rejected` 码，用来回答「为什么没有推荐另一个」。

### 6.2 规则（机械、可复现；按顺序，首条命中即停）

- **R1 已有决定**：存在未过期的 `keep-all` 或 `ignore` → `none`，原因 `already-decided`。
- **R2 跨客户端同目标 MCP**（D-CB4）：→ `keep-all`，原因 `cross-client-same-target`。
- **R3 证据不足**：任一成员 `evidence != file`，或影响面出现 `unknown` → `none`，原因 `evidence-unknown`。
- **R4 同名 Skill 内容相同（`relation: identical`）**：穷举「保留某一份、退役其余」。取影响面 `lost_clients` 与 `uncertain_clients` 都为空的选项；有多个时按固定次序取：先选「退役后受影响项目最少」的一方（退役项目级优先于退役用户级，因为用户级影响所有项目，`scope_notes: affects-all-projects`），仍并列时按 ref 字典序。没有任何选项满足 → `keep-all`，原因 `every-option-loses-a-client`。
- **R5 同名内容不同（漂移）**：→ `none`，原因 `content-differs`。不推荐「较新者」（CB §5.2：无可靠版本信号）。
- **R6 同客户端同目标不同名的 MCP**：→ `none`，原因 `choose-one`，列出候选。名字不能机械选择：`mcp__<名>__` 形式的工具名可能被权限与规则引用，引用关系工具读不到。
- **R7 受管来源**：成员来自插件或 `state-record` 时，保留受管的一份，退役未受管的一份（受管副本下次同步会重新出现，`managed-origin` 警告既有）。

### 6.3 与 CB「不做质量打分」及 #82「不给可删除结论」的关系

- 本引擎没有数值评分，没有跨 Skill 的排名，没有「建议删除」类措辞；输出是「按规则 R<n>，建议保留 X，归档其余（可手工移回）」，每条带规则编号。这与 CB §5.5 的「不输出评分、不排名、不输出『建议删除』」按字面不冲突，因为该条约束的是 Skill 质量判断；处置建议回答的是冗余副本的收敛。
- 冲突点：#82 §4 写「只有当全部副本的 `loaded` 都已确定时，才输出『可清理』候选」，而 `loaded` 在 inventory 里恒为 `unknown`。按字面执行，引擎永远不会推荐 `retire`，#184 要解决的 30 组仍要逐组手工处理。D-R4 列出两种取法。

### 6.4 可机械获得的前提标签（#184 第 5 条）

可机械判定（来自文件事实，输出为事实而不是评分）：

- 许可证：frontmatter 的 `license` 字段（Claude Code Skills 文档的 frontmatter 表列有 `license`，2026-11-24 读取）。没有则「未提供」。
- 环境要求声明：`compatibility` 字段（同一张表）。
- 自动执行的 hook：frontmatter 含 `hooks`（文档：skill 被调用时注册并在会话内持续运行）。需要确认 `parse_frontmatter` 对嵌套键只返回键存在性，T 实施前核对。
- 预授权工具：`allowed-tools`、`disable-model-invocation`、`paths`（激活条件）。
- 运行依赖线索（只给「出现了」，不给「需要」）：目录里存在 `package.json`、`*.js|*.mjs`（Node 线索）、`requirements.txt`、`pyproject.toml`、`*.py`（Python 线索）；文件文本里出现已知字体与 CDN 主机名（联网线索，主机清单在代码里列明）；出现 `curl|wget … | sh`、`chmod +x` 的形式（下载并执行线索）。这些是模式匹配，措辞固定为「检测到 X 的线索」。
- 体积与成本：沿用 inventory 既有键。

不能机械判定（输出为「未提供」，不猜）：

- 适用范围（仅某个项目还是通用）、适用载体（邮件、浏览器页面、应用界面、PPT）、适用模式、是否处理中文（`description` 的文字类别只能说明描述的语言，不能说明能力）、风格倾向与是否与项目规则冲突、功能是否真的重叠（只有 `overlap_hints` 的词元相似度）。这些属于领域判断，交回维护者。
- 观察：`overlap_hints` 的阈值在契约里是 0.8（至少 5 个词元），菜单里是 0.6（`SIMILARITY_THRESHOLD`，CB D-CB8）；两处不一致，是否统一见 D-R9。

词汇对齐：若将来引入目录（#72），字段名沿用 #82 §7 的 `requires_cli`、`auth`、`license`，不另起一套。

### 6.5 边界、测试、迁移、i18n、任务

- 边界：推荐只读，不写任何文件；用户可不采纳；`keep-all` 与 `ignore` 仍走既有命令。
- 测试：每条规则一组夹具，断言规则编号、候选穷举与 `rejected` 码；`unknown` 证据不出现 `retire`；输出里没有数值评分。
- 迁移：新增键；`default_for_existing: advisory`。
- i18n：约 25 条（规则原因与说明）。
- 任务：T4 规则引擎（agent-pack，新模块，读取 T1、T2 的输出）；T5 前提标签读取（inventory 的新键，或菜单只读层，CB-5 同口径）。

---

## 7. 步骤 4：批量预览与批量应用（下一版及以后，只设计）

### 7.1 输入、输出

- 输入：选择器 `--batch --client <客户端> --kind skill|mcp [--recommended | --group <id>…]`；每个成员组的计划由既有 `build_plan` 产生。
- 输出：`batch` 计划：`{members[{group, plan_digest, status}], steps[], blocked[], warnings[], impact[], covers_groups{物理路径: [组id…]}, batch_digest, next.apply}`。

### 7.2 批量计划指纹

`batch_digest = SHA-256(canonical({selector, members: 按组 id 排序的 [group, plan_digest]}))`。成员的 `plan_digest` 已含 `impact`，所以批量摘要自动绑定每组的影响面。`--apply --expect-plan <batch_digest>`：重新计算全部成员计划，任何一个变化，整批拒绝，零写入。

### 7.3 物理路径去重

Codex 与 Cursor 共读 `~/.agents/skills`，同一份 Skill 在两个客户端各有一组；退役 `<项目>/.agents/skills/x` 在两组里是同一次物理归档（§3.3）。批量计划按 `physical(path)` 合并步骤，`covers_groups` 列出该次归档对应的全部组。不合并会让第二个组在执行时发现源已被移走而失败。

### 7.4 部分失败与回滚

- 全部成员在同一个恢复目录（`<时间戳>-dedupe-batch-<digest 前 8 位>`）里执行；任一步失败，逆序回滚全部已完成步骤，`applied.status: rolled-back`，保留恢复目录。
- 备选「逐组尽力而为并报告」被排除：恢复目录与摘要的语义以「整批要么全成要么全回」为前提，部分成功会让预览与实际不一致（违背 CB §3.3 的「预览等于应用」）。
- MCP 受管退役：整批只对 `~/.claude.json` 做一次逐字节备份与一次原子改写，失败按字节还原（沿用 `dedupe-contract.md`）。
- 批量大小上限：建议 100 组，超出拒绝并要求分批（D-R10）。

### 7.5 回滚之外的恢复

沿用现状：没有 `restore` 命令（CB D-CB7 未决），结果给出每个归档的单行 `mv`。批量时按物理路径给出。

### 7.6 菜单与命令

`agenthelm dedupe --batch …` 与 `agenthelm project extensions dedupe --batch …`；菜单第 9 屏增加「按客户端批量处理」入口，进入前先显示合并后的影响面。

### 7.7 测试、迁移、i18n、任务

- 测试：30 组夹具（15 个 Skill × 2 个客户端）一次预览一次应用；中途失败整体回滚；批量摘要对成员顺序不敏感；物理路径去重；影响面为 `lost` 的成员出现在批量警告里。
- 迁移：新增命令形态，默认预览，`default_for_existing: off`；`docs/write-commands.md` 与 `tests/test_i18n_commands.py` 的 PREVIEW 登记补上批量形态。
- 任务：T6 批量计划（agent-pack，复用 T1 与 T4）；T7 菜单入口（project-orchestrator，改 `project_menu.py`、`project_ext_actions.py`，与 CB-7 同文件串行）。

---

## 8. 步骤 5：传输方式（下一版及以后，只设计）

### 8.1 输入与数据模型

- 同一服务的两种形态：原生 HTTP（可含 OAuth）与本机 stdio 经 `mcp-remote` 桥接。`mcp-remote` 的用法见其 README（`main` 分支，2026-11-24 读取）：`command: "npx"`，`args` 以 `mcp-remote` 起头、后跟服务 URL，凭据缓存在 `~/.mcp-auth`（可由 `MCP_REMOTE_CONFIG_DIR` 改变），不同 URL、资源、自定义头与授权参数的组合各有独立的令牌缓存。
- 数据：在步骤 2 的 `target` 上加桥接识别：`target.kind: "remote"`、`bridge: "mcp-remote"`；`npx … mcp-remote <URL>` 的目标标识为该 URL，与原生 HTTP 条目的 URL 按 §4.3 的端点规则比较，得到新的分组种类 `same-target-different-transport`。它与同名、同指纹的重复组并列，不并入「重复」，而是展示两种形态各自在哪些客户端可用。

### 8.2 推荐与说明

- 步骤 3 的规则增加 `R8 传输形态`：同一目标的原生与桥接两份并存时，只列出事实（各自的客户端、证据、已知的认证位置），不推荐「哪种稳定」。评论里的现象（超时、状态不一致、刷新锁）来自维护者本机日志与客户端 2.1.330，是否缺陷**未确认**，不写入推荐理由。
- 认证状态属于 #69（`cli` 证据，`claude mcp list` 等），当前未实现（§9）。在它落地前，推荐只写「认证状态未提供」。

### 8.3 `relocate` 的改传输预览

- 形态：`relocate <mcp ref> --to claude:user --transport stdio-bridge|http`。预览列出：新条目的命令与参数（`npx -y mcp-remote <URL>`，URL 取自原条目）、令牌存放位置（`~/.mcp-auth` 或 `MCP_REMOTE_CONFIG_DIR`，出处为 `mcp-remote` README）、「首次启动需要在浏览器完成授权」、「迁到用户级后，其他机器上的项目会失去它，需各自再配一次」（与 §3 的 `scope_notes: other-machines-lose-it` 同一条）。
- 不读不写令牌；含内联凭据的条目沿用 `inline-secret-in-project-file` 阻止。
- 写入沿用 `agent_pack_mcp_move.py` 的受管与未受管区分。`mcp-remote` 的版本固定方式（`@latest` 或具体版本）**未核实**：README 说明加 `@latest` 可强制检查更新，是否应固定版本是安全取舍，列为 D-R8。

### 8.4 `check` 的不一致说明

`capability_notice` 现在只列客户端名（`agenthelm.py:1478-1482`），而 `client_drift` 返回的 `missing / different / stale` 已有具体条目。改为在提示后追加最多 5 项「`mcp <名>`：缺失 / 内容不同 / 多余」，其余折叠为「另有 N 项」；不显示任何值。属于只读文案改动，等级 patch。

### 8.5 测试、迁移、i18n、任务

- 测试：`mcp-remote` 条目识别为 `remote` 目标；原生与桥接同 URL 成 `same-target-different-transport`；URL 路径不同不成组；预览含新命令与令牌位置；`check` 提示逐项列出。
- 迁移：新增形态默认关闭；`default_for_existing: off`；`check` 文案 patch。
- 任务：T8 桥接识别与分组（agent-pack，依赖 T2）；T9 `relocate --transport`（agent-pack，依赖 T8 与 T1 的 `scope_notes`）；T10 `check` 具体项（agent-pack，独立，可随时做）。

---

## 9. 重叠分析：哪些相关项并入 6.16.0

维护者的口径：「如果有相关功能就一起变更；功能比较独立就排除」。判据：是否与步骤 1、2 在同一批代码、同一批数据上。结论默认排除，只有同一函数与同一份数据才并入。已读到的状态：#82 的 I-4（作用域声明）已发布（`agent_pack_config.py` 有 `mcp_scope`、`skill_scope`）；`readiness`、`--probe`、`catalog` 在 agent-pack 里没有实现。

- **#69「已安装与已启用」**（`loaded`、设置键）：**独立，排除。** 数据不同：它要读 `skillOverrides`、`[[skills.config]]`、`enabledMcpjsonServers` 等设置键，产生 `setting` 证据，inventory 契约明确保留给后续版本；步骤 1 只用文件级可达性。代码不同：它改 inventory 的观测层，步骤 1 改 dedupe 与 relocate 的计划层。影响面的输出已固定带 `basis: "file"` 与「是否加载取决于设置」的说明，将来该项落地时，只需把 `setting` 证据作为新的输入传给 `reach.impact`，函数签名不变。
- **#69「客户端内置能力」**（`overridden-by-builtin` 表）：**独立，排除。** 它需要一份带出处的内置能力数据表，与目标判定和可达性没有共用数据。
- **#69「OAuth 认证状态」**：**独立，排除。** 需要 `cli` 证据（`claude mcp list`），与 #73 的 `--probe` 同一条代码路径；步骤 1、2 都不执行客户端命令。步骤 5 才会依赖它。
- **#73 就绪检查**（`doctor` 的 `readiness`、`requires_cli`、`auth`）：**独立，排除。** 改 `doctor` 与 Profile 声明，没有任何一行与 `dedupe`、`relocate`、`inventory` 的这两处改动重合；#82 I-3 的前置是 I-1 观测层，与本版也无依赖。
- **#72 推荐目录元数据**：**独立，排除。** 目录是一份新的数据与命令族（`catalog list|show|install`），6.16.0 没有任何一处读取它。与步骤 3 的前提标签有两处词汇重叠（`requires_cli`、`auth`、`license`），已在 §6.4 要求字段名一致，实现分开。
- **#67 共用密钥**（`user:<名称>`、按引用注入）：**独立，排除。** 改 `credential_env_file` 与 `mcp_env_launcher.py`，与比较 MCP 目标无共用代码；步骤 2 也不读 `credential_env_file`。
- **#64 的设计稿残余**（`progressive-loading.md` 的 I2、S1–S3；`opinion-progressive-loading.md`）：**独立，排除。** 它们改 `memory.py check` 的启动层预算提示与 opinion 的加载，数据是项目启动文件与规则，与 Skill、MCP 清单无关。`SKILL.md` 净增长预算只剩 2 字节，本版不改任何 `SKILL.md`，也不依赖它们的瘦身。
- **#65 的设计稿残余**（`case-state-authority.md` 的 S1–S3、I2）：**独立，排除。** 改 project-orchestrator 的 `health` 与 Case 状态权威检查，与 Skill、MCP 清单无关。
- **附带并入的一处（不属于上面任何 issue）**：`relocate` 项目级到用户级时的「其他机器上的该项目不再带有它」提示（#184 评论第 4 条）。理由：它由步骤 1 同一个 `impact` 对象的 `scope_notes` 表达，不需要新数据；追加工作量约 30 行与 2 条文案。列为 D-R7，由维护者决定是否并入。
- **不并入的相关项**：#82 I-8 的 `doctor --machine`（跨项目清单）。步骤 1 的 `affects-all-projects` 提示需要它才能列出项目名，6.16.0 只给不带项目名的静态提示；并入需要新的只读命令，工作量与风险都超出本版。

---

## 10. 决策项（需维护者拍板）

「倾向」是我的建议，「替代」是被排除的备选与理由。我擅长的是读代码、复现、结构化比较；以下几项涉及产品取向与风险偏好，最终由维护者决定。

1. **D-R1 步骤 1 的范围：是否把 MCP 的影响面一起做进 6.16.0。** 倾向：Skill 与 MCP 都做（同一个 `impact`，MCP 部分约占 T1 的三分之一，且 #184 第 2 条的「迁到用户级」也涉及 MCP）。替代：6.16.0 只做 Skill，MCP 推迟一版；排除理由：MCP 的 `.mcp.json` 批准与 Codex 受信任项目的降级路径正是影响面最容易误判的地方，与 Skill 同批设计成本最低。
2. **D-R2 `schema_version` 是否升到 2。** 倾向：保持 1（§4.9）。替代：升到 2，让「`duplicates.mcps[]` 的成员口径变化」显式化；排除理由：菜单对未知主版本降级为「不可用」，旧菜单会整体停用，换来的只是一次口径变化的标记。
3. **D-R3 影响面里的 `lost` 是警告还是阻止。** 倾向：6.16.0 仅 `warnings`，`status` 保持 `ready`。替代 a：`confirmations[]` 加 `--confirm-impact`，未带标志则 `--apply` 拒绝；排除理由：菜单转发的标志是固定的三个（`project_ext_actions.py:108`），版本错位时新 agent-pack 加旧菜单会让所有含 `lost` 的应用都被拒绝，且用户在菜单里无法补标志，等菜单同步升级后再收紧更稳。替代 b：直接 `blocked`；排除理由：会拦下用户有意让某客户端失去它的处置，且 `unknown` 情形无法区分。后续版本可在菜单与 agent-pack 同时支持后改为 a。
4. **D-R4 推荐 `retire` 是否要求 `loaded` 已确定。** 倾向：不要求，推荐以文件级事实（证据 `file`、关系 `identical`、影响面无 `lost` 与 `uncertain`）为条件，措辞限定为「文件级」并声明未读取 `loaded`。替代：要求 `loaded` 已确定（#82 §4 的字面规则）；排除理由：`loaded` 恒为 `unknown`，会让引擎永不推荐退役，#184 第 1 条无法达成；若维护者坚持字面规则，需要先落地 #69 的设置键读取。
5. **D-R5 `compat_reads` 是否作为驱动声明键。** 倾向：作为顶层可选键，只被影响面读取。替代 a：写成 `stores.skills[]` 的 `status: documented` 条目；排除理由：`stores()` 会把它们交给 inventory 扫描，改变既有输出，与契约「Cursor 的兼容加载未建模（有意）」冲突，并破坏驱动快照测试。替代 b：在模块里写常量表；排除理由：违反 #134「声明即数据，代码不含客户端知识」的守卫。
6. **D-R6 步骤 2 的 `env` 值是否参与目标判定。** 倾向：参与（键名命中密钥规则的值掩码，其余比较）。替代：只比较 `command`、`args`、`url`、`cwd`，`env` 仅作提示；排除理由：M3 是维护者描述的「目标不同」的合理成因之一，漏掉它等于只修一半误报；代价是会让既有测试里「`b` 多一个 `env.K`」的组被降级，已列入 §4.8。
7. **D-R7 「其他机器上的项目会失去它」提示是否并入 6.16.0。** 倾向：并入（§9 末条）。替代：留到步骤 5；排除理由：它由步骤 1 的同一对象表达，单独留到下一版会让同一份预览在两个版本里分两次改文案。
8. **D-R8 `mcp-remote` 的版本固定方式（步骤 5）。** 倾向：预览默认生成带具体版本的 `npx -y mcp-remote@<版本> <URL>`，并提示用户确认版本。替代：`@latest`（README 提到可强制检查更新）；排除理由：每次启动由客户端拉取最新包，等于把一个会读写凭据缓存的程序交给未固定的上游，与 #82 §7 的 `pinned-launch` 倾向冲突。具体版本号的选取属于产品判断，交回维护者。
9. **D-R9 `overlap_hints` 阈值统一。** 倾向：契约（0.8、至少 5 个词元）与菜单（0.6）按用途分开保留，并在契约里写明菜单另行取值。替代：统一为一个值；排除理由：两者阈值没有外部依据（CB D-CB8），统一哪个都没有依据，分开记录至少如实。该项与 #184 的步骤 1、2 无关，仅记录观察。
10. **D-R10 批量的大小上限与失败策略（步骤 4）。** 倾向：单批至多 100 组，整批要么全成要么全回。替代：不设上限；排除理由：回滚要逆序撤销全部步骤，超大批量的恢复目录与回滚时间无法预估，上限数值本身没有依据，属于风险偏好。

---

## 11. 未核实清单

只列没有亲自读到源码或官方文档的内容，或只部分核对的内容。

1. **Claude Code 不读取 `~/.agents/skills`。** 依据：驱动 `claude.json` 的 `stores.skills` 只有 `~/.claude/skills` 与 `<项目>/.claude/skills`；官方 Skills 文档的位置表（企业、个人、项目、嵌套、附加目录、插件、claude.ai 账号）没有列出 `.agents/skills`，页面全文也没有 `.agents` 字样（<https://code.claude.com/docs/en/skills>，2026-11-24 读取，MCP 页同）。这是「文档未列出」的推断，**未实测**；#184 作者的陈述与之一致。Claude Code 对符号链接的支持（「`<skill-name>` 条目可以是指向其他位置目录的软链，读取目标的 `SKILL.md`」）读自同一页。
2. **Cursor 的兼容读取位置**（`.claude/skills`、`.codex/skills`、`~/.claude/skills`、`~/.codex/skills`）：沿用 #82 §2.3 在 2026-11-11 的记录。本会话的网络策略拒绝访问 `cursor.com`，**未能重读**，所以 `compat_reads` 的 `source_url` 与措辞以实施时重读为准。Cursor 同名优先级官方文档未说明（#82 §13 条 6）。
3. **Claude Code 项目级 `.mcp.json` 需要逐项目批准**：读自 MCP 文档（「prompts for approval in interactive sessions before using project-scoped servers from `.mcp.json`」，2026-11-24）。批准选择落在哪个设置文件仍未确认（#82 §13 条 2）。local 作用域是否弹批准未确认（#82 §13 条 1）。
4. **Codex 的读取位置与受信任项目**：驱动里 `~/.codex/skills`、`~/.agents/skills`、`<项目>/.agents/skills` 与项目级 MCP 的 `trust_required` 来自 inventory 契约（2026-11-18 读取）。本会话无法访问 `learn.chatgpt.com`，**未重读**；`cwd`、`env_vars` 等键的 TOML 形态来自 #82 §2.3，未重读。
5. **Agent Skills 规范**（`agentskills.io/specification`）：本会话无法访问，`license`、`compatibility` 作为规范字段的说法只由 Claude Code Skills 文档的 frontmatter 表间接得到（该表写明 `license` 属于规范的一部分）。
6. **`parse_frontmatter` 对嵌套键（`hooks`）的行为**：未核对源码细节，步骤 3 实施前确认。
7. **M4（Codex `cwd` 不进指纹）**：由 `normalize_mcp` 代码阅读得到（它不读 `cwd`），未用 Codex 配置夹具实测。M1、M2、M3 与 §1.2 的悬空软链用 6.15.0 运行时在临时夹具上实测。
8. **#184 第 3 条的实际成因**：没有该用户的配置，M1–M4 中哪一种造成那 3 组误报**未确认**。
9. **`mcp-remote` 的 README 内容**读自其 `main` 分支（2026-11-24），不是固定提交；令牌目录 `~/.mcp-auth`、`MCP_REMOTE_CONFIG_DIR`、按组合分缓存的说法以该日内容为准；它在 Claude Code、Cursor、Codex 各自的 stdio 启动中的行为（README 自述 Cursor、Codex-Cli 与 Windows 版 Claude Desktop 有 `npx` 参数空格转义问题）**未实测**。
10. **同步盘上相对软链的行为**：未确认（沿用 #82 §13 条 10）；影响面只描述本机文件事实。
11. **评论里的客户端现象**（超时、`failed` 与实际不一致、刷新锁、版本协商探测超时）来自维护者本机日志与桌面端 2.1.330，是否客户端缺陷**未确认**；Claude Code 文档对 OAuth 刷新的描述（刷新令牌失败时的重连与提示，2026-11-24）与评论现象的对应关系**未核对**。
12. **启动器选项表**（`npx`、`uvx`、`docker run` 等带值选项）的完整性未评估；设计用「表外降级为 `opaque` 或 `binary`」处理，不依赖完整性。
13. **旧 `ext-decisions.json` 里被移入 `mcp_hints[]` 的组的清理**：本版不处理，是否需要清理命令未确认。
14. **基线对照测试取 `v6.15.0` 运行时**依赖 CI 取全 tag（`AGENTHELM_REQUIRE_TAGS=1`），沿用 PR #161 的做法，具体辅助函数名以实施时为准。

---

## 12. 术语对照表

- Impact（影响面）：一次处置之后，各客户端对某个 Skill 或 MCP 的可见性变化
- Reachability（可达性）：某客户端按其读取位置与软链解析，能否读到某个条目
- Physical path（物理路径）：父目录软链全部展开、最后一级不展开的路径
- Dangling symlink（悬空软链）：目标不存在的符号链接
- Fingerprint（指纹）：脱敏后的 `command + args` 或 `url` 的摘要
- Target（目标服务）：MCP 条目实际连接或启动的服务标识（URL、包、脚本、模块、可执行文件）
- Target class（目标等价类）：同一份输出内目标等价的条目共享的序号标签
- Hint（提示）：不构成重复组、只作说明的发现
- Evidence（证据等级）：`file`、`setting`、`cli`、`unknown`
- Verdict（判决）：`unchanged`、`lost`、`replaced`、`gained`、`uncertain`、`unknown`
- Compat read（兼容读取）：客户端文档记载会读取、但 inventory 未建模的位置
- plan / plan_digest（计划 / 计划摘要）：预览与应用的一致性凭证
- Batch digest（批量摘要）：成员计划摘要与选择器的联合摘要
- Retire（退役）：归档（移动到恢复目录），不是删除
- Managed / unmanaged（受管 / 未受管）：agent-pack 的 state 是否记录了归属
- Transport（传输方式）：`stdio`、`http`、`sse`
- OAuth（Open Authorization）：开放授权
- mcp-remote：把远程 MCP 服务桥接为本机 stdio 的第三方命令行程序
- Jaccard similarity（杰卡德相似度）：集合交并比
- CB：`skills-mcp-menu.md`（技能与 MCP 管理菜单设计）
- #82：`mcp-skill-multi-project.md`（多项目、多机的 MCP 与 Skill 管理设计）
