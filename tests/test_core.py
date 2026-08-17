import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

from astrbot_plugin_onepercent_generator.ai_client import (  # noqa: E402
    AIClient,
    AIClientNotConfigured,
)
from astrbot_plugin_onepercent_generator.post_process import (  # noqa: E402
    build_final_message,
    strip_generated_title_and_game_name,
)
from astrbot_plugin_onepercent_generator.prompt import build_main_prompt  # noqa: E402
from astrbot_plugin_onepercent_generator.rate_limiter import RateLimiter  # noqa: E402
from astrbot_plugin_onepercent_generator.search_service import (  # noqa: E402
    SearchService,
    _build_summary,
    _extract_meta_description,
)
from astrbot_plugin_onepercent_generator.whitelist import (  # noqa: E402
    is_session_allowed,
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
        self.assertTrue(message.startswith("什么是【我的百分之一】"))


class WhitelistTests(unittest.TestCase):
    def test_empty_whitelist_allows_every_session(self):
        self.assertTrue(is_session_allowed("100", "group", [], []))
        self.assertTrue(is_session_allowed("200", "private", [], []))

    def test_configured_whitelist_rejects_unknown_session(self):
        self.assertFalse(
            is_session_allowed("100", "group", ["101"], ["201"])
        )


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


if __name__ == "__main__":
    unittest.main()
