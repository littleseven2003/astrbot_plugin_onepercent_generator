import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from astrbot_plugin_onepercent_generator.ai_client import (
    AIClient,
    API_FORMAT_ANTHROPIC_MESSAGES,
)
from astrbot_plugin_onepercent_generator.config_center import (
    ConfigCenter,
    parse_umo,
    session_entry_error,
)
from astrbot_plugin_onepercent_generator.config_center_api import (
    ConfigCenterAPI,
)
from astrbot_plugin_onepercent_generator.model_service import (
    MANUAL_PRESETS,
    ModelService,
)
from astrbot_plugin_onepercent_generator.rate_limiter import RateLimiter
from astrbot_plugin_onepercent_generator.stats_store import StatsStore


class Config:
    def __init__(self, data):
        self.data = data
        self.saved = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __contains__(self, key):
        return key in self.data

    def __iter__(self):
        return iter(self.data)

    def keys(self):
        return self.data.keys()

    def update(self, data):
        self.data.update(data)

    def clear(self):
        self.data.clear()

    def setdefault(self, key, value):
        return self.data.setdefault(key, value)

    async def save_config_async(self):
        self.saved += 1


class Plugin:
    def __init__(self, data):
        self.config = Config(data)


class StatsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "stats.json"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_stats_persist_and_filter(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        chat = {"key": "qq:FriendMessage:u", "connector": "qq", "type": "private", "label": "qq · private · u", "umo": "qq:FriendMessage:u"}
        await store.record({"kind": "request", "status": "started", "chat": chat})
        await store.record({"kind": "search", "attempted": True, "status": "success", "backend": "anysearch", "degraded": True, "chat": chat})
        await store.record({"kind": "model", "status": "success", "model_kind": "generate", "token_usage": {"total_tokens": 12}, "token_available": True, "chat": chat})
        await store.record({"kind": "model", "status": "failed", "model_kind": "summarize", "token_usage": None, "token_available": False, "chat": chat})
        await store.record({"kind": "search", "attempted": True, "status": "failed", "backend": "web", "degraded": True, "chat": {**chat, "key": "qq:FriendMessage:v", "umo": "qq:FriendMessage:v"}})

        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["requests"], 1)
        self.assertEqual(data["totals"]["searches"], 2)
        self.assertEqual(data["totals"]["searches_by_mode"], {"unknown": 2})
        self.assertEqual(data["matched_events"], 5)
        self.assertEqual(data["totals"]["tokens"], 12)
        self.assertEqual(data["totals"]["token_status"], "incomplete")
        self.assertEqual(data["totals"]["failures"], 1)
        # 旧事件没有 fallback 也没有 request_id：不能把每次 degraded 都猜成回退。
        self.assertEqual(data["totals"]["fallback_attempts"], 0)
        self.assertTrue(data["totals"]["fallback_incomplete"])
        self.assertEqual(data["totals"]["fallback_unknown_events"], 2)
        self.assertEqual(data["totals"]["tokens_by_kind"]["generate"], 12)
        self.assertGreaterEqual(len(data["chats"]), 2)
        none = await store.snapshot("7d", [])
        self.assertEqual(none["totals"]["requests"], 0)
        self.assertEqual(none["matched_events"], 0)
        self.assertEqual(none["totals"]["searches_by_mode"], {})
        one = await store.snapshot("7d", ["qq:FriendMessage:u"])
        self.assertEqual(one["totals"]["requests"], 1)
        self.assertEqual(one["totals"]["searches"], 1)
        self.assertEqual(one["matched_events"], 4)

        restarted = StatsStore(self.path, clock=lambda: now)
        again = await restarted.snapshot("all")
        self.assertEqual(again["totals"]["requests"], 1)
        self.assertEqual(again["started_at"], now)

    async def test_stats_reports_matched_events_and_search_modes(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        a = {"key": "qq:FriendMessage:a", "connector": "qq", "type": "private",
             "label": "a", "umo": "qq:FriendMessage:a"}
        b = {"key": "qq:FriendMessage:b", "connector": "qq", "type": "private",
             "label": "b", "umo": "qq:FriendMessage:b"}
        await store.record({"kind": "request", "chat": a, "ts": now - 60})
        await store.record({"kind": "search", "attempted": True, "status": "success",
                            "backend": "x", "search_mode": "auto", "chat": a,
                            "ts": now - 50})
        await store.record({"kind": "search", "attempted": True, "status": "success",
                            "backend": "x", "search_mode": "api", "chat": b,
                            "ts": now - 40})
        await store.record({"kind": "search", "attempted": True, "status": "failed",
                            "backend": "x", "degraded": False, "chat": a,
                            "ts": now - 30})
        data = await store.snapshot("7d")
        self.assertEqual(data["matched_events"], 4)
        self.assertEqual(data["totals"]["searches"], 3)
        self.assertEqual(
            data["totals"]["searches_by_mode"],
            {"auto": 1, "api": 1, "unknown": 1},
        )
        self.assertEqual(data["recent"][0]["search_mode"], "")
        one = await store.snapshot("7d", ["qq:FriendMessage:a"])
        self.assertEqual(one["matched_events"], 3)
        self.assertEqual(one["totals"]["searches"], 2)
        self.assertEqual(one["totals"]["searches_by_mode"], {"auto": 1, "unknown": 1})

    async def test_fallback_counts_only_attempts_after_the_first(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        for index in range(3):
            await store.record({
                "kind": "search",
                "attempted": True,
                "status": "success" if index == 2 else "failed",
                "backend": "b",
                "fallback": index > 0,
                "degraded": index > 0,
            })
        # 旧事件只有 degraded、没有 fallback：首个尝试也不能算回退。
        await store.record({
            "kind": "search",
            "attempted": True,
            "status": "success",
            "backend": "legacy",
            "fallback": False,
            "degraded": True,
        })
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["searches"], 4)
        self.assertEqual(data["totals"]["fallback_attempts"], 2)

    async def test_legacy_grouped_degraded_events_recover_accurate_fallback(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        for _ in range(3):
            await store.record({
                "kind": "search",
                "attempted": True,
                "status": "failed",
                "backend": "b",
                "degraded": True,
                "request_id": "old-request",
            })
        # 分组内只有一次尝试且未标记 degraded 的旧请求不产生回退。
        await store.record({
            "kind": "search",
            "attempted": True,
            "status": "success",
            "backend": "b",
            "degraded": False,
            "request_id": "single-old-request",
        })
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["searches"], 4)
        self.assertEqual(data["totals"]["fallback_attempts"], 2)
        self.assertFalse(data["totals"]["fallback_incomplete"])
        self.assertEqual(data["totals"]["fallback_unknown_events"], 0)

    async def test_legacy_single_degraded_group_is_flagged_incomplete(self):
        store = StatsStore(self.path, clock=lambda: 2_000_000.0)
        await store.record({
            "kind": "search",
            "attempted": True,
            "status": "failed",
            "backend": "b",
            "degraded": True,
            "request_id": "truncated-request",
        })
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["fallback_attempts"], 0)
        self.assertTrue(data["totals"]["fallback_incomplete"])

    async def test_legacy_ungrouped_degraded_is_flagged_not_guessed(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        for _ in range(2):
            await store.record({
                "kind": "search",
                "attempted": True,
                "status": "failed",
                "backend": "b",
                "degraded": True,
            })
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["searches"], 2)
        self.assertEqual(data["totals"]["fallback_attempts"], 0)
        self.assertTrue(data["totals"]["fallback_incomplete"])
        self.assertEqual(data["totals"]["fallback_unknown_events"], 2)

    async def test_legacy_ungrouped_success_is_not_flagged_incomplete(self):
        store = StatsStore(self.path, clock=lambda: 2_000_000.0)
        await store.record({
            "kind": "search",
            "attempted": True,
            "status": "success",
            "backend": "b",
            "degraded": False,
        })
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["fallback_attempts"], 0)
        self.assertFalse(data["totals"]["fallback_incomplete"])
        self.assertEqual(data["totals"]["fallback_unknown_events"], 0)

    async def test_new_search_modes_are_not_reported_as_legacy_unknown(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        for index, mode in enumerate(("auto", "astrbot", "api", "web")):
            await store.record({
                "kind": "search",
                "attempted": True,
                "status": "success",
                "backend": "b",
                "search_mode": mode,
                "fallback": index > 0,
                "ts": now - 30 + index,
            })
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["searches"], 4)
        self.assertEqual(
            data["totals"]["searches_by_mode"],
            {"auto": 1, "astrbot": 1, "api": 1, "web": 1},
        )

    async def test_legacy_request_before_range_start_counts_every_in_range_attempt(self):
        # 旧记录的首个尝试在 5h 范围起点之前，后续尝试都在范围内：
        # 此时范围内每一次尝试都是回退，不能套用“尝试数 - 1”。
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        for offset in (6 * 3600, 4 * 3600, 2 * 3600):
            await store.record({
                "kind": "search",
                "attempted": True,
                "status": "failed",
                "backend": "b",
                "degraded": True,
                "request_id": "cross-range-request",
                "ts": now - offset,
            })
        data = await store.snapshot("5h")
        self.assertEqual(data["totals"]["searches"], 2)
        self.assertEqual(data["totals"]["fallback_attempts"], 2)
        self.assertFalse(data["totals"]["fallback_incomplete"])
        self.assertEqual(data["totals"]["fallback_unknown_events"], 0)
        # 全部范围仍然按完整请求还原 2 次回退。
        all_data = await store.snapshot("all")
        self.assertEqual(all_data["totals"]["searches"], 3)
        self.assertEqual(all_data["totals"]["fallback_attempts"], 2)

    async def test_legacy_grouped_attempts_all_before_range_are_not_counted(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        for offset in (8 * 3600, 7 * 3600):
            await store.record({
                "kind": "search",
                "attempted": True,
                "status": "failed",
                "backend": "b",
                "degraded": True,
                "request_id": "old-request",
                "ts": now - offset,
            })
        data = await store.snapshot("5h")
        self.assertEqual(data["totals"]["searches"], 0)
        self.assertEqual(data["totals"]["fallback_attempts"], 0)
        self.assertFalse(data["totals"]["fallback_incomplete"])

    async def test_empty_stats_token_is_unavailable_not_zero(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        data = await store.snapshot("7d")
        self.assertEqual(data["totals"]["tokens"], 0)
        self.assertEqual(data["totals"]["token_status"], "unavailable")
        self.assertEqual(data["retained_events"], 0)
        self.assertEqual(data["matched_events"], 0)
        self.assertEqual(data["totals"]["searches_by_mode"], {})

    async def test_period_boundaries_and_no_fake_history(self):
        now = 2_000_000.0
        store = StatsStore(self.path, clock=lambda: now)
        chat = {"key": "c", "connector": "qq", "type": "group", "label": "c", "umo": "qq:GroupMessage:c"}
        await store.record({"kind": "request", "chat": chat, "ts": now - 6 * 3600})
        await store.record({"kind": "request", "chat": chat, "ts": now - 60})
        data = await store.snapshot("5h")
        self.assertEqual(data["totals"]["requests"], 1)
        all_data = await store.snapshot("all")
        self.assertEqual(all_data["totals"]["requests"], 2)
        self.assertEqual(all_data["range_start"], now - 6 * 3600)


class ConfigCenterTests(unittest.TestCase):
    def test_session_migration_and_exact_match(self):
        plugin = Plugin({
            "whitelist_umos": ["qq:FriendMessage:u"],
            "admin_qqs": ["admin"],
        })
        center = ConfigCenter(plugin)
        self.assertTrue(center.is_session_allowed("qq:FriendMessage:u"))
        self.assertFalse(center.is_session_allowed("qq:FriendMessage:other"))
        self.assertEqual(center.settings["sessions"][0]["type"], "private")
        # 没有连接器作用域的旧管理员不能自动获得所有连接器权限。
        self.assertFalse(center.is_admin("qq", "admin"))
        self.assertFalse(center.is_admin("qq", "qq:FriendMessage:u"))

    def test_legacy_migration_cannot_be_bypassed_by_removed_flag(self):
        plugin = Plugin({
            "whitelist_groups": ["123"],
            "whitelist_umos": [],
        })
        center = ConfigCenter(plugin)
        self.assertTrue(center.migration_required())
        self.assertFalse(center.is_session_allowed("qq:GroupMessage:g"))
        # 旧的“确认不限制”字段已删除，残留值不能让旧限制重新放行。
        plugin.config["umos_unrestricted_confirmed"] = True
        center = ConfigCenter(plugin)
        self.assertTrue(center.migration_required())
        self.assertFalse(center.is_session_allowed("qq:GroupMessage:g"))

    def test_invalid_null_list_fails_closed(self):
        plugin = Plugin({"whitelist_umos": None})
        center = ConfigCenter(plugin)
        self.assertEqual(center.config_error, "invalid_whitelist_umos")
        self.assertFalse(center.is_session_allowed("qq:FriendMessage:u"))

    def test_type_switch_precedes_empty_list(self):
        plugin = Plugin({"session_settings": {
            "allow_private": False, "allow_group": True, "sessions": [],
        }})
        center = ConfigCenter(plugin)
        self.assertFalse(center.is_session_allowed("qq:FriendMessage:u"))
        self.assertTrue(center.is_session_allowed("qq:GroupMessage:g"))

    def test_session_id_colon_rules_match_host_three_part_parse(self):
        # 与 AstrBot core/umo_alias.parse_umo 一致：按 ":" 最多切三段，
        # 第三段是完整 session_id，可再含冒号；只有完整 session_id 为空才残缺。
        self.assertEqual(session_entry_error({"umo": "qq:FriendMessage::b"}), "")
        self.assertEqual(session_entry_error({"umo": "qq:FriendMessage:a:b"}), "")
        self.assertEqual(session_entry_error({"umo": "qq:FriendMessage:u"}), "")
        self.assertEqual(
            session_entry_error({"umo": "qq:FriendMessage:"}), "umo_malformed"
        )
        self.assertEqual(
            session_entry_error({"umo": "qq:FriendMessage"}), "umo_malformed"
        )
        self.assertEqual(parse_umo("qq:FriendMessage::b"), ("qq", "private"))
        self.assertEqual(parse_umo("qq:FriendMessage:a:b"), ("qq", "private"))

    def test_identity_link_is_connector_scoped(self):
        plugin = Plugin({})
        center = ConfigCenter(plugin)
        self.assertEqual(
            center.identity_key("qq", "u1"), "qq:u1"
        )
        link = center.link_identities("qq", ["u1", "u2"])
        self.assertEqual(center.identity_key("qq", "u1"), "link:" + link["id"])
        self.assertEqual(center.identity_key("qq", "u2"), "link:" + link["id"])
        self.assertEqual(center.identity_key("telegram", "u1"), "telegram:u1")
        with self.assertRaises(ValueError):
            center.link_identities("", ["u1", "u2"])


class MemoryKV:
    def __init__(self):
        self.values = {}

    async def get_kv_data(self, key, default=None):
        return self.values.get(key, default)

    async def put_kv_data(self, key, value):
        self.values[key] = value


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_window_and_daily_unlimited(self):
        limiter = RateLimiter(window_minutes=0, max_requests=1, daily_max=0)
        storage = MemoryKV()
        for _ in range(5):
            result = await limiter.check_and_record("qq:u", storage)
            self.assertTrue(result["allowed"])

    async def test_concurrent_requests_do_not_bypass(self):
        limiter = RateLimiter(window_minutes=10, max_requests=1, daily_max=10)
        storage = MemoryKV()
        results = await asyncio.gather(
            *[limiter.check_and_record("qq:u", storage) for _ in range(8)]
        )
        self.assertEqual(sum(1 for item in results if item["allowed"]), 1)

    async def test_merge_adds_distinct_identities(self):
        limiter = RateLimiter(window_minutes=10, max_requests=10, daily_max=20)
        storage = MemoryKV()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:x", storage)
            status = await limiter.get_usage_status("link:x", storage)
        self.assertEqual(status["window_used"], 3)
        self.assertEqual(status["daily_used"], 3)

    async def test_unlink_and_relink_do_not_duplicate_usage(self):
        limiter = RateLimiter(window_minutes=10, max_requests=10, daily_max=20)
        storage = MemoryKV()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:x", storage)
            await limiter.split_usage(
                "link:x", ["qq:a", "qq:b"], storage
            )
            await limiter.merge_usage(["qq:a", "qq:b"], "link:y", storage)
            status = await limiter.get_usage_status("link:y", storage)
        self.assertEqual(status["window_used"], 3)
        self.assertEqual(status["daily_used"], 3)

    async def test_legacy_split_records_upgrade_without_double_count(self):
        limiter = RateLimiter(window_minutes=10, max_requests=10, daily_max=20)
        storage = MemoryKVWithDelete()
        legacy = {
            "window_start": 1000.0,
            "window_count": 2,
            "daily_count": 2,
            "daily_date": "2026-01-01",
            "lineage": "link:old",
        }
        storage.values["rate_limit:qq:a"] = json.dumps(legacy)
        storage.values["rate_limit:qq:b"] = json.dumps(legacy)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_today_str",
            return_value="2026-01-01",
        ):
            await limiter.merge_usage(["qq:a", "qq:b"], "link:new", storage)
            status = await limiter.get_usage_status("link:new", storage)
        self.assertEqual(status["window_used"], 2)
        self.assertEqual(status["daily_used"], 2)

    async def test_relink_counts_usage_added_after_unlink(self):
        limiter = RateLimiter(window_minutes=10, max_requests=4, daily_max=20)
        storage = MemoryKVWithDelete()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:a", storage)
            await limiter.merge_usage(["qq:a"], "link:x", storage)
            await limiter.split_usage("link:x", ["qq:a", "qq:b"], storage)
            # 解除关联后两个身份各自新增一次，不能再被当成同一份历史。
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:y", storage)
            status = await limiter.get_usage_status("link:y", storage)

        self.assertEqual(status["window_used"], 4)
        self.assertEqual(status["daily_used"], 4)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            rejected = await limiter.check_and_record("link:y", storage)
        self.assertFalse(rejected["allowed"])
        self.assertIn("频繁", rejected["message"])

        # 重启只从持久计数重建，不能把合并后的余额翻倍。
        restarted = RateLimiter(
            window_minutes=10, max_requests=4, daily_max=20
        )
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            after_restart = await restarted.get_usage_status("link:y", storage)
        self.assertEqual(after_restart["window_used"], 4)
        self.assertEqual(after_restart["daily_used"], 4)

    async def test_repeated_link_unlink_cycles_add_only_new_requests(self):
        limiter = RateLimiter(window_minutes=10, max_requests=7, daily_max=20)
        storage = MemoryKVWithDelete()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:x", storage)
            for index in range(2):
                await limiter.split_usage(
                    f"link:{'x' if index == 0 else 'y'}",
                    ["qq:a", "qq:b"],
                    storage,
                )
                await limiter.check_and_record("qq:a", storage)
                await limiter.check_and_record("qq:b", storage)
                await limiter.merge_usage(
                    ["qq:a", "qq:b"],
                    f"link:{'y' if index == 0 else 'z'}",
                    storage,
                )
            status = await limiter.get_usage_status("link:z", storage)

        self.assertEqual(status["window_used"], 6)
        self.assertEqual(status["daily_used"], 6)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            self.assertTrue((await limiter.check_and_record("link:z", storage))["allowed"])
            self.assertFalse((await limiter.check_and_record("link:z", storage))["allowed"])

    async def test_cross_link_does_not_recount_absent_identity_history(self):
        limiter = RateLimiter(window_minutes=10, max_requests=4, daily_max=20)
        storage = MemoryKVWithDelete()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:x", storage)
            await limiter.split_usage("link:x", ["qq:a", "qq:b"], storage)
            # a 先与 c 关联，此时 b 不在关联中，但 link:y 已包含 a、b
            # 的共同历史。b 随后新增一次再与 link:y 合并。
            await limiter.merge_usage(["qq:a", "qq:c"], "link:y", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:b", "link:y"], "link:z", storage)
            status = await limiter.get_usage_status("link:z", storage)

        self.assertEqual(status["window_used"], 3)
        self.assertEqual(status["daily_used"], 3)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            self.assertTrue((await limiter.check_and_record("link:z", storage))["allowed"])
            self.assertFalse((await limiter.check_and_record("link:z", storage))["allowed"])

        restarted = RateLimiter(window_minutes=10, max_requests=4, daily_max=20)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            after_restart = await restarted.get_usage_status("link:z", storage)
        self.assertEqual(after_restart["window_used"], 4)

    async def test_window_and_day_boundaries_do_not_double_count_base(self):
        limiter = RateLimiter(window_minutes=10, max_requests=10, daily_max=20)
        storage = MemoryKVWithDelete()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_today_str",
            return_value="2026-01-01",
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:x", storage)
            await limiter.split_usage("link:x", ["qq:a", "qq:b"], storage)

        # a 在共同窗口内新增，b 的窗口已经过期后才新增。
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1300.0,
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_today_str",
            return_value="2026-01-01",
        ):
            await limiter.check_and_record("qq:a", storage)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=2000.0,
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_today_str",
            return_value="2026-01-01",
        ):
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:y", storage)
            status = await limiter.get_usage_status("link:y", storage)

        # 窗口只保留 b 在 2000 的新请求；每日仍保留当天全部 4 次。
        self.assertEqual(status["window_used"], 1)
        self.assertEqual(status["daily_used"], 4)

        # 跨日后共同基线不再计入每日额度，解除后新增的两次只算两次。
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=2000.0,
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_today_str",
            return_value="2026-01-01",
        ):
            await limiter.split_usage("link:y", ["qq:a", "qq:b"], storage)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=2500.0,
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_today_str",
            return_value="2026-01-02",
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            await limiter.merge_usage(["qq:a", "qq:b"], "link:z", storage)
            next_day = await limiter.get_usage_status("link:z", storage)

        self.assertEqual(next_day["window_used"], 3)
        self.assertEqual(next_day["daily_used"], 2)

    async def test_merge_failure_restores_both_identities(self):
        class FailingCanonical(MemoryKVWithDelete):
            async def put_kv_data(self, key, value):
                if key == "rate_limit:link:x":
                    raise RuntimeError("kv write failed")
                await super().put_kv_data(key, value)

        limiter = RateLimiter(window_minutes=10, max_requests=10, daily_max=20)
        storage = FailingCanonical()
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await limiter.check_and_record("qq:a", storage)
            await limiter.check_and_record("qq:b", storage)
            with self.assertRaises(RuntimeError):
                await limiter.merge_usage(["qq:a", "qq:b"], "link:x", storage)
            a = await limiter.get_usage_status("qq:a", storage)
            b = await limiter.get_usage_status("qq:b", storage)
        self.assertEqual(a["window_used"], 1)
        self.assertEqual(b["window_used"], 1)
        self.assertNotIn("rate_limit:link:x", storage.values)


class AnthropicTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_messages_request_response(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "sk-ant")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            payload = json.loads(request.content)
            self.assertEqual(payload["system"], "系统")
            self.assertEqual(payload["messages"][0]["content"], "用户")
            return httpx.Response(200, json={
                "model": "claude-test",
                "content": [{"type": "text", "text": "Claude 正文"}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            })

        client = AIClient(
            base_url="https://api.anthropic.com",
            api_key="sk-ant",
            model="claude-test",
            api_format=API_FORMAT_ANTHROPIC_MESSAGES,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await client._call_ai(
                system_prompt="系统",
                user_prompt="用户",
                temperature=0.3,
                max_tokens=99,
            )
        finally:
            await client.close()
        self.assertEqual(result["content"], "Claude 正文")
        self.assertEqual(result["token_usage"]["prompt_tokens"], 3)
        self.assertEqual(result["token_usage"]["completion_tokens"], 4)
        self.assertTrue(result["token_available"])

    def test_presets_cover_requested_providers(self):
        ids = {item["id"] for item in MANUAL_PRESETS}
        self.assertTrue({"deepseek", "mimo", "openai", "claude", "siliconflow", "custom"} <= ids)
        claude = next(item for item in MANUAL_PRESETS if item["id"] == "claude")
        self.assertEqual(claude["api_format"], "anthropic_messages")
        self.assertEqual(claude["base_url"], "https://api.anthropic.com")


class ConfigCenterAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_state_preserves_zero_rate_values(self):
        class FakeModel:
            def public_state(self):
                return {"source": "manual", "providers": [], "manual": {}}

        class FakeSearch:
            last_result = None

            async def candidate_status(self, event=None):
                return []

        class FakeStats:
            _data = {"started_at": 1.0}

            async def events(self):
                return []

        plugin = Plugin({"rate_limit": {
            "window_minutes": 0,
            "max_requests_per_window": 3,
            "daily_max": 0,
            "reset_timezone": "Asia/Shanghai",
        }})
        plugin.model_service = FakeModel()
        plugin.search_service = FakeSearch()
        plugin.stats_store = FakeStats()
        plugin.last_umo = ""
        plugin.config_center = ConfigCenter(plugin)
        state = await ConfigCenterAPI(plugin).state()
        self.assertEqual(state["rate"]["window_minutes"], 0)
        self.assertEqual(state["rate"]["daily_max"], 0)

    async def test_state_key_usable_requires_matching_origin(self):
        class FakeModel:
            def public_state(self):
                return {"source": "manual", "providers": [], "manual": {}}

        class FakeSearch:
            last_result = None

            async def candidate_status(self, event=None):
                return []

        class FakeStats:
            _data = {"started_at": 1.0}

            async def events(self):
                return []

        cases = (
            ({
                "api_key": "sk", "api_base_url": "https://api.example",
            }, False),
            ({
                "api_key": "sk",
                "api_key_origin": "https://old.example",
                "api_base_url": "https://api.example",
            }, False),
            ({
                "api_key": "sk",
                "api_key_origin": "https://api.example",
                "api_base_url": "https://api.example",
            }, True),
        )
        for search_cfg, expected in cases:
            plugin = Plugin({"search_config": search_cfg})
            plugin.model_service = FakeModel()
            plugin.search_service = FakeSearch()
            plugin.stats_store = FakeStats()
            plugin.last_umo = ""
            plugin.config_center = ConfigCenter(plugin)
            state = await ConfigCenterAPI(plugin).state()
            self.assertEqual(
                state["search"]["api_key_usable"], expected, search_cfg
            )

    async def test_rate_boundaries_allow_zero_and_reject_out_of_range(self):
        plugin = Plugin({"rate_limit": {}})
        api = ConfigCenterAPI(plugin)
        cfg = api._rate_config({
            "window_minutes": 0,
            "max_requests_per_window": 1,
            "daily_max": 0,
            "reset_timezone": "Asia/Shanghai",
        })
        self.assertEqual(cfg["window_minutes"], 0)
        self.assertEqual(cfg["daily_max"], 0)
        with self.assertRaises(ValueError):
            api._rate_config({
                "window_minutes": 1441,
                "max_requests_per_window": 3,
                "daily_max": 20,
            })
        with self.assertRaises(ValueError):
            api._rate_config({
                "window_minutes": 10,
                "max_requests_per_window": 0,
                "daily_max": 20,
            })

    async def test_search_groups_saved_order_is_the_runtime_policy(self):
        plugin = Plugin({"search_config": {}})
        api = ConfigCenterAPI(plugin)
        base = {
            "result_count": 5,
            "timeout_ms": 9000,
            "per_backend_timeout_ms": 4000,
            "summary_input_max_chars": 6000,
            "summarize_max_chars": 180,
        }
        # 全部取消勾选：只保留三组顺序，运行时关闭。
        cfg = api._search_config({
            **base,
            "mode_order": ["web", "api", "astrbot"],
            "mode_enabled": [],
        })
        self.assertEqual(cfg["mode_order"], ["web", "api", "astrbot"])
        self.assertEqual(cfg["mode_enabled"], [])
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(cfg["mode_prefs_version"], 2)
        # 列表顺序与勾选是事实来源：不再按单值 mode 重排。
        cfg = api._search_config({
            **base,
            "mode": "api",
            "mode_order": ["web", "api", "astrbot"],
            "mode_enabled": ["api", "web"],
        })
        self.assertEqual(cfg["mode_order"], ["web", "api", "astrbot"])
        self.assertEqual(cfg["mode_enabled"], ["web", "api"])
        self.assertEqual(cfg["mode"], "web")
        self.assertFalse(cfg["fallback_enabled"])
        # 旧前端/原生页只提交 mode：按三组重新生成一次，不产生 auto/off 行。
        cfg = api._search_config({**base, "mode": "api"})
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["api"])
        cfg = api._search_config({**base, "mode": "auto"})
        self.assertEqual(cfg["mode_enabled"], ["astrbot", "api", "web"])

    def test_model_test_tokens_distinguish_missing_and_zero(self):
        self.assertEqual(
            ConfigCenterAPI._model_test_tokens({}), (None, False)
        )
        self.assertEqual(
            ConfigCenterAPI._model_test_tokens({
                "token_usage": {"total_tokens": 0},
                "token_available": False,
            }),
            (None, False),
        )
        self.assertEqual(
            ConfigCenterAPI._model_test_tokens({
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "token_available": True,
            }),
            ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, True),
        )

    async def test_colon_session_id_saved_exactly_as_host_parses(self):
        plugin = Plugin({})
        api = ConfigCenterAPI(plugin)
        cfg = api._session_config({
            "allow_private": True,
            "allow_group": True,
            "sessions": [
                {"umo": "qq:FriendMessage::b", "enabled": True},
                {"umo": "qq:FriendMessage:a:b", "enabled": True},
            ],
        })
        self.assertEqual(
            [item["umo"] for item in cfg["session_settings"]["sessions"]],
            ["qq:FriendMessage::b", "qq:FriendMessage:a:b"],
        )
        self.assertEqual(
            cfg["whitelist_umos"],
            ["qq:FriendMessage::b", "qq:FriendMessage:a:b"],
        )
        # 完整会话 ID 为空仍被写路径拒绝（与读路径同口径）。
        with self.assertRaisesRegex(ValueError, "完整会话 ID 不能为空"):
            api._session_config({
                "allow_private": True,
                "allow_group": True,
                "sessions": [{"umo": "qq:FriendMessage:"}],
            })

    async def test_search_key_requires_reentry_when_address_changes(self):
        old_config = {
            "api_key": "sk-old",
            "api_key_origin": "https://old.example",
            "api_base_url": "https://old.example",
        }
        plugin = Plugin({"search_config": dict(old_config)})
        api = ConfigCenterAPI(plugin)
        base = {
            "mode": "off",
            "mode_order": ["off", "auto", "astrbot", "api", "web"],
            "mode_enabled": ["off"],
            "result_count": 5,
            "timeout_ms": 9000,
            "per_backend_timeout_ms": 4000,
            "summary_input_max_chars": 6000,
            "summarize_max_chars": 180,
            "api_base_url": "https://new.example",
        }
        # 只改地址、没有输入新 Key，也没有明确清除：拒绝保存，避免旧 Key
        # 以“已配置”姿态留在配置里，跨 endpoint 复用或造成误导。
        with self.assertRaisesRegex(ValueError, "请重新输入 API Key"):
            api._search_config(base)
        self.assertEqual(plugin.config["search_config"], old_config)
        cfg = api._search_config({**base, "api_key": "sk-new"})
        self.assertEqual(cfg["api_key"], "sk-new")
        self.assertEqual(cfg["api_key_origin"], "https://new.example")
        cfg = api._search_config({**base, "clear_api_key": True})
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["api_key_origin"], "")
        # 地址不变时仍可留空沿用已保存 Key，不会强迫每次保存都重输。
        cfg = api._search_config({
            **base,
            "api_base_url": "https://old.example",
        })
        self.assertEqual(cfg["api_key"], "sk-old")
        self.assertEqual(cfg["api_key_origin"], "https://old.example")

    async def test_search_key_kept_for_equivalent_host_but_reentered_for_path_case(self):
        old_config = {
            "api_key": "sk-old",
            "api_key_origin": "https://api.example.com/v1",
            "api_base_url": "https://api.example.com/v1",
        }
        plugin = Plugin({"search_config": dict(old_config)})
        api = ConfigCenterAPI(plugin)
        base = {
            "mode": "off",
            "mode_order": ["off", "auto", "astrbot", "api", "web"],
            "mode_enabled": ["off"],
            "result_count": 5,
            "timeout_ms": 9000,
            "per_backend_timeout_ms": 4000,
            "summary_input_max_chars": 6000,
            "summarize_max_chars": 180,
        }
        # 同一地址（仅主机大小写、末尾斜杠不同）视为同一凭据域，保留已保存 Key。
        same = api._search_config({**base, "api_base_url": "https://API.Example.COM/v1/"})
        self.assertEqual(same["api_key"], "sk-old")
        self.assertEqual(same["api_key_origin"], "https://API.Example.COM/v1/")
        # 路径大小写不同可能是不同凭据域，按不同地址处理，必须重新输入或显式清除。
        with self.assertRaisesRegex(ValueError, "请重新输入 API Key"):
            api._search_config({**base, "api_base_url": "https://api.example.com/V1"})

    async def test_key_without_origin_must_be_reentered_not_rebound(self):
        # 已迁移配置缺少 api_key_origin 时，保存绝不能把当前地址当作
        # 原绑定地址；只能重新输入或明确清除。
        old_config = {
            "api_key": "sk-unbound",
            "api_base_url": "https://new.example",
        }
        plugin = Plugin({"search_config": dict(old_config)})
        api = ConfigCenterAPI(plugin)
        base = {
            "mode": "api",
            "mode_order": ["api", "auto", "off"],
            "mode_enabled": ["api"],
            "result_count": 5,
            "timeout_ms": 9000,
            "per_backend_timeout_ms": 4000,
            "summary_input_max_chars": 6000,
            "summarize_max_chars": 180,
            "api_base_url": "https://new.example",
        }
        with self.assertRaisesRegex(ValueError, "缺少绑定地址"):
            api._search_config(base)
        self.assertEqual(plugin.config["search_config"], old_config)

        cfg = api._search_config({**base, "api_key": "sk-new"})
        self.assertEqual(cfg["api_key"], "sk-new")
        self.assertEqual(cfg["api_key_origin"], "https://new.example")
        cfg = api._search_config({**base, "clear_api_key": True})
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["api_key_origin"], "")

    async def test_failed_persist_restores_runtime_config(self):
        class FailingConfig(Config):
            async def save_config_async(self):
                self.saved += 1
                return False

        config = FailingConfig({"rate_limit": {"window_minutes": 10}})
        plugin = Plugin({})
        plugin.config = config
        api = ConfigCenterAPI(plugin)
        with self.assertRaises(RuntimeError):
            await api._persist({"rate_limit": {"window_minutes": 0}})
        self.assertEqual(config["rate_limit"]["window_minutes"], 10)


