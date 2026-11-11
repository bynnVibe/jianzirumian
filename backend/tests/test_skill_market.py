"""技能市场相似 Skill 搜索聚合服务测试（不发起真实网络请求）。"""
import asyncio

from app.services import skill_market as sm


def test_parse_skill_md_frontmatter():
    md = '---\nname: x\ndescription: "hello world"\n---\n\n# Body\nDo thing.'
    desc, body = sm._parse_skill_md(md)
    assert desc == "hello world"
    assert body.startswith("# Body")


def test_parse_skill_md_block_scalar():
    md = '---\nname: x\ndescription: >-\n  line one\n  line two\n---\nbody'
    desc, body = sm._parse_skill_md(md)
    assert desc == "line one line two"
    assert body == "body"


def test_parse_skill_md_no_frontmatter():
    desc, body = sm._parse_skill_md("plain body")
    assert desc == ""
    assert body == "plain body"


def test_search_market_empty_query():
    d = asyncio.run(sm.search_market("  "))
    assert d["results"] == []
    assert d["errors"]


def test_search_market_dedup_sort_limit(monkeypatch):
    async def fake_a(client, q, limit):
        return [
            {"id": "a:1", "source": "skills.sh", "name": "weather", "heat": 10},
            {"id": "a:2", "source": "skills.sh", "name": "Weather", "heat": 99},  # 同名去重
            {"id": "a:3", "source": "skills.sh", "name": "alpha", "heat": 5},
        ]

    async def fake_b(client, q, limit):
        return [{"id": "b:1", "source": "clawhub", "name": "beta", "heat": 50}]

    monkeypatch.setattr(sm, "_ADAPTERS", {"skills.sh": fake_a, "clawhub": fake_b})
    d = asyncio.run(sm.search_market("weather", limit=2))
    names = [r["name"] for r in d["results"]]
    # 按热度降序且同名去重保留最高热度：Weather(99) > beta(50)；limit=2 截断
    assert names == ["Weather", "beta"]
    assert all(r["source_name"] for r in d["results"])


def test_search_market_single_source_failure(monkeypatch):
    async def bad(client, q, limit):
        raise RuntimeError("boom")

    async def good(client, q, limit):
        return [{"id": "g:1", "source": "clawhub", "name": "ok", "heat": 1}]

    monkeypatch.setattr(sm, "_ADAPTERS", {"skills.sh": bad, "clawhub": good})
    d = asyncio.run(sm.search_market("x"))
    assert [r["name"] for r in d["results"]] == ["ok"]
    assert any("Skills.sh" in e for e in d["errors"])