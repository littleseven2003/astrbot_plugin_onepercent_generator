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

API_FORMAT_CHAT_COMPLETIONS = "chat_completions"
API_FORMAT_RESPONSES = "responses"
SUPPORTED_API_FORMATS = {
    API_FORMAT_CHAT_COMPLETIONS,
    API_FORMAT_RESPONSES,
}


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
    """AI API 客户端，兼容 OpenAI Chat Completions 与 Responses 格式"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = API_FORMAT_CHAT_COMPLETIONS,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "deepseek-chat"
        normalized_format = str(
            api_format or API_FORMAT_CHAT_COMPLETIONS
        ).strip().lower()
        if normalized_format not in SUPPORTED_API_FORMATS:
            logger.warning(
                "[小作文生成器] 未知 API 格式 %r，回退到 Chat Completions",
                api_format,
            )
            normalized_format = API_FORMAT_CHAT_COMPLETIONS
        self.api_format = normalized_format
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key 和 Base URL"""
        return bool(self.api_key and self.base_url)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._client

    def _build_request(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict]:
        """根据配置构造 API 地址与请求体。"""
        if self.api_format == API_FORMAT_RESPONSES:
            return f"{self.base_url}/responses", {
                "model": self.model,
                "instructions": system_prompt,
                "input": user_prompt,
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "store": False,
            }

        return f"{self.base_url}/chat/completions", {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    def _extract_content(self, data: dict) -> str:
        """从不同 API 格式的响应中提取最终文本。"""
        if self.api_format == API_FORMAT_CHAT_COMPLETIONS:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Chat Completions 响应正文为空")
            return content

        # 部分兼容服务会直接返回 SDK 风格的 output_text。
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        text_parts = []
        for item in data.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content_item in item.get("content", []):
                if not isinstance(content_item, dict):
                    continue
                if content_item.get("type") not in {"output_text", "text"}:
                    continue
                text = content_item.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)

        if not text_parts:
            raise ValueError("Responses API 响应中没有可用的文本输出")
        return "\n".join(text_parts)

    def _extract_token_usage(self, data: dict) -> dict:
        """将不同 API 格式的 Token 统计归一化。"""
        usage = data.get("usage") or {}
        if self.api_format == API_FORMAT_RESPONSES:
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
        else:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": usage.get(
                "total_tokens",
                prompt_tokens + completion_tokens,
            ),
        }

    async def generate(self, prompt: str) -> dict:
        """
        调用 AI 生成内容（通用方法）

        Args:
            prompt: 用户 prompt

        Returns:
            {
                "content": str,
                "model": str,
                "api_format": str,
                "duration_ms": int,
                "token_usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
            }
        """
        return await self._call_ai(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )

    async def summarize_search(self, game_name: str, raw_text: str, max_chars: int = 150) -> dict:
        """
        调用 AI 对搜索结果进行分析整理，生成简短的搜索汇总

        Args:
            game_name: 游戏名称
            raw_text: 搜索引擎返回的原始文本
            max_chars: 汇总最大字数

        Returns:
            同 generate() 返回结构，content 为整理后的搜索汇总
        """
        system = f"你是一个游戏资讯编辑。请根据提供的搜索资料，用中文写一段简短的游戏信息汇总（{max_chars}字以内），包括游戏类型、开发商、平台、特色等关键信息。直接输出汇总内容，不要加标题或前缀。"
        user = f"以下是关于游戏「{game_name}」的搜索结果，请整理为一段简短的信息汇总：\n\n{raw_text}"

        return await self._call_ai(
            system_prompt=system,
            user_prompt=user,
            temperature=0.3,
            max_tokens=300,
        )

    async def _call_ai(self, system_prompt: str, user_prompt: str,
                       temperature: float = 0.8, max_tokens: int = 2048) -> dict:
        """
        底层 AI 调用
        """
        if not self.is_configured:
            msg = "未配置 AI 模型信息（Base URL 或 API Key 为空）"
            logger.error(f"[小作文生成器] {msg}")
            raise AIClientNotConfigured(
                msg,
                "❌ 小作文功能未配置 AI 模型，请在插件配置页面填写 API Base URL 和 API Key",
            )

        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url, payload = self._build_request(
            system_prompt,
            user_prompt,
            temperature,
            max_tokens,
        )

        logger.info(
            f"[小作文生成器] 调用 AI API: {self.model} "
            f"({self.api_format}) @ {self.base_url}"
        )
        start_time = time.time()

        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            content = self._extract_content(data)
            duration_ms = int((time.time() - start_time) * 1000)

            token_usage = self._extract_token_usage(data)

            logger.info(
                f"[小作文生成器] AI 响应成功，长度: {len(content)} 字符，"
                f"耗时: {duration_ms}ms，tokens: {token_usage['total_tokens']}"
            )
            return {
                "content": content,
                "model": data.get("model", self.model),
                "api_format": self.api_format,
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
            msg = (
                f"AI API 返回 HTTP {status}: {self.model} "
                f"({self.api_format}) @ {self.base_url}"
            )
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
            await self._call_ai(
                system_prompt="你是一个连接测试助手。",
                user_prompt="请回复'连接正常'四个字",
                temperature=0,
                max_tokens=20,
            )

            return {
                "success": True,
                "message": (
                    f"模型服务正常，当前模型：{self.model}，"
                    f"API 格式：{self.api_format}"
                ),
                "model": self.model,
            }
        except AIClientError as e:
            return {
                "success": False,
                "message": e.user_message.removeprefix("❌ "),
                "model": self.model,
            }

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
