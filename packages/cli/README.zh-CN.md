# @sakanano-moe/agenthelm

[English](https://github.com/lc4t/agenthelm/blob/main/packages/cli/README.md) · **简体中文**

[agenthelm](https://github.com/lc4t/agenthelm) 的命令行。agenthelm 是跨 Agent 的项目管理插件集，适用于 Claude Code、Codex 与 Cursor，包含四个插件：agents-init（项目契约）、project-orchestrator（项目生命周期）、agent-pack（Plugin / Skill / MCP 管理）和 opinion-manager（Agent 行为规则）。

- **安装、检查与更新插件**：经各客户端自己的市场完成。写操作默认只预览，加 `--apply` 才执行。
- **用户级运行时**：一次 `npx @sakanano-moe/agenthelm runtime install --apply`，之后项目启动器打开菜单不联网、不经过 npx，也不需要 AI 客户端。
- **不需要 AI Agent 的项目菜单**：`agenthelm project` 或项目根目录的 `./menu`，用来找资料、看进展、读项目指南、检查与维护、记录事项，全程不调用模型。

需要 Python 3.11+（使用 npx 时另需 Node.js 18+）。只在 macOS 与 Linux 上验证过。离线时，从 GitHub Releases 下载发布包，解压后运行 `python3 <解压目录>/plugins/agent-pack/runtime/agenthelm.py runtime install --apply`。完整中文说明见仓库的 [README](https://github.com/lc4t/agenthelm#readme) 与 [INSTALL.md](https://github.com/lc4t/agenthelm/blob/main/INSTALL.md)。

## 常用命令

```bash
npx @sakanano-moe/agenthelm install                   # 预览：安装插件到检测到的客户端
npx @sakanano-moe/agenthelm install --apply
npx @sakanano-moe/agenthelm runtime install --apply   # 只需一次：用户级运行时
npx @sakanano-moe/agenthelm project --demo study      # 用虚构项目体验项目菜单
./menu                                                # 在已初始化的项目里打开菜单
./menu find 笔记 2026-10                               # 不进菜单：直接查；--output json 给脚本用
npx @sakanano-moe/agenthelm check                     # 只读：插件、项目模板与 Opinion 是否落后
npx @sakanano-moe/agenthelm update --apply            # 更新（先不加 --apply 预览）
```

退出码：0 成功；1 没有结果或有必须处理的项；2 用法错误；3 运行时缺失或不兼容；4 能力不可用；5 写入冲突；6 执行失败。

许可证：MIT
