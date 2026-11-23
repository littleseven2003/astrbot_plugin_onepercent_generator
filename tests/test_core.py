import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

from astrbot_plugin_onepercent_generator.ai_client import (  # noqa: E402
    API_FORMAT_CHAT_COMPLETIONS,
    API_FORMAT_RESPONSES,
    AIClient,
    AIClientNotConfigured,
)
from astrbot_plugin_onepercent_generator.post_process import (  # noqa: E402
    build_final_message,
    strip_generated_title_and_game_name,
)
from astrbot_plugin_onepercent_generator.prompt import (  # noqa: E402
    build_main_prompt,
    parse_activity_fields,
    parse_optional_extras,
    parse_writing_request,
)
from astrbot_plugin_onepercent_generator.rate_limiter import RateLimiter  # noqa: E402
from astrbot_plugin_onepercent_generator.search_service import (  # noqa: E402
    SearchService,
    _build_summary,
    _extract_meta_description,
)
from astrbot_plugin_onepercent_generator.whitelist import (  # noqa: E402
    is_session_allowed,
    migration_required,
    validate_umo_list,
)


class MemoryKV:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


class PromptAndPostProcessTests(unittest.TestCase):
    def test_prompt_preserves_game_name_and_search_context(self):
        prompt = build_main_prompt("星露谷物语", "像素风农场模拟游戏")

        self.assertIn('游戏名称"星露谷物语"', prompt)
        self.assertIn("像素风农场模拟游戏", prompt)
        self.assertIn('只能使用"星露谷物语"这个名称', prompt)

    def test_generated_title_and_game_name_are_removed(self):
        content = "标题：测试\n游戏名称：错误名称\n正文：\n发售平台：PC"

        self.assertEqual(
            strip_generated_title_and_game_name(content),
            "发售平台：PC",
        )

    def test_final_message_uses_user_game_name(self):
        message = build_final_message("原神", "发售平台：PC")

        self.assertIn("游戏名称：原神", message)
        self.assertTrue(
            message.startswith("【我的百分之一】+【原神】")
        )
        self.assertIn("什么是【我的百分之一】", message)
        self.assertNotIn("发布前提示", message)
        self.assertNotIn("补充方式", message)

    def test_placeholders_are_not_in_publishable_body(self):
        ai_body = (
            "发售平台：PC\n"
            "游玩时间：未提供\n"
            "推荐人群及理由：待补充\n"
            "游戏介绍：农场经营游戏。\n"
            "故事/感想：未提供"
        )

        message = build_final_message("星露谷物语", ai_body)
        body = message.split("---", 1)[0]

        self.assertNotIn("游玩时间：未提供", body)
        self.assertNotIn("推荐人群及理由：待补充", body)
        self.assertNotIn("故事/感想", body)
        self.assertIn("农场经营游戏。", body)
        self.assertNotIn("游戏介绍：", body)
        self.assertNotIn("发布前提示", message)
        self.assertNotIn("补充方式", message)
        self.assertNotIn("未提供", body)

    def test_missing_experience_story_is_outside_body(self):
        ai_body = "发售平台：PC\n游戏介绍：农场经营游戏。"

        message = build_final_message(
            "星露谷物语", ai_body, experience=""
        )
        body = message.split("---", 1)[0]

        self.assertNotIn("故事/感想", body)
        self.assertNotIn("发布前提示", message)
        self.assertNotIn("补充方式", message)
        self.assertNotIn("未提供", body)

    def test_natural_article_without_field_headings_is_not_marked_missing(self):
        ai_body = (
            "游戏采用明亮的卡通渲染，探索和战斗节奏都比较自由。\n\n"
            "它适合想在碎片时间慢慢逛地图的玩家。\n\n"
            "我玩了 20 小时，最喜欢在山脊上看不同时间的光线。"
        )

        message = build_final_message(
            "原神", ai_body, experience="我玩了 20 小时，喜欢看风景。"
        )

        self.assertNotIn("发布前提示", message)
        self.assertIn("游戏采用明亮的卡通渲染", message)
        self.assertIn("我玩了 20 小时", message)

    def test_partial_experience_does_not_claim_story_missing(self):
        ai_body = (
            "游戏介绍：这是一段真实游戏介绍。\n"
            "故事/感想：我玩了几个晚上，喜欢在雨里散步。"
        )

        message = build_final_message(
            "原神", ai_body, experience="我玩了几个晚上，喜欢在雨里散步。"
        )
        body = message.split("---", 1)[0]

        self.assertIn("这是一段真实游戏介绍。", body)
        self.assertNotIn("游戏介绍：", body)
        self.assertIn("我玩了几个晚上", body)
        self.assertNotIn("发布前提示", message)

    def test_markdown_bold_placeholders_are_removed(self):
        ai_body = (
            "**游玩时间：** 未提供\n"
            "**推荐人群及理由：** **待补充**\n"
            "**游戏介绍：** 这是一段真实游戏介绍。\n"
            "**故事/感想：** **未提供**"
        )

        message = build_final_message(
            "原神", ai_body, experience="我玩了 20 小时。",
        )
        body = message.split("---", 1)[0]

        self.assertNotIn("未提供", body)
        self.assertNotIn("待补充", body)
        self.assertIn("这是一段真实游戏介绍。", body)
        self.assertNotIn("游戏介绍：", body)
        self.assertNotIn("发布前提示", message)

    def test_markdown_and_colon_variants_of_placeholder_are_removed(self):
        variants = (
            "**游玩时间：** 未提供",
            "**游玩时间**：未提供",
            "**游玩时间**:  未提供",
            "__游玩时间__：未提供",
            "**游玩时间：** 未提供（信息不足）",
        )
        for line in variants:
            with self.subTest(line=line):
                message = build_final_message(
                    "原神", line + "\n后续有效自然段。", experience=""
                )
                body = message.split("---", 1)[0]
                self.assertNotIn("未提供", body)
                self.assertIn("后续有效自然段。", body)
                self.assertNotIn("发布前提示", message)

    def test_activity_field_names_are_recognized(self):
        ai_body = (
            "游戏当时发售平台：PC、iOS、Android\n"
            "玩游戏的时间：20 小时\n"
            "推荐人群：喜欢探索的玩家。\n"
            "游戏介绍：开放世界探索游戏。"
        )

        message = build_final_message(
            "原神", ai_body, experience="我玩了 20 小时。",
        )
        body = message.split("---", 1)[0]

        self.assertIn("游戏当时发售平台：PC、iOS、Android", body)
        self.assertIn("玩游戏的时间：20 小时", body)
        self.assertNotIn("发布前提示", message)

    def test_no_experience_story_section_is_skipped_until_next_field(self):
        ai_body = (
            "故事/感想：我玩了很久，感觉非常治愈。\n"
            "这是紧接着故事标签的段落。\n\n"
            "那天一直玩到半夜。\n"
            "游戏介绍：这是客观介绍。"
        )

        message = build_final_message("星露谷物语", ai_body, experience="")
        body = message.split("---", 1)[0]

        # 无经历时整节“故事/感想”都跳过（跨空行），直到下一个字段。
        self.assertNotIn("我玩了很久", body)
        self.assertNotIn("紧接着故事标签", body)
        self.assertNotIn("那天一直玩到半夜", body)
        self.assertIn("这是客观介绍。", body)

    def test_placeholder_removal_keeps_following_valid_paragraph(self):
        ai_body = (
            "**游玩时间：** 未提供\n\n"
            "游戏采用明亮的卡通渲染，探索和战斗节奏都比较自由。"
        )

        message = build_final_message("原神", ai_body, experience="")
        body = message.split("---", 1)[0]

        self.assertNotIn("游玩时间：未提供", body)
        self.assertIn("游戏采用明亮的卡通渲染", body)
        self.assertNotIn("游玩时间", body)

    def test_experience_story_remains_in_body(self):
        ai_body = (
            "发售平台：PC\n游玩时间：20 小时\n"
            "推荐人群及理由：喜欢农场经营的人。\n"
            "游戏介绍：农场经营游戏。\n故事/感想：我玩了 20 小时，喜欢钓鱼。"
        )

        message = build_final_message(
            "星露谷物语", ai_body, experience="我玩了 20 小时，喜欢钓鱼。"
        )
        body = message.split("---", 1)[0]

        self.assertIn("玩游戏的时间：20 小时", body)
        self.assertIn("我玩了 20 小时，喜欢钓鱼。", body)
        self.assertNotIn("故事/感想：", body)
        self.assertNotIn("故事/感想", body)


