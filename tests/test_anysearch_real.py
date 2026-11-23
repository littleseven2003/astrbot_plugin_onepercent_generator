# -*- coding: utf-8 -*-
"""真实 Anysearch 插件工具类 + 真实 AstrBot FunctionToolManager 集成测试。

默认跳过；运行前设置：
  ANYSEARCH_PLUGIN_PATH=/path/to/astrbot_plugin_anysearch
上游搜索客户端使用本地模拟，不发起 Anysearch 网络请求。
"""

import importlib
import json
import os
import sys
import unittest

PLUGIN_PATH = os.environ.get("ANYSEARCH_PLUGIN_PATH", "").strip()

try:
    from astrbot.core.provider.func_tool_manager import FunctionToolManager
    ASTRBOT_AVAILABLE = True
except Exception:
    FunctionToolManager = None
    ASTRBOT_AVAILABLE = False

if PLUGIN_PATH and os.path.isdir(PLUGIN_PATH):
    parent = os.path.dirname(PLUGIN_PATH.rstrip("/"))
    if parent not in sys.path:
        sys.path.insert(0, parent)

try:
    from astrbot_plugin_anysearch.tools import AnySearchSearchTool
    PLUGIN_IMPORTABLE = True
except Exception:
    AnySearchSearchTool = None
    PLUGIN_IMPORTABLE = False

from astrbot_plugin_onepercent_generator.search_service import (
    AnysearchSearchBackend,
    SearchService,
)


class FakeAnySearchClient:
    def __init__(self, payload=None, delay=0.0, error=None):
        self.payload = payload
        self.delay = delay
        self.error = error

    async def search(self, query, **kwargs):
        import asyncio
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.payload


class FakeEvent:
    unified_msg_origin = "qq_official:FriendMessage:openid-real-test"


class FakeContext:
    def __init__(self, manager):
        self.manager = manager

    def get_llm_tool_manager(self):
        return self.manager


class FakeOfficialBackend:
    name = "astrbot"

    def __init__(self, available=False, reason="astrbot_search_disabled"):
        self.available = available
        self.reason = reason

    async def availability(self, event=None):
        return self.available, self.reason

    async def search(self, query, max_results, event=None, timeout_s=None):
        raise AssertionError("official fake must not search")

    async def close(self):
        pass


@unittest.skipUnless(
    ASTRBOT_AVAILABLE and PLUGIN_IMPORTABLE,
    "需要真实 AstrBot 和 ANYSEARCH_PLUGIN_PATH",
)
class RealAnysearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, tool):
        manager = FunctionToolManager()
        manager.func_list.append(tool)
        return manager

    async def test_real_tool_class_is_discovered_through_real_manager(self):
        payload = json.dumps({
            "results": [{
                "title": "真实工具类结果",
                "url": "https://example.com/real",
                "snippet": "本地模拟上游",
            }]
        }, ensure_ascii=False)
        tool = AnySearchSearchTool(
            client=FakeAnySearchClient(payload=payload)
        )
        tool.active = True
        manager = self._manager(tool)
        backend = AnysearchSearchBackend(context=FakeContext(manager))

        available, reason = await backend.availability(FakeEvent())
        result = await backend.search("原神", 1, event=FakeEvent())

        self.assertTrue(available, reason)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.results[0]["url"], "https://example.com/real")

    async def test_official_disabled_anysearch_succeeds_through_service(self):
        payload = json.dumps({
            "results": [{
                "title": "Anysearch 结果",
                "url": "https://example.com/a",
                "snippet": "摘要",
            }]
        }, ensure_ascii=False)
        tool = AnySearchSearchTool(
            client=FakeAnySearchClient(payload=payload)
        )
        manager = self._manager(tool)
        service = SearchService(
            enabled=True,
            mode="astrbot",
            timeout_ms=2000,
            result_count=1,
            backends={
                "astrbot": FakeOfficialBackend(),
                "anysearch": AnysearchSearchBackend(
                    context=FakeContext(manager)
                ),
            },
        )
        result = await service.search_game_info("原神", event=FakeEvent())
        await service.close()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "anysearch")
        self.assertEqual(result["skipped"][0]["backend"], "astrbot")

    async def test_real_tool_disabled_is_not_called(self):
        tool = AnySearchSearchTool(
            client=FakeAnySearchClient(payload="{}")
        )
        tool.active = False
        manager = self._manager(tool)
        backend = AnysearchSearchBackend(context=FakeContext(manager))
        available, reason = await backend.availability(FakeEvent())
        self.assertFalse(available)
        self.assertEqual(reason, "anysearch_tool_disabled")

    async def test_real_tool_error_text_is_not_success(self):
        tool = AnySearchSearchTool(
            client=FakeAnySearchClient(payload="错误：Unauthorized")
        )
        manager = self._manager(tool)
        backend = AnysearchSearchBackend(context=FakeContext(manager))
        result = await backend.search("原神", 1, event=FakeEvent())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "anysearch_error")


if __name__ == "__main__":
    unittest.main()
