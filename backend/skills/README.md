# 见字如面 · Skill 文档库

本目录沉淀「Skill 配置」页面中每一个技能的**权威文档**，采用 OpenClaw / Anthropic
Agent Skills 的开放标准目录结构与 **渐进式加载（Progressive Disclosure）+ 懒加载**机制。

页面卡片只展示「标题 + 简要说明」（发现层），完整说明书与扩展参考在用户点开时按需拉取，
避免一次性把全部内容塞进上下文/首屏。

## 目录结构

```
skills/
├── README.md                       ← 本文件：索引与加载机制说明
├── web-content-fetch-fallback/     ← 网页链接抓取回退策略（playbook / url_import）
│   ├── SKILL.md                    ← 必需：YAML frontmatter + 主体说明（何时用 / 怎么用）
│   └── references/                 ← 可选：主体按需引用的扩展文档（执行层，懒加载）
│       ├── site-routing.md
│       └── fallback-chain.md
├── article-content-clean/          ← 链接正文清洗策略（playbook / polish）
│   ├── SKILL.md
│   └── references/
│       ├── noise-rules.md
│       └── preserve-guarantees.md
└── plain-health-summary/           ← 通俗化健康结论（prompt / chat）
    └── SKILL.md
```

每个技能一个独立文件夹，文件夹名即 `slug`。`SKILL.md` 是唯一必需文件；
`references/` 存放主体在运行/阅读到相应步骤时才拉取的扩展材料。

## 三层渐进式加载

| 层级 | 名称 | 加载内容 | 触发时机 | 承载 |
| --- | --- | --- | --- | --- |
| L1 | 发现 Discovery | `name` + 一行 `description` | 页面列表加载 | `GET /api/skills`（列表只回标题+简述） |
| L2 | 激活 Activation | `SKILL.md` 主体 Markdown | 用户点开「查看文档」 | `GET /api/skills/{id}/doc` |
| L3 | 执行 Execution | `references/*.md` 单篇扩展文档 | 用户点开某条参考链接 | `GET /api/skills/{id}/doc/ref?path=references/xxx.md` |

L1 只读 frontmatter 的 `name` / `description`（约几十 token），让页面能列出全部技能而几乎不占上下文；
只有真正需要时才逐层向下拉取，且每次只拉当前需要的那一篇，用完即可卸载。

## SKILL.md frontmatter 约定

```yaml
---
name: web-content-fetch-fallback      # slug，与目录名一致
description: 一句话说明「何时用」，是页面与调用方的发现契约
title: 网页链接抓取回退策略            # 页面展示的中文标题
kind: playbook                        # prompt | playbook
scope: url_import                     # chat | url_import | polish
builtin_key: url_fetch_fallback       # 与库内内置技能种子一一对应
version: 1.0.0
---
```

`builtin_key` 把文档与 `app/services/skill.py` 中 `BUILTIN_SKILLS` 的内置种子绑定：
后端据此把某个内置技能行反查到对应目录，实现「卡片简述 → 具体 skill 文档」的懒加载。

## 技能索引

| slug | 标题 | 类别 / 作用域 | builtin_key |
| --- | --- | --- | --- |
| `web-content-fetch-fallback` | 网页链接抓取回退策略 | playbook / url_import | `url_fetch_fallback` |
| `article-content-clean` | 链接正文清洗策略 | playbook / polish | `article_clean_policy` |
| `plain-health-summary` | 通俗化健康结论 | prompt / chat | `plain_health_summary` |

## 维护约定

- 文档是**说明书**，运行时的确定性行为仍以 `app/services/` 下的实现为准（`url_analyst.py` /
  `url_fetch.py` / `url_skills.py` / `article_clean.py`）；改实现时同步更新对应 `SKILL.md`。
- 自定义技能（用户在页面新建）不落盘到本目录，其「文档」由后端按 name/description/content 即时合成，
  同样走 L2 懒加载弹窗展示。
- 新增内置技能时：建目录 + `SKILL.md`（含 `builtin_key`），并在 `skill.py` 的 `BUILTIN_SKILLS`
  与后端 `skill_docs.BUILTIN_DOC_SLUGS` 中登记映射。
