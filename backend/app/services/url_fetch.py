"""公开网页的受控网络边界：DNS 固定、逐跳校验、限额解压，不携带业务凭据。"""
import asyncio
import ipaddress
import re
import socket
import time
import zlib
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp


class UrlImportError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise UrlImportError("unsafe_address", "链接解析到了非法网络地址") from None
    if not address.is_global or address.is_multicast or address.is_reserved or getattr(address, "ipv4_mapped", None):
        raise UrlImportError("unsafe_address", "仅允许访问公开互联网地址")
    return address


def normalize_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise UrlImportError("invalid_url", "请输入有效的公开网页链接（不超过 4096 字符）")
    value = value.strip()
    if re.search(r"[\x00-\x20\x7f\\]", value):
        raise UrlImportError("invalid_url", "链接包含不支持的字符")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError()
        host = parts.hostname.rstrip(".").lower()
        if "%" in host:
            raise ValueError()
        # 主机名字面量的回环/本地保留域名在规范化层直接拒绝，
        # 不单独依赖 DNS 解析层（PublicResolver）拦截，防止 hosts 改写或解析旁路。
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".localdomain")):
            raise ValueError()
        port = parts.port
        expected_port = 443 if parts.scheme.lower() == "https" else 80
        if port is not None and port != expected_port:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii")
            if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError()
        else:
            public_address(str(address))
            host = f"[{host}]" if address.version == 6 else host
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    except UrlImportError:
        raise
    except (ValueError, UnicodeError):
        raise UrlImportError("invalid_url", "仅支持无账号信息的 HTTP／HTTPS 公网链接及标准端口") from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        # 结果校验与 connector 使用同一份地址，避免再次 DNS 查询造成重绑定。
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        except OSError:
            raise UrlImportError("dns_failed", "无法解析网页域名") from None
        if not answers:
            raise UrlImportError("dns_failed", "无法解析网页域名")
        result = []
        for family, _, protocol, _, address in answers:
            public_address(address[0])
            result.append({"hostname": host, "host": address[0], "port": port, "family": family,
                           "proto": protocol, "flags": socket.AI_NUMERICHOST})
        return result

    async def close(self):
        pass


@dataclass
class FetchBudget:
    deadline: float = field(default_factory=lambda: time.monotonic() + 60)
    requests: int = 0
    size: int = 0
    cancelled: bool = False

    def check(self):
        if self.cancelled:
            raise asyncio.CancelledError()
        if time.monotonic() >= self.deadline:
            raise UrlImportError("timeout", "解析超时，请稍后重试或手动补充正文")

    def request(self):
        self.check()
        self.requests += 1
        if self.requests > 100:
            raise UrlImportError("budget", "网页资源请求过多，已停止解析")

    def consume(self, amount):
        self.check()
        self.size += amount
        if self.size > 20 * 1024 * 1024:
            raise UrlImportError("budget", "网页资源超出总大小限制")


@dataclass
class FetchResult:
    url: str
    body: bytes
    content_type: str
    status: int = 200
    location: str = ""

    def text(self):
        from bs4 import UnicodeDammit
        match = re.search(r"charset\s*=\s*[\"']?([^\s;\"']+)", self.content_type, re.I)
        return UnicodeDammit(self.body, known_definite_encodings=[match[1]] if match else [], is_html=True).unicode_markup or ""


async def bounded_body(response, budget, limit):
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding not in ("identity", "gzip", "deflate", ""):
        raise UrlImportError("encoding", "网页压缩格式不受支持")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in ("gzip", "deflate") else None
    output, compressed = bytearray(), 0
    try:
        async for block in response.content.iter_chunked(65536):
            compressed += len(block)
            if compressed > limit:
                raise UrlImportError("too_large", "网页资源超出大小限制")
            while block:
                data = decoder.decompress(block, 65536) if decoder else block
                block = decoder.unconsumed_tail if decoder else b""
                budget.consume(len(data))
                output.extend(data)
                if len(output) > limit:
                    raise UrlImportError("too_large", "网页正文解压后超出大小限制")
        if decoder and (not decoder.eof or decoder.unused_data):
            raise UrlImportError("encoding", "网页压缩内容不完整或格式不受支持")
    except zlib.error:
        raise UrlImportError("encoding", "无法解压网页内容") from None
    return bytes(output)


class SafeFetcher:
    def __init__(self, budget=None):
        self.budget = budget or FetchBudget()

    async def fetch(self, url, *, resource=False, follow_redirects=True, method="GET"):
        if method not in ("GET", "HEAD"):
            raise UrlImportError("method", "网页解析不允许提交数据")
        url = normalize_url(url)
        for hop in range(6):
            self.budget.request()
            connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, force_close=True)
            timeout = aiohttp.ClientTimeout(total=min(15, max(.01, self.budget.deadline - time.monotonic())))
            try:
                async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                                                 cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False) as client:
                    async with client.request(method, url, allow_redirects=False, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; JianzirumianKnowledge/1.0)",
                        "Accept": "text/html,application/xhtml+xml,text/plain,application/json,*/*;q=0.1",
                        "Accept-Encoding": "gzip, deflate", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                    }) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = normalize_url(urljoin(url, response.headers.get("Location", "")))
                            if not response.headers.get("Location"):
                                raise UrlImportError("redirect", "网页重定向缺少目标地址")
                            if not follow_redirects:
                                return FetchResult(url, b"", "text/plain", response.status, location)
                            if hop == 5:
                                raise UrlImportError("redirect", "网页重定向次数过多")
                            url = location
                            continue
                        if response.status in (401, 403, 429):
                            raise UrlImportError("restricted", "网站限制访问；不会绕过登录、验证或访问频率限制，可手动补充正文")
                        if response.status >= 400:
                            raise UrlImportError("http_error", f"网页返回 HTTP {response.status}，请检查链接")
                        content_type = response.headers.get("Content-Type", "").lower()
                        mime = content_type.split(";", 1)[0].strip()
                        allowed = {"text/html", "application/xhtml+xml", "text/plain"}
                        if resource:
                            allowed |= {"text/css", "application/javascript", "text/javascript", "application/json", "application/ld+json", "application/x-javascript"}
                        if mime not in allowed:
                            raise UrlImportError("content_type", "仅支持网页正文；PDF／Word 请使用文件上传")
                        limit = (2 if resource else 5) * 1024 * 1024
                        body = await bounded_body(response, self.budget, limit)
                        return FetchResult(url, body, content_type, response.status)
            except UrlImportError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                raise UrlImportError("fetch_failed", "无法获取网页，可能是网络超时或站点限制") from None
        raise UrlImportError("redirect", "网页重定向次数过多")
