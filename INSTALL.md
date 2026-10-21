# Agent 一句话初始化入口

当用户说“用 `https://skill.sakanano.moe/` 初始化当前项目”或同义请求时，按本文件执行。

## 目标

安装分发包中的四个 Plugin，并用 `agents-init` 创建项目契约：

- `agents-init`：创建或迁移项目骨架与 Project Profile；
- `project-orchestrator`：初始化后管理项目生命周期（Task/Case、路由、验证、Git）；
- `agent-pack`：管理项目与客户端之间的 Plugin/Skill/MCP。
- `opinion-manager`：生成和维护用户确认的 Agent 行为规则。

Opinion provider 保持独立职责，并作为 `opinion-manager` 随包安装。公开版本化目录含经批准的规则与模板；先用工具 `catalog --catalog plugins/opinion-manager/catalog --output json` 查看实际内容。

## 前提

- 需要联网下载公开仓库，并能在本地运行 Python 3.11 及以上；
- 需要写入当前项目；
- 安装到 Codex、Cursor 或 Claude Code 的用户级目录时，按宿主权限机制取得授权；
- 禁止读取或上传项目 `.env`、个人 Opinion 规则、凭据及私有目录内容。

## 执行

1. 读取 `https://skill.sakanano.moe/plugins/agents-init/plugin.json` 的 `repository` 字段，将该完整仓库下载或克隆到新建的安全临时目录。只下载 `skills/agents-init/` 子目录不受支持。
2. 根据当前宿主选择 `--client codex`、`--client cursor` 或 `--client claude`。
3. 从项目现有文件确认名称、项目类型、VCS、技术栈、runtime 与 Agent 客户端。项目名不能派生 ASCII slug 时（例如纯中文名），显式传入 `--slug example-project`，slug 使用 ASCII。无法确认且会改变生成契约时，一次性询问用户。
4. 确认目标项目目录已存在；全新项目先执行 `mkdir -p /absolute/path/to/project`。在完整仓库根目录运行统一编排器，默认只预演：

```bash
python3 scripts/bootstrap_and_init.py \
  --client codex \
  --project /absolute/path/to/project \
  --name example \
  --project-type code \
  --vcs github \
  --stack python \
  --runtime local \
  --agent-cli codex
```

5. 向用户展示四个 Plugin 的安装位置、拟创建文件和碰撞项。得到确认后原样追加 `--apply`。
6. `--apply` 成功后检查输出中的 `doctor.ok=true`；报告已安装 Plugin、已创建文件和仍需填写的 `TODO`。
7. 完整读取 Opinion Manager 的 Skill、[版本契约](plugins/opinion-manager/references/versioning.md)和[首次使用说明](plugins/opinion-manager/references/onboarding.md)。只提供三个主选项：直接用模板、选择模板的部分条目、从空白开始迭代；另可选择基于已授权记忆提出候选规则。选择后再展开需要的内容，禁止默认全选平行变体；目录确实为空时提供个人说明导入或从空白开始。
8. 正式保存前先生成 Profile 与 compose 两份预览，一次展示并确认完整正文、来源、个人修改、差异及拟保存文件。写前重新核对两项指纹；Agent 分别使用各自真实指纹保存，完整读回并执行 `verify`。相关内容漂移或存在冲突时重新审阅。个人来源、Profile、`OPINION.md` 与 `opinion.lock.json` 只保存在目标项目的指定私有位置，禁止复制到下载目录、公开模板、示例、测试或文档。
9. 隔离安装测试可使用 `--home /path/to/test-client-home`，保持当前客户端配置。安装盘点与 project doctor 分别验证客户端安装文件和项目结构；Codex 桌面端还须完成插件启用或重新加载，并在新 Session 确认 Skill 可被发现。文件安装完成不能单独证明当前 Session 已加载插件。

## 已有 v5 项目

项目存在 `.agents/moe.sakanano.project-runtime/project.json` 时，不要走上面的初始化流程：先安装四个 Plugin，再用 `skills/agents-init/scripts/init_project.py --mode migrate --recovery-dir <项目外目录>` 升级 Profile，详见 `skills/agents-init/AGENT.template.md` 的“从 v5 迁移”。

## 安全语义

- 默认 dry-run；没有 `--apply` 时禁止写入用户目录和项目。
- 初始化碰撞会在安装前阻断；禁止自动覆盖项目文件。
- 已安装 Plugin 内容不同会阻断；升级须单独使用 `agent-pack bootstrap --replace` 并保留恢复副本。
- 下载、安装和项目写入可能分别触发宿主授权。这不改变用户只需提出一句自然语言请求的交互目标。

## 桌面加载与来源

新项目优先使用用户易于选择的可见目录。插件文件安装、项目 doctor 与新 Session 的真实 Skill 发现需分别验证。同名包并存时明确当前来源及版本；不要把文件存在当作客户端已加载。升级须通过当前客户端的安装/启用机制，保留个人配置与恢复副本，未经授权不移除旧来源；禁止手改受客户端管理的缓存。详见首次使用说明。
