# -*- coding: utf-8 -*-
"""真实 AstrBot Web 请求下的身份保存并发边界回归。

用 asyncio.Event 精确控制“保存暂停/请求进入”的时序，不使用计时等待或概率压力测试。
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import MethodType
from unittest.mock import AsyncMock, patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

try:
    import astrbot.api.web  # noqa: F401
    from astrbot_plugin_onepercent_generator.main import OnePercentGenerator
    from astrbot_plugin_onepercent_generator.rate_limiter import RateLimiter
    ASTRBOT_AVAILABLE = True
except Exception:
    ASTRBOT_AVAILABLE = False

if ASTRBOT_AVAILABLE:
    from tests.test_config_center import (
        Config,
        MemoryKVWithDelete,
        SavingPlugin,
    )
else:
    # 无 AstrBot 环境时模块仍需可导入；下方测试类整体 skip。
    Config = object
    MemoryKVWithDelete = object


class PausingConfig(Config):
    def __init__(self, data, fail=False):
        super().__init__(data)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail
        self.calls = 0

    async def save_config_async(self):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        if self.fail:
            return False
        self.saved += 1
        return True


class FailingLinkKV(MemoryKVWithDelete):
    async def put_kv_data(self, key, value):
        if key.startswith("rate_limit:link:"):
            raise RuntimeError("kv write failed")
        await super().put_kv_data(key, value)


@unittest.skipUnless(ASTRBOT_AVAILABLE, "需要真实 AstrBot Web API")
class IdentityConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, links=None, daily_max=1):
        return {
            "rate_limit": {
                "window_minutes": 10,
                "max_requests_per_window": 10,
                "daily_max": daily_max,
            },
            "session_settings": {
                "allow_private": True, "allow_group": True, "sessions": [],
            },
            "identity_links": links or [],
            "admin_users": [],
        }

    def _plugin(self, config, storage, daily_max=1):
        plugin = SavingPlugin(config, storage)
        plugin.rate_limiter = RateLimiter(
            window_minutes=10, max_requests=10, daily_max=daily_max
        )
        plugin._authorize_request = MethodType(
            OnePercentGenerator._authorize_request, plugin
        )
        return plugin

    async def _seed(self, plugin, identity, times=1):
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            for _ in range(times):
                result = await plugin.rate_limiter.check_and_record(
                    identity, plugin
                )
                self.assertTrue(result["allowed"], result)

    async def _save(self, plugin, payload):
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            return await plugin.config_center_api.save()

    async def test_link_save_holds_mapping_until_counts_merged(self):
        config = PausingConfig(self._config())
        storage = MemoryKVWithDelete()
        plugin = self._plugin(config, storage)
        await self._seed(plugin, "qq:a")

        save_task = asyncio.create_task(self._save(plugin, {
            "section": "identity",
            "data": {
                "action": "link", "connector": "qq", "uids": ["a", "b"],
            },
        }))
        await asyncio.wait_for(config.started.wait(), 1.0)

        checked = asyncio.Event()
        original = plugin.rate_limiter.check_and_record

        async def tracking_check(*args, **kwargs):
            checked.set()
            return await original(*args, **kwargs)

        plugin.rate_limiter.check_and_record = tracking_check
        request_task = asyncio.create_task(plugin._authorize_request(
            "qq", "a", "qq:FriendMessage:a", "原神", ""
        ))
        await asyncio.sleep(0)
        # 保存尚未提交计数时，普通请求不能先看到新 link 的空计数。
        self.assertFalse(checked.is_set())
        self.assertFalse(request_task.done())

        config.release.set()
        save_result = await asyncio.wait_for(save_task, 1.0)
        request_result = await asyncio.wait_for(request_task, 1.0)

        self.assertTrue(save_result.get("saved"), save_result)
        self.assertEqual(request_result["kind"], "message")
        self.assertIn("今日", request_result["text"])
        link_id = config["identity_links"][0]["id"]
        status = await plugin.rate_limiter.get_usage_status(
            f"link:{link_id}", plugin
        )
        self.assertEqual(status["daily_used"], 1)
        self.assertNotIn("rate_limit:qq:a", storage.values)

    async def test_unlink_save_holds_mapping_until_counts_split(self):
        link = {
            "id": "abc", "connector": "qq", "uids": ["a", "b"],
            "created_at": 1.0,
        }
        config = PausingConfig(self._config([link], daily_max=2))
        storage = MemoryKVWithDelete()
        plugin = self._plugin(config, storage, daily_max=2)
        await self._seed(plugin, "link:abc", times=2)

        save_task = asyncio.create_task(self._save(plugin, {
            "section": "identity",
            "data": {"action": "unlink", "id": "abc"},
        }))
        await asyncio.wait_for(config.started.wait(), 1.0)

        checked = asyncio.Event()
        original = plugin.rate_limiter.check_and_record

        async def tracking_check(*args, **kwargs):
            checked.set()
            return await original(*args, **kwargs)

        plugin.rate_limiter.check_and_record = tracking_check
        request_task = asyncio.create_task(plugin._authorize_request(
            "qq", "a", "qq:FriendMessage:a", "原神", ""
        ))
        await asyncio.sleep(0)
        self.assertFalse(checked.is_set())

        config.release.set()
        save_result = await asyncio.wait_for(save_task, 1.0)
        request_result = await asyncio.wait_for(request_task, 1.0)

        self.assertTrue(save_result.get("saved"), save_result)
        self.assertEqual(request_result["kind"], "message")
        self.assertIn("今日", request_result["text"])
        # 解除关联先复制共同历史，随后请求在 2 次额度上被拒绝。
        status = await plugin.rate_limiter.get_usage_status("qq:a", plugin)
        self.assertEqual(status["daily_used"], 2)
        self.assertNotIn("rate_limit:link:abc", storage.values)

    async def test_failed_save_rolls_back_before_request_resumes(self):
        config = PausingConfig(self._config(), fail=True)
        storage = MemoryKVWithDelete()
        plugin = self._plugin(config, storage)
        await self._seed(plugin, "qq:a")

        save_task = asyncio.create_task(self._save(plugin, {
            "section": "identity",
            "data": {
                "action": "link", "connector": "qq", "uids": ["a", "b"],
            },
        }))
        await asyncio.wait_for(config.started.wait(), 1.0)
        request_task = asyncio.create_task(plugin._authorize_request(
            "qq", "a", "qq:FriendMessage:a", "原神", ""
        ))
        await asyncio.sleep(0)
        self.assertFalse(request_task.done())

        config.release.set()
        save_result = await asyncio.wait_for(save_task, 1.0)
        request_result = await asyncio.wait_for(request_task, 1.0)

        self.assertEqual(save_result["status"], "error")
        self.assertEqual(config["identity_links"], [])
        self.assertEqual(plugin.config_center.identity_links, [])
        self.assertEqual(request_result["kind"], "message")
        self.assertIn("今日", request_result["text"])
        self.assertFalse([
            key for key in storage.values
            if key.startswith("rate_limit:link:")
        ])
        self.assertIn("rate_limit:qq:a", storage.values)

    async def test_failed_count_migration_rolls_back_before_request_resumes(self):
        config = PausingConfig(self._config())
        storage = FailingLinkKV()
        plugin = self._plugin(config, storage)
        await self._seed(plugin, "qq:a")

        save_task = asyncio.create_task(self._save(plugin, {
            "section": "identity",
            "data": {
                "action": "link", "connector": "qq", "uids": ["a", "b"],
            },
        }))
        await asyncio.wait_for(config.started.wait(), 1.0)
        request_task = asyncio.create_task(plugin._authorize_request(
            "qq", "a", "qq:FriendMessage:a", "原神", ""
        ))
        await asyncio.sleep(0)
        self.assertFalse(request_task.done())

        config.release.set()
        save_result = await asyncio.wait_for(save_task, 1.0)
        request_result = await asyncio.wait_for(request_task, 1.0)

        self.assertEqual(save_result["status"], "error")
        self.assertEqual(config["identity_links"], [])
        self.assertEqual(plugin.config_center.identity_key("qq", "a"), "qq:a")
        self.assertEqual(request_result["kind"], "message")
        self.assertIn("今日", request_result["text"])
        self.assertIn("rate_limit:qq:a", storage.values)

    async def test_request_that_already_has_old_identity_finishes_first(self):
        config = PausingConfig(self._config())
        storage = MemoryKVWithDelete()
        plugin = self._plugin(config, storage)

        entered = asyncio.Event()
        release_request = asyncio.Event()
        original = plugin.rate_limiter.check_and_record

        async def pausing_check(*args, **kwargs):
            entered.set()
            await release_request.wait()
            return await original(*args, **kwargs)

        plugin.rate_limiter.check_and_record = pausing_check
        request_task = asyncio.create_task(plugin._authorize_request(
            "qq", "a", "qq:FriendMessage:a", "原神", ""
        ))
        await asyncio.wait_for(entered.wait(), 1.0)

        save_task = asyncio.create_task(self._save(plugin, {
            "section": "identity",
            "data": {
                "action": "link", "connector": "qq", "uids": ["a", "b"],
            },
        }))
        await asyncio.sleep(0)
        # 请求已解析出旧身份 qq:a；保存必须等它完成计数后才能迁移。
        self.assertFalse(config.started.is_set())

        config.release.set()
        release_request.set()
        request_result = await asyncio.wait_for(request_task, 1.0)
        save_result = await asyncio.wait_for(save_task, 1.0)

        self.assertEqual(request_result["kind"], "ok")
        self.assertEqual(request_result["identity_key"], "qq:a")
        self.assertTrue(save_result.get("saved"), save_result)
        link_id = config["identity_links"][0]["id"]
        status = await plugin.rate_limiter.get_usage_status(
            f"link:{link_id}", plugin
        )
        self.assertEqual(status["daily_used"], 1)
        self.assertNotIn("rate_limit:qq:a", storage.values)
        self.assertEqual(plugin.config_center.identity_key("qq", "a"), f"link:{link_id}")


if __name__ == "__main__":
    unittest.main()