try:
    import astrbot.api.web  # noqa: F401
    ASTRBOT_WEB_AVAILABLE = True
except Exception:
    ASTRBOT_WEB_AVAILABLE = False


class SavingPlugin(Plugin):
    def __init__(self, config, storage):
        super().__init__(config.data)
        self.config = config
        self.storage = storage
        self.rate_limiter = RateLimiter(
            window_minutes=10, max_requests=10, daily_max=20
        )
        self.config_center = ConfigCenter(self)
        self.config_center_api = ConfigCenterAPI(self)
        self.identity_rate_lock = asyncio.Lock()
        self.last_umo = ""
        self.reload_rate_limiter = lambda: None
        self.model_service = SimpleNamespace(
            public_state=lambda: {"source": "manual", "providers": [], "manual": {}}
        )
        self.search_service = SimpleNamespace(
            candidate_status=self._candidates, last_result=None
        )
        self.stats_store = SimpleNamespace(
            events=self._events, _data={"started_at": 1.0}
        )

    async def _candidates(self, event=None):
        return []

    async def _events(self):
        return []

    async def get_kv_data(self, key, default=None):
        return await self.storage.get_kv_data(key, default)

    async def put_kv_data(self, key, value):
        await self.storage.put_kv_data(key, value)

    async def delete_kv_data(self, key):
        self.storage.values.pop(key, None)


