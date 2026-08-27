"""
见字如面 - 认证 API 路由
"""
import logging

from fastapi import APIRouter, HTTPException, Header

from app.core import db
from app.services import auth as auth_service
from app.services.config_manager import runtime_config
from app.services.usage import estimate_tokens

logger = logging.getLogger("jianziruyang.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_token(authorization: str = Header("")) -> str:
    """从 Authorization header 提取 token"""
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return ""


# ============================================
# 公开接口
# ============================================


@router.post("/register")
async def register(body: dict):
    """注册新用户"""
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    contact = (body.get("contact") or "").strip()

    # 校验注册功能是否开启
    if not runtime_config.registration_enabled:
        raise HTTPException(400, "管理员已关闭注册功能")

    # 校验
    if not username or len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if not password or len(password) < 6:
        raise HTTPException(400, "密码至少 6 个字符")
    if not contact:
        raise HTTPException(400, "联系方式不能为空")

    try:
        user = auth_service.register(username, password, contact)
        return {"success": True, "user": user}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/login")
async def login(body: dict):
    """用户登录"""
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")

    try:
        token, user = auth_service.login(username, password)
        if token is None:
            # 区分"用户不存在"和"密码错误"
            raise HTTPException(401, "用户名或密码错误")
        return {"success": True, "token": token, "user": user}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/logout")
async def logout(authorization: str = Header("")):
    """登出"""
    token = _get_token(authorization)
    if token:
        auth_service.logout(token)
    return {"success": True}


@router.get("/me")
async def get_me(authorization: str = Header("")):
    """获取当前登录用户信息"""
    token = _get_token(authorization)
    if not token:
        raise HTTPException(401, "未登录")
    user = auth_service.get_session_user(token)
    if not user:
        raise HTTPException(401, "登录已过期，请重新登录")
    return {"success": True, "user": user}


@router.put("/me")
async def update_me(body: dict, authorization: str = Header("")):
    """修改当前登录用户的个人信息（用户名/联系方式/密码）"""
    token = _get_token(authorization)
    if not token:
        raise HTTPException(401, "未登录")
    try:
        user = auth_service.update_profile(
            token,
            username=body.get("username"),
            contact=body.get("contact"),
            old_password=body.get("old_password"),
            new_password=body.get("new_password"),
        )
        return {"success": True, "user": user}
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/me/stats")
async def my_usage_stats(days: int = 30, authorization: str = Header("")):
    """当前用户个人使用看板：近 N 天每日趋势、知识库/联网/上传次数、问答频率、好评差评等"""
    token = _get_token(authorization)
    if not token:
        raise HTTPException(401, "未登录")
    user = auth_service.get_session_user(token)
    if not user:
        raise HTTPException(401, "登录已过期，请重新登录")

    uid = user["id"]
    days = max(1, min(days, 365))

    from app.services.usage import get_user_usage
    usage = get_user_usage(uid, days=days)

    # 反馈统计
    fb_rows = db.query(
        "SELECT rating, COUNT(*) AS cnt FROM message_feedbacks"
        " WHERE user_id = ? GROUP BY rating",
        (uid,),
    )
    fb = {r["rating"]: r["cnt"] for r in fb_rows}

    # 会话数 / 收藏数 / 知识条目数
    session_count = db.query_one(
        "SELECT COUNT(*) AS cnt FROM chat_sessions WHERE user_id = ?", (uid,)
    )["cnt"]
    bookmark_count = db.query_one(
        "SELECT COUNT(*) AS cnt FROM bookmarks WHERE user_id = ?", (uid,)
    )["cnt"]
    kb_entry_count = db.query_one(
        "SELECT COUNT(*) AS cnt FROM knowledge_bases WHERE owner_id = ?", (uid,)
    )["cnt"]

    return {
        "success": True,
        "user": user,
        "days": days,
        "daily": usage["daily"],
        "total_query_count": usage["total_query_count"],
        "total_tokens": usage["total_tokens"],
        "model_calls": usage["model_calls"],
        "total_knowledge_count": usage["total_knowledge_count"],
        "total_web_search_count": usage["total_web_search_count"],
        "total_upload_count": usage["total_upload_count"],
        "active_days": usage["active_days"],
        "like_count": fb.get("like", 0),
        "dislike_count": fb.get("dislike", 0),
        "session_count": session_count,
        "bookmark_count": bookmark_count,
        "kb_count": kb_entry_count,
    }


# ============================================
# 管理员接口
# ============================================


@router.get("/users")
async def list_users(authorization: str = Header("")):
    """获取所有用户列表（仅管理员）"""
    token = _get_token(authorization)
    try:
        users = auth_service.get_all_users(token)
        return {"success": True, "users": users}
    except PermissionError as e:
        raise HTTPException(403, str(e))


@router.put("/users/{user_id}/active")
async def toggle_user_active(user_id: str, body: dict, authorization: str = Header("")):
    """启用/禁用用户（仅管理员）"""
    token = _get_token(authorization)
    is_active = body.get("is_active", True)
    try:
        user = auth_service.set_user_active(token, user_id, is_active)
        return {"success": True, "user": user}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/users/{user_id}")
async def remove_user(user_id: str, authorization: str = Header("")):
    """删除用户（仅管理员）"""
    token = _get_token(authorization)
    try:
        auth_service.delete_user(token, user_id)
        return {"success": True}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ============================================
# 用户统计看板
# ============================================


def _feedback_counts() -> dict:
    """按用户统计点赞/点踩数：{user_id: {like, dislike}}"""
    rows = db.query(
        "SELECT user_id, rating, COUNT(*) AS cnt"
        " FROM message_feedbacks GROUP BY user_id, rating"
    )
    counts: dict = {}
    for r in rows:
        c = counts.setdefault(r["user_id"], {"like": 0, "dislike": 0})
        if r["rating"] in c:
            c[r["rating"]] = r["cnt"]
    return counts


def _disliked_questions(user_id: str, limit: int = 50) -> list[dict]:
    """用户点踩的回答对应的提问列表（回答质量评估）"""
    rows = db.query(
        "SELECT f.message_id, f.session_id, f.comment, f.created_at,"
        " m.seq AS answer_seq, m.content AS answer_content"
        " FROM message_feedbacks f"
        " JOIN chat_messages m ON m.message_id = f.message_id"
        " WHERE f.user_id = ? AND f.rating = 'dislike'"
        " ORDER BY f.created_at DESC LIMIT ?",
        (user_id, limit),
    )
    result = []
    for r in rows:
        # 取该回答前最近的一条用户提问
        q = db.query_one(
            "SELECT content FROM chat_messages"
            " WHERE session_id = ? AND seq < ? AND role = 'user'"
            " ORDER BY seq DESC LIMIT 1",
            (r["session_id"], r["answer_seq"]),
        )
        result.append({
            "question": q["content"] if q else "",
            "answer_preview": (r["answer_content"] or "")[:120],
            "comment": r["comment"],
            "created_at": r["created_at"],
        })
    return result


@router.get("/users/stats")
async def users_stats(authorization: str = Header("")):
    """用户统计看板汇总（仅管理员）：最近登录、近 30 天查询/Token/调用数、好评差评数"""
    token = _get_token(authorization)
    try:
        users = auth_service.get_all_users(token)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    # 近 30 天用量按用户汇总
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    usage_rows = db.query(
        "SELECT user_id, SUM(query_count) AS queries,"
        " SUM(prompt_chars + completion_chars) AS chars"
        " FROM usage_daily WHERE day >= ? GROUP BY user_id",
        (since,),
    )
    usage_map = {r["user_id"]: r for r in usage_rows}
    fb_counts = _feedback_counts()

    stats = []
    for u in users:
        uid = u["id"]
        usage = usage_map.get(uid)
        fb = fb_counts.get(uid, {"like": 0, "dislike": 0})
        queries = usage["queries"] if usage else 0
        stats.append({
            **u,
            "query_count": queries,
            "tokens": estimate_tokens(usage["chars"]) if usage else 0,
            "model_calls": queries,
            "like_count": fb["like"],
            "dislike_count": fb["dislike"],
        })
    return {"success": True, "stats": stats}


@router.get("/users/{user_id}/stats")
async def user_stats_detail(user_id: str, authorization: str = Header("")):
    """单个用户统计详情（仅管理员）：近 30 天每日查询趋势、点踩问题清单"""
    token = _get_token(authorization)
    try:
        auth_service.get_all_users(token)  # 仅校验管理员权限
    except PermissionError as e:
        raise HTTPException(403, str(e))

    from app.services.usage import get_user_usage
    usage = get_user_usage(user_id, days=30)
    fb_rows = db.query(
        "SELECT rating, COUNT(*) AS cnt FROM message_feedbacks"
        " WHERE user_id = ? GROUP BY rating",
        (user_id,),
    )
    fb = {r["rating"]: r["cnt"] for r in fb_rows}

    return {
        "success": True,
        "daily": usage["daily"],
        "total_query_count": usage["total_query_count"],
        "total_tokens": usage["total_tokens"],
        "model_calls": usage["model_calls"],
        "like_count": fb.get("like", 0),
        "dislike_count": fb.get("dislike", 0),
        "disliked_questions": _disliked_questions(user_id),
    }
