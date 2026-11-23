"""
模型来源与调用统一层。

支持：
- astrbot：使用 AstrBot 已配置的聊天 Provider，不复制 Key/Base URL；
- manual：使用插件内的 OpenAI 兼容配置（Chat Completions / Responses）。

文章生成、搜索汇总和模型测试均经过本层，避免界面配置与实际调用分叉。
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from .ai_client import (
    AIClient,
    AIClientAPIError,
    AIClientError,
    AIClientNotConfigured,
)
from .prompt import SYSTEM_PROMPT

MODEL_SOURCE_ASTRBOT = "astrbot"
MODEL_SOURCE_MANUAL = "manual"
MODEL_SOURCES = (MODEL_SOURCE_ASTRBOT, MODEL_SOURCE_MANUAL)

API_FORMAT_CHOICES = (
    "chat_completions",
    "responses",
    "anthropic_messages",
)

# 只列 Base URL 与接口格式；不内置“最新模型清单”或未经核实的上下文长度。
MANUAL_PRESETS = [
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "api_format": "chat_completions",
        "context_length": None,
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_format": "chat_completions",
        "context_length": None,
    },
    {
        "id": "mimo",
        "label": "Xiaomi MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_format": "chat_completions",
        "context_length": None,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_format": "chat_completions",
        "context_length": None,
    },
    {
        "id": "claude",
        "label": "Claude / Anthropic",
        "base_url": "https://api.anthropic.com",
        "api_format": "anthropic_messages",
        "context_length": None,
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_format": "chat_completions",
        "context_length": None,
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_format": "chat_completions",
        "context_length": None,
    },
]


class ModelServiceError(AIClientError):
    """模型配置/调用错误，user_message 可直接展示给用户。"""


def _safe_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelServiceError(
            "模型 Base URL 无效",
            "❌ 模型 Base URL 无效，请填写 http(s) 地址",
        )
    return text


def _is_loopback(host: str | None) -> bool:
    return str(host or "").lower() in {"localhost", "127.0.0.1", "::1"}


async def fetch_manual_models(
    base_url: str,
    api_key: str = "",
    timeout: float = 20.0,
    api_format: str = "chat_completions",
) -> list[str]:
    """请求各协议真实支持的模型列表；失败时明确报错，不伪造模型。"""
    base = _safe_base_url(base_url)
    parsed = urlparse(base)
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ModelServiceError(
            "模型 Base URL 必须使用 HTTPS",
            "❌ 非本机模型地址必须使用 HTTPS，避免 Key 明文传输",
        )
    api_format = str(api_format or "chat_completions").lower()
    headers = {"Accept": "application/json"}
    if api_format == "anthropic_messages":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        url = (
            f"{base}/models"
            if base.rstrip("/").endswith("/v1")
            else f"{base}/v1/models"
        )
    else:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = f"{base}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise ModelServiceError(
            "获取模型列表超时",
            "❌ 获取模型列表超时，请检查 Base URL 或稍后重试",
        ) from exc
    except httpx.RequestError as exc:
        raise ModelServiceError(
            "获取模型列表网络失败",
            "❌ 无法连接模型服务，请检查 Base URL、代理和网络",
        ) from exc
    if resp.status_code in (401, 403):
        raise ModelServiceError(
            f"获取模型列表未授权 HTTP {resp.status_code}",
            "❌ 获取模型列表失败：API Key 无效或无权限",
        )
    if resp.status_code == 404:
        raise ModelServiceError(
            "模型列表接口不存在",
            "❌ 该服务未提供 /models 接口，请手动填写模型名称",
        )
    if resp.status_code == 429:
        raise ModelServiceError(
            "获取模型列表限流",
            "❌ 获取模型列表被限流，请稍后重试",
        )
    if resp.status_code >= 500:
        raise ModelServiceError(
            f"模型列表服务异常 HTTP {resp.status_code}",
            "❌ 模型服务异常，请稍后重试",
        )
    if resp.status_code != 200:
        raise ModelServiceError(
            f"模型列表 HTTP {resp.status_code}",
            f"❌ 获取模型列表失败（HTTP {resp.status_code}）",
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise ModelServiceError(
            "模型列表响应不是 JSON",
            "❌ 模型服务返回内容无法解析",
        ) from exc
    raw_items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        raise ModelServiceError(
            "模型列表响应格式异常",
            "❌ 模型服务返回格式异常，未找到模型列表",
        )
    models: list[str] = []
    seen = set()
    for item in raw_items:
        model_id = item.get("id") if isinstance(item, dict) else item
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(model_id)
    if not models:
        raise ModelServiceError(
            "模型列表为空",
            "❌ 接口返回了空模型列表，请检查服务或手动填写模型名称",
        )
    return models


class ModelService:
    def __init__(self, context, config):
        self.context = context
        self.config = config
        self.manual_client: AIClient | None = None
        self.reload()

    async def reload_async(self) -> None:
        """重新读取配置，并关闭替换下来的手动 HTTP 客户端。"""
        previous = self.manual_client
        self.reload()
        if previous is not None:
            await previous.close()

    def reload(self) -> None:
        cfg = self.config.get("ai_config", {})
        if not isinstance(cfg, dict):
            cfg = {}
        self.cfg = dict(cfg)
        source = str(cfg.get("source") or "").strip().lower()
        self.source = (
            source if source in MODEL_SOURCES else MODEL_SOURCE_MANUAL
        )
        self.provider_id = str(
            cfg.get("astrbot_provider_id") or ""
        ).strip()
        base_url = str(cfg.get("base_url") or "")
        key = str(cfg.get("api_key") or "")
        origin = str(cfg.get("api_key_origin") or base_url)
        provider = str(cfg.get("provider_preset") or "custom")
        origin_provider = str(cfg.get("api_key_provider") or "")
        # 路径区分大小写；换了供应商也不能把旧 Key 发到新目标。
        scope_ok = (
            origin.rstrip("/") == base_url.rstrip("/")
            and (not origin_provider or origin_provider == provider)
        )
        effective_key = key if scope_ok else ""
        self.manual_client = AIClient(
            base_url=base_url,
            api_key=effective_key,
            model=cfg.get("model", "deepseek-chat"),
            api_format=cfg.get("api_format", "chat_completions"),
            send_temperature=cfg.get("send_temperature", True),
        )

    @property
    def is_configured(self) -> bool:
        if self.source == MODEL_SOURCE_ASTRBOT:
            # 必须显式选择已配置 Provider；不回退到“当前会话模型”。
            return bool(self.provider_id)
        return self.manual_client is not None and self.manual_client.is_configured

    def public_state(self) -> dict:
        key = str(self.cfg.get("api_key") or "")
        key_origin = str(
            self.cfg.get("api_key_origin") or self.cfg.get("base_url") or ""
        )
        provider = str(self.cfg.get("provider_preset") or "custom")
        origin_provider = str(self.cfg.get("api_key_provider") or "")
        key_usable = (
            bool(key)
            and key_origin.rstrip("/") == str(
                self.cfg.get("base_url") or ""
            ).rstrip("/")
            and (not origin_provider or origin_provider == provider)
        )
        manual = {
            "base_url": self.cfg.get("base_url", ""),
            "model": self.cfg.get("model", ""),
            "api_format": self.cfg.get("api_format", "chat_completions"),
            "send_temperature": self.cfg.get("send_temperature", True),
            "provider_preset": self.cfg.get("provider_preset", "custom"),
            "key_configured": bool(key),
            "key_masked": "••••••••" if key else "",
            "key_usable": key_usable,
            "key_origin": key_origin,
            "context_length": None,
        }
        return {
            "source": self.source,
            "astrbot_provider_id": self.provider_id,
            "manual": manual,
            "providers": self.list_providers(),
            "presets": MANUAL_PRESETS,
            "api_formats": list(API_FORMAT_CHOICES),
            "context_length": None,
        }

    def list_providers(self) -> list[dict]:
        if self.context is None:
            return []
        try:
            providers = self.context.get_all_providers()
        except Exception:
            return []
        items = []
        for provider in providers:
            try:
                meta = provider.meta()
                provider_type = str(
                    getattr(meta.provider_type, "value", meta.provider_type)
                )
                if provider_type != "chat_completion":
                    continue
                items.append({
                    "id": str(meta.id),
                    "model": str(meta.model or ""),
                    "type": str(meta.type or ""),
                    "label": f"{meta.model or meta.id}（{meta.id}）",
                })
            except Exception:
                continue
        return sorted(items, key=lambda item: item["id"])

    def _provider_by_id(self, provider_id: str):
        providers = self.context.get_all_providers() if self.context else []
        for provider in providers:
            try:
                if str(provider.meta().id) == provider_id:
                    return provider
            except Exception:
                continue
        return None

    async def _astrbot_provider(self, umo: str = ""):
        if self.context is None:
            raise ModelServiceError(
                "AstrBot 上下文不可用",
                "❌ 当前无法读取 AstrBot 模型，请检查插件运行环境",
            )
        if not self.provider_id:
            raise ModelServiceError(
                "未选择 AstrBot 模型",
                "❌ 请到模型配置页选择一个已配置的 AstrBot 模型",
            )
        provider = self._provider_by_id(self.provider_id)
        if provider is None:
            raise ModelServiceError(
                "AstrBot Provider 不存在或已停用",
                "❌ 所选 AstrBot 模型不存在或已停用，请到模型配置页重新选择",
            )
        return provider

    async def _astrbot_generate(
        self, prompt: str, umo: str = "", system_prompt: str = ""
    ) -> dict:
        start = time.monotonic()
        provider = await self._astrbot_provider(umo)
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=prompt,
                system_prompt=system_prompt or None,
            )
        except AIClientError:
            raise
        except Exception as exc:
            raise ModelServiceError(
                f"AstrBot 模型调用失败: {type(exc).__name__}",
                "❌ AstrBot 模型调用失败，请检查模型是否启用或稍后重试",
            ) from exc

        content = str(getattr(response, "completion_text", "") or "")
        if not content.strip():
            raise ModelServiceError(
                "AstrBot 模型返回空正文",
                "❌ AstrBot 模型未返回有效内容",
            )
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "input", 0) or 0) if usage else 0
        completion_tokens = int(getattr(usage, "output", 0) or 0) if usage else 0
        total_tokens = (
            int(getattr(usage, "total", 0) or 0) if usage else 0
        )
        return {
            "content": content,
            "model": str(provider.meta().model or provider.meta().id),
            "api_format": "astrbot",
            "duration_ms": int((time.monotonic() - start) * 1000),
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
            "token_available": usage is not None,
            "finish_reason": "",
            "truncated": False,
        }

    async def generate(self, prompt: str, umo: str = "") -> dict:
        if self.source == MODEL_SOURCE_ASTRBOT:
            return await self._astrbot_generate(
                prompt, umo=umo, system_prompt=SYSTEM_PROMPT
            )
        if self.manual_client is None:
            raise AIClientNotConfigured(
                "手动模型未初始化",
                "❌ 手动模型配置不可用，请到模型配置页检查",
            )
        return await self.manual_client.generate(prompt)

    async def summarize_search(
        self, game_name: str, raw_text: str, max_chars: int = 150,
        umo: str = "",
    ) -> dict:
        if self.source == MODEL_SOURCE_ASTRBOT:
            system = (
                "你是一个游戏资讯编辑。请根据提供的搜索资料，用中文写一段"
                f"简短的游戏信息汇总（{max_chars}字以内），包括游戏类型、"
                "开发商、平台、特色等关键信息。直接输出汇总内容，不要加标题或前缀。"
            )
            user = (
                f"以下是关于游戏「{game_name}」的搜索结果，请整理为一段简短的信息汇总："
                f"\n\n{raw_text}"
            )
            return await self._astrbot_generate(
                user, umo=umo, system_prompt=system
            )
        if self.manual_client is None:
            raise AIClientNotConfigured(
                "手动模型未初始化",
                "❌ 手动模型配置不可用，请到模型配置页检查",
            )
        return await self.manual_client.summarize_search(
            game_name, raw_text, max_chars
        )

    async def test_connection(self, umo: str = "") -> dict:
        if self.source == MODEL_SOURCE_ASTRBOT:
            try:
                result = await self._astrbot_generate(
                    "请只回复：连接正常",
                    umo=umo,
                    system_prompt="你是一个连接测试助手。",
                )
                return {
                    "success": True,
                    "message": f"模型服务正常，当前模型：{result['model']}（AstrBot）",
                    "model": result["model"],
                    "token_usage": result.get("token_usage"),
                    "token_available": bool(result.get("token_available")),
                }
            except AIClientError as exc:
                return {
                    "success": False,
                    "message": exc.user_message.removeprefix("❌ "),
                    "model": self.provider_id or "AstrBot 当前模型",
                }
        if self.manual_client is None:
            return {
                "success": False,
                "message": "手动模型未初始化",
                "model": "",
            }
        return await self.manual_client.test_connection()

    async def close(self) -> None:
        if self.manual_client is not None:
            await self.manual_client.close()