class MemoryKVWithDelete(MemoryKV):
    async def delete_kv_data(self, key):
        self.values.pop(key, None)


class FailingKV(MemoryKVWithDelete):
    def __init__(self, fail_key):
        super().__init__()
        self.fail_key = fail_key

    async def put_kv_data(self, key, value):
        if key == self.fail_key:
            raise RuntimeError("kv write failed")
        await super().put_kv_data(key, value)


class FailingSaveConfig(Config):
    async def save_config_async(self):
        self.saved += 1
        return False


@unittest.skipUnless(ASTRBOT_WEB_AVAILABLE, "需要真实 AstrBot Web API")
class ConfigCenterAPISaveRollbackTests(unittest.IsolatedAsyncioTestCase):
    def _config(self):
        return Config({
            "rate_limit": {
                "window_minutes": 10,
                "max_requests_per_window": 10,
                "daily_max": 20,
            },
            "session_settings": {
                "allow_private": True, "allow_group": True, "sessions": [],
            },
            "identity_links": [],
            "admin_users": [],
        })

    async def _seed_usage(self, plugin):
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await plugin.rate_limiter.check_and_record("qq:u1", plugin)
            await plugin.rate_limiter.check_and_record("qq:u1", plugin)
            await plugin.rate_limiter.check_and_record("qq:u2", plugin)

    def _payload(self):
        return {
            "section": "identity",
            "data": {
                "action": "link",
                "connector": "qq",
                "uids": ["u1", "u2"],
            },
        }

    async def test_identity_save_merges_usage_before_restart(self):
        config = self._config()
        storage = MemoryKVWithDelete()
        plugin = SavingPlugin(config, storage)
        await self._seed_usage(plugin)

        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._payload()),
        ), patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            result = await plugin.config_center_api.save()

        self.assertTrue(result.get("saved"), result)
        self.assertEqual(len(config["identity_links"]), 1)
        link_id = config["identity_links"][0]["id"]
        canonical = f"rate_limit:link:{link_id}"
        self.assertNotIn("rate_limit:qq:u1", storage.values)
        self.assertNotIn("rate_limit:qq:u2", storage.values)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            status = await plugin.rate_limiter.get_usage_status(
                f"link:{link_id}", plugin
            )
        self.assertEqual(status["window_used"], 3)
        self.assertEqual(status["daily_used"], 3)

        restarted = ConfigCenter(
            SimpleNamespace(config=config)
        )
        self.assertEqual(restarted.identity_key("qq", "u1"), f"link:{link_id}")
        self.assertIn(canonical, storage.values)

    async def test_config_save_failure_rolls_back_link_and_counts(self):
        config = FailingSaveConfig(self._config().data)
        storage = MemoryKVWithDelete()
        plugin = SavingPlugin(config, storage)
        await self._seed_usage(plugin)

        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._payload()),
        ):
            result = await plugin.config_center_api.save()

        self.assertEqual(result["status"], "error")
        self.assertEqual(config["identity_links"], [])
        self.assertEqual(plugin.config_center.identity_links, [])
        self.assertIsNotNone(storage.values.get("rate_limit:qq:u1"))
        self.assertIsNotNone(storage.values.get("rate_limit:qq:u2"))
        self.assertEqual(
            [key for key in storage.values if key.startswith("rate_limit:link:")],
            [],
        )

    async def test_usage_merge_failure_rolls_back_all_side_effects(self):
        config = self._config()
        # 关联后的 canonical 键首次写入失败。
        link_storage = FailingKV("rate_limit:link:placeholder")
        storage = MemoryKVWithDelete()
        plugin = SavingPlugin(config, storage)
        await self._seed_usage(plugin)
        original_put = link_storage.put_kv_data
        created = []

        async def failing_put(key, value):
            if key not in storage.values:
                created.append(key)
                raise RuntimeError("kv write failed")
            await original_put(key, value)

        link_storage.put_kv_data = failing_put
        plugin.kv = link_storage
        # 让插件用会失败的存储执行计数迁移。
        plugin.get_kv_data = link_storage.get_kv_data
        plugin.put_kv_data = failing_put
        plugin.delete_kv_data = link_storage.delete_kv_data
        for key, value in storage.values.items():
            link_storage.values[key] = value

        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._payload()),
        ):
            result = await plugin.config_center_api.save()

        self.assertEqual(result["status"], "error")
        self.assertEqual(config["identity_links"], [])
        self.assertEqual(plugin.config_center.identity_links, [])
        self.assertIsNotNone(link_storage.values.get("rate_limit:qq:u1"))
        self.assertIsNotNone(link_storage.values.get("rate_limit:qq:u2"))
        self.assertFalse(
            any(key.startswith("rate_limit:link:") for key in link_storage.values)
        )

    def _rate_payload(self, window, base_revision=None):
        payload = {
            "section": "rate",
            "data": {
                "window_minutes": window,
                "max_requests_per_window": 5,
                "daily_max": 30,
                "reset_timezone": "",
            },
        }
        if base_revision is not None:
            payload["base_revision"] = base_revision
        return payload

    async def test_stale_base_revision_save_is_rejected(self):
        config = self._config()
        plugin = SavingPlugin(config, MemoryKVWithDelete())

        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._rate_payload(15, base_revision=0)),
        ):
            first = await plugin.config_center_api.save()
        self.assertTrue(first.get("saved"), first)
        self.assertEqual(config["rate_limit"]["window_minutes"], 15)
        revision = first.get("revision")
        self.assertEqual(revision, 1)

        # 第二个页面仍基于旧快照 revision 0 保存：应被拒绝且不覆盖。
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._rate_payload(20, base_revision=0)),
        ):
            stale = await plugin.config_center_api.save()
        self.assertFalse(stale.get("saved"), stale)
        self.assertTrue(stale.get("conflict"), stale)
        self.assertEqual(config["rate_limit"]["window_minutes"], 15)

        # 携带最新 revision 的保存正常生效。
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._rate_payload(20, base_revision=revision)),
        ):
            fresh = await plugin.config_center_api.save()
        self.assertTrue(fresh.get("saved"), fresh)
        self.assertEqual(config["rate_limit"]["window_minutes"], 20)
        self.assertEqual(fresh.get("revision"), revision + 1)

    async def test_save_without_base_revision_still_applies(self):
        config = self._config()
        plugin = SavingPlugin(config, MemoryKVWithDelete())
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=self._rate_payload(25)),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(config["rate_limit"]["window_minutes"], 25)


if __name__ == "__main__":
    unittest.main()
