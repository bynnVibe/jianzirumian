"""公开链接导入：安全抓取边界、站点 Skill 提取与确认状态机的离线回归。

全部用例不触达真实网络：SSRF/规范化为纯函数断言，站点提取使用固定 HTML 样例，
状态机使用临时 SQLite 并将异步解析/入库替换为可控桩，仅验证认领、版本与权限逻辑。
"""
import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException

from app.core import db
from app.services.url_fetch import UrlImportError, normalize_url, public_address
from app.services.url_skills import extract_article


# ============================================
# 一、URL 规范化与 SSRF 边界
# ============================================

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a", "http://localhost/a", "http://10.0.0.5/a", "http://192.168.1.1/a",
    "http://172.16.0.1/a", "http://169.254.169.254/latest/meta-data", "http://[::1]/a",
    "http://[fe80::1]/a", "http://[::ffff:127.0.0.1]/a", "http://0.0.0.0/a", "http://224.0.0.1/a",
])
def test_normalize_url_blocks_private_and_reserved(url):
    with pytest.raises(UrlImportError) as exc:
        normalize_url(url)
    assert exc.value.code in {"unsafe_address", "invalid_url"}


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/a", "gopher://example.com/a", "javascript:alert(1)",
    "http://user:pass@example.com/a", "http://example.com:8080/a", "https://example.com:8443/a",
    "", "not-a-url", "http:///no-host",
])
def test_normalize_url_blocks_scheme_userinfo_and_ports(url):
    with pytest.raises(UrlImportError) as exc:
        normalize_url(url)
    assert exc.value.code == "invalid_url"


def test_normalize_url_accepts_public_and_preserves_query():
    # 公众号等页面依赖查询参数，规范化不得删除
    result = normalize_url("HTTPS://MP.Weixin.QQ.com/s?__biz=ABC&mid=9#frag")
    assert result == "https://mp.weixin.qq.com/s?__biz=ABC&mid=9"
    assert normalize_url("http://example.com") == "http://example.com/"


def test_normalize_url_accepts_default_ports_and_public_ipv6():
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"
    assert normalize_url("http://[2606:2800:220:1:248:1893:25c8:1946]/a").startswith("http://[2606:2800")


def test_public_address_rejects_non_global():
    for value in ("127.0.0.1", "10.1.2.3", "169.254.1.1", "::1", "224.0.0.1"):
        with pytest.raises(UrlImportError):
            public_address(value)
    assert str(public_address("8.8.8.8")) == "8.8.8.8"


# ============================================
# 二、站点 Skill 正文提取
# ============================================

def test_extract_wechat_article_uses_js_content():
    html = """<html><head><title>忽略</title></head><body>
      <h1 id="activity-name">公众号标题</h1>
      <div id="js_content"><p>第一段正文内容，足够长以通过质量校验。</p>
      <p>第二段正文，包含<a href="/relative">相对链接</a>与图片。</p>
      <img data-src="https://mmbiz.qpic.cn/x.png" alt="示意图" />
      <script>var evil=1;</script></div></body></html>"""
    result = extract_article(html, "https://mp.weixin.qq.com/s?__biz=ABC")
    assert result["skill"] == "wechat_article"
    assert result["title"] == "公众号标题"
    assert result["source_site"] == "mp.weixin.qq.com"
    assert "第一段正文内容" in result["body"]
    # 相对链接补全为绝对地址
    assert "https://mp.weixin.qq.com/relative" in result["body"]
    # 图片降级为链接而非远程加载；脚本被清除
    assert "[图片] 示意图" in result["body"]
    assert "evil" not in result["body"]


def test_extract_zhihu_article_uses_richtext():
    html = """<html><body><h1>知乎标题</h1>
      <div class="RichText ztext"><p>知乎公开回答的正文第一段，内容需要足够长才能通过最小正文校验阈值，因此这里多写一些说明性文字。</p>
      <p>第二段继续补充公开可见的回答细节，确保提取后的 Markdown 正文长度超过八十个字符。</p>
      <ul><li>要点一</li><li>要点二</li></ul></div></body></html>"""
    result = extract_article(html, "https://www.zhihu.com/question/1/answer/2")
    assert result["skill"] == "zhihu_article"
    assert "知乎公开回答的正文第一段" in result["body"]
    assert "要点一" in result["body"]


