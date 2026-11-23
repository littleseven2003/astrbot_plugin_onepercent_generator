"""
多后端联网搜索服务。

后端：
- astrbot：AstrBot 内置 web_search_tavily 工具（版本仅诊断；验证上下文/工具/调用/配置能力）
- anysearch：AstrBot 中真实注册的 Anysearch 插件 anysearch_search 工具（显式适配）
- api：Tavily 结构化搜索 API（basic，1 credit/次，不自动重试）
- web：Bing + 百度搜索页解析（尽力而为，不抓取结果链接正文）
- off：关闭

统一使用一个总时间预算；后端不得各自累加超时。
"""

import asyncio
import html as html_lib
import importlib.metadata
import inspect
import ipaddress
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

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
# 配置中心只暴露三个搜索组；执行顺序完全由保存后的列表从上到下决定。
SEARCH_GROUPS = ("astrbot", "api", "web")
GROUP_BACKENDS = {
    # AstrBot 组内先官方 web_search_tavily，再已安装的 Anysearch 插件。
    "astrbot": ("astrbot", "anysearch"),
    "api": ("api",),
    "web": ("web",),
}
GROUP_LABELS = {
    "astrbot": "Astrbot与搜索插件",
    "api": "搜索API",
    "web": "网页搜索",
}
BACKEND_ORDER = ("astrbot", "anysearch", "api", "web")
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 2048
MAX_SNIPPET_CHARS = 1000
DEFAULT_SUMMARY_INPUT_MAX_CHARS = 6000
BACKEND_LABELS = {
    "astrbot": "AstrBot 官方搜索",
    "anysearch": "Anysearch 插件",
    "api": "Tavily API",
    "web": "网页搜索",
}
SOURCE_LABELS = {
    "bing": "必应",
    "baidu": "百度",
    "tavily": "Tavily",
    "astrbot": "AstrBot",
    "anysearch": "Anysearch",
    "web": "网页搜索",
}
# 已在源码层实测过的版本，仅用于诊断展示；不再作为硬门禁。
KNOWN_ASTRBOT_VERSIONS = frozenset({"4.29.4"})
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


MIN_SUMMARY_TITLE_CHARS = 10
MIN_SUMMARY_TITLE_WITH_SOURCE = 16
MIN_SNIPPET_FACT_CHARS = 12