class WhitelistTests(unittest.TestCase):
    def test_empty_umo_list_allows_every_session(self):
        self.assertTrue(is_session_allowed("100", []))
        self.assertTrue(is_session_allowed("", []))

    def test_nonempty_umo_requires_exact_match(self):
        allowed = "qq_official_bot_a:FriendMessage:openid-1"
        self.assertTrue(is_session_allowed(allowed, [allowed]))
        self.assertFalse(is_session_allowed("", [allowed]))
        self.assertFalse(is_session_allowed(allowed.upper(), [allowed]))
        self.assertFalse(
            is_session_allowed(
                "qq_official_bot_b:FriendMessage:openid-1", [allowed]
            )
        )

    def test_invalid_umo_config_fails_closed(self):
        for invalid in (
            None, "", "qq_bot:FriendMessage:openid-1", 123, object()
        ):
            with self.subTest(invalid=type(invalid).__name__):
                self.assertEqual(
                    validate_umo_list(invalid), "invalid_whitelist_umos"
                )
                self.assertFalse(
                    is_session_allowed(
                        "qq_bot:FriendMessage:openid-1", invalid
                    )
                )

    def test_old_legacy_restrictions_require_migration(self):
        self.assertTrue(
            migration_required([], ["old-group"], [])
        )
        self.assertTrue(
            migration_required([], [], ["old-private"])
        )
        self.assertFalse(
            migration_required([], [], [])
        )
        self.assertFalse(
            migration_required(["umo"], ["old-group"], [])
        )

    def test_unique_session_group_umos_are_not_group_wide(self):
        first = "qq_official_bot_a:GroupMessage:openid-1_group-1"
        second = "qq_official_bot_a:GroupMessage:openid-2_group-1"
        self.assertTrue(is_session_allowed(first, [first]))
        self.assertFalse(is_session_allowed(second, [first]))


class SearchServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_meta_description_supports_common_attribute_orders(self):
        self.assertEqual(
            _extract_meta_description(
                '<meta name="description" content="游戏介绍">'
            ),
            "游戏介绍",
        )
        self.assertEqual(
            _extract_meta_description(
                '<meta content="游戏介绍" name="description">'
            ),
            "游戏介绍",
        )

    def test_summary_falls_back_to_ellipsis_without_late_sentence_end(self):
        text = "第一句。" + "内容" * 50 + "。最后一句。"

        summary = _build_summary(text, max_length=30)

        self.assertEqual(summary, text[:30] + "...")

    async def test_disabled_search_returns_stable_result(self):
        result = await SearchService(enabled=False).search_game_info("原神")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["summary"], "")


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_window_limit_is_enforced(self):
        limiter = RateLimiter(window_minutes=10, max_requests=1, daily_max=2)
        storage = MemoryKV()

        first = await limiter.check_and_record("123", storage)
        second = await limiter.check_and_record("123", storage)

        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])

    async def test_expired_window_resets_count(self):
        limiter = RateLimiter(window_minutes=10, max_requests=1, daily_max=2)
        storage = MemoryKV()

        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            side_effect=[1000.0, 1601.0],
        ):
            first = await limiter.check_and_record("123", storage)
            second = await limiter.check_and_record("123", storage)

        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])


class AIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_requires_configuration(self):
        client = AIClient(base_url="", api_key="", model="test-model")

        with self.assertRaises(AIClientNotConfigured):
            await client.generate("测试")

    def test_unknown_api_format_falls_back_to_chat_completions(self):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="test-model",
            api_format="unknown",
        )

        self.assertEqual(client.api_format, API_FORMAT_CHAT_COMPLETIONS)

    async def test_chat_completions_request_and_response(self):
        async def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["max_tokens"], 2048)
            return httpx.Response(
                200,
                json={
                    "model": "chat-model",
                    "choices": [
                        {"message": {"content": "Chat Completions 正文"}}
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                    },
                },
            )

        client = AIClient(
            base_url="https://example.com/v1/",
            api_key="test-key",
            model="configured-model",
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        try:
            result = await client.generate("生成内容")
        finally:
            await client.close()

        self.assertEqual(result["content"], "Chat Completions 正文")
        self.assertEqual(result["model"], "chat-model")
        self.assertEqual(result["api_format"], API_FORMAT_CHAT_COMPLETIONS)
        self.assertEqual(result["token_usage"]["total_tokens"], 30)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["finish_reason"], "")

    async def test_chat_length_finish_marks_truncated(self):
        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "model": "chat-model",
                    "choices": [{
                        "message": {"content": "被截断的正文"},
                        "finish_reason": "length",
                    }],
                },
            )

        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="configured-model",
        )
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        try:
            result = await client.generate("生成内容")
        finally:
            await client.close()

        self.assertTrue(result["truncated"])
        self.assertEqual(result["finish_reason"], "length")

    async def test_responses_request_and_response(self):
        async def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertEqual(payload["instructions"], "系统提示词")
            self.assertEqual(payload["input"], "用户提示词")
            self.assertEqual(payload["max_output_tokens"], 99)
            self.assertFalse(payload["store"])
            self.assertNotIn("messages", payload)
            return httpx.Response(
                200,
                json={
                    "model": "responses-model",
                    "output": [
                        {"type": "reasoning", "content": []},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Responses 正文",
                                }
                            ],
                        },
                    ],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "total_tokens": 33,
                    },
                },
            )

        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="configured-model",
            api_format=API_FORMAT_RESPONSES,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        try:
            result = await client._call_ai(
                system_prompt="系统提示词",
                user_prompt="用户提示词",
                temperature=0.3,
                max_tokens=99,
            )
        finally:
            await client.close()

        self.assertEqual(result["content"], "Responses 正文")
        self.assertEqual(result["model"], "responses-model")
        self.assertEqual(result["api_format"], API_FORMAT_RESPONSES)
        self.assertEqual(
            result["token_usage"],
            {
                "prompt_tokens": 11,
                "completion_tokens": 22,
                "total_tokens": 33,
            },
        )

    def test_responses_top_level_output_text_compatibility(self):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="test-model",
            api_format=API_FORMAT_RESPONSES,
        )

        self.assertEqual(
            client._extract_content({"output_text": "兼容服务正文"}),
            "兼容服务正文",
        )


