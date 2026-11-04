# 版本与发布

功能分支从 `dev` 建立，PR 指向 `dev`。每个分支只增加独立命名的变更片段；各插件、内容包和套件的最终版本由发布命令统一计算。`main` 只承载已完成产物发布并经维护者确认的版本。

## 变更片段

`release/changes/<issue>-<slug>.json` 的例子：

```json
{
  "schema": 1,
  "summary": {"zh": "改善错误提示。", "en": "Improve error messages."},
  "changes": [{
    "scope": "agent-pack",
    "level": "patch",
    "actions": [],
    "user_action_required": [],
    "touches": [],
    "default_for_existing": "on",
    "template_changes": [],
    "contracts": []
  }],
  "suite": null
}
```

`scope` 为插件名或 `suite`（安装器、站点和发布工具）。`summary` 允许字符串或完整的 `zh`/`en` 对。等级为 `patch`、`minor`、`major`。插件 major 表示不兼容并需要用户动作，必须填写 `actions` 与 `user_action_required`；minor 是值得发布说明的新能力，patch 是兼容修复或小改进。与严格 SemVer 的差异是小能力可归 patch；下游 npm `^` 范围仍承诺不含破坏性变更。

声明沿用 `declaration: 2`：`actions` 是操作列表；`user_action_required` 为 `{"what":"操作", "session":"current|new"}` 的列表；`touches` 为用户项目路径或 Profile 字段；`default_for_existing` 为 `off|advisory|on`；`template_changes` 为 `{"path":"AGENTS.md", "class":"semantic|cosmetic"}` 的列表。语义模板变化作用于既有项目时必须有用户动作。路径应匹配内容包的来源路径或 `materializes` 路径，不能用无关路径占位。`contracts` 登记本次不兼容接口名，非空时必须声明 major；独立接口版本注册与依赖范围校验留待首次插件 major / 跨插件不兼容前实施。

发布按每个插件全部片段的最高等级升一次；套件取最高等级，也可用片段 `suite` 调高。新增插件强制套件至少 minor，移除插件强制 major。内容包由匹配的 `template_changes` 独立计算：semantic 升 minor，cosmetic 升 patch，没有变化保持。新插件与新内容包从 `1.0.0` 起；功能分支添加的元数据版本为临时占位，发布器会覆盖。未变化插件保持版本。

## 两道门禁

- 功能 PR：`python3 scripts/release_meta.py check-changes --base origin/dev`。发布内容须有本次新增且合法的对应 scope 片段；既有版本、摘要与派生文件保持基线值。历史记录、已有片段不可改。测试与 CI 配置本身不要求插件升版。
- 发布提交：`python3 scripts/release_meta.py check-release --since vX.Y.Z [--tag vA.B.C]`。检查派生文件、摘要、迁移声明、CHANGELOG、生成计划及 tag 一致。正式与候选共用门禁。

功能 PR 的模板内容可与冻结的旧摘要不同；应在临时副本里运行发布生成后验证发布门禁。不要把该验证产生的发布载体提交进功能 PR。普通测试仍检查清单版本与文件内容的一致性；修改模板的分支应使用发布预演副本运行完整测试。

## 唯一基线与载体

基线是最后一个正式 tag 的 BOM（历史 `latest.json` 的插件/内容包版本加 `index.json` 的套件版本）；RC 不移动基线。工具默认选最高正式 SemVer tag，并确认它已进入当前历史。兼容过去 `dev → main` 的同树合并 tag：tag 的同树父提交在当前历史中也可使用；其他未集成的正式 tag 会提示先把 `main` 合回 `dev`。可用 `--base` 显式选择已审查的正式基线。

纯函数 `release_model.plan` 的输入只有基线、全部片段、当前组件清单、日期和可选调高记录；排序、最高等级合并及输出均确定。重复预演或 RC 重新生成不会累加版本。日期可用 `--date YYYY-MM-DD` 固定。发布者可传 `--overrides file.json`（如 `{"agent-pack":{"level":"minor","reason":"影响面扩大"}}`），只可调高，原因保存在发布计划里。

载体注册表由 `release_workflow.version_fields` 与 `release_files` 实现：

