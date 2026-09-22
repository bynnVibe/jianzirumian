"""
见字如面 - llm-wiki 知识库文件系统布局

参照 LLM-Wiki（Karpathy 方法论 / llm-wiki-skill）的标准化目录结构，把编译产物
以 Markdown 落盘到 WIKI_DIR，形成可浏览、可导出、Obsidian 兼容的知识站点：

    data/wiki/
    ├── purpose.md          # 研究方向：目标、关键问题、研究范围
    ├── schema.md           # Wiki 结构规则：页面类型、分类方式
    ├── index.md            # 内容目录（LLM 导航入口，自动重建）
    ├── log.md              # 操作历史（可解析的时序记录）
    ├── overview.md         # 全局概要（每次 ingest 后自动更新）
    ├── raw/                # 原始素材（映射到 data/uploads，见 raw/README.md）
    │   ├── articles/  tweets/  wechat/  pdfs/  notes/  assets/
    ├── wiki/               # AI 生成的知识库
    │   ├── entities/       # 实体页（派生视图，由 Step1 分析聚合）
    │   ├── topics/         # 主题页（派生视图）
    │   ├── sources/        # 资料摘要页（page_type=source）
    │   ├── comparisons/    # 对比分析（预留）
    │   ├── synthesis/      # 跨资料综合分析（page_type=digest）
    │   │   └── sessions/   # 对话转知识页面（预留）
    │   └── queries/        # 保存的查询结果（预留）
    ├── .wiki-schema.md     # 配置文件（当前编译策略快照）
    └── .wiki-cache.json    # SHA256 去重缓存

设计原则：本模块只负责文件系统布局与派生视图，不做 LLM 调用与数据库写入；
所有落盘失败都静默降级（记 warning），绝不影响上传/编译/问答主链路。
"""
import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.config import settings

logger = logging.getLogger("jianziruyang.wiki.store")

# page_type → wiki/ 下子目录（派生视图 entities/topics 由 rebuild_views 单独生成）
PAGE_SUBDIR = {
    "source": "sources",
    "digest": "synthesis",
    "comparison": "comparisons",
    "query": "queries",
}

# source_type → raw/ 下原始素材分类目录。
# image(手写笔记 OCR)→notes；pdf→pdfs；word(打字文档)→articles；其余→assets。
RAW_CATEGORY_BY_SOURCE_TYPE = {
    "image": "notes",
    "pdf": "pdfs",
    "word": "articles",
}
RAW_DEFAULT_CATEGORY = "assets"

# 标准化目录骨架
_RAW_DIRS = ["articles", "tweets", "wechat", "pdfs", "notes", "assets"]
_WIKI_DIRS = ["entities", "topics", "sources", "comparisons", "synthesis/sessions", "queries"]


def _root() -> Path:
    return Path(settings.WIKI_DIR)


