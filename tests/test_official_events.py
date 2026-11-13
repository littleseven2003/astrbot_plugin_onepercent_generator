# -*- coding: utf-8 -*-
"""真实 AstrBot 事件类的 UMO/UID 与 unique_session 兼容测试。

该文件在未安装 AstrBot 的 Python 环境中跳过；在隔离 AstrBot 4.29.4
环境中使用真实 QQ 官方/WebChat 事件类。不发送消息、不访问网络。
"""

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

try:
    from astrbot.core.event_bus import UNIQUE_SESSION_ID_BUILDERS
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
    from astrbot.core.platform.sources.webchat.webchat_event import (
        WebChatMessageEvent,
    )
    ASTRBOT_AVAILABLE = True
except Exception:
    ASTRBOT_AVAILABLE = False

from astrbot_plugin_onepercent_generator.whitelist import (
    is_session_allowed,
)


def _message(
    message_type,
    sender_id,
    group_id="",
    session_id=None,
    text="小作文 原神",
):
    message = AstrBotMessage()
    message.type = message_type
    message.self_id = "bot"
    message.session_id = session_id or sender_id
    message.sender = MessageMember(sender_id, "tester")
    message.message_str = text
    message.message = []
    if group_id:
        message.group = Group(group_id)
    return message


def _qq_event(
    platform_id,
    message_type,
    sender_id,
    group_id="",
    session_id=None,
):
    metadata = PlatformMetadata(
        name="qq_official",
        description="QQ Official",
        id=platform_id,
    )
    return QQOfficialMessageEvent(
        "小作文 原神",
        _message(
            message_type, sender_id, group_id, session_id
        ),
        metadata,
        session_id or sender_id,
        bot=object(),
    )


@unittest.skipUnless(ASTRBOT_AVAILABLE, "需要隔离 AstrBot 环境")
class OfficialEventCompatibilityTests(unittest.TestCase):
    def test_qq_private_umo_and_openid_uid(self):
        event = _qq_event(
            "qq_official_bot_a",
            MessageType.FRIEND_MESSAGE,
            "openid-private-1",
        )

        self.assertEqual(
            event.unified_msg_origin,
            "qq_official_bot_a:FriendMessage:openid-private-1",
        )
        self.assertEqual(event.get_sender_id(), "openid-private-1")
        self.assertEqual(event.get_group_id(), "")

    def test_two_platform_instances_do_not_share_umo(self):
        first = _qq_event(
            "qq_official_bot_a",
            MessageType.FRIEND_MESSAGE,
            "openid-1",
        )
        second = _qq_event(
            "qq_official_bot_b",
            MessageType.FRIEND_MESSAGE,
            "openid-1",
        )

        self.assertNotEqual(
            first.unified_msg_origin, second.unified_msg_origin
        )
        self.assertTrue(
            is_session_allowed(
                "", "private", [], [], mode="umo",
                umo=first.unified_msg_origin,
                whitelist_umos=[first.unified_msg_origin],
            )
        )
        self.assertFalse(
            is_session_allowed(
                "", "private", [], [], mode="umo",
                umo=second.unified_msg_origin,
                whitelist_umos=[first.unified_msg_origin],
            )
        )

    def test_group_unique_session_builder_is_per_sender(self):
        first = _qq_event(
            "qq_official_bot_a",
            MessageType.GROUP_MESSAGE,
            "openid-1",
            group_id="group-1",
            session_id="openid-1_group-1",
        )
        second = _qq_event(
            "qq_official_bot_a",
            MessageType.GROUP_MESSAGE,
            "openid-2",
            group_id="group-1",
            session_id="openid-2_group-1",
        )
        builder = UNIQUE_SESSION_ID_BUILDERS["qq_official"]

        self.assertEqual(builder(first), "openid-1_group-1")
        self.assertEqual(builder(second), "openid-2_group-1")
        self.assertNotEqual(
            first.unified_msg_origin, second.unified_msg_origin
        )
        self.assertTrue(
            is_session_allowed(
                "", "group", [], [], mode="umo",
                umo=first.unified_msg_origin,
                whitelist_umos=[first.unified_msg_origin],
            )
        )
        self.assertFalse(
            is_session_allowed(
                "", "group", [], [], mode="umo",
                umo=second.unified_msg_origin,
                whitelist_umos=[first.unified_msg_origin],
            )
        )

    def test_group_without_unique_session_can_use_group_session_umo(self):
        event = _qq_event(
            "qq_official_bot_a",
            MessageType.GROUP_MESSAGE,
            "openid-1",
            group_id="group-1",
            session_id="group-1",
        )

        self.assertEqual(
            event.unified_msg_origin,
            "qq_official_bot_a:GroupMessage:group-1",
        )
        self.assertTrue(
            is_session_allowed(
                "", "group", [], [], mode="umo",
                umo=event.unified_msg_origin,
                whitelist_umos=[event.unified_msg_origin],
            )
        )

    def test_qq_group_umo_does_not_imply_admin(self):
        event = _qq_event(
            "qq_official_bot_a",
            MessageType.GROUP_MESSAGE,
            "openid-member",
            group_id="group-1",
            session_id="openid-member_group-1",
        )

        self.assertNotEqual(
            event.get_sender_id(), event.get_group_id()
        )
        self.assertNotIn(":GroupMessage:", event.get_sender_id())

    def test_webchat_event_has_exact_umo(self):
        metadata = PlatformMetadata(
            name="webchat",
            description="WebChat",
            id="webchat",
        )
        event = WebChatMessageEvent(
            "小作文 原神",
            _message(
                MessageType.FRIEND_MESSAGE,
                "webchat-user",
                session_id="webchat-user",
            ),
            metadata,
            "webchat-user",
        )

        self.assertEqual(
            event.unified_msg_origin,
            "webchat:FriendMessage:webchat-user",
        )
        self.assertTrue(
            is_session_allowed(
                "", "private", [], [], mode="umo",
                umo=event.unified_msg_origin,
                whitelist_umos=[event.unified_msg_origin],
            )
        )
        self.assertFalse(
            is_session_allowed(
                "", "private", [], [], mode="umo",
                umo=event.unified_msg_origin,
                whitelist_umos=["webchat:FriendMessage:other"],
            )
        )


if __name__ == "__main__":
    unittest.main()
