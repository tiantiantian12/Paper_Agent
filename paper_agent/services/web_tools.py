"""联网检索与资源下载（web_search / download_file）。

设计取舍：
- **免密可用**：网页检索走 DuckDuckGo 的 HTML 端点；图片素材依次尝试
  Openverse（CC 授权，免密）、DuckDuckGo 图片、Wikimedia Commons，
  这样不需要用户额外申请搜索 Key 就能"去网上找素材"。
- **下载有边界**：只允许 http/https，并挡掉内网 / 本机 / 保留地址
  （避免被诱导访问本地服务），限制体积与内容类型，
  落盘到产物目录，可直接当 PPT 背景或插图使用。

检索结果只返回标题 / 链接 / 摘要，**不抓正文**：正文浏览交给模型自行判断，
避免把整页 HTML 灌进上下文。
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from paper_agent.core.constants import ARTIFACTS_DIR
from paper_agent.services import session_artifacts
from paper_agent.services.skills.base import Tool, ToolParam, ToolRegistry, ToolResult
from paper_agent.utils.files import human_readable_size

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PaperAgent/1.0"
SEARCH_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 60
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024        # 单个资源上限 30MB
MAX_RESULTS = 10

# 允许下载并落盘的类型（背景图 / 插图够用）
IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

_TAG = re.compile(r"<[^>]+>")
_DDG_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
_DDG_SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(?P<text>.*?)</a>', re.S)
_DDG_VQD = re.compile(r'vqd=["\']?([\d-]+)["\']?')
# 必应 / 360 的网页结果块（都是「按块切分再逐个取标题与摘要」，避免标题摘要错配）
_BING_BLOCK = re.compile(r'<li class="b_algo"')
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', re.S)
_BING_TITLE = re.compile(r"<h2[^>]*>\s*<a[^>]*>(.*?)</a>", re.S)
_BING_SNIPPET = re.compile(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', re.S)
_BING_ANY_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_SO_BLOCK = re.compile(r'class="res-list')
_SO_LINK = re.compile(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"', re.S)
_SO_TITLE = re.compile(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", re.S)
_SO_SNIPPET = re.compile(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>', re.S)
_BING_MURL = re.compile(r'murl&quot;:&quot;(.*?)&quot;')
_BING_MTITLE = re.compile(r'&quot;t&quot;:&quot;(.*?)&quot;')


# ---------------------------------------------------------------- 网络基础
def _fetch(url: str, timeout: int = SEARCH_TIMEOUT, referer: str = "") -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def clean_text(raw: str, limit: int = 220) -> str:
    """把 HTML 片段压成一行纯文本。"""
    text = html.unescape(_TAG.sub("", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def unwrap_redirect(url: str) -> str:
    """还原跳转链接（DuckDuckGo 的结果链接是包了一层的）。"""
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    if "duckduckgo.com/l/" in url:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        target = query.get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return url


def is_public_url(url: str) -> bool:
    """是否是可安全访问的公网地址（挡掉内网 / 本机 / 保留地址）。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


# ---------------------------------------------------------------- 网页检索
def _parse_blocks(page: str, splitter, link_re, title_re, snippet_re, extra_re, source: str,
                  count: int) -> list[dict]:
    """按结果块切分页面再逐个取值，避免标题与摘要错位。"""
    results: list[dict] = []
    for block in splitter.split(page)[1 : count + 1]:
        found = link_re.search(block)
        if found is None:
            continue
        link = html.unescape(found.group(1))
        if not link.startswith("http"):
            continue
        title = title_re.search(block)
        snippet = snippet_re.search(block) or (extra_re.search(block) if extra_re else None)
        results.append(
            {
                "title": clean_text(title.group(1), 120) if title else "",
                "url": unwrap_redirect(link),
                "snippet": clean_text(snippet.group(1), 220) if snippet else "",
                "source": source,
            }
        )
    return results


