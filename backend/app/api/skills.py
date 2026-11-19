"""
见字如面 - 用户 Skill 配置 API（/api/skills）

侧边栏「Skill 配置」的后端支撑：用户管理自己的技能并决定是否应用。
- prompt 技能：注入对话系统提示，定制回答风格/领域约束；
- playbook 技能：链接解析的站点抓取策略（内置「网页链接抓取回退策略」即属此类）。
内置技能可开关，不可删除/改内容；自定义技能可增删改。所有操作按登录用户归属隔离。
"""
import asyncio
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import Response
from app.core import db

from app.api.deps import get_current_user
from app.services.skill import skill_service
from app.services import skill_docs
from app.services import skill_drafts, skill_packages, skill_dependencies, capability_gap
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


class SuggestionSeenRequest(BaseModel):
    ids: list[str] = Field(max_length=20)


class BusinessAcceptanceRequest(BaseModel):
    evidence_id: str = Field(min_length=32, max_length=32)
    expected_hash: str = Field(min_length=64, max_length=64)
    accepted: bool = Field(strict=True)
    note: str = Field(min_length=4, max_length=1000)


class SkillDraftConfirmRequest(BaseModel):
    expected_hash: str = Field(default="", max_length=64)
    allow_same_name: bool = False
    expected_dependency_hash: str = Field(default="", max_length=64)


class SkillDependencyRequest(BaseModel):
    expected_hash: str = Field(min_length=64, max_length=64)
    requirement: str = Field(min_length=1, max_length=247)


class SkillDependencyBinding(SkillDependencyRequest):
    tool_id: str = Field(default="", max_length=64)
    version: str = Field(default="", max_length=64)


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


@router.get("/suggestions")
async def personal_suggestions(user: dict = Depends(get_current_user)):
    result = capability_gap.frequency_suggestions(user_id=user["id"])
    rows = db.query("SELECT DISTINCT d.id,d.created_at FROM skill_drafts d JOIN capability_suggestions s "
                    "ON s.owner_id=d.owner_id AND (s.draft_id=d.id OR (s.session_id=d.session_id AND s.message_id=d.message_id)) "
                    "WHERE d.owner_id=? AND d.status IN ('pending','imported') AND s.status!='resolved' "
                    "ORDER BY d.created_at DESC LIMIT 20", (user["id"],))
    drafts = [skill_drafts.get_draft(row["id"], user["id"]) for row in rows]
    result["drafts"] = [draft for draft in drafts if draft["status"] in ("pending", "imported")]
    result["acceptances"] = capability_gap.pending_acceptances(user["id"])
    result["acceptance_history"] = capability_gap.acceptance_history(user["id"])
    return result


@router.post("/suggestions/seen")
async def mark_personal_suggestions_seen(body: SuggestionSeenRequest, user: dict = Depends(get_current_user)):
    return capability_gap.mark_suggestions_seen(user["id"], body.ids)


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_personal_suggestion(suggestion_id: str, user: dict = Depends(get_current_user)):
    return capability_gap.dismiss_suggestion(user["id"], suggestion_id)


@router.post("/suggestions/{suggestion_id}/acceptance")
async def confirm_personal_business_acceptance(suggestion_id: str, body: BusinessAcceptanceRequest,
                                                user: dict = Depends(get_current_user)):
    return capability_gap.confirm_business_acceptance(user["id"], suggestion_id, **body.model_dump())


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_personal_suggestion(suggestion_id: str, request: Request, user: dict = Depends(get_current_user)):
    task = asyncio.create_task(capability_gap.accept_suggestion(user["id"], suggestion_id))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.2)
            if await request.is_disconnected():
                raise asyncio.CancelledError()
        return await task
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(504, "发现超时，未安装能力，可刷新后重试") from exc
    except Exception as exc:
        raise HTTPException(502, "资料发现或草稿生成失败，未安装能力，可刷新后重试") from exc
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


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


# ---- 聊天沉淀 Skill 草稿确认（先于 /{skill_id} 注册，避免路径参数吞掉 drafts 段）----