| 事实 | 生成载体 |
| --- | --- |
| 插件版本 | 各 `plugin.json`、`.codex-plugin/plugin.json`、Skill `skill.json`、`index.json.skills`、市场清单、`latest.json` |
| 内容包版本与摘要 | 各 `content.json`、`latest.json` |
| 套件版本与日期 | `index.json.plugin.version` / `site.updated_at`、CLI `package.json`、站点 Release 徽标与页脚 |
| 初始化器版本 | 运行时读取自身插件 `plugin.json`，不再复制常量 |
| 迁移汇总 | `release/history/**/migrations.json` 加候选计划的迁移条目，生成 `release/migrations.json` 与 `latest.json.migrations` |
| 发布说明 | 当前计划生成的 CHANGELOG 标记段；正式发布计划与片段归档在 `release/history/<套件版本>/` |

既有手写迁移一次性原样导入 `release/history/legacy/migrations.json`。汇总不是第二个可编辑来源。暂不裁剪 `latest.json` 的历史窗口；达到 issue #28 的触发规模再加按需获取。

## 候选发布

1. 先提交所有功能片段，工作区和暂存区必须干净。运行 `python3 scripts/release_meta.py release plan --explain`，审查所有版本和声明。
2. 在 `dev` 上运行 `python3 scripts/release_meta.py release --candidate --apply`，只生成一个本地发布提交。失败会恢复本次修改；不自动 tag、不自动 push。候选保留原片段，可继续叠加修复片段并重新生成。
3. 审查并推送提交后，由维护者打 `v<套件版本>-rc.N` 并推送。工作流生成 GitHub pre-release 与 npm `next` 暂存。按候选验收表实机验证。

## 正式发布：先产物，后推进 main

1. 在 `dev` 运行 `release plan --explain`，从 `dev` 切出 `release/v<计划套件版本>`。确认 `main` 可快进到该分支（必要时先把 `main` 合回 `dev`）。
2. 运行 `python3 scripts/release_meta.py release --apply`。工具写完全部载体、生成迁移与发布段、把片段归档并从待发布目录移除，再校验门禁，最后产生一个本地发布提交。审查 diff 与测试，不自动 tag 或 push。
3. 维护者推送发布分支并在顶端打 `v<套件版本>`。`release.yml` 必须确认 tag 与该分支顶端相同，且 `main` 是其祖先；运行测试/发布门禁，生成 tarball 与 SHA-256 `release.json`，创建 GitHub Release，并暂存 npm。此时 `main` 未变化。
4. 维护者在 npmjs.com 用 2FA 批准暂存版本，确认 GitHub Release 与 npm 均可用。
5. 维护者将 `main` **快进**到已验证 tag：`git push origin v<套件版本>:main`（不得强推）；该 push 触发 Pages，此时 Release 已可附加到 `latest.json`。维护者须配置 GitHub 分支保护，禁止普通功能 PR 改 `main`；本工具不修改仓库权限或规则。
6. 把 `main` 合回 `dev`，保证正式基线已集成。

发布失败且尚未推进 `main`：客户端不变；修复并使用下一个 patch 版本，已推送 tag 不移动、不删除。推进后出错：向前发布修复并按需要 `npm deprecate`，不强推 `main`。npm 的人工批准尚未完成时，发布停在产物已暂存的状态，不推进 `main`。热修复从 `main` 创建 `release/vX.Y.Z`，沿用片段和同一流程，之后合回 `dev`；目前只支持一个正式主线。

## 工具失效时

所有生成文件均为普通 JSON / Markdown。可依据片段手工改载体和历史记录，再运行兼容的 `python3 scripts/release_meta.py --write` 与 `check-release` 验证；不要绕过同一个发布门禁。正式发布需有可审查的 `release/history/<版本>/plan.json`。首次三次正式发布后复盘手改载体数、耗时和发布 diff；未达到零手改或维护成本过大时，应简化实现。依赖范围、接口常量注册、迁移窗口与 promote 自动化均不在本次核心实现内。

自动化验证在临时 Git 仓库演练多个片段并行、确定性版本、候选重复生成、正式归档与失败回滚。真实 GitHub Release/npm 暂存/2FA/Pages/保护规则需维护者在实际发布时验收，本 PR 不执行这些外部动作。