def source_hash(text: str) -> str:
    """资料文本 SHA256，用于增量编译去重。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", (title or "wiki")).strip("_")
    return (name or "wiki")[:80]


# ---------------------------------------------------------------------------
# 目录骨架与根文件
# ---------------------------------------------------------------------------

def ensure_layout() -> None:
    """创建标准化目录骨架与默认根文件（幂等），并迁移旧布局。"""
    root = _root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        for d in _RAW_DIRS:
            (root / "raw" / d).mkdir(parents=True, exist_ok=True)
        for d in _WIKI_DIRS:
            (root / "wiki" / d).mkdir(parents=True, exist_ok=True)

        _migrate_legacy_layout(root)

        _write_if_missing(root / "purpose.md", _PURPOSE_MD)
        _write_if_missing(root / "schema.md", _SCHEMA_MD)
        _write_if_missing(root / "raw" / "README.md", _RAW_README)
        # 配置快照与全局概要/索引随当前状态刷新
        write_schema_config()
        _write_if_missing(root / "index.md", _INDEX_HEADER)
        _write_if_missing(root / "log.md", _LOG_HEADER)
    except Exception as e:  # pragma: no cover - 落盘失败不阻断主流程
        logger.warning("[WIKI] 初始化目录骨架失败: %s", e)


def _migrate_legacy_layout(root: Path) -> None:
    """旧布局（根级 sources/ digests/）迁移到标准化 wiki/ 子目录。"""
    moves = [("sources", "wiki/sources"), ("digests", "wiki/synthesis")]
    for old_name, new_rel in moves:
        old_dir = root / old_name
        if not old_dir.is_dir():
            continue
        new_dir = root / new_rel
        new_dir.mkdir(parents=True, exist_ok=True)
        try:
            for fp in old_dir.glob("*.md"):
                target = new_dir / fp.name
                if not target.exists():
                    shutil.move(str(fp), str(target))
            # 迁移后旧目录已空则移除
            if not any(old_dir.iterdir()):
                old_dir.rmdir()
            logger.info("[WIKI] 旧目录 %s 迁移至 %s", old_name, new_rel)
        except Exception as e:
            logger.warning("[WIKI] 迁移旧目录 %s 失败: %s", old_name, e)


def _write_if_missing(fp: Path, content: str) -> None:
    if not fp.exists():
        fp.write_text(content, encoding="utf-8")


def write_schema_config() -> None:
    """.wiki-schema.md：当前编译策略快照（每次启动/配置变更刷新）。"""
    body = (
        "# .wiki-schema.md — 编译配置快照\n\n"
        f"_自动生成于 {datetime.now().isoformat(timespec='seconds')}_\n\n"
        "| 配置项 | 值 |\n| --- | --- |\n"
        f"| WIKI_COMPILE_ENABLED | {settings.WIKI_COMPILE_ENABLED} |\n"
        f"| WIKI_TWO_STEP_ENABLED | {settings.WIKI_TWO_STEP_ENABLED} |\n"
        f"| WIKI_INCREMENTAL_ENABLED | {settings.WIKI_INCREMENTAL_ENABLED} |\n"
        f"| WIKI_COMPILE_MAX_ATTEMPTS | {settings.WIKI_COMPILE_MAX_ATTEMPTS} |\n"
        f"| WIKI_MAX_SOURCE_CHARS | {settings.WIKI_MAX_SOURCE_CHARS} |\n"
        f"| WIKI_MIN_SOURCE_CHARS | {settings.WIKI_MIN_SOURCE_CHARS} |\n"
        f"| WIKI_COMPILE_INTERVAL | {settings.WIKI_COMPILE_INTERVAL} |\n"
        f"| WIKI_LANG | {settings.WIKI_LANG} |\n"
    )
    try:
        (_root() / ".wiki-schema.md").write_text(body, encoding="utf-8")
    except Exception as e:
        logger.warning("[WIKI] 写入 .wiki-schema.md 失败: %s", e)


# ---------------------------------------------------------------------------
# SHA256 去重缓存 .wiki-cache.json
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    return _root() / ".wiki-cache.json"


def read_cache() -> Dict[str, dict]:
    try:
        if _cache_path().exists():
            return json.loads(_cache_path().read_text(encoding="utf-8") or "{}")
    except Exception as e:
        logger.warning("[WIKI] 读取 .wiki-cache.json 失败: %s", e)
    return {}


def update_cache(source_image: str, sha: str, page_id: str) -> None:
    if not source_image:
        return
    cache = read_cache()
    cache[source_image] = {
        "hash": sha,
        "page_id": page_id,
        "compiled_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_cache(cache)


def remove_cache(source_image: str) -> None:
    if not source_image:
        return
    cache = read_cache()
    if cache.pop(source_image, None) is not None:
        _write_cache(cache)


def _write_cache(cache: Dict[str, dict]) -> None:
    try:
        _cache_path().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("[WIKI] 写入 .wiki-cache.json 失败: %s", e)


# ---------------------------------------------------------------------------
# raw/ 原始素材归档：把上传的原始文件按类别镜像到 wiki/raw/{category}/
# ---------------------------------------------------------------------------

def raw_category_for(source_type: str) -> str:
    """source_type → raw/ 分类目录名。"""
    return RAW_CATEGORY_BY_SOURCE_TYPE.get((source_type or "").lower(), RAW_DEFAULT_CATEGORY)


def raw_path_for(source_path: str, source_type: str) -> Path:
    """原始文件在 raw/ 下的目标路径（按类别 + 原文件名）。"""
    category = raw_category_for(source_type)
    return _root() / "raw" / category / Path(source_path or "").name


def archive_raw(source_path: str, source_type: str) -> Optional[Path]:
    """把原始文件归档（硬链接优先、复制兜底）到 wiki/raw/{category}/，幂等。

    uploads/ 仍是服务下发的源；raw/ 作为不可变素材存档，与 LLM-Wiki 标准结构对齐。
    返回归档后的路径；源不存在或失败返回 None（不阻断上传主流程）。
    """
    if not source_path:
        return None
    src = Path(source_path)
    if not src.is_file():
        return None
    target = raw_path_for(source_path, source_type)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            return target  # 已归档，幂等跳过
        try:
            os.link(src, target)  # 同文件系统硬链接：零额外空间、数据同源
        except OSError:
            shutil.copy2(src, target)  # 跨文件系统兜底复制
        return target
    except Exception as e:
        logger.warning("[WIKI] 归档原始素材失败(%s): %s", src.name, e)
        return None


def remove_raw(source_path: str) -> None:
    """删除原始文件在 raw/ 下的归档镜像（来源删除时联动，避免残留隐私数据）。"""
    if not source_path:
        return
    name = Path(source_path).name
    try:
        for category in list(RAW_CATEGORY_BY_SOURCE_TYPE.values()) + [RAW_DEFAULT_CATEGORY]:
            fp = _root() / "raw" / category / name
            if fp.exists() or fp.is_symlink():
                fp.unlink()
    except OSError as e:
        logger.warning("[WIKI] 删除 raw 归档失败(%s): %s", name, e)


def backfill_raw() -> int:
    """一次性把存量上传记录对应的原始文件归档到 raw/（幂等，启动时调用）。"""
    from app.services.records import upload_records

    archived = 0
    try:
        records, _ = upload_records.list_records(limit=10000, offset=0)
        for rec in records:
            src = rec.get("image_path", "")
            if not src:
                continue
            if archive_raw(src, rec.get("source_type", "image")):
                archived += 1
            # Word 的预览 PDF 也归档到 pdfs/（原始排版产物）
            pdf = rec.get("pdf_preview_path")
            if pdf:
                archive_raw(pdf, "pdf")
        if archived:
            logger.info("[WIKI] 存量原始素材归档完成：%d 个文件", archived)
    except Exception as e:
        logger.warning("[WIKI] 存量原始素材回填失败: %s", e)
    return archived


# ---------------------------------------------------------------------------
# 页面镜像（YAML frontmatter + Markdown 正文）
# ---------------------------------------------------------------------------

def page_relpath(page: dict) -> str:
    """页面相对 WIKI_DIR 的路径，用于 [[wikilink]] 与索引。"""
    subdir = PAGE_SUBDIR.get(page.get("page_type", "source"), "sources")
    return f"wiki/{subdir}/{page_filename(page)}"


def page_filename(page: dict) -> str:
    return f"{safe_filename(page.get('title', ''))}-{page['id'][:8]}.md"


def write_page(page: dict) -> None:
    """把百科页面以带 YAML frontmatter 的 Markdown 落盘镜像。"""
    try:
        subdir = PAGE_SUBDIR.get(page.get("page_type", "source"), "sources")
        folder = _root() / "wiki" / subdir
        folder.mkdir(parents=True, exist_ok=True)
        fp = folder / page_filename(page)
        fp.write_text(_frontmatter(page) + page.get("content", "") + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("[WIKI] 镜像 Markdown 失败(%s): %s", page.get("title"), e)


def remove_page(page: dict) -> None:
    try:
        subdir = PAGE_SUBDIR.get(page.get("page_type", "source"), "sources")
        fp = _root() / "wiki" / subdir / page_filename(page)
        if fp.exists():
            fp.unlink()
    except OSError:
        pass


def _frontmatter(page: dict) -> str:
    refs = page.get("source_refs") or []
    fm = [
        "---",
        f"type: {page.get('page_type', 'source')}",
        f"title: {page.get('title', '')}",
        f"id: {page.get('id', '')}",
        f"kb_id: {page.get('kb_id') or '-'}",
        f"visibility: {page.get('visibility', 'public')}",
        f"source_hash: {page.get('source_hash', '') or '-'}",
        f"created: {page.get('created_at', '')}",
        f"updated: {page.get('updated_at', '')}",
    ]
    if refs:
        fm.append("sources:")
        fm.extend(f"  - {r}" for r in refs)
    else:
        fm.append("sources: []")
    fm.append("---\n\n")
    return "\n".join(fm)


# ---------------------------------------------------------------------------
# 操作日志 log.md（可解析时序记录）
# ---------------------------------------------------------------------------

def append_log(operation: str, page_type: str, title: str, extra: str = "") -> None:
    """追加一条时序操作记录：ts | op | type | title | extra"""
    try:
        fp = _root() / "log.md"
        line = (
            f"- {datetime.now().isoformat(timespec='seconds')} | {operation} | "
            f"{page_type} | {title}" + (f" | {extra}" if extra else "") + "\n"
        )
        with fp.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.warning("[WIKI] 写入 log.md 失败: %s", e)


# ---------------------------------------------------------------------------
# 派生视图：index.md / overview.md / entities / topics
# ---------------------------------------------------------------------------

def rebuild_views(pages: List[dict]) -> None:
    """基于全部百科页面重建导航索引、全局概要与实体/主题派生页。

    pages: wiki_pages 行转换后的 dict 列表（含 analysis 字段）。
    entities/topics 为派生视图，每次全量重建（先清理再写入），保证与数据一致。
    """
    try:
        sources = [p for p in pages if p.get("page_type") == "source"]
        digests = [p for p in pages if p.get("page_type") == "digest"]
        entities, topics = _aggregate(pages)

        _rebuild_entities(entities)
        _rebuild_topics(topics)
        _rebuild_index(sources, digests, entities, topics)
        _rebuild_overview(sources, digests, entities, topics)
    except Exception as e:
        logger.warning("[WIKI] 重建派生视图失败: %s", e)


def _aggregate(pages: List[dict]):
    """从各页面 Step1 分析结果聚合实体与主题 → 引用来源列表。"""
    entities: Dict[str, dict] = {}
    topics: Dict[str, dict] = {}
    for p in pages:
        if p.get("page_type") != "source":
            continue
        analysis = p.get("analysis") or {}
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis or "{}")
            except Exception:
                analysis = {}
        rel = page_relpath(p)
        title = p.get("title", "")
        for ent in analysis.get("entities", []) or []:
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            slot = entities.setdefault(
                name, {"type": ent.get("type", "concept"), "note": ent.get("note", ""), "sources": []}
            )
            if ent.get("type"):
                slot["type"] = ent.get("type")
            if ent.get("note") and not slot.get("note"):
                slot["note"] = ent.get("note")
            if rel not in slot["sources"]:
                slot["sources"].append(rel)
            slot.setdefault("titles", []).append(title)
        keywords = (analysis.get("structure", {}) or {}).get("keywords", []) or []
        for kw in keywords:
            name = (kw or "").strip() if isinstance(kw, str) else ""
            if not name:
                continue
            slot = topics.setdefault(name, {"sources": [], "titles": []})
            if rel not in slot["sources"]:
                slot["sources"].append(rel)
            slot["titles"].append(title)
    return entities, topics


def _clear_dir(rel: str) -> Path:
    folder = _root() / rel
    folder.mkdir(parents=True, exist_ok=True)
    for fp in folder.glob("*.md"):
        try:
            fp.unlink()
        except OSError:
            pass
    return folder


def _rebuild_entities(entities: Dict[str, dict]) -> None:
    folder = _clear_dir("wiki/entities")
    for name, info in entities.items():
        try:
            srcs = "\n".join(f"- [[{s}]]" for s in info["sources"])
            body = (
                "---\n"
                "type: entity\n"
                f"title: {name}\n"
                f"entity_type: {info.get('type', 'concept')}\n"
                "---\n\n"
                f"# {name}\n\n"
                f"> 实体类型：{info.get('type', 'concept')}\n\n"
            )
            if info.get("note"):
                body += f"{info['note']}\n\n"
            body += f"## 提及资料（{len(info['sources'])}）\n{srcs}\n"
            (folder / f"{safe_filename(name)}.md").write_text(body, encoding="utf-8")
        except Exception as e:
            logger.warning("[WIKI] 写实体页失败(%s): %s", name, e)


def _rebuild_topics(topics: Dict[str, dict]) -> None:
    folder = _clear_dir("wiki/topics")
    for name, info in topics.items():
        try:
            srcs = "\n".join(f"- [[{s}]]" for s in info["sources"])
            body = (
                "---\n"
                "type: topic\n"
                f"title: {name}\n"
                "---\n\n"
                f"# {name}\n\n"
                f"## 相关资料（{len(info['sources'])}）\n{srcs}\n"
            )
            (folder / f"{safe_filename(name)}.md").write_text(body, encoding="utf-8")
        except Exception as e:
            logger.warning("[WIKI] 写主题页失败(%s): %s", name, e)


def _rebuild_index(sources, digests, entities, topics) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    lines = [
        _INDEX_HEADER.rstrip(),
        "",
        f"_自动生成于 {ts}｜资料摘要 {len(sources)}｜综合报告 {len(digests)}｜"
        f"实体 {len(entities)}｜主题 {len(topics)}_",
        "",
        "## 资料摘要页 (sources)",
        "",
    ]
    for p in sorted(sources, key=lambda x: x.get("updated_at", ""), reverse=True):
        lines.append(f"- [[{page_relpath(p)}|{p.get('title', '')}]]")
    if not sources:
        lines.append("_（暂无）_")
    lines += ["", "## 综合报告 (synthesis)", ""]
    for p in sorted(digests, key=lambda x: x.get("updated_at", ""), reverse=True):
        lines.append(f"- [[{page_relpath(p)}|{p.get('title', '')}]]")
    if not digests:
        lines.append("_（暂无）_")
    lines += ["", "## 实体 (entities)", ""]
    for name, info in sorted(entities.items(), key=lambda kv: len(kv[1]["sources"]), reverse=True):
        lines.append(f"- [[wiki/entities/{safe_filename(name)}.md|{name}]] （{len(info['sources'])}）")
    if not entities:
        lines.append("_（暂无）_")
    lines += ["", "## 主题 (topics)", ""]
    for name, info in sorted(topics.items(), key=lambda kv: len(kv[1]["sources"]), reverse=True):
        lines.append(f"- [[wiki/topics/{safe_filename(name)}.md|{name}]] （{len(info['sources'])}）")
    if not topics:
        lines.append("_（暂无）_")
    try:
        (_root() / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("[WIKI] 写入 index.md 失败: %s", e)


def _rebuild_overview(sources, digests, entities, topics) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    top_entities = sorted(entities.items(), key=lambda kv: len(kv[1]["sources"]), reverse=True)[:10]
    top_topics = sorted(topics.items(), key=lambda kv: len(kv[1]["sources"]), reverse=True)[:10]
    recent = sorted(sources + digests, key=lambda x: x.get("updated_at", ""), reverse=True)[:8]
    lines = [
        "# 全局概要 (overview.md)",
        "",
        f"_每次 ingest 后自动更新，反映 Wiki 最新状态｜{ts}_",
        "",
        "## 统计",
        f"- 资料摘要页：{len(sources)}",
        f"- 综合报告：{len(digests)}",
        f"- 实体：{len(entities)}",
        f"- 主题：{len(topics)}",
        "",
        "## 高频实体 Top10",
    ]
    lines += [f"- {n}（{len(i['sources'])}）" for n, i in top_entities] or ["_（暂无）_"]
    lines += ["", "## 高频主题 Top10"]
    lines += [f"- {n}（{len(i['sources'])}）" for n, i in top_topics] or ["_（暂无）_"]
    lines += ["", "## 最近更新"]
    lines += [f"- {p.get('title', '')}（{p.get('page_type')}｜{p.get('updated_at', '')[:19]}）" for p in recent] or ["_（暂无）_"]
    try:
        (_root() / "overview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("[WIKI] 写入 overview.md 失败: %s", e)


# ---------------------------------------------------------------------------
# 默认根文件文案
# ---------------------------------------------------------------------------

_INDEX_HEADER = (
    "# 知识库索引 (index.md)\n\n"
    "> LLM 导航入口：查询时先读本文件了解知识库有哪些页面，再决定读哪些详情。\n"
)

_LOG_HEADER = (
    "# 操作历史 (log.md)\n\n"
    "> 可解析的时序记录，格式：`时间 | 操作 | 页面类型 | 标题 | 附加信息`。\n"
    "> 操作类型：ingest(消化编译) / digest(综合) / delete(删除) / skip(哈希未变跳过)。\n\n"
)

_PURPOSE_MD = """# 研究方向 (purpose.md)

