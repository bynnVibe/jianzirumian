"""链接正文确定性清洗（article_clean）回归测试（纯函数，不出网）。

覆盖：
  - 噪声行删除（页码/版权/关注引导/署名/导航/纯 URL/纯符号）；
  - 角标删除且不动句子其余部分；
  - 多余空格折叠、连续空行压缩、自然段（单个空行）保留；
  - 保序保段：输出行是输入行的子序列，不新增内容；
  - 策略开关独立生效（全关时不清洗）、用户扩展正则、非法正则容错。
"""
from app.services.article_clean import clean_article

FULL_POLICY = {
    "remove_footnote_markers": True,
    "remove_noise_lines": True,
    "collapse_spaces": True,
    "collapse_blank_lines": True,
    "extra_line_patterns": [],
}

SAMPLE = """扫码关注本公众号，获取更多资讯

高血压  的预防[1]，关键在于低盐饮食与规律运动【2】。

第 3 页

适量运动有助于控制体重，改善心血管功能。



版权所有，转载请注明出处

●●●

上一篇：如何测血压
"""


def test_noise_lines_removed():
    out = clean_article(SAMPLE, FULL_POLICY)["cleaned"]
    for noise in ("扫码关注", "第 3 页", "版权所有", "●●●", "上一篇"):
        assert noise not in out


def test_footnote_markers_removed_sentence_intact():
    out = clean_article(SAMPLE, FULL_POLICY)["cleaned"]
    assert "[1]" not in out and "【2】" not in out
    # 句子保持完整，仅去掉角标
    assert "高血压 的预防，关键在于低盐饮食与规律运动。" in out


def test_spaces_collapsed():
    result = clean_article("过量  饮酒   会升高血压", FULL_POLICY)
    assert result["cleaned"] == "过量 饮酒 会升高血压"
    assert result["stats"]["collapsed_spaces"] >= 2


def test_paragraph_order_and_blank_lines_preserved():
    out = clean_article(SAMPLE, FULL_POLICY)["cleaned"]
    lines = out.split("\n")
    # 保序：正文两段的相对顺序不变
    idx1 = next(i for i, l in enumerate(lines) if "预防" in l)
    idx2 = next(i for i, l in enumerate(lines) if "适量运动" in l)
    assert idx1 < idx2
    # 自然段分隔（单个空行）保留
    assert "" in lines[idx1:idx2]
    # 首尾无空行
    assert lines[0].strip() and lines[-1].strip()


def test_no_content_added():
    """只做减法：输出行经空白/角标归一化后必是输入行的子串，不新增任何内容。"""
    import re as _re
    # 与清洗器同一角标规则：删角标不删字词，故归一化后子串关系必然成立
    foot = _re.compile(r"[\[【]\d{1,2}(?:[,，、]\d{1,2})*[\]】]")
    result = clean_article(SAMPLE, FULL_POLICY)
    src_norm = [_re.sub(r"\s+", "", foot.sub("", l)) for l in SAMPLE.splitlines() if l.strip()]
    for line in result["cleaned"].splitlines():
        if not line.strip():
            continue
        norm = _re.sub(r"\s+", "", line)
        assert any(norm in s for s in src_norm), f"疑似新增内容: {line}"
    assert result["stats"]["after_chars"] <= result["stats"]["before_chars"]


def test_stats_counts():
    result = clean_article(SAMPLE, FULL_POLICY)
    stats = result["stats"]
    assert stats["removed_footnotes"] == 2
    assert stats["removed_lines"] >= 5  # 页码/版权/关注/符号/上一篇


def test_empty_policy_keeps_text():
    text = "正文  保持[1]原样\n第 3 页"
    result = clean_article(text, {})
    assert result["cleaned"] == text
    assert result["stats"]["removed_lines"] == 0


def test_only_collapse_blank_lines():
    text = "第一段\n\n\n\n第二段"
    assert clean_article(text, {"collapse_blank_lines": True})["cleaned"] == "第一段\n\n第二段"
    # 关闭压缩则保留原始空行（仅去首尾）
    assert clean_article(text, {})["cleaned"] == text


def test_extra_line_patterns():
    # 用不与内置噪声规则重叠的自定义正则，验证扩展通道独立生效
    policy = dict(FULL_POLICY, extra_line_patterns=["^本站特约", r"^免责声明\s"])
    result = clean_article("正文一句\n本站特约：专家坐诊\n免责声明 栏目\n第二句", policy)
    out = result["cleaned"]
    assert "本站特约" not in out and "免责声明" not in out
    assert "正文一句" in out and "第二句" in out
    assert result["stats"]["extra_patterns_removed"] == 2


def test_invalid_extra_pattern_ignored():
    policy = dict(FULL_POLICY, extra_line_patterns=["([", "^本站特约"])
    result = clean_article("正文\n本站特约栏目", policy)
    assert "本站特约" not in result["cleaned"]  # 合法正则仍生效，非法正则跳过不报错


def test_empty_text():
    result = clean_article("", FULL_POLICY)
    assert result["cleaned"] == ""
    assert result["stats"]["after_chars"] == 0


def test_normal_sentences_not_removed():
    """普通正文短句（含数字/标点）不应被噪声规则误删。"""
    text = ("每天食盐不超过5克。\n"
            "血压高于140/90mmHg时应就医。\n"
            "运动30分钟，每周5次。")
    out = clean_article(text, FULL_POLICY)["cleaned"]
    assert out == text
