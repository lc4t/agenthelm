# 发布验证工具

统筹每个版本都要手写的对照脚本，沉淀为三个只用标准库、不联网、Python 3.11–3.13 可运行的工具。目录 `release/verify/` 不进 npm 包（包只含 `bin`、`runtime`）也不进发布 tarball（`.gitattributes` 对其 `export-ignore`；发布工作流对 tag 做 `git archive`）。`tests/test_release_verify.py` 固定这两点，并对每个工具跑一次自检和一次真实仓库用例。公共引擎在 `_common.py`；夹具沿用 `release/acceptance/mkfixture.py`。

## 为什么每个工具都带自检

6.15.0 的教训：`clients` 命令被多写了不存在的 `--latest`，新旧两边都报参数错误（退出码 2），对照脚本据此判「相同」。因此三个工具都先判断「命令真的执行了」，两边同样失败时报**空比较**（退出码 3）而不是相同，并用 `--self-test` 注入差异、注入失败来证明这一点。

退出码约定：`0` 全部相同；`1` 有差异；`2` 用法错误；`3` 空比较（没有任何东西被真正比较）。

## compare_runs.py：新旧对照

```bash
python3 release/verify/compare_runs.py                        # origin/dev 对 HEAD，默认清单
python3 release/verify/compare_runs.py --old v6.14.0 --new-dir . --manifest my.json --report out.json
python3 release/verify/compare_runs.py --self-test
```

- 旧、新各取一份工作树（`git archive <ref>`，本地数据）；`--old-dir/--new-dir` 直接指定目录（如 `.` 表示含未提交修改的当前树）。
- 每条命令在两边各有**全新的独立 HOME**，并且旧、新使用**同一路径**（旧一侧运行完后删除再跑新一侧），依赖项目路径派生名称的输出不会被当成差异。
- 比较：标准输出、标准错误、退出码、桩（`stubs` 里列出的假客户端，放在 PATH 最前，记录收到的 argv 与工作目录）、执行后 HOME 与项目目录的文件树哈希（排除 `.git`）。
- 归一化：时间戳（ISO 8601、`YYYY-MM-DD HH:MM:SS`、10 位 epoch）换成 `<TS>`；各目录按最长路径优先换成 `<TREE> <HOME> <PROJECT> <WORK> <FIXTURE>`。名称与额外正则可在清单 `normalize.names` / `normalize.regex` 配置。
- 清单（JSON，或定义 `MANIFEST` 的 `.py`）每条命令：`name`、`argv`、可选 `fixture`（`mkfixture.py` 的 kind）、`stubs`、`stdin`、`env`、`expect_exit`、`expect_nonzero`、`expect_output`、`stdout_json`、`timeout`。argv 里的 `python3` 即当前解释器，`{tree} {runtime} {home} {project} {work} {bin} {latest}` 按边替换。默认清单 `manifests/compare_runs.json`。
- 「真的执行了」：默认退出码 2（参数错误）、126、127（命令不存在）、超时都算没执行；确需如此的命令写 `expect_exit`。两边都没执行 → `EMPTY`；仅一边没执行 → `DIFFERENT` 并写明原因与该边 stderr 末行。
- 输出：JSON 报告到标准输出（或 `--report`），每条命令一个状态加差异细节；人读摘要写到标准错误。

自检注入：新一侧多输出一个字符、多写一个文件、多一个退出码、桩多收一个参数、桩多输出；两边都报参数错误、两边命令不存在；`expect_exit` 显式接受 2。

## tag_matrix.py：逐 tag 回归矩阵

```bash
git fetch --tags origin
python3 release/verify/tag_matrix.py --latest windowed.json --baseline latest.json [--since v6.5.0] [--tags v6.14.0,v6.15.0] [--report out.json]
python3 release/verify/tag_matrix.py --self-test
```