> LLM 在每次 ingest 与 query 时都会读取本文件以获取上下文。schema 是结构规则，purpose 是方向意图。

## 目标
把用户上传的手写笔记、Word/PDF 文档编译成结构化、彼此关联的个人百科知识库，
使问答可直接命中预编译的高质量知识卡片，而非每次从头检索原始碎片。

## 关键问题
- 这批资料围绕哪些主题、实体与结论？
- 不同资料之间是否存在关联、互补或矛盾？
- 哪些知识值得沉淀为独立词条，供后续复用与浏览？

## 研究范围
- 纳入：用户上传并入库的图片(OCR)、Word/PDF 文档文本。
- 排除：聊天记录中的敏感信息（手机号、身份证、API Key、明文密码）。

## 演进中的论点
_（随素材积累由编译流程补充/修订）_
"""

_SCHEMA_MD = """# Wiki 结构规则 (schema.md)

> 定义本知识库的页面类型与分类方式，是"关于知识本身的元知识"。

## 页面类型
| 类型 | 目录 | 说明 | 来源 |
| --- | --- | --- | --- |
| source（资料摘要页） | `wiki/sources/` | 单份资料编译成的结构化百科卡片 | LLM 两步编译 |
| digest（综合分析） | `wiki/synthesis/` | 跨素材围绕主题的深度综合报告 | digest 工作流 |
| entity（实体页） | `wiki/entities/` | 人物/组织/产品/概念，由 Step1 分析聚合 | 派生视图 |
| topic（主题页） | `wiki/topics/` | 理论/方法/技术关键词聚合 | 派生视图 |
| comparison（对比） | `wiki/comparisons/` | 对比分析（预留） | — |
| query（查询） | `wiki/queries/` | 保存的查询结果（预留） | — |

