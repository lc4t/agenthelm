# Agent 能力配置

仅在盘点、迁移、同步或验证 Agent Plugin、Skill、MCP 时读取本参考。

## 可移植项目结构

`agents-init v5` 创建以下能力包：

```text
.agents/
├── plugin.json
├── skills/
├── mcp.json
└── moe.sakanano.agent-pack/
    └── project.json
```

- `plugin.json` 与 `mcp.json` 面向 Agent Plugins 1.0.0。
- `skills/` 的直接子目录包含 `SKILL.md`。
- `project.json` 是客户端扩展，也是机器可读的 Project Profile。
- 个人偏好和凭据禁止进入可移植公开模板。

## CLI

从已加载 Skill 解析插件根目录，再运行：

```bash
python3 <plugin-root>/runtime/agent_pack_config.py --output json inventory --source project:/path/to/project
python3 <plugin-root>/runtime/agent_pack_config.py --output json inventory --source client:codex
python3 <plugin-root>/runtime/agent_pack_config.py --output json inventory --source client:cursor
python3 <plugin-root>/runtime/agent_pack_config.py --output json inventory --source client:claude
python3 <plugin-root>/runtime/agent_pack_config.py --output json doctor --project /path/to/project
python3 <plugin-root>/runtime/agent_pack_config.py --output json doctor --project /path/to/project \
  --client claude --client codex
```

`doctor --client <客户端>`（可重复）在项目检查之外，再把该客户端装的东西和项目权威比一遍，结果放在 `clients.<客户端>`：

- `missing`：项目有、客户端没有；`different`：内容不同；`stale`：本包装过、项目里已经没有；`dangling`：链接或 MCP 参数指向不存在的文件；
- `coupled`：Claude 的安装记录指向别的客户端的包目录（例如 Codex 包）；
- `overrides`：只是本机多了或改了认证类值（`headers` / `env`），只报键名，不算问题；
- `native`：用户自己配置、不属于本包的条目，只列出；`excluded`：被 `mcp_client_policy` 排除的条目。

前四类和 `coupled` 计入 `issues`，`overrides` 计入 `warnings`。不加 `--client` 时行为与以前相同。

runtime 尚未安装时，bootstrap 包含本 Skill 的插件：

```bash
python3 /path/to/agent-pack/runtime/agent_pack_config.py --output json bootstrap \
  --plugin /path/to/agent-pack --client codex
python3 /path/to/agent-pack/runtime/agent_pack_config.py --output json bootstrap \
  --plugin /path/to/agent-pack --client codex --apply
```

`--client` 可取 `codex`、`cursor`、`claude`。Claude Code 没有 Agent Plugins 加载器，`bootstrap --client claude` 把插件投影成它实际读取的三处：

- `~/.claude/skills/<skill>` → 源插件 `skills/<skill>` 的符号链接；
- `~/.claude/agent-pack/plugins/<name>` → 源插件根目录的符号链接（安装记录，`inventory --source client:claude` 从这里读已安装版本）；
- 插件 `mcp.json` 中每个 server 写入 `~/.claude.json#mcpServers`（user scope）：`${PLUGIN_ROOT}` 展开为安装记录路径，`streamable-http` 映射为 `http`，丢弃 Claude 不支持的 `cwd`；其他 `${VAR}` 原样保留，由 Claude 启动时从进程环境展开。写入保留文件原权限。

`bootstrap` 在 Claude 端是**活链接**，不固定副本：源目录改动立即生效，版本即源 `plugin.json#version`。

- `--skill <名称>` / `--mcp <名称>`（可重复）只处理选中的组件。安装记录仍会建立，因为 MCP 参数要经过它。
- 本机条目与插件版本只差认证类值（`headers` / `env` 里插件没有的键、占位符或凭据类键）时，判为 `keep-override` 并保留本机值；还有其他不同时需要 `--replace`，替换后同样保留本机认证。加 `--reset-overrides` 才丢弃本机值。结果只列出保留的键名（`kept_overrides`），不输出值。
- 旧内容进入 `~/.agent-pack/recovery/<stamp>-claude-<name>/`（`replaced-skills/`、`replaced-plugins/`、`replaced-mcp.json`，后者权限 `0600`）。装完需重启 Claude Code 才会加载新 Skill / MCP。

