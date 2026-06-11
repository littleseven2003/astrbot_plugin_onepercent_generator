"""
AI API 调用模块
封装 OpenAI 格式的 API 调用，支持 Mock 模式
"""

import logging
import httpx

from .prompt import SYSTEM_PROMPT

logger = logging.getLogger("astrbot")

# AI 调用参数
TEMPERATURE = 0.8
MAX_TOKENS = 2048
TIMEOUT_SECONDS = 120

# Mock 响应示例
MOCK_RESPONSE = """发售平台：PC / Switch / iOS / Android
游玩时间：断断续续玩了两年多
推荐人群：喜欢种田养老、温馨治愈风格的玩家

星露谷物语是一款像素风格的模拟经营游戏，你继承了爷爷留下的农场，从零开始打理一切。种地、钓鱼、挖矿、社交，每天都有做不完的事情，但节奏却出奇地让人放松。

我是被朋友安利入坑的，一开始觉得画面有点简陋，结果一玩就停不下来。最让我印象深刻的是和村民们的互动，每个人都有自己的故事，慢慢解锁他们的剧情线特别有成就感。冬天的时候窝在农场里整理仓库，听着背景音乐，感觉整个世界都安静下来了。

如果你想找个能玩很久又不会太累的游戏，星露谷物语绝对值得一试。"""


class AIClient:
    """AI API 客户端，兼容 OpenAI Chat Completions 格式"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "deepseek-chat"
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key"""
        return bool(self.api_key and self.base_url)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._client

    async def generate(self, prompt: str) -> str:
        """
        调用 AI 生成内容

        Args:
            prompt: 用户 prompt（由 build_main_prompt 生成）

        Returns:
            AI 生成的文本内容

        Raises:
            Exception: API 调用失败时抛出异常
        """
        if not self.is_configured:
            logger.warning("AI API 未配置，使用 Mock 模式")
            return MOCK_RESPONSE

        client = await self._get_client()
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }

        logger.info(f"调用 AI API: {self.model} @ {self.base_url}")

        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        content = data["choices"][0]["message"]["content"]
        logger.info(f"AI 响应长度: {len(content)} 字符")
        return content

    async def test_connection(self) -> dict:
        """
        测试 API 连接是否正常

        Returns:
            {"success": bool, "message": str, "model": str}
        """
        if not self.is_configured:
            return {
                "success": False,
                "message": "未配置 API Key 或 Base URL",
                "model": self.model,
            }

        try:
            client = await self._get_client()
            url = f"{self.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": "请回复'连接正常'四个字"},
                ],
                "temperature": 0,
                "max_tokens": 20,
            }

            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]

            return {
                "success": True,
                "message": f"模型服务正常，当前模型：{self.model}",
                "model": self.model,
            }
        except httpx.TimeoutException:
            return {
                "success": False,
                "message": "连接超时，请检查 API 地址是否正确",
                "model": self.model,
            }
        except httpx.HTTPStatusError as e:
            return {
                "success": False,
                "message": f"API 返回错误: HTTP {e.response.status_code}",
                "model": self.model,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"连接失败: {str(e)}",
                "model": self.model,
            }

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
