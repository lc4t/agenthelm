# 候选版验收记录与待验项

## rc.3 后的输出修正（#50）

| 项目 | 处理与验证边界 |
| --- | --- |
| health 标题条数 | 合并后的视图同时显示可见项数与展开项数；`--details` 和 JSON 仍逐项。中英文夹具覆盖。 |
| 固定 ref 的最新版本 | 文案明确为“插件最新版本”，有 `release.tag` 时仍附目标 tag。 |
| 目标 tag 未知 | 不联网猜测候选版 tag：`market_pin.target` 为 `null`，界面明确说目标版本未知。固定 ref 对不带 ref 的 `marketplace add` 仍构成来源差异，保留诊断；不称为已知旧版本。验收时应向 `latest.json` 补入准确的 `release.tag`。 |
| hook 的版本源 | `check --hook` 使用显式 `--latest`，同时遵守 `--offline` 与 `--client`；读取失败仍不阻断会话。 |
| Claude 更新后的原件位置 | 实际执行了 Claude 步骤的汇总说明旧目录由客户端保留在 `~/.claude/plugins/cache/agenthelm/<插件>/<旧版本>/`，agenthelm 不清理。 |
| 同步盘提示 | `check` 默认合并同步盘与其中的本机专属目录，`check --details` 展开；JSON 不删发现。 |
| 英文下的中文迁移说明 | 暂不机器翻译历史发布声明。在英文界面显示“Migration notes are currently available only in Chinese”；原文保留。后续英文声明设计另行处理。 |
| Codex 候选版市场 | 按下节使用显式 `--ref`，安装器稳定版默认命令保持不变。 |
| runtime 的旁置目录 | 说明只是“其他已安装目录”，版本标签高于 current 不代表内容更新；保留显式切换命令，不自动切换或删除。 |

## Codex 候选版安装

`install --client codex` 默认预览假设稳定版市场跟随 `main`。验收候选版时，先检查目标 tag 的 `latest.json`，然后将预览里的市场添加命令替换为：

```sh
codex plugin marketplace add lc4t/agenthelm --ref vX.Y.Z-rc.N
```

若提示市场已存在，先记录当前市场来源和每个已装插件的版本、installed、enabled 状态，再评估是否执行 `codex plugin marketplace remove agenthelm`。移除对已装插件的影响仍未确认，不将 remove + add 后的一次读回当成证明。添加后安装/升级目标插件，重启客户端，再读回验证。运行时单独通过带版本的 npx 命令试用；候选运行时安装默认旁置，启用需 `--use` 或 `runtime use <版本> --apply`。

## 验收夹具

验收用的临时项目一律由仓库内的 `release/acceptance/mkfixture.py` 生成，验收提示词不再内嵌生成器。它依赖 `plugins/agent-pack/tests` 里的 `make_project`，该目录不在 npm 包内，因此 `<extracted-package-dir>` 必须是目标 tag 的 **GitHub tarball 解压目录**：`python3 -I release/acceptance/mkfixture.py <kind> <outdir> <extracted-package-dir>`，kind 为 `clean | token | home | multisource | credname | wrapped`，末行打印项目根；参数错误退出码 2。`tests/test_acceptance_fixture.py` 逐个 kind 运行它，防止 `make_project` 接口漂移。各 kind 的用例用途见脚本 docstring。

## 云端先行：验收提示词先在 Linux 沙盒逐字预跑

适用于每一份发给实机验收会话的提示词。目的是在占用实机之前，先发现提示词本身的错误，并让每个用例的预期来自真实输出。

1. **逐字预跑。** 提示词里的每个 bash 块，先在 Linux 沙盒里原样执行：用 `release/acceptance/extract_blocks.py` 把提示词（markdown）里的 bash 块按顺序提成一个脚本，先 `bash -n` 检查语法，再在临时 HOME 下运行。

   ```sh
   python3 -I release/acceptance/extract_blocks.py <提示词.md> <输出.sh>
   bash -n <输出.sh>
   HOME=$(mktemp -d) bash <输出.sh>
   ```

2. **预期取自真实输出。** 每个用例的「预跑预期（Linux、临时 HOME，非实机）」从这次预跑的真实输出里取，不凭记忆写；与实机无关的部分（如本机路径）用占位说明。
3. **不一致时先复核环境。** 实机输出与预期不一致，先核对界面语言、`HOME` 是否为临时目录、是否误带了 `python3 -I`，再判断是缺陷还是环境差异。
4. **提示词的固定约定**（6.13.1 预跑时发现的提示词错误）：
   - 全程 `export AGENTHELM_LANG=en` 固定输出语言（Mac 默认可能是中文）；涉及中文输出的用例单独设 `zh`。
   - 命令里不要用 `| head`（Python 会报 BrokenPipe 堆栈），先落盘再取前几行。
   - `grep` 的模式不要匹配到 usage 行（先看 `--help` 输出再定模式）。
   - 产品命令不加 `python3 -I`，参数以 `--help` 为准；只有 `release/acceptance/` 下的脚本用 `-I`。
   - 解压后的目录名带 `-rc.N` 时，`--version` 的标签也带 `-rc.N`；非 rc 目录不带。
   - 零写入反例（改坏 SHA-256）要在仍落后的安装上做：已更新的 HOME 不会再下载，走不到校验。
5. **分工。** Claude Code 与 Codex 由 Mac 上的 Claude desktop 会话验收，提示词第 0 步让它按统筹会话的标题联络统筹（只按名称，找不到时把报告写在对话里并保存为 `~/agenthelm-acceptance/<版本>-report.md`）；Cursor 单独一份，由维护者在 Cursor 里执行。

## 下一个候选版或正式版的实机补测

以下均为**待验收**，本次的夹具与桩客户端测试不替代实机结果。补测结果应记录到发布跟踪 #44，写明操作系统、客户端版本、目标 tag、命令、前后状态与通过/失败证据。

- [ ] 用 rc.1 或以后初始化、内容锁含有效基线摘要的真实项目修改 `AGENTS.md`；从 GitHub Release 下载历史包，验证三方合并成功、用户修改保留、备份可恢复。另行记录无摘要与旧模板不可渲染两个分支。
- [ ] Codex：在 `/hooks` 审阅并信任插件 hook，开启新会话核对 SessionStart 的实际输出与状态。
- [ ] Cursor：核对 `~/.cursor/hooks.json` 中 sessionStart 命令，在新会话确认注入；同时核对 #47 的发布源更新边界。
- [ ] Codex 市场移除：在隔离测试用户或可恢复的配置中，分别记录 remove 前、remove 后（未 add）、重新 add 后的 installed、enabled、版本与配置文件，确认已有插件是否仍有效。结论出来前保留 INSTALL.md 的“未确认”。
