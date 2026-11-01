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

## 提交与 issue 的关联

- 每个提交的标题带范围：`feat(#30): …`、`fix(#43): …`；一个提交对应多个 issue 时写 `(#33 #39)`。提交正文里，部分完成写 `Refs #N`，完全解决写 `Closes #N`。
- GitHub 只在**合并到默认分支（`main`）**时才按关键词关闭 issue。`dev` 是集成分支，提交里的 `Closes #N` 到不了默认分支就不会生效，所以把关闭关键词写进 `dev` → `main` 的 PR 描述，逐个列出：`Closes #30, Closes #31 …`。
- 发布后核对：CHANGELOG 新版本小节里提到的每个 issue 都要么已关闭，要么在 issue 里留评论写明剩余部分（只部分解决的保持打开）。关闭时的评论列出对应提交的短哈希和 CHANGELOG 小节名，以便追溯。
- 不为补关联而改写已经打过 tag、发布过的历史：tag、npm 溯源、Release 包的校验值都指向这些提交。缺的关联用 issue 评论补。
- 把工作拆给子会话或其他贡献者时，提示词里写明负责的 issue 编号，并要求提交带 `Refs #N`。

## 发布声明门禁

`python3 scripts/release_meta.py --since <上一个发布 tag>` 检查：版本是否随发布内容升级、`release/migrations.json` 是否有对应条目并带 `"declaration": 2`（`actions`、`user_action_required`、`touches`、`default_for_existing`、`template_changes`）、`CHANGELOG.md` 是否提到新版本、模板/骨架/schema 变更是否在 `template_changes` / `touches` 里声明。反例测试在 `tests/test_release_meta.py`。