**项目同步到 Claude 用 `sync` / `reconcile --client claude`**，不要用 `bootstrap` 指向别的客户端的包：

- 按项目和 Claude 的 `mcp_client_policy` 单独生成一份包，放在 `~/.claude/agent-pack/packages/<name>/`；安装记录 `~/.claude/agent-pack/plugins/<name>` 改为指向它；Skill 链接与 MCP 条目都指向这份包。
- `~/.claude/agent-pack/state/<name>.json` 记下本包装进 Claude 的 Skill 与 MCP。项目里新增的会加上，改了的会更新，删掉或被策略排除的会移除；**只动清单里的条目**，用户自己配置的条目从不改动。
- 与用户自己的同名条目冲突时停止，加 `--replace` 才替换（旧内容进恢复目录）。本机认证默认保留，`--reset-overrides` 丢弃。
- 安装记录原来指向别的目录（旧做法：`bootstrap --plugin ~/.agents/plugins/plugins/<name> --client claude`）时，第一次同步自动迁移：旧记录指向写入恢复目录的 `record.json`，指向旧目录的 Skill 链接改为指向新包。此后 Codex 端的包怎么变、是否删除，都不影响 Claude。
- 默认只预览；`--apply` 时整笔操作写入同一个恢复目录，任一步失败全部回滚。

在来源与已初始化项目之间迁移选中的组件：

```bash
# 预演
python3 <plugin-root>/runtime/agent_pack_config.py --output json transfer \
  --from client:codex --to-project /path/to/project --skill example-skill

# 审阅预演结果后再应用
python3 <plugin-root>/runtime/agent_pack_config.py --output json transfer \
  --from project:/path/to/source --to-project /path/to/destination \
  --skill example-skill --mcp example-server --apply
```

为客户端构建并安装项目的可移植能力：

```bash
# 预演（--client 可取 codex、cursor、claude）
python3 <plugin-root>/runtime/agent_pack_config.py --output json sync \
  --project /path/to/project --client cursor

# 获得授权后应用
python3 <plugin-root>/runtime/agent_pack_config.py --output json sync \
  --project /path/to/project --client cursor --apply
```

迁移时使用 `reconcile` 分类目标，并归档明确选择的旧组件。默认只生成只读计划：

```bash
python3 <plugin-root>/runtime/agent_pack_config.py --output json reconcile \
  --project /path/to/project --client cursor \
  --retire-skill old-helper --retire-skill legacy-sync

python3 <plugin-root>/runtime/agent_pack_config.py --output json reconcile \
  --project /path/to/project --client cursor \
  --retire-skill old-helper --retire-skill legacy-sync --apply
```

只退役旧组件、不安装或替换项目包时加 `--retire-only`（至少需要一个 `--retire-skill` / `--retire-plugin`）：

```bash
python3 <plugin-root>/runtime/agent_pack_config.py --output json reconcile \
  --project /path/to/project --client cursor --retire-plugin project-runtime --retire-only --apply
```

### 退役插件的完整清单（`--retire-plugin`）

退役一个插件会同时处理它在该客户端的所有入口；预览（不带 `--apply`）逐项列出，`--apply` 先归档到 `--recovery-root`（默认 `~/.agent-pack/recovery/`）再移除，失败时整体回滚：

| 客户端 | 自动处理（`operations`） | 不自动处理（`residuals`，每项含 `path` 与 `manual`） |
| --- | --- | --- |
| Claude | `archive-plugin`（安装记录）；`archive-skill`（`~/.claude/skills/` 中**仅指向该插件目录**的符号链接，其它链接、真实目录与项目包的链接不动） | 仍指向该插件的 `~/.claude.json` MCP 条目（`claude mcp remove --scope user <名称>`）；由 Claude Code 管理的 `installed_plugins.json` 记录（`claude plugin uninstall`） |
| Codex | `archive-plugin`（缓存版本）；`archive-plugin-copy`（`~/.agents/plugins/plugins/<名称>/`）；`remove-marketplace-entry`（个人市场 `marketplace.json` 中该插件的条目，其它条目与缩进保持） | 缓存里的其它版本、`~/.codex/config.toml` 里的 `[plugins."<名称>@…"]` 表（用户配置，不自动编辑） |
| Cursor | `archive-plugin`（本地插件目录） | 无 |

