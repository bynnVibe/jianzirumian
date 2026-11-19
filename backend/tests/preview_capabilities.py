"""能力添加浏览器验收入口：真实路由和状态机，外部资料／模型使用显式离线桩。

运行 --check 做隔离冒烟；运行 --serve 在 127.0.0.1:18765 提供最新 dist。
不加载 .env，不访问业务 SQLite，不启动后台恢复，不连接外网或任何本地上游。
此入口不是生产服务，也不证明真实模型、API 或 Linux Worker 验收通过。
"""
import argparse
import asyncio
import json
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".dev-logs" / "production-assessment"


def build_preview():
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="browser-", dir=OUT))
    # 清除包括开发固定登录态、凭据、Worker 和代理在内的继承环境。
    path = os.environ.get("PATH", "")
    os.environ.clear()
    os.environ.update(PATH=path, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HOME=str(work / "hf"), PERSONAL_TOOLS_ENABLED="true")
    sys.path.insert(0, str(ROOT / "backend"))

    def blocked(*args, **kwargs):
        raise RuntimeError("离线浏览器验收禁止真实出站连接")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
    with patch("dotenv.load_dotenv", return_value=False), patch("os.makedirs"):
        from app import config
    config.BASE_DIR = work
    config.DATA_DIR = work / "data"
    for attr, suffix in (("UPLOAD_DIR", "uploads"), ("KNOWLEDGE_DIR", "knowledge"),
                         ("WIKI_DIR", "wiki"), ("VECTOR_STORE_PATH", "vector_store/faiss_index")):
        target = config.DATA_DIR / suffix
        directory = target.parent if attr == "VECTOR_STORE_PATH" else target
        directory.mkdir(parents=True, exist_ok=True)
        setattr(config.settings, attr, str(target))
    config.settings.FILE_CACHE_ENABLED = False
    config.settings.WIKI_COMPILE_ENABLED = False
    from app.core import db, log_setup
    db.DB_PATH = work / "app.db"
    db._conn = None
    log_setup.setup_logging = lambda *args, **kwargs: None
    original_connect = sqlite3.connect

    def isolated_connect(database, *args, **kwargs):
        if str(database) != ":memory:" and not Path(database).resolve().is_relative_to(work):
            raise RuntimeError("拒绝访问本次预览目录外的 SQLite")
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = isolated_connect
    from app.main import app as backend
    from app.services import auth, capability_gap, skill_workflow, tool_workflow
    from app.services import personal_tools, personal_tool_specs as specs
    from app.services import chat as chat_module
    from app.services.skill import skill_service
    from app.services.web_search import SearchResult

    # 仅限本次临时数据库的合成账号，不是用户的真实凭据。
    users = [auth.register(name, "Preview-only-2026!", name + "@example.invalid")
             for name in ("preview-admin", "preview-user", "preview-other")]
    for user in users:
        for query, scenario in (("北京现在的天气", "realtime_weather"),
                                ("北京未来三天天气预报", "realtime_weather"),
                                ("杭州坐标海拔查询", "realtime_data")):
            for i in range(3):
                capability_gap.record_gap(user_id=user["id"], query=query, scenario=scenario,
                                          request_id=f"fixture-{query}-{i}")
        skill_service.list_skills(user["id"])

    async def discover(owner, description):
        await asyncio.sleep(5 if "天气" in description or "慢速" in description else 0.3)
        return [], [], ["离线验收：未访问真实市场或外部来源"]

    async def search(query):
        return []

    async def model_json(owner, system, value):
        if "required_tools" in system:
            request = str(value.get("request", ""))
            need = "天气" in request or "依赖" in request
            return {"name": "离线样例·" + request[:20], "slug": "preview-workflow",
                    "description": "离线生成样例，验证草稿确认与历史恢复；未验证实际业务能力。",
                    "body": "# 离线验收工作流\n\n先说明资料范围，再分点总结。\n\n"
                            "仅为离线浏览器测试生成，不代表真实工具或实时数据。",
                    "required_tools": ["weather_current_api"] if need else []}
        if "skill_id" in system:
            return {"skill_id": ""}
        return {"query": str(value.get("request", ""))[:80]}

    skill_workflow.discover = discover
    skill_workflow._search = search
    skill_workflow._model_json = model_json

    async def tool_draft(owner, session_id, message_id, description):
        await asyncio.sleep(0.5)
        if "邮件" in description:
            raise ValueError("首版仅开放只读 API 与隔离计算，不能添加外部副作用工具")
        spec = specs.connector_spec("weather")
        if "计算" in description:
            spec.update(name="离线样例·隔离加倍计算", executor="python",
                        config={"code": "def main(data):\n    return {'value': data['n'] * 2}"},
                        input_schema=specs.object_schema({"n": {"type": "number"}}),
                        output_schema=specs.object_schema({"value": {"type": "number"}}),
                        tests=[{"input": {"n": 2}, "assertions": [{"path": "/value", "op": "equals", "value": 4}]}],
                        sources=[])
        spec["limitations"] += "；离线浏览器样例，未验证真实 API／Worker。"
        return personal_tools.propose(owner, spec, session_id, message_id)

    tool_workflow.build_draft = tool_draft

    class OfflineLLM:
        extra_body = None
        model = "offline-browser-fixture"
        cache_info = {}

        async def chat(self, messages, stream=True, **kwargs):
            query = messages[-1].get("content", "")
            if "长文" in query:
                body = "\n\n".join(f"第 {i} 段：这是离线流式样例，用于检查向上滚动后不会被拉回底部。" for i in range(1, 61))
            else:
                body = ("这是离线浏览器验收回答，不是实时查询结果。\n\n"
                        "已按当前选项完成测试流程，可检查过程折叠与历史恢复。\n\n"
                        "| 项目 | 状态 |\n| --- | --- |\n| 外部上游 | 未连接 |\n| 数据来源 | 合成测试资料 |\n\n"
                        "```python\nprint('offline-browser-fixture')\n```\n\n"
                        "<img src=x onerror=alert(1)>\n\n[非法链接](javascript:alert(1))")
                if "知识上下文" in query or "来源" in query:
                    body += "\n\n引用测试资料 [来源 1]。"
            await asyncio.sleep(0.8)
            for offset in range(0, len(body), 20):
                await asyncio.sleep(0.08)
                yield body[offset:offset + 20]

    async def complexity(*args, **kwargs):
        return {"mode": "fast"}

    async def intent(query, *args, **kwargs):
        return {"query": query, "freshness": "static", "scope": "personal"}

    async def evidence(*args, **kwargs):
        return True, ""

    async def web_results(query, *args, **kwargs):
        return [SearchResult(title="离线测试网页（非实时资料）", url="https://example.invalid/fixture", snippet="合成资料，仅用于界面验收。")]

    chat_module.LLMFactory.create_for_role = lambda *args, **kwargs: OfflineLLM()
    chat_module.classify_complexity = complexity
    chat_module.analyze_intent = intent
    chat_module.check_evidence_sufficiency = evidence
    chat_module.web_search = web_results
    chat_module.knowledge_service.retrieve_for_user = lambda *args, **kwargs: [("合成知识证据", {}, 1.0)]
    chat_module.knowledge_service.format_knowledge_context = lambda *args, **kwargs: (
        "[来源 1] 离线合成知识证据，仅用于界面验收。",
        [{"index": 1, "filename": "离线测试资料", "score": 1.0, "text": "合成知识证据"}])

    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
    frontend = FastAPI()
    dist = ROOT / "frontend" / "dist"
    frontend.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @frontend.get("/{path:path}")
    async def page(path: str):
        target = (dist / path).resolve()
        if not target.is_relative_to(dist.resolve()):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target)
        text = (dist / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(text.replace("<title>", "<title>离线验收 · "), headers={"Cache-Control": "no-store"})

    async def application(scope, receive, send):
        target = backend if scope.get("path", "").startswith(("/api/", "/v1/", "/uploads/")) else frontend
        await target(scope, receive, send)

    print(f"离线验收目录：{work}", flush=True)
    print("仅使用临时账号 preview-admin / preview-user / preview-other，密码 Preview-only-2026!", flush=True)
    return application, users


def check(application):
    from fastapi.testclient import TestClient
    client = TestClient(application)
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["knowledge_count"] == 0
    login = client.post("/api/auth/login", json={"username": "preview-user", "password": "Preview-only-2026!"})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    suggestions = client.get("/api/skills/suggestions").json()["suggestions"]
    assert len(suggestions) == 3
    ids = client.post("/api/skills/suggestions/seen", json={"ids": [s["id"] for s in suggestions]}).json()["ids"]
    assert len(ids) == 3
    accepted = client.post(f"/api/skills/suggestions/{ids[0]}/accept")
    assert accepted.status_code == 200, accepted.text
    proposal = accepted.json()["draft"]
    assert proposal["status"] == "pending"
    for query in ("@skills 分点总结", "@tool 隔离计算", "普通回答"):
        response = client.post("/api/chat/send", json={"query": query, "use_knowledge": True, "use_search": False})
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["event"] == "done", response.text
        assert not any(e["event"] == "error" for e in events), response.text
        if query.startswith("@"):
            assert any(e["event"] in ("skill_draft", "tool_draft") for e in events), response.text
    assert client.get("/skills").status_code == 200
    print("隔离预览冒烟通过：真实登录、个人建议、接受草稿、命令与普通 SSE。", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    application, _ = build_preview()
    if args.check:
        check(application)
    else:
        import uvicorn
        uvicorn.run(application, host="127.0.0.1", port=18765, lifespan="off", proxy_headers=False)
