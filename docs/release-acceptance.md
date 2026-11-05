# 发布与实机验收台账

对应 [#44](https://github.com/lc4t/agenthelm/issues/44)，核对时间：2026-11-04 UTC。这里记录源码与发布记录能证明的事实，以及仍需真实客户端验收的事项。夹具结果不能替代真实客户端结果。

## 已确认的发布状态

- [PR #52](https://github.com/lc4t/agenthelm/pull/52) 已把 `dev` 合入 `main`；`main` 和正式 tag `v6.11.0` 指向 `508226eb4eb10f6bff3c322233e3859065fde12e`。
- [v6.11.0 正式 Release](https://github.com/lc4t/agenthelm/releases/tag/v6.11.0) 于 2026-11-01 19:26:06 UTC 发布，`draft=false`、`prerelease=false`，带有 `agenthelm-v6.11.0.tar.gz` 与 `release.json`。
- 本轮修复的 `dev` 基线为 `709f6194c72bfb25fbaf061a98907e659c3ecfcb`；其文件树已包含正式版 CHANGELOG。正式 tag 位于合并提交上，不是该 `dev` 提交的祖先；因此 `git describe` 返回 rc.3，不能据此断言正式版尚未发布。
- #44 正文的“正式版暂不发布、需先出 rc.2”是历史状态。rc.2、rc.3 与正式版的变更说明已经保存在 `CHANGELOG.md`。

以上证据来自 GitHub `branches/main`、`git/ref/tags/v6.11.0` 与 `releases/tags/v6.11.0`。本轮没有打 tag、创建 Release、暂存 npm、推进 `main` 或合并 PR。npm 当前发布通道、本机安装版本与 hook 信任状态不由这些 GitHub 记录证明。

## 后续修复与验收

| 问题 | 本轮代码审阅入口 | 真实机器上仍需核对 |
| --- | --- | --- |
| [#45](https://github.com/lc4t/agenthelm/issues/45)：接管项目只比较受管区块 | 独立修复 PR，夹具检查用户原文与旧保留决定 | 接管项目升级后，区块外原文不再触发内容冲突；区块更新仍提示 |
| [#46](https://github.com/lc4t/agenthelm/issues/46)：写命令预览一致性 | 独立修复 PR，保留兼容并补 Case 预览 | 项目内旧脚本仍能执行，新预览不写文件 |
| [#47](https://github.com/lc4t/agenthelm/issues/47)：Cursor 发布更新 | 独立修复 PR，覆盖远程产物与本地源 | 无本地仓库时下载、校验、安装、回读，以及 sessionStart 注入 |
| [#48](https://github.com/lc4t/agenthelm/issues/48)：多客户端旧副本阻塞 | 独立修复 PR，多客户端版本组合夹具 | 一份新副本、一份旧副本时内容更新正常 |
| [#49](https://github.com/lc4t/agenthelm/issues/49)：内容退出码、差异与保留状态 | 独立修复 PR，中英文回归 | 请求被阻塞时非零、差异可直接消费、已保留文件不重复追问 |
| [#50](https://github.com/lc4t/agenthelm/issues/50)：rc.3 小问题 | 独立修复 PR，逐项 disposition 与待验清单 | 下表的实机项目 |
| [#51](https://github.com/lc4t/agenthelm/issues/51)：远程 MCP 与多机声明 | 独立修复 PR，三客户端配置与不泄漏夹具 | A 机导入修改、B 机发现与同步；客户端实际展开环境变量并连接 |
| [#24](https://github.com/lc4t/agenthelm/issues/24)：剩余国际化 | 已完成项见 issue 评论；剩余项分开审阅 | 英文内容人工审校仍待有资质的审阅者完成 |
| [#28](https://github.com/lc4t/agenthelm/issues/28)：发布流程 | 实现已由 [PR #62](https://github.com/lc4t/agenthelm/pull/62) 合入 `dev`（变更片段与新发布门禁） | 完整发布演练（GitHub Release、npm 暂存与 2FA、Pages、分支保护）需维护者执行；不得用单元测试声称 GitHub/npm/客户端已验证 |

表中的修复入口表示拟交付范围；以关联 PR 的最终 diff、测试记录与 CI 为准，不能据此把 issue 的验收项全部打勾。

## 未完成的真实客户端验收

| 项目 | 可复现的操作与需保存的证据 | 状态 |
| --- | --- | --- |
| 有基线的联网三方合并 | 用 rc.1 及以后初始化的测试项目；保留内容锁，修改 AGENTS.md；使用正式 Release 取历史基线；记录来源、预览、合并结果和恢复目录，确认用户改动保留 | 待验 |
| Codex hook 信任与运行 | 在 `/hooks` 审阅并信任；开启新会话；记录 hook 实际注入和退出状态 | 待验 |
| Cursor sessionStart | 核对 `~/.cursor/hooks.json`，打开新会话，记录实际注入及路径（含空格） | 待验 |
| Codex marketplace remove | 在可恢复的测试配置下，分别保存 remove 前、remove 后、重新 add 后的 installed/enabled 状态，不能只有 add 后读回 | 待验 |
| Claude Code Stop 与用户消息 hook | 测试会话记录 `additionalContext` 和关键词提醒的实际效果 | 待验 |
| SOP、nudge、本机专属目录写入 | 在隔离测试项目执行预览、显式应用、恢复与读回；不在日常工作目录直接试验 | 待验 |
| npm 与升级来源 | 查询正式包版本与 dist-tags，核对所用版本源、市场 ref、运行时 current 与安装后实际版本 | 待验 |

结果应记录客户端版本、操作系统、来源 tag、命令、退出码与脱敏输出。令牌只记录变量名，不能附值。没有真实机器证据就保持“待验”，不得因为对应代码已合并而自动改为通过。

## 审阅与合并顺序

本轮所有 PR 都以 `dev` 为目标，不自动合并。[PR #62](https://github.com/lc4t/agenthelm/pull/62) 已合入 `dev`：功能 PR 不再手改版本与派生发布载体，而是新增唯一命名的 `release/changes/<issue>-<slug>.json` 变更片段，并通过 `python3 scripts/release_meta.py check-changes --base origin/dev` 门禁（规则见 `release/VERSIONING.md`）。基于 #62 之前旧 `dev` 的 PR 需先合并当前 `dev`，把发布载体恢复为 `dev` 的值并补片段后再跑新门禁；GitHub 上旧工作流的绿灯不能代表新门禁通过。不要把功能 PR 直接合入 `main` 来绕开门禁。
