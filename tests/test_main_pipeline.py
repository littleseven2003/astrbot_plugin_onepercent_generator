# -*- coding: utf-8 -*-
"""主流程搜索接入测试：使用最小 AstrBot stub，不发真实请求。"""

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

from astrbot_plugin_onepercent_generator.ai_client import AIClientError  # noqa: E402
from astrbot_plugin_onepercent_generator.main import OnePercentGenerator  # noqa: E402


class FakeContext:
    pass


class FakeEvent:
    def __init__(self, text="小作文 测试游戏"):
        self.message_str = text
        self.unified_msg_origin = "webchat:test"
        self.stopped = False

    def get_sender_id(self):
        return "user1"

    def get_group_id(self):
        return ""

    def get_sender_name(self):
        return "tester"

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def stop_event(self):
        self.stopped = True


class FakeAIClient:
    def __init__(self, summarize_text="", summarize_error=None):
        self.summarize_text = summarize_text
        self.summarize_error = summarize_error
        self.prompts = []
        self.summarize_calls = []

    async def summarize_search(self, game_name, raw_text, max_chars=150):
        self.summarize_calls.append({
            "game_name": game_name,
            "raw_text": raw_text,
            "max_chars": max_chars,
        })
        if self.summarize_error is not None:
            raise self.summarize_error
        return {"content": self.summarize_text}

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


class FakeSearchService:
    def __init__(self, result=None, diagnostic=None):
        self.result = result or {}
        self.diagnostic = diagnostic or {}
        self.calls = []
        self.test_calls = []

    async def search_game_info(self, game_name, event=None):
        self.calls.append({"game_name": game_name, "event": event})
        return self.result

    async def test_search(self, game_name, event=None):
        self.test_calls.append({"game_name": game_name, "event": event})
        return self.diagnostic

    async def close(self):
        return None


class FakeRateLimiter:
    async def get_usage_status(self, qq_id, storage):
        return {
            "window_used": 1,
            "window_max": 3,
            "window_minutes": 10,
            "daily_used": 1,
            "daily_max": 20,
        }


def make_plugin(search_result=None, diagnostic=None):
    config = {
        "ai_config": {
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "test-key",
            "model": "mock-model",
        },
        "search_config": {
            "enabled": True,
            "mode": "api",
            "timeout_ms": 1000,
            "result_count": 3,
            "summarize_enabled": True,
            "summarize_max_chars": 40,
            "summary_input_max_chars": 6000,
        },
        "rate_limit": {
            "window_minutes": 10,
            "max_requests_per_window": 3,
            "daily_max": 20,
        },
        "admin_qqs": ["user1"],
        "preset_games": ["测试游戏"],
        "whitelist_groups": [],
        "whitelist_privates": [],
        "show_generation_stats": False,
    }
    plugin = OnePercentGenerator(FakeContext(), config)
    plugin.search_service = FakeSearchService(search_result, diagnostic)
    plugin.rate_limiter = FakeRateLimiter()
    return plugin


def search_result(summary="RAW" * 200, count=2):
    return {
        "status": "success",
        "summary": summary,
        "provider": "Tavily API",
        "duration_ms": 5,
        "results": [
            {
                "title": f"T{i}",
                "url": f"https://example.com/{i}",
                "snippet": f"S{i}",
                "source": "tavily",
            }
            for i in range(count)
        ],
        "attempted": [{
            "backend": "api",
            "status": "success",
            "reason": "",
            "duration_ms": 5,
            "result_count": count,
        }],
        "degraded": True,
        "partial": False,
        "partial_failures": [],
        "effective_backend": "api",
        "failure_reason": "",
    }


class MainPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, plugin, event=None):
        event = event or FakeEvent()
        messages = [item async for item in plugin.on_message(event)]
        return event, messages

    async def test_summary_budget_applied_to_generate_prompt(self):
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(summarize_text="AI" * 200)
        plugin.ai_client = fake_ai

        event, messages = await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 1)
        self.assertEqual(len(fake_ai.prompts), 1)
        expected = ("AI" * 200)[:40]
        self.assertIn(expected, fake_ai.prompts[0])
        self.assertNotIn("AI" * 200, fake_ai.prompts[0])
        self.assertTrue(event.stopped)
        self.assertTrue(any(m["text"].startswith("什么是【我的百分之一】")
                            for m in messages))

    async def test_summarize_failure_falls_back_to_raw_truncated(self):
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(
            summarize_error=AIClientError("summary failed", "汇总失败")
        )
        plugin.ai_client = fake_ai

        await self._run(plugin)

        expected = ("RAW" * 200)[:40]
        self.assertIn(expected, fake_ai.prompts[0])
        self.assertNotIn("RAW" * 200, fake_ai.prompts[0])

    async def test_summarize_disabled_uses_raw_truncated(self):
        plugin = make_plugin(search_result())
        plugin.search_summarize_enabled = False
        fake_ai = FakeAIClient(summarize_text="SHOULD-NOT-BE-USED")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 0)
        self.assertIn(("RAW" * 200)[:40], fake_ai.prompts[0])
        self.assertNotIn("SHOULD-NOT-BE-USED", fake_ai.prompts[0])

    async def test_summary_input_capped_before_ai_summarize(self):
        plugin = make_plugin(search_result(summary="R" * 2000))
        plugin.search_summary_input_max_chars = 120
        fake_ai = FakeAIClient(summarize_text="AI" * 200)
        plugin.ai_client = fake_ai

        await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 1)
        self.assertLessEqual(
            len(fake_ai.summarize_calls[0]["raw_text"]), 120
        )

    async def test_generation_stats_show_partial_source(self):
        result = search_result(count=1)
        result["partial"] = True
        result["partial_failures"] = [{
            "source": "baidu",
            "status": "timeout",
            "reason": "timeout",
        }]
        result["cleanup_incomplete"] = True
        plugin = make_plugin(result)
        plugin.show_generation_stats = True
        plugin.ai_client = FakeAIClient(summarize_text="AI" * 200)

        _, messages = await self._run(plugin)

        stats = [m["text"] for m in messages if "本次生成信息" in m["text"]]
        self.assertEqual(len(stats), 1)
        self.assertIn("搜索部分可用：是", stats[0])
        self.assertIn("部分失败来源：百度（超时）", stats[0])
        self.assertIn("搜索清理：未完成", stats[0])

    async def test_generation_stats_show_backend_and_degraded(self):
        plugin = make_plugin(search_result(count=2))
        plugin.show_generation_stats = True
        plugin.ai_client = FakeAIClient(summarize_text="AI" * 200)

        _, messages = await self._run(plugin)

        stats = [m["text"] for m in messages if "本次生成信息" in m["text"]]
        self.assertEqual(len(stats), 1)
        self.assertIn("搜索后端：Tavily API", stats[0])
        self.assertIn("搜索降级：是", stats[0])
        self.assertIn("有效结果：2", stats[0])

    async def test_admin_search_failure_message(self):
        diagnostic = {
            "success": False,
            "message": "搜索失败（原因：无有效结果；尝试：网页搜索（无有效结果））",
            "summary": "",
            "backend": "",
            "degraded": False,
            "result_count": 0,
            "failure_reason": "empty_results",
            "attempted": [],
        }
        plugin = make_plugin(diagnostic=diagnostic)
        event = FakeEvent("/小作文搜索测试 游戏")

        messages = [item async for item in plugin.test_search(event, "游戏")]

        text = "\n".join(m["text"] for m in messages)
        self.assertIn("❌ 搜索失败", text)
        self.assertNotIn("搜索结果摘要", text)
        self.assertTrue(event.stopped)

    async def test_admin_search_diagnostic_uses_service_message(self):
        diagnostic = {
            "success": True,
            "message": "搜索成功（后端：Tavily API，有效结果：2，降级：是）",
            "summary": "预览摘要",
            "backend": "api",
            "degraded": True,
            "result_count": 2,
            "failure_reason": "",
            "attempted": [],
        }
        plugin = make_plugin(diagnostic=diagnostic)
        event = FakeEvent("/小作文搜索测试 游戏")

        messages = [item async for item in plugin.test_search(event, "游戏")]

        self.assertEqual(len(plugin.search_service.test_calls), 1)
        text = "\n".join(m["text"] for m in messages)
        self.assertIn("搜索成功（后端：Tavily API", text)
        self.assertIn("搜索结果摘要", text)
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()
