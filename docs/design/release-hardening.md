# 发布加固设计：promote 自动化、latest.json 迁移窗口、发布工具拆分

关联 [#28](https://github.com/lc4t/agenthelm/issues/28)（第八节“加固项”与 2026-11-10 的前 3 次发布复盘评论）。基线 `v6.13.0`（`dev` 与 `main` 同在 `bb84a08`），核对日期 2026-11-11。**本文只设计，不实施；不改任何脚本、工作流或发布载体。**

## 0. 结论

1. **promote 自动化**：保留 npm 2FA 人工批准，分两步做。第一步新增独立的 `promote.yml`（`workflow_dispatch`，输入 tag），带需要审批的 GitHub Environment（环境）`promote`，前置检查全部通过才快进 `main`；第二步把触发改成定时轮询 `npm view`，npm 上出现该版本即视为人工批准已发生，去掉第二次点击。令牌取 GitHub App 安装令牌（备选：`GITHUB_TOKEN` + 规则集旁路，旁路能力**未确认**）。**不把 Environment 绑到 npm Trusted Publisher**，从根上避开 #28 评论里的顺序风险。
2. **latest.json 迁移窗口**：按 D3 的“最近 12 个套件版本”今天**一条都裁不掉**（现有 12 个正式 tag 全在窗口内），实测只省约 2 KB；真正的体积在条目正文（`summary` 占 38%、`actions` 占 24%）。建议把“窗口”定义为**全文窗口**：窗口外的条目在 `latest.json` 里降级为索引条目（保留 `plugin/from/to/change_kind/breaking/date`，不兼容条目保留短摘要），全文放在 Pages 已发布的 `release/migrations.json`，新客户端按需拉取。老客户端（6.8 至 6.13）不报错、不丢“不兼容”标记，只丢窗口外步骤的文字。实施前需维护者拍板 N 与降级口径（决策 D2-1、D2-2）。
3. **发布工具拆分**：合计 1,022 行，超出预算 222 行（27.8%）。**拆分本身不减少总行数**（估计增加 40 到 60 行导入与门面），只能改善“单文件可审查性”和依赖方向；本周期不实施。真正的结构病灶是 `release_workflow` 通过 `types.SimpleNamespace(**globals())` 反向依赖 `release_meta`（三处），先消除它再谈拆分。

编号决策项见第 5 节，未核实清单见第 6 节，issue 草稿见第 4 节。

---

## 1. promote 自动化（#28 第七节第 4 步）

### 1.1 现状（均已读到源码）

- `release.yml`（tag `v*` 触发）：校验 tag 位于 `release/vX.Y.Z` 顶端且 `main` 是其祖先 → 测试 → `check-release` → 生成 tarball 与 `release.json` → `validate_release.py` → 创建 GitHub Release → `npm stage publish` 暂存（`.github/workflows/release.yml`）。末尾注释明确：不碰 `main` 与 Pages，由维护者批准 npm 后快进 `main`。
- `pages.yml`：`push` 到 `main` 或 `workflow_dispatch` 触发；两个作业都有 `if: github.ref_name == 'main'`，所以手动触发必须 `--ref main`。构建时复制整个 `release/` 目录并用 `attach_release.py` 附上 Release 信息。
- `release.yml` **没有 `environment:`**，权限为 `contents: write`、`id-token: write`、`actions: write`。
- **与任务背景的偏差**：任务说明写“release.yml 现已用 `gh workflow run pages.yml` 绕开”。实际该步骤在 `ce20e77`（PR #62，2026-11-05）之前存在（`git show ce20e77^:.github/workflows/release.yml` 第 110 行），此后已删除；现版只剩没用上的 `actions: write`。promote 实施时需要重新引入“显式触发 Pages”。
- 手动 promote 已执行 3 次（v6.11.0、v6.12.0、v6.13.0），触发条件已满足（#28 复盘评论）。

### 1.2 官方文档核实

| 事实 | 出处 | 结论 |
| --- | --- | --- |
| 由 `GITHUB_TOKEN` 触发的事件不创建新的 workflow run，`workflow_dispatch` 与 `repository_dispatch` 除外；用它 push 也不会触发 `push` 工作流 | [GITHUB_TOKEN 概念页](https://docs.github.com/en/actions/concepts/security/github_token)；[events-that-trigger-workflows](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows) | 已核实。`GITHUB_TOKEN` 快进 `main` 不会触发 `pages.yml` 与 `tests.yml` 的 `push main`，必须在 promote 里显式 `workflow_dispatch` Pages |
| `workflow_dispatch` 仅在工作流文件位于默认分支时触发 | 同上 events 页 | 已核实。默认分支 `main`（`gh api -X GET search/repositories -f q='repo:lc4t/agenthelm'` 返回 `default_branch: main`）。**`promote.yml` 在第一次随发布进入 `main` 之前无法被分派**（见 1.6 的第 0 步） |
| Environment 的 required reviewers 最多 6 人，一人批准即放行；可勾选 Prevent self-review；环境密钥在审批通过后才可用；可限定可部署的分支与 tag | [Managing environments](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment) | 已核实。**独立维护者场景需关闭 Prevent self-review**，否则自己触发的 run 无人能批 |
| 工作流等待环境审批最长 30 天；单个 run 最长 35 天；托管 runner 单作业 6 小时 | [Actions limits](https://docs.github.com/en/actions/reference/limits) | 已核实。轮询不能挂在一个长作业里，应采用定时重试 |
| 规则集的旁路角色可以是仓库角色（admin、maintain/write 及自定义角色）、团队、GitHub Apps、Dependabot；页面未提 `GITHUB_TOKEN` / GitHub Actions 能否作为旁路主体 | [Creating rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository) | GitHub App 可旁路已核实；**Actions 内置身份能否旁路未确认** |
| npm `stage publish` 对受信发布者始终允许；`stage` 的 `list/view/approve/reject` 需要交互认证、不能用 OIDC 令牌；推荐只授权 stage，使每个版本都要维护者 2FA 批准 | [npm Trusted publishers](https://docs.npmjs.com/trusted-publishers) | 已核实。**npm 批准无法被自动化，这是保留 2FA 的硬约束，不是实现缺口** |
| 受信发布者字段：组织/用户、仓库、工作流文件名必填，环境名可选；npm 保存时不校验，发布时才报错；已有配置不能编辑，只能删除重建；新配置须在 2 天内完成首次成功发布，否则过期 | 同上 | 已核实（对应 #28 评论的顺序风险） |

“GitHub App 令牌推送能否触发后续 workflow”：官方页面推荐用 App 令牌或 PAT 绕开 `GITHUB_TOKEN` 的限制（[GITHUB_TOKEN 概念页](https://docs.github.com/en/actions/concepts/security/github_token)的“Next steps”与[令牌页](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)的“创建 GitHub App 并在工作流内生成安装令牌”），但“App 令牌的 push 必然触发 `push` 工作流”的原文**未取到**（`triggering-a-workflow` 页返回 404），标**未确认**；设计上不依赖它：无论哪种令牌，promote 都显式分派 Pages。

### 1.3 触发条件：如何得知“npm 已批准”

穷尽可行选项：

| 选项 | 做法 | 评价 |
| --- | --- | --- |
| T1 同一 run 里 `needs: release` 的 promote 作业 + Environment 审批 | tag 推送后 release 作业完成，promote 作业挂起等待点击 | 排除：run 挂起期间不可重跑单作业、占用并发组；批准晚于 30 天会失败；失败后需重跑整个发布 run |
| **T2 独立 `promote.yml`，`workflow_dispatch(tag)` + Environment 审批** | 维护者批准 npm 后分派并点一次批准 | **第一步采用**：与发布 run 解耦，可重跑，幂等 |
| **T3 同一文件加 `schedule`，轮询 `npm view @sakanano-moe/agenthelm@<版本> version`** | 找到“已有 Release 且 `main` 尚未包含 tag 提交”的最新正式 tag，npm 有该版本即 promote | **第二步采用**：npm 批准本身就是人工决定，轮询只是读取这个决定；去掉第二次点击 |
| T4 npm 向 GitHub 发 webhook/`repository_dispatch` | — | 排除：npm 文档未提供该能力（**未确认**其不存在，但没有可引用的文档） |
| T5 单作业内 `sleep` 轮询 | — | 排除：6 小时作业上限，且占用 runner |

T3 的定时事件不是 `GITHUB_TOKEN` 触发的事件，不受“不触发新 run”限制；定时工作流只在默认分支的最新提交上运行（**未取到官方原文，未确认**，设计上本来就要求 `promote.yml` 在 `main` 上）。

### 1.4 权限与令牌

```
tag v6.14.0 ──► release.yml：测试/门禁/Release/npm 暂存（main 不动）
                   │
维护者在 npmjs.com 用 2FA 批准 ──► npm 上出现 6.14.0（不可自动化）
                   │
promote.yml（dispatch 或定时）
  前置检查（任一失败即退出且不推送）
    a. tag 是 vX.Y.Z（排除 -rc），等于 index.json 套件版本
    b. tag 提交 == origin/release/vX.Y.Z 顶端
    c. main 是 tag 提交的祖先（可快进）；main 已等于 tag 提交则成功空操作
    d. 该提交上 tests.yml 的 release/* push 运行结论为 success（API 读取）
    e. GitHub Release 存在且 release.json.commit == tag 提交
    f. npm view @sakanano-moe/agenthelm@<版本> version 命中
  [环境 promote：需审批（第一步）]
  git push origin <sha>:refs/heads/main     （不带 --force）
  gh workflow run pages.yml --ref main      （显式；不依赖 push 触发）
```

- 令牌：推荐 **GitHub App 安装令牌**（仅 `contents: write`、`actions: write`，装在本仓库），App 加入 `main` 规则集的旁路列表（官方文档确认 App 可旁路）。备选：`GITHUB_TOKEN`，前提是“Actions 身份可旁路”已验证（**未确认**）；若不能，则只能改成规则集放行 `maintain` 角色并由人推送，等于回到手动。
- `permissions` 逐作业声明：promote 作业 `contents: write`、`actions: write`，**不要** `id-token: write`。release 作业里没有被使用的 `actions: write` 可在同一个 PR 删除。
- 不推 tag、不推别的分支；唯一写操作是“快进 `main` 到已验证提交”。
- 在 `promote.yml` 中把 `main` 的推进限制为 `--ff-only` 等价语义：先 `git merge-base --is-ancestor origin/main <sha>`，再普通 push；非快进时服务器会拒绝，不需要也不允许强推。

### 1.5 `main` 保护现状与需要维护者确认的事项

仓库当前的分支保护 / 规则集**未核实**：本会话没有读取仓库设置的权限（GitHub MCP 无规则集接口，`gh` 仅用于搜索）。#28 复盘里的唯一实测线索是：2026-10-30 AI 会话推 tag 被拒（403，无 `GH013` 规则集报错，维护者本机推送成功）。需要维护者确认并回填：

1. `main` 是否启用规则集或分支保护；是否“限制更新 / 要求 PR”；旁路主体是谁。
2. tag（`v*`）是否有规则集；Actions 身份能否创建 tag（影响后续“自动打 tag”，本文不设计）。
3. 现有 Environment 清单（至少有 `github-pages`），以及其部署分支限制。
4. 是否存在其他维护者（决定 Prevent self-review 是否能开启）。

### 1.6 分步实施顺序（每步以前一步稳定运行为前提）

| 步 | 内容 | 验证 | 回退 |
| --- | --- | --- | --- |
| 0 | 维护者回填 1.5 的四项设置；决定令牌方案（决策 D1-2） | 设置截图或 API 输出记录在 issue | — |
| 1 | `promote.yml` 干跑版：只做前置检查 a 到 f 并输出“将要推送的命令”，不推送；随一次正常发布（手动 promote）进入 `main` | 该次发布中分派干跑，输出与实际手动 promote 的 sha 一致；故意传入 rc tag、过期 tag、未批准的 npm 版本，三种都应失败 | 删除文件；手动流程不受影响 |
| 2 | 加 Environment `promote`（required reviewers，开关 Prevent self-review 视决策）与真实推送 + Pages 分派；手动 promote 作为后备保留 | **演练**：见下“临时 tag 演练”；之后连续 3 次真实发布由工作流 promote，每次核对 `main` 提交、Pages 部署、`latest.json.release.tag` | 关闭 Environment 审批人并停用工作流；手动 `git push origin vX:main` |
| 3 | 加 `schedule` 轮询；Environment 审批保留一个周期，再决定是否去掉 | 轮询在 npm 批准前保持“检查未通过”而非失败；批准后一个周期内完成 | 注释掉 `schedule` 回到步骤 2 |
| 4 | （不在本文设计范围）自动打 tag；陷阱：`GITHUB_TOKEN` 推的 tag 不触发 `release.yml`，需 App 令牌或在同一工作流内串联 | — | — |

**临时 tag 演练**（步骤 2 之前必做）：

- 约束：`release.yml` 对 `v*` 都会运行，演练 tag 不能真的触发 npm 暂存。做法：在一个**一次性沙盒仓库**（复制本仓库的 workflow 与 `main` 规则集，npm 步骤因没有受信发布者配置而自然失败）上推送临时 tag `v0.0.0` 到 `release/v0.0.0` 顶端，分派 `promote.yml`（沙盒里把“npm 命中”检查通过输入 `skip_npm_check` 跳过，该输入只在 `github.repository != lc4t/agenthelm` 时有效），验证：令牌可推送受保护的 `main`、Pages 被显式分派、非快进被拒、重复分派是空操作。
- 真仓库里只能做到“干跑 + 推送到 `promote-drill/<tag>` 临时分支”，验证令牌的写权限与规则集的 tag/分支模式；**无法在不移动真 `main` 的前提下验证 `main` 规则集的旁路**，因此沙盒这一步不可省，或接受第一次真实 promote 带着手动后备执行。
- 真实 `main` 在演练中绝不被推动。

### 1.7 失败、回滚与安全评估

- 失败：前置检查失败或推送被拒时 `main` 不变，客户端无影响，与 #28 第七节一致；npm 超时未批准则保持“产物已就绪、客户端未生效”。已 promote 后的问题仍然向前修复（新 patch + `npm deprecate`），不强推。
- 幂等：`main` 已等于 tag 提交时成功退出；轮询每次都重新检查全部前置条件，不保存状态。
- 谁能触发：`workflow_dispatch` 需要仓库写权限；定时触发无人为输入。攻击面：有写权限者可推 `v*` tag 触发发布流程，但 npm 发布需要维护者 2FA，promote 只在 npm 已发布该版本后才推进。仍有的缺口：写权限者推一个指向 `release/vX` 顶端之外提交的 tag——检查 b 拒绝；推一个已有 npm 版本的重复 tag——检查 b/e 拒绝。检查 f 只证明“该版本号已在 npm 上”，不证明 npm 内容来自该提交；加固（可选）为比对 npm 的 provenance 声明里的提交 SHA，`npm view` 暴露哪些字段**未确认**。
- 能推什么：只能把 `main` 快进到一个同时是 `release/vX` 顶端、已通过 CI、已有 Release 的提交。
- 不把 Environment 绑到 npm：受信发布者的环境名一旦绑定就要“先改 npm 配置（删除重建）再改工作流”，且重建后须 2 天内首次成功发布，窗口内暂存会失败。promote 作业与 npm 无关，把 Environment 放在 promote 作业上、`release` 作业保持无 `environment:`，npm 配置完全不用动。需要绑定时按“先 npm 后工作流”并选在没有待发布版本的时段执行（决策 D1-3 倾向不绑）。

---

## 2. `latest.json` 迁移窗口（#28 第五节 R1、第八节触发条件、D3）

### 2.1 实测现状（可复算）

```sh
wc -c latest.json release/migrations.json                    # 85120  82032
python3 -c "import json;m=json.load(open('latest.json'))['migrations'];print(len(m))"   # 41
git tag --list 'v*' | grep -v rc | sort -V                   # 12 个正式 tag：v6.2.0 … v6.13.0
curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' https://agenthelm.sakanano.moe/latest.json            # 200 85416
curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' https://agenthelm.sakanano.moe/release/migrations.json # 200 82032
```

- 仓库内 `latest.json` 85,120 字节、41 条迁移；线上 85,416 字节（多出 `release` 信息块）。`migrations` 序列化（缩进 2）约 80 KB，占 94%；去掉 `migrations` 后仅约 1.3 KB。
- 条目字节分布（`ensure_ascii=False` 紧凑序列化后各字段合计）：`summary` 30,996、`actions` 19,447、`summary_en` 8,975、`user_action_required` 5,436、`touches` 3,523，其余元字段合计不足 3 KB。
- 来源分布：`release/history/legacy` 34 条 61,120 字节（72%，无套件版本归属）、`6.12.0` 4 条、`6.13.0` 3 条。每次发布约增加 3 到 4 条、8 到 12 KB。
- 触发条件：>30 KB 已满足，>60 条未到（#28 第八节）。
- **Pages 站点已发布全量迁移**：线上 `https://agenthelm.sakanano.moe/release/migrations.json` 200，82,032 字节，与仓库文件字节数相同；原因是 `pages.yml` 把整个 `release/` 复制进 `_site/`（含 `release/history/**`）。注意路径是 `/release/migrations.json`，**不在站点根**（#28 R1 的“站点根已发布该文件”表述不精确）。`raw.githubusercontent.com/lc4t/agenthelm/main/release/migrations.json` 是同路径的第二来源（客户端的两个默认 `latest.json` 来源见 `agenthelm.py:51-54`，同一前缀）。

### 2.2 窗口大小的实测：D3 的字面执行几乎裁不掉

窗口口径：对第 N 个最近正式 tag 的各插件版本记为“下限”，丢弃 `to` ≤ 下限的条目（脚本：读 `git show <tag>:latest.json` 的 `plugins.*.version`，v6.2.0、v6.4.0 的 tag 没有 `latest.json`，所以只有 10 个正式 tag 有 BOM）。

| N（正式套件版本） | 保留条目 | latest.json 字节 |
| --- | --- | --- |
| 12（D3 字面） | 37 | 83,173 |
| 8 | 35 | 82,301 |
| 6 | 31 | 79,756 |
| 4 | 24 | 75,451 |
| 3 | 7 | 22,561（但丢掉 13 条不兼容条目中的 12 条，见 2.3） |

结论：D3 的 N=12 在今天只省约 2 KB；体积由条目正文决定，不由条数决定。若要真正回到 30 KB 以下，必须同时降级窗口外条目的**正文**。

### 2.3 消费端兼容调研（已读源码）

读取 `migrations` 的只有 `plugins/agent-pack/runtime/agenthelm.py`（`packages/cli/runtime/` 下的副本字节相同，`diff -q` 无差异；`agents-init`、`opinion-manager`、`project-orchestrator` 的运行时无读取，`grep` 已核对）：

- `migrations_between(latest, plugin, current, target)`（第 433 行，自 v6.8.0 起存在：`git show v6.8.0:…agenthelm.py` 第 252 行）：遍历 `latest.get("migrations", [])`，保留 `current < to ≤ target` 的条目，同一 `to` 取 `from` 更新者。只读 `plugin/from/to`。
- `plugin_findings`（第 467 行）：`status == "behind"` 才调用；从步骤派生 `change_kinds`、`breaking`、`steps[{to, change_kind, breaking, summary, actions}]`。
- 渲染（约第 1422 行）：不兼容步骤打印 `"{to} 不兼容：{summary}"` 与 `actions` 列表；`summary` 缺失时会打印 `None`（`step.get('summary')` 直接格式化）。`migration_language_note` 与 `migrate` 的两个调用点（第 1385、2901 行）读取 `summary` 判断是否含中文。
- 缓存：`load_latest` 每日缓存整个 `latest.json`（含 `migrations`），离线或联网失败时返回缓存；`_fetch` 只校验 `marketplace` 字段，**多出的顶层字段对所有历史客户端无害**。

**老客户端（6.8 至 6.13）遇到窗口外缺失条目时：** `migrations_between` 返回空步骤，`breaking` 为 `False`，既不报错也不提示——**“落后很多版本、其间有不兼容变更”的用户被静默告知“无迁移说明”**。这是窗口的核心风险，不是体积问题。现有 41 条中 13 条 `breaking=true`（`agents-init` 6.4→6.5、6.7→6.8、6.8→6.8.1、6.7→6.8.2 等），其中大部分已在窗口外或将出窗口。

**升级跨度大于窗口：** 新客户端检测到“当前版本低于窗口下限”时需要全量。**离线时**只有缓存可用；**失败时**不能退化为“无说明”。

### 2.4 方案与排除项

穷尽选项：

| 选项 | 评价 |
| --- | --- |
| W1 硬裁窗口外条目 | 排除：老客户端静默丢失不兼容提示（2.3） |
| W2 窗口外只留不兼容条目（全文） | 排除：13 条不兼容条目全文约 60 KB，几乎不省 |
| W3 窗口外条目整体降级为“索引条目”（`plugin/from/to/change_kind/breaking/date`） | 单独不够：老客户端对不兼容步骤打印 `None` |
| **W4（推荐）W3 + 窗口外的不兼容条目保留短摘要（≤80 字符）且 `actions: []`，全文在 `release/migrations.json`，`latest.json` 加窗口元数据** | 老客户端：仍显示“不兼容”与一句话，不再列出操作清单（降级可见）；体积见下表 |
| W5 把迁移整体移出 `latest.json`，只留指针 | 排除：老客户端会静默得到空迁移，影响面与 W1 相同，且 `latest.json` 契约变更不可回退 |

W4 的体积（窗口内全文条数以“最新 N 条”近似；套件版本到条数的映射见 2.5）：

| 窗口内全文条数 | latest.json 字节 |
| --- | --- |
| 14（近似，未做套件版本映射） | 50,919 |
| 7（恰为 6.12.0 与 6.13.0 的 7 条） | 30,955 |
| 3 | 19,142 |

窗口元数据建议（新字段，老客户端忽略）：

```json
"migrations_window": {
  "schema": 1,
  "floor": {"agents-init": "6.9.1", "agent-pack": "1.12.0", "...": "..."},
  "full": "release/migrations.json"
}
```

- `floor` 是窗口下限的各插件版本：当前版本 ≥ 下限的插件，窗口内已有完整步骤；低于下限的插件需要全文。
- **生成侧**：只改 `build_latest`（`scripts/release_meta.py:172`）；`read_migrations()` 与 `history_migrations()` 仍返回全量，`release/migrations.json`、`since_problems`（第 324 与 347 行）、`migration_problems` 继续使用全量，**发布门禁不受窗口影响**。`consistency_problems` 对 `latest.json` 的“生成物一致”检查改为比对“窗口化后”的结果；窗口只依赖 `release/history/*/plan.json` 与 `legacy`，**不读 git tag**，避免 tarball 解压目录（无 git）里生成结果不同（现有测试在无 tag 环境下有 `AGENTHELM_REQUIRE_TAGS` 的特殊处理）。
- **消费侧（agent-pack 新版本）**：`migrations_between` 在“当前版本 < `floor[plugin]`”时，从 `latest` 来源同前缀拉取 `release/migrations.json`（Pages 与 raw 两个来源同相对路径，已核实 Pages 200）；只在 `behind` 且有缺口时联网，结果按日缓存到 `env.cache_dir`（沿用 `load_latest` 的缓存与失败退避）。
- **离线 / 失败 / 无 `migrations_window`**：有缓存全文则用缓存；否则在步骤里加一条明确的 `gap` 记录（“窗口外迁移说明暂不可用，请查看 CHANGELOG.md 或联网重试”），**视为可能不兼容**而不是“无变更”；退出码与现有 `check` 一致（不新增失败路径）。旧格式的 `latest.json`（无 `migrations_window`）按现状处理，保证客户端新旧 `latest.json` 双向兼容。
- **跨度大于窗口**：同上，一次拉取全量文件即可；全量文件随发布增长约 10 KB/次，在 N 个版本后也只在大跨度升级时才下载。

### 2.5 套件版本到条目的映射（D3 的“12 个套件版本”如何落地）

条目本身没有套件版本。可用的确定性映射：`release/history/<套件版本>/migrations.json` 目录名；`legacy` 是一个无归属的整体。因此窗口可以定义为“最近 N 个 `history/<版本>` 目录加（尚未出窗口时的）`legacy`”，不需要 git tag；今天只有 2 个版本目录，`legacy`（34 条）必然被视为窗口内。**要让窗口今天就起作用，必须由维护者给 `legacy` 条目指定一个归属或日期截止线（决策 D2-1）。**

### 2.6 需要改的文件

- `scripts/release_meta.py`（`build_latest`、`consistency_problems`）；窗口计算建议放入 `release_workflow.py` 的 `history_migrations` 旁，纯函数。
- `plugins/agent-pack/runtime/agenthelm.py`（`migrations_between`、渲染的 `None` 保护、按需拉取与缓存）及其 `locale` 文案；运行时 npm 副本由 `sync-runtime.mjs` 同步。
- 测试：`tests/test_release_meta.py`（窗口化生成与一致性）、`plugins/agent-pack/tests`（新客户端读新旧 `latest.json`、离线、失败、无字段）、`tests/test_site_release.py`（`release/migrations.json` 随 Pages 发布）。
- 文档：`release/VERSIONING.md`“唯一基线与载体”末段（现写“暂不裁剪”）。
- 不改：`pages.yml`（已复制 `release/`）。

### 2.7 判定“可以实施”的验收标准

1. 夹具：对当前 `release/migrations.json` 生成窗口化 `latest.json`，**所有 6.8、6.11、6.12、6.13 的客户端运行时**（用对应 tag 的 `agenthelm.py`）读取后，对任一落后版本的 `breaking` 判定与全量版本一致（回归矩阵，必须逐 tag 实跑）。
2. 窗口化后 `latest.json` ≤ 30 KB（以 D2-1 的 N 为准）且不兼容条目数不减。
3. 新客户端：低于 `floor` 时能取到全文；离线有缓存/无缓存、两个来源都 404、返回非法 JSON 四种情形都不抛未捕获异常，且不输出“无迁移”。
4. `release/migrations.json` 仍等于全量生成物；`check-release` 对窗口不敏感。
5. 在沙盒 Pages 或本地 HTTP 服务上做一次真实“跨 5 个版本”的升级演练并记录。
6. 先发布新客户端（agent-pack minor），**其后**才启用窗口生成；两步分属不同的发布，避免“生成侧先上线”。

---

## 3. 发布工具职责拆分（#28 第八节预算）

### 3.1 行数复核

```sh
wc -l scripts/release_meta.py scripts/release_model.py scripts/release_workflow.py
# 426 178 418 → 合计 1022；预算 800：超 222 行（+27.8%）
```

函数规模（`ast` 统计，行）：`release_model.plan` 76、`check_changes` 63、`release_files` 45、`fragment_problems` 45、`apply_release` 44、`release_workflow.main` 43、`consistency_problems` 41、`check_release` 40、`source_files` 38、`declaration_problems` 36、`since_problems` 35、`release_meta.main` 34。

### 3.2 现有耦合

- `release_workflow` 的函数接收 `meta`（`release_meta` 全局的 `types.SimpleNamespace(**globals())` 快照），用到 `meta.ROOT/read_json/git/dump/load_plugins/main/...` 等约 40 处。快照在调用时创建（`release_meta.py:165/392/400`），测试靠 `mock.patch.object(release_meta, "ROOT"/"read_migrations", …)` 生效（`tests/test_release_meta.py:27,87,204`）。拆分时这些补丁点必须同步迁移，是最大风险。
- 外部入口：`release.yml` 调 `scripts/release_meta.py check-release --tag`、`scripts/validate_release.py`、`scripts/release_notes.py`；`ci_gate.py` `import release_meta`；`release/VERSIONING.md` 点名 `release_workflow.version_fields/release_files`；`tests/test_release_meta.py`（368 行）、`test_release_workflow.py`（349）、`test_ci_gate.py`（170）、`test_validate_release.py`。

### 3.3 拆分方案（依赖方向：model ← manifest ← derive ← plan ← gates / apply ← cli）

| 新模块 | 归入的函数（现位置） | 约行数 |
| --- | --- | --- |
| `release_model.py`（不变） | `plan`、`bump`、`highest`、`fragment_problems`… | 178 |
| `release_manifest.py` 载体与清单 | 常量、`read_json`、`dump`、`git`、`json_at`、`plugin_roots`、`source_of`、`load_plugins`、`sha256_file`、`pack_files`、`refreshed_content`、`is_unshipped`、`version_fields` | 150 |
| `release_derive.py` 派生文件 | `build_marketplace`、`build_latest`、`read_migrations`、`history_migrations` | 100 |
| `release_plan.py` 计划与基线 | `bom_at`、`official_base`、`source_files`、`current_bom`、`fragments`、`validate_fragments`、`make_plan` | 120 |
| `release_gates.py` 门禁 | `migration_problems`、`declaration_problems`、`consistency_problems`、`since_problems`、`release_declaration_problems`、`changelog_mentions`、`changed_paths`、`owned_scope`、`frozen_json`、`check_changes`、`check_release`、`shipped_changes` | 340 |
| `release_apply.py` 写入 | `release_files`、`apply_release` | 100 |
| `release_meta.py`（门面+CLI，保留入口） | `main`（旧的 `--write/--since` 与三个子命令分发），对上述名字的重新导出 | 60 |

合计约 1,050 行，**总量不降反升**。若不删功能，“工具本体 ≤ 800 行”的预算无法靠拆分满足，只能改为“每个模块 ≤ 约 350 行、总量设上限 1,100 行”或删除功能（候选：`release_meta.main` 的旧 `--since` 入口与 `check-release` 的重叠，须另行评估，本文不下结论）。

### 3.4 评估与结论

- 收益：单文件职责清晰；`meta` 快照消除后，补丁点由“改全局”变为“改被查找的模块”，测试更直接；门禁（只读）与写入分离，审查写入器时不需读门禁。
- 风险：（1）载体注册表（`version_fields` 与 `release_files` 互相对应）被拆到两个模块，易漏同步，需要一个“载体清单 ↔ 写入文件”的一致性测试；（2）`release_meta.py` 作为 CLI 与 `import release_meta` 的兼容面必须保留全部现有名字；（3）`mock.patch.object(release_meta, …)` 要么保留 `release_meta` 作为查找命名空间（门面转发），要么逐个迁移；（4）`ci_gate.py` 与 `release.yml` 的命令行不得变化；（5）门禁行为零变化需要以“拆分前后同一批夹具输出完全一致”作验收。
- **结论：值得，但本周期不实施。** 先做无行为变化的前置重构（用显式模块引用替换 `SimpleNamespace(**globals())`），再拆模块。需要移动的测试：`test_release_meta.py` 中按被测函数分组到 `test_release_gates.py`、`test_release_derive.py`；`test_release_workflow.py` 拆到 `test_release_plan.py`、`test_release_apply.py`；`test_ci_gate.py` 与 `test_validate_release.py` 不动。

---

## 4. 后续实施的 issue 草稿

| # | 标题 | 范围 | 发布等级 | 依赖 | 验收 |
| --- | --- | --- | --- | --- | --- |
| I1 | promote.yml 干跑版 | 新增 `.github/workflows/promote.yml`（dispatch，前置检查 a 到 f，不推送）；删除 `release.yml` 里未使用的 `actions: write` | 预计无发布影响（工作流不在发布内容内，以 `check-changes` 实测为准，未确认） | 维护者回填 1.5；决策 D1-2 | 1.6 第 1 步的三类失败用例；干跑输出与手动 promote 的 sha 一致 |
| I2 | promote 真实推送与 Environment | 创建 Environment、App 令牌（或旁路验证）、推送 + 显式分派 Pages；VERSIONING.md 更新步骤 5 | 同上；文档变更无需片段 | I1 稳定一个发布；沙盒演练通过 | 沙盒演练四项；连续 3 次真实发布由工作流 promote |
| I3 | promote 轮询触发 | `schedule` 轮询 `npm view`；去掉第二次点击的决定（决策 D1-1） | 同上 | I2 稳定 3 次 | 批准前不推送、批准后一个周期内完成；无重复 promote |
| I4a | agent-pack：窗口外迁移按需拉取 | `migrations_between` 读取 `migrations_window`，按需拉取与缓存，`None` 保护与离线/失败降级 | agent-pack minor | 决策 D2-1、D2-2 | 2.7 的 3、5 |
| I4b | 生成窗口化 latest.json | `build_latest`/`consistency_problems`、VERSIONING.md、测试 | 套件内部工具变更，`suite` patch（实施时以门禁实测为准，未确认） | I4a 已发布 | 2.7 的 1、2、4 |
| I5 | 发布工具拆分（前置重构 + 拆模块） | 3.3；先消除 `SimpleNamespace` | 无发布影响（`scripts/`；以门禁实测为准，未确认） | 无；建议在 I4b 之后 | 拆分前后门禁夹具输出一致；`ci_gate` / `release.yml` 命令行不变 |

---

## 5. 决策项（需要维护者拍板）

- **D1-1 promote 是否去掉第二次点击**（I3）。倾向：先 T2（一次审批），3 次稳定后改 T3。替代视角：保留审批永久存在，作为“误发布”的最后拦截；代价是每次发布多一次点击。
- **D1-2 推送令牌**。倾向：GitHub App 安装令牌 + 规则集旁路（官方文档确认 App 可旁路）。替代：`GITHUB_TOKEN` 需先验证 Actions 能否旁路（未确认）；PAT 排除（绑定个人账号、过期与权限过宽）。
- **D1-3 是否把 Environment 绑定 npm 受信发布者**。倾向：不绑，避免“先改 npm、再改工作流”的顺序风险与 2 天首发失效窗口。替代：绑定可让“只有经审批的作业才能发布 npm”，但暂存模式下已有 2FA 人工批准，增益有限。
- **D1-4 沙盒演练**。倾向：用一次性沙盒仓库验证 `main` 规则集旁路。替代：只做真仓库干跑，第一次真实 promote 带手动后备。
- **D2-1 窗口的 N 与 `legacy` 的归属**。倾向：N=2 个套件版本的全文窗口（约 31 KB 量级，实测值见 2.4），并把 `legacy` 整体视为窗口外。替代：N=12（D3 字面，几乎不省）；按日期截止。
- **D2-2 窗口外条目的降级口径**。倾向：W4（不兼容条目留 ≤80 字符摘要且清空 `actions`）。替代：留完整 `actions`（更安全，约多 15 KB）；完全索引化（体积最小，但老客户端会打印 `None`，必须配合客户端修复，不推荐）。
- **D3-1 工具预算口径**。倾向：改为“每模块 ≤ 350 行、总量 ≤ 1,100 行”，拆分在 I4b 后做。替代：保持 800 行总预算并删除功能（需先评估旧 `--since` 入口）。

## 6. 未核实清单

1. `main` 的分支保护/规则集、tag 规则集、现有 Environment 清单（需维护者回填，1.5）。
2. Actions 内置身份能否作为规则集旁路主体（官方页面未提及）。
3. GitHub App 令牌 push 是否触发 `push` 工作流（官方 `triggering-a-workflow` 页取回 404，未取到原文）；设计不依赖它。
4. 定时工作流只在默认分支最新提交上运行（未取到官方原文）；设计要求 `promote.yml` 位于 `main`。
5. npm 是否提供“批准后”的 webhook / 事件（未见文档）；`npm view` 是否暴露可与 GitHub 提交比对的 provenance 字段。
6. `check-changes` 对 `.github/workflows/`、`scripts/` 变更是否要求片段（现有 VERSIONING.md 仅写“测试与 CI 配置本身不要求插件升版”，以 `ci_gate` 实测为准）。
7. 老客户端“静默丢失不兼容提示”是源码推断，已用 v6.8.0 的 `migrations_between` 与 v6.12.0 的同名函数核对形态，**未用真实旧版运行时实跑**（验收标准 2.7-1 的内容）。
8. 窗口体积数字（2.2、2.4）是对当前 `latest.json` 的离线计算，条数到套件版本的映射按“最新 N 条”近似。
9. Claude Code / Codex / Cursor 客户端是否会缓存或二次解析 `latest.json`：本仓库只由 agent-pack 运行时读取 `migrations`，客户端本身不读取（grep 范围：`plugins/`、`packages/`、`scripts/`）。

## 7. 复核命令

```sh
wc -l scripts/release_meta.py scripts/release_model.py scripts/release_workflow.py
wc -c latest.json release/migrations.json
grep -n "migrations" plugins/agent-pack/runtime/agenthelm.py          # 读取点
git show ce20e77^:.github/workflows/release.yml | grep -n "workflow run"   # 旧版的 Pages 触发
gh api -X GET search/repositories -f q='repo:lc4t/agenthelm' --jq '.items[0].default_branch'
```
