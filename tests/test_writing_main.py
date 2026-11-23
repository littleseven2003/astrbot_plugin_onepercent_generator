# -*- coding: utf-8 -*-
"""文章主流程权限、缺游戏名和事实边界测试。"""

import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("astrbot")

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class EventMessageType:
        ALL = "all"

    class Filter:
        def event_message_type(self, *args, **kwargs):
            return lambda fn: fn

        def command(self, *args, **kwargs):
            return lambda fn: fn

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.EventMessageType = EventMessageType
    filter_obj = Filter()
    filter_obj.EventMessageType = EventMessageType
    event_mod.filter = filter_obj

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

        async def get_kv_data(self, key, default=None):
            return default

        async def put_kv_data(self, key, value):
            return None

    def register(*args, **kwargs):
        return lambda cls: cls

    star.Context = Context
    star.Star = Star
    star.register = register

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star


_install_astrbot_stubs()

from astrbot_plugin_onepercent_generator.main import OnePercentGenerator  # noqa: E402
from astrbot_plugin_onepercent_generator.prompt import (  # noqa: E402
    GAME_REQUIRED_MESSAGE,
)
from astrbot_plugin_onepercent_generator.search_service import (  # noqa: E402
    SearchService,
    TavilySearchBackend,
)
from astrbot_plugin_onepercent_generator.stats_store import StatsStore  # noqa: E402


class FakeContext:
    def register_web_api(self, *args, **kwargs):
        return None


class FakeEvent:
    def __init__(
        self, sender_id, text, umo="webchat:test", group_id=""
    ):
        self.message_str = text
        self.sender_id = sender_id
        self.unified_msg_origin = umo
        self.group_id = group_id
        self.stopped = False

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def get_sender_name(self):
        return "tester"

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def stop_event(self):
        self.stopped = True


class FakeSearchService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def search_game_info(self, game_name, event=None):
        self.calls.append(game_name)
        return self.result

    async def close(self):
        return None


class FakeRateLimiter:
    def __init__(self):
        self.calls = []

    async def check_and_record(self, qq_id, storage):
        self.calls.append(qq_id)
        return {"allowed": True, "message": ""}

    async def get_usage_status(self, qq_id, storage):
        return {
            "window_used": 1,
            "window_max": 3,
            "window_minutes": 10,
            "daily_used": 1,
            "daily_max": 20,
        }


