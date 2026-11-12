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
import ipaddress
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
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 2048
MAX_SNIPPET_CHARS = 1000
DEFAULT_SUMMARY_INPUT_MAX_CHARS = 6000
BACKEND_LABELS = {
    "astrbot": "AstrBot",
    "api": "Tavily API",
    "web": "网页搜索",
}
SOURCE_LABELS = {
    "bing": "必应",
    "baidu": "百度",
    "tavily": "Tavily",
    "astrbot": "AstrBot",
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


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip().lower()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    title = _truncate_text(str(raw.get("title") or ""), MAX_TITLE_CHARS)
    url = str(raw.get("url") or "").strip()
    snippet = _truncate_text(
        str(raw.get("snippet") or raw.get("content") or ""),
        MAX_SNIPPET_CHARS,
    )
    source = str(raw.get("source") or default_source or "").strip()[:64]
    if not title or len(url) > MAX_URL_CHARS or not _is_http_url(url):
        return None
    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "source": source,
    }


def build_search_summary(
    results: list[dict],
    max_total_chars: int = DEFAULT_SUMMARY_INPUT_MAX_CHARS,
) -> str:
    """把统一结果拼成供 AI 汇总/生成参考的文本。

    预算规则（确定、可测试）：
    1. 先为每条结果保留可识别的标题和来源，所有条目均分标题预算；
    2. 剩余预算优先均分给摘要；
    3. 仍有空间时才追加完整 URL，绝不放宽为“截断后的假 URL”；
    4. 最终长度严格不超过总预算。

    总预算小到连分隔符/每条一个字符都放不下时，只保留第一条标题，
    不编造其他字段。
    """
    items = []
    for item in results:
        title = _truncate_text(str(item.get("title") or ""), MAX_TITLE_CHARS)
        if not title:
            continue
        url = str(item.get("url") or "").strip()
        if len(url) > MAX_URL_CHARS or not _is_http_url(url):
            url = ""  # URL 可省略，但不能截断成看似完整的错误 URL
        snippet = _truncate_text(
            str(item.get("snippet") or ""), MAX_SNIPPET_CHARS
        )
        source = str(item.get("source") or "").strip()[:64]
        items.append((source, title, snippet, url))

    if not items:
        return ""

    try:
        total = int(max_total_chars)
    except (TypeError, ValueError):
        total = DEFAULT_SUMMARY_INPUT_MAX_CHARS
    total = max(1, total)

    count = len(items)
    separators = 2 * (count - 1)
    if total <= separators:
        return _truncate_text(items[0][1], total)

    available = total - separators
    if available < count:
        return _truncate_text(items[0][1], total)

    # 有摘要时为摘要预留约 30% 的总预算，避免五条长标题把摘要空间
    # 全部吃掉；标题仍均分剩余预算，保证每条结果可识别。
    has_snippets = any(item[2] for item in items)
    snippet_reserve = 0
    if has_snippets:
        snippet_reserve = min(
            available // 2, max(20, int(available * 0.3))
        )
    header_available = max(count, available - snippet_reserve)
    header_share = max(1, header_available // count)
    headers = []
    for source, title, _snippet, _url in items:
        header = f"[{source}] {title}" if source else title
        headers.append(_truncate_text(header, header_share))

    blocks = list(headers)
    used = sum(len(block) for block in blocks)
    remaining = max(0, available - used)

    # 摘要优先于 URL；为每条摘要的换行符预留空间后均分。
    snippet_indices = [
        index for index, item in enumerate(items) if item[2]
    ]
    if snippet_indices and remaining > len(snippet_indices):
        text_budget = remaining - len(snippet_indices)
        per_snippet = text_budget // len(snippet_indices)
        extra = text_budget - per_snippet * len(snippet_indices)
        for index in snippet_indices:
            allowance = per_snippet + (1 if extra > 0 else 0)
            if extra > 0:
                extra -= 1
            if allowance <= 0:
                continue
            snippet = _truncate_text(items[index][2], allowance)
            if not snippet:
                continue
            blocks[index] += f"\n{snippet}"
            used += 1 + len(snippet)

    # URL 只在完整放得下时追加，避免截断 URL 造成错误链接。
    for index, (_source, _title, _snippet, url) in enumerate(items):
        if not url:
            continue
        addition = 1 + len(url)
        if used + addition <= available:
            blocks[index] += f"\n{url}"
            used += addition

    return "\n\n".join(blocks)


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
    partial: bool = False
    failures: list[dict] = field(default_factory=list)
    cleanup_incomplete: bool = False


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
    Context.get_llm_tool_manager() -> FunctionToolManager
    manager.is_builtin_tool("web_search_tavily")
    manager.get_builtin_tool("web_search_tavily").call(
        ContextWrapper[AstrAgentContext], query=..., max_results=...
    )

    只有工具已注册、active=True 且当前会话 provider_settings 允许时才可用；
    不直接实例化工具类，避免绕过停用状态。AstrBot 的 Tavily 实现会在多个
    Key 之间对 401/403/429/432 做故障转移重试，因此本项目仅在恰好一个
    Tavily Key 时启用该后端，避免自动重复计费请求。
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
        except Exception:
            return False, "astrbot_contract_unavailable"

        try:
            tool_mgr = self._context.get_llm_tool_manager()
        except Exception:
            return False, "astrbot_tool_manager_unavailable"
        try:
            registered = bool(tool_mgr.is_builtin_tool("web_search_tavily"))
        except Exception:
            return False, "astrbot_tool_manager_unavailable"
        if not registered:
            return False, "astrbot_tool_unregistered"
        try:
            tool = tool_mgr.get_builtin_tool("web_search_tavily")
        except Exception:
            return False, "astrbot_tool_unregistered"
        if tool is None:
            return False, "astrbot_tool_unregistered"
        if not bool(getattr(tool, "active", True)):
            return False, "astrbot_tool_disabled"

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
        else:
            keys = list(keys)
        if not keys:
            return False, "astrbot_key_missing"
        if len(keys) > 1:
            # AstrBot 内置实现会按 Key 轮换重试可重试状态码。
            return False, "astrbot_retry_risk"
        if not isinstance(keys[0], str) or not keys[0].strip():
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

    def _managed_tool(self):
        tool_mgr = self._context.get_llm_tool_manager()
        if not tool_mgr.is_builtin_tool("web_search_tavily"):
            raise RuntimeError("web_search_tavily is not registered")
        tool = tool_mgr.get_builtin_tool("web_search_tavily")
        if tool is None:
            raise RuntimeError("web_search_tavily is unavailable")
        if not bool(getattr(tool, "active", True)):
            raise RuntimeError("web_search_tavily is disabled")
        return tool

    async def _call_builtin(self, query: str, max_results: int, event):
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.astr_agent_context import AstrAgentContext

        wrapper = ContextWrapper(
            AstrAgentContext(context=self._context, event=event)
        )
        tool = self._managed_tool()
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

    def _base_url_allowed(self) -> bool:
        """携带 Key 时默认必须 HTTPS；仅允许回环 HTTP 供本地模拟。"""
        parsed = urlparse(self._base_url)
        if parsed.scheme == "https":
            return bool(parsed.hostname)
        if parsed.scheme == "http":
            return _is_loopback_host(parsed.hostname)
        return False

    def _is_loopback_http(self) -> bool:
        parsed = urlparse(self._base_url)
        return (
            parsed.scheme == "http"
            and _is_loopback_host(parsed.hostname)
        )

    async def availability(self, event=None) -> tuple[bool, str]:
        if self._provider != "tavily":
            return False, "api_provider_unverified"
        if not self._api_key:
            return False, "missing_api_key"
        if not _is_http_url(self._base_url):
            return False, "invalid_base_url"
        if not self._base_url_allowed():
            return False, "insecure_api_base_url"
        return True, ""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if self._is_loopback_http():
                # 回环 HTTP 只用于本机测试；禁用 trust_env，确保
                # HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 不会把明文 Bearer
                # Key 转发给远端代理。HTTPS 正式服务仍保留系统代理。
                self._client = httpx.AsyncClient(
                    timeout=None, trust_env=False
                )
            else:
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
        if not self._base_url_allowed():
            return self._failure("failed", "insecure_api_base_url", start)
        try:
            client = await self._get_client()
        except Exception:
            # 系统代理等环境配置异常时，明确失败，不静默关闭 HTTPS 代理。
            return self._failure("failed", "client_init_error", start)
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
        except httpx.InvalidURL:
            return self._failure("failed", "client_init_error", start)
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


class _BingResultParser(HTMLParser):
    """单次解析 b_algo 容器：标题、URL、摘要一次收集。

    HTMLParser(convert_charrefs=True) 只解码一次实体；收集的是文本 data，
    不再重建 HTML 后二次解析，也不对其调用 _strip_html/unescape，避免
    &lt; / &gt; / &amp;lt; 被删除或重复解码。摘要只接受结果容器顶层 <p>，
    嵌套列表/普通 li/相邻结果不会混入。
    """

    def __init__(self, container_tag: str):
        super().__init__(convert_charrefs=True)
        self.container_tag = container_tag
        self.depth = 0
        self.result_depth: int | None = None
        self.results: list[dict] = []
        self._reset_result()

    def _reset_result(self):
        self.in_title = False
        self.title_seen = False
        self.title_parts: list[str] = []
        self.url = ""
        self.h2_depth = 0
        self.list_depth = 0
        self.in_snippet = False
        self.snippet_parts: list[str] = []
        self.snippet = ""
        self.snippet_found = False

    @staticmethod
    def _is_result_start(attrs) -> bool:
        classes = dict(attrs).get("class") or ""
        return "b_algo" in classes.split()

    def _finish_title(self):
        if self.in_title and "".join(self.title_parts).strip():
            self.title_seen = True
        self.in_title = False

    def _finish_snippet(self):
        if not self.in_snippet:
            return
        text = "".join(self.snippet_parts).strip()
        if text and not self.snippet_found:
            self.snippet = text
            self.snippet_found = True
        self.in_snippet = False
        self.snippet_parts = []

    def _finish_result(self):
        self._finish_title()
        self._finish_snippet()
        title = "".join(self.title_parts).strip()
        if title:
            self.results.append({
                "title": title,
                "url": self.url,
                "snippet": self.snippet,
            })

    def handle_starttag(self, tag, attrs):
        if tag == self.container_tag:
            self.depth += 1
            if (
                self.result_depth is None
                and self._is_result_start(attrs)
            ):
                self.result_depth = self.depth
                self._reset_result()
            return

        if self.result_depth is None:
            return
        if tag in ("ul", "ol"):
            if self.in_snippet:
                self._finish_snippet()
            self.list_depth += 1
            return
        if tag == "h2":
            self.h2_depth += 1
            return
        if tag == "br" and self.in_snippet:
            self.snippet_parts.append(" ")
            return
        if tag == "a" and self.h2_depth > 0 and not self.in_title:
            href = dict(attrs).get("href") or ""
            self.in_title = True
            self.title_parts = []
            if href:
                self.url = urljoin(
                    "https://www.bing.com", str(href)
                )
            return
        if (
            tag == "p"
            and self.title_seen
            and self.list_depth == 0
            and not self.in_snippet
            and not self.snippet_found
        ):
            self.in_snippet = True
            self.snippet_parts = []

    def handle_startendtag(self, tag, attrs):
        if self.in_snippet and tag == "br":
            self.snippet_parts.append(" ")

    def handle_endtag(self, tag):
        if tag == self.container_tag:
            if (
                self.result_depth is not None
                and self.depth == self.result_depth
            ):
                self._finish_result()
                self.result_depth = None
            self.depth = max(0, self.depth - 1)
            return

        if self.result_depth is None:
            return
        if tag in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
            return
        if tag == "a" and self.in_title:
            self._finish_title()
            return
        if tag == "h2":
            self.h2_depth = max(0, self.h2_depth - 1)
            return
        if tag == "p" and self.in_snippet:
            self._finish_snippet()

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.in_snippet:
            self.snippet_parts.append(data)


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
        self._cleanup_tasks: set[asyncio.Task] = set()

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

    @staticmethod
    async def _cancel_tasks(tasks) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _source_deadlines(timeout_s: float | None) -> tuple[
        float | None, float
    ]:
        """返回来源等待截止时间和取消后的有界清理预算。

        来源截止时间提前于后端总预算，给被取消来源的 finally/异步清理
        留出空间，避免外层总超时再次取消网页后端、丢掉已拿到的结果。
        清理预算有上限；若清理仍超时，会记录 cleanup_incomplete 并
        由 close() 继续收尾，属于明确报告的限制。
        """
        if timeout_s is None:
            return None, 0.0
        cleanup = min(0.06, max(0.005, timeout_s * 0.6))
        margin = min(0.01, max(0.001, timeout_s * 0.05))
        source = timeout_s - cleanup - margin
        if source < 0.005:
            source = max(0.001, timeout_s * 0.2)
            cleanup = max(0.001, timeout_s - source - margin)
        return max(0.001, source), max(0.001, cleanup)

    @classmethod
    def _is_verification_page(cls, html: str) -> bool:
        # 仅在“没有解析出任何有效结果”时调用。正常结果页即使摘要里出现
        # “验证码”等词，也已在解析阶段直接按结果页处理，不会走到这里。
        text = _extract_text_from_html(html)
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE
        )
        title = _strip_html(title_match.group(1)).lower() if title_match else ""
        if title and any(
            keyword.lower() in title for keyword in cls.VERIFICATION_KEYWORDS
        ):
            return True
        if not text:
            return False
        # 大页面里的“验证码”更可能来自正常摘要或页脚，避免整页误判。
        if len(text) > 1200:
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
        # 来源截止提前于后端预算，给取消后的 finally/异步清理留空间；
        # 清理预算有界，避免外层总超时再次取消整个网页后端。
        source_timeout, cleanup_budget = self._source_deadlines(timeout_s)
        labeled_tasks = [
            ("bing", asyncio.create_task(
                self._search_bing(client, query, max_results, source_timeout)
            )),
            ("baidu", asyncio.create_task(
                self._search_baidu(client, query, max_results, source_timeout)
            )),
        ]
        labels = {task: label for label, task in labeled_tasks}
        tasks = list(labels)
        try:
            done, pending = await asyncio.wait(tasks, timeout=source_timeout)
        except asyncio.CancelledError:
            await self._cancel_tasks(tasks)
            raise

        cleanup_incomplete = False
        if pending:
            pending_list = list(pending)
            for task in pending_list:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending_list, return_exceptions=True),
                    timeout=cleanup_budget,
                )
            except asyncio.TimeoutError:
                cleanup_incomplete = True
                self._cleanup_tasks.update(
                    task for task in pending_list if not task.done()
                )
                logger.warning(
                    "[小作文生成器] 网页来源取消清理超时: %s",
                    ", ".join(labels[task] for task in pending_list),
                )
                for task in pending_list:
                    if not task.done():
                        task.cancel()
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                await self._cancel_tasks(tasks)
                raise

        source_results = []
        for task in tasks:
            label = labels[task]
            if task in pending:
                source_results.append((label, "timeout", [], "timeout"))
                continue
            if task.cancelled():
                source_results.append((label, "failed", [], "backend_error"))
                continue
            exc = task.exception()
            if exc is not None:
                source_results.append((label, "failed", [], "backend_error"))
                continue
            source_results.append(task.result())

        merged: list[dict] = []
        seen: set[str] = set()
        failures: list[dict] = []
        for label, status, items, reason in source_results:
            if status == "success":
                for item in items:
                    key = item["url"].strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(item)
            else:
                failures.append({
                    "source": label,
                    "status": status,
                    "reason": reason or status,
                })
        merged = merged[:max_results]
        duration_ms = int((time.monotonic() - start) * 1000)

        if merged:
            reason = "; ".join(
                f"{item['source']}:{item['reason']}" for item in failures
            )
            return BackendResult(
                self.name,
                "success",
                merged,
                reason,
                duration_ms,
                partial=bool(failures),
                failures=failures,
                cleanup_incomplete=cleanup_incomplete,
            )

        if failures:
            reason = failures[0]["reason"] or "failed"
            if any(item["reason"] == "verification_page" for item in failures):
                reason = "verification_page"
            else:
                meaningful = [
                    item["reason"] for item in failures
                    if item["reason"] not in ("empty_results", "no_results")
                ]
                if meaningful:
                    reason = meaningful[0]
                elif all(
                    item["reason"] in ("empty_results", "no_results")
                    for item in failures
                ):
                    reason = "empty_results"
            return BackendResult(
                self.name,
                "failed",
                [],
                reason,
                duration_ms,
                partial=False,
                failures=failures,
                cleanup_incomplete=cleanup_incomplete,
            )
        return BackendResult(
            self.name,
            "no_results",
            [],
            "empty_results",
            duration_ms,
            cleanup_incomplete=cleanup_incomplete,
        )

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

        items = self._parse_bing(html)[:max_results]
        if items:
            return ("bing", "success", items, "")
        if self._is_verification_page(html):
            return ("bing", "failed", [], "verification_page")
        return ("bing", "no_results", [], "empty_results")

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

        items = self._parse_baidu(html)[:max_results]
        if items:
            return ("baidu", "success", items, "")
        if self._is_verification_page(html):
            return ("baidu", "failed", [], "verification_page")
        return ("baidu", "no_results", [], "empty_results")

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
        # 只解析 b_algo 容器；单次 HTMLParser 直接收集文本，避免实体
        # 解码后重建 HTML 再解析导致的删除/重复解码。
        results = []
        for container_tag in ("li", "div"):
            parser = _BingResultParser(container_tag)
            try:
                parser.feed(html)
                parser.close()
            except Exception:
                results = parser.results
                break
            if parser.results:
                results = parser.results
                break
        items = []
        for result in results:
            item = _normalize_item({
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("snippet", ""),
                "source": "bing",
            })
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
        if self._cleanup_tasks:
            pending = list(self._cleanup_tasks)
            self._cleanup_tasks.clear()
            await self._cancel_tasks(pending)
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


