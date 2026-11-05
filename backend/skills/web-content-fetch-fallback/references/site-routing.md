# 域名路由与站点抓取姿态（L3 参考）

本文件是 `web-content-fetch-fallback` 的执行层参考，仅在需要完整路由表时按需加载。

## 1. 内置 playbook 规则（本项目默认种子）

`app/services/skill.py` 中 `_BUILTIN_URL_PLAYBOOK_RULES`：

| match | mobile_ua | prefer_render | 说明 |
| --- | --- | --- | --- |
| `mp.weixin.qq.com` | ✅ | — | 微信公众号文章：移动端 UA 更易拿到完整正文 |
| `csdn.net` | — | ✅ | CSDN：正文常延迟加载/含反爬，渲染服务可用时优先隔离渲染 |

规则字段语义：

- `match`（必填，字符串）：主机名后缀匹配。命中条件为 `host == match` 或 `host.endswith("." + match)`
  或 `match in host`。例：`csdn.net` 命中 `blog.csdn.net`。
- `mobile_ua`（可选，布尔）：抓取时改用 `MOBILE_UA`（iPhone Safari 标识）替代默认桌面 UA。
- `prefer_render`（可选，布尔）：仅当 `settings.URL_RENDERER_ENABLED=true` 时，先走隔离浏览器渲染。
- `note`（可选，字符串）：会记录到可观测 trace 的 `url_skill_strategy` 步骤，便于排障。

规则数组上限 30 条（`_MAX_PLAYBOOK_RULES`），防超大 JSON。多个已启用技能命中同一 host 时，
`mobile_ua` / `prefer_render` 按 **OR** 合并，`note` 追加。

## 2. 内置站点提取 Skill（选择器路由）

`app/services/url_skills.py` 中 `SKILLS`——按 host 选择正文容器选择器，命中则精确提取，
否则回落通用 `GENERIC`（`article` / `main`）+ trafilatura：

| skill | hosts | 正文选择器 | 标题选择器 |
| --- | --- | --- | --- |
| `wechat_article` | `mp.weixin.qq.com` | `#js_content` | `#activity-name` |
| `zhihu_article` | `zhihu.com` | `.RichText.ztext` / `.RichText` / `.Post-RichTextContainer` | `h1` |
| `generic_article` | 其它 | `article` / `main` | `h1` / `<title>` |

知乎额外兜底：正文选择器落空时，尝试解析 `script[type="application/ld+json"]` 的 `articleBody`。

## 3. 抓取姿态对照（借鉴 web-content-fetcher 域名路由）

`web-content-fetcher` 用 fast / stealth 双模式并给出域名路由表。映射到本项目：
`fast ≈ 直抓(strict)`，`stealth ≈ 隔离渲染(prefer_render)`。下表为常见站点的**推荐姿态**，
可据此在「Skill 配置」中扩展自建 playbook 规则：

| 域名 | 推荐姿态 | 本项目对应 | 说明 |
| --- | --- | --- | --- |
| `mp.weixin.qq.com` | stealth/fast | 移动端 UA + `#js_content` | JS 渲染内容，内置 Skill 精确命中 |
| `zhuanlan.zhihu.com` / `zhihu.com` | stealth | `prefer_render` + `.RichText` | 反爬 + JS，需有效文章 URL |
| `juejin.cn` | stealth(auto) | `prefer_render` | JS 渲染 SPA，直抓常空 → 自动降级渲染 |
| `blog.csdn.net` | fast | 直抓（内置 `prefer_render`） | 静态 HTML 为主，含反爬时渲染优先 |
| `sspai.com` | fast | 直抓 + `article` | 静态 HTML |
| `cnblogs.com` | fast | 直抓 + `article`/`main` | 静态 HTML |
| `toutiao.com` / `infoq.cn` | stealth | `prefer_render` | JS 渲染，需有效文章 URL |
| GitHub / MDN / 各类文档站 | fast | 直抓 + `main`/`article` | 静态正文清晰 |

## 4. 受限/不支持平台（不会绕过）

| 平台/情形 | 结果 | 处理 |
| --- | --- | --- |
| 小红书等需登录态 | `restricted` | 不绕过登录，提示手动补充正文 |
| Cloudflare「Just a moment」人机验证 | `restricted` | 命中 `RESTRICTED` 正则，明确拒绝 |
| 付费墙 / 「购买后阅读」 | `restricted` 或带 `warnings` | 不绕过付费；正文可能不完整时给警告 |
| PDF / Word 链接 | `content_type` | 仅支持网页正文，文件请走「文件上传」 |

`RESTRICTED` 命中词（`url_skills.py`）：`访问过于频繁 / 安全验证 / 完成验证后 / 验证码 /
扫码登录后 / 登录后(查看|阅读) / 付费(后|阅读) / 购买后阅读 / access denied /
verify you are human / just a moment`。判定上，验证页通常短且无正常正文，故仅在
`标题命中` 或 `可见文本 < 1500 字且命中` 时判为受限，避免长文中提及「验证码」被误杀。