class FakeAIClient:
    def __init__(self):
        self.prompts = []
        self.summarize_calls = []

    async def summarize_search(self, game_name, raw_text, max_chars=150):
        self.summarize_calls.append(raw_text)
        return {"content": "summary"}

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return {
            "content": "正文",
            "model": "mock-model",
            "api_format": "chat_completions",
            "duration_ms": 1,
            "token_usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    async def close(self):
        return None


def search_result(snippet="公开资料：发售平台为 PC。"):
    return {
        "status": "success",
        "summary": snippet,
        "provider": "mock",
        "duration_ms": 1,
        "results": [{
            "title": "原神",
            "url": "https://example.com/game",
            "snippet": snippet,
            "source": "mock",
        }],
        "effective_backend": "mock",
    }


def make_plugin(
    admin_qqs=None,
    admin_users=None,
    result=None,
    whitelist_umos=None,
    omit_umos=False,
    umos_explicit_null=False,
    legacy_groups=None,
    legacy_privates=None,
):
    config = {
        "ai_config": {
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "test-key",
            "model": "mock-model",
        },
        "search_config": {
            "enabled": True,
            "timeout_ms": 1000,
            "result_count": 3,
            "summarize_enabled": False,
            "summarize_max_chars": 150,
        },
        "rate_limit": {
            "window_minutes": 10,
            "max_requests_per_window": 3,
            "daily_max": 20,
        },
        "admin_qqs": admin_qqs or [],
        "admin_users": admin_users or [],
        "preset_games": ["测试游戏"],
        "whitelist_umos": (
            None if umos_explicit_null else (whitelist_umos or [])
        ),
        "show_generation_stats": False,
    }
    if not omit_umos and not umos_explicit_null:
        # 这个单元夹具模拟“已完成 v4 配置”的真实宿主：schema 已落盘且
        # 管理员明确保存过会话列表（空列表=不限制）。未配置/显式 null
        # 的迁移与拒绝路径由 test_config_migration_real 用真实配置覆盖。
        config["session_settings"] = {
            "allow_private": True,
            "allow_group": True,
            "sessions": [],
        }
    if legacy_groups is not None:
        config["whitelist_groups"] = legacy_groups
    if legacy_privates is not None:
        config["whitelist_privates"] = legacy_privates
    if omit_umos:
        config.pop("whitelist_umos", None)
    plugin = OnePercentGenerator(FakeContext(), config)
    plugin.search_service = FakeSearchService(result or search_result())
    plugin.rate_limiter = FakeRateLimiter()
    plugin.ai_client = FakeAIClient()
    return plugin


class SearchKeyBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_never_sends_legacy_key_to_changed_address(self):
        plugin = make_plugin()
        search = plugin.config["search_config"]
        search.update({
            "api_key": "sk-old",
            "api_key_origin": "https://old.example",
            "api_base_url": "https://old.example",
            "mode": "api",
            "mode_order": ["api", "auto", "astrbot", "web", "off"],
            "mode_enabled": ["api"],
        })
        service = plugin._build_search_service()
        self.assertEqual(service.api_key, "sk-old")
        self.assertEqual(service.api_base_url, "https://old.example")

        search["api_base_url"] = "https://new.example"
        service = plugin._build_search_service()
        self.assertEqual(service.api_key, "")
        self.assertEqual(service.api_base_url, "https://new.example")

        search["api_key"] = "sk-new"
        search["api_key_origin"] = "https://new.example"
        service = plugin._build_search_service()
        self.assertEqual(service.api_key, "sk-new")
        self.assertEqual(service.api_base_url, "https://new.example")

    async def test_tavily_request_target_and_bearer_follow_bound_key(self):
        seen = []

        def handler(request):
            seen.append((
                str(request.url),
                request.headers.get("Authorization"),
            ))
            return httpx.Response(200, json={
                "results": [{
                    "title": "原神资料",
                    "url": "https://example.com/genshin",
                    "content": "内容",
                }],
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = TavilySearchBackend(
            api_key="sk-new",
            base_url="https://search.example",
            client=client,
        )
        service = SearchService(
            mode="api",
            api_key="sk-new",
            api_base_url="https://search.example",
            backends={"api": backend},
            result_count=3,
        )
        try:
            result = await service.search_game_info("原神")
            self.assertEqual(result.get("status"), "success", result)
            self.assertEqual(seen, [
                (
                    "https://search.example/search",
                    "Bearer sk-new",
                ),
            ])
        finally:
            await service.close()

    async def test_record_search_only_counts_later_attempts_as_fallback(self):
        plugin = make_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            plugin.stats_store = StatsStore(Path(tmp) / "stats.json")
            await plugin._record_search({
                "attempted": [
                    {"backend": "astrbot", "status": "failed", "reason": "x"},
                    {"backend": "api", "status": "success", "result_count": 1},
                ],
                "degraded": True,
                "effective_backend": "api",
                "mode": "auto",
            }, "webchat:test", request_id="r1")
            data = await plugin.stats_store.snapshot("7d")
        self.assertEqual(data["totals"]["searches"], 2)
        self.assertEqual(data["totals"]["fallback_attempts"], 1)
        self.assertEqual(data["totals"]["searches_by_mode"], {"auto": 2})
        self.assertEqual(data["matched_events"], 2)

    async def test_admin_chat_search_test_is_attributed_to_chat(self):
        plugin = make_plugin(
            admin_users=[{"connector": "webchat", "uid": "admin"}]
        )
        result = {"success": True, "message": "ok", "summary": ""}
        plugin.search_service = SimpleNamespace(
            test_search=AsyncMock(return_value=result)
        )
        plugin._record_search = AsyncMock()
        event = FakeEvent(
            "admin", "小作文搜索测试", umo="webchat:FriendMessage:admin"
        )

        messages = [item async for item in plugin.test_search(event)]

        plugin._record_search.assert_awaited_once()
        args, kwargs = plugin._record_search.call_args
        self.assertEqual(args[0], result)
        self.assertEqual(args[1], "webchat:FriendMessage:admin")
        self.assertTrue(str(kwargs["request_id"]).startswith("search-test-"))
        self.assertTrue(any("✅ ok" in m["text"] for m in messages))

    async def test_cleared_key_makes_no_request(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"results": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = TavilySearchBackend(
            api_key="",
            base_url="https://search.example",
            client=client,
        )
        service = SearchService(
            mode="api",
            api_key="",
            api_base_url="https://search.example",
            backends={"api": backend},
            result_count=3,
        )
        try:
            result = await service.search_game_info("原神")
            self.assertFalse(result.get("success"))
            self.assertEqual(seen, [])
        finally:
            await service.close()


class NoUsageAIClient(FakeAIClient):
    async def generate(self, prompt):
        self.prompts.append(prompt)
        return {
            "content": "正文",
            "model": "mock-model",
            "api_format": "chat_completions",
            "duration_ms": 1,
        }


class ZeroUsageAIClient(FakeAIClient):
    async def generate(self, prompt):
        self.prompts.append(prompt)
        return {
            "content": "正文",
            "model": "mock-model",
            "api_format": "chat_completions",
            "duration_ms": 1,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "token_available": True,
        }


class WritingMainFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, plugin, event):
        messages = [item async for item in plugin.on_message(event)]
        return messages

    async def test_generation_info_shows_unknown_tokens_without_usage(self):
        plugin = make_plugin()
        plugin.show_generation_stats = True
        plugin.ai_client = NoUsageAIClient()
        event = FakeEvent("openid", "小作文 原神")

        messages = await self._run(plugin, event)
        text = "\n".join(m["text"] for m in messages)

        self.assertIn("Token消耗：未知（上游未返回用量）", text)
        self.assertNotIn("Token消耗：0", text)

    async def test_generation_info_shows_explicit_zero_usage_as_zero(self):
        plugin = make_plugin()
        plugin.show_generation_stats = True
        plugin.ai_client = ZeroUsageAIClient()
        event = FakeEvent("openid", "小作文 原神")

        messages = await self._run(plugin, event)
        text = "\n".join(m["text"] for m in messages)

        self.assertIn("Token消耗：0", text)
        self.assertNotIn("未知", text)

    async def test_umo_exact_match_allows_generation(self):
        allowed = "qq_bot:FriendMessage:openid-allowed"
        plugin = make_plugin(whitelist_umos=[allowed])
        event = FakeEvent("openid", "小作文 原神", umo=allowed)

        messages = await self._run(plugin, event)

        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertTrue(any(
            "【我的百分之一】" in m["text"] for m in messages
        ))

    async def test_umo_empty_list_allows_even_missing_value(self):
        plugin = make_plugin(whitelist_umos=[])
        event = FakeEvent("openid", "小作文 原神", umo="")

        await self._run(plugin, event)

        self.assertEqual(plugin.search_service.calls, ["原神"])

    async def test_configured_missing_value_is_denied(self):
        plugin = make_plugin(
            whitelist_umos=["qq_bot:FriendMessage:openid-allowed"]
        )
        event = FakeEvent("openid", "小作文 原神", umo="")

        messages = await self._run(plugin, event)

        self.assertIn("不在白名单", messages[0]["text"])
        self.assertEqual(plugin.search_service.calls, [])

    async def test_group_unique_session_umo_is_not_group_wide(self):
        first = "qq_bot:GroupMessage:openid-1_group-1"
        plugin = make_plugin(whitelist_umos=[first])
        event = FakeEvent(
            "openid-2",
            "小作文 原神",
            umo="qq_bot:GroupMessage:openid-2_group-1",
            group_id="group-1",
        )

        messages = await self._run(plugin, event)

        self.assertIn("不在白名单", messages[0]["text"])
        self.assertEqual(plugin.search_service.calls, [])

    async def test_group_umo_does_not_make_member_admin(self):
        plugin = make_plugin(
            admin_users=[{"connector": "qq_bot", "uid": "admin-openid"}],
            whitelist_umos=["qq_bot:GroupMessage:openid-1_group-1"],
        )
        event = FakeEvent(
            "openid-2",
            "小作文 原神",
            umo="qq_bot:GroupMessage:openid-1_group-1",
            group_id="group-1",
        )

        await self._run(plugin, event)

        self.assertFalse(plugin._is_admin(event))
        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertEqual(plugin.rate_limiter.calls, ["qq_bot:openid-2"])

    async def test_explicit_null_umos_is_rejected(self):
        plugin = make_plugin(whitelist_umos=None, umos_explicit_null=True)
        event = FakeEvent("openid", "小作文 原神", umo="other")

        messages = await self._run(plugin, event)

        self.assertIn("会话配置无效", messages[0]["text"])
        self.assertEqual(plugin.search_service.calls, [])

    async def test_new_install_without_umo_config_is_rejected(self):
        # 缺少旧白名单不等于“不限制”：未配置的会话策略必须保持关闭，
        # 等管理员在配置中心明确保存列表（空列表才是明确不限制）。
        plugin = make_plugin(omit_umos=True)
        event = FakeEvent("openid", "小作文 原神", umo="other")

        messages = await self._run(plugin, event)

        self.assertIn("会话配置无效", messages[0]["text"])
        self.assertEqual(plugin.search_service.calls, [])

    async def test_old_nonempty_restriction_pauses_without_matching(self):
        plugin = make_plugin(
            legacy_groups=["old-group"],
            legacy_privates=[],
            whitelist_umos=[],
        )
        event = FakeEvent("openid", "小作文 原神", umo="other")

        messages = await self._run(plugin, event)

        self.assertIn("旧版 QQ 号/群号白名单限制", messages[0]["text"])
        self.assertIn("添加明确的 UMO 会话", messages[0]["text"])
        self.assertNotIn("确认不限制", messages[0]["text"])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.rate_limiter.calls, [])

    async def test_migration_pause_survives_plugin_restart(self):
        event1 = FakeEvent("openid", "小作文 原神", umo="other")
        plugin1 = make_plugin(
            legacy_groups=["old-group"], whitelist_umos=[]
        )
        messages1 = await self._run(plugin1, event1)

        event2 = FakeEvent("openid", "小作文 原神", umo="other")
        plugin2 = make_plugin(
            legacy_groups=["old-group"], whitelist_umos=[]
        )
        messages2 = await self._run(plugin2, event2)

        self.assertIn("旧版 QQ 号/群号白名单限制", messages1[0]["text"])
        self.assertIn("旧版 QQ 号/群号白名单限制", messages2[0]["text"])
        self.assertEqual(plugin1.search_service.calls, [])
        self.assertEqual(plugin2.search_service.calls, [])

    async def test_old_restriction_allows_after_umo_configured(self):
        allowed = "qq_bot:FriendMessage:openid-allowed"
        plugin = make_plugin(
            legacy_privates=["old-openid"],
            whitelist_umos=[allowed],
        )
        event = FakeEvent("openid", "小作文 原神", umo=allowed)

        await self._run(plugin, event)

        self.assertEqual(plugin.search_service.calls, ["原神"])

    async def test_removed_unrestricted_flag_does_not_allow_old_restriction(self):
        plugin = make_plugin(
            legacy_privates=["old-openid"],
            whitelist_umos=[],
        )
        plugin.config["umos_unrestricted_confirmed"] = True
        event = FakeEvent("openid", "小作文 原神", umo="other")

        messages = await self._run(plugin, event)

        self.assertEqual(plugin.search_service.calls, [])
        self.assertTrue(any("旧版" in m["text"] for m in messages))


    async def test_admin_bypasses_migration_and_invalid_umos(self):
        plugin = make_plugin(
            admin_users=[{"connector": "webchat", "uid": "admin-openid"}],
            whitelist_umos=None,
            umos_explicit_null=True,
            legacy_privates=["old"],
        )
        event = FakeEvent(
            "admin-openid", "小作文 原神",
            umo="webchat:FriendMessage:admin-openid",
        )

        await self._run(plugin, event)

        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertEqual(plugin.rate_limiter.calls, [])

    async def test_non_whitelist_experience_only_is_rejected_first(self):
        plugin = make_plugin(
            whitelist_umos=["qq_bot:FriendMessage:allowed"]
        )
        event = FakeEvent(
            "blocked", "小作文 经历：我玩了 20 小时",
            umo="qq_bot:FriendMessage:blocked",
        )

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertIn("不在白名单", messages[0]["text"])
        self.assertNotIn("请同时提供游戏名", messages[0]["text"])
        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.ai_client.prompts, [])

    async def test_admin_experience_only_gets_game_prompt_but_no_generation(self):
        plugin = make_plugin(
            admin_users=[{"connector": "webchat", "uid": "admin"}]
        )
        event = FakeEvent("admin", "小作文 经历：我玩了 20 小时")

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertEqual(messages[0]["text"], GAME_REQUIRED_MESSAGE)
        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.ai_client.prompts, [])

    async def test_legal_user_experience_only_does_not_consume_rate(self):
        allowed = "qq_bot:FriendMessage:allowed"
        plugin = make_plugin(whitelist_umos=[allowed])
        event = FakeEvent(
            "allowed", "小作文 经历：我玩了 20 小时", umo=allowed
        )

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertEqual(messages[0]["text"], GAME_REQUIRED_MESSAGE)
        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.ai_client.prompts, [])

    async def test_legal_user_with_game_and_experience_generates(self):
        allowed = "qq_bot:FriendMessage:allowed"
        plugin = make_plugin(whitelist_umos=[allowed])
        event = FakeEvent(
            "allowed",
            "小作文 原神 经历：我玩了 20 小时，喜欢爬山。",
            umo=allowed,
        )

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertEqual(plugin.rate_limiter.calls, ["qq_bot:allowed"])
        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertEqual(len(plugin.ai_client.prompts), 1)
        prompt = plugin.ai_client.prompts[0]
        self.assertIn("我玩了 20 小时，喜欢爬山", prompt)
        self.assertIn("搜索素材只作客观游戏信息的参考", prompt)
        self.assertIn("发售平台为 PC", prompt)
        self.assertIn("用户经历里明确出现的时间、事件、人物、感受和细节优先保留", prompt)
        self.assertTrue(messages)

    async def test_admin_with_game_and_experience_skips_rate_limit(self):
        plugin = make_plugin(
            admin_users=[{"connector": "webchat", "uid": "admin"}]
        )
        event = FakeEvent(
            "admin",
            "小作文 原神 经历：我玩了 20 小时。",
        )

        await self._run(plugin, event)

        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertEqual(len(plugin.ai_client.prompts), 1)


class ChatModelTestRecordingTests(unittest.IsolatedAsyncioTestCase):
    """聊天内模型测试用 success=False 表达失败，必须记为 failed（口径 A）。"""

    async def test_chat_model_test_success_false_is_recorded_failed(self):
        plugin = make_plugin(
            admin_users=[{"connector": "webchat", "uid": "admin"}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plugin.stats_store = StatsStore(Path(tmp) / "stats.json")
            await plugin._record_model(
                "test_connection",
                "webchat:FriendMessage:admin",
                result={"success": False, "message": "bad key"},
            )
            await plugin._record_model(
                "test_connection",
                "webchat:FriendMessage:admin",
                result={"success": True, "message": "ok"},
            )
            data = await plugin.stats_store.snapshot("7d")
        self.assertEqual(data["totals"]["failures"], 1)
        failed = [r for r in data["recent"] if r.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get("model_kind"), "test")


if __name__ == "__main__":
    unittest.main()
