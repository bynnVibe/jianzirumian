"""Skill 原始包发现：固定 Git commit、完整目录与来源追踪，不执行外部代码。"""
import asyncio
import base64
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urljoin

from app.services.url_fetch import SafeFetcher
from app.services import skill_packages as packages

OFFICIAL_REPOS = (
    ("anthropics/skills", "skills/"),
    ("vercel-labs/agent-skills", "skills/"),
    ("huggingface/skills", ""),
    ("NousResearch/hermes-agent", "skills/"),
)
DISCOVERY_SITES = ("lobehub.com", "browse.sh", "github.com/openai/plugins", "agentskillshub.top",
                   "skillhub.club", "ai.skillatlas.cn", "skilldock.io")


async def _json(fetcher, url):
    result = await fetcher.fetch(url, resource=True)
    return json.loads(result.body)


def github_location(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or parsed.hostname not in ("github.com", "raw.githubusercontent.com") or len(parts) < 2:
        raise packages.PackageError("需要 HTTPS GitHub 仓库或具体 Skill 链接")
    repo = "/".join(parts[:2])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise packages.PackageError("仓库名称非法")
    ref, path = "HEAD", ""
    if parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
        ref, path = parts[2], "/".join(parts[3:])
    elif len(parts) >= 4 and parts[2] in ("tree", "blob"):
        ref, path = parts[3], "/".join(parts[4:])
    elif len(parts) > 2:
        raise packages.PackageError("GitHub 链接不是仓库或文件目录")
    if path:
        packages.safe_path(path)
    return repo, ref, path


async def _tree(fetcher, repo, ref):
    commit = await _json(fetcher, f"https://api.github.com/repos/{repo}/commits/{quote(ref, safe='')}")
    sha = commit.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise packages.PackageError("无法固定 Git commit")
    result = await _json(fetcher, f"https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1")
    if result.get("truncated"):
        raise packages.PackageError("仓库文件索引不完整，不能标记完整包")
    return sha, result.get("tree", [])


async def fetch_github_package(url):
    repo, ref, path = github_location(url)
    fetcher = SafeFetcher()
    sha, tree = await _tree(fetcher, repo, ref)
    skill_path = path if path.endswith("SKILL.md") else ((path + "/") if path else "") + "SKILL.md"
    if not any(e["path"] == skill_path for e in tree):
        candidates = [e["path"] for e in tree if e["path"].endswith("/SKILL.md") and
                      (not path or e["path"].startswith(path + "/"))]
        if len(candidates) != 1:
            raise packages.PackageError("请提供具体 Skill 目录；仓库包含多个或没有 SKILL.md")
        skill_path = candidates[0]
    prefix = skill_path[:-len("SKILL.md")]
    entries = [e for e in tree if e["path"].startswith(prefix) and e.get("type") != "tree"]
    # 根 Skill 不随包安装插件/hooks/agents；发现此类配置明确拒绝，不静默删资源。
    if len(entries) > packages.MAX_FILES:
        raise packages.PackageError("Skill 目录超过 100 文件")
    if sum(e.get("size", 0) for e in entries) > packages.MAX_BYTES:
        raise packages.PackageError("Skill 目录超过 10 MiB")
    files = {}
    for entry in entries:
        relative = packages.safe_path(entry["path"][len(prefix):])
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise packages.PackageError("目录含符号链接、子模块或未知文件类型")
        if relative.split("/")[0] in ("hooks", "agents", "mcp"):
            raise packages.PackageError("插件 hooks/MCP/agents 不属于可自动安装的 Skill 资源")
        if entry.get("size", 0) > packages.MAX_BYTES:
            raise packages.PackageError("资源超限")
        blob = await _json(fetcher, f"https://api.github.com/repos/{repo}/git/blobs/{entry['sha']}")
        if blob.get("encoding") != "base64":
            raise packages.PackageError("GitHub blob 编码不受支持")
        data = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
        git_hash = __import__("hashlib").sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if git_hash != entry["sha"]:
            raise packages.PackageError("Git blob 内容 hash 不一致")
        files[relative] = data
    # 保留仓库根授权文件，不把市场简介中的授权推测写入包。
    if prefix:
        license_entry = next((e for e in tree if e["path"].upper() in ("LICENSE", "LICENSE.MD", "LICENSE.TXT")
                              and e.get("mode") == "100644"), None)
        if license_entry and not any(p.upper().startswith("LICENSE") for p in files):
            blob = await _json(fetcher, f"https://api.github.com/repos/{repo}/git/blobs/{license_entry['sha']}")
            files[license_entry["path"]] = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
    return packages.from_files(files, {"origin": "source_package", "url": url, "author": repo.split("/")[0],
        "repository": repo, "path": prefix.rstrip("/"), "commit": sha,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "scan_version": packages.SCAN_VERSION})


async def discover_official(query, limit=6):
    words = [x.lower() for x in re.findall(r"[\w-]+", query) if len(x) > 1]
    results, errors = [], []
    async def discover(repo, directory):
        try:
            async with asyncio.timeout(10):
                sha, tree = await _tree(SafeFetcher(), repo, "HEAD")
            for entry in tree:
                path = entry["path"]
                if not path.startswith(directory) or not path.endswith("/SKILL.md"):
                    continue
                relevance = sum(w in path.lower() for w in words)
                if not relevance:
                    continue
                results.append({"id": f"{repo}:{path}:{sha}", "source": "github_official",
                    "name": path.split("/")[-2], "description": "官方仓库候选，待完整包核验",
                    "url": f"https://github.com/{repo}/blob/{sha}/{path}", "repository": repo,
                    "path": path[:-9], "commit": sha, "heat": 0, "relevance": relevance})
        except Exception as exc:
            errors.append(f"{repo}：{type(exc).__name__}")
    await asyncio.gather(*(discover(*repo) for repo in OFFICIAL_REPOS))
    return {"results": sorted(results, key=lambda x: x["relevance"], reverse=True)[:limit], "errors": errors}


async def discover_index(site_url, limit=10):
    parsed = urlsplit(site_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    data = await _json(SafeFetcher(), origin + "/.well-known/skills/index.json")
    entries = data.get("skills") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise packages.PackageError("标准 Skill 索引格式不合法")
    result = []
    for item in entries[:limit]:
        if not isinstance(item, dict):
            continue
        target = item.get("url") or item.get("path")
        if target:
            result.append({"name": item.get("name", ""), "description": item.get("description", ""),
                           "url": urljoin(origin + "/", target), "source": "standard_index", "heat": 0})
    return result
