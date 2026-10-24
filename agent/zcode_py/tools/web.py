"""出网工具：webfetch / websearch（M2 任务书；R23）。

安全约束（PLAN §9.1，任务书原文）：
- 仅 http/https；
- 发请求前对目标 host 做校验：解析后拒绝 localhost/环回/私有/保留地址，
  含 IPv6 与十进制 IP 变体——netguard.py 统一实现（变体由 OS 解析器归一，
  见该模块 docstring 的实测记录）；
- 重定向逐跳重新校验（最多 5 跳），防 302 跳内网；
- http 客户端 ``trust_env=False``：不读代理环境变量，堵住"经代理绕过
  host 校验"的洞（企业代理环境属后续里程碑的显式配置项）；
- 隐私护栏：URL 形似敏感文件（*.pem/.env/id_rsa…）时结果带 warning。

websearch 后端默认 DuckDuckGo Lite HTML（无 Key、无依赖），容忍性正则解析；
可用 init.settings.searchBaseUrl 换 SearXNG 等自建后端（同样过 host 校验）。
解析器对页面结构变化是 best-effort——失败时返回结构化错误而非抛异常。
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Callable
from urllib.parse import quote_plus, urljoin

import httpx

from ..netguard import NetguardError, validate_url
from ..privacy import privacy_warning_for_url
from . import MAX_TOOL_OUTPUT_CHARS, ToolOutcome, ToolSpec, error_outcome, head_tail

FETCH_TIMEOUT = 20.0
FETCH_MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_SEARCH_BASE = "https://lite.duckduckgo.com/lite/"

_UA = "Mozilla/5.0 (compatible; zcode-py/0.1; +https://localhost)"


# ---------------------------------------------------------------------------
# 取回（纯函数化：tests 用 fake 注入）
# ---------------------------------------------------------------------------


def html_to_text(raw: str) -> str:
    """轻量 HTML→文本：去 script/style、剥标签、解实体、压空行。"""
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _iter_content(client: httpx.Client, url: str) -> tuple[httpx.Response, bytes]:
    """流式读 body，2MB 封顶（防尺寸炸弹）。"""
    with client.stream("GET", url) as resp:
        buf = bytearray()
        for chunk in resp.iter_bytes(64 * 1024):
            buf.extend(chunk)
            if len(buf) >= FETCH_MAX_BYTES:
                break
        return resp, bytes(buf)


def fetch_page(
    url: str,
    *,
    fetch: Callable[[str], tuple[int, str, str, bytes]] | None = None,
) -> dict:
    """校验 + 取回 + 重定向逐跳校验。返回 {url,status,contentType,body}。

    NetguardError 原样上抛（webfetch/websearch 转结构化错误）。
    ``fetch`` 注入点仅供离线测试替换真实 HTTP。
    """
    current = validate_url(url)
    visited = 0
    if fetch is not None:
        status, content_type, final_url, body = fetch(current)
        return {"url": url, "finalUrl": final_url, "status": status,
                "contentType": content_type, "body": body}
    with httpx.Client(
        timeout=FETCH_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
        headers={"user-agent": _UA},
    ) as client:
        while True:
            resp, body = _iter_content(client, current)
            if resp.is_redirect:
                location = resp.headers.get("location")
                visited += 1
                if not location or visited > MAX_REDIRECTS:
                    raise NetguardError(
                        f"too many redirects or missing Location (hop {visited})"
                    )
                current = validate_url(urljoin(current, location))
                continue
            content_type = resp.headers.get("content-type", "")
            return {
                "url": url,
                "finalUrl": str(resp.url),
                "status": resp.status_code,
                "contentType": content_type,
                "body": body,
            }


# ---------------------------------------------------------------------------
# webfetch 工具
# ---------------------------------------------------------------------------


def tool_webfetch(args: dict, ctx) -> ToolOutcome:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return error_outcome("webfetch requires a string 'url'")
    max_chars = args.get("max_chars")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        max_chars = MAX_TOOL_OUTPUT_CHARS
    try:
        page = fetch_page(url)
    except NetguardError as exc:
        return ToolOutcome(
            result={"error": "blocked", "detail": f"host check failed: {exc.reason}"},
            is_error=True,
        )
    except httpx.HTTPError as exc:
        return ToolOutcome(
            result={"error": "network",
                    "detail": f"{type(exc).__name__}: {exc}"},
            is_error=True,
        )
    if page["status"] >= 400:
        return ToolOutcome(
            result={"error": "http", "status": page["status"],
                    "detail": f"HTTP {page['status']} for {page['finalUrl']}"},
            is_error=True,
        )
    raw = page["body"].decode("utf-8", errors="replace")
    if "html" in (page["contentType"] or "").lower():
        text = html_to_text(raw)
    else:
        text = raw
    text, truncated = head_tail(text, max_chars)
    return ToolOutcome(
        result={
            "url": page["finalUrl"],
            "status": page["status"],
            "contentType": page["contentType"],
            "content": text,
            "truncated": truncated,
        },
        warning=privacy_warning_for_url(url),
    )


# ---------------------------------------------------------------------------
# websearch 工具（DDG Lite 解析）
# ---------------------------------------------------------------------------

_ANCHOR_RE = re.compile(r"<a\s+([^>]*class=['\"][^'\"]*result-link[^'\"]*['\"][^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r"""href=['"]([^'"]+)['"]""", re.I)
_SNIPPET_RE = re.compile(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_ddg_lite(page_html: str, *, max_results: int = 5) -> list[dict]:
    """容忍性解析 DuckDuckGo Lite 结果页（纯函数，离线可测）。"""
    anchors = _ANCHOR_RE.findall(page_html)
    snippets = [
        html_mod.unescape(_TAG_RE.sub("", s)).strip()
        for s in _SNIPPET_RE.findall(page_html)
    ]
    results: list[dict] = []
    seen: set[str] = set()
    for i, (attrs, title_html) in enumerate(anchors):
        href_match = _HREF_RE.search(attrs)
        if href_match is None:
            continue
        href = href_match.group(1)
        # 跳过广告与站内链接
        if "duckduckgo.com" in href or href.startswith("/"):
            continue
        if href in seen:
            continue
        seen.add(href)
        title = html_mod.unescape(_TAG_RE.sub("", title_html)).strip()
        if not title:
            continue
        entry = {"title": title[:300], "url": href}
        if i < len(snippets):
            entry["snippet"] = snippets[i][:500]
        results.append(entry)
        if len(results) >= max_results:
            break
    return results


def _search_fetch(url: str) -> tuple[int, str, str, bytes]:
    """真实出网取回（netguard 已在 fetch_page 内先行校验）。"""
    page = fetch_page(url)
    return page["status"], page["contentType"], page["finalUrl"], page["body"]


def tool_websearch(args: dict, ctx) -> ToolOutcome:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return error_outcome("websearch requires a non-empty string 'query'")
    max_results = args.get("max_results")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
        max_results = 5
    max_results = min(max_results, 10)
    base = ctx.state.get("searchBaseUrl") or DEFAULT_SEARCH_BASE
    search_url = base.rstrip("/") + "/?q=" + quote_plus(query)
    try:
        status, content_type, final_url, body = _search_fetch(search_url)
    except NetguardError as exc:
        return ToolOutcome(
            result={"error": "blocked", "detail": f"host check failed: {exc.reason}"},
            is_error=True,
        )
    except httpx.HTTPError as exc:
        return ToolOutcome(
            result={"error": "network", "detail": f"{type(exc).__name__}: {exc}"},
            is_error=True,
        )
    if status >= 400:
        return ToolOutcome(
            result={"error": "http", "status": status, "detail": f"HTTP {status}"},
            is_error=True,
        )
    results = parse_ddg_lite(body.decode("utf-8", errors="replace"), max_results=max_results)
    if not results:
        return ToolOutcome(
            result={
                "error": "no_results",
                "detail": "search backend returned no parsable results "
                          "(page layout may have changed; configure settings.searchBaseUrl)",
            },
            is_error=True,
        )
    return ToolOutcome(result={"query": query, "results": results, "count": len(results)})


WEBFETCH_SPEC = ToolSpec(
    name="webfetch",
    description=(
        "Fetch an http/https URL and return readable text (HTML stripped), "
        f"head+tail truncated at {MAX_TOOL_OUTPUT_CHARS} chars. Internal/private "
        "hosts are rejected (SSRF guard)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "content budget (default 30000)"},
        },
        "required": ["url"],
    },
    tier="readonly",
    handler=tool_webfetch,
)

WEBSEARCH_SPEC = ToolSpec(
    name="websearch",
    description=(
        "Web search (DuckDuckGo backend by default): returns [{title, url, "
        "snippet}]. Use webfetch to read a result in full."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "1-10, default 5"},
        },
        "required": ["query"],
    },
    tier="readonly",
    handler=tool_websearch,
)
