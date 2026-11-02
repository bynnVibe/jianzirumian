"""内置、版本管理的站点 Skill；只提取资料，不接受网页中的执行指令。"""
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from markdownify import markdownify
import trafilatura

from app.services.url_fetch import UrlImportError


@dataclass(frozen=True)
class ArticleSkill:
    name: str
    hosts: tuple
    selectors: tuple
    title_selector: str = "h1"
    version: str = "1"

    def matches(self, host):
        return any(host == domain or host.endswith("." + domain) for domain in self.hosts)


SKILLS = (
    ArticleSkill("wechat_article", ("mp.weixin.qq.com",), ("#js_content",), "#activity-name"),
    ArticleSkill("zhihu_article", ("zhihu.com",), (".RichText.ztext", ".RichText", ".Post-RichTextContainer")),
)
GENERIC = ArticleSkill("generic_article", (), ("article", "main"))
RESTRICTED = re.compile(r"访问过于频繁|安全验证|完成验证后|验证码|扫码登录后|登录后(?:查看|阅读)|付费(?:后|阅读)|购买后阅读|access denied|verify you are human|just a moment", re.I)


def safe_link(value, base):
    try:
        result = urljoin(base, value or "")
        parts = urlsplit(result)
        return result if parts.scheme in ("http", "https") and parts.hostname and not parts.username else ""
    except ValueError:
        return ""


def clean_article(node, base):
    for element in node.select("script,style,iframe,object,embed,form,nav,footer,button,noscript"):
        element.decompose()
    for element in node.find_all(True):
        if element.name == "img":
            target = safe_link(element.get("data-src") or element.get("src"), base)
            label = element.get("alt") or "图片"
            # 图片保留为普通链接，预览不向第三方自动请求。
            replacement = node.new_tag("a") if isinstance(node, BeautifulSoup) else BeautifulSoup("", "html.parser").new_tag("a")
            replacement.string = f"[图片] {label}"
            if target:
                replacement["href"] = target
            element.replace_with(replacement)
        else:
            href = safe_link(element.get("href"), base) if element.name == "a" else ""
            element.attrs = {"href": href} if href else {}
    return markdownify(str(node), heading_style="ATX", bullets="-", strip=["img"]).strip()


def extract_article(html, url):
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(url).hostname or ""
    skill = next((entry for entry in SKILLS if entry.matches(host)), GENERIC)
    title_node = soup.select_one(skill.title_selector) or soup.title
    title = title_node.get_text(" ", strip=True) if title_node else host
    visible = soup.get_text(" ", strip=True)
    # 验证页通常短且无正常正文；长文章中提及“验证码”不应误判。
    if RESTRICTED.search(title) or (len(visible) < 1500 and RESTRICTED.search(visible)):
        raise UrlImportError("restricted", "网页要求登录、验证或付费，未获取到可公开使用的正文")
    node = next((soup.select_one(selector) for selector in skill.selectors if soup.select_one(selector)), None)
    body = ""
    if skill is not GENERIC and node:
        body = clean_article(node, url)
    if not body and skill.name == "zhihu_article":
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(script.string or "{}")
                objects = value if isinstance(value, list) else [value]
                body = next((item.get("articleBody", "") for item in objects if isinstance(item, dict) and item.get("articleBody")), "")
                if body:
                    break
            except (ValueError, TypeError):
                continue
    if not body:
        # 先净化 HTML 并补全链接，再由通用 Skill 去除导航和广告。
        clean_article(soup, url)
        extracted = trafilatura.extract(str(soup), url=url, output_format="html", include_links=True,
                                        include_tables=True, include_comments=False, include_images=False)
        if extracted:
            body = clean_article(BeautifulSoup(extracted, "html.parser"), url)
        elif node and skill is GENERIC:
            body = clean_article(node, url)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) < 80:
        raise UrlImportError("empty_article", "未提取到足够的正文，网页可能需要渲染；也可手动补充正文")
    warnings = []
    if re.search(r"展开全文|阅读剩余|查看完整|此内容.*付费", body):
        warnings.append("正文可能不完整，请对照原文检查后确认")
    return {"title": title[:300], "body": body[:500000], "source_site": host,
            "skill": skill.name, "skill_version": skill.version, "warnings": warnings}
