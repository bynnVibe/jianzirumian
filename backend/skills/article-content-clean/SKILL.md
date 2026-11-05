---
name: article-content-clean
description: Use when cleaning fetched article text before knowledge-base ingestion. Deterministic, line-oriented de-noising (footnote markers, page/copyright/follow prompts, nav, ads, stray spaces, extra blank lines) that only subtracts and never rewrites — order, paragraphs and sentence completeness are strictly preserved.
title: 链接正文清洗策略
kind: playbook
scope: polish
builtin_key: article_clean_policy
version: 1.0.0
---

# 链接正文清洗策略

## Overview

对抓取到的网页正文做**确定性、可复现、可审计**的去噪清洗，用于知识库入库前的「内容润色」。
本技能是**零模型依赖的规则清洗**，与 LLM 润色互补：结果稳定、逐行可追溯，只做减法。

设计融合了三处经验：

- **本项目既有实现**（`app/services/article_clean.py`）：逐行处理、内置噪声正则、角标删除、
  空格/空行折叠，策略来自 `scope=polish` 的 playbook，可开关、可扩展正则。
- **BogdanovychA/skills · text-cleaner**：把「技术噪声」（时间戳、残留 HTML 标签、技术注释、
  emoji 等）作为一类可清理目标——本技能将其收敛为**可选扩展正则**，默认不激进删除以免误伤正文。
- **BogdanovychA/skills · mr-obsidian**：结构化整理时**严格保留原文完整性**——本技能据此确立
  「只减不增、保序保段」的硬契约，任何清洗都不得丢句、不得改写、不得新增观点。
- **ConardLi/garden-skills**：SKILL.md + `references/` 的渐进式组织与「learn-before-process /
  有界处理」理念——把噪声规则清单与保留契约拆到按需加载的参考文档，主体只讲何时用、怎么用。

## Trigger（何时启用）

- 「链接解析」拿到正文后、入库前需要批量去噪；
- 用户在前端点「内容润色」（`POST /api/knowledge/polish-ocr`）；
- 需要把脏文本（含页码/版权/关注引导/角标）整理成干净段落，但**不允许改写原意**。

清洗策略由已启用的 `scope=polish` playbook 技能合并而来（布尔开关按 OR，`extra_line_patterns`
去重追加）。**未启用任何清洗技能时返回原文**，不做任何改动。

## 清洗策略（playbook JSON）

内置默认策略即本技能 content：

```json
{
  "remove_footnote_markers": true,
  "remove_noise_lines": true,
  "collapse_spaces": true,
  "collapse_blank_lines": true,
  "extra_line_patterns": []
}
```

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `remove_footnote_markers` | bool | 删除行内解析角标 `[1]`/`【2】`/`[1,2]`，只删角标不动句子其余部分 |
| `remove_noise_lines` | bool | 整行命中噪声规则即删除（页码/版权/关注引导/署名/上下篇/纯 URL/广告/纯符号行） |
| `collapse_spaces` | bool | 半角/全角/制表/不换行空格折叠为单个空格并去首尾 |
| `collapse_blank_lines` | bool | 连续空行压缩为一个（保留自然段分隔的单个空行） |
| `extra_line_patterns` | string[] | 用户扩展的整行删除正则，≤10 条、每条 ≤120 字，非法正则跳过并告警 |

字段校验见 `_validate_polish_policy`；四个布尔 + `extra_line_patterns` 之外的字段一律拒绝。

## 处理流程（逐行、有界、只减不增）

```
输入正文 text
  │  按行遍历（绝不重排、绝不合并段落顺序）
  ▼
每行：① collapse_spaces 折叠多余空格
      ② 空行 → collapse_blank_lines 压缩连续空行（保留单个自然段空行）
      ③ remove_noise_lines：内置噪声正则 / 纯符号行(≤40字) / extra_line_patterns 命中 → 删行
      ④ remove_footnote_markers：删行内角标（保留句子其余部分）
  ▼
去首尾空行 → 输出 { cleaned, stats }
```

`stats` 回传删除计数（`removed_lines` / `removed_footnotes` / `collapsed_spaces` /
`extra_patterns_removed` / `before_chars` / `after_chars`），供前端提示与审计。

## 保留契约（硬约束）

1. **只减不增**：不新增任何观点、句子或内容；不改写、不润色措辞（那是 LLM 润色的职责）。
2. **保序保段**：逐行处理，输出行是输入行的**子序列**，自然段顺序与连贯性不变。
3. **句子完整性**：删角标只删角标本身；删噪声只删**整行命中**的行，不切碎正文句。
4. **可复现可审计**：同输入同策略必得同输出，全过程有 `stats` 计数留痕。

完整契约、边界案例与「结构化整理（TOC/摘要）时如何仍保完整」见 **references/preserve-guarantees.md**。

## Progressive Disclosure（按需展开）

- 需要**完整噪声正则清单 / 如何扩展 text-cleaner 式规则（时间戳/残留 HTML/emoji）** → 读 `references/noise-rules.md`
- 需要**保留契约细则 / 边界案例 / 与 LLM 润色的分工** → 读 `references/preserve-guarantees.md`
