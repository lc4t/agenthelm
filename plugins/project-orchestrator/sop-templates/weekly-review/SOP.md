---
schema_version: "1.0"
id: "{{id}}"
title: "每周复盘"
status: "draft"
version: "1.0.0"
description: "每周回看进展，写复盘文档并规划下周"
trigger: "用户要做周复盘、回顾本周，或到了固定的复盘日"
exclude: "只是查询某个事项当前进展"
schedule: "weekly:sun"
confirm: "ask"
creates_case: "always"
created: "{{date}}"
aliases:
  - "周复盘"
examples_hit:
  - "做一下本周复盘"
  - "回顾一下这周做了什么"
examples_miss:
  - "这个事项现在到哪了"
on_events: []
outputs: []
sources: []
decided: []
---
# {{title}}

## 目的

回看本周实际完成了什么、卡在哪里，沉淀经验，并定下下周最重要的几件事。

## 前置条件

- 命令：agenthelm｜用途：读取各事项的进展（`agenthelm project progress --output json`）｜安装：见 agenthelm 的 INSTALL.md｜缺失时：询问用户

## 步骤

1. 收集本周进展
   - 工具：agenthelm
   - 输入：本周的起止日期
   - 中间产物：进展清单（事项、状态、本周的工作记录）
   - 检查点：覆盖所有进行中和本周关闭的事项
2. 对照上周计划
   - 输入：上一次复盘文档里的“下周计划”
   - 检查点：每条计划都有结论：完成 / 未完成及原因
3. 写复盘文档
   - 输入：以上两步的结果
   - 最终产物：docs/review-本周日期.md（使用本目录 templates/review.md 的结构）
   - 检查点：所有小节都有内容，结论有依据（指向事项或文件）
4. 规划下周
   - 输入：复盘文档里的未完成项与阻塞
   - 检查点：不超过用户设定的数量；每项有明确的下一动作
   - 需确认：是
   - 说明：下周计划由用户决定，只提出建议
5. 登记交付物
   - 说明：把复盘文档登记为 Case 的交付物

## 完成标准

- 复盘文档已保存并登记为交付物
- 下周计划已经用户确认

## 例外与回退

- 本周没有任何记录：照实写“本周无记录”，并提示用户是否漏记

## 修订记录

暂无。