def _truncate_title(text: str, max_chars: int) -> str:
    """保留标题开头、中部和结尾，尽量覆盖三处区分信息。"""
    text = (text or "").strip()
    try:
        limit = max(1, int(max_chars))
    except (TypeError, ValueError):
        limit = 150
    if len(text) <= limit:
        return text
    if limit <= 8:
        return text[:limit]
    body = limit - 2  # 两个省略号
    head = max(1, body * 2 // 5)
    middle = max(1, body * 3 // 10)
    tail = max(1, body - head - middle)
    if head + middle + tail > body:
        middle = max(1, body - head - tail)
    mid_start = max(0, (len(text) - middle) // 2)
    return (
        text[:head]
        + "…"
        + text[mid_start:mid_start + middle]
        + "…"
        + text[-tail:]
    )


def _summary_header(source: str, title: str, budget: int) -> str:
    """来源标签必须完整；放不下标签时直接省略，不输出半截标签。"""
    budget = max(1, int(budget))
    label = f"[{source}] " if source else ""
    needed_title = min(len(title), MIN_SUMMARY_TITLE_WITH_SOURCE)
    if label and len(label) + needed_title <= budget:
        return label + _truncate_title(title, budget - len(label))
    return _truncate_title(title, budget)


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip().lower()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url_dedupe_key(url: str):
    """只规范化 scheme/hostname；path/query/fragment 保留原始大小写。"""
    try:
        parsed = urlparse(str(url))
        port = parsed.port
    except (TypeError, ValueError):
        return ("raw", str(url))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ("raw", str(url))
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port,
        parsed.path or "/",
        parsed.params,
        parsed.query,
        parsed.fragment,
    )


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _join_api_url(base_url: str, endpoint: str) -> str:
    """拼接 API 路径，并保留 Base URL 已有的查询参数。

    不能直接做字符串拼接：base 为 https://host/api?token=x 时，
    f"{base}/search" 会变成 ...?token=x/search，查询串被破坏且路径错误。
    """
    parsed = urlsplit((base_url or "").strip())
    path = parsed.path.rstrip("/") + "/" + endpoint.lstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


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
    1. 选择条数时同时计算标题和至少一段可理解摘要，空间不足时优先
       输出少量有事实的片段，而不是大量标题/单字符碎片；
    2. 来源标签必须完整，放不下时直接省略，绝不输出半截标签；
    3. 标题保留开头、中部和结尾；标题预算按实际使用量回收；
    4. 摘要预留按实际摘要长度申请；有摘要时至少保住每条可理解的
       事实长度，短摘要不会占用固定空间；
    5. 剩余预算优先均分给摘要；
    6. 仍有空间时才追加完整 URL，绝不放宽为“截断后的假 URL”；
    7. 最终长度严格不超过总预算。

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

    # 以实际标题/来源长度和至少一段可理解摘要决定能保留多少条。
    min_headers = []
    min_snippets = []
    for source, title, snippet, _url in items:
        label = f"[{source}] " if source else ""
        min_headers.append(
            len(label) + min(len(title), MIN_SUMMARY_TITLE_CHARS)
        )
        min_snippets.append(
            1 + min(len(snippet), MIN_SNIPPET_FACT_CHARS)
            if snippet else 0
        )

    selected_count = 1
    for candidate in range(len(items), 0, -1):
        if (
            sum(min_headers[:candidate])
            + sum(min_snippets[:candidate])
            + 2 * (candidate - 1)
            <= total
        ):
            selected_count = candidate
            break
    items = items[:selected_count]
    min_headers = min_headers[:selected_count]
    min_snippets = min_snippets[:selected_count]
    count = len(items)
    separators = 2 * (count - 1)
    available = total - separators

    # 摘要按实际长度申请；有摘要时至少保住每条的“可理解事实”长度。
    snippet_need = sum(
        1 + min(len(item[2]), MAX_SNIPPET_CHARS)
        for item in items
        if item[2]
    )
    min_snippet_need = sum(min_snippets)
    max_reserve = max(0, available - sum(min_headers))
    snippet_reserve = min(
        max_reserve,
        max(
            min_snippet_need,
            min(snippet_need, int(available * 0.4)),
        ),
    )
    header_available = available - snippet_reserve
    if header_available <= sum(min_headers):
        base, remainder = divmod(header_available, count)
        header_budgets = [
            max(1, base + (1 if index < remainder else 0))
            for index in range(count)
        ]
    else:
        extra_header = header_available - sum(min_headers)
        base_extra, remainder = divmod(extra_header, count)
        header_budgets = [
            min_budget + base_extra + (1 if index < remainder else 0)
            for index, min_budget in enumerate(min_headers)
        ]
    headers = [
        _summary_header(source, title, budget)
        for (source, title, _snippet, _url), budget in zip(
            items, header_budgets
        )
    ]

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


SENTENCE_ENDINGS = "。！？!?；;\n"


def _truncate_sentences(text: str, max_chars: int) -> str:
    """按句子边界截断片段，避免半句话进入素材。

    至少在预算 40% 之后才接受句末标点；找不到合适断点时退回硬截断，
    不额外添加省略号或编造内容。
    """
    value = str(text or "").strip()
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, limit)
    if len(value) <= limit:
        return value
    window = value[:limit]
    floor = max(1, int(limit * 0.4))
    cut = -1
    for index in range(len(window) - 1, floor - 1, -1):
        if window[index] in SENTENCE_ENDINGS:
            cut = index + 1
            break
    if cut > 0:
        return window[:cut].strip()
    return window.rstrip()


def build_material_block(
    results: list[dict],
    max_chars: int = 3000,
    max_items: int = 6,
    snippet_chars: int = 400,
    include_link: bool = True,
) -> str:
    """把清洗后的搜索结果按预算拼成可直接入模的素材块。

    每条包含「来源、标题、链接、片段」；片段按句子边界截断。总长度
    不超过 ``max_chars``，条数不超过 ``max_items``，单条片段不超过
    ``snippet_chars``。多个来源/查询方向交替挑选，避免某一路占满素材池。

    ``include_link=False`` 用于链接放不下时的退化输出：仍保留标题与
    片段，且同样遵守字数、条数与单条片段上限，不能借回退突破任何一项。
    """
    items = []
    for item in results or []:
        title = _truncate_text(str(item.get("title") or ""), MAX_TITLE_CHARS)
        url = str(item.get("url") or "").strip()
        if not title or len(url) > MAX_URL_CHARS or not _is_http_url(url):
            continue
        items.append({
            "title": title,
            "url": url,
            "source": str(item.get("source") or "").strip()[:64],
            "snippet": str(item.get("snippet") or "").strip(),
        })
    if not items:
        return ""

    try:
        total = max(200, int(max_chars))
    except (TypeError, ValueError):
        total = 3000
    try:
        limit_items = max(1, min(int(max_items), 20))
    except (TypeError, ValueError):
        limit_items = 6
    try:
        per_snippet = max(80, int(snippet_chars))
    except (TypeError, ValueError):
        per_snippet = 400

    # 按来源轮转排列候选，保证不同查询方向都有机会进入素材池。
    # 注意：这里不先按条数截断——放不下的条目要在写入阶段跳过，
    # 并且只有真正写入的条目才计入条数，否则「首条过长、次条很短」
    # 会直接返回空块，让调用方回退到不受预算约束的旧摘要。
    by_source: dict[str, list[dict]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    ordered: list[dict] = []
    queues = list(by_source.values())
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    if not ordered:
        return ""

    def header(item: dict) -> str:
        label = f"[{item['source']}] " if item["source"] else ""
        return f"{label}{item['title']}"

    # 逐条写入并在写入前校验总预算：单条长链接放不下就跳过它，
    # 继续保留后续能装入预算的素材，最终长度严格不超过总预算。
    blocks: list[str] = []
    used = 0
    for item in ordered:
        if len(blocks) >= limit_items:
            break
        head = header(item)
        base = head
        if include_link:
            base += "\n" + f"链接：{item['url']}"
        separator = 2 if blocks else 0
        if used + separator + len(base) > total:
            continue
        remaining = total - used - separator - len(base)
        snippet = ""
        if item["snippet"] and remaining > len("\n片段："):
            allowance_local = min(per_snippet, remaining - len("\n片段："))
            snippet = _truncate_sentences(item["snippet"], allowance_local)
        block = base
        if snippet:
            block += "\n" + f"片段：{snippet}"
        if used + separator + len(block) > total:
            continue
        blocks.append(block)
        used += separator + len(block)
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


def game_search_queries(game_name: str) -> list[str]:
    """网页组的两个查询方向：平台/玩法机制 与 体验评价/剧情。

    只搜游戏名往往只拿到基础介绍；分开查询后，素材池里同时有客观
    平台机制信息和玩家体验/剧情信息，交给生成模型直接使用。
    """
    name = str(game_name or "").strip()
    if not name:
        return []
    return [
        f"{name} 首发平台 玩法机制",
        f"{name} 体验评价 剧情",
    ]


def search_enabled(value) -> bool:
    """规范化总开关，兼容旧配置写入的字符串 "false" 等值。

    宿主 schema 声明 bool，但旧版本/原生配置页可能留下字符串，不能把
    非空字符串一律当成 True 而继续联网。
    """
    if isinstance(value, str):
        return value.strip().lower() not in {
            "", "false", "0", "no", "off", "disabled",
        }
    return bool(value)


def resolve_search_mode(enabled=True, mode=None) -> str:
    """解析模式并迁移旧 enabled=false。

    旧配置 enabled=false 必须保持不联网；此时 off 优先于 mode。
    """
    enabled = search_enabled(enabled)
    if not enabled:
        return "off"

    normalized = str(mode or "").strip().lower()
    if not normalized:
        return "auto"
    if normalized not in MODES:
        return "auto"
    return normalized


def resolve_search_groups(
    enabled=True,
    mode=None,
    fallback_enabled=False,
    groups=None,
) -> list[str]:
    """解析实际要尝试的搜索组，按保存后的列表从上到下。

    新配置直接给有序的组列表（已启用的组）；没有列表时按旧 mode 与
    fallback 推导，兼容旧调用方（单测、旧配置在迁移前被直接构造）。
    """
    if groups is not None:
        result: list[str] = []
        for item in groups:
            key = str(item).strip().lower()
            if key in SEARCH_GROUPS and key not in result:
                result.append(key)
        if not enabled:
            return []
        return result

    legacy = resolve_search_mode(enabled, mode)
    if legacy == "off":
        return []
    if legacy == "auto":
        return list(SEARCH_GROUPS)
    if legacy == "astrbot":
        return ["astrbot", "api", "web"] if fallback_enabled else ["astrbot"]
    if legacy == "api":
        return ["api", "web"] if fallback_enabled else ["api"]
    if legacy == "web":
        return ["web"]
    return []


def search_groups_from_config(search_cfg: dict) -> list[str]:
    """从已保存配置读取有序的启用组列表（运行时唯一依据）。

    保存端会写 mode_order（三组全序）与 mode_enabled（勾选子集）。
    配置没有显式列表时（旧配置尚未迁移）才用 mode/fallback 推导。
    """
    if not isinstance(search_cfg, dict):
        search_cfg = {}
    if not search_enabled(search_cfg.get("enabled", True)):
        return []
    raw_order = search_cfg.get("mode_order")
    raw_enabled = search_cfg.get("mode_enabled")
    has_list = isinstance(raw_order, list) and bool(raw_order)
    if not has_list:
        return resolve_search_groups(
            enabled=search_cfg.get("enabled", True),
            mode=search_cfg.get("mode"),
            fallback_enabled=search_cfg.get("fallback_enabled", False),
        )
    enabled = set()
    if isinstance(raw_enabled, (list, tuple, set)):
        enabled = {
            str(item).strip().lower() for item in raw_enabled
        }
    ordered: list[str] = []
    for item in raw_order:
        key = str(item).strip().lower()
        if (
            key in SEARCH_GROUPS
            and key in enabled
            and key not in ordered
        ):
            ordered.append(key)
    return ordered


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

    安全能力发现：
    - 版本号仅用于诊断，不是硬门禁；
    - 检查 Context 读配置/取工具管理器、管理器注册/获取内置工具；
    - 检查工具 call(context, query=..., max_results=...) 的签名契约；
    - 检查当前会话 provider_settings 与单 Key 限制。

    只有工具已注册、active=True、调用契约可验证且配置匹配时才可用。
    不直接实例化工具类，避免绕过停用状态。AstrBot 的 Tavily 实现会在多个
    Key 之间对 401/403/429/432 做故障转移重试，因此本项目仅在恰好一个
    Tavily Key 时启用该后端，避免自动重复计费请求。
    """

    name = "astrbot"

    def __init__(self, context=None, adapter=None):
        self._context = context
        self._adapter = adapter
        self._last_version = self._detect_version()

    @staticmethod
    def _detect_version() -> str:
        """读取 AstrBot 版本；无法读取时返回空字符串而不是伪造版本。"""
        try:
            import astrbot  # type: ignore
            version = str(getattr(astrbot, "__version__", "") or "")
            if version:
                return version
        except Exception:
            pass
        try:
            return str(importlib.metadata.version("astrbot"))
        except Exception:
            return ""

    @staticmethod
    def _call_contract_ok(tool) -> bool:
        """只接受带 context 位置参数且支持 query/max_results 的 call。"""
        call = getattr(tool, "call", None)
        if not callable(call) or not inspect.iscoroutinefunction(call):
            return False
        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):
            return False
        parameters = signature.parameters
        positional = [
            parameter for parameter in parameters.values()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        var_positional = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters.values()
        )
        has_context = (
            bool(positional)
            and positional[0].name in {"context", "ctx"}
        ) or var_positional
        var_keyword = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        has_query = "query" in parameters or var_keyword
        has_max_results = "max_results" in parameters or var_keyword
        return has_context and has_query and has_max_results

    async def availability(self, event=None) -> tuple[bool, str]:
        if self._adapter is not None:
            return True, ""
        if self._context is None:
            return False, "astrbot_context_missing"
        umo = getattr(event, "unified_msg_origin", None) if event else None

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
        if not self._call_contract_ok(tool):
            return False, "astrbot_call_contract_unverified"
        if not bool(getattr(tool, "active", True)):
            return False, "astrbot_tool_disabled"

        try:
            cfg = (
                self._context.get_config(umo=umo)
                if umo else self._context.get_config()
            )
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

        if not isinstance(payload, (str, dict)):
            return BackendResult(
                self.name, "failed", [], "invalid_response",
                int((time.monotonic() - start) * 1000),
            )
        if isinstance(payload, str):
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
        else:
            data = payload
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

# AnySearch 真实返回是分节 Markdown：标题行 + “- **URL**: …” + 摘要行，
# 不是 JSON，也不是 [标题](链接) 形式。下面两个表达式只做结构识别，
# 具体清洗交给 _normalize_item，避免把半截链接当成有效结果。
_ANYSEARCH_HEADING_RE = re.compile(
    r"^\s{0,3}(?:"
    r"#{1,6}\s*(?P<hash>.+?)\s*#*"
    r"|\*\*\s*(?P<bold>\d{1,3}\s*[.、)）]\s*.+?)\s*\*\*"
    r"|(?P<num>\d{1,3}\s*[.、)）]\s*.+?)"
    r"|\*\*(?P<purebold>.+?)\*\*"
    r")\s*$"
)
_ANYSEARCH_URL_RE = re.compile(
    r"^\s*(?:[-*+•]\s*)?(?:"
    r"\*\*\s*(?:URL|Url|url|链接|网址|Link)\s*\*\*"
    r"|(?:URL|Url|url|链接|网址|Link)"
    r")\s*[:：]\s*(?:\*\*)?\s*(?P<url>https?://[^\s)>\]]+)",
)
_ANYSEARCH_INLINE_URL_RE = re.compile(
    r"^\s*(?:[-*+•]\s*)?(?:\*\*)?\s*(?P<url>https?://[^\s)>\]]+)\s*$"
)
_ANYSEARCH_LABEL_RE = re.compile(
    r"^\s*(?:[-*+•]\s*)?(?:\*\*)?\s*"
    r"(?:摘要|简介|内容|说明|描述|正文|Summary|Snippet)\s*(?:\*\*)?\s*[:：]\s*"
    r"(?P<text>.*)$",
    re.IGNORECASE,
)


def _strip_markdown_inline(text: str) -> str:
    """去掉行内的加粗/斜体/代码标记，保留文字本身。"""
    value = str(text or "")
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)
    value = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"^\s*[-*+•]\s*", "", value)
    return value.strip()


def _anysearch_section_results(text: str) -> list[dict]:
    """解析 AnySearch 的分节 Markdown（标题 + URL 行 + 摘要行）。

    真实返回形如::

        ## 星露谷物语 玩法
        - **URL**: https://example.com/a
        星露谷物语是一款……

    也兼容 ``1. 标题`` / ``**标题**`` 等标题写法、裸 URL 行，以及
    摘要带 ``摘要：``/``内容：`` 标记的情况。只有同时拿到标题和
    http(s) 链接的分节才会产出结果；没有 URL 的文本不当作结果。
    """
    results: list[dict] = []
    seen_urls: set[str] = set()
    current: dict | None = None

    def flush():
        nonlocal current
        if current is None:
            return
        url = current["url"].strip()
        if url:
            snippet = "\n".join(
                line for line in current["lines"] if line.strip()
            ).strip()
            title = current["title"].strip()
            if not title:
                title = (snippet[:60] or url).strip()
            key = _url_dedupe_key(url)
            if key not in seen_urls:
                item = _normalize_item({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "source": "anysearch",
                })
                if item:
                    seen_urls.add(key)
                    results.append(item)
        current = None

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _ANYSEARCH_HEADING_RE.match(line)
        if heading and (heading.group("hash") or heading.group("bold")
                        or heading.group("num") or heading.group("purebold")):
            flush()
            title = next(
                group for group in (
                    heading.group("hash"),
                    heading.group("bold"),
                    heading.group("num"),
                    heading.group("purebold"),
                )
                if group
            )
            title = re.sub(r"^\d{1,3}\s*[.、)）]\s*", "", title)
            current = {
                "title": _strip_markdown_inline(title)[:MAX_TITLE_CHARS],
                "url": "",
                "lines": [],
            }
            continue
        url_match = _ANYSEARCH_URL_RE.match(line)
        if url_match:
            if current is None:
                current = {"title": "", "url": "", "lines": []}
            if current["url"]:
                flush()
                current = {"title": "", "url": "", "lines": []}
            current["url"] = url_match.group("url")
            rest = line[url_match.end():]
            rest = rest.strip(" \t-*:：")
            if rest:
                current["lines"].append(_strip_markdown_inline(rest))
            continue
        bare_url = _ANYSEARCH_INLINE_URL_RE.match(line)
        if bare_url:
            if current is None:
                current = {"title": "", "url": "", "lines": []}
            if current["url"]:
                flush()
                current = {"title": "", "url": "", "lines": []}
            current["url"] = bare_url.group("url")
            continue
        label = _ANYSEARCH_LABEL_RE.match(line)
        if current is None:
            current = {"title": "", "url": "", "lines": []}
        current["lines"].append(
            _strip_markdown_inline(
                label.group("text") if label else line
            )
        )
    flush()
    return results


_ANYSEARCH_EMPTY_TEXT_RE = re.compile(
    r"(未找到|没有找到|未搜索到|无搜索结果|暂无结果|没有相关结果|"
    r"无结果|抱歉[，,]?\s*没有|no results?|no matching)",
    re.IGNORECASE,
)


def _anysearch_payload_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        parts = []
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _anysearch_declares_empty(payload) -> bool:
    """判断返回是否“明确零结果”（合法空 JSON 或文字声明没搜到）。

    与“有内容但解析失败”严格区分：前者应为 no_results/empty_results，
    后者才是 unparsed_results。
    """
    if isinstance(payload, list):
        # 原生空数组是插件明确零结果（不是解析失败）。
        return not payload
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "records"):
            value = payload.get(key)
            if isinstance(value, list) and not value:
                return True
        content = payload.get("content")
        if isinstance(content, list):
            if not content:
                return True
            # MCP/工具统一包装：{"content": [{"type": "text", "text": "[]"}]}
            parts = [
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if parts:
                return _anysearch_declares_empty("\n".join(parts))
    if isinstance(payload, str):
        text = payload.strip()
        if text in ("", "[]", "{}", "null", "None"):
            return True
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (list, dict)):
            return _anysearch_declares_empty(parsed)
        # 结果头/条数声明优先于摘要词句：`## Search Results (0 results, 100ms)`
        # 是本轮明确零结果；声明 N>0 时即使摘要出现“没有找到存档”也不能
        # 当成确无结果，那是解析失败（有结果但没有链接）。
        count_match = re.search(
            r"(\d+)\s*results?\b|共\s*(\d+)\s*条", text, re.IGNORECASE
        )
        if count_match:
            value = count_match.group(1) or count_match.group(2)
            return int(value) == 0
        if _ANYSEARCH_EMPTY_TEXT_RE.search(text):
            return True
    return False


def _anysearch_structured_results(payload) -> list[dict]:
    """把 Anysearch 插件返回的 JSON/文本归一化为标准结果列表。

    支持四种真实形态：结构化 JSON（results/items/content 列表）、
    分节 Markdown（标题 + ``- **URL**:`` + 摘要）、``[标题](链接)``
    Markdown 链接，以及裸链接行。
    """
    data = payload
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        if text.startswith(("错误", "Error", "error")):
            raise ValueError("anysearch_error")
        try:
            data = json.loads(text)
        except Exception:
            data = None
    if isinstance(data, dict):
        if isinstance(data.get("content"), list):
            parts = []
            for item in data["content"]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return _anysearch_structured_results("\n".join(parts))
        for key in ("results", "items", "data", "records"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
        else:
            data = [data] if data.get("url") or data.get("link") else []
    if isinstance(data, list):
        results = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            item = _normalize_item(raw, "anysearch")
            if item:
                results.append(item)
        if results:
            return results
    if isinstance(payload, str):
        text = payload
        # 优先按真实返回的分节 Markdown 解析；没有分节结构时再退回
        # [标题](链接) 形式。
        sections = _anysearch_section_results(text)
        if sections:
            return sections
        matches = list(re.finditer(
            r"\[([^\]\n]{1,300})\]\((https?://[^)\s]+)\)", text
        ))
        results = []
        for index, match in enumerate(matches):
            title = match.group(1).strip()
            url = match.group(2).strip()
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            snippet = text[start:end].strip().strip("\n-* ")
            item = _normalize_item({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source": "anysearch",
            })
            if item:
                results.append(item)
        return results
    return []


class AnysearchSearchBackend(SearchBackend):
    """显式适配 AstrBot Anysearch 插件的 anysearch_search 工具。"""

    name = "anysearch"
    TOOL_NAME = "anysearch_search"

    def __init__(self, context=None, adapter=None, tool_manager=None):
        self._context = context
        self._adapter = adapter
        self._tool_manager = tool_manager

    def _manager(self):
        if self._tool_manager is not None:
            return self._tool_manager
        if self._context is None:
            return None
        try:
            return self._context.get_llm_tool_manager()
        except Exception:
            return None

    @staticmethod
    def _module_path(tool) -> str:
        value = getattr(tool, "handler_module_path", "") or ""
        if value:
            return str(value)
        return str(getattr(tool, "__module__", "") or "")

    def _find_tool(self):
        manager = self._manager()
        if manager is None:
            return None, "anysearch_tool_manager_unavailable"
        tools = getattr(manager, "func_list", None)
        if not isinstance(tools, list):
            return None, "anysearch_tool_manager_unavailable"
        tool = None
        for candidate in reversed(tools):
            if getattr(candidate, "name", "") == self.TOOL_NAME:
                tool = candidate
                break
        if tool is None:
            return None, "anysearch_plugin_not_installed"
        module = self._module_path(tool)
        class_name = type(tool).__name__
        if "anysearch" not in module.lower() and "AnySearch" not in class_name:
            return None, "anysearch_adapter_mismatch"
        try:
            from astrbot.core.star.star import star_map
            owner = None
            for key, metadata in star_map.items():
                if module == key or module.startswith(key + "."):
                    owner = metadata
                    break
            if owner is not None and not bool(getattr(owner, "activated", True)):
                return None, "anysearch_plugin_disabled"
        except Exception:
            pass
        if not bool(getattr(tool, "active", True)):
            return None, "anysearch_tool_disabled"
        run = getattr(tool, "run", None)
        if callable(run) and inspect.iscoroutinefunction(run):
            try:
                signature = inspect.signature(run)
                positional = [
                    parameter for parameter in signature.parameters.values()
                    if parameter.kind in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
                var_keyword = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
                first_name = positional[0].name if positional else ""
                if first_name in {"event", "e"} and (
                    "query" in signature.parameters or var_keyword
                ):
                    return ("run", tool), ""
            except (TypeError, ValueError):
                pass
        call = getattr(tool, "call", None)
        if (
            callable(call)
            and inspect.iscoroutinefunction(call)
            and AstrBotSearchBackend._call_contract_ok(tool)
        ):
            return ("call", tool), ""
        return None, "anysearch_call_contract_unverified"

    async def availability(self, event=None) -> tuple[bool, str]:
        if self._adapter is not None:
            return True, ""
        found, reason = self._find_tool()
        if found is None:
            return False, reason
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
                found, reason = self._find_tool()
                if found is None:
                    return BackendResult(
                        self.name, "unavailable", [], reason,
                        int((time.monotonic() - start) * 1000),
                    )
                mode, tool = found
                limit = max(1, min(int(max_results), 100))
                if mode == "run":
                    payload = await tool.run(event, query=query, max_results=limit)
                else:
                    from astrbot.core.agent.run_context import ContextWrapper
                    from astrbot.core.astr_agent_context import AstrAgentContext

                    wrapper = ContextWrapper(
                        AstrAgentContext(context=self._context, event=event)
                    )
                    payload = await tool.call(
                        wrapper, query=query, max_results=limit
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            return BackendResult(
                self.name, "failed", [], "anysearch_search_error",
                int((time.monotonic() - start) * 1000),
            )
        try:
            results = _anysearch_structured_results(payload)
        except ValueError:
            return BackendResult(
                self.name, "failed", [], "anysearch_error",
                int((time.monotonic() - start) * 1000),
            )
        except Exception:
            return BackendResult(
                self.name, "failed", [], "invalid_response",
                int((time.monotonic() - start) * 1000),
            )
        results = results[:max_results]
        duration_ms = int((time.monotonic() - start) * 1000)
        if not results:
            reason = (
                "empty_results"
                if _anysearch_declares_empty(payload)
                else "unparsed_results"
            )
            return BackendResult(
                self.name, "no_results", [], reason, duration_ms
            )
        return BackendResult(self.name, "success", results, "", duration_ms)

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
                _join_api_url(self._base_url, "search"),
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
        self.title_anchor_seen = False
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
        if (
            tag == "a"
            and self.h2_depth > 0
            and not self.in_title
            and not self.title_anchor_seen
        ):
            href = dict(attrs).get("href") or ""
            self.title_anchor_seen = True
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


class _BaiduResultFrame:
    def __init__(self, depth: int):
        self.depth = depth
        self.in_title = False
        self.title_anchor_seen = False
        self.title_parts: list[str] = []
        self.url = ""
        self.h3_depth = 0
        self.h3_seen = False
        self.anchor_seen = False
        self.later_h3_after_title = False
        self.ambiguous_child = False
        self.titled_children: list[dict] = []
        self.in_snippet = False
        self.snippet_tag: str | None = None
        self.snippet_tag_depth = 0
        self.snippet_parts: list[str] = []
        self.snippet = ""
        self.snippet_found = False


class _BaiduResultParser(HTMLParser):
    """按容器栈解析 Baidu 结果。

    规则矩阵：
    - 单独 result / c-container / 同元素组合：自身有标题即结果；
    - 内层候选没有标题：视为当前结果的布局或摘要包装，摘要可上推给
      最近的有标题祖先；
    - 内层候选有标题：视为独立结果，父子各自保留标题/URL/摘要；
    - 外层无标题且包含多个有标题子结果：各子结果分别输出；
    - 外层无标题且只有摘要包装：不能凭空产生结果，丢弃摘要；
    - 摘要只接受当前叶子候选内 c-abstract/abstract；相邻结果、侧栏、
      content-right 广告不拼接。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.tag_depth: dict[str, int] = {}
        self.stack: list[_BaiduResultFrame] = []
        self.results: list[dict] = []

    @staticmethod
    def _is_candidate(attrs) -> bool:
        classes = (dict(attrs).get("class") or "").split()
        return "result" in classes or "c-container" in classes

    def _finish_title(self, frame: _BaiduResultFrame):
        frame.in_title = False

    def _finish_snippet(self, frame: _BaiduResultFrame):
        if not frame.in_snippet:
            return
        text = "".join(frame.snippet_parts).strip()
        if text and not frame.snippet_found:
            frame.snippet = text
            frame.snippet_found = True
        frame.in_snippet = False
        frame.snippet_tag = None
        frame.snippet_parts = []

    def _finish_frame(
        self, frame: _BaiduResultFrame, parent: _BaiduResultFrame | None
    ):
        self._finish_title(frame)
        self._finish_snippet(frame)
        title = "".join(frame.title_parts).strip()
        if title:
            entry = {
                "title": title,
                "url": frame.url,
                "snippet": frame.snippet,
            }
            self.results.append(entry)
            if parent is not None:
                parent.titled_children.append(entry)
            return

        # 出现过 h3/链接却没有有效标题，或内部有标题不可用的子结果：
        # 这是标题不可用的独立结果或歧义包装，摘要不能上推/补给父结果。
        if frame.h3_seen or frame.anchor_seen or frame.ambiguous_child:
            if parent is not None:
                parent.ambiguous_child = True
            return

        if frame.titled_children:
            # 父容器自身无标题，但只有一个有标题子结果，且摘要在父容器
            # 兄弟节点：唯一归属明确，补给该子结果。多个子结果时不猜。
            if (
                len(frame.titled_children) == 1
                and frame.snippet
                and not frame.titled_children[0]["snippet"]
            ):
                frame.titled_children[0]["snippet"] = frame.snippet
            return

        if parent is None:
            return

        # 纯布局/摘要包装：摘要上推给最近的无标题祖先，随后由有标题
        # 祖先消费；不拼接多个包装的摘要。
        if frame.snippet and not parent.snippet:
            parent.snippet = frame.snippet

    def handle_starttag(self, tag, attrs):
        self.tag_depth[tag] = self.tag_depth.get(tag, 0) + 1
        if tag == "div":
            self.div_depth += 1
            if self._is_candidate(attrs):
                self.stack.append(_BaiduResultFrame(self.div_depth))

        if not self.stack:
            return
        frame = self.stack[-1]
        if tag == "h3":
            if frame.title_anchor_seen:
                frame.later_h3_after_title = True
            frame.h3_seen = True
            frame.h3_depth += 1
            return
        if tag == "a":
            # 摘要正文中的内联链接是内容，不代表新的独立结果；只有
            # 标题区之外、摘要区之外的锚点才参与“无效标题”判断。
            if not frame.in_snippet and frame.h3_depth == 0:
                frame.anchor_seen = True
        if (
            tag == "a"
            and frame.h3_depth > 0
            and not frame.title_anchor_seen
        ):
            href = str(dict(attrs).get("href") or "").strip()
            if (
                not href
                or href.startswith("#")
                or href.lower().startswith("javascript:")
            ):
                return
            resolved = urljoin("https://www.baidu.com", href)
            if not _is_http_url(resolved):
                return
            frame.title_anchor_seen = True
            frame.in_title = True
            frame.title_parts = []
            frame.url = resolved
            return

        classes = (dict(attrs).get("class") or "").split()
        if (
            ("c-abstract" in classes or "abstract" in classes)
            and not frame.in_snippet
            and not frame.snippet_found
            and not frame.later_h3_after_title
        ):
            frame.in_snippet = True
            frame.snippet_tag = tag
            frame.snippet_tag_depth = self.tag_depth.get(tag, 0)
            frame.snippet_parts = []

    def handle_endtag(self, tag):
        if (
            tag == "div"
            and self.stack
            and self.div_depth == self.stack[-1].depth
        ):
            frame = self.stack.pop()
            parent = self.stack[-1] if self.stack else None
            self._finish_frame(frame, parent)
            self.tag_depth[tag] = max(0, self.tag_depth.get(tag, 0) - 1)
            self.div_depth = max(0, self.div_depth - 1)
            return

        if self.stack:
            frame = self.stack[-1]
            if tag == "a" and frame.in_title:
                self._finish_title(frame)
            elif tag == "h3":
                frame.h3_depth = max(0, frame.h3_depth - 1)
            if (
                tag == frame.snippet_tag
                and frame.in_snippet
                and self.tag_depth.get(tag, 0)
                == frame.snippet_tag_depth
            ):
                self._finish_snippet(frame)

        if self.tag_depth.get(tag, 0) > 0:
            self.tag_depth[tag] -= 1
        if tag == "div":
            self.div_depth = max(0, self.div_depth - 1)

    def handle_data(self, data):
        if not self.stack:
            return
        frame = self.stack[-1]
        if frame.in_title:
            frame.title_parts.append(data)
        if frame.in_snippet:
            frame.snippet_parts.append(data)


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

    CLOSE_CLEANUP_TIMEOUT = 2.0
    CLOSE_CANCEL_GRACE = 0.2

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._cleanup_tasks: set[asyncio.Task] = set()
        self.close_incomplete = False
        self.cleanup_failures = 0

    @property
    def pending_cleanup_count(self) -> int:
        return sum(1 for task in self._cleanup_tasks if not task.done())

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
        cleanup = min(0.06, max(0.01, timeout_s * 0.6))
        margin = min(0.02, max(0.005, timeout_s * 0.1))
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
        return await self._search_queries([query], max_results, timeout_s)

    async def search_multiple(
        self,
        queries: list[str],
        max_results: int,
        event=None,
        timeout_s: float | None = None,
    ) -> BackendResult:
        """按多路查询共享同一份超时预算与全局结果数上限。

        每个来源对每路查询各发起一次请求；同一来源内按查询轮转交错，
        再由来源之间轮转合并，保证不同查询都有机会进入素材池，同时在
        两路查询返回相同页面时保持原有去重与顺序。
        """
        cleaned: list[str] = []
        for query in queries or []:
            text = str(query or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            return BackendResult(self.name, "failed", [], "empty_query", 0)
        return await self._search_queries(cleaned, max_results, timeout_s)

    async def _search_queries(
        self,
        queries: list[str],
        max_results: int,
        timeout_s: float | None = None,
    ) -> BackendResult:
        start = time.monotonic()
        max_results = max(1, int(max_results))
        client = await self._get_client()
        # 来源截止提前于后端预算，给取消后的 finally/异步清理留空间；
        # 清理预算有界，避免外层总超时再次取消整个网页后端。
        source_timeout, cleanup_budget = self._source_deadlines(timeout_s)
        labeled_tasks = []
        for query_index, query in enumerate(queries):
            labeled_tasks.append(
                ((query_index, "bing"), asyncio.create_task(
                    self._search_bing(
                        client, query, max_results, source_timeout
                    )
                )),
            )
            labeled_tasks.append(
                ((query_index, "baidu"), asyncio.create_task(
                    self._search_baidu(
                        client, query, max_results, source_timeout
                    )
                )),
            )
        labels = {task: label for label, task in labeled_tasks}
        tasks = list(labels)
        try:
            done, pending = await asyncio.wait(tasks, timeout=source_timeout)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            self._cleanup_tasks.update(
                task for task in tasks if not task.done()
            )
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            raise

        cleanup_incomplete = False
        if pending:
            pending_list = list(pending)
            for task in pending_list:
                task.cancel()
            try:
                # 只取消一次；这里不再用 wait_for，避免超时后自动发起
                # 第二次取消，静默打断 finally 中的正常资源清理。
                _, cleanup_pending = await asyncio.wait(
                    pending_list, timeout=cleanup_budget
                )
            except asyncio.CancelledError:
                self._cleanup_tasks.update(
                    task for task in pending_list if not task.done()
                )
                raise

            for task in pending_list:
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        logger.warning(
                            "[小作文生成器] 网页来源清理异常: %s",
                            type(exc).__name__,
                        )
            if cleanup_pending:
                cleanup_incomplete = True
                self._cleanup_tasks.update(cleanup_pending)
                logger.warning(
                    "[小作文生成器] 网页来源清理超出同步预算，已转入关闭阶段: %s",
                    ", ".join(
                        f"{labels[task][1]}#{labels[task][0] + 1}"
                        for task in cleanup_pending
                    ),
                )

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
            # _search_bing/_search_baidu 返回 (source, status, items, reason)。
            _, status, items, reason = task.result()
            source_results.append((label, status, items, reason))

        failures: list[dict] = []
        # 按“查询方向 × 来源”分别收集并去重；最终合并让查询方向参与，
        # 避免结果上限较小或某轮全重复时只剩单一方向/来源。
        per_query_sources: list[dict[str, list[dict]]] = [
            {} for _ in queries
        ]
        source_order: list[str] = []
        for (query_index, label), status, items, reason in source_results:
            if label not in source_order:
                source_order.append(label)
            if status != "success":
                entry = {
                    "source": label,
                    "status": status,
                    "reason": reason or status,
                }
                if entry not in failures:
                    failures.append(entry)
                continue
            local_seen = set()
            queue = []
            for item in items:
                key = _url_dedupe_key(item.get("url", ""))
                if key in local_seen:
                    continue
                local_seen.add(key)
                queue.append(item)
            per_query_sources[query_index][label] = queue

        def round_robin(queues: list[list[dict]], cap: int) -> list[dict]:
            """多队列轮转合并：全重复的队列跳过，继续遍历后面的队列。"""
            merged: list[dict] = []
            seen = set()
            cursors = [0] * len(queues)
            while len(merged) < cap:
                progressed = False
                for index, items in enumerate(queues):
                    while cursors[index] < len(items):
                        item = items[cursors[index]]
                        cursors[index] += 1
                        key = _url_dedupe_key(item.get("url", ""))
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(item)
                        progressed = True
                        break
                    if len(merged) >= cap:
                        break
                if not progressed:
                    break
            return merged

        per_query_merged = [
            [
                {**item, "query_index": qi}
                for item in round_robin(
                    [
                        per_query_sources[qi].get(label, [])
                        for label in source_order
                    ],
                    max_results,
                )
            ]
            for qi in range(len(queries))
        ]
        merged = round_robin(per_query_merged, max_results)
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

        # 先保留比 result_count 更多的候选供去重补位；仍设有上限，
        # 不额外发请求、不无限收集页面结果。
        candidate_limit = max(20, max_results * 4)
        items = self._parse_bing(html)[:candidate_limit]
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

        candidate_limit = max(20, max_results * 4)
        items = self._parse_baidu(html)[:candidate_limit]
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
        parser = _BaiduResultParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            pass
        items = []
        for result in parser.results:
            item = _normalize_item({
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("snippet", ""),
                "source": "baidu",
            })
            if item:
                items.append(item)
        return items

    def _consume_cleanup_exceptions(self, tasks) -> None:
        for task in tasks:
            if task.done() and not task.cancelled():
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    continue
                if exc is not None:
                    self.cleanup_failures += 1
                    logger.warning(
                        "[小作文生成器] 网页来源清理任务失败: %s",
                        type(exc).__name__,
                    )

    async def close(self) -> None:
        self.close_incomplete = False
        if self._cleanup_tasks:
            pending = {
                task for task in self._cleanup_tasks if not task.done()
            }
            self._consume_cleanup_exceptions(self._cleanup_tasks - pending)
            self._cleanup_tasks = pending
            if pending:
                # 先给正常 finally 清理足够时间，不主动第二次取消。
                try:
                    done, pending = await asyncio.wait(
                        pending, timeout=self.CLOSE_CLEANUP_TIMEOUT
                    )
                except asyncio.CancelledError:
                    for task in pending:
                        task.cancel()
                    self._cleanup_tasks = set(pending)
                    self.close_incomplete = True
                    raise

                self._consume_cleanup_exceptions(done)
                self._cleanup_tasks = {
                    task for task in pending if not task.done()
                }
                if self._cleanup_tasks:
                    logger.warning(
                        "[小作文生成器] close 等待清理超时，"
                        "执行最后一次取消: %s",
                        len(self._cleanup_tasks),
                    )
                    for task in self._cleanup_tasks:
                        task.cancel()
                    try:
                        done, pending = await asyncio.wait(
                            self._cleanup_tasks,
                            timeout=self.CLOSE_CANCEL_GRACE,
                        )
                    except asyncio.CancelledError:
                        self._cleanup_tasks = {
                            task
                            for task in self._cleanup_tasks
                            if not task.done()
                        }
                        self.close_incomplete = True
                        raise

                    self._consume_cleanup_exceptions(done)
                    self._cleanup_tasks = {
                        task for task in pending if not task.done()
                    }
                    if self._cleanup_tasks:
                        self.close_incomplete = True
                        logger.error(
                            "[小作文生成器] close 时仍有无法终止的"
                            "清理任务: %s",
                            len(self._cleanup_tasks),
                        )

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
        groups: list[str] | None = None,
        api_key: str = "",
        api_base_url: str = "https://api.tavily.com",
        api_provider: str = "tavily",
        astrbot_context=None,
        astrbot_adapter=None,
        anysearch_adapter=None,
        backends: dict | None = None,
        api_client: httpx.AsyncClient | None = None,
        web_client: httpx.AsyncClient | None = None,
    ):
        self.enabled = search_enabled(enabled)
        legacy_mode = resolve_search_mode(enabled, mode)
        self.groups = resolve_search_groups(
            self.enabled, mode, bool(fallback_enabled), groups
        )
        # 兼容旧调用方：没有传组列表时保留旧 mode 语义（含 auto/off）用于
        # 诊断与统计；新配置则把首个启用组作为展示用 mode。
        self.mode = (
            legacy_mode if groups is None
            else (self.groups[0] if self.groups else "off")
        )
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
        self.last_result = None

        if backends is not None:
            self._backends = dict(backends)
        else:
            self._backends = {
                "astrbot": AstrBotSearchBackend(
                    context=astrbot_context,
                    adapter=astrbot_adapter,
                ),
                "anysearch": AnysearchSearchBackend(
                    context=astrbot_context,
                    adapter=anysearch_adapter,
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
        """按保存的组列表从上到下展开为具体后端。

        AstrBot 组内先官方工具、再搜索插件；组内任一后端成功即返回，
        不再继续后续组（search_game_info 首个有效结果即返回）。
        """
        order: list[str] = []
        for group in self.groups:
            for name in GROUP_BACKENDS.get(group, ()):
                if name not in order:
                    order.append(name)
        return order

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

    def _disabled_result(self, reason: str = "disabled") -> dict:
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
            "mode": self.mode,
        }

    async def search_game_info(self, game_name: str, event=None) -> dict:
        if self.mode == "off":
            return self._disabled_result()

        queries = game_search_queries(game_name)
        query = queries[0] if queries else str(game_name or "")
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
            # 网页组支持两路查询：共享同一份超时预算与全局结果数上限。
            if queries and hasattr(backend, "search_multiple"):
                search_call = backend.search_multiple(
                    queries,
                    self.result_count,
                    event=event,
                    timeout_s=search_timeout,
                )
            else:
                search_call = backend.search(
                    query,
                    self.result_count,
                    event=event,
                    timeout_s=search_timeout,
                )
            try:
                result = await asyncio.wait_for(
                    search_call,
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
                payload = {
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
                    "mode": self.mode,
                }
                self.last_result = payload
                return payload

        duration_ms = int((loop.time() - start) * 1000)
        failure_reason = "no_results"
        # 区分“后端正常调用但零结果”和真正的失败：只有出现 failed/timeout
        # 等真实失败才标记 failed；全部是正常空结果时整体状态用 no_results，
        # 避免把正常的空结果与网络/服务故障混为一谈。
        genuine_failures = [
            item for item in attempts
            if item["status"] not in ("success", "no_results")
        ]
        no_result_attempts = [
            item for item in attempts if item["status"] == "no_results"
        ]
        overall_status = "failed"
        if attempts:
            if genuine_failures:
                failure_reason = (
                    genuine_failures[-1]["reason"]
                    or genuine_failures[-1]["status"]
                )
            elif no_result_attempts and not any(
                item["status"] == "success" for item in attempts
            ):
                overall_status = "no_results"
                failure_reason = (
                    no_result_attempts[-1]["reason"] or "no_results"
                )
        elif skipped:
            failure_reason = skipped[-1]["reason"] or skipped[-1]["status"]
        else:
            failure_reason = "no_backend"

        payload = {
            "status": overall_status,
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
            "mode": self.mode,
        }
        self.last_result = payload
        return payload

    async def candidate_status(self, event=None) -> list[dict]:
        """配置中心展示用：不发起搜索，只报告候选可用性。"""
        items = []
        for name in BACKEND_ORDER:
            backend = self._backends.get(name)
            available, reason = (False, "backend_unavailable")
            if backend is not None:
                try:
                    available, reason = await backend.availability(event)
                except Exception:
                    available, reason = False, "backend_unavailable"
            items.append({
                "backend": name,
                "label": BACKEND_LABELS.get(name, name),
                "available": bool(available),
                "reason": reason,
                "in_mode": name in self._candidate_order(),
            })
        return items

    async def test_search(self, game_name: str, event=None) -> dict:
        result = await self.search_game_info(game_name, event=event)
        message = format_search_diagnostic(result)
        return {
            "success": result["status"] == "success" and bool(result["results"]),
            "message": message,
            "summary": result["summary"][:500],
            "backend": result.get("effective_backend", ""),
            "effective_backend": result.get("effective_backend", ""),
            "mode": result.get("mode", self.mode),
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
    "astrbot_call_contract_unverified": "AstrBot 工具调用契约未验证",
    "astrbot_config_unavailable": "AstrBot 配置不可读",
    "astrbot_search_disabled": "AstrBot 搜索未启用",
    "astrbot_provider_unverified": "AstrBot 搜索提供商未验证",
    "astrbot_key_missing": "AstrBot 未配置密钥",
    "astrbot_search_error": "AstrBot 搜索失败",
    "astrbot_tool_manager_unavailable": "AstrBot 工具管理器不可用",
    "astrbot_tool_unregistered": "AstrBot 搜索工具未注册",
    "astrbot_tool_disabled": "AstrBot 搜索工具已停用",
    "astrbot_retry_risk": "AstrBot 多 Key 自动重试风险",
    "anysearch_tool_manager_unavailable": "Anysearch 工具管理器不可用",
    "anysearch_plugin_not_installed": "Anysearch 插件未安装或工具未注册",
    "anysearch_adapter_mismatch": "Anysearch 工具来源不匹配",
    "anysearch_plugin_disabled": "Anysearch 插件已停用",
    "anysearch_tool_disabled": "Anysearch 工具已停用",
    "anysearch_call_contract_unverified": "Anysearch 调用契约未验证",
    "anysearch_search_error": "Anysearch 搜索失败",
    "anysearch_error": "Anysearch 返回错误",
    "unparsed_results": "Anysearch 结果无法解析为有效链接",
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


def search_outcome_label(result: dict) -> str:
    """统计用搜索结果状态，避免把“全部失败”误写成“已降级”。"""
    status = result.get("status", "failed")
    if status == "success":
        attempts = result.get("attempted") or []
        skipped = result.get("skipped") or []
        effective = result.get("effective_backend") or ""
        first_backend = (
            attempts[0].get("backend") if attempts else ""
        )
        switched = bool(skipped) or (
            bool(first_backend and effective)
            and first_backend != effective
        )
        if switched:
            return "已切换到其他后端"
        if result.get("partial"):
            return "首选后端部分成功"
        return "首选后端成功"
    if status == "disabled":
        return "未调用（搜索关闭）"
    if status == "no_results":
        return "已调用但无有效结果"
    if result.get("attempted"):
        return "全部候选后端失败"
    if result.get("skipped"):
        return "未调用（无可用后端）"
    return "未调用"


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

    if status == "no_results":
        no_result_details = []
        for item in result.get("attempted", []):
            label = BACKEND_LABELS.get(
                item.get("backend", ""), item.get("backend", "")
            )
            item_reason = _REASON_TEXT.get(
                item.get("reason", ""), item.get("reason", "")
            )
            no_result_details.append(
                f"{label}（{item_reason or item.get('status', '')}）"
            )
        detail_suffix = (
            f"；尝试：{' → '.join(no_result_details)}"
            if no_result_details
            else ""
        )
        degraded = "是" if result.get("degraded") else "否"
        return (
            f"已正常调用但无有效结果（后端：无，有效结果：0，"
            f"降级：{degraded}{detail_suffix}）"
        )

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