def _search_bing(query: str, count: int) -> list[dict]:
    """必应中国：国内网络可达，实测稳定（免密）。"""
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "ensearch": "0"})
    page = _fetch(url).decode("utf-8", errors="replace")
    return _parse_blocks(
        page, _BING_BLOCK, _BING_LINK, _BING_TITLE, _BING_SNIPPET, _BING_ANY_P, "bing", count
    )


def _search_360(query: str, count: int) -> list[dict]:
    """360 搜索：必应解析不到时的国内兜底。"""
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    page = _fetch(url).decode("utf-8", errors="replace")
    return _parse_blocks(
        page, _SO_BLOCK, _SO_LINK, _SO_TITLE, _SO_SNIPPET, _BING_ANY_P, "360", count
    )


def _search_duckduckgo(query: str, count: int) -> list[dict]:
    """DuckDuckGo：境外网络下的补充来源。"""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    page = _fetch(url).decode("utf-8", errors="replace")
    titles = _DDG_RESULT.findall(page)
    snippets = [clean_text(item) for item in _DDG_SNIPPET.findall(page)]

    results: list[dict] = []
    for index, (link, title) in enumerate(titles[:count]):
        results.append(
            {
                "title": clean_text(title, 120),
                "url": unwrap_redirect(html.unescape(link)),
                "snippet": snippets[index] if index < len(snippets) else "",
                "source": "duckduckgo",
            }
        )
    return results


def _search_first(providers, query: str, count: int) -> list[dict]:
    """依次尝试多个来源，任一有结果即返回；全部失败才抛出汇总错误。

    顺序按「当前网络可达性」排：必应中国 → 360 → DuckDuckGo →
    （图片再加 Openverse / Wikimedia 兜底）。
    """
    errors: list[str] = []
    for provider in providers:
        try:
            results = provider(query, count)
        except Exception as exc:      # noqa: BLE001 - 逐个降级，别让一个源拖垮
            errors.append(f"{provider.__name__}: {str(exc)[:80]}")
            continue
        if results:
            return results
    if errors:
        raise RuntimeError("所有检索源都失败：" + "；".join(errors))
    return []


def search_web(query: str, count: int = 5) -> list[dict]:
    """检索网页，返回 ``[{title, url, snippet}]``。"""
    text = (query or "").strip()
    if not text:
        raise ValueError("检索关键词为空")
    return _search_first(
        (_search_bing, _search_360, _search_duckduckgo),
        text,
        max(1, min(int(count or 5), MAX_RESULTS)),
    )


# ---------------------------------------------------------------- 图片素材
def _search_bing_images(query: str, count: int) -> list[dict]:
    """必应图片：国内可达、出图快，且返回的是原图直链（实测可直接下载）。

    实测：不需要 Referer 也能下到图，故下载环节不额外伪造来源页。
    """
    url = "https://cn.bing.com/images/async?" + urllib.parse.urlencode(
        {"q": query, "first": "1", "count": str(max(count * 2, 12)), "mmasync": "1"}
    )
    page = _fetch(url, referer="https://cn.bing.com/images/search").decode(
        "utf-8", errors="replace"
    )
    links = [html.unescape(item) for item in _BING_MURL.findall(page)]
    titles = [clean_text(item, 90) for item in _BING_MTITLE.findall(page)]

    results: list[dict] = []
    seen: set[str] = set()
    for link in links:
        if not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        index = len(results)
        results.append(
            {
                "title": titles[index] if index < len(titles) else "",
                "url": link,
                "source": "bing",
            }
        )
        if len(results) >= count:
            break
    return results


def _search_openverse(query: str, count: int) -> list[dict]:
    """Openverse：CC 授权的开放素材，免密。"""
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
        {"q": query, "page_size": count, "mature": "false"}
    )
    data = json.loads(_fetch(url).decode("utf-8", errors="replace"))
    results = []
    for item in (data.get("results") or [])[:count]:
        link = item.get("url") or ""
        if not link:
            continue
        results.append(
            {
                "title": clean_text(item.get("title") or "", 90),
                "url": link,
                "creator": clean_text(item.get("creator") or "", 40),
                "license": item.get("license") or "",
                "source": "openverse",
            }
        )
    return results


