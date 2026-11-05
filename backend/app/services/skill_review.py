"""
见字如面 - 用户 Skill 添加预审（安全评审 + 能力可行性评审）

借鉴主流 Agent Skill 安全评审要点（提示注入/越权角色扮演、系统与凭据泄露、
数据外传、凭据收集、有害内容生成），并结合本系统技能的实际能力边界
（prompt 技能仅注入对话系统提示；playbook 技能仅驱动链接抓取/正文清洗策略，
不具备执行命令、读写文件、发消息、定时任务、直连数据库等真实执行能力），
在用户**添加/修改**技能时先行评审：

- 命中安全风险（high）或超出系统能力（unsupported）→  verdict=reject，
  明确告知用户原因且该技能不会落库；
- 每次评审（含通过）都写入 skill_review_logs 表，拒绝项额外写 WARNING 日志，
  供「Agent 可观测性」页重点暴露（/api/observability/skill-reviews）。
"""
import json
import logging
import re
import secrets
from datetime import datetime
from typing import List, Optional

from app.core import db

logger = logging.getLogger("jianziruyang.skill_review")

# 评审动作
ACTIONS = ("create", "update", "import")

# ---- 高安全风险规则（主流 Skill 安全评审维度）----
_SECURITY_RULES = [
    ("prompt_injection", "试图覆盖系统指令或角色设定（提示注入/越狱）", re.compile(
        r"忽略(之前|以上|上述|系统)|(之前|以上|上述)的?(全部)?指令|无视(系统|规则)|"
        r"ignore (previous|above|prior|all|system) (instructions?|prompts?|rules?)|disregard (previous|above|system)|"
        r"you are now|从现在起你是|扮演不受限制|不受任何限制|DAN mode|jailbreak|越狱模式", re.I)),
    ("system_leak", "要求泄露系统提示词/密钥/内部配置", re.compile(
        r"(输出|打印|复述|泄露|告诉我|展示).{0,12}(系统提示|system prompt|密钥|api key|apikey|内部配置|隐藏指令)|"
        r"reveal (system prompt|keys|secrets|hidden instructions)", re.I)),
    ("credential_harvest", "诱导收集用户凭据/敏感信息", re.compile(
        r"(提供|输入|填写|告知).{0,10}(密码|验证码|银行卡|身份证|信用卡).{0,24}(发送|提交|回传|告诉|回复)|"
        r"收集.{0,10}(密码|验证码|银行卡|身份证)", re.I)),
    ("data_exfil", "指示把对话/用户数据外传到外部地址", re.compile(
        r"(发送|上传|POST|提交|携带).{0,24}(到|至).{0,12}(https?://|外部(接口|服务器|地址))|"
        r"https?://\S{0,60}(携带|包含|附上).{0,10}(用户数据|对话(记录|内容)|聊天(记录|内容))", re.I)),
    ("harmful_content", "指示生成攻击/欺诈/恶意代码等有害内容", re.compile(
        r"(编写|生成|提供|教我).{0,12}(病毒|木马|恶意代码|勒索(软件|脚本)|钓鱼(邮件|网站)|诈骗(话术|脚本)|攻击脚本|爆破|漏洞利用|免杀)", re.I)),
]

# ---- 能力可行性规则：本系统技能无法真实执行的行为 ----
_CAPABILITY_RULES = [
    ("exec_code", "执行命令/代码/脚本", re.compile(
        r"(执行|运行).{0,8}(shell|bash|终端命令|系统命令|python|代码|脚本)|"
        r"(run|execute).{0,8}(shell|bash|command|code|script)", re.I)),
    ("file_io", "读写本地文件/落盘", re.compile(
        r"(读取|写入|保存|删除|修改).{0,8}(本地文件|磁盘|文件系统|服务器文件)|"
        r"(read|write).{0,6}local files?", re.I)),
    ("send_message", "发送邮件/短信/呼叫", re.compile(
        r"(发送|发).{0,6}(邮件|短信|email|sms)|(拨打|呼叫).{0,6}电话", re.I)),
    ("schedule", "定时/周期自动执行", re.compile(
        r"(定时|每天|每周|每日|每小时).{0,10}(自动|执行|运行|发送|提醒我)|cron|scheduled? task", re.I)),
    ("db_access", "直接读写数据库/执行 SQL", re.compile(
        r"(直接)?(读写|访问|操作|修改).{0,6}数据库|执行.{0,6}SQL|"
        r"(insert|update|delete)\s+(into\s+)?\w*\s*(table|database)", re.I)),
    ("browser_control", "控制浏览器/点击页面等真实操作", re.compile(
        r"(控制|操作|打开).{0,6}(浏览器|网页按钮)|(自动)?(点击|单击).{0,6}(按钮|页面|链接)|control the browser", re.I)),
]