class SearchService:
    """多后端搜索编排器。"""

    def __init__(
        self,
        enabled: bool = True,
        timeout_ms: int = 8000,
        per_backend_timeout_ms: int = 0,
        summary_input_max_chars: int = DEFAULT_SUMMARY_INPUT_MAX_CHARS,
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
            per_backend_timeout_ms = int(per_backend_timeout_ms)
        except (TypeError, ValueError):
            per_backend_timeout_ms = 0
        self.per_backend_timeout = max(0.0, per_backend_timeout_ms / 1000.0)
        try:
            summary_input_max_chars = int(summary_input_max_chars)
        except (TypeError, ValueError):
            summary_input_max_chars = DEFAULT_SUMMARY_INPUT_MAX_CHARS
        self.summary_input_max_chars = max(200, summary_input_max_chars)
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

    def _attempt_timeout(
        self, remaining: float, remaining_candidates: int
    ) -> float:
        """单后端超时上限/分配。

        配置了 per_backend_timeout_ms 时取固定上限；否则把当前剩余预算
        均分给尚未尝试的候选，保证前一个超时后后续仍有预算。
        """
        if remaining <= 0:
            return 0.0
        if self.per_backend_timeout > 0:
            return min(remaining, self.per_backend_timeout)
        if remaining_candidates <= 1:
            return remaining
        return remaining / remaining_candidates

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
            "partial": False,
            "partial_failures": [],
            "cleanup_incomplete": False,
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

        for index, name in enumerate(order):
            remaining = deadline - loop.time()
            attempt_budget = self._attempt_timeout(
                remaining, len(order) - index
            )
            if attempt_budget <= 0:
                skip(name, "timeout", "budget_exhausted")
                break

            backend = self._backends.get(name)
            if backend is None:
                skip(name, "unavailable", "backend_unavailable")
                continue

            backend_deadline = min(
                deadline, loop.time() + attempt_budget
            )
            try:
                availability_timeout = backend_deadline - loop.time()
                if availability_timeout <= 0:
                    skip(name, "timeout", "backend_budget_exhausted")
                    continue
                available, reason = await asyncio.wait_for(
                    backend.availability(event),
                    timeout=availability_timeout,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # 只消耗该后端的分配额度，让后续候选继续使用剩余预算。
                skip(name, "timeout", "backend_budget_exhausted")
                continue
            except Exception:
                available, reason = False, "backend_unavailable"
            if not available:
                skip(name, "unavailable", reason or "backend_unavailable")
                continue

            search_timeout = backend_deadline - loop.time()
            if search_timeout <= 0:
                skip(name, "timeout", "backend_budget_exhausted")
                if deadline <= loop.time():
                    break
                continue

            attempt_start = loop.time()
            try:
                result = await asyncio.wait_for(
                    backend.search(
                        query,
                        self.result_count,
                        event=event,
                        timeout_s=search_timeout,
                    ),
                    timeout=search_timeout,
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
                "partial": bool(result.partial),
                "failures": list(result.failures),
                "cleanup_incomplete": bool(result.cleanup_incomplete),
            })

            if result.status == "success" and result.results:
                duration_ms = int((loop.time() - start) * 1000)
                partial = bool(result.partial or result.failures)
                return {
                    "status": "success",
                    "summary": build_search_summary(
                        result.results, self.summary_input_max_chars
                    ),
                    "provider": BACKEND_LABELS.get(name, name),
                    "duration_ms": duration_ms,
                    "results": result.results,
                    "attempted": attempts,
                    "skipped": skipped,
                    "degraded": bool(skipped) or len(attempts) > 1 or partial,
                    "partial": partial,
                    "partial_failures": list(result.failures),
                    "cleanup_incomplete": bool(result.cleanup_incomplete),
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
            "partial": False,
            "partial_failures": [],
            "cleanup_incomplete": any(
                item.get("cleanup_incomplete") for item in attempts
            ),
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
            "partial": result.get("partial", False),
            "partial_failures": result.get("partial_failures", []),
            "cleanup_incomplete": result.get("cleanup_incomplete", False),
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
    "astrbot_tool_manager_unavailable": "AstrBot 工具管理器不可用",
    "astrbot_tool_unregistered": "AstrBot 搜索工具未注册",
    "astrbot_tool_disabled": "AstrBot 搜索工具已停用",
    "astrbot_retry_risk": "AstrBot 多 Key 自动重试风险",
    "insecure_api_base_url": "API 地址必须使用 HTTPS（回环地址除外）",
    "client_init_error": "搜索客户端初始化失败",
    "verification_page": "验证页",
    "empty_results": "无有效结果",
    "no_results": "无有效结果",
    "timeout": "超时",
    "budget_exhausted": "总时间预算耗尽",
    "backend_budget_exhausted": "单后端时间预算耗尽",
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


def format_source_failures(failures: list[dict]) -> str:
    parts = []
    for item in failures or []:
        source = SOURCE_LABELS.get(
            item.get("source", ""), item.get("source", "")
        )
        reason = _REASON_TEXT.get(
            item.get("reason", ""), item.get("reason", item.get("status", ""))
        )
        parts.append(f"{source}（{reason}）")
    return "、".join(parts)


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
        partial = "是" if result.get("partial") else "否"
        message = (
            f"搜索成功（后端：{backend}，有效结果：{count}，"
            f"降级：{degraded}，部分可用：{partial}"
        )
        if result.get("partial"):
            failures_text = format_source_failures(
                result.get("partial_failures", [])
            )
            if failures_text:
                message += f"；失败来源：{failures_text}"
        if result.get("cleanup_incomplete"):
            message += "；清理未完成"
        return message + "）"

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
    if result.get("cleanup_incomplete"):
        suffix += "；清理未完成"
    degraded = "是" if result.get("degraded") else "否"
    return (
        f"搜索失败（后端：无，有效结果：0，降级：{degraded}；"
        f"原因：{reason}{suffix}）"
    )
