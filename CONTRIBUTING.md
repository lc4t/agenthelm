# 贡献指南

## 如何新增夹具（非空目录与升级场景的测试）

冲突与升级相关的测试都在 `plugins/agents-init/skills/agents-init/scripts/` 下，文件名 `test_*.py`（不随插件发布，不影响版本门禁）。

| 文件 | 内容 |
| --- | --- |
| `test_fixtures.py` | 非空目录夹具矩阵、写入类不变量的通用断言、备份与原子性 |
| `test_upgrade_matrix.py` | 用历史 tag 生成旧版本项目，模拟用户修改，再用当前版本升级 |
| `test_ai_contract.py` | 标题启发式分类、AI 录制响应的结构校验、落盘只经由确定性命令 |

### 加一个“接管前的用户目录”夹具

1. 在 `test_fixtures.py` 里写一个 `def _my_case(c: Ctx) -> None`：用 `c.write("相对路径", 文本)` 建项目里的文件，`c.outside(...)` 建项目目录以外、需要保持不变的路径（上级目录文件、符号链接目标），`c.link(...)` 建符号链接。
2. 在 `FIXTURES` 里加一行 `Fixture("kebab-case-名字", _my_case)`。`expect="refuse"` 表示该状态下命令应以 `invalid-request` 拒绝且不写任何东西。
3. 不用再写断言：`test_every_fixture_obeys_the_write_invariants` 会对它自动检查 scan/adopt 预演零写入、`--apply` 后用户条目（字节、符号链接目标、可执行位）不变、重复执行幂等；并在第 1、3、7 次写入处注入失败，检查不留半成品。
4. 夹具专属的行为（例如“链接目标里不得被写入”）另写一个小用例，复用 `fx.cli` / `fx.tree` / `fx.changed`。

### 加一个通用不变量

写成 `assert_xxx(test, ctx, ...)` 放在 `test_fixtures.py` 的“通用不变量断言”一节，其他文件用 `import test_fixtures as fx` 复用。

### 加一个升级场景

在 `test_upgrade_matrix.py` 的 `UpgradeMatrixTests` 里加一个 `@per_tag` 方法，签名 `(self, tag, base, root)`：`root` 已是该 tag 的旧版 `init_project.py` 生成的项目。模拟用户修改后用 `self.refresh(...)` / `fx.cli(...)` 调用**当前**版本，断言处置（`unchanged / local-only / clean / modified / missing / kept-by-decision`）与用户字节。要覆盖更多历史版本，把 tag 加进 `TAGS`。

取不到 tag 时：本地整组 skip 并写明原因（`git fetch --tags origin`）；设置 `AGENTHELM_REQUIRE_TAGS=1` 则改为失败。CI 的 `actions/checkout` 使用 `fetch-depth: 0`，会同时取回全部 tag；`tests.yml` 里有一个专门的步骤带着该环境变量运行升级矩阵，防止 CI 静默跳过。

### 约定

- 所有命令在临时 HOME 里运行，不读写真实的 `~/.agent-pack`、`~/.config`。
- 不运行真实的 claude / codex / cursor。
- 与平台相关的断言（可执行位、符号链接、路径大小写）要写明；macOS 上的行为由 CI 的 `macos-latest` 矩阵验证。

## 变更片段与发布门禁

普通功能和修复从 `dev` 建分支，PR 目标为 `dev`。不要改最终版本、内容摘要、`latest.json`、市场清单、迁移汇总或 CHANGELOG 的发布段；新增一个唯一的 `release/changes/<issue>-<slug>.json`，说明受影响插件、等级与迁移声明。CI 使用 `python3 scripts/release_meta.py check-changes --base origin/dev` 校验片段、变更覆盖及版本载体未被手改。已合并片段不可编辑，修正应另添片段。

格式、版本规则、完整发布步骤、手工恢复方式见 [release/VERSIONING.md](release/VERSIONING.md)。发布者使用 `release plan --explain` 审查，`release --apply` 才会生成一个本地发布提交；不自动打 tag、推送、发布或合并。`check-release --since <上一个正式 tag>` 验证发布声明与所有派生文件。原 `--since`/`--write` 仍作为兼容检查和生成工具，不作为功能 PR 的门禁。