def _search_duckduckgo_images(query: str, count: int) -> list[dict]:
    """DuckDuckGo 图片：先取 vqd 令牌，再查 i.js 接口。"""
    page = _fetch(
        "https://duckduckgo.com/?" + urllib.parse.urlencode({"q": query, "iax": "images", "ia": "images"})
    ).decode("utf-8", errors="replace")
    token = _DDG_VQD.search(page)
    if token is None:
        return []
    url = "https://duckduckgo.com/i.js?" + urllib.parse.urlencode(
        {"l": "us-en", "o": "json", "q": query, "vqd": token.group(1), "f": ",,,", "p": "1"}
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://duckduckgo.com/"},
    )
    with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    results = []
    for item in (data.get("results") or [])[:count]:
        link = item.get("image") or ""
        if not link:
            continue
        results.append(
            {
                "title": clean_text(item.get("title") or "", 90),
                "url": link,
                "width": item.get("width"),
                "height": item.get("height"),
                "source": "duckduckgo",
            }
        )
    return results


def _search_commons(query: str, count: int) -> list[dict]:
    """Wikimedia Commons 兜底：免密、有明确授权信息。"""
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrlimit": count,
            "gsrnamespace": "6",
            "prop": "imageinfo",
            "iiprop": "url|size",
            "iiurlwidth": "1600",
            "format": "json",
        }
    )
    data = json.loads(_fetch(url).decode("utf-8", errors="replace"))
    pages = ((data.get("query") or {}).get("pages") or {}).values()
    results = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        link = info.get("thumburl") or info.get("url") or ""
        if not link:
            continue
        results.append(
            {
                "title": clean_text(page.get("title") or "", 90),
                "url": link,
                "license": "Commons",
                "source": "wikimedia",
            }
        )
    return results


def search_images(query: str, count: int = 6) -> list[dict]:
    """检索图片素材，返回带直链的结果列表。

    实测（国内网络）：必应图片可用且能直接下载，因此排在最前；
    Openverse / Wikimedia 等境外源留作兜底（境外网络下更有用）。
    """
    text = (query or "").strip()
    if not text:
        raise ValueError("检索关键词为空")
    limit = max(1, min(int(count or 6), MAX_RESULTS))
    results = _search_first(
        (
            _search_bing_images,
            _search_duckduckgo_images,
            _search_openverse,
            _search_commons,
        ),
        text,
        limit,
    )
    if not results:
        raise RuntimeError("没有找到可用的图片素材")
    return results


# ---------------------------------------------------------------- 下载
def _safe_filename(name: str, suffix: str) -> str:
    """清理文件名并按**实际内容类型**补扩展名（避免出现 ``x.png.jpg``）。"""
    stem = Path(name or "").name
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem).strip(" .")
    if Path(stem).suffix.lower() in IMAGE_TYPES.values():
        stem = Path(stem).stem              # 丢掉用户给的扩展名，以真实类型为准
    if not stem:
        stem = f"素材-{time.strftime('%Y%m%d-%H%M%S')}"
    return f"{stem}{suffix}"


def _unique_path(name: str, directory: Path | None = None) -> Path:
    """在目标目录（默认本会话产物目录）里取一个不重名的路径。"""
    return session_artifacts.unique_path(name, base=directory or ARTIFACTS_DIR)


