# -*- coding: utf-8 -*-
"""真实 AstrBot QQ 官方事件 + 插件 on_message 的离线完整路径测试。

需要隔离 AstrBot 4.29.4；未安装时跳过。只使用本地 Fake AI/Search/Rate，
不访问网络、不读取真实 Key。
"""

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

try:
    from astrbot.core.platform.astrbot_message import (
        AstrBotMessage,
        Group,
        MessageMember,
    )
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from astrbot_plugin_onepercent_generator.main import OnePercentGenerator
    ASTRBOT_AVAILABLE = True
except Exception:
    ASTRBOT_AVAILABLE = False


class FakeSearch:
    def __init__(self):
        self.calls = []

    async def search_game_info(self, game_name, event=None):
        self.calls.append(game_name)
        return {
            "status": "success",
            "summary": "公开资料：发售平台为 PC。",
            "provider": "mock",
            "duration_ms": 1,
            "results": [{
                "title": "原神",
                "url": "https://example.com/game",
                "snippet": "公开资料：发售平台为 PC。",
                "source": "mock",
            }],
            "effective_backend": "mock",
        }

    async def close(self):
        return None


class FakeAI:
    def __init__(self, content):
        self.content = content
        self.prompts = []

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return {
            "content": self.content,
            "model": "mock-model",
            "api_format": "chat_completions",
            "duration_ms": 1,
            "token_usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
            "finish_reason": "stop",
            "truncated": False,
        }

    async def summarize_search(self, *args, **kwargs):
        return {"content": "summary"}

    async def close(self):
        return None


class FakeRate:
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


def _text(message):
    if isinstance(message, dict):
        return str(message.get("text", ""))
    return message.get_plain_text()


def _event(umo, sender_id, text, group_id=""):
    message = AstrBotMessage()
    message.type = (
        MessageType.GROUP_MESSAGE if group_id
        else MessageType.FRIEND_MESSAGE
    )
    message.self_id = "bot"
    message.session_id = umo.rsplit(":", 1)[-1]
    message.sender = MessageMember(sender_id, "tester")
    message.message_str = text
    message.message = []
    if group_id:
        message.group = Group(group_id)
    metadata = PlatformMetadata(
        name="qq_official", description="QQ Official",
        id=umo.split(":", 1)[0],
    )
    return QQOfficialMessageEvent(
        text, message, metadata, message.session_id, bot=object()
    )


def _plugin(umo, admin_qqs=None):
    class Ctx:
        pass

    config = {
        "ai_config": {
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "test-key",
            "model": "mock-model",
        },
        "search_config": {
            "enabled": False,
            "mode": "off",
            "summarize_enabled": False,
            "timeout_ms": 100,
            "result_count": 3,
        },
        "rate_limit": {
            "window_minutes": 10,
            "max_requests_per_window": 3,
            "daily_max": 20,
        },
        "admin_qqs": admin_qqs or [],
        "preset_games": ["原神"],
        "whitelist_mode": "umo",
        "whitelist_umos": [umo],
        "whitelist_groups": [],
        "whitelist_privates": [],
        "show_generation_stats": False,
    }
    plugin = OnePercentGenerator(Ctx(), config)
    plugin.search_service = FakeSearch()
    plugin.rate_limiter = FakeRate()
    return plugin


@unittest.skipUnless(ASTRBOT_AVAILABLE, "需要隔离 AstrBot 环境")
class OfficialPluginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_private_experience_generates_copyable_article(self):
        umo = "qq_official_bot: FriendMessage:openid-1".replace(" ", "")
        event = _event(
            umo, "openid-1",
            "小作文 原神 经历：我玩了 20 小时，喜欢爬山。",
        )
        plugin = _plugin(umo)
        plugin.ai_client = FakeAI(
            "发售平台：PC\n游玩时间：20 小时\n"
            "推荐人群及理由：喜欢探索的人。\n"
            "游戏介绍：开放世界探索游戏。\n"
            "故事/感想：我玩了 20 小时，喜欢爬山。"
        )

        messages = [item async for item in plugin.on_message(event)]
        final = [
            _text(m) for m in messages
            if "【我的百分之一】" in _text(m)
        ][0]

        self.assertEqual(plugin.search_service.calls, ["原神"])
        self.assertTrue(final.startswith("【我的百分之一】+【原神】"))
        self.assertIn("游戏名称：原神", final)
        self.assertIn("故事/感想：我玩了 20 小时", final)
        self.assertIn("请复制标题和正文后自行发布", final)

    async def test_qq_private_missing_experience_keeps_story_outside_body(self):
        umo = "qq_official_bot:FriendMessage:openid-2"
        event = _event(umo, "openid-2", "小作文 原神")
        plugin = _plugin(umo)
        plugin.ai_client = FakeAI(
            "发售平台：PC\n游戏介绍：开放世界探索游戏。"
        )

        messages = [item async for item in plugin.on_message(event)]
        final = [
            _text(m) for m in messages
            if "【我的百分之一】" in _text(m)
        ][0]
        body = final.split("---", 1)[0]

        self.assertNotIn("故事/感想", body)
        self.assertIn("未提供个人经历", final)
        self.assertIn("故事/感想", final)

    async def test_qq_admin_openid_can_be_configured_without_numeric_id(self):
        umo = "qq_official_bot:FriendMessage:openid-admin"
        event = _event(umo, "openid-admin", "小作文 经历：我玩了 20 小时")
        plugin = _plugin(umo, admin_qqs=["openid-admin"])
        plugin.ai_client = FakeAI("正文")

        messages = [item async for item in plugin.on_message(event)]

        self.assertTrue(any(
            "请同时提供游戏名" in _text(m) for m in messages
        ))
        self.assertEqual(plugin.search_service.calls, [])
        self.assertEqual(plugin.rate_limiter.calls, [])

    async def test_qq_group_umo_must_match_exact_session(self):
        allowed = "qq_official_bot:GroupMessage:openid-1_group-1"
        event = _event(
            "qq_official_bot:GroupMessage:openid-2_group-1",
            "openid-2", "小作文 原神", group_id="group-1",
        )
        plugin = _plugin(allowed)
        plugin.ai_client = FakeAI("正文")

        messages = [item async for item in plugin.on_message(event)]

        self.assertIn("不在白名单", _text(messages[0]))
        self.assertEqual(plugin.search_service.calls, [])


if __name__ == "__main__":
    unittest.main()