没有残留项时输出不含 `residuals`；有残留项时文本输出以 `! residual …` 逐行列出（不回显文件内容）。

结果把组件分为 `exact`、`conflict`、`missing`、`native_only` 与 `blocked`。替换包和明确退役的组件移动到 `~/.agent-pack/recovery/`；事务失败时自动恢复。

Codex 同步会写入 `~/.agents/plugins/plugins/<name>` 可移植包、`.codex-plugin` adapter 与 `~/.agents/plugins/marketplace.json` 的 `personal` 条目。之后需在 Codex 桌面版的插件设置里启用 / 重新同步 `<name>@personal`（并确认 `~/.codex/config.toml` 有 `[plugins."<name>@personal"] enabled = true`）来激活；同步只更新 marketplace 源，Codex 自身缓存 `~/.codex/plugins/cache/personal/` 要到下次激活或重启才追平。homebrew 版 `codex-cli` 无 `codex plugin` 子命令，`plugin` / `marketplace` 命令与 UI 随 Codex 版本变化，以当前 Codex 文档为准。Cursor 同步会在本地插件根目录安装真实的 Agent Plugin 目录。

## MCP Tool

随附的 stdio server 暴露以下 Tool：

- `agent_pack_inventory`
- `agent_pack_doctor`
- `agent_pack_transfer`
- `agent_pack_sync`
- `agent_pack_bootstrap`
- `agent_pack_reconcile`

所有会修改状态的 MCP Tool 默认使用 `apply=false`。必须先展示并审阅操作计划，才能使用 `apply=true` 重试。

## 安全与碰撞规则

- 禁止把原生客户端 MCP 配置中的凭据复制进可移植包。
- 拒绝包含疑似密钥环境变量、header 或 token 字面值的 MCP 条目。
- 迁移期间禁止跟随指向来源 Skill 目录之外的 symlink。
- 禁止静默覆盖现有 Skill、MCP 名称、Agent Plugin、客户端包或 marketplace 条目。
- 在 `native_mcp_sources` 中声明历史或客户端专属 MCP 文件。可移植条目成为打包候选；包含密钥的条目保持为 `blocked`/doctor warning，并留在客户端本地。
- 原生 MCP 来源可以使用 `__PROJECT_DIR__` 与 `__REPO_ROOT__`；源文件保留占位符，生成客户端包时解析为当前本地项目路径。
- **stdio 的环境变量引用**：把 `credential_env_file` 设置为项目相对路径的 dotenv 文件，权限必须为 `0600`。生成包只保存 `${VAR}` 引用；本地 launcher 在 MCP 启动时读取值，禁止复制进 Git 或插件 manifest。
- 使用 `mcp_client_policy.<server>.include` 或 `.exclude` 完成客户端路由（`codex`、`cursor`、`claude` 各自生效）。禁止在 MCP 定义中放入 adapter 专属 `_skip` 字段。
- 客户端里本机才有的认证值（例如远程 MCP 的 `headers.Authorization`）不进入项目；同步与 bootstrap 默认保留它们，`--reset-overrides` 才丢弃。
- `${PLUGIN_ROOT}` 用于不可变包文件，`${PLUGIN_DATA}` 用于客户端管理的可写状态。项目合并包只含 Skill、`mcp.json` 与 launcher，详见下一节。

## MCP 条目里的路径与变量占位符

