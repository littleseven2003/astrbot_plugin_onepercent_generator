# -*- coding: utf-8 -*-
"""模型来源统一层与模型配置 API 测试（无真实付费请求）。"""

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from astrbot_plugin_onepercent_generator.model_service import (
    ModelService,
    ModelServiceError,
    fetch_manual_models,
)
from astrbot_plugin_onepercent_generator.model_config_api import ModelConfigAPI

try:
    from astrbot.api.web import PluginRequest, bind_request_context
    ASTRBOT_AVAILABLE = True
except Exception:
    PluginRequest = None
    bind_request_context = None
    ASTRBOT_AVAILABLE = False


class FakeMeta:
    def __init__(self, provider_id, model):
        self.id = provider_id
        self.model = model
        self.type = "openai_chat_completion"
        self.provider_type = "chat_completion"


class FakeProvider:
    def __init__(self, provider_id, model):
        self._meta = FakeMeta(provider_id, model)

    def meta(self):
        return self._meta


class FakeUsage:
    input = 10
    output = 20
    total = 30


class FakeResponse:
    completion_text = "AstrBot 生成正文"
    usage = FakeUsage()


class FakeModelContext:
    def __init__(self, providers=None, current=None):
        self.providers = providers or []
        self.current = current
        self.calls = []

    def get_all_providers(self):
        return list(self.providers)

    async def get_using_provider_async(self, umo=None):
        return self.current

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse()


class ModelServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_astrbot_provider_selection_and_umo(self):
        provider = FakeProvider("p1", "model-1")
        context = FakeModelContext([provider])
        service = ModelService(context, {
            "ai_config": {"source": "astrbot", "astrbot_provider_id": "p1"}
        })

        result = await service.generate("写文章", umo="platform:FriendMessage:u1")

        self.assertEqual(result["content"], "AstrBot 生成正文")
        self.assertEqual(result["model"], "model-1")
        self.assertEqual(result["api_format"], "astrbot")
        self.assertEqual(result["token_usage"]["total_tokens"], 30)
        self.assertEqual(context.calls[0]["chat_provider_id"], "p1")
        self.assertEqual(service.public_state()["providers"][0]["id"], "p1")

    async def test_astrbot_without_selected_provider_errors(self):
        provider = FakeProvider("current", "model-current")
        context = FakeModelContext([provider], current=provider)
        service = ModelService(context, {"ai_config": {"source": "astrbot"}})

        with self.assertRaises(ModelServiceError):
            await service.generate("写文章", umo="umo-1")
        self.assertEqual(context.calls, [])

    async def test_missing_selected_provider_never_falls_back(self):
        context = FakeModelContext([])
        service = ModelService(context, {
            "ai_config": {"source": "astrbot", "astrbot_provider_id": "missing"}
        })

        with self.assertRaises(ModelServiceError):
            await service.generate("写文章")
        self.assertEqual(context.calls, [])

    async def test_manual_source_preserves_legacy_config(self):
        service = ModelService(None, {
            "ai_config": {
                "base_url": "https://example.com/v1",
                "api_key": "sk-test",
                "model": "m",
                "api_format": "chat_completions",
            }
        })
        self.assertEqual(service.source, "manual")
        self.assertTrue(service.is_configured)
        state = service.public_state()
        self.assertTrue(state["manual"]["key_configured"])
        self.assertNotIn("sk-test", json.dumps(state))

    async def test_service_error_does_not_expose_key(self):
        service = ModelService(None, {
            "ai_config": {
                "source": "astrbot",
                "astrbot_provider_id": "missing",
            }
        })
        with self.assertRaises(ModelServiceError) as ctx:
            await service.generate("x")
        self.assertNotIn("sk-", str(ctx.exception))


class FetchModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_models_parses_and_deduplicates(self):
        class Response:
            status_code = 200
            def json(self):
                return {"data": [{"id": "m1"}, {"id": "m1"}, {"id": "m2"}, {}]}

        class Client:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def get(self, url, headers):
                return Response()

        with patch(
            "astrbot_plugin_onepercent_generator.model_service.httpx.AsyncClient",
            Client,
        ):
            models = await fetch_manual_models(
                "https://example.com/v1", "sk-secret"
            )
        self.assertEqual(models, ["m1", "m2"])

    async def test_fetch_models_unauthorized_error_hides_key(self):
        class Response:
            status_code = 401
            def json(self):
                return {}
        class Client:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def get(self, url, headers):
                return Response()

        with patch(
            "astrbot_plugin_onepercent_generator.model_service.httpx.AsyncClient",
            Client,
        ):
            with self.assertRaises(ModelServiceError) as ctx:
                await fetch_manual_models(
                    "https://example.com/v1", "sk-secret"
                )
        self.assertNotIn("sk-secret", ctx.exception.user_message)


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0
    async def save_config_async(self):
        self.saved += 1


class FakePlugin:
    def __init__(self, config):
        self.config = config
        self.context = FakeModelContext([FakeProvider("p1", "m1")])
        self.model_service = ModelService(self.context, config)
        self.ai_client = self.model_service


def fake_request(method="POST", body=None):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path="/api/v1/plugins/extensions/x/model-config/state"),
        headers={},
        cookies={},
        client=SimpleNamespace(host="127.0.0.1"),
        query_params=SimpleNamespace(multi_items=lambda: []),
        json=unittest.mock.AsyncMock(return_value=body or {}),
        body=unittest.mock.AsyncMock(return_value=b""),
        form=unittest.mock.AsyncMock(return_value={}),
        files=unittest.mock.AsyncMock(return_value={}),
    )


