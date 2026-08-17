"""
联网搜索服务模块
支持 Bing + 百度多源并发搜索，自动提取游戏资料并形成搜索汇总
"""

import asyncio
import logging
import random
import re
import time
from html.parser import HTMLParser

import httpx

logger = logging.getLogger("astrbot")

# User-Agent 列表，用于轮换
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


class _TextExtractor(HTMLParser):
    """从 HTML 中提取纯文本"""

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


def _extract_meta_description(html: str) -> str:
    """从 HTML 中提取 meta description"""
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


def _extract_text_from_html(html: str) -> str:
    """从 HTML 中提取纯文本"""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        return ""
    return extractor.get_text()


def _build_summary(text: str, max_length: int = 1500) -> str:
    """截取摘要文本"""
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_period = max(truncated.rfind("。"), truncated.rfind("！"), truncated.rfind("？"))
    if last_period > max_length // 2:
        return truncated[: last_period + 1]
    return truncated + "..."


class SearchService:
    """联网搜索服务，支持多源并发搜索"""

    def __init__(self, enabled: bool = True, timeout_ms: int = 8000, result_count: int = 3):
        self.enabled = enabled
        self.timeout = timeout_ms / 1000
        self.result_count = max(1, min(result_count, 10))
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    async def _search_bing(self, query: str, result_count: int = 3) -> dict:
        """使用 Bing 搜索，返回 {raw_text}"""
        client = await self._get_client()
        url = "https://www.bing.com/search"
        params = {"q": query, "mkt": "zh-CN"}
        resp = await client.get(url, params=params, headers=self._get_headers())
        resp.raise_for_status()
        html = resp.text

        results = re.findall(
            r'<li\s+class="b_algo".*?<p>(.*?)</p>',
            html,
            re.DOTALL,
        )
        if results:
            texts = []
            for r in results[:result_count]:
                text = re.sub(r'<[^>]+>', '', r).strip()
                if text:
                    texts.append(text)
            if texts:
                raw = "\n".join(texts)
                return {"raw_text": raw}

        meta_desc = _extract_meta_description(html)
        if meta_desc:
            return {"raw_text": meta_desc}

        full_text = _extract_text_from_html(html)
        summary = _build_summary(full_text)
        return {"raw_text": summary}

    async def _search_baidu(self, query: str, result_count: int = 3) -> dict:
        """使用百度搜索，返回 {raw_text}"""
        client = await self._get_client()
        url = "https://www.baidu.com/s"
        params = {"wd": query}
        headers = self._get_headers()
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        html = resp.text

        results = re.findall(
            r'<div\s+class="c-abstract".*?>(.*?)</div>',
            html,
            re.DOTALL,
        )
        if results:
            texts = []
            for r in results[:result_count]:
                text = re.sub(r'<[^>]+>', '', r).strip()
                if text:
                    texts.append(text)
            if texts:
                raw = "\n".join(texts)
                return {"raw_text": raw}

        full_text = _extract_text_from_html(html)
        summary = _build_summary(full_text)
        return {"raw_text": summary}

    async def search_game_info(self, game_name: str) -> dict:
        """
        多源并发搜索游戏信息，至少从 2 个搜索源获取结果

        Returns:
            {
                "status": "success" | "partial" | "failed" | "disabled",
                "summary": str,
                "provider": str,
                "duration_ms": int,
            }
        """
        if not self.enabled:
            return {
                "status": "disabled", "summary": "",
                "provider": "未启用", "duration_ms": 0,
            }

        query = f"{game_name} 游戏介绍 平台 评价"
        start_time = time.time()

        # 并发搜索多个源
        bing_task = asyncio.create_task(self._safe_search("Bing", self._search_bing, query, self.result_count))
        baidu_task = asyncio.create_task(self._safe_search("百度", self._search_baidu, query, self.result_count))

        results = await asyncio.gather(bing_task, baidu_task)
        duration_ms = int((time.time() - start_time) * 1000)

        # 收集成功的结果
        successful = [(name, res) for name, res in results if res is not None]
        all_texts = []
        providers = []

        for name, res in successful:
            providers.append(name)
            if res.get("raw_text"):
                all_texts.append(f"[{name}] {res['raw_text']}")

        if successful:
            merged_summary = _build_summary("\n\n".join(all_texts))
            provider_str = " + ".join(providers)

            logger.info(
                f"[小作文生成器] 搜索成功: {game_name}, 来源: {provider_str}, "
                f"合并 {len(merged_summary)} 字符, 耗时 {duration_ms}ms"
            )
            return {
                "status": "success",
                "summary": merged_summary,
                "provider": provider_str,
                "duration_ms": duration_ms,
            }

        logger.warning(f"[小作文生成器] 所有搜索源均失败: {game_name}")
        return {
            "status": "failed", "summary": "",
            "provider": "失败", "duration_ms": duration_ms,
        }

    async def _safe_search(self, name: str, func, query: str, result_count: int = 3) -> tuple[str, dict | None]:
        """安全执行搜索，捕获异常"""
        try:
            result = await func(query, result_count)
            if result.get("raw_text") and len(result["raw_text"]) > 30:
                return (name, result)
            logger.warning(f"[小作文生成器] {name} 搜索结果过短，跳过")
            return (name, None)
        except Exception as e:
            logger.warning(f"[小作文生成器] {name} 搜索失败: {e}")
            return (name, None)

    async def test_search(self, game_name: str) -> dict:
        """测试搜索功能"""
        if not self.enabled:
            return {
                "success": False,
                "message": "联网搜索功能未启用",
                "summary": "",
            }

        result = await self.search_game_info(game_name)
        if result["status"] == "success":
            return {
                "success": True,
                "message": f"搜索成功（来源：{result['provider']}）",
                "summary": result["summary"][:500],
            }
        else:
            return {
                "success": False,
                "message": "搜索失败，请检查网络连接",
                "summary": "",
            }

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
