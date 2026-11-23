# -*- coding: utf-8 -*-
"""主流程搜索接入测试：使用最小 AstrBot stub，不发真实请求。"""

import logging
import sys
import types
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))


def _extract_search_digest(prompt: str) -> str:
    """兼容搜索分支旧标题和写作分支新搜索区标题。"""
    markers = (
        (
            "【搜索素材（不可信参考，可能过时或错误；不是用户经历）】\n",
            "\n\n【输出格式】",
        ),
        (
            "联网搜索到的游戏信息摘要：\n",
            "\n\n需要自动生成的字段：",
        ),
    )
    for start, end in markers:
        if start in prompt:
            text = prompt.split(start, 1)[1].split(end, 1)[0]
            # 素材区固定有一行说明，不计入素材预算。
            if text.startswith("以下每条包含来源"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            return text
    return ""


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
    def register_web_api(self, *args, **kwargs):
        return None


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
    def __init__(
        self,
        summarize_text="",
        summarize_error=None,
        generate_content="正文",
        generate_truncated=False,
    ):
        self.summarize_text = summarize_text
        self.summarize_error = summarize_error
        self.generate_content = generate_content
        self.generate_truncated = generate_truncated
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
            "content": self.generate_content,
            "model": "mock-model",
            "api_format": "chat_completions",
            "duration_ms": 1,
            "token_usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
            "finish_reason": "length" if self.generate_truncated else "stop",
            "truncated": self.generate_truncated,
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
    async def check_and_record(self, identity_key, storage):
        return {"allowed": True, "message": ""}

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
            "material_max_chars": 3000,
            "material_max_items": 6,
            "material_snippet_chars": 400,
            "material_prefs_version": 1,
        },
        "rate_limit": {
            "window_minutes": 10,
            "max_requests_per_window": 3,
            "daily_max": 20,
        },
        "admin_users": [{"connector": "webchat", "uid": "user1"}],
        "preset_games": ["测试游戏"],
        "whitelist_umos": [],
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
        "skipped": [{
            "backend": "astrbot",
            "status": "unavailable",
            "reason": "astrbot_contract_unverified",
            "duration_ms": 0,
            "result_count": 0,
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

    async def test_experience_reaches_prompt_with_search_platform(self):
        result = search_result(count=2)
        result["results"][0]["snippet"] = (
            "公开资料：发售平台为 PC、iOS、Android。"
        )
        plugin = make_plugin(result)
        plugin.search_summarize_enabled = False
        plugin.search_summarize_max_chars = 150
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai
        event = FakeEvent("小作文 原神 经历：我玩了 20 小时，喜欢爬山。")

        await self._run(plugin, event)

        self.assertEqual(
            plugin.search_service.calls[0]["game_name"], "原神"
        )
        prompt = fake_ai.prompts[0]
        self.assertIn("我玩了 20 小时，喜欢爬山", prompt)
        self.assertIn("搜索素材只作客观游戏信息的参考", prompt)
        self.assertIn("发售平台为 PC、iOS、Android", prompt)

    async def test_material_direct_uses_single_model_call(self):
        # 默认素材直传：只调用一次写作模型，不调用汇总模型。
        plugin = make_plugin(search_result())
        plugin.search_summarize_enabled = False
        fake_ai = FakeAIClient(generate_content="正文内容。")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 0)
        self.assertEqual(len(fake_ai.prompts), 1)
        prompt = fake_ai.prompts[0]
        self.assertIn("[tavily]", prompt)
        self.assertIn("链接：https://example.com/0", prompt)
        self.assertIn("片段：", prompt)

    async def test_no_experience_adds_no_extra_message(self):
        # 新口径：没有经历也不再追加「前置信息还差/可复制补充」等尾注。
        plugin = make_plugin(search_result())
        plugin.search_summarize_enabled = False
        fake_ai = FakeAIClient(generate_content="正文内容。")
        plugin.ai_client = fake_ai

        _, messages = await self._run(plugin)

        texts = [item["text"] for item in messages]
        finals = [text for text in texts if text.startswith("【我的百分之一】")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(texts), 2)  # 进度提示 + 最终帖文，没有追加尾注
        for tail in ("前置信息还差", "可复制补充", "钓鱼", "真实经历"):
            self.assertNotIn(tail, finals[0])

    async def test_experience_does_not_add_hint_message(self):
        plugin = make_plugin(search_result())
        plugin.search_summarize_enabled = False
        fake_ai = FakeAIClient(generate_content="正文内容。")
        plugin.ai_client = fake_ai
        event = FakeEvent("小作文 测试游戏 经历：我玩了 20 小时。")

        _, messages = await self._run(plugin, event)

        texts = [item["text"] for item in messages]
        finals = [text for text in texts if text.startswith("【我的百分之一】")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(texts), 2)  # 进度提示 + 最终帖文

    async def test_experience_only_asks_for_game_without_random(self):
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai
        event = FakeEvent("小作文 经历：我玩了 20 小时，喜欢爬山。")

        event, messages = await self._run(plugin, event)

        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(fake_ai.prompts, [])
        self.assertTrue(event.stopped)
        self.assertTrue(any(
            "请同时提供游戏名" in message["text"]
            for message in messages
        ))

    async def test_search_failure_still_generates(self):
        failed = {
            "status": "failed",
            "summary": "",
            "provider": "失败",
            "duration_ms": 1,
            "results": [],
            "effective_backend": "",
            "failure_reason": "timeout",
        }
        plugin = make_plugin(failed)
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        event, messages = await self._run(plugin)

        self.assertEqual(len(fake_ai.prompts), 1)
        self.assertTrue(event.stopped)
        self.assertTrue(messages)

    async def test_search_disabled_still_generates(self):
        disabled = {
            "status": "disabled",
            "summary": "",
            "provider": "未启用",
            "duration_ms": 0,
            "results": [],
            "effective_backend": "",
            "failure_reason": "disabled",
        }
        plugin = make_plugin(disabled)
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        event, messages = await self._run(plugin)

        self.assertEqual(len(fake_ai.prompts), 1)
        self.assertTrue(event.stopped)
        self.assertTrue(messages)

    async def test_final_message_has_no_release_prompt_even_when_truncated(self):
        body = (
            "发售平台：PC\n"
            "游玩时间：20 小时\n"
            "推荐人群及理由：喜欢农场经营的人。\n"
            "游戏介绍：农场经营游戏。\n"
            "故事/感想：我玩了 20 小时，喜欢钓鱼。"
        )
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(
            generate_content=body, generate_truncated=True
        )
        plugin.ai_client = fake_ai
        event = FakeEvent("小作文 星露谷物语 经历：我玩了 20 小时，喜欢钓鱼。")

        _, messages = await self._run(plugin, event)

        final = [
            m["text"] for m in messages
            if m["text"].startswith("【我的百分之一】")
        ][0]
        self.assertIn("【我的百分之一】+【星露谷物语】", final)
        self.assertNotIn("发布前提示", final)
        self.assertNotIn("模型输出达到长度上限", final)
        self.assertNotIn("补充方式", final)

    async def test_short_article_has_no_supplement_prompt(self):
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(generate_content="发售平台：PC")
        plugin.ai_client = fake_ai

        _, messages = await self._run(plugin)

        final = [
            m["text"] for m in messages
            if m["text"].startswith("【我的百分之一】")
        ][0]
        self.assertNotIn("低于建议", final)
        self.assertNotIn("未自动重试", final)
        self.assertNotIn("发布前提示", final)

    async def test_prefix_only_output_gets_fallbacks_without_hint_tail(self):
        # 模型只返回前置字段时：保留「正文：」并给自然兜底，不再追加尾注。
        result = search_result()
        result["results"][0]["snippet"] = "公开资料：发售平台为 PC。"
        plugin = make_plugin(result)
        fake_ai = FakeAIClient(
            generate_content="发售平台：PC\n推荐人群（理由）：喜欢慢节奏的人"
        )
        plugin.ai_client = fake_ai
        event = FakeEvent("小作文 星露谷物语")

        _, messages = await self._run(plugin, event)

        texts = [m["text"] for m in messages]
        finals = [text for text in texts if text.startswith("【我的百分之一】")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(texts), 2)
        final = finals[0]
        self.assertIn("游戏当时发售平台：PC", final)
        self.assertIn("推荐人群（理由）：喜欢慢节奏的人", final)
        self.assertIn("\n正文：", final)
        self.assertNotIn("【请补充", final)
        for tail in ("前置信息还差", "可复制补充", "钓鱼"):
            self.assertNotIn(tail, final)

    async def test_missing_prefix_fields_get_natural_fallbacks(self):
        # 模型漏填前置字段时给完整自然的兜底，不出现占位符。
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(generate_content="只有正文段落。")
        plugin.ai_client = fake_ai

        _, messages = await self._run(plugin)

        finals = [
            item["text"] for item in messages
            if item["text"].startswith("【我的百分之一】")
        ]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(messages), 2)
        final = finals[0]
        self.assertIn("游戏当时发售平台：多平台", final)
        self.assertIn("玩游戏的时间：玩了一段时间", final)
        self.assertIn("推荐人群（理由）：喜欢这类游戏的玩家", final)
        self.assertNotIn("【请补充", final)

    async def test_known_facts_fix_time_conflict_and_platform_gap(self):
        # 素材事实参与成文兜底：按素材能确定的年月日校验时间，首发平台
        # 只认紧跟首发短语的平台并区分主机代际。
        cases = [
            (
                "《艾尔登法环》于 2022 年 2 月 25 日发售。",
                "玩游戏的时间：2018 年春天\n推荐人群（理由）：喜欢探索的人\n正文段落。",
                "玩游戏的时间：2022 年发售后不久",
                "2018",
            ),
            (
                # 同年 1 月也早于 2 月 25 日发售。
                "《艾尔登法环》于 2022 年 2 月 25 日发售。",
                "玩游戏的时间：2022 年 1 月\n推荐人群（理由）：喜欢探索的人\n正文段落。",
                "玩游戏的时间：2022 年发售后不久",
                "2022 年 1 月",
            ),
            (
                # 同年 2 月 24 日仍早一天，同样替换。
                "《艾尔登法环》于 2022 年 2 月 25 日发售。",
                "玩游戏的时间：2022 年 2 月 24 日\n推荐人群（理由）：喜欢探索的人\n正文段落。",
                "玩游戏的时间：2022 年发售后不久",
                "2022 年 2 月 24 日",
            ),
            (
                # 正例：发售之后的月份保留模型写法。
                "《艾尔登法环》于 2022 年 2 月 25 日发售。",
                "玩游戏的时间：2022 年 3 月\n推荐人群（理由）：喜欢探索的人\n正文段落。",
                "玩游戏的时间：2022 年 3 月",
                "发售后不久",
            ),
            (
                # 「仅」后面不加「为」也要认；只支持 Switch 就写 Switch。
                "《塞尔达传说 王国之泪》首发平台仅 Nintendo Switch。",
                "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢解谜的人\n正文段落。",
                "游戏当时发售平台：Nintendo Switch",
                "游戏当时发售平台：多平台",
            ),
            (
                # 后续移植到 PS5 不算首发：模型填 PS5 也要改回 PC。
                "《原神》首发于 PC 并于次年移植到 PS5。",
                "游戏当时发售平台：PS5\n玩游戏的时间：大学期间\n"
                "推荐人群（理由）：喜欢开放世界的人\n正文段落。",
                "游戏当时发售平台：PC",
                "PS5",
            ),
            (
                # 数字格式发售日同样要挡住前一天。
                "发售日期：2022-02-25",
                "玩游戏的时间：2022 年 2 月 24 日开始玩\n"
                "推荐人群（理由）：喜欢探索的人\n正文段落。",
                "玩游戏的时间：2022 年发售后不久",
                "2022 年 2 月 24 日",
            ),
            (
                # 「不包括」不是首发依据，同一素材里的正例才算。
                "《原神》首发平台不包括 PS5，游戏首发于 PC。",
                "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢开放世界的人\n正文段落。",
                "游戏当时发售平台：PC",
                "PlayStation 5",
            ),
            (
                # 「以及」是平台列表连接词，后面的平台不能丢。
                "《原神》首发平台包括 PC、PS4 以及 PS5。",
                "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢开放世界的人\n正文段落。",
                "游戏当时发售平台：PC / PlayStation 4 / PlayStation 5",
                "游戏当时发售平台：多平台",
            ),
            (
                # 素材只支持 Switch 2，模型写 Switch 属于写错代际。
                "《塞尔达传说 王国之泪》首发平台仅为 Nintendo Switch 2。",
                "游戏当时发售平台：Nintendo Switch\n玩游戏的时间：大学期间\n"
                "推荐人群（理由）：喜欢解谜的人\n正文段落。",
                "游戏当时发售平台：Nintendo Switch 2",
                "游戏当时发售平台：Nintendo Switch\n",
            ),
        ]
        for snippet, model_body, expected, unexpected in cases:
            with self.subTest(snippet=snippet, expected=expected):
                result = search_result()
                result["results"][0]["snippet"] = snippet
                plugin = make_plugin(result)
                fake_ai = FakeAIClient(generate_content=model_body)
                plugin.ai_client = fake_ai
                event = FakeEvent("小作文 测试游戏")

                _, messages = await self._run(plugin, event)

                final = [
                    item["text"] for item in messages
                    if item["text"].startswith("【我的百分之一】")
                ][0]
                self.assertIn(expected, final)
                self.assertNotIn(unexpected, final)

    async def test_user_fields_stay_primary_over_model_values(self):
        # 用户显式给了平台/时间/推荐人群：优先保留，模型不得覆盖。
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(
            generate_content=(
                "游戏当时发售平台：Xbox Series X\n"
                "玩游戏的时间：大学期间\n推荐人群（理由）：模型写的理由\n"
                "正文段落。"
            )
        )
        plugin.ai_client = fake_ai
        event = FakeEvent(
            "小作文 艾尔登法环\n发售平台：PC / PS5\n"
            "玩游戏的时间：去年暑假\n推荐人群：喜欢魂类游戏的玩家"
        )

        _, messages = await self._run(plugin, event)

        final = [
            item["text"] for item in messages
            if item["text"].startswith("【我的百分之一】")
        ][0]
        self.assertIn("游戏当时发售平台：PC / PS5", final)
        self.assertIn("玩游戏的时间：去年暑假", final)
        self.assertIn("推荐人群（理由）：喜欢魂类游戏的玩家", final)
        self.assertNotIn("Xbox Series X", final)
        self.assertNotIn("模型写的理由", final)

    async def test_summary_budget_applied_to_generate_prompt(self):
        plugin = make_plugin(search_result())
        fake_ai = FakeAIClient(summarize_text="AI" * 200)
        plugin.ai_client = fake_ai

        event, messages = await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 1)
        self.assertEqual(len(fake_ai.prompts), 1)
        expected = ("AI" * 200)[:40]
        prompt = fake_ai.prompts[0]
        self.assertIn(expected, prompt)
        self.assertNotIn("AI" * 200, prompt)
        digest = _extract_search_digest(prompt)
        self.assertLessEqual(len(digest), 40)
        self.assertTrue(event.stopped)
        self.assertTrue(any(
            m["text"].startswith("【我的百分之一】+【测试游戏】")
            for m in messages
        ))

    async def test_summarize_failure_rebuilds_structured_summary(self):
        plugin = make_plugin(search_result())
        plugin.search_summarize_max_chars = 120
        fake_ai = FakeAIClient(
            summarize_error=AIClientError("summary failed", "汇总失败")
        )
        plugin.ai_client = fake_ai

        await self._run(plugin)

        prompt = fake_ai.prompts[0]
        self.assertIn("T0", prompt)
        self.assertIn("T1", prompt)
        self.assertNotIn("RAW" * 200, prompt)

    async def test_summarize_disabled_rebuilds_structured_summary(self):
        plugin = make_plugin(search_result())
        plugin.search_summarize_enabled = False
        plugin.search_summarize_max_chars = 120
        fake_ai = FakeAIClient(summarize_text="SHOULD-NOT-BE-USED")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 0)
        self.assertIn("T0", fake_ai.prompts[0])
        self.assertIn("T1", fake_ai.prompts[0])
        self.assertNotIn("SHOULD-NOT-BE-USED", fake_ai.prompts[0])

    async def test_material_prompt_keeps_facts_within_budget(self):
        # 素材直传（默认路径）：条数与总字数都受预算约束，但每条事实
        # 仍要能进入 Prompt，不能只剩标题。
        result = search_result(count=7)
        for index, entry in enumerate(result["results"]):
            entry["title"] = f"LongTitle-{index}-" + "T" * 80
            entry["snippet"] = f"Fact-{index}-release-2026"
        plugin = make_plugin(result)
        plugin.search_summarize_enabled = False
        plugin.search_material_max_chars = 900
        plugin.search_material_max_items = 4
        plugin.search_material_snippet_chars = 200
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        prompt = fake_ai.prompts[0]
        self.assertIn("Fact-0", prompt)
        self.assertIn("Fact-3", prompt)
        self.assertGreaterEqual(prompt.count("Fact-"), 3)
        self.assertNotIn("Fact-4", prompt)
        digest = _extract_search_digest(prompt)
        self.assertLessEqual(len(digest), 900)

    async def test_material_fallback_path_also_respects_budget(self):
        # 审查回归：素材块为空时回退文本摘要，回退路径也必须受素材
        # 直传预算约束，不能借旧摘要预算把超长内容塞进生成 Prompt。
        result = search_result(count=3)
        for index, entry in enumerate(result["results"]):
            entry["url"] = "https://example.com/" + "z" * 2100
            entry["snippet"] = "R" * 2000
        result["summary"] = "R" * 4000
        plugin = make_plugin(result)
        plugin.search_summarize_enabled = False
        plugin.search_material_max_chars = 1000
        plugin.search_material_max_items = 1
        plugin.search_material_snippet_chars = 200
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        digest = _extract_search_digest(fake_ai.prompts[0])
        self.assertTrue(digest)
        self.assertLessEqual(len(digest), 1000)

    async def test_no_link_fallback_respects_chars_items_and_snippet(self):
        # 审查回归：两条链接都放不下时，回退也要遵守字数、条数、单条片段上限，
        # 不能把两条结果一起塞进模型。
        result = search_result(count=2)
        for entry in result["results"]:
            entry["url"] = "https://example.com/" + "w" * 1500
            entry["snippet"] = "句子。" * 300
        plugin = make_plugin(result)
        plugin.search_summarize_enabled = False
        plugin.search_material_max_chars = 1000
        plugin.search_material_max_items = 1
        plugin.search_material_snippet_chars = 200
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        digest = _extract_search_digest(fake_ai.prompts[0])
        self.assertIn("T0", digest)
        self.assertNotIn("T1", digest)
        self.assertNotIn("链接：", digest)
        self.assertLessEqual(len(digest), 1000)
        snippet = digest.split("片段：", 1)[1]
        self.assertLessEqual(len(snippet), 200)

    async def test_experience_material_ranks_before_platform(self):
        # 第 6 项：生成入模阶段体验/剧情素材优先，机制/平台资料靠后。
        result = search_result(count=2)
        result["results"][0].update({
            "title": "平台机制资料", "source": "bing", "query_index": 0,
        })
        result["results"][1].update({
            "title": "玩家体验剧情", "source": "baidu", "query_index": 1,
        })
        plugin = make_plugin(result)
        plugin.search_summarize_enabled = False
        plugin.search_material_max_chars = 3000
        plugin.search_material_max_items = 2
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        digest = _extract_search_digest(fake_ai.prompts[0])
        self.assertLess(digest.index("玩家体验剧情"), digest.index("平台机制资料"))

    async def test_material_prompt_keeps_multiple_items(self):
        plugin = make_plugin(search_result(count=3))
        plugin.search_summarize_enabled = False
        plugin.search_material_max_chars = 600
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        prompt = fake_ai.prompts[0]
        self.assertIn("T0", prompt)
        self.assertIn("T1", prompt)
        self.assertIn("T2", prompt)
        digest = _extract_search_digest(prompt)
        self.assertLessEqual(len(digest), 600)

    async def test_summarize_empty_content_rebuilds_structured_summary(self):
        plugin = make_plugin(search_result())
        plugin.search_summarize_max_chars = 120
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        await self._run(plugin)

        self.assertEqual(len(fake_ai.summarize_calls), 1)
        self.assertIn("T0", fake_ai.prompts[0])
        self.assertIn("T1", fake_ai.prompts[0])

    async def test_summary_input_capped_before_ai_summarize(self):
        result = search_result(summary="R" * 2000)
        result["results"][0]["snippet"] = "R" * 2000
        result["results"][1]["snippet"] = "R" * 2000
        plugin = make_plugin(result)
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

    async def test_generation_stats_distinguish_all_failed(self):
        failed = {
            "status": "failed",
            "summary": "",
            "provider": "失败",
            "duration_ms": 1,
            "results": [],
            "attempted": [{"backend": "astrbot", "status": "unavailable",
                           "reason": "astrbot_call_contract_unverified"}],
            "skipped": [],
            "effective_backend": "",
            "failure_reason": "astrbot_call_contract_unverified",
        }
        plugin = make_plugin(failed)
        plugin.show_generation_stats = True
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        _, messages = await self._run(plugin)

        stats = [m["text"] for m in messages if "本次生成信息" in m["text"]][0]
        self.assertIn("搜索状态：全部候选后端失败", stats)
        self.assertIn("搜索降级：否（全部失败）", stats)
        self.assertIn("搜索失败原因：astrbot_call_contract_unverified", stats)

    async def test_generation_stats_distinguish_search_disabled(self):
        disabled = {
            "status": "disabled",
            "summary": "",
            "provider": "未启用",
            "duration_ms": 0,
            "results": [],
            "attempted": [],
            "skipped": [],
            "degraded": False,
            "effective_backend": "",
            "failure_reason": "disabled",
        }
        plugin = make_plugin(disabled)
        plugin.show_generation_stats = True
        fake_ai = FakeAIClient(summarize_text="")
        plugin.ai_client = fake_ai

        _, messages = await self._run(plugin)

        stats = [m["text"] for m in messages if "本次生成信息" in m["text"]][0]
        self.assertIn("搜索状态：未调用（搜索关闭）", stats)
        self.assertIn("搜索降级：未调用（搜索关闭）", stats)

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