@unittest.skipUnless(ASTRBOT_AVAILABLE, "需要真实 AstrBot Web API")
class ModelConfigAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_key_is_masked_and_blank_preserves_existing(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://example.com/v1",
            "api_key": "sk-old",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        payload = {
            "source": "manual",
            "manual": {
                "base_url": "https://example.com/v1",
                "model": "m2",
                "api_key": "",
                "api_format": "chat_completions",
            },
        }
        with bind_request_context(PluginRequest(fake_request(body=payload))):
            result = await api.save()

        self.assertTrue(result["saved"])
        self.assertEqual(config["ai_config"]["api_key"], "sk-old")
        self.assertNotIn("sk-old", json.dumps(result))
        self.assertTrue(result["state"]["manual"]["key_configured"])
        self.assertTrue(result["state"]["manual"]["key_masked"])

    async def test_clear_and_replace_key(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://example.com/v1",
            "api_key": "sk-old",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        replace = {"source": "manual", "manual": {
            "base_url": "https://example.com/v1",
            "model": "m2", "api_key": "sk-new",
        }}
        with bind_request_context(PluginRequest(fake_request(body=replace))):
            await api.save()
        self.assertEqual(config["ai_config"]["api_key"], "sk-new")
        clear = {"source": "manual", "manual": {
            "base_url": "https://example.com/v1",
            "model": "m2", "clear_key": True,
        }}
        with bind_request_context(PluginRequest(fake_request(body=clear))):
            await api.save()
        self.assertEqual(config["ai_config"]["api_key"], "")

    async def test_masked_value_is_not_saved_as_key(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://example.com/v1",
            "api_key": "sk-old",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        payload = {"source": "manual", "manual": {
            "base_url": "https://example.com/v1",
            "model": "m2", "api_key": "••••••••",
        }}
        with bind_request_context(PluginRequest(fake_request(body=payload))):
            await api.save()
        self.assertEqual(config["ai_config"]["api_key"], "sk-old")

    async def test_cross_address_blank_does_not_inherit_key(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://old.example/v1",
            "api_key": "sk-old",
            "api_key_origin": "https://old.example/v1",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        payload = {"source": "manual", "manual": {
            "base_url": "https://new.example/v1",
            "model": "m1", "api_key": "",
            "provider_preset": "custom",
            "api_format": "chat_completions",
        }}
        with bind_request_context(PluginRequest(fake_request(body=payload))):
            result = await api.save()
        self.assertTrue(result["saved"])
        self.assertEqual(config["ai_config"]["api_key"], "")
        self.assertEqual(config["ai_config"]["api_key_origin"], "")
        self.assertFalse(result["state"]["manual"]["key_configured"])

    async def test_same_address_blank_still_preserves_key(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://same.example/v1",
            "api_key": "sk-old",
            "api_key_origin": "https://same.example/v1",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        payload = {"source": "manual", "manual": {
            "base_url": "https://same.example/v1/",
            "model": "m1", "api_key": "",
        }}
        with bind_request_context(PluginRequest(fake_request(body=payload))):
            await api.save()
        self.assertEqual(config["ai_config"]["api_key"], "sk-old")

    async def test_runtime_does_not_send_old_key_to_new_address(self):
        config = {
            "ai_config": {
                "source": "manual",
                "base_url": "https://new.example/v1",
                "api_key": "sk-old",
                "api_key_origin": "https://old.example/v1",
                "model": "m1",
            }
        }
        service = ModelService(None, config)
        self.assertEqual(service.manual_client.api_key, "")
        self.assertFalse(service.is_configured)

    async def test_anthropic_model_discovery_uses_headers_and_path(self):
        seen = {}

        async def handler(request):
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("x-api-key")
            seen["version"] = request.headers.get("anthropic-version")
            return httpx.Response(200, json={"data": [
                {"id": "claude-a"}, {"id": "claude-b"}
            ]})

        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            return original(transport=httpx.MockTransport(handler))

        with patch(
            "astrbot_plugin_onepercent_generator.model_service.httpx.AsyncClient",
            factory,
        ):
            models = await fetch_manual_models(
                "https://api.anthropic.com",
                "sk-ant",
                api_format="anthropic_messages",
            )
        self.assertEqual(models, ["claude-a", "claude-b"])
        self.assertEqual(seen["path"], "/v1/models")
        self.assertEqual(seen["key"], "sk-ant")
        self.assertEqual(seen["version"], "2023-06-01")

    async def test_models_reads_nested_manual_draft(self):
        config = FakeConfig({"ai_config": {
            "source": "manual", "base_url": "https://old/v1",
            "api_key": "sk-saved", "model": "old",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        seen = {}

        async def fake_fetch(base_url, key, **kwargs):
            seen["base_url"] = base_url
            seen["key"] = key
            return ["m1", "m2"]

        payload = {"source": "manual", "manual": {
            "base_url": "https://new/v1", "api_key": "",
            "model": "m1",
        }}
        with patch(
            "astrbot_plugin_onepercent_generator.model_config_api.fetch_manual_models",
            fake_fetch,
        ), bind_request_context(PluginRequest(fake_request(body=payload))):
            result = await api.models()

        self.assertEqual(result["models"], ["m1", "m2"])
        self.assertEqual(seen["base_url"], "https://new/v1")
        self.assertEqual(seen["key"], "")

    async def test_invalid_source_is_rejected(self):
        config = FakeConfig({"ai_config": {}})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        with bind_request_context(PluginRequest(fake_request(body={"source": "bad"}))):
            result = await api.save()
        self.assertEqual(result["status"], "error")
        self.assertFalse(plugin.config.saved)


if __name__ == "__main__":
    unittest.main()


class KeyScopeNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_norm_base_keeps_path_case(self):
        from astrbot_plugin_onepercent_generator.model_config_api import _norm_base

        self.assertNotEqual(
            _norm_base("https://example.com/tenant/A/v1"),
            _norm_base("https://example.com/tenant/a/v1"),
        )
        self.assertEqual(
            _norm_base("HTTPS://Example.COM:443/tenant/A/v1/"),
            _norm_base("https://example.com/tenant/A/v1"),
        )

    @unittest.skipUnless(ASTRBOT_AVAILABLE, "需要真实 AstrBot Web 请求上下文")
    async def test_models_request_uses_key_only_for_exact_path_scope(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://example.com/tenant/A/v1",
            "api_key": "sk-old",
            "api_key_origin": "https://example.com/tenant/A/v1",
            "provider_preset": "custom",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        seen = []

        async def fake_fetch(base_url, key, api_format="chat_completions"):
            seen.append((base_url, key))
            return ["m1"]

        payload = {"source": "manual", "manual": {
            "base_url": "https://example.com/tenant/a/v1",
            "api_key": "",
            "provider_preset": "custom",
            "api_format": "chat_completions",
        }}
        with patch(
            "astrbot_plugin_onepercent_generator.model_config_api.fetch_manual_models",
            side_effect=fake_fetch,
        ), bind_request_context(PluginRequest(fake_request(body=payload))):
            result = await api.models()
        self.assertEqual(result["models"], ["m1"])
        self.assertEqual(seen[0][1], "")

        with patch(
            "astrbot_plugin_onepercent_generator.model_config_api.fetch_manual_models",
            side_effect=fake_fetch,
        ), bind_request_context(PluginRequest(fake_request(body=payload))):
            await api.save()
        self.assertEqual(config["ai_config"]["api_key"], "")
        self.assertEqual(config["ai_config"]["api_key_origin"], "")
        service = ModelService(None, config)
        self.assertEqual(service.manual_client.api_key, "")
        self.assertFalse(service.is_configured)

    @unittest.skipUnless(ASTRBOT_AVAILABLE, "需要真实 AstrBot Web 请求上下文")
    async def test_same_path_and_provider_preserves_key_for_models(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://example.com/v1",
            "api_key": "sk-old",
            "api_key_origin": "https://example.com/v1",
            "provider_preset": "deepseek",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        seen = []

        async def fake_fetch(base_url, key, api_format="chat_completions"):
            seen.append(key)
            return ["m1"]

        payload = {"source": "manual", "manual": {
            "base_url": "https://example.com/v1/",
            "api_key": "",
            "provider_preset": "deepseek",
            "api_format": "chat_completions",
        }}
        with patch(
            "astrbot_plugin_onepercent_generator.model_config_api.fetch_manual_models",
            side_effect=fake_fetch,
        ), bind_request_context(PluginRequest(fake_request(body=payload))):
            await api.models()
        self.assertEqual(seen, ["sk-old"])

    @unittest.skipUnless(ASTRBOT_AVAILABLE, "需要真实 AstrBot Web 请求上下文")
    async def test_provider_change_does_not_reuse_key(self):
        config = FakeConfig({"ai_config": {
            "source": "manual",
            "base_url": "https://example.com/v1",
            "api_key": "sk-old",
            "api_key_origin": "https://example.com/v1",
            "provider_preset": "deepseek",
            "model": "m1",
        }})
        plugin = FakePlugin(config)
        api = ModelConfigAPI(plugin)
        payload = {"source": "manual", "manual": {
            "base_url": "https://example.com/v1",
            "api_key": "",
            "provider_preset": "openai",
            "api_format": "chat_completions",
        }}
        with bind_request_context(PluginRequest(fake_request(body=payload))):
            result = await api.save()
        self.assertTrue(result["saved"])
        self.assertEqual(config["ai_config"]["api_key"], "")
        self.assertFalse(result["state"]["manual"]["key_configured"])

    async def test_fetch_models_sends_credential_in_authorization_header(self):
        seen = {}

        async def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [{"id": "m1"}]})

        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            return original(transport=httpx.MockTransport(handler))

        with patch(
            "astrbot_plugin_onepercent_generator.model_service.httpx.AsyncClient",
            factory,
        ):
            models = await fetch_manual_models(
                "https://example.com/tenant/A/v1", "sk-scoped"
            )
        self.assertEqual(models, ["m1"])
        self.assertEqual(seen["auth"], "Bearer sk-scoped")
