# 写操作预览策略（#46）

本次采用 #46 正文候选方案中的兼容策略：新命令默认预览、`--apply` 才写入；已有直接写入的 Case CLI 增加 `--dry-run`，**省略参数仍直接写入**，没有先警告再切换默认的计划。项目内 Agent 与旧脚本无需迁移，直接使用 CLI 时也能先审阅。这不满足 #46 标题的「默认预览」，该取舍需维护者在 issue 中确认，因此本 PR 只 Refs #46。

残余没有默认预览的写命令：`case.py new/update`（有 `--dry-run`，默认直接写入）、`assets.py rebuild`、`opinion_manager.py inbox add/decide/present/expire`（`inbox adopt` 默认预览，见下表）、`nudge hook`（见下文逐项说明）。

## 命令盘点

| 入口与写操作 | 预览方式 | 执行方式 |
| --- | --- | --- |
| `agenthelm install/update/migrate/fix/config` 写子命令 | 默认预览 | `--apply` |
| `agenthelm runtime install/update/use` | 默认预览 | `--apply` |
| `agenthelm local-only fix` | 默认预览 | `--apply` |
| `agenthelm project record/fix/tools` | 默认预览；菜单显示计划 | `--apply`；菜单确认 |
| `case.py new/update` | `--dry-run`（返回 `status: preview`、目标 `path` 与 unified `diff`） | 省略 `--dry-run`，保留兼容行为 |
| `sop.py new/activate/retire/restore/decide/revise/run/step/confirm-read/init/private` 与 `defaults --set` | 默认预览 | `--apply` |
| `memory.py create/verify/retire` | 默认预览 | `--apply` |
| `assets.py rebuild` | **无预览**：直接重建文件索引，只写本机缓存 `$XDG_CACHE_HOME/agenthelm/assets`（缺省 `~/.cache`），不写项目 | 直接执行（无 `--apply`，仅有 `--force`） |
| `opinion_manager.py inbox add/decide/present/expire` | **无预览**：直接写 `.opinion/inbox.json` | 直接执行；`decide` 另需 `--confirmed` |
| `opinion_manager.py inbox adopt` | 默认预览（零写入）；`--apply --confirm <预览指纹>` 才写 Profile、`OPINION.md`、锁并读回，失败回滚 | 预览指纹即确认；不使用 `--confirmed` |
| `case_workspace.py migrate` | 默认预览 | `--apply` |
| `opinion_manager.py compose` 与 versioned Profile/发布操作 | 默认预览 | `--apply`，不可变发布还需 `--confirm` 指纹 |
| `init_project.py` 初始化、接管、刷新、区块等写入模式 | 默认预览 | `--apply` |
| `agent_pack_config.py transfer/sync/bootstrap/reconcile`（亦经 MCP 提供） | 默认预览 | `--apply`；MCP `apply: true` |
| `agenthelm nudge install-hooks` | 默认预览 | `--apply` |

`case.py` 的预览复用与执行相同的 schema、路径、项目工具让位和并发前置条件校验。新建 ID 是预测值；并发创建可能占用该序号，执行时会重新分配。更新后再执行时，应传入读到的 `--expect-sha256` 或 `--expect-updated-at`，拒绝覆盖并发修改。

## 例外与边界

- `nudge hook` 是客户端事件消费者，会自动写本机去重状态；不是交互式项目编辑命令，若默认预览就无法正确去重，因此保留无预览例外。其行为由独立 hook 测试覆盖。
- 版本检查的本机缓存、解释器字节码缓存属于运行时缓存；预览不得修改项目文件或用户配置。`assets.py rebuild` 的文件索引同样只写本机缓存，但它是该命令的全部作用，所以仍在上表列为无预览，不能当作「预览」登记。
- `sop events` **只读**，仅匹配事件并给出建议，不触发执行；`sop run/step/confirm-read` 当前已经有 `--apply`，不属于缺少预览的命令。
- 打开文件、剪贴板、交互式查找器与项目自定义工具不是受管文件编辑 API；自定义工具需自行实现已声明的预览契约。
- `scripts/` 下维护者用的发布工具不属于插件运行时命令盘点；发布命令的预览与确认由发布流程单独约束。

`tests/test_i18n_commands.py` 自动发现全部子命令并要求登记，另有对账测试：登记为 PREVIEW 的命令其 `--help` 必须有 `--apply`/`--dry-run`；READ_ONLY 与 `DIRECT_WRITE`（直接写入、无预览，必须写明原因）的命令不得有这两个参数，只读用例运行前后磁盘不得变化。新增写命令漏登、或预览参数与登记不一致都会失败；一个直接写入且没有任何预览参数的新命令若被误登为只读，只会被「运行前后磁盘不变」的检查在其用例涉及写入时发现，不能保证。Case 预览已移入 `PREVIEW`；SOP 事件移入 `READ_ONLY`，步骤与私有读取确认的预览也进入 `PREVIEW`。专用 Case 测试验证成功和校验失败的预览均零写入，并验证原有执行方式继续可用。

English: Case `new` and `update` now accept `--dry-run`. They validate and return a proposed diff without writing. Omitting the flag retains immediate writes for compatibility. New commands continue to preview by default and require `--apply`. Client event hooks retain their documented local-state exception.