def test_extract_generic_blog_article():
    html = """<html><head><title>博客标题</title></head><body>
      <nav>导航 首页 关于</nav>
      <article><h1>文章主标题</h1>
      <p>这是一篇通用博客文章的正文段落，内容需要足够长以通过通用提取器的最小正文校验。</p>
      <p>第二个段落继续补充说明，确保 trafilatura 能够识别为正文而非样板导航内容。</p>
      <table><tr><td>单元格A</td><td>单元格B</td></tr></table></article>
      <footer>版权信息</footer></body></html>"""
    result = extract_article(html, "https://blog.example.com/post/1")
    assert result["skill"] == "generic_article"
    assert "通用博客文章的正文段落" in result["body"]


def test_extract_restricted_verification_page():
    html = """<html><body><h1>安全验证</h1>
      <p>访问过于频繁，请完成验证后继续。扫码登录后即可查看完整内容。</p></body></html>"""
    with pytest.raises(UrlImportError) as exc:
        extract_article(html, "https://mp.weixin.qq.com/s?__biz=X")
    assert exc.value.code == "restricted"


def test_extract_empty_article_raises():
    html = "<html><body><h1>标题</h1><div id='js_content'><p>短</p></div></body></html>"
    with pytest.raises(UrlImportError) as exc:
        extract_article(html, "https://mp.weixin.qq.com/s")
    assert exc.value.code == "empty_article"


# ============================================
# 三、确认状态机（隔离、版本、权限、幂等）
# ============================================

@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.core.agent_runtime import runtime
    from app.services import url_imports as mod

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(runtime, "require_owner", lambda: None)
    # create/confirm 不触发真实异步解析与入库
    monkeypatch.setattr(mod.UrlImportService, "schedule", lambda self, task_id: None)

    async def _noop_ingest(self, task_id):
        return None
    monkeypatch.setattr(mod.UrlImportService, "ingest", _noop_ingest)

    def seed_user(uid, role="user"):
        return {"id": uid, "role": role, "is_active": 1}

    def seed_kb(kb_id, visibility, owner_id):
        db.execute("INSERT INTO knowledge_bases(id,name,topic,visibility,owner_id,created_at) VALUES(?,?,?,?,?,?)",
                   (kb_id, kb_id, "其他", visibility, owner_id, "2026-01-01"))

    def seed_task(owner, *, status="done", phase="parse", revision=1, kb_id="",
                  parsed_text="已确认的正文内容", original_text="已确认的正文内容", source_url="https://example.com/a"):
        task_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO url_imports(id,owner_id,source_url,title,parsed_text,original_title,original_text,"
            "metadata,status,phase,revision,kb_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, owner, source_url, "标题", parsed_text, "标题", original_text,
             json.dumps({"source_site": "example.com"}), status, phase, revision, kb_id, "2026-01-01", "2026-01-01"))
        return task_id

    yield {"service": mod.url_imports, "user": seed_user, "kb": seed_kb, "task": seed_task}
    if db._conn is not None:
        db._conn.close()


def test_formal_user_rejects_guest(env):
    with pytest.raises(HTTPException) as exc:
        env["service"].list({"id": "guest", "role": "user", "is_active": 1})
    assert exc.value.status_code == 403


def test_task_isolated_between_users(env):
    user_a, user_b = env["user"]("a"), env["user"]("b")
    task = env["task"]("a")
    assert env["service"].get(task, user_a)["id"] == task
    with pytest.raises(HTTPException) as exc:
        env["service"].get(task, user_b)
    assert exc.value.status_code == 404


