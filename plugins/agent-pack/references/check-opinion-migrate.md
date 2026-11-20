# check 的 Opinion 迁移提示

6.14.0 起 opinion-manager 支持标签按需加载与 `opinion_manager.py migrate`。已有项目的 opinion 不会自动变化；`agenthelm check` 只负责告诉用户“有可迁移的内容”，预览与确认由用户用 `migrate` 自己完成。

## 触发与分级

- 建议项（advisory）：不改退出码，不计入 `summary.up_to_date`，`--strict` 不会因它失败。
- 只在显式的 `agenthelm check` 里运行。会话开始的 hook（`check --hook`）、nudge、`doctor`、`update` 都不调用 migrate。
- 项目有 `opinion.lock.json` 且能找到 opinion-manager（与 agent-pack 同目录的副本优先，其次各客户端已安装的最新版）时，对该项目只运行一次只读预览，零写入，超时 5 秒：

  `python3 <opinion-manager>/runtime/opinion_manager.py migrate --project <项目> --output json`

- agent-pack 与 opinion-manager 只经子进程互调，不 import。`--offline`、`--client` 不影响（migrate 不联网）。

## 结果与提示

- `status: preview`（退出码 0）：提示一条，含目标 Profile 与 schema、更新项摘要、`inbox.incomplete` 条数，以及可直接复制的预览命令（路径按 shell 规则引用，含空格或引号也能直接运行）。提示不含 `--apply` 与确认指纹；确认步骤由 migrate 自己的预览给出。
- `status: cannot-migrate` 且 `reason` 为 `manual-edit-conflict` 或 `catalog-conflict`（退出码 3）：提示一条简短说明与同一条查看命令。文字由 agent-pack 按 reason 生成（跟随界面语言），manager 的 `message` 只放进 JSON。
- 其余一律静默：`unchanged`、`nothing-to-migrate`、`migrated`、`rolled-back`、`refused`、`failed`、其他 reason、没有锁、没有安装 opinion-manager、超时、非 JSON 输出、非对象输出、未知 status、preview 但退出码非 0。

## JSON

`check --output json` 在有提示时多一个顶层数组 `opinion_migrate`；静默时不出现该字段，既有输出逐字节不变。每项：

- `kind`：固定 `"opinion-migrate"`；`status`：`"preview"` 或 `"cannot-migrate"`；`reason`：preview 为 `null`；`command`：预览命令。
- preview：`target{profile, schema_version}`、`updates[{kind, id, current, target, action}]`、`inbox_incomplete`（整数）。
- cannot-migrate：`message`（manager 原文，可能为空）。

不迁移则保持原样，不影响现有行为。迁移本身见 `plugins/opinion-manager/references/versioning.md`「迁移」。
