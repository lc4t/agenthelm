# 写操作预览策略（#46）

本次采用兼容策略：新命令默认预览、`--apply` 才写入；已有直接写入的 Case CLI 增加 `--dry-run`，保留旧默认。没有先警告再切换默认的计划。这样项目内 Agent 与旧脚本无需迁移，直接使用 CLI 时也能先审阅。

## 命令盘点

| 入口与写操作 | 预览方式 | 执行方式 |
| --- | --- | --- |
| `agenthelm install/update/migrate/fix/config` 写子命令 | 默认预览 | `--apply` |
| `agenthelm runtime install/update/use` | 默认预览 | `--apply` |
| `agenthelm local-only fix` | 默认预览 | `--apply` |
| `agenthelm project record/fix` | 默认预览；菜单显示计划 | `--apply`；菜单确认 |
| `case.py new/update` | `--dry-run`（返回 `status: preview`、目标 `path` 与 unified `diff`） | 省略 `--dry-run`，保留兼容行为 |
| `sop.py new/activate/retire/restore/decide/revise/run/step/confirm-read/init/private` 与 `defaults --set` | 默认预览 | `--apply` |
| `memory.py create/verify/retire` | 默认预览 | `--apply` |
| `assets.py rebuild` | 默认预览 | `--apply` |
| `case_workspace.py migrate` | 默认预览 | `--apply` |
| `opinion_manager.py compose` 与 versioned Profile/发布操作 | 默认预览 | `--apply`，不可变发布还需 `--confirm` 指纹 |
| `init_project.py` 初始化、接管、刷新、区块等写入模式 | 默认预览 | `--apply` |
| `agent_pack_config.py transfer/sync/bootstrap/reconcile`（亦经 MCP 提供） | 默认预览 | `--apply`；MCP `apply: true` |
| `agenthelm nudge install-hooks` | 默认预览 | `--apply` |

`case.py` 的预览复用与执行相同的 schema、路径、项目工具让位和并发前置条件校验。新建 ID 是预测值；并发创建可能占用该序号，执行时会重新分配。更新后再执行时，应传入读到的 `--expect-sha256` 或 `--expect-updated-at`，拒绝覆盖并发修改。

## 例外与边界

- `nudge hook` 是客户端事件消费者，会自动写本机去重状态；不是交互式项目编辑命令，若默认预览就无法正确去重，因此保留无预览例外。其行为由独立 hook 测试覆盖。
- 版本检查的本机缓存、解释器字节码缓存属于运行时缓存；预览不得修改项目文件或用户配置。
- `sop events` **只读**，仅匹配事件并给出建议，不触发执行；`sop run/step/confirm-read` 当前已经有 `--apply`，不属于缺少预览的命令。
- 打开文件、剪贴板、交互式查找器与项目自定义工具不是受管文件编辑 API；自定义工具需自行实现已声明的预览契约。
- `scripts/` 下维护者用的发布工具不属于插件运行时命令盘点；发布命令的预览与确认由发布流程单独约束。

`tests/test_i18n_commands.py` 自动发现全部子命令并要求登记。Case 预览已移入 `PREVIEW`；SOP 事件移入 `READ_ONLY`，步骤与私有读取确认的预览也进入 `PREVIEW`。专用 Case 测试验证成功和校验失败的预览均零写入，并验证原有执行方式继续可用。

English: Case `new` and `update` now accept `--dry-run`. They validate and return a proposed diff without writing. Omitting the flag retains immediate writes for compatibility. New commands continue to preview by default and require `--apply`. Client event hooks retain their documented local-state exception.