| 写法 | 含义 | 何时解析 |
| --- | --- | --- |
| `${HOME}`、`${USER}`、`${LOGNAME}`、`${TMPDIR}`、`${XDG_CONFIG_HOME}`、`${XDG_DATA_HOME}`、`${XDG_CACHE_HOME}`、`${XDG_STATE_HOME}` | 系统变量，描述本机而不是凭据。`HOME` 取 `--home`（默认当前用户主目录），XDG 变量取进程环境，未设置时用 `HOME` 下的默认目录 | `sync` / `bootstrap` 生成包时展开为本机值；每台机器各自同步，所以是“用户主目录”的可移植写法，禁止写死绝对路径 |
| `__PROJECT_DIR__` | 该条目所在 MCP 来源文件的目录 | 生成包时展开 |
| `__REPO_ROOT__` | 项目根目录 | 生成包时展开 |
| `${PLUGIN_ROOT}/<路径>`（含被 launcher 包装的条目） | 该条目所在来源的插件目录（来源文件所在目录）里的文件 | 生成包时按**各自来源**解析；包内本来就有的（`skills/<名称>/…`）保持 `${PLUGIN_ROOT}`，其余指向来源目录里实际存在的文件的绝对路径；两处都没有则 `sync` 与 `doctor` 阻塞并给出条目名、来源文件与路径。launcher 按自身位置（`<包根>/runtime/`）推导 `PLUGIN_ROOT`，不从进程环境或 `credential_env_file` 读取 |
| `${PLUGIN_DATA}` | 客户端管理的可写状态目录 | 由客户端在启动时解析；被 launcher 包装的条目（含凭据键）引用它时，生成的 launcher 参数末尾追加 `--plugin-data ${PLUGIN_DATA}`，由客户端展开后传给 launcher，launcher 再代入其 config |
| 其他 `${NAME}` | 凭据键 | 必须在 `credential_env_file` 里存在；缺失时 `sync` 阻塞并只列出键名 |

系统变量不要求出现在 `credential_env_file` 中；真正缺失的凭据键仍会被报告。预览与 `--apply` 使用同一套校验。
- 客户端原生 adapter 只在安装阶段使用；Agent Plugins 1.0 文件保持权威源地位。

## 远程 HTTP 认证与更新项目声明

远程 MCP 的 `headers.Authorization: "Bearer ${EXAMPLE_MCP_TOKEN}"` 与 `bearer_token_env_var: "EXAMPLE_MCP_TOKEN"` 等价，不读取 `credential_env_file`。Codex 导入还支持 `http_headers` / `env_http_headers`，Cursor 导入接受 `${env:NAME}`；项目统一保存 `${NAME}` 引用。`doctor --client` 只检查进程环境中变量是否非空，只显示变量名。不要把令牌值传给任何导入或同步命令。

`sync --client codex` 把远程配置写入原生 `~/.codex/config.toml` 的 `bearer_token_env_var` / `env_http_headers`，项目包只加载 stdio MCP，避免远程服务器重复加载。现存同名原生配置不同、或本项目安装的条目被本机改动时，需 `--replace`（替换整条配置）；未被本机改动的条目在后续同步时更新，删除时仅移除仍与记录一致的条目。其他 TOML 内容保留，旧配置进入本机恢复目录。Codex 的 header 变量只支持纯 `${NAME}` 或 Authorization 的 `Bearer ${NAME}`，更复杂的拼接会明确拒绝。Claude Code 原样保存 `${NAME}`，Cursor 输出 `${env:NAME}`；已有 Claude 本机认证值仍按上述覆盖规则保留，需要改为项目引用时显式加 `--reset-overrides`。

`transfer --update` 允许更新已有同名 Skill / MCP；默认预览列出变化路径、Skill 前后摘要和 MCP 变化的键名，不显示配置值。只有同时加 `--apply` 才替换，旧内容进入本机恢复目录，失败时回滚。MCP Tool `agent_pack_transfer` 提供等价的 `update` / `apply` 参数。

`check` / SessionStart 本地比较检测到的客户端与项目声明；一致时不增加提示，联网版本检查失败也不会阻止差异提示。只关闭能力检查可在本机用户配置设置 `capability_check_ignored_clients: ["cursor"]`；忽略整个客户端沿用 `agenthelm config ignore-client cursor --apply`。多机完整步骤见 [README](../../../README.md#A-机新增能力B-机同步)。
