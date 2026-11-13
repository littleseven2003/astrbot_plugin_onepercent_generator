# -*- coding: utf-8 -*-
"""文章主流程权限、缺游戏名和事实边界测试。"""

import logging
import sys
import types
import unittest
from pathlib import Path


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


class FakeContext:
    pass


class FakeEvent:
    def __init__(self, sender_id, text):
        self.message_str = text
        self.sender_id = sender_id
        self.stopped = False

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return ""

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
    whitelist_privates=None,
    result=None,
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
        "preset_games": ["测试游戏"],
        "whitelist_groups": [],
        "whitelist_privates": whitelist_privates or [],
        "show_generation_stats": False,
    }
    plugin = OnePercentGenerator(FakeContext(), config)
    plugin.search_service = FakeSearchService(result or search_result())
    plugin.rate_limiter = FakeRateLimiter()
    plugin.ai_client = FakeAIClient()
    return plugin


class WritingMainFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, plugin, event):
        messages = [item async for item in plugin.on_message(event)]
        return messages

    async def test_non_whitelist_experience_only_is_rejected_first(self):
        plugin = make_plugin(whitelist_privates=["allowed"])
        event = FakeEvent("blocked", "小作文 经历：我玩了 20 小时")

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertIn("不在白名单", messages[0]["text"])
        self.assertNotIn("请同时提供游戏名", messages[0]["text"])
        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.ai_client.prompts, [])

    async def test_admin_experience_only_gets_game_prompt_but_no_generation(self):
        plugin = make_plugin(admin_qqs=["admin"])
        event = FakeEvent("admin", "小作文 经历：我玩了 20 小时")

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertEqual(messages[0]["text"], GAME_REQUIRED_MESSAGE)
        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.ai_client.prompts, [])

    async def test_legal_user_experience_only_does_not_consume_rate(self):
        plugin = make_plugin(whitelist_privates=["allowed"])
        event = FakeEvent("allowed", "小作文 经历：我玩了 20 小时")

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertEqual(messages[0]["text"], GAME_REQUIRED_MESSAGE)
        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.ai_client.prompts, [])

    async def test_legal_user_with_game_and_experience_generates(self):
        plugin = make_plugin(whitelist_privates=["allowed"])
        event = FakeEvent(
            "allowed",
            "小作文 原神 经历：我玩了 20 小时，喜欢爬山。",
        )

        messages = await self._run(plugin, event)

        self.assertTrue(event.stopped)
        self.assertEqual(plugin.rate_limiter.calls, ["allowed"])
        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertEqual(len(plugin.ai_client.prompts), 1)
        prompt = plugin.ai_client.prompts[0]
        self.assertIn("我玩了 20 小时，喜欢爬山", prompt)
        self.assertIn("发售平台：可基于搜索资料", prompt)
        self.assertIn("发售平台为 PC", prompt)
        self.assertIn("经历没有提到的个人字段", prompt)
        self.assertTrue(messages)

    async def test_admin_with_game_and_experience_skips_rate_limit(self):
        plugin = make_plugin(admin_qqs=["admin"])
        event = FakeEvent(
            "admin",
            "小作文 原神 经历：我玩了 20 小时。",
        )

        await self._run(plugin, event)

        self.assertEqual(plugin.rate_limiter.calls, [])
        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertEqual(len(plugin.ai_client.prompts), 1)


if __name__ == "__main__":
    unittest.main()
