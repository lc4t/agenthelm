# 本机专属目录（local-only）

项目放在 iCloud Drive、Dropbox、OneDrive、Google Drive 等同步目录里、在多台设备上使用时，`.venv`、`node_modules`、工具缓存这类**可重建的大目录**不该进同步盘：同步慢、产生冲突副本，不同系统或架构的二进制还会互相覆盖。`agenthelm local-only` 把它们放到每台设备自己的 `~/.local/share/agenthelm/local-only/` 下，在项目里留一个软链接；并能检查出“设备 A 装好了、设备 B 还没装”这类差异。

不在同步目录里的项目不受影响：没有同步服务、也没有 local-only 配置时，`check` 直接返回空结果，不扫描。

## 三条硬约束

1. **链接文本必须在所有设备上相同。** 软链接本身会随项目同步，所以目标路径只由三样东西拼成：项目里记录的 `home`、`project_id`、项目内相对路径——`<home>/.local/share/agenthelm/local-only/<project_id>/<相对路径>`。不读 `XDG_DATA_HOME`。
2. **各设备的 HOME 绝对路径必须一致**（例如都是 `/home/<同一个用户名>`）。`home` 记在项目配置里；不一致时 `check` 报“HOME 与项目约定不一致”，`fix` 拒绝建立或修改任何链接。
3. **工具只给方案和预览，改文件必须 `--apply`。** 移动失败整体撤销；旧配置先备份到 `~/.agent-pack/recovery/<时间>-agenthelm-local-only/`。

## 怎么处理每一类

| 类别 | 例子 | 处理 |
| --- | --- | --- |
| 依赖安装物（可外置） | 含 `pyvenv.cfg` 的目录（不管叫什么）、`node_modules`（yarn 经典版、bun、未知管理器） | `fix` 移到本机目录，原位置建软链接 |
| 依赖安装物（只报告） | `node_modules`（npm、pnpm、yarn 2+）、`.tox`、`.nox`、`.terraform`、`.gradle` | 报告；`.tox` 等没有实测过软链接，确认后可用 `--externalize` 指定 |
| 工具缓存 | `__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.hypothesis`、`.cache`、`.eslintcache`、`.parcel-cache`、`.turbo`，以及任何带 `CACHEDIR.TAG` 的目录 | 报告并给出环境变量写法，不移动 |
| 运行中进程的文件 | `*-wal`、`*-shm`、`*.pid`、`*.sock`（`*.lock` 是依赖锁，必须同步，不算） | 只报告 |
| **默认保持同步** | `dist`、`build`、`out`（构建产物，别的设备不必重新构建）、`.claude`、`.cursor`、`.idea`、`.vscode`（本机工具状态） | 不报告；体积由阈值兜底；可用 `keep_synced` 调整 |
| 不碰 | `.git`、`.svn`、`.hg` | 不进入 |

识别靠**标记文件**，名字只作兜底：`pyvenv.cfg` 是虚拟环境，`CACHEDIR.TAG` 是工具声明的缓存。“被 git 忽略”只能作候选，不能自动归类——`.env` 也被忽略，但不可重建。

## 实测兼容性

2026-10-29，Linux 容器，软链接指向项目外目录：

| 工具 | 结果 |
| --- | --- |
| uv 0.11.32 | 正常，链接保留；悬空链接时 `uv sync` 报 `File exists (os error 17)`，先建目标目录即可 |
| 普通 venv + pip | 正常（脚本里的路径经过链接解析） |
| yarn 1.22.22、bun 1.4.4 | 正常，重复安装后链接保留 |
| **npm 10.9.4** | `install` 与 `ci` 都把链接**换成真实目录**（相对、绝对、无变化都一样） |
| **pnpm 10.28.0** | 安装失败（退出码 236），并删掉了链接目标目录 |
| `PYTHONPYCACHEPREFIX`、`RUFF_CACHE_DIR`、`MYPY_CACHE_DIR`、`pytest -o cache_dir`、`UV_CACHE_DIR` | 生效，项目内零残留 |

结论：同步目录里的 Node 项目尽量用 **yarn（经典版）或 bun**；npm、pnpm 的 `node_modules` 没有已验证的外置办法，工具只报告，不替你换包管理器（那会改 lock 文件）。项目不在同步目录里时随便用。

**未确认**（需要在真机核对，工具不依赖它们给结论）：iCloud、Dropbox、OneDrive 是否同步软链接本身；yarn 2+（PnP 或 node-modules 链接方式）；iCloud“桌面与文稿”同步的项目路径上看不出来（需要声明 `sync_service`）；各服务冲突副本的命名（目前只识别 Dropbox 的 `conflicted copy`、iCloud 的 `.icloud` 占位文件）；macOS 上同步状态（是否在同步、账号状态）没有找到可靠的检测办法，工具不报告。

## 命令

```bash
agenthelm local-only check [--mode quick|standard|full] [--strict] [--output json]
agenthelm local-only check --fingerprint [--explain 对方的.json]
agenthelm local-only fix [--externalize 路径]… [--raise-limit 目录=MB[/文件数]]… [--service icloud|dropbox|onedrive|google-drive|syncthing|other|none] [--rebuild] [--apply]
agenthelm local-only config [--output json]
```

