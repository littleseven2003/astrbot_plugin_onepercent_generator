# -*- coding: utf-8 -*-
"""真实 AstrBotConfig 旧配置迁移回归。

需要隔离 AstrBot 4.29.4；未安装时 skip。只操作临时配置文件，不联网。
"""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

try:
    import astrbot.api.web  # noqa: F401
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot_plugin_onepercent_generator.config_center_api import ConfigCenterAPI
    from astrbot_plugin_onepercent_generator.main import OnePercentGenerator
    from astrbot_plugin_onepercent_generator.migration import MIGRATION_VERSION
    from astrbot_plugin_onepercent_generator.search_service import (
        BackendResult,
        SearchBackend,
    )
    ASTRBOT_AVAILABLE = True
except Exception:
    ASTRBOT_AVAILABLE = False


class Ctx:
    def register_web_api(self, *args, **kwargs):
        return None


class _RecordingBackend(SearchBackend):
    """离线替身后端：只记录调用，不发请求。"""

    def __init__(self, name, status="success", results=None, reason=""):
        self.name = name
        self.status = status
        self.results = list(results or [])
        self.reason = reason
        self.calls = []

    async def availability(self, event=None):
        return True, ""

    async def search(self, query, max_results, event=None, timeout_s=None):
        self.calls.append(query)
        return BackendResult(
            self.name, self.status, self.results[:max_results], self.reason, 0
        )


OLD_UMO = "qq_official_bot:FriendMessage:openid-1"
OLD_GROUP = "qq_official_bot:GroupMessage:group-1"


