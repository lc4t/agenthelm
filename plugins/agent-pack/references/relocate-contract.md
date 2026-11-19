# `agenthelm relocate` 契约（schema_version 1）

English: [relocate-contract.en.md](relocate-contract.en.md)

```
agenthelm relocate <ref> --to <客户端>:<作用域> [--mode copy|link] [--project 目录] [--allow-widen] [--keep-source]
                   [--confirm-project-content] [--output json|text] [--lang zh|en|auto]
                   [--apply --expect-plan <plan_digest>]
```

只处理 Skill，`<ref>` 取 `agenthelm inventory` 的 `skills[].ref`（`skill:<名>@<客户端>:<作用域>`，主存储之外带 `#agents`）。**默认预览，零写入**；`--apply` 必须同时带预览给出的 `--expect-plan`，重新计算的 `plan_digest` 与之不同就拒绝（退出码 1，不写任何文件）。退出码：0 成功或预览无阻止项；1 有阻止项、指纹不一致或写入失败；2 参数错误。只支持 macOS 与 Linux。

## 目标存储

`claude:user` → `~/.claude/skills`；`claude:project` → `<项目>/.claude/skills`；`codex:user` → `~/.agents/skills`；`codex:project` 与 `cursor:project` → `<项目>/.agents/skills`；`cursor:user` → `~/.cursor/skills`。源已在目标客户端原生读取的存储里时，`plan.status` 为 `native`，零写入。

## 规则

- **绝不覆盖**：目标已有同名条目 → `blocked: target-exists-identical|target-exists-different`，不改名、不合并。
- **复制与链接**：跨作用域（家目录 ↔ 项目）一律复制（`--mode link` 被 `link-cross-scope` 阻止）。同一作用域内 `--mode link` 建相对符号链接，并隐含保留源。路径含 iCloud / Dropbox / OneDrive 等同步盘目录名（启发式）时降级为复制，`warnings` 含 `sync-folder-demoted`，最后一步 `promise` 为 `cannot-promise`。
- **源的处置**：默认校验通过后归档源到 `~/.agent-pack/recovery/<时间戳>-relocate-<名>/skills/<名>`（归档是移动，不是删除；目录布局与 `reconcile --retire-skill` 的恢复目录相同：`<时间戳>-<标签>/skills/<名>`，测试断言两者一致）；`--keep-source` 复制并保留源。结果给出 `recovery_dir` 与手工恢复的单行 `mv` 步骤，本版没有 `restore` 命令。
- **写入顺序与回滚**：恢复目录（0700）→ 同目录临时名复制 → 读回校验（逐文件相对路径与内容摘要，`SKILL.md` 摘要与源一致）→ 重命名到位 → 归档源。任一步失败，只移除本次自己创建的目标，已归档的源移回原位，保留恢复目录。目录内指向目录外的符号链接 → `source-has-external-symlink`。
- **放宽**（项目级 → 用户级）需要 `--allow-widen`，`direction: "widen"`，文本预览在终端里标红。
- **进入项目仓库**（用户级 → 项目级）：`confirmations` 列出「将进入项目目录，可能被提交到版本库」，`--apply` 还必须带 `--confirm-project-content`；许可证与是否含私有内容由使用者判断，工具不判断。
- 证据不是 `file` 的源（目录损坏、`SKILL.md` 过大）只读：`source-not-file-evidence`。MCP 条目：`unsupported-kind`。

## JSON

`{schema_version, command: "relocate", mode: "preview"|"apply", plan, applied}`。`plan`：`ref`、`kind`、`source`、`target`、`mode`、`retire_source`、`direction`（`narrow|same|widen`）、`status`（`ready|blocked|native`）、`steps[{id,action,promise,...}]`、`blocked[{code,message,...}]`、`warnings[{code,message}]`、`confirmations[{id,text,flag}]`、`recovery_base`、`plan_digest`、`next.apply`。`promise`：`certain`（文件级事实）、`best-effort`（需重启客户端或重新进入项目才能确认出现）、`cannot-promise`（工具只保证文件改动，不保证客户端行为）。`plan_digest` 与界面语言无关（不含 `message` / `text`）。`applied.status`：`applied|rolled-back|refused|native`。键只追加；输出不含文件内容，只有名字、路径（`~` 与 `<project>` 缩写）与摘要。