class ActivityFormatTests(unittest.TestCase):
    PENDING_TIME = "玩游戏的时间：玩了一段时间"

    def test_prefix_order_and_optional_tail(self):
        body = (
            "发售平台：PC / Switch\n"
            "游玩时间：初中暑假\n"
            "推荐人群：喜欢慢节奏经营的人\n"
            "游戏介绍：星露谷物语是农场经营游戏。\n"
            "故事/感想：那年暑假我每天钓鱼。"
        )
        message = build_final_message(
            "星露谷物语",
            body,
            experience="初中暑假开始玩的",
            extras={"许愿卡": "百分之一福利", "区服": "QQ 区", "系统": "iOS"},
        )
        positions = [
            message.index("游戏名称：星露谷物语"),
            message.index("游戏当时发售平台：PC / Switch"),
            message.index("玩游戏的时间：初中暑假"),
            message.index("推荐人群（理由）：喜欢慢节奏经营的人"),
            message.index("星露谷物语是农场经营游戏。"),
            message.index("许愿卡：百分之一福利"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(message.index("那年暑假我每天钓鱼。"), message.index("许愿卡："))
        self.assertIn("区服：QQ 区", message)
        self.assertIn("系统：iOS", message)
        # 游戏介绍/故事小标题被去掉，合并为正文自然段
        self.assertNotIn("游戏介绍：", message)
        self.assertNotIn("故事/感想：", message)

    def test_duplicate_fields_and_fake_tail_are_sanitized(self):
        body = (
            "**游戏名称：伪造名**\n"
            "游戏名称：另一个名字\n"
            "游戏当时发售平台：PC\n"
            "发售平台：Switch\n"
            "**许愿卡：胡说八道**\n"
            "游戏ID：99999\n"
            "推荐人群（理由）：喜欢慢节奏的人。\n"
            "游戏介绍：这是介绍。"
        )
        message = build_final_message(
            "原神", body, experience="我玩了 20 小时",
        )
        self.assertEqual(message.count("游戏名称："), 1)
        self.assertNotIn("伪造名", message)
        self.assertNotIn("另一个名字", message)
        self.assertNotIn("许愿卡", message)
        self.assertNotIn("游戏ID", message)
        self.assertEqual(message.count("游戏当时发售平台："), 1)
        self.assertIn("游戏当时发售平台：Switch", message)
        self.assertIn("推荐人群（理由）：喜欢慢节奏的人。", message)

    def test_model_play_time_is_kept_without_user_time(self):
        # 用户没给时间时，模型结合游戏情况补的合理时期直接采用。
        message = build_final_message(
            "原神",
            "玩游戏的时间：大学期间\n游戏介绍：这是介绍。",
            experience="我觉得挺好玩的",
        )
        self.assertIn("玩游戏的时间：大学期间", message)
        self.assertNotIn("【请补充", message)
        self.assertIn("这是介绍。", message)

    def test_user_play_time_overrides_model_output(self):
        message = build_final_message(
            "原神",
            "玩游戏的时间：约 50 小时\n游戏介绍：这是介绍。",
            experience="初中暑假开始玩的",
        )
        self.assertIn("玩游戏的时间：初中暑假", message)
        self.assertNotIn("50 小时", message)

    def test_user_season_phrase_is_kept(self):
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="去年冬天入坑的",
        )
        self.assertIn("玩游戏的时间：去年冬天", message)

    def test_release_age_is_not_user_play_time(self):
        # 审查回归：「发售 10 年了」说的是游戏年龄，不是玩家游玩时间。
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="游戏发售 10 年了，我去年冬天入坑",
        )
        self.assertIn("玩游戏的时间：去年冬天", message)
        self.assertNotIn("10 年", message)

    def test_year_month_play_start_is_kept_whole(self):
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="2024年8月开始玩",
        )
        self.assertIn("玩游戏的时间：2024年8月", message)

    def test_waiting_and_install_time_is_not_play_time(self):
        # 审查回归：等下载/安装的时长不是游玩时间，只取与玩直接关联的时期。
        waiting = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="先等了 3 天才下载，去年冬天开始玩",
        )
        self.assertIn("玩游戏的时间：去年冬天", waiting)
        self.assertNotIn("3 天", waiting)
        installing = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="安装花了 2 小时，昨晚才开始玩",
        )
        self.assertIn("玩游戏的时间：昨晚", installing)
        self.assertNotIn("2 小时", installing)

    def test_decimal_and_actual_play_time_are_kept(self):
        # 审查回归：1.5 小时不能被小数点切成两段；同句里「流程 30 小时」
        # 与「我只玩了 2 小时」并存时，取真正落地的游玩时长。
        decimal = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="我玩了 1.5 小时，喜欢合作",
        )
        self.assertIn("玩游戏的时间：1.5 小时", decimal)
        actual = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="游戏流程大概 30 小时但我只玩了 2 小时",
        )
        self.assertIn("玩游戏的时间：2 小时", actual)
        self.assertNotIn("玩游戏的时间：30 小时", actual)

    def test_planned_play_time_is_omitted(self):
        # 审查回归：「本来想玩 20 小时」是计划，不是已发生的经历。
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="我本来想玩 20 小时，但没打开过",
        )
        self.assertIn(self.PENDING_TIME, message)
        self.assertIn("这是介绍。", message)

    def test_negated_or_other_person_play_time_is_omitted(self):
        # 审查回归：否定（还没有玩过）、他人主语（看别人玩）、等待朋友
        # 都不是发帖者本人的游玩经历，自由文本推断一律省略。
        for experience in (
            "我还没有玩过 2 小时",
            "我只看别人玩了 6 小时",
            "等朋友 3 天才开始玩",
        ):
            message = build_final_message(
                "原神", "游戏介绍：这是介绍。", experience=experience
            )
            self.assertIn(self.PENDING_TIME, message, experience)
            self.assertIn("这是介绍。", message)
        # 同伴一起玩仍算本人。
        together = build_final_message(
            "原神", "游戏介绍：这是介绍。", experience="跟朋友一起玩了 20 小时"
        )
        self.assertIn("玩游戏的时间：20 小时", together)

    def test_ambiguous_play_time_ownership_is_omitted(self):
        # 审查回归：话语里带他人或等待语境时，时长归属不明确就不填。
        for experience in (
            "我还没真正玩过 2 小时",
            "我看哥哥玩了 6 小时",
            "我等开黑 3 天才开始玩",
            "我玩的时候朋友在旁边看了 6 小时",
        ):
            message = build_final_message(
                "原神", "游戏介绍：这是介绍。", experience=experience
            )
            self.assertIn(self.PENDING_TIME, message, experience)

    def test_pre_play_or_planned_duration_is_omitted(self):
        # 审查回归：攻略时长（玩之前看）、计划时长（打算先玩）都不能写成游玩。
        for experience in (
            "玩之前我看了 6 小时攻略",
            "我打算先玩 20 小时再决定",
        ):
            message = build_final_message(
                "原神", "游戏介绍：这是介绍。", experience=experience
            )
            self.assertIn(self.PENDING_TIME, message, experience)
        # 「我的朋友」是同伴，仍算本人游玩。
        message = build_final_message(
            "原神", "游戏介绍：这是介绍。", experience="我和我的朋友一起玩了 2 小时"
        )
        self.assertIn("玩游戏的时间：2 小时", message)

    def test_friend_play_vs_together_and_guide_time(self):
        # 审查回归：朋友的游玩不算本人；一起玩才算本人；同句另一动作
        # （看攻略）的时长也不算游玩。
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="我的朋友玩了 2 小时，我没玩",
        )
        self.assertIn(self.PENDING_TIME, message)
        self.assertNotIn("玩游戏的时间：2 小时", message)
        together = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="我和我的朋友一起玩了 2 小时",
        )
        self.assertIn("玩游戏的时间：2 小时", together)
        guide = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="玩游戏的时候我看了 6 小时攻略",
        )
        self.assertIn(self.PENDING_TIME, guide)

    def test_duration_must_directly_link_to_play_action(self):
        # 审查回归：时长要直接跟在本人游玩动作后；「还加班了 6 小时」
        # 是别的动作的时长，不能算游玩。
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="玩游戏那天我还加班了 6 小时",
        )
        self.assertIn(self.PENDING_TIME, message)
        self.assertNotIn("玩游戏的时间：6 小时", message)
        direct = build_final_message(
            "原神", "游戏介绍：这是介绍。", experience="玩游戏那天我玩了 2 小时"
        )
        self.assertIn("玩游戏的时间：2 小时", direct)

    def test_duration_before_action_is_not_play_time(self):
        # 审查回归：时长在游玩动作之前（加班 6 小时后才开始玩）不算游玩；
        # 只有日期/时点（昨晚）可以放在动作之前。
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="我加班了 6 小时后才开始玩",
        )
        self.assertIn(self.PENDING_TIME, message)
        mixed = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="我加班了 6 小时后才开始玩，昨晚其实才玩了一会儿",
        )
        self.assertIn("玩游戏的时间：昨晚", mixed)
        self.assertNotIn("6 小时", mixed)

    def test_half_hour_and_minutes_are_kept(self):
        # 官方模板要求时间行必须存在，支持「半小时」「30 分钟」这类写法。
        for experience, expected in (
            ("我玩了半小时", "半小时"),
            ("我玩了30分钟", "30分钟"),
        ):
            message = build_final_message(
                "原神", "游戏介绍：这是介绍。", experience=experience
            )
            self.assertIn("玩游戏的时间：" + expected, message, experience)

    def test_standalone_time_line_is_not_game_name(self):
        # 用户独立一行写「玩游戏的时间：半小时」，值不能进游戏名，且优先采用。
        matched, game_name, experience = parse_writing_request(
            "小作文 星露谷物语\n玩游戏的时间：半小时"
        )
        self.assertTrue(matched)
        self.assertEqual(game_name, "星露谷物语")
        fields = parse_activity_fields("小作文 星露谷物语\n玩游戏的时间：半小时")
        message = build_final_message(
            "星露谷物语", "游戏介绍：这是介绍。",
            experience=experience, explicit_fields=fields,
        )
        self.assertIn("游戏名称：星露谷物语", message)
        self.assertIn("玩游戏的时间：半小时", message)

    def test_release_year_season_is_kept(self):
        # 「发售那年暑假开始玩」有玩家自己的时点依据，保留并输出。
        message = build_final_message(
            "原神", "游戏介绍：这是介绍。",
            experience="发售那年暑假开始玩，天天和朋友联机。",
        )
        self.assertIn("玩游戏的时间：发售那年暑假", message)

    def test_explicit_platform_field_wins(self):
        message = build_final_message(
            "原神", "游戏介绍：这是介绍。",
            experience="我玩了 2 小时",
            explicit_fields={"发售平台": "PC / Switch"},
        )
        self.assertIn("游戏当时发售平台：PC / Switch", message)

    def test_template_fixed_parts_are_verbatim(self):
        # 标题、两行活动说明、五个字段前缀逐字符核对。
        message = build_final_message(
            "原神", "游戏介绍：这是介绍。", experience="我玩了 2 小时"
        )
        self.assertTrue(message.startswith("【我的百分之一】+【原神】"))
        self.assertIn(
            "什么是【我的百分之一】见帖子说明：\n"
            "https://www.taptap.cn/moment/371075389700702390\n"
            "（这个需要加，省得新玩家不知道是啥情况）",
            message,
        )
        for line in (
            "游戏名称：原神",
            "游戏当时发售平台：",
            "玩游戏的时间：2 小时",
            "推荐人群（理由）：",
            "正文：",
        ):
            self.assertIn(line, message)
        body = message.split("正文：", 1)[1]
        # 首段与「正文：」同一行（官方模板），不是下一行开头。
        self.assertFalse(body.startswith("\n"), body[:20])
        self.assertIn("这是介绍。", body.split("\n", 1)[0])
        self.assertNotIn("游戏介绍：", body)
        self.assertNotIn("故事/感想：", body)

    def test_model_platform_fills_constrained_prefix(self):
        # 受约束的两行前置（平台/推荐）可以由模型提供，时间仍只取用户依据。
        message = build_final_message(
            "原神",
            "游戏当时发售平台：PC / Switch\n"
            "推荐人群（理由）：喜欢慢节奏探索的人\n"
            "正文首段。",
            experience="我玩了半小时",
        )
        self.assertIn("游戏当时发售平台：PC / Switch", message)
        self.assertIn("推荐人群（理由）：喜欢慢节奏探索的人", message)
        self.assertIn("玩游戏的时间：半小时", message)
        self.assertNotIn("【请补充游戏当时发售平台】", message)

    def test_model_platform_is_kept_without_strict_evidence(self):
        # 新口径：模型按游戏常识补的平台直接采用，不再强行打回占位符。
        message = build_final_message(
            "原神", "游戏当时发售平台：PC / PS5\n正文首段。", experience=""
        )
        self.assertIn("游戏当时发售平台：PC / PS5", message)
        self.assertNotIn("【请补充", message)

    def test_missing_prefix_fields_use_natural_fallbacks(self):
        message = build_final_message(
            "艾尔登法环", "游戏介绍：开放世界动作 RPG。", experience=""
        )
        self.assertIn("游戏当时发售平台：多平台", message)
        self.assertIn("玩游戏的时间：玩了一段时间", message)
        self.assertIn("推荐人群（理由）：喜欢这类游戏的玩家", message)
        self.assertNotIn("【请补充", message)

    def test_model_platform_grounded_in_user_experience_is_kept(self):
        message = build_final_message(
            "原神",
            "游戏当时发售平台：PS5\n正文首段。",
            experience="PS5 首发那天我就买了，玩了 20 小时。",
        )
        self.assertIn("游戏当时发售平台：PS5", message)

    def test_body_label_kept_with_natural_fallback_when_body_missing(self):
        # 模型只返回前置字段时，固定模板保留「正文：」并给自然兜底，不出现占位符。
        message = build_final_message(
            "原神",
            "游戏当时发售平台：PC\n推荐人群（理由）：喜欢探索的人",
        )
        self.assertIn("\n正文：", message)
        self.assertNotIn("【请补充", message)
        self.assertNotIn("标题：", message)

    def test_time_conflicting_with_release_year_is_replaced(self):
        # 素材明确 2022 年发售，模型写 2018 年春天属于明显穿帮：换成发售后不久。
        message = build_final_message(
            "艾尔登法环",
            "玩游戏的时间：2018 年春天\n推荐人群（理由）：喜欢探索的人\n正文首段。",
            known_facts="《艾尔登法环》于 2022 年 2 月 25 日发售。",
        )
        self.assertIn("玩游戏的时间：2022 年发售后不久", message)
        self.assertNotIn("2018", message)

    def test_platform_fallback_uses_material_launch_platform(self):
        # 模型漏填平台时，用素材明确的首发平台，而不是笼统的「多平台」。
        message = build_final_message(
            "塞尔达传说 王国之泪",
            "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢解谜的人\n正文首段。",
            known_facts="《塞尔达传说 王国之泪》首发平台仅为 Nintendo Switch。",
        )
        self.assertIn("游戏当时发售平台：Nintendo Switch", message)
        self.assertNotIn("游戏当时发售平台：多平台", message)

    def test_play_time_compared_down_to_release_month_and_day(self):
        # 素材能确定到月日时，同年 1 月、2 月 24 日都早于 2 月 25 日发售。
        facts = "《艾尔登法环》于 2022 年 2 月 25 日发售。"
        for value in ("2022 年 1 月", "2022 年 2 月 24 日", "2021 年 12 月"):
            with self.subTest(value=value):
                message = build_final_message(
                    "艾尔登法环",
                    f"玩游戏的时间：{value}\n推荐人群（理由）：喜欢探索的人\n正文首段。",
                    known_facts=facts,
                )
                self.assertIn("玩游戏的时间：2022 年发售后不久", message)
                self.assertNotIn(value, message)
        # 发售之后或精度不足时不改：只到年份时无法判断月日。
        for value in ("2022 年 3 月", "2022 年春天", "2023 年 1 月"):
            with self.subTest(value=value):
                message = build_final_message(
                    "艾尔登法环",
                    f"玩游戏的时间：{value}\n推荐人群（理由）：喜欢探索的人\n正文首段。",
                    known_facts=facts,
                )
                self.assertIn(f"玩游戏的时间：{value}", message)

    def test_launch_platform_phrase_stops_before_follow_up_action(self):
        # 首发短语只修饰紧跟的平台：后续移植、手柄兼容都不算首发。
        cases = (
            ("《原神》首发于 PC 并于次年移植到 PS5。", "PC"),
            ("《原神》首发于 PC 并支持 PS5 手柄。", "PC"),
            ("《原神》首发于 PC。2021 年登陆 PS5。", "PC"),
        )
        for snippet, expected in cases:
            with self.subTest(snippet=snippet):
                message = build_final_message(
                    "原神",
                    "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢开放世界的人\n正文首段。",
                    known_facts=snippet,
                )
                self.assertIn(f"游戏当时发售平台：{expected}", message)
                self.assertNotIn("游戏当时发售平台：PC / PS5", message)
                self.assertNotIn("PS5\n", message)

    def test_launch_platform_keeps_console_generation(self):
        # Switch 2 与 Switch、Xbox One 与 Xbox Series 各自独立。
        cases = (
            (
                "《塞尔达传说 王国之泪》首发平台仅为 Nintendo Switch 2。",
                "Nintendo Switch",
                "Nintendo Switch 2",
            ),
            (
                "《光环 无限》首发平台为 Xbox One。",
                "Xbox Series X",
                "Xbox One",
            ),
            (
                "《塞尔达传说 王国之泪》首发平台仅 Nintendo Switch。",
                "Nintendo Switch",
                "Nintendo Switch",
            ),
        )
        for snippet, model_value, expected in cases:
            with self.subTest(snippet=snippet):
                message = build_final_message(
                    "测试游戏",
                    f"游戏当时发售平台：{model_value}\n"
                    "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢解谜的人\n正文首段。",
                    known_facts=snippet,
                )
                self.assertIn(f"游戏当时发售平台：{expected}", message)
                if expected != model_value:
                    self.assertNotIn(
                        f"游戏当时发售平台：{model_value}\n", message
                    )

    def test_digit_release_date_is_compared(self):
        # 素材用 2022-02-25 这类数字写法时，同样要挡住前一天的游玩时间。
        message = build_final_message(
            "艾尔登法环",
            "玩游戏的时间：2022 年 2 月 24 日开始玩\n"
            "推荐人群（理由）：喜欢探索的人\n正文首段。",
            known_facts="发售日期：2022-02-25",
        )
        self.assertIn("玩游戏的时间：2022 年发售后不久", message)
        self.assertNotIn("2022 年 2 月 24 日", message)

    def test_negated_launch_platform_is_not_evidence(self):
        # 否定说法不是首发依据：继续找同一素材里的正例。
        message = build_final_message(
            "原神",
            "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢开放世界的人\n正文首段。",
            known_facts="《原神》首发平台不包括 PS5，游戏首发于 PC。",
        )
        self.assertIn("游戏当时发售平台：PC", message)
        self.assertNotIn("PlayStation 5", message)

    def test_launch_platform_list_with_and(self):
        # 「以及」连接的是平台列表，不能在这里截断丢掉后面的平台。
        message = build_final_message(
            "原神",
            "玩游戏的时间：大学期间\n推荐人群（理由）：喜欢开放世界的人\n正文首段。",
            known_facts="《原神》首发平台包括 PC、PS4 以及 PS5。",
        )
        self.assertIn(
            "游戏当时发售平台：PC / PlayStation 4 / PlayStation 5", message
        )
        self.assertNotIn("游戏当时发售平台：多平台", message)

    def test_user_play_time_beats_release_year_check(self):
        # 用户显式给的时间仍然最高优先。
        message = build_final_message(
            "艾尔登法环",
            "正文首段。",
            experience="我 2018 年春天开始玩的",
            known_facts="《艾尔登法环》于 2022 年发售。",
        )
        self.assertIn("玩游戏的时间：2018 年", message)
        self.assertNotIn("2022 年发售后不久", message)

    def test_explicit_play_time_field_wins(self):
        # 用户明确填写的字段优先，不能被后面的自由文本改写或省略。
        message = build_final_message(
            "原神",
            "游戏介绍：这是介绍。",
            experience="玩游戏的时间：去年国庆假期\n游戏发售 10 年了",
        )
        self.assertIn("玩游戏的时间：去年国庆假期", message)
        self.assertNotIn("10 年", message)

    def test_no_experience_multiparagraph_story_is_fully_skipped(self):
        message = build_final_message(
            "原神",
            "故事/感想：第一段回忆。\n\n那天一直玩到半夜。\n游戏介绍：这是介绍。",
            experience="",
        )
        self.assertNotIn("第一段回忆", message)
        self.assertNotIn("那天一直玩到半夜", message)
        self.assertIn("这是介绍。", message)

    def test_missing_prefix_fields_are_omitted(self):
        message = build_final_message(
            "原神", "游戏介绍：开放世界探索游戏。", experience=""
        )
        self.assertIn("游戏名称：原神", message)
        self.assertNotIn("游玩时间：", message)
        self.assertNotIn("推荐人群：", message)
        self.assertIn("开放世界探索游戏。", message)

    def test_optional_tail_is_never_invented(self):
        message = build_final_message("原神", "正文内容。", experience="")
        for label in ("许愿卡", "游戏ID", "账号ID", "区服", "系统"):
            self.assertNotIn(label + "：", message)

    def test_parse_optional_extras_strips_fields_from_experience(self):
        cleaned, extras = parse_optional_extras(
            "我玩了 20 小时 许愿卡：百分之一 区服：QQ 系统：iOS"
        )
        self.assertEqual(cleaned, "我玩了 20 小时")
        self.assertEqual(
            extras,
            {"许愿卡": "百分之一", "区服": "QQ", "系统": "iOS"},
        )
        self.assertEqual(parse_optional_extras("玩了 5 小时")[1], {})


if __name__ == "__main__":
    unittest.main()
