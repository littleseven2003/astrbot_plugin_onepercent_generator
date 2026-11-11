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

# Responses API 的 incomplete_details.reason 到用户可理解提示的映射。
# 仅使用官方枚举值，不拼接第三方响应中的原始文本。
INCOMPLETE_REASON_MESSAGES = {
    "max_output_tokens": "❌ AI 回答达到最大输出长度而被截断，请重试或缩短输入内容",
    "max_messages": "❌ AI 回答达到最大消息数限制而中断，请重试",
    "content_filter": "❌ AI 回答因内容安全过滤而中断，请调整输入后重试",
    "steered": "❌ AI 回答被中途调整而中断，请重试",
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


class AIClientIncomplete(AIClientError):
    """Responses API 明确返回 incomplete，正文不完整"""


class AIClientResponseFailed(AIClientError):
    """Responses API 返回 failed、取消或未完成等不可用状态"""


class AIClientEmptyResponse(AIClientError):
    """请求成功但服务未返回可用正文"""


class AIClient:
    """AI API 客户端，兼容 OpenAI Chat Completions 与 Responses 格式"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = API_FORMAT_CHAT_COMPLETIONS,
        send_temperature: bool = True,
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
        # 默认发送 temperature，保持旧配置行为；非布尔值回退为 True。
        # 部分推理模型不接受 temperature，可由配置显式关闭。
        self.send_temperature = (
            send_temperature if isinstance(send_temperature, bool) else True
        )
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
        """根据配置构造 API 地址与请求体。

        send_temperature 为 False 时，两种 API 格式都不发送 temperature，
        其余字段保持不变。
        """
        if self.api_format == API_FORMAT_RESPONSES:
            payload = {
                "model": self.model,
                "instructions": system_prompt,
                "input": user_prompt,
                "max_output_tokens": max_tokens,
                "store": False,
            }
            if self.send_temperature:
                payload["temperature"] = temperature
            return f"{self.base_url}/responses", payload

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
        if self.send_temperature:
            payload["temperature"] = temperature
        return f"{self.base_url}/chat/completions", payload

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
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content_item in item.get("content") or []:
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

    @staticmethod
    def _response_status(data: dict) -> str:
        """读取 Responses API 顶层 status，缺失时返回空字符串。"""
        raw = data.get("status")
        return raw.strip().lower() if isinstance(raw, str) else ""

    @staticmethod
    def _incomplete_reason(data: dict) -> str:
        """读取 incomplete_details.reason，缺失或格式异常时返回空字符串。"""
        details = data.get("incomplete_details")
        if isinstance(details, dict):
            reason = details.get("reason")
            if isinstance(reason, str):
                return reason.strip()
        return ""

    @classmethod
    def _has_incomplete_marker(cls, data: dict) -> bool:
        """无顶层 status 时，识别显式的 incomplete 标记。"""
        if cls._incomplete_reason(data):
            return True
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("status") == "incomplete":
                return True
        return False

    def _check_response_status(self, data: dict) -> None:
        """校验 Responses API 响应状态，避免把不完整正文当作成功。

        兼容行为：部分兼容服务不返回 status。此时保持旧行为，只要正文可解析
        即视为成功；但若存在显式 error 或 incomplete_details/output 标记，
        仍按失败或不完整处理。
        """
        if self.api_format != API_FORMAT_RESPONSES:
            return

        status = self._response_status(data)
        if not status:
            if data.get("error"):
                self._raise_response_failed(data)
            if self._has_incomplete_marker(data):
                self._raise_response_incomplete(data)
            return

        if status == "completed":
            return
        if status == "incomplete":
            self._raise_response_incomplete(data)
        if status == "failed":
            self._raise_response_failed(data)
        if status in {"cancelled", "canceled"}:
            logger.warning("[小作文生成器] Responses API 返回状态: %s", status)
            raise AIClientResponseFailed(
                f"Responses API status={status}",
                "❌ AI 回答已被取消，请稍后重试",
            )
        if status in {"queued", "in_progress"}:
            logger.warning("[小作文生成器] Responses API 返回状态: %s", status)
            raise AIClientResponseFailed(
                f"Responses API status={status}",
                "❌ AI 回答尚未完成，请稍后重试",
            )

        logger.warning("[小作文生成器] Responses API 返回未知状态: %r", status)
        raise AIClientResponseFailed(
            f"Responses API status={status}",
            "❌ AI 服务返回未知状态，请稍后重试",
        )

    def _raise_response_incomplete(self, data: dict) -> None:
        reason = self._incomplete_reason(data)
        user_message = INCOMPLETE_REASON_MESSAGES.get(
            reason,
            "❌ AI 回答不完整，请重试",
        )
        logger.warning(
            "[小作文生成器] Responses API 返回 incomplete，原因: %s",
            reason or "unknown",
        )
        raise AIClientIncomplete(
            f"Responses API incomplete (reason={reason or 'unknown'})",
            user_message,
        )

    def _raise_response_failed(self, data: dict) -> None:
        # 只保留错误码字符，不记录或回显第三方 error.message，避免敏感信息泄漏。
        code = ""
        error = data.get("error")
        if isinstance(error, dict):
            raw_code = error.get("code")
            if isinstance(raw_code, str):
                code = "".join(
                    ch for ch in raw_code if ch.isalnum() or ch in "_.-"
                )[:64]
        logger.warning(
            "[小作文生成器] Responses API 返回 failed，错误码: %s",
            code or "unknown",
        )
        raise AIClientResponseFailed(
            f"Responses API failed (code={code or 'unknown'})",
            "❌ AI 服务返回失败，请稍后重试",
        )

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

            self._check_response_status(data)
            try:
                content = self._extract_content(data)
            except ValueError as e:
                logger.error("[小作文生成器] AI 响应没有可用正文: %s", e)
                raise AIClientEmptyResponse(
                    f"AI 响应正文为空: {e}",
                    "❌ AI 服务未返回有效内容，请稍后重试",
                )
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

        except AIClientError:
            raise

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