## 分类与链接约定
- 每个页面带 YAML frontmatter：`type / title / sources[] / source_hash / created / updated`。
- 页面间用 `[[wikilink]]` 双向链接（Obsidian 兼容）。
- `index.md` 为导航入口，`overview.md` 为全局概要，均在每次编译后自动重建。
- `raw/` 存放原始素材（本项目映射到 `data/uploads`，详见 `raw/README.md`），不可变。

## 编译策略
- 两步思维链：Step1 输出结构化分析 JSON（实体/论点/关联/矛盾）→ Step2 基于分析生成卡片。
- SHA256 增量：来源文本哈希未变则跳过重复编译（`.wiki-cache.json` 去重）。
- 持久化队列：串行处理防并发，失败自动重试，重启后恢复未完成任务。
"""

_RAW_README = """# raw/ — 原始素材层（不可变）

本项目的原始素材实际存放于 `data/uploads/`（图片 OCR、Word/PDF）。此处的分类目录
用于对齐 LLM-Wiki 标准结构，作为素材类型的逻辑归类入口：

- `articles/` 网页文章
- `tweets/`    X/Twitter
- `wechat/`    微信公众号
- `pdfs/`      PDF 文档
- `notes/`     手写笔记（图片 OCR）
- `assets/`    其他附件

原始文本经编译后生成的结构化知识页面写入 `../wiki/` 对应子目录，`raw/` 内容保持不可变。
"""
