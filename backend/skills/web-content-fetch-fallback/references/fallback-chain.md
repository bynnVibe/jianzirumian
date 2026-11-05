# 回退链、安全边界与错误码（L3 参考）

本文件是 `web-content-fetch-fallback` 的执行层参考，仅在需要回退判定/错误语义时按需加载。
对应实现：`app/services/url_analyst.py`（编排）、`app/services/url_fetch.py`（安全抓取）、
`app/services/url_renderer.py`（隔离渲染）、`app/services/url_skills.py`（正文提取）。

## 1. 逐级降级判定

整体在 `asyncio.timeout(60)` 内进行，`extraction_method` 记录最终成功路径：

| 级 | 姿态 | 进入条件 | method | 失败处理 |
| --- | --- | --- | --- | --- |
| [1] | 隔离渲染优先 | `prefer_render` 且 `URL_RENDERER_ENABLED` | `browser` | 渲染报错 → 记 `render_first_failed`，转 [2] |
| [2] | 安全直抓（strict） | [1] 未产出正文 | `http` | 见下方「直抓失败分流」 |
| [3] | 隔离渲染重试 | [2] 抛 `empty_article/fetch_failed/tls_failed` 且渲染已启用 | `browser` | 渲染不可用/失败 → 转 [4]（若已有直抓响应）否则原样抛 |
| [4] | 宽松降级提取 | 渲染未启用或 [3] 失败，且 [2] 有响应体 | `http_relaxed` | 仍无正文 → 抛 `empty_article` |

**直抓失败分流（[2]→[3]/[4]）**：只有 `empty_article`、`fetch_failed`、`tls_failed` 三类可降级重试；
`restricted`（访问控制）、`too_large`（超限）、`content_type`（非网页）等**安全拒绝不通过浏览器重试**，
直接抛出。这是「不绕过访问控制」底线的关键分支。

**宽松模式（[4]）差异**：`extract_article(relaxed=True)` 会
① 放宽 trafilatura 召回（`favor_recall=True`）；② 最小正文阈值 80→40 字；
③ trafilatura 无语义容器时用整页净化文本兜底（`degraded=True`，附「可能含导航噪声」警告，交人工对照）。

## 2. 安全抓取边界（SafeFetcher）

- **URL 规范化 `normalize_url`**：仅 `http/https`、无用户名/密码、标准端口（80/443）；
  拒绝 `localhost` 及 `.local/.internal/.invalid/.test/.localdomain` 等保留域；主机名 IDNA 编码校验。
- **DNS 固定 `PublicResolver`**：解析结果即连接地址（`AI_NUMERICHOST`，`use_dns_cache=False`），
  经 `public_address` 校验为全局单播地址，拒绝内网/回环/组播/保留/IPv4-mapped，防 DNS 重绑定。
- **逐跳校验**：重定向手动跟随，≤6 跳；每跳重新规范化 + 校验目标地址。
- **限额预算 `FetchBudget`**：总时限 60s、请求数 ≤100、累计解压 ≤20MB；单文档正文 ≤5MB、子资源 ≤2MB；
  gzip/deflate 边解压边计量，超限即断（防解压炸弹）。
- **无凭据**：`trust_env=False`、`DummyCookieJar`（不带 Cookie/代理凭据）；仅常规浏览器头。
- **TLS**：`ssl_context()` 显式加载 certifi CA（规避 macOS/pyenv 缺系统 CA 导致的误报超时）；
  证书校验失败单独归类 `tls_failed`，绝不降级为「不校验」。
- **内容类型白名单**：正文 `text/html`、`application/xhtml+xml`、`text/plain`；子资源另放 css/js/json/ld+json。

## 3. 错误码语义表（UrlImportError.code）

| code | 含义 | 用户提示方向 |
| --- | --- | --- |
| `invalid_url` | 非公网 http(s)/含凭据/非法端口或字符 | 检查链接格式 |
| `unsafe_address` | 解析到内网/保留地址 | 仅允许公开互联网地址 |
| `dns_failed` | 域名无法解析 | 检查域名可达性 |
| `tls_failed` | HTTPS 证书校验失败 | 站点证书链问题，不绕过 |
| `fetch_failed` | 网络超时/连接失败/站点限制 | 可重试或渲染降级 |
| `redirect` | 重定向过多/缺目标 | 检查链接 |
| `restricted` | 401/403/429 或命中验证/付费/登录墙 | 不绕过，手动补充正文 |
| `http_error` | 其它 4xx/5xx | 检查链接有效性 |
| `content_type` | 非网页正文（PDF/Word 等） | 改用文件上传 |
| `too_large` / `budget` / `encoding` | 超限/请求过多/压缩格式异常 | 内容过大或异常 |
| `empty_article` | 未提取到足够正文 | 可能需渲染或手动补充 |
| `timeout` | 整体解析超时（60s） | 重试或手动补充 |
| `renderer_unavailable` / `renderer_protocol` / `render_failed` | 隔离渲染不可用/协议错/解析失败 | 有直抓结果则降级，否则报错 |

## 4. 输出契约

成功返回：`{ title(≤300), body(Markdown, ≤500000), source_site, skill, skill_version,
warnings[], final_url, extraction_method, fetched_at, content_origin:"fetched" }`。

`warnings` 常见两类：① 渲染不可用时的整页降级提取（可能含噪声）；② 命中
`展开全文/阅读剩余/查看完整/此内容…付费` 提示正文可能不完整——两者都要求「对照原文检查后确认」。

## 5. 可观测

每步经 `app.core.observability` 记录 trace：`url_skill_strategy`（命中的 note）、
`safe_http_fetch` / `isolated_browser_render`（字节数+耗时）、`http_fallback` /
`render_first_failed` / `render_fallback_failed` / `relaxed_direct_extract`（降级原因）、
`article_skill`（`skill@version`）。排障时按 trace 还原实际走过的回退路径。