@router.get("/drafts/{draft_id}")
async def get_skill_draft(draft_id: str, user: dict = Depends(get_current_user)):
    """查询草稿状态（含过期自动判定，刷新后确认卡恢复状态用）。"""
    return skill_drafts.get_draft(draft_id, user["id"])


@router.post("/drafts/{draft_id}/confirm")
async def confirm_skill_draft(draft_id: str, body: SkillDraftConfirmRequest | None = None,
                              user: dict = Depends(get_current_user)):
    """确认导入：系统自动把草稿写入本人个人 Skill 配置（默认启用，含安全预审）。"""
    body = body or SkillDraftConfirmRequest()
    return skill_drafts.confirm(draft_id, user["id"], actor_name=user.get("username", ""),
                                expected_hash=body.expected_hash, allow_same_name=body.allow_same_name,
                                expected_dependency_hash=body.expected_dependency_hash)


@router.put("/drafts/{draft_id}/dependencies")
async def bind_skill_dependency(draft_id: str, body: SkillDependencyBinding, user=Depends(get_current_user)):
    try:
        return skill_dependencies.bind(user["id"], draft_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, "依赖内容无效，请重新预览") from exc


@router.post("/drafts/{draft_id}/dependencies/discover")
async def discover_skill_dependency(draft_id: str, body: SkillDependencyRequest, request: Request,
                                    user=Depends(get_current_user)):
    task = asyncio.create_task(skill_dependencies.discover(user["id"], draft_id, **body.model_dump()))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.2)
            if await request.is_disconnected():
                raise asyncio.CancelledError()
        return await task
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(504, "依赖发现超时，未安装工具") from exc
    except Exception as exc:
        raise HTTPException(422, "依赖发现失败或不兼容，未安装工具；可选择已有工具或补充 API 文档") from exc
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


@router.get("/drafts/{draft_id}/download")
async def download_skill_draft(draft_id: str, expected_hash: str = Query(..., max_length=64),
                               user: dict = Depends(get_current_user)):
    bundle = skill_drafts.draft_package(draft_id, user["id"], expected_hash)
    slug = skill_packages.validate(bundle)["metadata"]["name"]
    return Response(skill_packages.export_zip(bundle), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{slug}.zip"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/drafts/{draft_id}/resource")
async def get_skill_draft_resource(draft_id: str, path: str = Query(..., max_length=240),
                                   expected_hash: str = Query(..., max_length=64),
                                   user: dict = Depends(get_current_user)):
    bundle = skill_drafts.draft_package(draft_id, user["id"], expected_hash)
    try:
        return skill_packages.resource(bundle, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/drafts/{draft_id}/cancel")
async def cancel_skill_draft(draft_id: str, user: dict = Depends(get_current_user)):
    """取消草稿（不写入任何配置）。"""
    return skill_drafts.cancel(draft_id, user["id"])


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
    if skill["kind"] == "agent_skill":
        try:
            bundle = skill_packages.load(user["id"], skill_id)
            info = skill_packages.validate(bundle)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        draft = db.query_one("SELECT * FROM skill_drafts WHERE owner_id=? AND skill_id=? AND status='imported' "
                             "ORDER BY updated_at DESC LIMIT 1", (user["id"], skill_id))
        return {"doc": {"slug": info["metadata"]["name"], "name": info["metadata"]["name"],
                        "title": skill["name"], "description": skill["description"],
                        "kind": "agent_skill", "scope": "chat", "version": info["hash"],
                        "body": info["texts"]["SKILL.md"], "source": "package",
                        "review": skill_dependencies.installed_review(user["id"], skill_id, bundle),
                        "dependency_draft": skill_drafts.serialize(draft) if draft else None,
                        "provenance": bundle.get("provenance", {}),
                        "references": [{**r, "title": r["path"]} for r in info["files"] if r["path"] != "SKILL.md"]}}
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
    if skill["kind"] == "agent_skill":
        try:
            return {"reference": skill_packages.load_resource(user["id"], skill_id, path)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
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
