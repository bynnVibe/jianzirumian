"""
见字如面 - 链接正文确定性清洗（「内容润色」）

设计约束（与用户需求一一对应）：
1) 去掉多余空格、无关内容、解析角标、无关符号、页头页尾噪声；
2) 逐行处理、绝不重排，保留原始顺序、自然段与连贯性；
3) 只做减法不做加法：不新增任何观点或内容，保持句子完整性；
4) 清洗策略来自 Skill 配置（scope=polish 的 playbook 技能），可开关、可扩展正则。

与 LLM 润色（polish-ocr）互补：本模块是零模型依赖的确定性规则清洗，
适用于链接抓取正文的批量去噪，结果可复现、可审计。
"""
import logging
import re
from typing import Optional

logger = logging.getLogger("jianziruyang.article_clean")

# 解析角标：[1]、【2】、[1,2]、[3、4] 等上标引用
_FOOTNOTE_RE = re.compile(r"[\[【]\d{1,2}(?:[,，、]\d{1,2})*[\]】]")

# 纯符号行（配合长度上限，避免误删正文短句）
_SYMBOL_ONLY_RE = re.compile(r"^[\W_]+$")

# 页头/页尾与正文无关的噪声行（整行命中即删除）
_NOISE_RES = [
    # 页码：第 3 页 / 3 / 12 页 / - 5 - / 第1页 共2页
    re.compile(r"^第?\s*\d{1,4}\s*页?(?:\s*/\s*共?\s*\d{1,4}\s*页?)?$"),
    re.compile(r"^[-—–]\s*\d{1,4}\s*[-—–]$"),
    # 版权/转载声明
    re.compile(r"版权所有|转载或(?:转载必究|请注明)|注明出处|All Rights Reserved|Copyright", re.I),
    # 关注/分享引导（公众号页尾页头常见）
    re.compile(r"扫码关注|长按二维码|点击上方蓝字|点击关注|关注我们|关注公众号|阅读原文|本文源自|来源[:：]?\s*$"),
    re.compile(r"^(点赞|在看|分享|收藏|转发|一键三连|打赏|赞赏|喜欢此内容).{0,8}$"),
    # 编辑/作者/来源署名行
    re.compile(r"^(编辑|责编|审核|校对|整理|摄影|文|图|来源|作者)[:：]"),
    # 上下篇/相关阅读导航
    re.compile(r"上一篇|下一篇|上一页|下一页|返回首页|相关阅读|推荐阅读|延伸阅读|猜你喜欢|往期回顾"),
    # 纯 URL 行
    re.compile(r"^https?://\S+$"),
    # 广告/推广
    re.compile(r"^(广告|推广|赞助|点击领取|免费领取|扫码领取|限时优惠)"),
]

# 策略里布尔开关的默认值（技能全部关闭时不做任何清洗）
_POLICY_KEYS = ("remove_footnote_markers", "remove_noise_lines",
                "collapse_spaces", "collapse_blank_lines")

_SPACE_RE = re.compile(r"[ \t\u3000\u00a0]+")


def _compile_extra_patterns(patterns) -> list:
    """编译用户扩展的整行删除正则；非法正则跳过并告警，不影响其余规则。"""
    compiled = []
    for p in patterns or []:
        if not isinstance(p, str) or not p.strip():
            continue
        try:
            compiled.append(re.compile(p))
        except re.error:
            logger.warning("清洗策略中忽略非法正则: %s", p[:40])
    return compiled


def clean_article(text: str, policy: Optional[dict]) -> dict:
    """按策略逐行清洗正文，返回 {'cleaned': str, 'stats': dict}。

    只删噪声、不重排、不新增内容；单个空行保留（自然段分隔），连续空行压缩为一个。
    """
    policy = policy or {}
    remove_footnotes = bool(policy.get("remove_footnote_markers"))
    remove_noise = bool(policy.get("remove_noise_lines"))
    collapse_spaces = bool(policy.get("collapse_spaces"))
    collapse_blank = bool(policy.get("collapse_blank_lines"))
    extra_res = _compile_extra_patterns(policy.get("extra_line_patterns"))

    stats = {
        "removed_lines": 0,
        "removed_footnotes": 0,
        "collapsed_spaces": 0,
        "extra_patterns_removed": 0,
        "before_chars": len(text or ""),
    }
    if not text or not text.strip():
        stats["after_chars"] = 0
        return {"cleaned": "", "stats": stats}

    out_lines = []
    prev_blank = False

    def _mark_removed():
        """整行被删除后，若上一输出行已是空行则继续参与空行压缩。"""
        nonlocal prev_blank
        if collapse_blank and out_lines and out_lines[-1] == "":
            prev_blank = True

    for raw_line in text.splitlines():
        line = raw_line
        # 1) 折叠多余空格（半角/全角/制表/不换行空格 → 单个空格，并去首尾）
        if collapse_spaces and _SPACE_RE.search(line):
            n = len(_SPACE_RE.findall(line))
            line = _SPACE_RE.sub(" ", line).strip()
            if line != raw_line.strip():
                stats["collapsed_spaces"] += n
            elif line != raw_line:
                stats["collapsed_spaces"] += n
        else:
            line = line.rstrip()

        stripped = line.strip()
        # 2) 空行：连续空行压缩为一个（保留自然段分隔）
        if not stripped:
            if collapse_blank and prev_blank:
                continue
            out_lines.append("")
            prev_blank = True
            continue
        prev_blank = False

        # 3) 整行噪声删除：内置规则 + 纯符号行 + 用户扩展正则
        if remove_noise and (
            any(r.search(stripped) for r in _NOISE_RES)
            or (_SYMBOL_ONLY_RE.match(stripped) and len(stripped) <= 40)
        ):
            stats["removed_lines"] += 1
            _mark_removed()
            continue
        if any(r.search(stripped) for r in extra_res):
            stats["extra_patterns_removed"] += 1
            stats["removed_lines"] += 1
            _mark_removed()
            continue

        # 4) 行内角标删除（仅删角标本身，不动句子其余部分）
        if remove_footnotes:
            line, n = _FOOTNOTE_RE.subn("", line)
            if n:
                stats["removed_footnotes"] += n
                if collapse_spaces:
                    line = _SPACE_RE.sub(" ", line).strip()
                else:
                    line = line.rstrip()
                if not line:
                    _mark_removed()
                    continue

        out_lines.append(line)

    # 5) 去首尾空行；collapse_blank 关闭时保留原始空行结构
    while out_lines and not out_lines[0].strip():
        out_lines.pop(0)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()

    cleaned = "\n".join(out_lines)
    stats["after_chars"] = len(cleaned)
    return {"cleaned": cleaned, "stats": stats}
