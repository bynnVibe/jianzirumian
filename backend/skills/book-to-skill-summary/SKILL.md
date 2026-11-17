---
name: book-to-skill-summary
description: Use when summarizing knowledge-base documents. Structured five-part summary spec (overview / chapters / glossary / patterns / cheatsheet) inspired by the open-source book-to-skill project. Disabled by default; enabling it resumes waiting summaries in background.
title: 知识总结（book-to-skill）
kind: playbook
scope: kb_summary
builtin_key: book_to_skill_summary
version: 1.0.0
---

# 知识总结（book-to-skill）

## Overview

一个 **playbook 类**技能（`scope=kb_summary`）：开启后，知识库文档入库时后台自动生成
**五段式结构化总结**——核心概览 / 章节结构 / 术语表 / 关键模式 / 速查清单，
在知识库详情「知识总结」选项卡在线查看。

设计借鉴开源项目 [book-to-skill](https://github.com/virgiliojr94/book-to-skill)
（将书籍/文档转化为可复用的结构化知识），适配中文文档场景：
**确定性切分 + LLM 按规范生成 JSON**，产物落 `kb_doc_summaries`（每文档一份，重跑覆盖）。

**默认关闭**：未开启时入库仅登记等待任务（`waiting_skill`），不做任何摘要动作；
开启后 `resume_pending()` 将等待中的任务转后台继续执行。

## Trigger（何时生效）

- 用户在「Skill 配置」中开启本技能（`update_skill` 钩子触发 `kb_summary.resume_pending()`）；
- 文档入库成功（上传管线 / 链接导入）→ `kb_summary.schedule_summary(...)`：
  - 技能开启 → `pending` 入后台生成；
  - 技能未开启 → 仅登记 `waiting_skill`；
- 「知识总结」页「摘要总结」按钮手动触发：未开启返回 `skill_disabled` 引导开启，
  开启后后台继续执行；运行中可轮询 `job_status`。

## 生成流程

1. **确定性提取器**：按标题行（`第X章/节`、数字/中文序号）切块；无标题时按 1500 字窗口切块，最多 12 块；
2. **LLM 按 spec 生成**：本技能 content 作为系统规范注入，经网关 `create_for_role("kb_summary", user_id=owner)`
   走用户密钥出网，要求严格 JSON 输出；
3. **校验与重试**：解析失败重试 1 次，仍失败标 `error`；成功落 `done` 并存结构化 JSON。

## 产物结构（本技能 content 规范）

```json
{
  "overview": "全文核心概览（150-300 字）",
  "chapters": [{"title": "章节标题", "summary": "章节摘要", "key_points": ["要点"]}],
  "glossary": [{"term": "术语", "definition": "文档中的定义"}],
  "patterns": ["可复用方法/模式"],
  "cheatsheet": ["速查清单条目"]
}
```

要求：chapters 按文档实际结构划分 3-12 条；glossary 3-15 条；patterns/cheatsheet 各 3-10 条；
全部内容严格忠于原文，不得编造；中文输出。

## 组合与约束

- 本技能为内置种子，**仅可开关，不可改内容/删除**（`ensure_builtin` 幂等）。
- 摘要按文档 owner 维度生效：谁的密钥出网、谁的技能开关决定其文档是否摘要。
- 状态机：`waiting_skill → pending → running → done / error`；运行前技能被关闭退回 `waiting_skill`。

## 关联实现

- `app/services/skill.py`：`BUILTIN_SKILLS` 种子 / `kb_summary_spec` / `kb_summary_skill_id` / 开启钩子。
- `app/services/kb_summary.py`：调度 / 切分 / LLM 生成 / 状态机（消费侧）。
- `app/services/upload_pipeline.py`、`app/services/url_imports.py`：入库钩子。
- `app/api/kb_insights.py`：`/{kb_id}/summaries` 列表 / 详情 / generate / job_status。