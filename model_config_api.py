"""模型配置视图的认证 Web API。"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from .model_service import (
    API_FORMAT_CHOICES,
    MANUAL_PRESETS,
    MODEL_SOURCE_ASTRBOT,
    MODEL_SOURCE_MANUAL,
    MODEL_SOURCES,
    ModelService,
    fetch_manual_models,
)
from .ai_client import AIClientError

logger = logging.getLogger("astrbot")

_MASK_PREFIX = "•"


def _error(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _validate_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not (text.startswith("http://") or text.startswith("https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    return text


def _norm_base(value: str) -> str:
    """比较目标地址时只规范化 scheme/host/默认端口，保留路径大小写。

    路径在多数网关中区分大小写，/tenant/A 与 /tenant/a 可能是不同凭据域。
    """
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.hostname:
        return text
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return text
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _key_scope_matches(
    existing: dict, base_url: str, provider_preset: str = ""
) -> bool:
    origin = str(
        existing.get("api_key_origin") or existing.get("base_url") or ""
    )
    if not origin or _norm_base(origin) != _norm_base(base_url):
        return False
    origin_provider = str(
        existing.get("api_key_provider")
        or existing.get("provider_preset")
        or "custom"
    ).strip()
    requested = str(provider_preset or "custom").strip()
    # 旧配置没有 provider 字段时按 custom 处理；一旦切换到具名供应商，
    # 即使地址相同也不自动复用旧 Key。
    if origin_provider != requested:
        return False
    return True


def _preserve_secret(
    payload: dict,
    existing: dict,
    base_url: str,
    provider_preset: str = "",
) -> str:
    current = str(existing.get("api_key") or "")
    if payload.get("clear_key"):
        return ""
    raw = payload.get("api_key")
    if not isinstance(raw, str):
        raw = ""
    raw = raw.strip()
    if raw and not raw.startswith(_MASK_PREFIX):
        return raw
    # 留空/遮罩只在同一服务地址、同一供应商内复用；换目标绝不发送旧 Key。
    return (
        current
        if _key_scope_matches(existing, base_url, provider_preset)
        else ""
    )


class ModelConfigAPI:
    def __init__(self, plugin):
        self.plugin = plugin

    async def state(self):
        return self.plugin.model_service.public_state()

    async def save(self):
        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        try:
            cfg = self._merge_saved(payload)
        except ValueError as exc:
            return _error(str(exc))
        self.plugin.config["ai_config"] = cfg
        try:
            saver = getattr(self.plugin.config, "save_config_async", None)
            if saver is not None:
                await saver()
            else:
                sync_saver = getattr(self.plugin.config, "save_config", None)
                if sync_saver is not None:
                    sync_saver()
        except Exception:
            logger.exception("[小作文生成器] 保存模型配置失败")
            return _error("保存配置失败，请查看 AstrBot 日志")
        await self.plugin.model_service.reload_async()
        if self.plugin.ai_client is not self.plugin.model_service:
            self.plugin.ai_client = self.plugin.model_service
        return {
            "saved": True,
            "state": self.plugin.model_service.public_state(),
        }

    async def models(self):
        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        if str(payload.get("source") or "").lower() == MODEL_SOURCE_ASTRBOT:
            return _error("AstrBot 模型由 AstrBot 提供商管理，无需获取远程 /models")
        manual = payload.get("manual")
        if not isinstance(manual, dict):
            manual = payload
        base_url = _validate_url(manual.get("base_url") or "")
        if not base_url:
            return _error("请先填写 Base URL")
        api_format = str(
            manual.get("api_format") or "chat_completions"
        ).strip().lower()
        key = self._draft_key(payload, base_url)
        try:
            models = await fetch_manual_models(
                base_url, key, api_format=api_format
            )
        except (ValueError, AIClientError) as exc:
            message = getattr(exc, "user_message", "") or str(exc)
            return _error(message.removeprefix("❌ "))
        return {"models": models, "context_length": None}

    async def test(self):
        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        try:
            cfg = self._merge_draft(payload)
        except ValueError as exc:
            return _error(str(exc))
        service = ModelService(self.plugin.context, {"ai_config": cfg})
        try:
            result = await service.test_connection(
                umo=str(payload.get("umo") or "")
            )
        finally:
            await service.close()
        if not result.get("success"):
            return _error(result.get("message") or "模型测试失败")
        return {
            "success": True,
            "message": result.get("message", "模型测试成功"),
            "model": result.get("model", ""),
        }

    def _draft_key(self, payload: dict, base_url: str) -> str:
        existing = self.plugin.config.get("ai_config", {}) or {}
        manual = payload.get("manual")
        if not isinstance(manual, dict):
            manual = payload
        preset = str(
            manual.get("provider_preset")
            or existing.get("provider_preset", "custom")
        ).strip()
        return _preserve_secret(manual, existing, base_url, preset)

    def _merge_draft(self, payload: dict) -> dict:
        existing = self.plugin.config.get("ai_config", {}) or {}
        return self._build(payload, existing)

    def _merge_saved(self, payload: dict) -> dict:
        existing = self.plugin.config.get("ai_config", {}) or {}
        return self._build(payload, existing)

    def _build(self, payload: dict, existing: dict) -> dict:
        source = str(payload.get("source") or "").strip().lower()
        if source not in MODEL_SOURCES:
            raise ValueError("请选择有效的模型来源")
        cfg = dict(existing)
        cfg["source"] = source
        cfg["astrbot_provider_id"] = str(
            payload.get("astrbot_provider_id") or ""
        ).strip()
        if source == MODEL_SOURCE_ASTRBOT and not cfg["astrbot_provider_id"]:
            raise ValueError("请选择一个已配置的 AstrBot 模型")
        manual_payload = payload.get("manual")
        if not isinstance(manual_payload, dict):
            manual_payload = payload
        cfg["base_url"] = _validate_url(
            manual_payload.get("base_url", cfg.get("base_url", ""))
        )
        preset = str(
            manual_payload.get(
                "provider_preset", cfg.get("provider_preset", "custom")
            )
        ).strip()
        preset_ids = {item["id"] for item in MANUAL_PRESETS}
        cfg["provider_preset"] = preset if preset in preset_ids else "custom"
        cfg["api_key"] = _preserve_secret(
            manual_payload, existing, cfg["base_url"],
            cfg["provider_preset"],
        )
        cfg["api_key_origin"] = (
            cfg["base_url"] if cfg["api_key"] else ""
        )
        cfg["api_key_provider"] = (
            cfg["provider_preset"] if cfg["api_key"] else ""
        )
        cfg["model"] = str(
            manual_payload.get("model", cfg.get("model", ""))
        ).strip()
        api_format = str(
            manual_payload.get(
                "api_format", cfg.get("api_format", "chat_completions")
            )
        ).strip().lower()
        if api_format not in API_FORMAT_CHOICES:
            raise ValueError("API 格式无效")
        cfg["api_format"] = api_format
        if "send_temperature" in manual_payload:
            cfg["send_temperature"] = bool(
                manual_payload.get("send_temperature")
            )
        else:
            cfg["send_temperature"] = bool(
                cfg.get("send_temperature", True)
            )
        return cfg
