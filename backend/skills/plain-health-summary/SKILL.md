---
name: plain-health-summary
description: Use when answering health / wellness questions in chat. Injects a system-prompt instruction to give a one-sentence plain-language conclusion first, then evidence-backed professional detail. Disabled by default.
title: 通俗化健康结论
kind: prompt
scope: chat
builtin_key: plain_health_summary
version: 1.0.0
---

# 通俗化健康结论

## Overview

一个 **prompt 类**技能（`scope=chat`）：启用后，把一段指令注入对话系统提示，定制回答风格——
回答健康养生类问题时**先用一句通俗的话给出核心结论，再展开专业、有据可查的细节**。

与 playbook 技能不同，prompt 技能不驱动抓取/清洗逻辑，只影响 LLM 的回答口吻与结构。
**默认关闭**，用户可按需在「Skill 配置」中开启。

## Trigger（何时生效）

- 用户在「Skill 配置」中开启本技能；
- 之后每次对话，`skill_service.build_prompt_text(user)` 会把已启用的 chat prompt 技能拼进系统提示。

注入受字符预算约束（`SKILL_PROMPT_CHARS`，默认 1500）：按 `内置优先 + 最近更新优先` 顺序拼接，
超出预算的技能不再注入，避免系统提示过长挤占上下文。

## 注入内容（本技能 content）

```
在回答健康养生类问题时，请先用一句通俗易懂的话概括核心结论，再给出专业、有据可查的细节。
```

## 组合与约束

- 可与其它 chat prompt 技能叠加，各自成一行 `- {name}：{content}` 注入。
- prompt 内容上限 4000 字；不得含密码/密钥/证件号等敏感信息（`_SENSITIVE_RE` 拦截）。
- 新建/修改 prompt 技能会经安全与能力预审（`skill_review`），未通过则拒绝落库并留痕。
- 本技能为内置种子，**仅可开关，不可改内容/删除**。

## 关联实现

- `app/services/skill.py`：`active_prompt_skills` / `build_prompt_text`（消费侧）。
- `app/services/chat.py`：把 `build_prompt_text` 结果并入对话系统提示。