def test_save_revision_conflict_and_content_origin(env):
    user = env["user"]("a")
    task = env["task"]("a", revision=3)
    # 版本不匹配 → 409，不覆盖
    with pytest.raises(HTTPException) as exc:
        env["service"].save(task, user, 2, "新标题", "新正文")
    assert exc.value.status_code == 409
    # 版本匹配 → revision+1，用户编辑标记为 user_edited
    saved = env["service"].save(task, user, 3, "新标题", "用户改过的正文")
    assert saved["revision"] == 4
    assert saved["title"] == "新标题"
    assert saved["content_origin"] == "user_edited"


def test_save_rejected_after_confirm(env):
    user = env["user"]("a")
    task = env["task"]("a", phase="ingest", status="ingesting")
    with pytest.raises(HTTPException) as exc:
        env["service"].save(task, user, 1, "t", "x")
    assert exc.value.status_code == 409


def test_retry_only_from_parse_phase(env):
    user = env["user"]("a")
    parse_task = env["task"]("a", status="error", revision=1)
    retried = env["service"].retry(parse_task, user, 1)
    assert retried["status"] == "pending" and retried["revision"] == 2
    ingest_task = env["task"]("a", status="interrupted", phase="ingest", revision=1)
    with pytest.raises(HTTPException) as exc:
        env["service"].retry(ingest_task, user, 1)
    assert exc.value.status_code == 409


def test_confirm_enforces_target_write_permission(env):
    env["kb"]("pub", "public", "")
    env["kb"]("priv-b", "private", "b")
    env["kb"]("priv-a", "private", "a")
    user = env["user"]("a")

    # 公开库：非管理员拒绝
    task1 = env["task"]("a")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_confirm(env["service"], task1, user, 1, "pub"))
    assert exc.value.status_code == 403

    # 他人私人库：拒绝
    task2 = env["task"]("a")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_confirm(env["service"], task2, user, 1, "priv-b"))
    assert exc.value.status_code == 403

    # 本人私人库：原子认领 done→ingesting
    task3 = env["task"]("a")
    result = asyncio.run(_confirm(env["service"], task3, user, 1, "priv-a"))
    assert result["status"] == "ingesting" and result["phase"] == "ingest" and result["kb_id"] == "priv-a"


def test_confirm_admin_may_write_public(env):
    env["kb"]("pub", "public", "")
    admin = env["user"]("admin", role="admin")
    task = env["task"]("admin")
    result = asyncio.run(_confirm(env["service"], task, admin, 1, "pub"))
    assert result["status"] == "ingesting"


def test_confirm_idempotent_after_success(env):
    env["kb"]("priv-a", "private", "a")
    user = env["user"]("a")
    task = env["task"]("a", status="succeeded", phase="ingest", kb_id="priv-a", revision=5)
    # 已成功且同库：返回原结果，不重复入库
    result = asyncio.run(_confirm(env["service"], task, user, 5, "priv-a"))
    assert result["status"] == "succeeded"
    # 版本不符：409
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_confirm(env["service"], task, user, 4, "priv-a"))
    assert exc.value.status_code == 409


def test_delete_blocked_during_ingest(env):
    user = env["user"]("a")
    task = env["task"]("a", status="ingesting", phase="ingest")
    with pytest.raises(HTTPException) as exc:
        env["service"].delete(task, user)
    assert exc.value.status_code == 409
    draft = env["task"]("a", status="done", phase="parse")
    env["service"].delete(draft, user)
    with pytest.raises(HTTPException):
        env["service"].get(draft, user)


def test_capacity_limit_returns_429(env):
    user = env["user"]("a")
    for _ in range(10):
        env["task"]("a", status="pending")
    with pytest.raises(HTTPException) as exc:
        env["service"].create("https://example.com/new", user)
    assert exc.value.status_code == 429


async def _confirm(service, task_id, user, revision, kb_id):
    """在事件循环内调用 confirm，使其内部 create_task(ingest) 可用（ingest 已被替换为空桩）。"""
    return service.confirm(task_id, user, revision, kb_id)