def download_file(url: str, filename: str = "", directory: Path | None = None) -> Path:
    """下载一个图片资源到产物目录（默认**本会话**的产物目录），返回本地路径。"""
    link = (url or "").strip()
    if not link:
        raise ValueError("下载地址为空")
    if not is_public_url(link):
        raise ValueError(f"拒绝下载：只允许公网 http/https 地址（{link[:80]}）")

    request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        content_type = (response.headers.get_content_type() or "").lower()
        data = response.read(MAX_DOWNLOAD_BYTES + 1)

    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"资源过大（超过 {human_readable_size(MAX_DOWNLOAD_BYTES)}）")
    if not data:
        raise ValueError("下载到空文件")

    suffix = IMAGE_TYPES.get(content_type, "")
    if not suffix:
        # 有些 CDN 给 octet-stream，按 URL 后缀兜底
        guess = Path(urllib.parse.urlparse(link).path).suffix.lower()
        suffix = guess if guess in IMAGE_TYPES.values() else ""
    if not suffix:
        raise ValueError(f"不支持的类型：{content_type or '未知'}（只支持常见图片格式）")

    path = _unique_path(
        _safe_filename(filename or Path(link).name, suffix),
        Path(directory) if directory else None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------- 工具
class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网检索。`kind=web` 查资料、找论据与数据来源（返回标题/链接/摘要）；"
        "`kind=image` 找可用的图片素材（返回图片直链 + 授权信息），"
        "配合 download_file 下载后即可当 PPT 背景或正文插图。"
        "需要论文背景资料、最新数据、图片素材时用它，不要凭记忆编造来源。"
    )

    def __init__(self) -> None:
        self.parameters = [
            ToolParam("query", "string", "检索关键词，尽量具体"),
            ToolParam("kind", "string", "web=网页资料；image=图片素材", required=False,
                      enum=["web", "image"]),
            ToolParam("count", "integer", "返回条数，默认 5（图片默认 6）", required=False),
        ]

    def run(self, query: str = "", kind: str = "web", count: int = 0, **kwargs) -> ToolResult:
        mode = (kind or "web").strip().lower()
        try:
            if mode == "image":
                items = search_images(query, count or 6)
            else:
                items = search_web(query, count or 5)
        except Exception as exc:      # noqa: BLE001 - 网络问题转成可读提示
            return ToolResult(
                content=f"联网检索失败：{exc}",
                success=False,
                error=str(exc),
            )

        lines = [f"「{query}」的检索结果（{'图片素材' if mode == 'image' else '网页'}）："]
        for index, item in enumerate(items, start=1):
            extra = " · ".join(
                str(item[key])
                for key in ("creator", "license", "source")
                if item.get(key)
            )
            line = f"{index}. {item.get('title') or '（无标题）'}\n   {item.get('url')}"
            if item.get("snippet"):
                line += f"\n   {item['snippet']}"
            if extra:
                line += f"\n   来源信息：{extra}"
            lines.append(line)
        if mode == "image":
            lines.append("提示：用 download_file 下载所需图片，再作为插图或 PPT 背景使用。")
        return ToolResult(content="\n".join(lines))


class DownloadFileTool(Tool):
    name = "download_file"
    description = (
        "把联网找到的资源（图片）下载到本会话产物目录，返回本地路径。"
        "下载后的图片可直接给 create_pptx 的 background / backgrounds 参数当背景，"
        "也可以写进 Markdown 的 ![图 x-y 说明](本地文件名) 当插图。"
    )

    def __init__(self) -> None:
        self.parameters = [
            ToolParam("url", "string", "公网 http/https 图片直链"),
            ToolParam("filename", "string", "保存的文件名（可省略）", required=False),
        ]

    def run(self, url: str = "", filename: str = "", **kwargs) -> ToolResult:
        try:
            path = download_file(url, filename)
        except Exception as exc:      # noqa: BLE001
            return ToolResult(
                content=f"下载失败：{exc}", success=False, error=str(exc)
            )
        size = human_readable_size(path.stat().st_size)
        return ToolResult(
            content=f"已下载到产物目录：{path.name}（{size}）\n路径：{path}",
            artifact_paths=[str(path)],
        )


def register_web_skills(registry: ToolRegistry) -> ToolRegistry:
    """注册联网能力（检索 + 下载）。"""
    for tool in (WebSearchTool(), DownloadFileTool()):
        registry.register(tool)
    return registry
