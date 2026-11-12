"""
多后端联网搜索服务。

后端：
- astrbot：AstrBot 4.29.4 内置 web_search_tavily 工具（内部契约，严格版本/配置门禁）
- api：Tavily 结构化搜索 API（basic，1 credit/次，不自动重试）
- web：Bing + 百度搜索页解析（尽力而为，不抓取结果链接正文）
- off：关闭

统一使用一个总时间预算；后端不得各自累加超时。
"""

import asyncio
import html as html_lib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger("astrbot")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

MODES = ("auto", "astrbot", "api", "web", "off")
BACKEND_ORDER = ("astrbot", "api", "web")
BACKEND_LABELS = {
    "astrbot": "AstrBot",
    "api": "Tavily API",
    "web": "网页搜索",
}
# 只对已在源码层核实调用契约的 AstrBot 版本启用内部适配。
SUPPORTED_ASTRBOT_VERSIONS = frozenset({"4.29.4"})
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class _TextExtractor(HTMLParser):
    """从 HTML 中提取纯文本（用于验证页识别）。"""

    def __init__(self):
        super().__init__()
        self._text_parts: list[str] = []
        self._skip_tags = {"script", "style", "noscript"}
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._text_parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._text_parts)


def _extract_text_from_html(html: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        return ""
    return extractor.get_text()


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _extract_meta_description(html: str) -> str:
    """保留旧辅助函数，供兼容测试/调用。"""
    match = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    match = re.search(
        r'<meta\s+content=["\'](.*?)["\']\s+name=["\']description["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return ""


def _build_summary(text: str, max_length: int = 1500) -> str:
    """保留旧摘要函数，供兼容测试/调用。"""
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_period = max(
        truncated.rfind("。"), truncated.rfind("！"), truncated.rfind("？")
    )
    if last_period > max_length // 2:
        return truncated[: last_period + 1]
    return truncated + "..."


def _truncate_text(text: str, max_chars: int) -> str:
    """按字符预算硬截断，不添加编造内容。"""
    text = (text or "").strip()
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 150
    limit = max(1, limit)
    return text[:limit]


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _normalize_item(raw: dict, default_source: str = "") -> dict | None:
    """只保留有标题和 http(s) URL 的条目；缺失字段留空，不编造。"""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    snippet = str(raw.get("snippet") or raw.get("content") or "").strip()
    source = str(raw.get("source") or default_source or "").strip()
    if not title or not _is_http_url(url):
        return None
    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "source": source,
    }


def build_search_summary(results: list[dict]) -> str:
    """把统一结果拼成供 AI 汇总/生成参考的文本。"""
    lines = []
    for item in results:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        source = str(item.get("source") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        url = str(item.get("url") or "").strip()
        block = f"[{source}] {title}" if source else title
        if snippet:
            block += f"\n{snippet}"
        if url:
            block += f"\n{url}"
        lines.append(block)
    return "\n\n".join(lines)


def prepare_search_digest(
    raw_summary: str,
    ai_digest: str,
    max_chars: int,
) -> str:
    """统一执行字数预算：AI 摘要优先，失败时回退原始摘要，均硬截断。"""
    text = (ai_digest or "").strip()
    if not text:
        text = (raw_summary or "").strip()
    return _truncate_text(text, max_chars)


def resolve_search_mode(enabled=True, mode=None) -> str:
    """解析模式并迁移旧 enabled=false。

    旧配置 enabled=false 必须保持不联网；此时 off 优先于 mode。
    """
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {
            "", "false", "0", "no", "off", "disabled",
        }
    elif not isinstance(enabled, bool):
        enabled = bool(enabled)
    if not enabled:
        return "off"

    normalized = str(mode or "").strip().lower()
    if not normalized:
        return "auto"
    if normalized not in MODES:
        return "auto"
    return normalized


@dataclass
class BackendResult:
    backend: str
    status: str = "failed"
    results: list[dict] = field(default_factory=list)
    reason: str = ""
    duration_ms: int = 0


class SearchBackend:
    """后端统一接口。"""

    name = "base"

    async def availability(self, event=None) -> tuple[bool, str]:
        return True, ""

    async def search(
        self,
        query: str,
        max_results: int,
        event=None,
        timeout_s: float | None = None,
    ) -> BackendResult:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class AstrBotSearchBackend(SearchBackend):
    """AstrBot 内置 web_search_tavily 适配器。

    契约来自 AstrBot 4.29.4 源码：
    astrbot.core.tools.web_search_tools.TavilyWebSearchTool.call(
        ContextWrapper[AstrAgentContext], query=..., max_results=...
    )
    该接口不是公开插件 API，因此只对已验证版本启用，并检查当前会话
    provider_settings 的 web_search/provider/key。任何一项不满足即不可用。
    """

    name = "astrbot"

    def __init__(self, context=None, adapter=None):
        self._context = context
        self._adapter = adapter

    async def availability(self, event=None) -> tuple[bool, str]:
        if self._adapter is not None:
            return True, ""
        if self._context is None or event is None:
            return False, "astrbot_context_missing"
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return False, "astrbot_context_missing"

        try:
            import astrbot  # type: ignore
        except Exception:
            return False, "astrbot_contract_unavailable"

        version = str(getattr(astrbot, "__version__", ""))
        if version not in SUPPORTED_ASTRBOT_VERSIONS:
            return False, "astrbot_version_unverified"

        try:
            from astrbot.core.agent.run_context import ContextWrapper  # noqa: F401
            from astrbot.core.astr_agent_context import AstrAgentContext  # noqa: F401
            from astrbot.core.tools.web_search_tools import TavilyWebSearchTool  # noqa: F401
        except Exception:
            return False, "astrbot_contract_unavailable"

        try:
            cfg = self._context.get_config(umo=umo)
            provider_settings = cfg.get("provider_settings", {}) or {}
        except Exception:
            return False, "astrbot_config_unavailable"

        if not provider_settings.get("web_search"):
            return False, "astrbot_search_disabled"
        if str(provider_settings.get("websearch_provider") or "").lower() != "tavily":
            return False, "astrbot_provider_unverified"

        keys = provider_settings.get("websearch_tavily_key") or []
        if isinstance(keys, str):
            keys = [keys] if keys else []
        if not keys:
            return False, "astrbot_key_missing"
        return True, ""

    async def search(
        self,
        query: str,
        max_results: int,
        event=None,
        timeout_s: float | None = None,
    ) -> BackendResult:
        start = time.monotonic()
        try:
            if self._adapter is not None:
                payload = await self._adapter(query, max_results, event)
            else:
                payload = await self._call_builtin(query, max_results, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            return BackendResult(
                self.name, "failed", [], "astrbot_search_error",
                int((time.monotonic() - start) * 1000),
            )

        if not isinstance(payload, str):
            return BackendResult(
                self.name, "failed", [], "invalid_response",
                int((time.monotonic() - start) * 1000),
            )
        if payload.startswith("Error:"):
            return BackendResult(
                self.name, "failed", [], "astrbot_search_error",
                int((time.monotonic() - start) * 1000),
            )
        try:
            data = json.loads(payload)
        except Exception:
            return BackendResult(
                self.name, "failed", [], "invalid_response",
                int((time.monotonic() - start) * 1000),
            )
        if not isinstance(data, dict):
            return BackendResult(
                self.name, "failed", [], "invalid_response",
                int((time.monotonic() - start) * 1000),
            )

        results = []
        for raw in data.get("results") or []:
            item = _normalize_item(raw, "astrbot")
            if item:
                results.append(item)
        results = results[:max_results]
        duration_ms = int((time.monotonic() - start) * 1000)
        if not results:
            return BackendResult(self.name, "no_results", [], "empty_results", duration_ms)
        return BackendResult(self.name, "success", results, "", duration_ms)

    async def _call_builtin(self, query: str, max_results: int, event):
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.astr_agent_context import AstrAgentContext
        from astrbot.core.tools.web_search_tools import TavilyWebSearchTool

        wrapper = ContextWrapper(
            AstrAgentContext(context=self._context, event=event)
        )
        tool = TavilyWebSearchTool()
        # AstrBot 内置工具声明 max_results 范围为 5-20，按契约取安全值；
        # 解析后再按调用方 result_count 截断。
        tool_max_results = max(5, min(int(max_results), 20))
        return await tool.call(
            wrapper,
            query=query,
            max_results=tool_max_results,
        )


class TavilySearchBackend(SearchBackend):
    """Tavily 结构化搜索 API 后端（basic，1 credit/次，不自动重试）。"""

    name = "api"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.tavily.com",
        provider: str = "tavily",
        client: httpx.AsyncClient | None = None,
    ):
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").strip().rstrip("/")
        self._provider = (provider or "tavily").strip().lower()
        self._client = client

    async def availability(self, event=None) -> tuple[bool, str]:
        if self._provider != "tavily":
            return False, "api_provider_unverified"
        if not self._api_key:
            return False, "missing_api_key"
        if not _is_http_url(self._base_url):
            return False, "invalid_base_url"
        return True, ""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    def _failure(self, status: str, reason: str, start: float) -> BackendResult:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "[小作文生成器] Tavily 搜索失败: status=%s reason=%s",
            status,
            reason,
        )
        return BackendResult(self.name, status, [], reason, duration_ms)

    async def search(
        self,
        query: str,
        max_results: int,
        event=None,
        timeout_s: float | None = None,
    ) -> BackendResult:
        start = time.monotonic()
        if not self._api_key:
            return BackendResult(
                self.name, "unavailable", [], "missing_api_key", 0
            )
        client = await self._get_client()
        payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = await client.post(
                f"{self._base_url}/search",
                json=payload,
                headers=headers,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            return self._failure("timeout", "timeout", start)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                reason = "http_401"
            elif status == 403:
                reason = "http_403"
            elif status == 429:
                reason = "http_429"
            elif status >= 500:
                reason = "http_5xx"
            else:
                reason = "http_error"
            return self._failure("failed", reason, start)
        except httpx.RequestError:
            return self._failure("failed", "network_error", start)
        except Exception:
            return self._failure("failed", "invalid_response", start)

        if not isinstance(data, dict):
            return self._failure("failed", "invalid_response", start)

        results = []
        for raw in data.get("results") or []:
            item = _normalize_item(raw, "tavily")
            if item:
                results.append(item)
        results = results[:max_results]
        duration_ms = int((time.monotonic() - start) * 1000)
        if not results:
            return BackendResult(self.name, "no_results", [], "empty_results", duration_ms)
        return BackendResult(self.name, "success", results, "", duration_ms)

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


class WebSearchBackend(SearchBackend):
    """Bing + 百度搜索页尽力而为后端；不抓取结果链接正文。"""

    name = "web"

    VERIFICATION_KEYWORDS = (
        "安全验证",
        "验证码",
        "人机验证",
        "异常流量",
        "请开启JavaScript",
        "请开启 JavaScript",
        "网络不给力",
        "captcha",
        "verify you are human",
        "access denied",
    )

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=None, follow_redirects=True)
        return self._client

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    @classmethod
    def _is_verification_page(cls, html: str) -> bool:
        text = _extract_text_from_html(html)
        if not text:
            return False
        low = text.lower()
        return any(keyword.lower() in low for keyword in cls.VERIFICATION_KEYWORDS)

    async def search(
        self,
        query: str,
        max_results: int,
        event=None,
        timeout_s: float | None = None,
    ) -> BackendResult:
        start = time.monotonic()
        client = await self._get_client()
        tasks = [
            asyncio.create_task(
                self._search_bing(client, query, max_results, timeout_s)
            ),
            asyncio.create_task(
                self._search_baidu(client, query, max_results, timeout_s)
            ),
        ]
        try:
            source_results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise

        merged: list[dict] = []
        seen: set[str] = set()
        failures: list[str] = []
        for label, status, items, reason in source_results:
            if status == "success":
                for item in items:
                    key = item["url"].strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(item)
            elif reason:
                failures.append(f"{label}:{reason}")
        merged = merged[:max_results]
        duration_ms = int((time.monotonic() - start) * 1000)

        if merged:
            reason = "; ".join(failures)
            return BackendResult(self.name, "success", merged, reason, duration_ms)

        if failures:
            reason = failures[0].split(":", 1)[1]
            if any("verification_page" in item for item in failures):
                reason = "verification_page"
            elif all("empty_results" in item or "no_results" in item for item in failures):
                reason = "empty_results"
            return BackendResult(self.name, "failed", [], reason, duration_ms)
        return BackendResult(self.name, "no_results", [], "empty_results", duration_ms)

    async def _search_bing(self, client, query, max_results, timeout_s):
        try:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": query, "mkt": "zh-CN"},
                headers=self._headers(),
                timeout=timeout_s,
            )
            resp.raise_for_status()
            html = resp.text
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            return ("bing", "timeout", [], "timeout")
        except httpx.HTTPStatusError as exc:
            return ("bing", "failed", [], self._http_reason(exc))
        except httpx.RequestError:
            return ("bing", "failed", [], "network_error")
        except Exception:
            return ("bing", "failed", [], "backend_error")

        if self._is_verification_page(html):
            return ("bing", "failed", [], "verification_page")
        items = self._parse_bing(html)[:max_results]
        if not items:
            return ("bing", "no_results", [], "empty_results")
        return ("bing", "success", items, "")

    async def _search_baidu(self, client, query, max_results, timeout_s):
        try:
            headers = self._headers()
            headers["Accept-Language"] = "zh-CN,zh;q=0.9"
            resp = await client.get(
                "https://www.baidu.com/s",
                params={"wd": query},
                headers=headers,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            html = resp.text
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            return ("baidu", "timeout", [], "timeout")
        except httpx.HTTPStatusError as exc:
            return ("baidu", "failed", [], self._http_reason(exc))
        except httpx.RequestError:
            return ("baidu", "failed", [], "network_error")
        except Exception:
            return ("baidu", "failed", [], "backend_error")

        if self._is_verification_page(html):
            return ("baidu", "failed", [], "verification_page")
        items = self._parse_baidu(html)[:max_results]
        if not items:
            return ("baidu", "no_results", [], "empty_results")
        return ("baidu", "success", items, "")

    @staticmethod
    def _http_reason(exc: httpx.HTTPStatusError) -> str:
        status = exc.response.status_code
        if status == 429:
            return "http_429"
        if status >= 500:
            return "http_5xx"
        return "http_error"

    @staticmethod
    def _parse_bing(html: str) -> list[dict]:
        blocks = re.findall(
            r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if not blocks:
            blocks = re.findall(
                r'<div[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</div>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
        items = []
        for block in blocks:
            anchor = re.search(
                r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                block,
                re.DOTALL | re.IGNORECASE,
            )
            if not anchor:
                continue
            url = urljoin("https://www.bing.com", html_lib.unescape(anchor.group(1)))
            title = _strip_html(anchor.group(2))
            para = re.search(
                r'<p[^>]*>(.*?)</p>', block, re.DOTALL | re.IGNORECASE
            )
            snippet = _strip_html(para.group(1)) if para else ""
            item = _normalize_item(
                {"title": title, "url": url, "snippet": snippet, "source": "bing"}
            )
            if item:
                items.append(item)
        return items

    @staticmethod
    def _parse_baidu(html: str) -> list[dict]:
        anchors = list(
            re.finditer(
                r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h3>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
        )
        items = []
        for index, anchor in enumerate(anchors):
            url = urljoin("https://www.baidu.com", html_lib.unescape(anchor.group(1)))
            title = _strip_html(anchor.group(2))
            end = (
                anchors[index + 1].start()
                if index + 1 < len(anchors)
                else min(len(html), anchor.end() + 2500)
            )
            chunk = html[anchor.end():end]
            abstract = re.search(
                r'class="[^"]*(?:c-abstract|content-right|abstract)[^"]*"[^>]*>(.*?)</(?:div|span)>',
                chunk,
                re.DOTALL | re.IGNORECASE,
            )
            snippet = _strip_html(abstract.group(1)) if abstract else ""
            item = _normalize_item(
                {"title": title, "url": url, "snippet": snippet, "source": "baidu"}
            )
            if item:
                items.append(item)
        return items

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


class SearchService:
    """多后端搜索编排器。"""

    def __init__(
        self,
        enabled: bool = True,
        timeout_ms: int = 8000,
        result_count: int = 3,
        mode: str | None = None,
        fallback_enabled: bool = False,
        api_key: str = "",
        api_base_url: str = "https://api.tavily.com",
        api_provider: str = "tavily",
        astrbot_context=None,
        astrbot_adapter=None,
        backends: dict | None = None,
        api_client: httpx.AsyncClient | None = None,
        web_client: httpx.AsyncClient | None = None,
    ):
        self.enabled = enabled
        self.mode = resolve_search_mode(enabled, mode)
        try:
            timeout_ms = int(timeout_ms)
        except (TypeError, ValueError):
            timeout_ms = 8000
        self.timeout = max(0.05, timeout_ms / 1000.0)
        try:
            result_count = int(result_count)
        except (TypeError, ValueError):
            result_count = 3
        self.result_count = max(1, min(result_count, 10))
        self.fallback_enabled = bool(fallback_enabled)
        self.api_key = str(api_key or "").strip()
        self.api_base_url = str(api_base_url or "").strip()
        self.api_provider = str(api_provider or "tavily").strip().lower()

        if backends is not None:
            self._backends = dict(backends)
        else:
            self._backends = {
                "astrbot": AstrBotSearchBackend(
                    context=astrbot_context,
                    adapter=astrbot_adapter,
                ),
                "api": TavilySearchBackend(
                    api_key=self.api_key,
                    base_url=self.api_base_url,
                    provider=self.api_provider,
                    client=api_client,
                ),
                "web": WebSearchBackend(client=web_client),
            }

    def _candidate_order(self) -> list[str]:
        if self.mode == "off":
            return []
        if self.mode == "auto":
            return list(BACKEND_ORDER)
        if self.mode not in BACKEND_ORDER:
            return []
        start = BACKEND_ORDER.index(self.mode)
        if self.fallback_enabled:
            return list(BACKEND_ORDER[start:])
        return [self.mode]

    @staticmethod
    def _disabled_result(reason: str = "disabled") -> dict:
        return {
            "status": "disabled",
            "summary": "",
            "provider": "未启用",
            "duration_ms": 0,
            "results": [],
            "attempted": [],
            "skipped": [],
            "degraded": False,
            "effective_backend": "",
            "failure_reason": reason,
        }

    async def search_game_info(self, game_name: str, event=None) -> dict:
        if self.mode == "off":
            return self._disabled_result()

        query = f"{game_name} 游戏介绍 平台 评价"
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + self.timeout
        attempts: list[dict] = []
        skipped: list[dict] = []
        order = self._candidate_order()

        def skip(name, status, reason):
            skipped.append({
                "backend": name,
                "status": status,
                "reason": reason,
                "duration_ms": 0,
                "result_count": 0,
            })

        for name in order:
            remaining = deadline - loop.time()
            if remaining <= 0:
                skip(name, "timeout", "budget_exhausted")
                break

            backend = self._backends.get(name)
            if backend is None:
                skip(name, "unavailable", "backend_unavailable")
                continue

            try:
                available, reason = await asyncio.wait_for(
                    backend.availability(event), timeout=remaining
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                skip(name, "timeout", "budget_exhausted")
                break
            except Exception:
                available, reason = False, "backend_unavailable"
            if not available:
                skip(name, "unavailable", reason or "backend_unavailable")
                continue

            remaining = deadline - loop.time()
            if remaining <= 0:
                skip(name, "timeout", "budget_exhausted")
                break

            attempt_start = loop.time()
            try:
                result = await asyncio.wait_for(
                    backend.search(
                        query,
                        self.result_count,
                        event=event,
                        timeout_s=remaining,
                    ),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                result = BackendResult(
                    name,
                    "timeout",
                    [],
                    "timeout",
                    int((loop.time() - attempt_start) * 1000),
                )
            except Exception:
                result = BackendResult(
                    name,
                    "failed",
                    [],
                    "backend_error",
                    int((loop.time() - attempt_start) * 1000),
                )

            attempts.append({
                "backend": name,
                "status": result.status,
                "reason": result.reason,
                "duration_ms": result.duration_ms,
                "result_count": len(result.results),
            })

            if result.status == "success" and result.results:
                duration_ms = int((loop.time() - start) * 1000)
                return {
                    "status": "success",
                    "summary": build_search_summary(result.results),
                    "provider": BACKEND_LABELS.get(name, name),
                    "duration_ms": duration_ms,
                    "results": result.results,
                    "attempted": attempts,
                    "skipped": skipped,
                    "degraded": bool(skipped) or len(attempts) > 1,
                    "effective_backend": name,
                    "failure_reason": "",
                }

        duration_ms = int((loop.time() - start) * 1000)
        failure_reason = "no_results"
        if attempts:
            failed = [
                item for item in attempts
                if item["status"] not in ("success",)
            ]
            if failed:
                failure_reason = failed[-1]["reason"] or failed[-1]["status"]
        elif skipped:
            failure_reason = skipped[-1]["reason"] or skipped[-1]["status"]
        else:
            failure_reason = "no_backend"

        return {
            "status": "failed",
            "summary": "",
            "provider": "失败",
            "duration_ms": duration_ms,
            "results": [],
            "attempted": attempts,
            "skipped": skipped,
            "degraded": bool(skipped) or len(attempts) > 1,
            "effective_backend": "",
            "failure_reason": failure_reason,
        }

    async def test_search(self, game_name: str, event=None) -> dict:
        result = await self.search_game_info(game_name, event=event)
        message = format_search_diagnostic(result)
        return {
            "success": result["status"] == "success" and bool(result["results"]),
            "message": message,
            "summary": result["summary"][:500],
            "backend": result.get("effective_backend", ""),
            "degraded": result.get("degraded", False),
            "result_count": len(result.get("results", [])),
            "failure_reason": result.get("failure_reason", ""),
            "attempted": result.get("attempted", []),
            "skipped": result.get("skipped", []),
        }

    async def close(self) -> None:
        for backend in self._backends.values():
            try:
                await backend.close()
            except Exception:
                pass


_REASON_TEXT = {
    "disabled": "未启用",
    "missing_api_key": "未配置 API Key",
    "api_provider_unverified": "API 提供商未验证",
    "invalid_base_url": "API 地址无效",
    "astrbot_context_missing": "缺少 AstrBot 会话上下文",
    "astrbot_version_unverified": "AstrBot 版本未验证",
    "astrbot_contract_unavailable": "AstrBot 搜索契约不可用",
    "astrbot_config_unavailable": "AstrBot 配置不可读",
    "astrbot_search_disabled": "AstrBot 搜索未启用",
    "astrbot_provider_unverified": "AstrBot 搜索提供商未验证",
    "astrbot_key_missing": "AstrBot 未配置密钥",
    "astrbot_search_error": "AstrBot 搜索失败",
    "verification_page": "验证页",
    "empty_results": "无有效结果",
    "no_results": "无有效结果",
    "timeout": "超时",
    "budget_exhausted": "总时间预算耗尽",
    "http_401": "API Key 无效",
    "http_403": "API 访问被拒绝",
    "http_429": "API 限流",
    "http_5xx": "API 服务异常",
    "http_error": "API HTTP 错误",
    "network_error": "网络错误",
    "invalid_response": "响应格式异常",
    "backend_error": "后端异常",
    "backend_unavailable": "后端不可用",
    "no_backend": "没有可用后端",
}


def format_search_diagnostic(result: dict) -> str:
    """管理员诊断文本：实际后端、降级、有效结果数、失败原因。"""
    status = result.get("status", "failed")
    if status == "disabled":
        return "搜索未启用（off，后端：无，有效结果：0，降级：否）"

    if status == "success":
        backend = BACKEND_LABELS.get(
            result.get("effective_backend", ""),
            result.get("provider", ""),
        )
        count = len(result.get("results", []))
        degraded = "是" if result.get("degraded") else "否"
        return f"搜索成功（后端：{backend}，有效结果：{count}，降级：{degraded}）"

    reason = _REASON_TEXT.get(
        result.get("failure_reason", ""),
        result.get("failure_reason", "未知原因"),
    )
    details = []
    for item in result.get("attempted", []):
        label = BACKEND_LABELS.get(item.get("backend", ""), item.get("backend", ""))
        item_reason = _REASON_TEXT.get(item.get("reason", ""), item.get("reason", ""))
        details.append(f"{label}（{item_reason or item.get('status', '')}）")
    skipped_details = []
    for item in result.get("skipped", []):
        label = BACKEND_LABELS.get(item.get("backend", ""), item.get("backend", ""))
        item_reason = _REASON_TEXT.get(item.get("reason", ""), item.get("reason", ""))
        skipped_details.append(
            f"{label}（{item_reason or item.get('status', '')}）"
        )
    suffix = f"；尝试：{' → '.join(details)}" if details else ""
    if skipped_details:
        suffix += f"；跳过：{' → '.join(skipped_details)}"
    degraded = "是" if result.get("degraded") else "否"
    return (
        f"搜索失败（后端：无，有效结果：0，降级：{degraded}；"
        f"原因：{reason}{suffix}）"
    )
