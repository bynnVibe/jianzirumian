"""
见字如面 - 用户 Skill 配置 API（/api/skills）

侧边栏「Skill 配置」的后端支撑：用户管理自己的技能并决定是否应用。
- prompt 技能：注入对话系统提示，定制回答风格/领域约束；
- playbook 技能：链接解析的站点抓取策略（内置「网页链接抓取回退策略」即属此类）。
内置技能可开关，不可删除/改内容；自定义技能可增删改。所有操作按登录用户归属隔离。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.skill import skill_service
from app.services import skill_docs
from app.services.skill_market import skill_market

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillCreateRequest(BaseModel):
    name: str
    kind: str = "prompt"
    content: str = ""
    description: str = ""
    enabled: bool = True
    # 作用域：prompt 仅 chat；playbook 可选 url_import（链接解析）/ polish（正文清洗），缺省取首选
    scope: str | None = None
    # 可见性：personal（仅自己）/ public（全员可见可用），缺省 personal
    visibility: str = "personal"


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None


def _raise_if_error(res: dict):
    """把服务层返回的 {'error': ...} 转成合适的 HTTP 异常。"""
    err = res.get("error")
    if not err:
        return
    if err == "not_found":
        raise HTTPException(status_code=404, detail="技能不存在或无权访问")
    raise HTTPException(status_code=400, detail=err)


@router.get("")
async def list_skills(user: dict = Depends(get_current_user)):
    """列出我的技能（内置在前，按更新时间倒序）。"""
    return {"skills": skill_service.list_skills(user["id"])}


@router.post("")
async def create_skill(body: SkillCreateRequest, user: dict = Depends(get_current_user)):
    """新建自定义技能（含安全/能力预审，未通过则拒绝并留痕）。"""
    res = skill_service.create_skill(
        user["id"], body.name, body.kind, body.content,
        description=body.description, enabled=body.enabled,
        scope=body.scope, actor_name=user.get("username", ""),
        visibility=body.visibility,
    )
    _raise_if_error(res)
    return {"success": True, **res}


@router.get("/market/search")
async def search_skill_market(
    q: str = Query("", max_length=120),
    limit: int = Query(6, ge=1, le=12),
    user: dict = Depends(get_current_user),
):
    """从主流技能市场搜寻类似功能的 Skill（沉淀编辑界面用）。

    并发查询多个市场，归一化卡片（名称/描述/指令内容/来源/热度/链接）；
    单个市场不可达不影响整体，返回 errors 供前端提示。
    """
    return await skill_market.search(q, limit)


@router.put("/{skill_id}")
async def update_skill(skill_id: str, body: SkillUpdateRequest, user: dict = Depends(get_current_user)):
    """编辑技能；内置技能仅支持开关（enabled）。"""
    res = skill_service.update_skill(
        user["id"], skill_id, name=body.name, description=body.description,
        content=body.content, enabled=body.enabled,
        actor_name=user.get("username", ""),
    )
    _raise_if_error(res)
    return {"success": True, **res}


@router.get("/{skill_id}/doc")
async def get_skill_doc(skill_id: str, user: dict = Depends(get_current_user)):
    """L2 激活：拉取技能的完整说明书（SKILL.md frontmatter + 主体 + 可用 references 列表）。

    内置技能读落盘文档，自定义技能即时合成；references 只回路径/标题，正文由 L3 端点按需拉取。
    """
    skill = skill_service.get_skill(user["id"], skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在或无权访问")
    doc = skill_docs.doc_for_skill(user["id"], skill)
    if not doc:
        raise HTTPException(status_code=404, detail="技能文档不存在")
    return {"doc": doc}


@router.get("/{skill_id}/doc/ref")
async def get_skill_doc_reference(skill_id: str, path: str, user: dict = Depends(get_current_user)):
    """L3 执行：按需拉取 references/ 下单篇扩展文档（path 形如 references/site-routing.md）。"""
    skill = skill_service.get_skill(user["id"], skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在或无权访问")
    slug = skill.get("doc_slug") or skill_docs.resolve_builtin_slug(user["id"], skill_id)
    if not slug:
        raise HTTPException(status_code=404, detail="该技能无落盘参考文档")
    ref = skill_docs.load_reference(slug, path)
    if not ref:
        raise HTTPException(status_code=404, detail="参考文档不存在")
    return {"reference": ref}


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, user: dict = Depends(get_current_user)):
    """删除自定义技能（内置技能不可删除，可关闭）。"""
    res = skill_service.delete_skill(user["id"], skill_id)
    _raise_if_error(res)
    return {"success": True, "message": "技能已删除"}