_CAPABILITY_NOTE = ("本系统技能仅支持「对话提示注入」与「链接抓取/正文清洗策略」两类配置，"
                    "不具备真实执行能力，无法实现该行为")


def review_skill(name: str, kind: str, scope: str, content: str) -> dict:
    """对技能名称+内容做预审。

    返回 {'verdict': 'pass'|'reject', 'risk': ''|'high'|'unsupported',
          'categories': [...], 'reasons': [...]}。
    """
    text = f"{name or ''}\n{content or ''}"
    reasons: List[str] = []
    categories: List[str] = []
    risk = ""
    for cat, label, rx in _SECURITY_RULES:
        if rx.search(text):
            categories.append(cat)
            reasons.append(f"安全风险：{label}")
            risk = "high"
    for cat, label, rx in _CAPABILITY_RULES:
        if rx.search(text):
            categories.append(cat)
            reasons.append(f"超出系统能力：{label}——{_CAPABILITY_NOTE}")
            if not risk:
                risk = "unsupported"
    return {
        "verdict": "reject" if reasons else "pass",
        "risk": risk,
        "categories": categories,
        "reasons": reasons,
    }


def _now() -> str:
    return datetime.now().isoformat()


def log_review(user_id: str, username: str, action: str, skill_name: str,
               kind: str, scope: str, result: dict, content: str = "") -> None:
    """评审结果落库；拒绝项额外写 WARNING 日志（可观测性重点暴露）。写库失败静默忽略。"""
    verdict = result.get("verdict", "pass")
    reasons = result.get("reasons", [])
    try:
        db.execute(
            "INSERT INTO skill_review_logs (id, user_id, username, action, skill_name,"
            " kind, scope, verdict, risk, reasons, content_excerpt, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (secrets.token_hex(16), user_id or "", username or "", action,
             (skill_name or "")[:60], kind or "", scope or "", verdict,
             result.get("risk", ""), json.dumps(reasons, ensure_ascii=False),
             (content or "")[:200], _now()),
        )
    except Exception:  # 观测数据不得影响主流程
        logger.exception("skill_review_logs 写入失败")
    if verdict == "reject":
        logger.warning(
            "[SKILL-REVIEW] reject user=%s(%s) action=%s skill=%r risk=%s reasons=%s",
            username, user_id, action, skill_name, result.get("risk"), "; ".join(reasons),
        )


def list_reviews(limit: int = 30, offset: int = 0) -> dict:
    """可观测性查询：评审汇总 + 最近记录（倒序）。"""
    total = db.query_one("SELECT COUNT(*) AS n FROM skill_review_logs")["n"]
    rejected = db.query_one(
        "SELECT COUNT(*) AS n FROM skill_review_logs WHERE verdict = 'reject'")["n"]
    high = db.query_one(
        "SELECT COUNT(*) AS n FROM skill_review_logs WHERE verdict = 'reject' AND risk = 'high'")["n"]
    unsupported = db.query_one(
        "SELECT COUNT(*) AS n FROM skill_review_logs WHERE verdict = 'reject' AND risk = 'unsupported'")["n"]
    last = db.query_one(
        "SELECT created_at FROM skill_review_logs WHERE verdict = 'reject'"
        " ORDER BY created_at DESC LIMIT 1")
    rows = db.query(
        "SELECT * FROM skill_review_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    items = []
    for r in rows:
        try:
            reasons = json.loads(r["reasons"] or "[]")
        except (ValueError, TypeError):
            reasons = []
        items.append({
            "id": r["id"], "user_id": r["user_id"], "username": r["username"],
            "action": r["action"], "skill_name": r["skill_name"], "kind": r["kind"],
            "scope": r["scope"], "verdict": r["verdict"], "risk": r["risk"],
            "reasons": reasons, "content_excerpt": r["content_excerpt"],
            "created_at": r["created_at"],
        })
    return {
        "summary": {
            "total": total, "rejected": rejected, "high_risk": high,
            "unsupported": unsupported,
            "last_reject_at": last["created_at"] if last else None,
        },
        "items": items,
    }


def reject_message(result: dict) -> str:
    """把拒绝结果拼成面向用户的可读提示。"""
    return "技能预审未通过，已拒绝添加：" + "；".join(result.get("reasons", []))
