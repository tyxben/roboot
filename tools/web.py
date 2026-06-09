"""Web fetch + search tools.

Gives the agent eyes on the live web instead of asking the user to open a
browser or shelling out to `curl` (which the danger gate flags and which
pollutes the tool audit). Both tools are side_effect="read" — no confirmation.

SSRF defense (these run on an LLM that can be prompt-injected from a Telegram
message or a fetched page): web_fetch refuses anything that isn't a public
http(s) URL. Scheme must be http/https; the host is resolved and every
resolved IP must be global (no loopback / private / link-local / reserved /
multicast — that blocks localhost, 127.0.0.1, 10/172.16/192.168, ::1, and the
cloud metadata endpoint 169.254.169.254). Redirects are followed manually, one
hop at a time, re-validating each Location's host — so a public URL can't 302
you onto an internal one.
"""

from __future__ import annotations

import html
import ipaddress
import logging
import socket
import urllib.parse
from html.parser import HTMLParser

import arcana

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15.0
MAX_BODY_BYTES = 2_000_000
MAX_RETURN_CHARS = 8_000
MAX_REDIRECTS = 4
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Roboot/1.0"


def _host_is_safe(host: str) -> tuple[bool, str]:
    """Resolve `host` and require every address to be globally routable."""
    if not host:
        return False, "URL 缺少主机名"
    host = host.strip("[]")  # ipv6 literal brackets
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"无法解析主机名 {host}：{e}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip scope id
        except ValueError:
            return False, f"无法解析 IP：{addr}"
        if not ip.is_global or ip.is_multicast:
            return False, f"拒绝访问非公网地址（{addr}），疑似 SSRF"
    return True, ""


def _validate_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"URL 解析失败：{e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"只允许 http/https，拒绝 scheme：{parsed.scheme or '(空)'}"
    return _host_is_safe(parsed.hostname or "")


class _TextExtractor(HTMLParser):
    """Strip tags → readable text, dropping script/style/noscript content."""

    _SKIP = {"script", "style", "noscript", "template", "svg"}
    _BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _html_to_text(body: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(body)
    except Exception:
        pass
    return parser.text()


async def _get(url: str, *, data: dict | None = None) -> "tuple[int, dict, str]":
    """SSRF-checked GET/POST with manual redirect following. Returns
    (status, headers, body_text). Raises on transport error."""
    import httpx

    method = "POST" if data is not None else "GET"
    async with httpx.AsyncClient(follow_redirects=False, timeout=REQUEST_TIMEOUT) as cli:
        for _ in range(MAX_REDIRECTS + 1):
            ok, reason = _validate_url(url)
            if not ok:
                raise ValueError(reason)
            resp = await cli.request(
                method, url, headers={"User-Agent": _UA}, data=data
            )
            if resp.is_redirect and "location" in resp.headers:
                url = urllib.parse.urljoin(url, resp.headers["location"])
                method, data = "GET", None  # follow as GET
                continue
            body = resp.content[:MAX_BODY_BYTES]
            ctype = resp.headers.get("content-type", "")
            try:
                text = body.decode(resp.encoding or "utf-8", errors="replace")
            except Exception:
                text = body.decode("utf-8", errors="replace")
            return resp.status_code, {"content-type": ctype}, text
    raise ValueError("重定向次数过多")


@arcana.tool(
    when_to_use=(
        "当你需要读取一个网页/接口的内容时，例如查文档、看一篇文章、读 JSON API。"
        "只接受公网 http/https 链接。"
    ),
    what_to_expect="网页正文的纯文本（已去标签、截断到约 8000 字）或原始文本/JSON",
    failure_meaning="URL 非法、指向内网（被 SSRF 防护拦下）、超时或返回错误",
    side_effect="read",
)
async def web_fetch(url: str) -> str:
    """抓取一个公网 URL 并返回其可读文本内容。"""
    url = (url or "").strip()
    if not url:
        return "URL 不能为空"
    ok, reason = _validate_url(url)
    if not ok:
        return reason
    try:
        status, headers, body = await _get(url)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"抓取失败：{e}"
    ctype = headers.get("content-type", "")
    if "html" in ctype or (not ctype and "<html" in body[:2000].lower()):
        body = _html_to_text(body)
    if len(body) > MAX_RETURN_CHARS:
        body = body[:MAX_RETURN_CHARS] + "\n…(内容已截断)"
    prefix = f"[HTTP {status}] {url}\n"
    return prefix + (body or "(空响应)")


class _DDGResults(HTMLParser):
    """Pull (title, url) pairs out of DuckDuckGo's HTML results page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []
        self._in_result = False
        self._href = ""
        self._title_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            ad = dict(attrs)
            cls = ad.get("class", "") or ""
            if "result__a" in cls:
                self._in_result = True
                self._href = ad.get("href", "") or ""
                self._title_parts = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_result:
            self._in_result = False
            title = "".join(self._title_parts).strip()
            href = self._unwrap(self._href)
            if title and href:
                self.results.append((title, href))

    def handle_data(self, data):
        if self._in_result:
            self._title_parts.append(data)

    @staticmethod
    def _unwrap(href: str) -> str:
        # DDG wraps the real URL in //duckduckgo.com/l/?uddg=<encoded>
        try:
            q = urllib.parse.urlparse(href).query
            uddg = urllib.parse.parse_qs(q).get("uddg")
            if uddg:
                return urllib.parse.unquote(uddg[0])
        except Exception:
            pass
        if href.startswith("//"):
            return "https:" + href
        return href


@arcana.tool(
    when_to_use=(
        "当你需要在网上搜索实时信息时，例如查某个库的最新版本、查新闻、查事实。"
        "返回前几条结果的标题和链接，可再用 web_fetch 打开。"
    ),
    what_to_expect="前若干条搜索结果（标题 + 链接）",
    failure_meaning="搜索服务不可用或无结果",
    side_effect="read",
)
async def web_search(query: str, limit: int = 5) -> str:
    """用 DuckDuckGo 搜索（无需 API key），返回前 limit 条结果。"""
    query = (query or "").strip()
    if not query:
        return "搜索词不能为空"
    try:
        _, _, body = await _get(
            "https://html.duckduckgo.com/html/", data={"q": query}
        )
    except Exception as e:
        return f"搜索失败：{e}"
    parser = _DDGResults()
    try:
        parser.feed(body)
    except Exception:
        pass
    results = parser.results[: max(1, min(limit, 10))]
    if not results:
        return f"没有找到「{query}」的结果"
    lines = [f"{i + 1}. {html.unescape(t)}\n   {u}" for i, (t, u) in enumerate(results)]
    return f"「{query}」的搜索结果：\n" + "\n".join(lines)
