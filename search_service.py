"""
联网搜索服务模块
支持 Bing → 百度 多源 fallback，自动提取游戏资料
"""

import logging
import random
import re
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

# 单个搜索源超时（秒）
SINGLE_SOURCE_TIMEOUT = 8


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
    # 尝试 content 在 name 前面的情况
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
    # 在合适的位置截断
    truncated = text[:max_length]
    last_period = max(truncated.rfind("。"), truncated.rfind("！"), truncated.rfind("？"))
    if last_period > max_length // 2:
        return truncated[: last_period + 1]
    return truncated + "..."


class SearchService:
    """联网搜索服务"""

    def __init__(self, enabled: bool = True, timeout_ms: int = 8000):
        self.enabled = enabled
        self.timeout = timeout_ms / 1000  # 转换为秒
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

    async def _search_bing(self, query: str) -> str:
        """使用 Bing 搜索"""
        client = await self._get_client()
        url = "https://www.bing.com/search"
        params = {"q": query, "mkt": "zh-CN"}
        resp = await client.get(url, params=params, headers=self._get_headers())
        resp.raise_for_status()
        html = resp.text

        # 提取搜索结果摘要
        results = re.findall(
            r'<li\s+class="b_algo".*?<p>(.*?)</p>',
            html,
            re.DOTALL,
        )
        if results:
            texts = []
            for r in results[:5]:
                text = re.sub(r'<[^>]+>', '', r).strip()
                if text:
                    texts.append(text)
            if texts:
                return "\n".join(texts)

        # fallback: 提取 meta description
        meta_desc = _extract_meta_description(html)
        if meta_desc:
            return meta_desc

        # fallback: 提取全部文本
        full_text = _extract_text_from_html(html)
        return _build_summary(full_text)

    async def _search_baidu(self, query: str) -> str:
        """使用百度搜索"""
        client = await self._get_client()
        url = "https://www.baidu.com/s"
        params = {"wd": query}
        headers = self._get_headers()
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        html = resp.text

        # 提取搜索结果摘要
        results = re.findall(
            r'<div\s+class="c-abstract".*?>(.*?)</div>',
            html,
            re.DOTALL,
        )
        if results:
            texts = []
            for r in results[:5]:
                text = re.sub(r'<[^>]+>', '', r).strip()
                if text:
                    texts.append(text)
            if texts:
                return "\n".join(texts)

        # fallback: 提取全部文本
        full_text = _extract_text_from_html(html)
        return _build_summary(full_text)

    async def search_game_info(self, game_name: str) -> dict:
        """
        搜索游戏信息

        Args:
            game_name: 游戏名称

        Returns:
            {
                "status": "success" | "partial" | "failed" | "disabled",
                "summary": "搜索结果摘要",
            }
        """
        if not self.enabled:
            return {"status": "disabled", "summary": ""}

        query = f"{game_name} 游戏介绍 平台 评价"

        # 尝试 Bing
        try:
            text = await self._search_bing(query)
            if text and len(text) > 50:
                logger.info(f"Bing 搜索成功: {game_name}, {len(text)} 字符")
                return {"status": "success", "summary": _build_summary(text)}
        except Exception as e:
            logger.warning(f"Bing 搜索失败: {e}")

        # 尝试百度
        try:
            text = await self._search_baidu(query)
            if text and len(text) > 50:
                logger.info(f"百度搜索成功: {game_name}, {len(text)} 字符")
                return {"status": "success", "summary": _build_summary(text)}
        except Exception as e:
            logger.warning(f"百度搜索失败: {e}")

        logger.warning(f"所有搜索源均失败: {game_name}")
        return {"status": "failed", "summary": ""}

    async def test_search(self, game_name: str) -> dict:
        """
        测试搜索功能

        Args:
            game_name: 游戏名称

        Returns:
            {
                "success": bool,
                "message": str,
                "summary": str,
            }
        """
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
                "message": "搜索成功",
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