- `check` 默认 `full`（最多 120 秒）；`standard`（6 秒预算）用于 `agenthelm check` / `health`；`quick`（浅层两级目录、不统计体积，1.5 秒）用于会话开始钩子。超过预算会在“说明”里标出只扫了一部分。
- `check --strict`：有“需要处理”的项时退出码为 1。`agenthelm check --strict` 与会话开始的一行提示也把它们算作未处理。
- `fix` 不带选项：处理所有推荐项（venv、可外置的 node_modules，以及这台设备上缺失的已声明条目）。首次会创建项目配置。
- `fix --rebuild`：外置后在本机运行重建命令，由锁文件推断（`uv.lock`→`uv sync`，`poetry.lock`→`poetry install`，`yarn.lock`/`bun.lock(b)`→`install --frozen-lockfile`）。找不到锁文件或工具就跳过。重建失败不撤销外置。
- 决定（不再提示）沿用 `agenthelm fix --skip <ID> --reason …`；`local:` 开头的 ID 只记在这台机器上。

## 配置

项目配置 `.agents/moe.sakanano.agent-pack/local-only.json`（随项目同步；`fix` 创建，之后你可以手改）：

```json
{
  "schema_version": 1,
  "project_id": "p-5bcae0a8ad",
  "home": "/home/me",
  "sync_service": "icloud",
  "items": [{"path": ".venv", "kind": "venv"}],
  "limits": {
    "warn_ratio": 0.8,
    "dir_mb": 200,
    "dir_files": 20000,
    "overrides": {"docs": {"dir_mb": 1000}}
  },
  "keep_synced": ["dist", "build", "out", ".claude", ".cursor", ".idea", ".vscode"],
  "ignore": ["vendor"],
  "extra_local_only": ["data/cache"]
}
```

- `project_id`：由 `home` 与项目路径派生，预览与执行一致；两台设备同时初始化写出相同内容。
- `sync_service`：声明后不再靠路径猜；`none` 表示不在同步盘里。
- `limits`：每个**顶层目录**（及项目根的散文件）的体积与文件数阈值；`warn_ratio` 到达阈值的这个比例就开始提示。`overrides` 按目录覆盖。这些数字是默认的启发值，没有任何同步服务的官方限额作依据。
- `keep_synced`：这些名字（或相对路径）永远不报告，覆盖标记判定。
- `ignore`：完全不扫描。`extra_local_only`：额外当作本机专属的目录。
- 用户级覆盖：`~/.config/agenthelm/config.json` 的 `local_only` 段，可写 `limits`、`keep_synced`、`ignore`。优先级：内置默认 < 用户配置 < 项目配置。项目配置优先，是因为阈值会影响各设备得出的结论，应当随项目走。

## 体积快到阈值时

达到 `warn_ratio` 的目录出现一条“接近阈值”的发现（达到阈值则是“需要处理”）。工具不替你选，列出：

1. 确认这个体积正常，提高阈值（`fix --raise-limit 目录=MB/文件数 --apply`，写入项目配置，建议值为现值的 1.25 倍与旧阈值 2 倍中的较大者）；
2. 不再提示（`fix --skip local:size:<目录> --reason …`，只记在这台机器；状态或阈值变化后会重新提示）；
3. 在同步服务里把它设为不同步，或移出同步目录（工具不替你做）。

不提供“外置”：普通内容外置后，其他设备看不到它。

## 发现 ID

| ID | 含义 | 级别 |
| --- | --- | --- |
| `local:sync-folder` | 项目位于同步盘（总览） | 建议 |
| `local:home-mismatch` | HOME 与项目记录不一致 | 需要处理 |
| `local:real:<路径>` | 同步目录里有真实的本机专属目录 | 需要处理 |
| `local:link:<路径>` | 已声明条目的状态：未安装、尚未建立、又变回真实目录、指向别处、被文件占用 | 未安装与尚未建立为建议，其余需要处理 |
| `local:pm:<目录>` | 同步目录里用 npm 或 pnpm | 建议 |
| `local:cache` | 带环境变量写法的工具缓存目录（一组） | 建议 |
| `local:size:<目录>` | 体积接近或超过阈值 | 接近为建议，超过需要处理 |
| `local:runtime-files` | `-wal`/`-shm`/pid/socket 文件 | 建议 |
| `local:conflict-copies` | 冲突副本 | 建议 |
| `local:local-only-config` | 配置有错误（有错误的项不生效） | 建议 |

“需要处理”在 `agenthelm check` 的汇总里让 `up_to_date` 变为 false（`--strict` 退出码 1）；`health` 里仍是“建议处理”，不改变 `health` 的退出码。

## 多设备流程

1. **设备 A（首次）**：`agenthelm local-only check` 查看；`agenthelm local-only fix` 预览；确认后 `fix --apply`。项目配置和软链接随同步盘到达其他设备。
2. **设备 B**：同步完成后运行 `agenthelm local-only check`。软链接在、本机目录不在时报“这台机器还没有安装 …”（此时直接运行 `uv sync` 会报 `File exists`）；`fix --apply --rebuild` 建本机目录并按锁文件安装。同步服务不同步软链接时报“尚未建立”，同一条 `fix --apply` 补上。
3. **对比**：两台设备各运行 `agenthelm local-only check --fingerprint --output json`，用 `--explain 对方的.json` 看差在哪里（有差异退出码 1）。`agenthelm check --fingerprint` 在项目配置了 local-only 时也带本机环境段，`--explain` 一并比较。指纹只含 HOME、链接状态与链接文本，不含平台和体积——macOS 与 Linux 不同属于正常。

## 给 Agent 的协议

- 用 MCP 工具 `agent_pack_cloud_sync_safety_check`（只读）获取发现；不要自己 `mv` / `ln -s` 项目里的目录。
- `attention: true` 的发现需要告诉用户；每项的 `options` 里有可执行的命令，**移动目录与改配置要用户同意后**再运行 `agenthelm local-only fix … --apply`。
- 体积类发现必须把选项交给用户决定，不要替用户提高阈值。
- 项目没有同步服务、也没有配置时工具返回空发现，不要自己提示“需要外置”。
