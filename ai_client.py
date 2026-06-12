"""
AI API 调用模块
封装 OpenAI 格式的 API 调用
"""

import logging
import time

import httpx

from .prompt import SYSTEM_PROMPT

logger = logging.getLogger("astrbot")

# AI 调用参数
TEMPERATURE = 0.8
MAX_TOKENS = 2048
TIMEOUT_SECONDS = 120


class AIClientError(Exception):
    """AI 客户端错误基类"""

    def __init__(self, message: str, user_message: str = ""):
        super().__init__(message)
        self.user_message = user_message or message


class AIClientNotConfigured(AIClientError):
    """未配置 AI 模型信息"""


class AIClientTimeout(AIClientError):
    """请求超时"""


class AIClientAPIError(AIClientError):
    """API 返回错误"""


class AIClient:
    """AI API 客户端，兼容 OpenAI Chat Completions 格式"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "deepseek-chat"
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key 和 Base URL"""
        return bool(self.api_key and self.base_url)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._client

    async def generate(self, prompt: str) -> dict:
        """
        调用 AI 生成内容

        Args:
            prompt: 用户 prompt（由 build_main_prompt 生成）

        Returns:
            {
                "content": str,       # AI 生成的文本
                "model": str,         # 模型名称
                "duration_ms": int,   # 生成耗时（毫秒）
                "token_usage": {      # token 消耗
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "total_tokens": int,
                }
            }

        Raises:
            AIClientNotConfigured: 未配置模型信息
            AIClientTimeout: 请求超时
            AIClientAPIError: API 返回错误
            AIClientError: 其他调用错误
        """
        if not self.is_configured:
            msg = "未配置 AI 模型信息（Base URL 或 API Key 为空）"
            logger.error(f"[小作文生成器] {msg}")
            raise AIClientNotConfigured(
                msg,
                "❌ 小作文功能未配置 AI 模型，请在插件配置页面填写 API Base URL 和 API Key",
            )

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

        logger.info(f"[小作文生成器] 调用 AI API: {self.model} @ {self.base_url}")
        start_time = time.time()

        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            content = data["choices"][0]["message"]["content"]
            duration_ms = int((time.time() - start_time) * 1000)

            # 提取 token 用量
            usage = data.get("usage", {})
            token_usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }

            logger.info(
                f"[小作文生成器] AI 响应成功，长度: {len(content)} 字符，"
                f"耗时: {duration_ms}ms，tokens: {token_usage['total_tokens']}"
            )
            return {
                "content": content,
                "model": self.model,
                "duration_ms": duration_ms,
                "token_usage": token_usage,
            }

        except httpx.TimeoutException:
            msg = f"AI 请求超时（{TIMEOUT_SECONDS}s）: {self.model} @ {self.base_url}"
            logger.error(f"[小作文生成器] {msg}")
            raise AIClientTimeout(
                msg,
                f"❌ AI 生成超时（{TIMEOUT_SECONDS}秒），请稍后重试",
            )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            msg = f"AI API 返回 HTTP {status}: {self.model} @ {self.base_url}"
            logger.error(f"[小作文生成器] {msg}")
            detail = ""
            if status == 401:
                detail = "API Key 无效，请检查配置"
            elif status == 403:
                detail = "API 访问被拒绝，请检查 API Key 权限"
            elif status == 429:
                detail = "API 请求频率超限，请稍后重试"
            elif status >= 500:
                detail = "AI 服务端异常，请稍后重试"
            else:
                detail = f"HTTP {status} 错误"
            raise AIClientAPIError(
                msg,
                f"❌ AI 调用失败：{detail}",
            )

        except Exception as e:
            msg = f"AI 调用异常: {type(e).__name__}: {e}"
            logger.error(f"[小作文生成器] {msg}")
            raise AIClientError(
                msg,
                f"❌ AI 生成失败：{str(e)}",
            )

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