class _FakeSearchHandler(BaseHTTPRequestHandler):
    """本地搜索替身：只记录请求，不访问真实上游。"""

    def log_message(self, *args, **kwargs):
        return None

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.captured.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "body": body,
        })
        payload = json.dumps({
            "results": [{
                "title": "替身结果",
                "url": "https://example.com/result",
                "content": "本地替身搜索摘要",
            }],
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _old_config(umo=OLD_UMO, groups=None, privates=None, admin="admin-old"):
    return {
        "ai_config": {
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "test-key",
            "model": "mock-model",
        },
        "search_config": {"enabled": False, "mode": "off"},
        "rate_limit": {
            "window_minutes": 10,
            "max_requests_per_window": 3,
            "daily_max": 20,
        },
        "admin_qqs": [admin] if admin else [],
        "whitelist_mode": "whitelist",
        "whitelist_umos": [umo] if umo else [],
        "whitelist_groups": groups or [],
        "whitelist_privates": privates or [],
        "preset_games": ["原神"],
    }


@unittest.skipUnless(ASTRBOT_AVAILABLE, "需要隔离 AstrBot 环境")
class RealConfigMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "plugin_config.json"
        self.schema = json.loads(
            (PROJECT_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data):
        self.path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _load(self):
        return AstrBotConfig(config_path=str(self.path), schema=self.schema)

    def test_defaults_and_legacy_fields_survive_then_umo_is_exact(self):
        self._write(_old_config(groups=[OLD_GROUP], privates=["openid-2"]))
        raw = self._load()
        # schema 的 sessions 默认值是 null（不是 []）：真实 AstrBotConfig
        # 不再把“未配置”伪造成有意空列表，避免普通会话被误放行。
        self.assertIsNone(raw["session_settings"]["sessions"])
        self.assertEqual(raw["admin_users"], [])
        self.assertEqual(raw["whitelist_umos"], [OLD_UMO])
        self.assertEqual(raw["whitelist_groups"], [OLD_GROUP])
        self.assertEqual(raw["config_migration_version"], 0)

        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertEqual(len(raw["session_settings"]["sessions"]), 1)
        self.assertEqual(
            raw["session_settings"]["sessions"][0]["umo"], OLD_UMO
        )
        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertEqual(raw["legacy_admin_pending"][0]["uid"], "admin-old")
        self.assertFalse(plugin.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:openid-other"
        ))
        # 旧群限制不可安全映射，不能被当成已授权。
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))

        # 保存后重启：标记生效，不重复追加旧 UMO。
        plugin.config.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(len(raw2["session_settings"]["sessions"]), 1)
        self.assertEqual(raw2["config_migration_version"], MIGRATION_VERSION)
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))

    async def test_clear_legacy_admin_pending_keeps_active_admins(self):
        data = _old_config(admin=None)
        data["config_migration_version"] = MIGRATION_VERSION
        data["admin_users"] = [
            {"connector": "qq_official_bot", "uid": "active-admin"}
        ]
        data["legacy_admin_pending"] = [
            {"uid": "old-admin", "connector": ""}
        ]
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(raw["legacy_admin_pending"][0]["uid"], "old-admin")

        api = ConfigCenterAPI(plugin)
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value={
                "section": "admins",
                "data": {
                    "admin_users": [
                        {"connector": "qq_official_bot", "uid": "active-admin"}
                    ],
                    "clear_legacy_admin_pending": True,
                },
            }),
        ):
            result = await api.save()
        self.assertTrue(result.get("saved"), result)

        raw2 = self._load()
        self.assertEqual(raw2["legacy_admin_pending"], [])
        self.assertEqual(len(raw2["admin_users"]), 1)
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["legacy_admin_pending"], [])
        self.assertTrue(plugin2.config_center.is_admin(
            "qq_official_bot", "active-admin"
        ))
        self.assertFalse(plugin2.config_center.is_admin("qq_official_bot", "old-admin"))

    async def test_active_search_test_records_real_attempts(self):
        self._write(_old_config())
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        plugin.last_umo = "webchat:FriendMessage:user-1"
        result = {
            "success": True,
            "attempted": [
                {"backend": "astrbot", "status": "failed"},
                {"backend": "api", "status": "success", "result_count": 2},
            ],
            "degraded": True,
            "effective_backend": "api",
        }
        plugin.search_service = SimpleNamespace(
            test_search=AsyncMock(return_value=result)
        )
        plugin._record_search = AsyncMock()

        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value={"game": "原神"}),
        ):
            response = await ConfigCenterAPI(plugin).search_test()

        self.assertEqual(response, result)
        plugin._record_search.assert_awaited_once()
        args, kwargs = plugin._record_search.call_args
        self.assertEqual(args[0], result)
        # 网页主动测试没有聊天归属，不能借用 last_umo。
        self.assertEqual(args[1], "")
        self.assertTrue(str(kwargs["request_id"]).startswith("search-test-"))

    def test_legacy_search_key_origin_is_bound_once_and_respected_at_runtime(self):
        data = _old_config()
        data["search_config"] = {
            "enabled": True,
            "mode": "api",
            "api_key": "sk-legacy",
            "api_base_url": "https://old.example",
            "mode_order": ["api", "auto", "astrbot", "web", "off"],
            "mode_enabled": ["api"],
        }
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(
            raw["search_config"]["api_key_origin"], "https://old.example"
        )
        service = plugin._build_search_service()
        self.assertEqual(service.api_key, "sk-legacy")
        self.assertEqual(service.api_base_url, "https://old.example")

        raw["search_config"]["api_base_url"] = "https://new.example"
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        # 只改地址不能改写已绑定地址，也不能把旧 Key 交给新地址。
        self.assertEqual(
            raw2["search_config"]["api_key_origin"], "https://old.example"
        )
        service2 = plugin2._build_search_service()
        self.assertEqual(service2.api_key, "")
        self.assertEqual(service2.api_base_url, "https://new.example")

        raw2["search_config"]["api_key"] = "sk-new"
        raw2["search_config"]["api_key_origin"] = "https://new.example"
        raw2.save_config()
        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        service3 = plugin3._build_search_service()
        self.assertEqual(service3.api_key, "sk-new")
        self.assertEqual(service3.api_base_url, "https://new.example")

    def test_group_only_old_restriction_stays_closed_and_survives_reload(self):
        self._write(_old_config(
            umo="", groups=[OLD_GROUP], privates=["openid-2"]
        ))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertEqual(raw["whitelist_groups"], [OLD_GROUP])
        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.migration_required())
        self.assertFalse(plugin2.config_center.is_session_allowed(OLD_GROUP))

    def test_removed_unrestricted_flag_cannot_complete_migration(self):
        self._write(_old_config(umo="", groups=[OLD_GROUP]))
        raw = self._load()
        # 旧字段已从 schema 删除；即使残留也不能绕过旧限制。
        raw["umos_unrestricted_confirmed"] = True
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))
        self.assertTrue(raw["legacy_restrictions_pending"])

    def test_v2_confirmed_empty_sessions_are_reopened(self):
        data = _old_config(umo="", groups=[OLD_GROUP])
        data.update({
            "config_migration_version": 2,
            "legacy_restrictions_pending": False,
            "umos_unrestricted_confirmed": True,
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.migration_required())
        self.assertFalse(plugin2.config_center.is_session_allowed(OLD_GROUP))

    def test_v2_existing_explicit_sessions_are_not_reopened(self):
        new_umo = "webchat:FriendMessage:new-user"
        data = _old_config(umo="", groups=[OLD_GROUP])
        data.update({
            "config_migration_version": 2,
            "legacy_restrictions_pending": False,
            "umos_unrestricted_confirmed": True,
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{
                    "umo": new_umo,
                    "connector": "webchat",
                    "type": "private",
                    "enabled": True,
                }],
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertFalse(plugin.config_center.migration_required())
        self.assertTrue(plugin.config_center.is_session_allowed(new_umo))
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))
        self.assertFalse(raw["legacy_restrictions_pending"])
        self.assertFalse(raw["umos_unrestricted_confirmed"])
        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)

    async def test_v3_pending_survives_v4_and_is_cleared_by_explicit_session(self):
        # 真实 v3 状态：旧版确认“不限制”后旧白名单和确认标记已被清空，
        # 只剩空会话与 legacy_restrictions_pending=True。v4 只应补记搜索
        # Key 绑定，不能重跑会话迁移把这个待处理状态改成 False。
        data = _old_config(umo="", groups=[], privates=[], admin=None)
        data["search_config"] = {
            "enabled": True,
            "mode": "api",
            "api_key": "sk-legacy",
            "api_base_url": "https://old.example",
        }
        data.update({
            "config_migration_version": 3,
            "legacy_restrictions_pending": True,
            "umos_unrestricted_confirmed": False,
            "legacy_umo_pending": [],
            "whitelist_umos": [],
            "whitelist_groups": [],
            "whitelist_privates": [],
            "admin_qqs": ["oldqq"],
            "admin_users": [{
                "connector": "webchat", "uid": "active-admin", "legacy": False,
            }],
            "legacy_admin_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(
            "webchat:FriendMessage:any-user"
        ))
        self.assertEqual(
            raw["search_config"]["api_key_origin"], "https://old.example"
        )
        # 已处理过的旧管理员不因升级重新进入 pending，现有管理员保留。
        self.assertTrue(plugin.config_center.is_admin(
            "webchat", "active-admin"
        ))
        self.assertFalse(plugin.config_center.is_admin("webchat", "oldqq"))
        self.assertEqual(raw["legacy_admin_pending"], [])

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.migration_required())
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "webchat:FriendMessage:any-user"
        ))
        self.assertTrue(raw2["legacy_restrictions_pending"])

        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{
                    "umo": "webchat:FriendMessage:new-user",
                    "enabled": True,
                }],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertFalse(raw2["legacy_restrictions_pending"])
        self.assertFalse(plugin2.config_center.migration_required())
        self.assertTrue(plugin2.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))

        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        self.assertFalse(plugin3.config_center.migration_required())
        self.assertTrue(plugin3.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))
        self.assertEqual(
            [item["umo"] for item in raw3["session_settings"]["sessions"]],
            ["webchat:FriendMessage:new-user"],
        )

    def test_v3_blank_config_stays_closed_after_v4(self):
        # v3 的空白配置：没有任何明确会话、旧白名单和旧管理员，但仍处于
        # legacy_restrictions_pending。升级只补版本和 Key 绑定，不得把
        # pending 改成 False，也不得让任意会话通过。
        data = _old_config(umo="", admin=None)
        data.update({
            "config_migration_version": 3,
            "legacy_restrictions_pending": True,
            "umos_unrestricted_confirmed": False,
            "legacy_umo_pending": [],
            "whitelist_umos": [],
            "whitelist_groups": [],
            "whitelist_privates": [],
            "admin_qqs": [],
            "admin_users": [],
            "legacy_admin_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertEqual(raw["session_settings"]["sessions"], [])
        self.assertEqual(raw["legacy_admin_pending"], [])
        self.assertFalse(plugin.config_center.is_session_allowed(
            "webchat:FriendMessage:any-user"
        ))
        self.assertFalse(plugin.config_center.is_session_allowed(
            OLD_UMO
        ))

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["config_migration_version"], MIGRATION_VERSION)
        self.assertTrue(raw2["legacy_restrictions_pending"])
        self.assertTrue(plugin2.config_center.migration_required())
        self.assertEqual(raw2["session_settings"]["sessions"], [])
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "webchat:FriendMessage:any-user"
        ))

    def test_v3_existing_sessions_and_admins_are_not_migrated_again(self):
        existing = "webchat:FriendMessage:existing"
        data = _old_config(umo="", groups=[], privates=[], admin=None)
        data.update({
            "config_migration_version": 3,
            "legacy_restrictions_pending": False,
            "umos_unrestricted_confirmed": False,
            "legacy_umo_pending": [],
            "whitelist_umos": [],
            "whitelist_groups": [],
            "whitelist_privates": [],
            "admin_qqs": ["oldqq"],
            "admin_users": [{
                "connector": "webchat", "uid": "active-admin", "legacy": False,
            }],
            "legacy_admin_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{
                    "umo": existing,
                    "connector": "webchat",
                    "type": "private",
                    "enabled": True,
                }],
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertEqual(raw["config_migration_version"], MIGRATION_VERSION)
        self.assertFalse(raw["legacy_restrictions_pending"])
        self.assertEqual(
            [item["umo"] for item in raw["session_settings"]["sessions"]],
            [existing],
        )
        self.assertTrue(plugin.config_center.is_session_allowed(existing))
        self.assertTrue(plugin.config_center.is_admin(
            "webchat", "active-admin"
        ))
        self.assertEqual(raw["legacy_admin_pending"], [])

    def test_v2_confirmed_unrestricted_with_cleared_fields_is_reopened(self):
        # bd242ee 的真实保存结果：确认“不限制”后旧白名单、待处理列表全部被
        # 清空，只剩 umos_unrestricted_confirmed=True 和空 sessions。
        data = _old_config(umo="", groups=[], privates=[], admin=None)
        data.update({
            "config_migration_version": 2,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "umos_unrestricted_confirmed": True,
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
            "whitelist_umos": [],
            "whitelist_groups": [],
            "whitelist_privates": [],
            "whitelist_mode": "",
        })
        self._write(data)
        raw = self._load()
        self.assertTrue(raw["umos_unrestricted_confirmed"])
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertTrue(plugin.config_center.migration_required())
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertFalse(raw["umos_unrestricted_confirmed"])
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:anyone"
        ))

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.migration_required())
        self.assertTrue(raw2["legacy_restrictions_pending"])
        self.assertFalse(raw2["umos_unrestricted_confirmed"])

    def test_v2_handled_old_admin_is_not_readded(self):
        data = _old_config(umo=OLD_UMO, groups=[], admin=None)
        data.update({
            "config_migration_version": 2,
            "admin_qqs": ["admin-old"],
            "admin_users": [],
            "legacy_admin_pending": [],
            "legacy_restrictions_pending": False,
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(raw["legacy_admin_pending"], [])
        self.assertEqual(plugin.config_center.legacy_admin_pending, [])
        self.assertFalse(plugin.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))

    async def test_legacy_single_mode_converts_to_groups_and_restarts(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {"enabled": True, "mode": "api"},
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["api"])
        self.assertEqual(cfg["mode"], "api")
        self.assertEqual(cfg["mode_prefs_version"], 2)
        self.assertFalse(cfg["fallback_enabled"])
        self.assertEqual(plugin.search_service.groups, ["api"])

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        cfg2 = raw2["search_config"]
        self.assertEqual(cfg2["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg2["mode_enabled"], ["api"])
        self.assertEqual(cfg2["mode"], "api")
        self.assertEqual(plugin2.search_service.groups, ["api"])
        state = await plugin2.config_center_api.state()
        self.assertEqual(state["search"]["mode"], "api")
        self.assertEqual(state["search"]["mode_enabled"], ["api"])

    async def test_legacy_mode_with_fallback_converts_to_following_groups(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True, "mode": "api", "fallback_enabled": True,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["api", "web"])
        self.assertEqual(plugin.search_service.groups, ["api", "web"])
        self.assertFalse(cfg["fallback_enabled"])
    async def test_legacy_off_migrates_to_empty_group_selection(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {"enabled": True, "mode": "off"},
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(cfg["mode_enabled"], [])
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(plugin.search_service.groups, [])
        state = await plugin.config_center_api.state()
        self.assertEqual(state["search"]["mode"], "off")
        self.assertEqual(state["search"]["mode_enabled"], [])
        self.assertNotIn("fallback_enabled", state["search"])

    async def test_missing_enabled_follows_mode_but_keeps_custom_order(self):
        # 审查回归：保留自定义顺序、只缺 mode_enabled 时，旧 off/仅 API
        # 不能被宿主补成全开。
        for mode, expected in (("off", []), ("api", ["api"])):
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_order": ["web", "api", "astrbot"],
                    "mode_prefs_version": 1,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            cfg = raw["search_config"]
            self.assertEqual(cfg["mode_order"], ["web", "api", "astrbot"], mode)
            self.assertEqual(cfg["mode_enabled"], expected, mode)
            self.assertEqual(plugin.search_service.groups, expected, mode)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(raw2["search_config"]["mode_enabled"], expected, mode)
            self.assertEqual(plugin2.search_service.groups, expected, mode)

    async def test_missing_order_uses_enabled_selection(self):
        # 审查回归：只缺 mode_order、保留 mode_enabled=["auto"] 时旧版会
        # 搜索，新版也必须启用三组；显式空选/具体组同样保留。
        for mode, enabled, expected in (
            ("auto", ["auto"], ["astrbot", "api", "web"]),
            ("web", [], []),
            ("web", ["web"], ["web"]),
            ("off", ["auto"], []),
        ):
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_enabled": enabled,
                    "mode_prefs_version": 1,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            self.assertEqual(
                raw["search_config"]["mode_enabled"], expected, (mode, enabled)
            )
            self.assertEqual(plugin.search_service.groups, expected, (mode, enabled))
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(
                raw2["search_config"]["mode_enabled"], expected, (mode, enabled)
            )
            self.assertEqual(plugin2.search_service.groups, expected, enabled)

    async def test_v0_schema_default_pair_follows_single_mode(self):
        # 审查回归：旧 schema 默认对（5 项顺序 + ["auto"]）被宿主补默认后
        # 落盘时，不是用户改动；旧版只看 mode，升级也必须按 mode 收窄。
        cases = [
            ("off", []),
            ("api", ["api"]),
            ("web", ["web"]),
            ("auto", ["astrbot", "api", "web"]),
        ]
        for mode, expected in cases:
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_order": ["auto", "astrbot", "api", "web", "off"],
                    "mode_enabled": ["auto"],
                    "mode_prefs_version": 0,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            cfg = raw["search_config"]
            self.assertEqual(cfg["mode_enabled"], expected, mode)
            self.assertEqual(plugin.search_service.groups, expected, mode)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(raw2["search_config"]["mode_enabled"], expected, mode)
            self.assertEqual(plugin2.search_service.groups, expected, mode)

    async def test_v0_changed_list_auto_and_completed_legacy_order(self):
        # 审查回归：v0 列表是用户显式改动，旧版迁移会按列表回写 mode。
        # - ["web","api","auto","astrbot","off"] + ["auto"]：auto 生效，
        #   应依次尝试三组，而不是被 mode=off 判成零调用；
        # - ["web"] + ["api"]：先补齐旧五项顺序再判定首个启用项，保留 API。
        variants = [
            (["web", "api", "auto", "astrbot", "off"], ["auto"],
             ["astrbot", "api", "web"], ["astrbot", "api", "web"]),
            (["web"], ["api"],
             ["web", "astrbot", "api"], ["api"]),
        ]
        for order, enabled, expect_order, expect_enabled in variants:
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": "off",
                    "mode_order": order,
                    "mode_enabled": enabled,
                    "mode_prefs_version": 0,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            cfg = raw["search_config"]
            key = (order, enabled)
            self.assertEqual(cfg["mode_order"], expect_order, key)
            self.assertEqual(cfg["mode_enabled"], expect_enabled, key)
            self.assertEqual(plugin.search_service.groups, expect_enabled, key)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(raw2["search_config"]["mode_order"], expect_order, key)
            self.assertEqual(raw2["search_config"]["mode_enabled"], expect_enabled, key)
            self.assertEqual(plugin2.search_service.groups, expect_enabled, key)

    async def test_explicit_group_checks_survive_stale_mode_and_missing_order(self):
        # 审查回归：v0 显式只勾网页 + 残留 mode=auto、v1/v2 缺顺序但显式
        # 勾选具体组时，列表必须优先，不能被 mode 收窄或扩成全开。
        variants = [
            # (version, mode, order, enabled, expected_order, expected_enabled)
            (0, "auto", ["auto", "web", "astrbot", "api", "off"],
             ["web"], ["web", "astrbot", "api"], ["web"]),
            (1, "off", None, ["web"], ["astrbot", "api", "web"], ["web"]),
            (2, "api", None, ["api", "web"],
             ["astrbot", "api", "web"], ["api", "web"]),
        ]
        for version, mode, order, enabled, expect_order, expect_enabled in variants:
            data = _old_config(umo="", groups=[], admin=None)
            search = {
                "enabled": True,
                "mode": mode,
                "mode_enabled": enabled,
                "mode_prefs_version": version,
            }
            if order is not None:
                search["mode_order"] = order
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": search,
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            cfg = raw["search_config"]
            key = (version, mode, order, enabled)
            self.assertEqual(cfg["mode_order"], expect_order, key)
            self.assertEqual(cfg["mode_enabled"], expect_enabled, key)
            self.assertEqual(plugin.search_service.groups, expect_enabled, key)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(raw2["search_config"]["mode_order"], expect_order, key)
            self.assertEqual(raw2["search_config"]["mode_enabled"], expect_enabled, key)
            self.assertEqual(plugin2.search_service.groups, expect_enabled, key)

    async def test_single_mode_wins_over_residual_auto_list(self):
        # 审查回归：旧 v1 单值 mode 与残留 mode_enabled=["auto"] 冲突时，
        # 旧版实际运行的是 mode；顺序缺失/损坏/自定义都不能把它扩成全开。
        variants = [
            ("off", None, ["astrbot", "api", "web"], []),
            ("off", 123, ["astrbot", "api", "web"], []),
            ("off", ["web", "api", "astrbot"], ["web", "api", "astrbot"], []),
            ("api", None, ["astrbot", "api", "web"], ["api"]),
            ("api", 123, ["astrbot", "api", "web"], ["api"]),
            ("api", ["web", "api", "astrbot"], ["web", "api", "astrbot"], ["api"]),
        ]
        for mode, order, expect_order, expect_enabled in variants:
            data = _old_config(umo="", groups=[], admin=None)
            search = {
                "enabled": True,
                "mode": mode,
                "mode_enabled": ["auto"],
                "mode_prefs_version": 1,
            }
            if order is not None:
                search["mode_order"] = order
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": search,
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            cfg = raw["search_config"]
            key = (mode, order)
            self.assertEqual(cfg["mode_order"], expect_order, key)
            self.assertEqual(cfg["mode_enabled"], expect_enabled, key)
            self.assertEqual(plugin.search_service.groups, expect_enabled, key)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(raw2["search_config"]["mode_order"], expect_order, key)
            self.assertEqual(raw2["search_config"]["mode_enabled"], expect_enabled, key)
            self.assertEqual(plugin2.search_service.groups, expect_enabled, key)

    async def test_saved_v1_config_follows_mode_and_fallback(self):
        # 审查回归：d8f9da9 正常保存入口落盘的 v1 完整配置（五项默认顺序 +
        # 勾选单个组 + 降级开关）升级后必须与旧运行时的实际调用一致：
        # - 仅 API＋允许降级：API 无结果后继续尝试网页；
        # - 仅 AstrBot＋允许降级：三组依次尝试；
        # - 保存后只改可见 mode（api->off、astrbot->api）：以 mode 为准。
        order = ["auto", "astrbot", "api", "web", "off"]
        cases = [
            ("api", ["api"], True, ["api", "web"], ["api", "web"]),
            ("astrbot", ["astrbot"], True,
             ["astrbot", "api", "web"],
             ["astrbot", "anysearch", "api", "web"]),
            ("off", ["api"], True, [], []),
            ("api", ["astrbot"], False, ["api"], ["api"]),
        ]
        for mode, enabled, fallback, expect_groups, expect_calls in cases:
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_order": order,
                    "mode_enabled": enabled,
                    "fallback_enabled": fallback,
                    "mode_prefs_version": 1,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            key = (mode, enabled, fallback)
            self.assertEqual(plugin.search_service.groups, expect_groups, key)
            self.assertEqual(
                plugin.search_service._candidate_order(), expect_calls, key
            )
            self.assertEqual(raw["search_config"]["mode"], mode, key)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(
                raw2["search_config"]["mode_enabled"], expect_groups, key
            )
            self.assertEqual(plugin2.search_service.groups, expect_groups, key)
            self.assertEqual(
                plugin2.search_service._candidate_order(), expect_calls, key
            )

    async def test_saved_v1_fallback_attempts_expected_backends(self):
        # 审查回归：用替身后端记录实际调用序列。仅 API＋允许降级的旧配置
        # 在 API 失败后必须继续尝试网页；保存后只把可见 mode 改成 off 时
        # 必须 0 次调用。
        order = ["auto", "astrbot", "api", "web", "off"]
        for mode, expect_calls, expect_status in (
            ("api", ["api", "web"], "success"),
            ("off", [], "disabled"),
        ):
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_order": order,
                    "mode_enabled": ["api"],
                    "fallback_enabled": True,
                    "mode_prefs_version": 1,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            api = _RecordingBackend("api", status="failed", reason="http_500")
            web = _RecordingBackend(
                "web", results=[{
                    "title": "T", "url": "https://example.com/a",
                    "snippet": "S", "source": "bing",
                }],
            )
            plugin.search_service._backends = {"api": api, "web": web}
            result = await plugin.search_service.search_game_info("原神")
            self.assertEqual(result["status"], expect_status, mode)
            self.assertEqual(
                [a["backend"] for a in result["attempted"]], expect_calls, mode
            )
            self.assertEqual(len(api.calls), 1 if expect_calls else 0, mode)
            self.assertEqual(len(web.calls), 1 if expect_calls else 0, mode)
            await plugin.search_service.close()

    async def test_saved_v1_auto_checked_web_and_close_then_mode(self):
        # 审查回归：旧配置中心实际保存的三类 v1 配置，用替身后端记录真实
        # 调用序列，覆盖升级、保存普通参数与两次重载。
        # - 「自动」生效同时勾选网页：旧版按 AstrBot、Anysearch、API、网页；
        # - 仅勾 API＋允许降级、未勾选的网页排在 API 前：旧版先 API 再网页，
        #   迁移后的列表也要保持这个执行顺序；
        # - 旧中心保存「关闭」后宿主把可见 mode 改成 api：旧版只调 API。
        default_order = ["auto", "astrbot", "api", "web", "off"]
        web_before_api = ["web", "auto", "astrbot", "api", "off"]
        web_before_api_v0 = ["web", "api", "auto", "astrbot", "off"]
        closed_order = ["off", "auto", "astrbot", "api", "web"]
        cases = [
            ("auto", default_order, ["auto", "web"], False,
             ["astrbot", "api", "web"], ["astrbot", "api", "web"],
             ["astrbot", "anysearch", "api", "web"]),
            ("api", web_before_api, ["api"], True,
             ["api", "web"], ["api", "web", "astrbot"],
             ["api", "web"]),
            # 同一个样本多勾一个未生效的 auto：auto 不算具体组多选，
            # 执行顺序仍必须是 API → 网页。
            ("api", web_before_api_v0, ["api", "auto"], True,
             ["api", "web"], ["api", "web", "astrbot"],
             ["api", "web"]),
            ("api", web_before_api_v0, ["api"], True,
             ["api", "web"], ["api", "web", "astrbot"],
             ["api", "web"]),
            ("api", closed_order, ["off"], False,
             ["api"], ["api", "astrbot", "web"], ["api"]),
        ]
        for (mode, order, enabled, fallback,
             expect_groups, expect_order, expect_calls) in cases:
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_order": order,
                    "mode_enabled": enabled,
                    "fallback_enabled": fallback,
                    "mode_prefs_version": 1,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            key = (mode, order, enabled, fallback)
            self.assertEqual(
                raw["search_config"]["mode_order"], expect_order, key
            )
            self.assertEqual(plugin.search_service.groups, expect_groups, key)
            self.assertEqual(
                plugin.search_service._candidate_order(), expect_calls, key
            )
            backends = {
                name: _RecordingBackend(
                    name,
                    status="failed" if name != "web" else "success",
                    results=[] if name != "web" else [{
                        "title": "T", "url": "https://example.com/a",
                        "snippet": "S", "source": "bing",
                    }],
                    reason="http_500" if name != "web" else "",
                )
                for name in ("astrbot", "anysearch", "api", "web")
            }
            plugin.search_service._backends = backends
            result = await plugin.search_service.search_game_info("原神")
            self.assertEqual(
                [a["backend"] for a in result["attempted"]], expect_calls, key
            )
            await plugin.search_service.close()

            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(
                raw2["search_config"]["mode_enabled"], expect_groups, key
            )
            self.assertEqual(
                raw2["search_config"]["mode_order"], expect_order, key
            )
            self.assertEqual(plugin2.search_service.groups, expect_groups, key)
            self.assertEqual(
                plugin2.search_service._candidate_order(), expect_calls, key
            )
            await plugin2.search_service.close()

    async def test_host_filled_defaults_do_not_override_mode_off(self):
        # 审查回归：旧 v1 配置只有 mode=off，列表字段缺失，宿主补入三组
        # 默认值后不能当成显式全选；升级后必须仍然 0 次搜索。
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "off",
                "mode_prefs_version": 1,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_enabled"], [])
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(plugin.search_service.groups, [])
        self.assertEqual(plugin.search_service._candidate_order(), [])
        state = await plugin.config_center_api.state()
        self.assertEqual(state["search"]["mode"], "off")
        self.assertEqual(state["search"]["mode_enabled"], [])
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(plugin2.search_service.groups, [])
        self.assertEqual(raw2["search_config"]["mode_enabled"], [])

    async def test_material_prefs_default_to_direct_and_keep_explicit_summary(self):
        # 旧默认是“AI 汇总 + 150 字”；升级后默认素材直传，只关一次；
        # 用户之后在管理页重新开启汇总要被保留。
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "off",
                "mode_order": ["astrbot", "api", "web"],
                "mode_enabled": [],
                "mode_prefs_version": 2,
                "summarize_enabled": True,
            },
        })
        self._write(data)
        raw = self._load()
        OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertFalse(cfg["summarize_enabled"])
        self.assertEqual(cfg["material_max_chars"], 3000)
        self.assertEqual(cfg["material_max_items"], 6)
        self.assertEqual(cfg["material_snippet_chars"], 400)
        self.assertEqual(cfg["material_prefs_version"], 1)

        raw.save_config()
        raw2 = self._load()
        OnePercentGenerator(Ctx(), raw2)
        self.assertFalse(raw2["search_config"]["summarize_enabled"])
        self.assertEqual(raw2["search_config"]["material_max_chars"], 3000)

        # 迁移版本已写入：用户显式开启 AI 汇总后不再被强制关闭。
        raw2["search_config"]["summarize_enabled"] = True
        raw2.save_config()
        raw3 = self._load()
        OnePercentGenerator(Ctx(), raw3)
        self.assertTrue(raw3["search_config"]["summarize_enabled"])

    async def test_host_filled_defaults_with_corrupt_order_use_mode(self):
        # mode_enabled 缺失、mode_order 为数字：旧 off 与仅 API 都不能扩成
        # 全开，必须沿用旧 mode。
        for mode, expected in (("off", []), ("api", ["api"])):
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": mode,
                    "mode_order": 123,
                    "mode_prefs_version": 1,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            self.assertEqual(
                raw["search_config"]["mode_enabled"], expected, mode
            )
            self.assertEqual(plugin.search_service.groups, expected, mode)
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(
                raw2["search_config"]["mode_enabled"], expected, mode
            )
            self.assertEqual(plugin2.search_service.groups, expected, mode)

    async def test_host_filled_defaults_with_missing_lists_follow_auto(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "auto",
                "mode_prefs_version": 1,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(
            raw["search_config"]["mode_enabled"],
            ["astrbot", "api", "web"],
        )
        self.assertEqual(plugin.search_service.groups, ["astrbot", "api", "web"])

    async def test_master_off_preserves_groups_for_later_enable(self):
        # 审查回归：总开关关闭时保存的 auto 列表被清空，重新打开后无组可用。
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": False,
                "mode": "auto",
                "mode_order": ["auto", "astrbot", "api", "web", "off"],
                "mode_enabled": ["auto"],
                "mode_prefs_version": 1,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["mode_enabled"], ["astrbot", "api", "web"])
        # 总开关关闭时运行态不尝试任何组，但列表已保留，供重新开启。
        self.assertEqual(plugin.search_service.groups, [])
        state = await plugin.config_center_api.state()
        self.assertFalse(state["search"]["enabled"])
        self.assertEqual(state["search"]["mode_enabled"], ["astrbot", "api", "web"])

        # 打开总开关并保存：组列表仍在，生成立刻可用。
        payload = {
            "section": "search",
            "data": {
                "enabled": True,
                "mode_order": state["search"]["mode_order"],
                "mode_enabled": state["search"]["mode_enabled"],
                "result_count": 3,
                "timeout_ms": 8000,
                "per_backend_timeout_ms": 0,
                "summary_input_max_chars": 6000,
                "summarize_enabled": True,
                "summarize_max_chars": 150,
                "api_provider": "tavily",
                "api_base_url": "https://api.tavily.com",
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(raw["search_config"]["mode_enabled"], ["astrbot", "api", "web"])
        self.assertEqual(plugin.search_service.groups, ["astrbot", "api", "web"])

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["search_config"]["mode_enabled"], ["astrbot", "api", "web"])
        self.assertEqual(plugin2.search_service.groups, ["astrbot", "api", "web"])

    async def test_explicit_three_groups_with_default_order_are_kept(self):
        # 审查回归：默认行顺序 + 显式勾选三个具体组，升级后不能只剩 AstrBot。
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "astrbot",
                "mode_order": ["auto", "astrbot", "api", "web", "off"],
                "mode_enabled": ["astrbot", "api", "web"],
                "mode_prefs_version": 1,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["astrbot", "api", "web"])
        self.assertEqual(plugin.search_service.groups, ["astrbot", "api", "web"])
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["search_config"]["mode_enabled"], ["astrbot", "api", "web"])
        self.assertEqual(plugin2.search_service.groups, ["astrbot", "api", "web"])

    async def test_explicit_empty_selection_stays_off(self):
        # 审查回归：旧版显式空列表 [] 表示关闭，不能按旧 mode=web 开网页搜索。
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "web",
                "mode_order": ["auto", "astrbot", "api", "web", "off"],
                "mode_enabled": [],
                "mode_prefs_version": 1,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_enabled"], [])
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(plugin.search_service.groups, [])
        state = await plugin.config_center_api.state()
        self.assertEqual(state["search"]["mode"], "off")
        self.assertEqual(state["search"]["mode_enabled"], [])
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(plugin2.search_service.groups, [])
        self.assertEqual(raw2["search_config"]["mode_enabled"], [])

    async def test_inactive_auto_keeps_group_order_and_checks(self):
        # 审查回归：未勾选、或勾选但并未生效的旧 auto 不能重排顺序，也不能
        # 打开从未实际运行的分组。
        cases = [
            (["auto", "web", "astrbot", "api", "off"], ["web"]),
            (["web", "auto", "astrbot", "api", "off"], ["web", "auto"]),
        ]
        for order, enabled in cases:
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": True,
                    "mode": "web",
                    "mode_order": order,
                    "mode_enabled": enabled,
                    "mode_prefs_version": 0,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            cfg = raw["search_config"]
            self.assertEqual(cfg["mode_order"], ["web", "astrbot", "api"], order)
            self.assertEqual(cfg["mode_enabled"], ["web"], order)
            self.assertEqual(cfg["mode"], "web")
            self.assertEqual(plugin.search_service.groups, ["web"], order)

    async def test_active_auto_expands_to_three_groups(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "auto",
                "mode_order": ["auto", "web", "astrbot", "api", "off"],
                "mode_enabled": ["auto"],
                "mode_prefs_version": 0,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["astrbot", "api", "web"])
        self.assertEqual(plugin.search_service.groups, ["astrbot", "api", "web"])

    async def test_corrupt_numeric_mode_order_does_not_crash(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "web",
                "mode_order": 123,
                "mode_enabled": ["web"],
                "mode_prefs_version": 0,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["web"])
        self.assertEqual(plugin.search_service.groups, ["web"])

    async def test_string_false_enabled_stays_closed(self):
        for value in ("false", "0", "off"):
            data = _old_config(umo="", groups=[], admin=None)
            data.update({
                "config_migration_version": MIGRATION_VERSION,
                "search_config": {
                    "enabled": value,
                    "mode": "auto",
                    "mode_order": ["astrbot", "api", "web"],
                    "mode_enabled": ["astrbot", "api", "web"],
                    "mode_prefs_version": 2,
                },
            })
            self._write(data)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            self.assertEqual(plugin.search_service.groups, [], value)
            state = await plugin.config_center_api.state()
            self.assertFalse(state["search"]["enabled"], value)
            self.assertEqual(
                [item for item in state["search"]["candidates"] if item["in_mode"]],
                [],
                value,
            )

    async def test_saving_only_other_search_fields_preserves_group_list(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "web",
                "mode_order": ["web", "api", "astrbot"],
                "mode_enabled": ["web", "api"],
                "mode_prefs_version": 2,
                "fallback_enabled": False,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        payload = {
            "section": "search",
            "data": {
                "result_count": 7,
                "timeout_ms": 9000,
                "per_backend_timeout_ms": 0,
                "summary_input_max_chars": 6000,
                "summarize_enabled": True,
                "summarize_max_chars": 200,
                "api_provider": "tavily",
                "api_base_url": "https://api.tavily.com",
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["web", "api", "astrbot"])
        self.assertEqual(cfg["mode_enabled"], ["web", "api"])
        self.assertEqual(cfg["mode"], "web")
        self.assertEqual(cfg["result_count"], 7)
        self.assertEqual(plugin.search_service.groups, ["web", "api"])

    async def test_saving_other_search_fields_keeps_group_policy(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {"enabled": True, "mode": "api"},
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        state = await plugin.config_center_api.state()
        payload = {
            "section": "search",
            "data": {
                "enabled": True,
                "mode": state["search"]["mode"],
                "mode_order": state["search"]["mode_order"],
                "mode_enabled": state["search"]["mode_enabled"],
                "result_count": 4,
                "timeout_ms": 9000,
                "per_backend_timeout_ms": 3000,
                "summary_input_max_chars": 6000,
                "summarize_enabled": True,
                "summarize_max_chars": 200,
                "api_provider": "tavily",
                "api_base_url": "https://api.tavily.com",
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["api"])
        self.assertEqual(cfg["mode"], "api")
        self.assertEqual(cfg["result_count"], 4)
        self.assertEqual(cfg["mode_prefs_version"], 2)
        self.assertFalse(cfg["fallback_enabled"])

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        state2 = await plugin2.config_center_api.state()
        self.assertEqual(state2["search"]["mode"], "api")
        self.assertEqual(raw2["search_config"]["mode_enabled"], ["api"])
        self.assertEqual(plugin2.search_service.groups, ["api"])
    async def test_explicit_legacy_prefs_preserve_order_and_disabled_groups(self):
        order = ["web", "api", "auto", "astrbot", "off"]
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "web",
                "mode_order": order,
                "mode_enabled": ["web"],
                "mode_prefs_version": 0,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        # 旧列表里用户已显式排序、只勾了 web：保留相对顺序与勾选，
        # 不因 mode=auto 重新启用 api/astrbot。
        self.assertEqual(cfg["mode_order"], ["web", "api", "astrbot"])
        self.assertEqual(cfg["mode_enabled"], ["web"])
        self.assertEqual(cfg["mode"], "web")
        self.assertEqual(cfg["mode_prefs_version"], 2)
        self.assertEqual(plugin.search_service.groups, ["web"])
        state = await plugin.config_center_api.state()
        self.assertEqual(state["search"]["mode"], "web")

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        state2 = await plugin2.config_center_api.state()
        cfg2 = raw2["search_config"]
        self.assertEqual(cfg2["mode_order"], ["web", "api", "astrbot"])
        self.assertEqual(cfg2["mode_enabled"], ["web"])
        self.assertEqual(plugin2.search_service.groups, ["web"])
        self.assertEqual(state2["search"]["mode"], "web")
    async def test_legacy_single_mode_honored_when_list_is_schema_default(self):
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                # 旧版只改过可见 mode，列表仍是 schema 默认值。
                "mode": "web",
                "mode_order": ["auto", "astrbot", "api", "web", "off"],
                "mode_enabled": ["auto"],
                "mode_prefs_version": 1,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode"], "web")
        self.assertEqual(cfg["mode_enabled"], ["web"])
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(plugin.search_service.groups, ["web"])
        state = await plugin.config_center_api.state()
        self.assertEqual(state["search"]["mode"], "web")
        self.assertEqual(state["search"]["mode_enabled"], ["web"])
    def test_new_install_search_defaults_to_three_groups(self):
        self._write({})
        raw = self._load()
        OnePercentGenerator(Ctx(), raw)
        cfg = raw["search_config"]
        self.assertEqual(cfg["mode_order"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode_enabled"], ["astrbot", "api", "web"])
        self.assertEqual(cfg["mode"], "astrbot")
        self.assertFalse(cfg["fallback_enabled"])
        self.assertEqual(cfg["mode_prefs_version"], 2)
    async def test_saved_group_order_drives_generation_and_search_test(self):
        # 生成与主动测试共用 plugin.search_service；它必须由已保存的组列表
        # 构建，而不是任何旧 mode 字段。
        data = _old_config(umo="", groups=[], admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "search_config": {
                "enabled": True,
                "mode": "web",
                "mode_order": ["web", "api", "astrbot"],
                "mode_enabled": ["web", "api"],
                "mode_prefs_version": 2,
                "fallback_enabled": False,
            },
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.search_service.groups, ["web", "api"])

        seen = []

        class FakeService:
            groups = ["web", "api"]

            async def test_search(self, game, event=None):
                seen.append(game)
                return {"success": True, "message": "mock ok"}

        plugin.search_service = FakeService()
        plugin._record_search = AsyncMock()
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value={"game": "原神"}),
        ):
            result = await plugin.config_center_api.search_test()
        self.assertTrue(result.get("success"), result)
        self.assertEqual(seen, ["原神"])

    def test_version_marker_does_not_overwrite_new_sessions(self):
        self._write(_old_config(groups=[OLD_GROUP]))
        raw = self._load()
        OnePercentGenerator(Ctx(), raw)
        raw["session_settings"]["sessions"] = [{
            "umo": "webchat:FriendMessage:new-user",
            "connector": "webchat",
            "type": "private",
            "enabled": True,
        }]
        raw.save_config()
        raw2 = self._load()
        plugin = OnePercentGenerator(Ctx(), raw2)
        sessions = raw2["session_settings"]["sessions"]
        self.assertEqual([item["umo"] for item in sessions],
                         ["webchat:FriendMessage:new-user"])
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertTrue(plugin.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))

    async def test_admin_save_via_api_removes_pending_and_restarts(self):
        self._write(_old_config(umo=OLD_UMO))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        payload = {
            "section": "admins",
            "data": {"admin_users": [{
                "connector": "qq_official_bot", "uid": "admin-old"
            }]},
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertFalse(raw["legacy_admin_pending"])
        self.assertEqual(raw["admin_qqs"], [])
        self.assertTrue(plugin.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))
        self.assertFalse(plugin.config_center.is_admin(
            "webchat", "admin-old"
        ))

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))
        self.assertFalse(plugin2.config_center.legacy_admin_present())


    async def test_unknown_legacy_umo_is_pending_and_cannot_open_access(self):
        self._write(_old_config(
            umo="qq:OtherMessage:restricted", groups=[], privates=[]
        ))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        # 整张旧列表都不可迁移时，保持 null（尚未配置），不能留下
        # “空列表=不限制”的假象。
        self.assertIsNone(raw["session_settings"]["sessions"])
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertEqual(raw["legacy_umo_pending"], [{
            "umo": "qq:OtherMessage:restricted", "reason": "unknown_type",
        }])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:OtherMessage:restricted"
        ))
        decision = await plugin._authorize_request(
            "qq", "someone", "qq:FriendMessage:someone", "原神", ""
        )
        self.assertEqual(decision["kind"], "message")
        self.assertIn("旧版", decision["text"])

        # 保存并重启后仍保持关闭；重复迁移不能把 pending 清掉。
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["config_migration_version"], MIGRATION_VERSION)
        self.assertEqual(raw2["legacy_umo_pending"], [{
            "umo": "qq:OtherMessage:restricted", "reason": "unknown_type",
        }])
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))

        # 管理员明确写入新 UMO 策略后，只有明确条目被授权。
        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{
                    "umo": "webchat:FriendMessage:new-user",
                    "enabled": True,
                }],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(raw2["legacy_umo_pending"], [])
        self.assertFalse(raw2["legacy_restrictions_pending"])
        self.assertTrue(plugin2.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))

        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        self.assertTrue(plugin3.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))
        self.assertFalse(plugin3.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))

    async def test_legacy_whitelist_only_blank_session_umo_stays_pending(self):
        # 旧白名单只有 qq:FriendMessage:（第三段为空）时，不能因为冒号
        # 数量足够就当成可迁移会话；必须保持 null 并留下待修诊断。
        old = _old_config(umo="", groups=[], privates=[])
        old["whitelist_umos"] = ["qq:FriendMessage:"]
        self._write(old)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        expected = [{
            "umo": "qq:FriendMessage:", "reason": "invalid_format",
        }]
        self.assertIsNone(raw["session_settings"]["sessions"])
        self.assertEqual(raw["legacy_umo_pending"], expected)
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:"
        ))
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))
        state = await plugin.config_center_api.state()
        self.assertEqual(state["session"]["legacy_umo_pending"], expected)

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertIsNone(raw2["session_settings"]["sessions"])
        self.assertEqual(raw2["legacy_umo_pending"], expected)
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:"
        ))
        self.assertFalse(plugin2.config_center.is_session_allowed(OLD_UMO))

    async def test_legacy_whitelist_mix_valid_and_blank_session_umos(self):
        # 有效旧 UMO 与第三段为空的旧 UMO 混合时，只迁移有效项；
        # 空白第三段保留为待修诊断，普通会话仍被拒绝。
        old = _old_config(umo="", groups=[], privates=[])
        old["whitelist_umos"] = [
            OLD_UMO, "qq:FriendMessage:", "qq:OtherMessage:restricted",
        ]
        self._write(old)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertEqual(
            [item["umo"] for item in raw["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertEqual(raw["legacy_umo_pending"], [
            {"umo": "qq:FriendMessage:", "reason": "invalid_format"},
            {"umo": "qq:OtherMessage:restricted", "reason": "unknown_type"},
        ])
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:"
        ))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:OtherMessage:restricted"
        ))
        state = await plugin.config_center_api.state()
        self.assertEqual(
            state["session"]["legacy_umo_pending"],
            raw["legacy_umo_pending"],
        )

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(
            [item["umo"] for item in raw2["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertEqual(raw2["legacy_umo_pending"], raw["legacy_umo_pending"])
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:"
        ))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))

    async def test_persisted_v4_legacy_empty_session_entries_are_repaired(self):
        # 升级回归：旧候选 e0472c0 会把 `qq:FriendMessage:`（平台/类型合法
        # 但完整会话 ID 为空）当有效 UMO 写进 sessions 并落盘为 v4。新版
        # 加载时必须定向修复：有效条目保留可用，坏条目移回 pending 待修，
        # 且再保存、重载后行为不变。
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": MIGRATION_VERSION,
            "umos_unrestricted_confirmed": False,
            "legacy_restrictions_pending": True,
            "legacy_umo_pending": [
                {"umo": "qq:OtherMessage:restricted", "reason": "unknown_type"},
            ],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [
                    {"umo": OLD_UMO, "connector": "qq_official_bot",
                     "type": "private", "enabled": True},
                    {"umo": "qq:FriendMessage:", "connector": "qq",
                     "type": "private", "enabled": True},
                ],
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertEqual(
            [item["umo"] for item in raw["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertEqual(raw["legacy_umo_pending"], [
            {"umo": "qq:OtherMessage:restricted", "reason": "unknown_type"},
            {"umo": "qq:FriendMessage:", "reason": "invalid_format"},
        ])
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertEqual(plugin.config_center.config_error, "")
        self.assertEqual(plugin.config_center.invalid_sessions, [])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:"
        ))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))
        state = await plugin.config_center_api.state()
        self.assertEqual(
            state["session"]["legacy_umo_pending"],
            raw["legacy_umo_pending"],
        )
        self.assertEqual(
            [item["umo"] for item in state["session"]["sessions"]], [OLD_UMO]
        )

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(
            [item["umo"] for item in raw2["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertEqual(raw2["legacy_umo_pending"], raw["legacy_umo_pending"])
        self.assertEqual(plugin2.config_center.config_error, "")
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:someone"
        ))

    async def test_persisted_v4_only_empty_session_entry_stays_closed(self):
        # 边界：落盘 v4 若只有 `qq:FriendMessage:` 这种坏条目，修复后不能
        # 变成空列表（空列表=不限制），必须回到 null（未配置）并保持拒绝。
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": MIGRATION_VERSION,
            "umos_unrestricted_confirmed": False,
            "legacy_restrictions_pending": True,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [
                    {"umo": "qq:FriendMessage:", "connector": "qq",
                     "type": "private", "enabled": True},
                ],
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertIsNone(raw["session_settings"]["sessions"])
        self.assertEqual(raw["legacy_umo_pending"], [
            {"umo": "qq:FriendMessage:", "reason": "invalid_format"},
        ])
        self.assertTrue(raw["legacy_restrictions_pending"])
        self.assertEqual(plugin.config_center.config_error, "sessions_unset")
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:anyone"
        ))

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertIsNone(raw2["session_settings"]["sessions"])
        self.assertEqual(plugin2.config_center.config_error, "sessions_unset")
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:anyone"
        ))

    async def test_mixed_known_and_unknown_umo_keeps_exact_known_access(self):
        old = _old_config(umo="", groups=[], privates=[])
        old["whitelist_umos"] = [
            OLD_UMO, "qq:OtherMessage:restricted", "qq:OtherMessage",
            "", None,
        ]
        self._write(old)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        sessions = raw["session_settings"]["sessions"]
        self.assertEqual([item["umo"] for item in sessions], [OLD_UMO])
        self.assertTrue(plugin.config_center.migration_required())
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:other"
        ))
        reasons = {item["reason"] for item in raw["legacy_umo_pending"]}
        self.assertEqual(
            reasons, {"unknown_type", "invalid_format", "empty"}
        )

        plugin.rate_limiter = SimpleNamespace(
            check_and_record=AsyncMock(return_value={
                "allowed": True, "message": "",
            })
        )
        allowed = await plugin._authorize_request(
            "qq_official_bot", "openid-1", OLD_UMO, "原神", ""
        )
        self.assertEqual(allowed["kind"], "ok", allowed)
        denied = await plugin._authorize_request(
            "qq_official_bot", "openid-other",
            "qq_official_bot:FriendMessage:other", "原神", ""
        )
        self.assertEqual(denied["kind"], "message")

    async def test_invalid_legacy_umo_entries_are_visible_and_safe(self):
        old = _old_config(umo="", groups=[], privates=[])
        old["whitelist_umos"] = [None, "", "qq:OtherMessage", "no-colon"]
        self._write(old)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)

        self.assertIsNone(raw["session_settings"]["sessions"])
        self.assertTrue(raw["legacy_restrictions_pending"])
        # None 与空字符串都归一为空条目并去重，其余两个是格式错误。
        self.assertEqual(len(raw["legacy_umo_pending"]), 3)
        self.assertEqual(
            {item["reason"] for item in raw["legacy_umo_pending"]},
            {"empty", "invalid_format"},
        )
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:anyone"
        ))
        state = await plugin.config_center_api.state()
        self.assertEqual(
            state["session"]["legacy_umo_pending"],
            raw["legacy_umo_pending"],
        )

        # 已删除的确认字段不能把空列表变成“不限制”。
        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
                "umos_unrestricted_confirmed": True,
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertEqual(result.get("status"), "error", result)
        self.assertIn("请添加明确的 UMO 会话", result.get("message", ""))
        self.assertTrue(plugin.config_center.migration_required())

        # 只有明确的 UMO 会话可以完成迁移。
        payload["data"]["sessions"] = [{
            "umo": "qq:FriendMessage:anyone",
            "enabled": True,
        }]
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertFalse(plugin.config_center.migration_required())
        self.assertTrue(plugin.config_center.is_session_allowed(
            "qq:FriendMessage:anyone"
        ))

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertFalse(plugin2.config_center.migration_required())
        self.assertTrue(plugin2.config_center.is_session_allowed(
            "qq:FriendMessage:anyone"
        ))
        self.assertFalse(raw2["umos_unrestricted_confirmed"])


    async def test_admin_removal_via_api_persists_to_real_config(self):
        data = _old_config(umo="", admin=None)
        data.update({
            "config_migration_version": MIGRATION_VERSION,
            "admin_users": [{
                "connector": "qq_official_bot",
                "uid": "admin-old",
                "legacy": False,
            }],
        })
        self._write(data)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))

        payload = {
            "section": "admins",
            "data": {"admin_users": []},
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertFalse(plugin.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))
        on_disk = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.assertEqual(on_disk["admin_users"], [])

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertFalse(plugin2.config_center.is_admin(
            "qq_official_bot", "admin-old"
        ))

    async def test_session_switches_and_migration_completion_persist(self):
        self._write(_old_config(umo="", groups=[OLD_GROUP]))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.migration_required())

        payload = {
            "section": "sessions",
            "data": {
                "allow_private": False,
                "allow_group": False,
                "sessions": [{
                    "umo": "webchat:FriendMessage:new-user",
                    "enabled": False,
                }],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertFalse(raw["legacy_restrictions_pending"])
        self.assertFalse(raw["session_settings"]["allow_private"])
        self.assertFalse(raw["session_settings"]["allow_group"])
        self.assertFalse(plugin.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertFalse(plugin2.config_center.migration_required())
        self.assertFalse(raw2["session_settings"]["allow_private"])
        self.assertFalse(raw2["session_settings"]["allow_group"])
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "webchat:FriendMessage:new-user"
        ))
        self.assertFalse(raw2["umos_unrestricted_confirmed"])

    # ---- 残留状态与非法会话列表：不得误放行 ----

    def _base_no_umo(self):
        return _old_config(umo=None, groups=[], privates=[], admin=None)

    def test_residual_unrestricted_confirmed_reoffers_pause(self):
        # v3/v4 残留：确认标记仍在、pending=False、sessions 为空。
        for version in (3, 4):
            cfg = self._base_no_umo()
            cfg.update({
                "config_migration_version": version,
                "umos_unrestricted_confirmed": True,
                "legacy_restrictions_pending": False,
                "legacy_umo_pending": [],
                "session_settings": {
                    "allow_private": True,
                    "allow_group": True,
                    "sessions": [],
                },
            })
            self._write(cfg)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            self.assertTrue(raw["legacy_restrictions_pending"], version)
            self.assertFalse(raw["umos_unrestricted_confirmed"])
            self.assertTrue(plugin.config_center.migration_required(), version)
            self.assertFalse(plugin.config_center.is_session_allowed(
                "qq_official_bot:FriendMessage:anyone"
            ), version)

            # 清理结果必须真正落盘：重启后仍是待确认状态，不能重新放行。
            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertTrue(raw2["legacy_restrictions_pending"], version)
            self.assertFalse(raw2["umos_unrestricted_confirmed"])
            self.assertTrue(plugin2.config_center.migration_required(), version)
            self.assertFalse(plugin2.config_center.is_session_allowed(
                "qq_official_bot:FriendMessage:anyone"
            ), version)

    async def test_nonempty_invalid_sessions_does_not_become_unrestricted(self):
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 4,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{"umo": ""}, {"oops": 1}, "junk"],
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "no_valid_sessions")
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        # 非法列表落盘重启后仍拒绝，且不能与“有意的空列表”混为一谈。
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(plugin2.config_center.config_error, "no_valid_sessions")
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        # 管理员补一条精确 UMO 后，只放行这个 UMO。
        corrected = "qq_official_bot:FriendMessage:recovered"
        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{"umo": corrected, "enabled": True}],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(plugin2.config_center.config_error, "")
        self.assertTrue(plugin2.config_center.is_session_allowed(corrected))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        self.assertEqual(plugin3.config_center.config_error, "")
        self.assertTrue(plugin3.config_center.is_session_allowed(corrected))
        self.assertFalse(plugin3.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    def test_real_empty_session_list_stays_unrestricted(self):
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 4,
            "umos_unrestricted_confirmed": False,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "")
        self.assertTrue(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        # 空列表是合法的“不限制”，重启后依旧放行。
        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(plugin2.config_center.config_error, "")
        self.assertTrue(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    def test_explicit_session_list_restricts_other_chats_after_restart(self):
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 4,
            "umos_unrestricted_confirmed": False,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{"umo": OLD_UMO, "enabled": True}],
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:openid-2"
        ))

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(OLD_GROUP))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:openid-2"
        ))

    # ---- P0/P1: 真实宿主默认值、非数组会话列表与读写诊断一致 ----

    def _v4_session_config(self, sessions):
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": MIGRATION_VERSION,
            "umos_unrestricted_confirmed": False,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": sessions,
            },
        })
        return cfg

    async def test_v4_null_sessions_is_unset_and_admin_repairs(self):
        # P0：真实 AstrBotConfig 4.29.4 读到 sessions:null 时，schema 的
        # items.sessions.default 必须是 null，不能补成有意的空列表。
        self._write(self._v4_session_config(None))
        raw = self._load()
        self.assertIsNone(raw["session_settings"]["sessions"])
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "sessions_unset")
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        state = await plugin.config_center_api.state()
        self.assertEqual(state["session"]["config_error"], "sessions_unset")
        self.assertEqual(state["session"]["sessions"], [])
        self.assertEqual(state["session"]["invalid_sessions"], [])
        self.assertEqual(state["session"]["sessions_raw_type"], "NoneType")

        # 保存 + 重启都不能把 null 洗成“明确不限制”。
        raw.save_config()
        raw2 = self._load()
        self.assertIsNone(raw2["session_settings"]["sessions"])
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(plugin2.config_center.config_error, "sessions_unset")
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        # 管理员在会话页保存一条精确 UMO 后恢复，且只放行这一条。
        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{"umo": OLD_UMO, "enabled": True}],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(plugin2.config_center.config_error, "")
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        self.assertEqual(raw2["session_settings"]["sessions"][0]["umo"], OLD_UMO)

        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        self.assertEqual(plugin3.config_center.config_error, "")
        self.assertTrue(plugin3.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin3.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    async def test_v4_non_list_sessions_state_is_diagnosable_and_repairable(self):
        # P1：sessions 是非数组时状态接口不能抛 TypeError（管理员需要诊断页面）。
        self._write(self._v4_session_config(7))
        raw = self._load()
        self.assertEqual(raw["session_settings"]["sessions"], 7)
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "invalid_session_list")
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))
        state = await plugin.config_center_api.state()
        self.assertEqual(state["session"]["config_error"], "invalid_session_list")
        self.assertEqual(state["session"]["sessions"], [])
        self.assertEqual(state["session"]["sessions_raw_type"], "int")

        # 落盘重启后仍保留原值，不会归一化成 [] 而误放行。
        raw.save_config()
        raw2 = self._load()
        self.assertEqual(raw2["session_settings"]["sessions"], 7)
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        state2 = await plugin2.config_center_api.state()
        self.assertEqual(state2["session"]["config_error"], "invalid_session_list")
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        # 总开关保存不携带 sessions：非数组原值 7 不能被草稿里的安全占位
        # 洗成 []，状态接口仍要能诊断。
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value={
                "section": "sessions",
                "data": {"allow_private": False, "allow_group": True},
            }),
        ):
            toggled = await plugin2.config_center_api.save()
        self.assertTrue(toggled.get("saved"), toggled)
        self.assertEqual(raw2["session_settings"]["sessions"], 7)
        self.assertFalse(raw2["session_settings"]["allow_private"])
        self.assertEqual(plugin2.config_center.config_error, "invalid_session_list")
        state3 = await plugin2.config_center_api.state()
        self.assertEqual(state3["session"]["config_error"], "invalid_session_list")
        self.assertEqual(state3["session"]["sessions_raw_type"], "int")

        # 管理员显式保存空列表 = 明确不限制。
        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(plugin2.config_center.config_error, "")
        self.assertTrue(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        raw3 = self._load()
        self.assertEqual(raw3["session_settings"]["sessions"], [])
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        self.assertTrue(plugin3.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    def test_v2_non_list_sessions_is_not_normalized_to_empty(self):
        # P0：v2 迁移曾把非数组 sessions 改成 []，导致普通会话误放行。
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 2,
            "legacy_restrictions_pending": False,
            "umos_unrestricted_confirmed": True,
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": 7,
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(raw["session_settings"]["sessions"], 7)
        self.assertEqual(plugin.config_center.config_error, "invalid_session_list")
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["session_settings"]["sessions"], 7)
        self.assertEqual(plugin2.config_center.config_error, "invalid_session_list")
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    async def test_invalid_session_entries_read_write_diagnostics_match(self):
        # P2：无效 UMO 的读诊断、状态接口和保存校验必须一致，不能
        # “读成启用、保存又被拒绝”或静默丢弃后部分生效。
        self._write(self._v4_session_config([
            {"umo": OLD_UMO, "enabled": True},
            {"umo": "bad"},
            "junk",
        ]))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "invalid_session_entries")
        self.assertEqual(plugin.config_center.invalid_sessions, [
            {"index": 1, "umo": "bad", "reason": "umo_malformed"},
            {"index": 2, "umo": "", "reason": "entry_not_object"},
        ])
        # 存在无效条目时整体拒绝，不能“有效条目部分生效”。
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))
        state = await plugin.config_center_api.state()
        self.assertEqual(
            state["session"]["invalid_sessions"],
            plugin.config_center.invalid_sessions,
        )
        self.assertEqual(
            [item["umo"] for item in state["session"]["sessions"]], [OLD_UMO]
        )

        # 保存原始列表：与读路径同一诊断（第二条 UMO 无效），且不改变运行态。
        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [
                    {"umo": OLD_UMO, "enabled": True},
                    {"umo": "bad"},
                    "junk",
                ],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            rejected = await plugin.config_center_api.save()
        self.assertFalse(rejected.get("saved"))
        self.assertIn("会话 UMO 无效", rejected.get("message", ""))
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))

        # 移除无效条目后保存：只放行这一条精确 UMO。
        payload["data"]["sessions"] = [{"umo": OLD_UMO, "enabled": True}]
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            accepted = await plugin.config_center_api.save()
        self.assertTrue(accepted.get("saved"), accepted)
        self.assertEqual(plugin.config_center.config_error, "")
        self.assertEqual(plugin.config_center.invalid_sessions, [])
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(OLD_GROUP))

    async def test_switch_save_preserves_unset_and_explicit_session_lists(self):
        # P0：私聊/群聊总开关只表达放行策略。真实宿主把 sessions:null 装进
        # 页面时，草稿里的 [] 只是安全占位；开关保存绝不能把它提交成
        # “明确不限制”，刷新/重启后必须继续拒绝普通会话。
        self._write(self._v4_session_config(None))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "sessions_unset")

        toggle = {
            "section": "sessions",
            "data": {"allow_private": False, "allow_group": True},
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=toggle),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertIsNone(raw["session_settings"]["sessions"])
        self.assertFalse(raw["session_settings"]["allow_private"])
        self.assertEqual(plugin.config_center.config_error, "sessions_unset")
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        state = await plugin.config_center_api.state()
        self.assertEqual(state["session"]["config_error"], "sessions_unset")
        self.assertEqual(state["session"]["sessions"], [])
        self.assertEqual(state["session"]["sessions_raw_type"], "NoneType")

        raw.save_config()
        raw2 = self._load()
        self.assertIsNone(raw2["session_settings"]["sessions"])
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(plugin2.config_center.config_error, "sessions_unset")
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        # 重启后再切一次总开关，仍然保留 null 策略。
        plugin = plugin2
        toggle["data"]["allow_private"] = True
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=toggle),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertIsNone(raw2["session_settings"]["sessions"])
        self.assertEqual(plugin.config_center.config_error, "sessions_unset")

        # 精确列表 + 总开关保存：列表原样保留，仍只放行这一条。
        self._write(self._v4_session_config([
            {"umo": OLD_UMO, "enabled": True},
        ]))
        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        toggle_list = {
            "section": "sessions",
            "data": {"allow_private": True, "allow_group": False},
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=toggle_list),
        ):
            result = await plugin3.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(
            [item["umo"] for item in raw3["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertFalse(raw3["session_settings"]["allow_group"])
        self.assertTrue(plugin3.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin3.config_center.is_session_allowed(OLD_GROUP))
        self.assertFalse(plugin3.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:openid-2"
        ))
        raw4 = self._load()
        plugin4 = OnePercentGenerator(Ctx(), raw4)
        self.assertEqual(
            [item["umo"] for item in raw4["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertFalse(raw4["session_settings"]["allow_group"])
        self.assertTrue(plugin4.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin4.config_center.is_session_allowed(OLD_GROUP))

        # 明确空列表是管理员的有意选择：开关保存继续保留不限制。
        self._write(self._v4_session_config([]))
        raw5 = self._load()
        plugin5 = OnePercentGenerator(Ctx(), raw5)
        self.assertEqual(plugin5.config_center.config_error, "")
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value={
                "section": "sessions",
                "data": {"allow_private": True, "allow_group": True},
            }),
        ):
            result = await plugin5.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(raw5["session_settings"]["sessions"], [])
        self.assertTrue(plugin5.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        raw6 = self._load()
        plugin6 = OnePercentGenerator(Ctx(), raw6)
        self.assertEqual(raw6["session_settings"]["sessions"], [])
        self.assertTrue(plugin6.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    def test_v2_null_and_missing_sessions_stay_closed(self):
        # P0：真实 4.29.4 无法区分 v2 的显式 null 与缺省字段（都读成 None），
        # 两者都必须保持拒绝，只能由管理员显式保存新策略。
        for variant in ("explicit-null", "missing-field"):
            cfg = self._base_no_umo()
            settings = {"allow_private": True, "allow_group": True}
            if variant == "explicit-null":
                settings["sessions"] = None
            cfg.update({
                "config_migration_version": 2,
                "legacy_restrictions_pending": False,
                "legacy_umo_pending": [],
                "umos_unrestricted_confirmed": True,
                "session_settings": settings,
            })
            self._write(cfg)
            raw = self._load()
            self.assertIsNone(raw["session_settings"].get("sessions"), variant)
            plugin = OnePercentGenerator(Ctx(), raw)
            self.assertEqual(
                plugin.config_center.config_error, "sessions_unset", variant
            )
            self.assertFalse(plugin.config_center.is_session_allowed(
                "qq_official_bot:FriendMessage:anyone"
            ), variant)

            raw.save_config()
            raw2 = self._load()
            self.assertIsNone(raw2["session_settings"].get("sessions"), variant)
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            self.assertEqual(
                plugin2.config_center.config_error, "sessions_unset", variant
            )
            self.assertFalse(plugin2.config_center.is_session_allowed(
                "qq_official_bot:FriendMessage:anyone"
            ), variant)

    def test_v2_null_with_migratable_whitelist_keeps_exact_restriction(self):
        # null 本身不能迁移成不限制；但旧白名单能可靠映射时，仍写明确列表。
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 2,
            "whitelist_umos": [OLD_UMO],
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": None,
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(
            [item["umo"] for item in raw["session_settings"]["sessions"]],
            [OLD_UMO],
        )
        self.assertEqual(plugin.config_center.config_error, "")
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        self.assertFalse(plugin2.config_center.migration_required())

    async def test_empty_or_blank_session_umo_is_rejected_read_and_write(self):
        # P2：读写共用校验必须拒绝空、纯空白和缺少第三段的 UMO；状态页要
        # 给出逐条诊断，保存同一列表也必须被拒绝。
        self._write(self._v4_session_config([
            {"umo": OLD_UMO},
            {"umo": "qq_official_bot:FriendMessage"},
            {"umo": "   "},
            {"umo": " : : "},
            {"umo": "bad"},
        ]))
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertEqual(plugin.config_center.config_error, "invalid_session_entries")
        self.assertEqual(plugin.config_center.invalid_sessions, [
            {"index": 1, "umo": "qq_official_bot:FriendMessage",
             "reason": "umo_malformed"},
            {"index": 2, "umo": "", "reason": "umo_missing"},
            {"index": 3, "umo": ": :", "reason": "umo_malformed"},
            {"index": 4, "umo": "bad", "reason": "umo_malformed"},
        ])
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        state = await plugin.config_center_api.state()
        self.assertEqual(
            state["session"]["invalid_sessions"],
            plugin.config_center.invalid_sessions,
        )
        self.assertEqual(
            [item["umo"] for item in state["session"]["sessions"]],
            [OLD_UMO],
        )

        # 总开关保存只改放行策略：无效条目和逐条诊断必须原样保留。
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value={
                "section": "sessions",
                "data": {"allow_private": False, "allow_group": True},
            }),
        ):
            toggled = await plugin.config_center_api.save()
        self.assertTrue(toggled.get("saved"), toggled)
        self.assertEqual(
            plugin.config_center.config_error, "invalid_session_entries"
        )
        self.assertEqual(len(plugin.config_center.invalid_sessions), 4)
        self.assertEqual(len(raw["session_settings"]["sessions"]), 5)
        self.assertFalse(raw["session_settings"]["allow_private"])
        state = await plugin.config_center_api.state()
        self.assertEqual(
            state["session"]["invalid_sessions"],
            plugin.config_center.invalid_sessions,
        )

        cases = [
            ({"umo": "qq_official_bot:FriendMessage:"}, "会话 UMO 无效"),
            ({"umo": "   "}, "会话 UMO 缺失"),
            ({"umo": "bad"}, "会话 UMO 无效"),
        ]
        for item, message in cases:
            payload = {
                "section": "sessions",
                "data": {
                    "allow_private": True,
                    "allow_group": True,
                    "sessions": [item],
                },
            }
            with patch(
                "astrbot.api.web.request.json",
                new=AsyncMock(return_value=payload),
            ):
                rejected = await plugin.config_center_api.save()
            self.assertFalse(rejected.get("saved"), rejected)
            self.assertIn(message, rejected.get("message", ""))
            self.assertEqual(
                plugin.config_center.config_error, "invalid_session_entries"
            )

        # 管理员移除无效条目后保存精确 UMO，恢复且重启一致。
        payload["data"]["sessions"] = [{"umo": OLD_UMO, "enabled": True}]
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            accepted = await plugin.config_center_api.save()
        self.assertTrue(accepted.get("saved"), accepted)
        self.assertEqual(plugin.config_center.config_error, "")
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(
            "qq_official_bot:FriendMessage:anyone"
        ))

    def _search_config(self, base_url, key):
        return {
            "enabled": True,
            "mode": "api",
            "mode_order": ["api", "auto", "off"],
            "mode_enabled": ["api"],
            "result_count": 3,
            "timeout_ms": 5000,
            "per_backend_timeout_ms": 2000,
            "summary_input_max_chars": 2000,
            "summarize_max_chars": 200,
            "api_provider": "tavily",
            "api_base_url": base_url,
            "api_key": key,
        }

    async def test_v4_missing_key_origin_is_not_rebound_on_load_or_save(self):
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 4,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
            "search_config": self._search_config(
                "https://new.example", "sk-fake-v4"
            ),
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        # v4 缺少 origin 不能把当前地址当成旧 Key 的来源；运行时拒绝使用。
        self.assertEqual(raw["search_config"].get("api_key_origin", ""), "")
        self.assertEqual(plugin.search_service.api_key, "")

        raw.save_config()
        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertEqual(raw2["search_config"].get("api_key_origin", ""), "")
        self.assertEqual(plugin2.search_service.api_key, "")

        payload = {
            "section": "search",
            "data": {
                "mode": "api",
                "mode_order": ["api", "auto", "off"],
                "mode_enabled": ["api"],
                "enabled": True,
                "result_count": 3,
                "timeout_ms": 5000,
                "per_backend_timeout_ms": 2000,
                "summary_input_max_chars": 2000,
                "summarize_max_chars": 200,
                "api_base_url": "https://new.example",
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertEqual(result.get("status"), "error")
        self.assertIn("缺少绑定地址", result.get("message", ""))
        self.assertEqual(raw2["search_config"]["api_key"], "sk-fake-v4")
        self.assertEqual(raw2["search_config"].get("api_key_origin", ""), "")

        payload["data"]["api_key"] = "sk-new-v4"
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin2.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertEqual(
            raw2["search_config"]["api_key_origin"], "https://new.example"
        )
        self.assertEqual(plugin2.search_service.api_key, "sk-new-v4")

        raw3 = self._load()
        plugin3 = OnePercentGenerator(Ctx(), raw3)
        self.assertEqual(
            raw3["search_config"]["api_key_origin"], "https://new.example"
        )
        self.assertEqual(plugin3.search_service.api_key, "sk-new-v4")
        await plugin.search_service.close()
        await plugin2.search_service.close()
        await plugin3.search_service.close()

    async def test_v3_fake_key_binds_once_and_requests_local_stand_in(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSearchHandler)
        server.captured = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d/api?token=abc" % server.server_port
        plugins = []
        try:
            cfg = self._base_no_umo()
            cfg.update({
                "config_migration_version": 3,
                "legacy_restrictions_pending": False,
                "legacy_umo_pending": [],
                "session_settings": {
                    "allow_private": True,
                    "allow_group": True,
                    "sessions": [],
                },
                "search_config": self._search_config(base, "sk-fake-standin"),
            })
            self._write(cfg)
            raw = self._load()
            plugin = OnePercentGenerator(Ctx(), raw)
            plugins.append(plugin)
            # 旧版升级边界：v3 -> v4 才补记当前地址。
            self.assertEqual(raw["search_config"]["api_key_origin"], base)
            self.assertEqual(plugin.search_service.api_key, "sk-fake-standin")

            raw.save_config()
            raw2 = self._load()
            plugin2 = OnePercentGenerator(Ctx(), raw2)
            plugins.append(plugin2)
            self.assertEqual(raw2["search_config"]["api_key_origin"], base)
            self.assertEqual(plugin2.search_service.api_key, "sk-fake-standin")

            result = await plugin2.search_service.test_search("原神")
            self.assertTrue(result.get("success"), result)
            self.assertEqual(result.get("effective_backend"), "api")
            self.assertEqual(len(server.captured), 1)
            request = server.captured[0]
            parsed = urlsplit(request["path"])
            self.assertEqual(parsed.path, "/api/search")
            self.assertEqual(parse_qs(parsed.query), {"token": ["abc"]})
            self.assertEqual(
                request["authorization"], "Bearer sk-fake-standin"
            )
            self.assertIn(
                "原神", json.loads(request["body"].decode("utf-8"))["query"]
            )
        finally:
            for plugin in plugins:
                await plugin.search_service.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    async def test_residual_confirmation_can_be_corrected_with_exact_session(self):
        cfg = self._base_no_umo()
        cfg.update({
            "config_migration_version": 4,
            "umos_unrestricted_confirmed": True,
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "session_settings": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [],
            },
        })
        self._write(cfg)
        raw = self._load()
        plugin = OnePercentGenerator(Ctx(), raw)
        self.assertTrue(plugin.config_center.migration_required())
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_UMO))

        payload = {
            "section": "sessions",
            "data": {
                "allow_private": True,
                "allow_group": True,
                "sessions": [{"umo": OLD_UMO, "enabled": True}],
            },
        }
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            result = await plugin.config_center_api.save()
        self.assertTrue(result.get("saved"), result)
        self.assertFalse(raw["legacy_restrictions_pending"])
        self.assertFalse(plugin.config_center.migration_required())
        self.assertTrue(plugin.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin.config_center.is_session_allowed(OLD_GROUP))

        raw2 = self._load()
        plugin2 = OnePercentGenerator(Ctx(), raw2)
        self.assertFalse(plugin2.config_center.migration_required())
        self.assertTrue(plugin2.config_center.is_session_allowed(OLD_UMO))
        self.assertFalse(plugin2.config_center.is_session_allowed(OLD_GROUP))


if __name__ == "__main__":
    unittest.main()