- 取本地 `git tag` 里全部 `vX.Y.Z`（不含 `-rc`），用 `git archive` 取出该 tag 的 `plugins/agent-pack/runtime`，在独立进程里同时读 `--baseline`（全量参照）与 `--latest`（待验）。
- 对基线里每个插件、每个出现过的版本：该 tag 自带的 `plugin_findings` 在两份文件上的 `status`、不兼容判定、变更类别、各步骤（版本，是否不兼容）必须一致；不得抛异常；待验文件产生的 finding 里不得出现基线没有的 `None` 字样（正文里本来有这个词，所以只比较差异）。另离线运行该 tag 的 `check`（两份文件各一次，json 与 text）：不崩溃、不多出 `None`、`summary.breaking` 一致。
- 每个 tag 一行 `OK/FAIL/SKIP/EMPTY`。没有 agent-pack 运行时或没有 `plugin_findings` 的早期 tag 记 `SKIP`（不算失败）。基线没有不兼容判定（`--min-breaking`，默认 1）时记 `EMPTY`，避免空转。
- 缺 tag：提示 `git fetch --tags origin`，退出码 3；设置 `AGENTHELM_REQUIRE_TAGS=1` 时退出码 1。
- 已知限制：`check` 的冒烟在空 HOME 下运行（没有已装插件），迁移判定的主体由 `plugin_findings` 逐版本覆盖；能读 `migrations_window` 并拉全文的客户端（6.15.0 起）在待验文件丢失标记时仍会通过，因为它会取全量，这是预期行为，自检用的是不认识窗口的 tag。

生成窗口化的 `latest.json`（纯函数 `window_migrations`）：

```python
import json, sys; sys.path.insert(0, "scripts")
import release_workflow as w
latest = json.load(open("latest.json"))
floor = {name: item["version"] for name, item in latest["plugins"].items()}   # 最激进：全部在窗口外
migrations, window = w.window_migrations(latest["migrations"], floor)
json.dump({**latest, "migrations": migrations, "migrations_window": window}, open("windowed.json", "w"))
```

自检使用一个临时 git 仓库（带假运行时与两个 tag）：窗口化文件通过；丢失不兼容标记的文件失败；基线无不兼容判定得空比较；`--since`、缺 tag 的软/硬失败。

## payload_compare.py：hook 载荷对照

```bash
python3 release/verify/payload_compare.py [--old origin/dev] [--new HEAD] [--client claude] [--cases cases.json] [--report out.json]
python3 release/verify/payload_compare.py --self-test
```

- 入口 `python3 <tree>/plugins/agent-pack/runtime/agenthelm.py nudge hook --client <客户端> --event <事件>`，载荷 JSON 走标准输入（协议见 `agent_pack_nudge.py` 与各 `drivers/*.json` 的 `hooks`）。
- 不带 `--cases` 时，从新一侧每个驱动的 `hooks.events` 与 `hooks.payload`（项目目录键、会话键）生成载荷；项目夹具含 `.opinion/`、git 仓库和一个待确认候选（用本仓库的 opinion-manager 生成，两边共用同一份），使 session-start / prompt-submit / stop 都有话可说。`--no-seed` 去掉候选。
- `--cases` 为 JSON 列表（或目录里的多个 `*.json`）：`{"name","client","event","payload","expect_output","env"}`；载荷里的 `{project}` 换成项目目录。
- 比较：标准输出按 JSON 解析后逐字段（列出路径），退出码、标准错误、HOME 与项目里的副作用文件。
- 「真的执行了」：hook 找不到项目、事件名未知时会静默，两边都静默看起来像「相同」。所以每个用例必须在某一边有输出，或显式写 `expect_output: false`（如驱动声明 `stop_prompt: off` 的 stop），否则 `EMPTY`；退出码必须是 0，未知事件的参数错误（2）算没执行。
- 已知限制：旧一侧不认识的客户端（如在 6.13.1 上的 `gemini`）会被报告为仅旧侧未执行的差异，这是预期的真实差异；自动生成的载荷只覆盖驱动已声明的键，客户端自己的新字段需用 `--cases` 补。
