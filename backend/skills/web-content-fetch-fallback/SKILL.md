---
name: web-content-fetch-fallback
description: Use when resolving a public web URL into clean article Markdown for the knowledge base. Covers mobile-UA / render-first site routing, a safe fetch boundary (DNS pinning, no credentials, TLS verify), and a strict→render→relaxed fallback chain that never bypasses login, captcha or paywall.
title: 网页链接抓取回退策略
kind: playbook
scope: url_import
builtin_key: url_fetch_fallback
version: 1.0.0
---

# 网页链接抓取回退策略

## Overview

把一条公开网页链接**稳定地**解析成干净正文 Markdown，用于知识库入库。核心是两件事：

1. **站点路由**：按主机名选择抓取姿态（移动端 UA / 渲染优先），命中公众号、CSDN、知乎等
   国内站点的反爬与延迟加载特性。
2. **回退链**：严格直抓 → 隔离渲染 → 宽松降级提取，逐级放宽召回，任一环节拿到合格正文即止；
   全程不绕过登录/验证/付费，不携带业务凭据。

设计上借鉴了 `web-content-fetcher`（Scrapling fast/stealth 双模式自动降级 + Jina Reader 备选 +
域名路由表 + 标准 Markdown 输出）的**分层降级 + 域名路由**思路，并落到本项目**安全优先**的
受控网络边界上：所有出网经 `SafeFetcher`，DNS 固定、逐跳校验、限额解压、TLS 证书校验。

## Trigger（何时启用）

当满足以下任一条件时，本策略生效：

- 用户在「链接解析 / URL 导入」中提交了一条 `http(s)` 公开链接；
- 需要把网页正文抽取为 Markdown 落入知识库；
- 目标站点已知对机器人 UA 敏感（公众号/CSDN/知乎等）或正文延迟加载。

策略规则来自 `scope=url_import` 的 playbook 技能（本技能为内置种子，可开关）；关闭后仅走
默认桌面 UA + 严格直抓，不再有站点级姿态调整。

## 抓取姿态（域名路由）

规则内容即本技能的 playbook JSON，每条规则形如：

```json
{ "match": "mp.weixin.qq.com", "mobile_ua": true,  "note": "移动端 UA 更易拿到完整正文" }
{ "match": "csdn.net",         "prefer_render": true, "note": "正文延迟加载/含反爬，渲染可用时优先隔离渲染" }
```

- `match`：主机名后缀匹配（`csdn.net` 命中 `blog.csdn.net`）。
- `mobile_ua`：改用移动端真实浏览器 UA（部分站点对移动端返回更完整正文）。
- `prefer_render`：在 `URL_RENDERER_ENABLED` 时优先走隔离浏览器渲染，失败再回落直抓链。

内置种子默认覆盖 `mp.weixin.qq.com`（移动端 UA）与 `csdn.net`（渲染优先）。完整的域名路由表、
各站点抓取姿态与「支持/受限平台」清单见 **references/site-routing.md**。

## 提取策略（回退链）

```
URL 输入
  │  normalize_url（协议/端口/公网地址校验，拒绝内网与凭据）
  ▼
strategy = url_strategy(user, host)   →  mobile_ua / prefer_render
  │
  ├─ prefer_render 且渲染已启用 ──► [1] 隔离浏览器渲染 ──► extract_article(strict)
  │                                   失败 ▼
  ▼
[2] SafeFetcher 直抓（按 mobile_ua 选 UA）──► extract_article(strict)
  │  命中 empty_article / fetch_failed / tls_failed ▼
  ├─ 渲染已启用 ──► [3] 隔离渲染重试 ──► extract_article(strict)
  │                   失败/未启用 ▼
  ▼
[4] 宽松降级提取 extract_article(relaxed=True)
      · 放宽 trafilatura 召回、最小正文阈值 80→40
      · 仍无正文时用整页净化文本兜底（带「可能含噪声」警告，交人工对照）
  ▼
输出：{ title, body(Markdown), source_site, skill, extraction_method, warnings }
```

对应实现见 `app/services/url_analyst.py`（编排）与 `app/services/url_skills.py`（站点 Skill +
正文提取）。逐级降级的判定条件、安全边界与错误码语义见 **references/fallback-chain.md**。

## 安全底线（与「什么都能抓」的抓取器不同）

- **不绕过访问控制**：`401/403/429` 一律归类为 `restricted`，不重试、不换 UA 硬闯，提示用户手动补充正文。
- **不携带凭据**：`DummyCookieJar`，无业务 Cookie / Token；仅发送真实浏览器 UA 与常规 Accept 头。
- **只访问公网**：`PublicResolver` 做 DNS 固定并校验解析结果，拒绝内网/回环/保留地址与 DNS 重绑定。
- **限额与超时**：单文档解压上限、总大小/请求数预算、逐跳超时与重定向 ≤6 次。
- **TLS 校验**：显式加载 certifi CA，证书失败单独归类 `tls_failed`，不静默降级为不校验。

## Output

统一输出标准 Markdown：标题层级、超链接、列表、代码/引用块保留；图片降级为 `[图片] alt` 链接
（不向第三方自动请求远程图）。相对链接补全为绝对地址。

## Progressive Disclosure（按需展开）

- 需要**完整域名路由表 / 各站点姿态 / 支持与受限平台清单** → 读 `references/site-routing.md`
- 需要**回退链判定条件 / 安全边界细节 / 错误码语义表** → 读 `references/fallback-chain.md`
