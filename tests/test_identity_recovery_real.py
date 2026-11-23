"""身份关联持久化中断恢复的真实配置/KV 回归。"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from astrbot.core.config.astrbot_config import AstrBotConfig

    ASTRBOT_AVAILABLE = True
except Exception:
    AstrBotConfig = object
    ASTRBOT_AVAILABLE = False

from astrbot_plugin_onepercent_generator.config_center import ConfigCenter
from astrbot_plugin_onepercent_generator.config_center_api import (
    ConfigCenterAPI,
)

# 不直接从被测模块导入，便于把同一组回归跑在旧提交上验证其确实失败。
IDENTITY_MIGRATION_KEY = "identity_migration"
from astrbot_plugin_onepercent_generator.rate_limiter import RateLimiter

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8"))

# 子进程脚本：在 unlink 的配置提交后直接 os._exit，模拟真实进程终止。
WORKER = r"""
import asyncio
import json
import os
import sys
from pathlib import Path

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot_plugin_onepercent_generator.config_center import ConfigCenter
from astrbot_plugin_onepercent_generator.config_center_api import ConfigCenterAPI
from astrbot_plugin_onepercent_generator.rate_limiter import RateLimiter

cfg_path, kv_path, schema_path, link_id = sys.argv[1:5]


class ExitConfig(AstrBotConfig):
    async def save_config_async(self, *args, **kwargs):
        result = await super().save_config_async(*args, **kwargs)
        if self.__dict__.get("armed"):
            os._exit(17)
        return result


class KV:
    def __init__(self, path):
        self.path = Path(path)
        self.values = json.loads(self.path.read_text(encoding="utf-8"))

    def _flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    async def get_kv_data(self, key, default=None):
        return self.values.get(key, default)

    async def put_kv_data(self, key, value):
        self.values[key] = value
        self._flush()

    async def delete_kv_data(self, key):
        self.values.pop(key, None)
        self._flush()


class Plugin:
    def __init__(self, config, kv):
        self.config = config
        self.kv = kv
        self.rate_limiter = RateLimiter(10, 100, 20)
        self.config_center = ConfigCenter(self)
        self.config_center_api = ConfigCenterAPI(self)
        self.identity_rate_lock = asyncio.Lock()

    async def get_kv_data(self, key, default=None):
        return await self.kv.get_kv_data(key, default)

    async def put_kv_data(self, key, value):
        await self.kv.put_kv_data(key, value)

    async def delete_kv_data(self, key):
        await self.kv.delete_kv_data(key)


async def main():
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    config = ExitConfig(cfg_path, schema=schema)
    plugin = Plugin(config, KV(kv_path))
    object.__setattr__(config, "armed", True)
    await plugin.config_center_api._save_locked(
        "identity", {"action": "unlink", "id": link_id}
    )


asyncio.run(main())
"""


class SimulatedCrash(BaseException):
    """模拟进程终止；不能被 except Exception 吞掉。"""


class CrashConfig(AstrBotConfig if ASTRBOT_AVAILABLE else object):
    # AstrBotConfig.__setattr__ 会把属性写进配置字典，getattr 也可能读到
    # 同名配置项；控制标志只放实例 __dict__，并从这里读。
    def _ctl(self, name, default=None):
        return self.__dict__.get(name, default)

    async def save_config_async(self, *args, **kwargs):
        calls = int(self._ctl("save_calls", 0)) + 1
        object.__setattr__(self, "save_calls", calls)
        target = int(self._ctl("crash_before_save_number", 0) or 0)
        if target and calls == target:
            object.__setattr__(self, "crash_before_save_number", 0)
            raise SimulatedCrash("crash before config commit")
        result = await super().save_config_async(*args, **kwargs)
        if self._ctl("crash_after_save", False):
            object.__setattr__(self, "crash_after_save", False)
            raise SimulatedCrash("crash after config commit")
        return result


class RejectNextConfig(CrashConfig):
    async def save_config_async(self, *args, **kwargs):
        if self._ctl("reject_next_save", False):
            object.__setattr__(self, "reject_next_save", False)
            return False
        return await super().save_config_async(*args, **kwargs)


def _flag(target, name, value=True):
    object.__setattr__(target, name, value)


class FileKV:
    """落盘到临时文件的持久 KV；每次写后真实 flush。"""

    def __init__(
        self,
        path,
        crash_put=None,
        crash_delete=None,
        fail_delete=None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.values = {}
        self.crash_put = crash_put
        self.crash_delete = crash_delete
        self.fail_delete = fail_delete
        self._lock = asyncio.Lock()

    def _flush(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    async def get_kv_data(self, key, default=None):
        async with self._lock:
            return copy.deepcopy(self.values.get(key, default))

    async def put_kv_data(self, key, value):
        async with self._lock:
            self.values[key] = copy.deepcopy(value)
            self._flush()
        if self.crash_put and self.crash_put(key):
            raise SimulatedCrash("crash after kv put")

    async def delete_kv_data(self, key):
        async with self._lock:
            if self.fail_delete and self.fail_delete(key):
                raise RuntimeError("kv delete failed")
            self.values.pop(key, None)
            self._flush()
        if self.crash_delete and self.crash_delete(key):
            raise SimulatedCrash("crash after kv delete")


class Harness:
    def __init__(self, config, kv, daily_max=20):
        self.config = config
        self.kv = kv
        self.rate_limiter = RateLimiter(
            window_minutes=10, max_requests=100, daily_max=daily_max
        )
        self.config_center = ConfigCenter(self)
        self.config_center_api = ConfigCenterAPI(self)
        self.identity_rate_lock = asyncio.Lock()
        self.last_umo = ""
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
        return await self.kv.get_kv_data(key, default)

    async def put_kv_data(self, key, value):
        await self.kv.put_kv_data(key, value)

    async def delete_kv_data(self, key):
        await self.kv.delete_kv_data(key)

    async def authorize(self, uid, game="原神"):
        return await self._authorize(
            "qq", uid, f"qq:FriendMessage:{uid}", game, ""
        )

    async def _authorize(self, connector, sender, umo, game, experience):
        return await self._authorize_impl(
            connector, sender, umo, game, experience
        )

    async def link(self, uids):
        return await self._save({
            "section": "identity",
            "data": {"action": "link", "connector": "qq", "uids": list(uids)},
        })

    async def unlink(self, link_id):
        return await self._save({
            "section": "identity",
            "data": {"action": "unlink", "id": link_id},
        })

    async def _save(self, payload):
        with patch(
            "astrbot.api.web.request.json",
            new=AsyncMock(return_value=payload),
        ):
            return await self.config_center_api.save()


async def _bind_authorize(harness):
    from astrbot_plugin_onepercent_generator.main import OnePercentGenerator

    harness._authorize_impl = MethodType(
        OnePercentGenerator._authorize_request, harness
    )
    return harness


def _config(path, daily_max=20, config_cls=CrashConfig):
    cfg = config_cls(str(path), schema=SCHEMA)
    cfg.update({
        "rate_limit": {
            "window_minutes": 10,
            "max_requests_per_window": 100,
            "daily_max": daily_max,
        },
        "session_settings": {
            "allow_private": True,
            "allow_group": True,
            "sessions": [],
        },
        "identity_links": [],
        "identity_migration": {},
        "admin_users": [],
        "config_migration_version": 1,
        "legacy_restrictions_pending": False,
        "legacy_umo_pending": [],
        "whitelist_umos": [],
        "whitelist_groups": [],
        "whitelist_privates": [],
    })
    cfg.save_config()
    return cfg


def _paths(tmp):
    return Path(tmp) / "plugin_config.json", Path(tmp) / "kv.json"


def _open(tmp, daily_max=20, config_cls=CrashConfig, **kv_kwargs):
    cfg_path, kv_path = _paths(tmp)
    cfg = _config(cfg_path, daily_max=daily_max, config_cls=config_cls)
    return (
        Harness(cfg, FileKV(kv_path, **kv_kwargs), daily_max=daily_max),
        cfg_path,
        kv_path,
    )


def _reopen(tmp, daily_max=20, config_cls=CrashConfig, **kv_kwargs):
    cfg_path, kv_path = _paths(tmp)
    cfg = config_cls(str(cfg_path), schema=SCHEMA)
    return (
        Harness(cfg, FileKV(kv_path, **kv_kwargs), daily_max=daily_max),
        cfg_path,
        kv_path,
    )


async def _seed(harness, identity, times=1):
    with patch(
        "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
        return_value=1000.0,
    ):
        for _ in range(times):
            result = await harness.rate_limiter.check_and_record(
                identity, harness
            )
            if not result["allowed"]:
                raise AssertionError(f"seed {identity} rejected: {result}")


async def _usage(harness, identity):
    with patch(
        "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
        return_value=1000.0,
    ):
        return await harness.rate_limiter.get_usage_status(identity, harness)


@unittest.skipUnless(ASTRBOT_AVAILABLE, "需要真实 AstrBot Web API")
class IdentityRecoveryRealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    async def test_link_crash_after_config_commit_does_not_reset_quota(self):
        h, _, _ = _open(self.tmp, daily_max=1)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        _flag(h.config, "crash_after_save", True)
        with self.assertRaises(SimulatedCrash):
            await h.link(["a", "b"])

        h2, _, _ = _reopen(self.tmp, daily_max=1)
        await _bind_authorize(h2)
        self.assertTrue(h2.config.get(IDENTITY_MIGRATION_KEY))
        result = await h2.authorize("a")
        self.assertEqual(result["kind"], "message", result)
        self.assertIn("今日", result["text"])
        self.assertFalse(h2.config.get(IDENTITY_MIGRATION_KEY))
        link_id = h2.config["identity_links"][0]["id"]
        self.assertEqual((await _usage(h2, f"link:{link_id}"))["daily_used"], 1)
        self.assertNotIn("rate_limit:qq:a", h2.kv.values)

    async def test_link_crash_during_merge_is_idempotent(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a", 2)
        await _seed(h, "qq:b")
        h2, _, _ = _reopen(
            self.tmp,
            daily_max=20,
            crash_put=lambda key: key.startswith("rate_limit:link:"),
        )
        # 新对象重新读取磁盘，随后模拟进程在写 canonical 后终止。
        h = h2
        with self.assertRaises(SimulatedCrash):
            await h.link(["a", "b"])

        h3, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h3)
        ok, message = await h3.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        link_id = h3.config["identity_links"][0]["id"]
        self.assertEqual((await _usage(h3, f"link:{link_id}"))["daily_used"], 3)
        self.assertNotIn("rate_limit:qq:a", h3.kv.values)
        self.assertNotIn("rate_limit:qq:b", h3.kv.values)
        self.assertFalse(h3.config.get(IDENTITY_MIGRATION_KEY))

    async def test_link_crash_after_merge_before_journal_clear(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        _flag(h.config, "save_calls", 0)
        _flag(h.config, "crash_before_save_number", 2)
        with self.assertRaises(SimulatedCrash):
            await h.link(["a", "b"])

        h2, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h2)
        self.assertTrue(h2.config.get(IDENTITY_MIGRATION_KEY))
        ok, message = await h2.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        link_id = h2.config["identity_links"][0]["id"]
        self.assertEqual((await _usage(h2, f"link:{link_id}"))["daily_used"], 1)
        self.assertFalse(h2.config.get(IDENTITY_MIGRATION_KEY))

    async def test_unlink_crash_after_config_commit_preserves_quota(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        saved = await h.link(["a", "b"])
        self.assertTrue(saved.get("saved"), saved)
        link_id = h.config["identity_links"][0]["id"]
        _flag(h.config, "crash_after_save", True)
        with self.assertRaises(SimulatedCrash):
            await h.unlink(link_id)

        h2, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h2)
        self.assertEqual(h2.config["identity_links"], [])
        self.assertTrue(h2.config.get(IDENTITY_MIGRATION_KEY))
        ok, message = await h2.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        self.assertEqual((await _usage(h2, "qq:a"))["daily_used"], 2)
        self.assertEqual((await _usage(h2, "qq:b"))["daily_used"], 2)
        self.assertNotIn(f"rate_limit:link:{link_id}", h2.kv.values)
        self.assertFalse(h2.config.get(IDENTITY_MIGRATION_KEY))

    async def test_unlink_crash_during_split_before_source_delete(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        saved = await h.link(["a", "b"])
        self.assertTrue(saved.get("saved"), saved)
        link_id = h.config["identity_links"][0]["id"]
        h.kv.crash_put = lambda key: key == "rate_limit:qq:a"
        with self.assertRaises(SimulatedCrash):
            await h.unlink(link_id)

        h2, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h2)
        ok, message = await h2.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        self.assertEqual((await _usage(h2, "qq:a"))["daily_used"], 2)
        self.assertEqual((await _usage(h2, "qq:b"))["daily_used"], 2)
        self.assertNotIn(f"rate_limit:link:{link_id}", h2.kv.values)

    async def test_unlink_crash_after_split_before_journal_clear(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        saved = await h.link(["a", "b"])
        self.assertTrue(saved.get("saved"), saved)
        link_id = h.config["identity_links"][0]["id"]
        _flag(h.config, "save_calls", 0)
        _flag(h.config, "crash_before_save_number", 2)
        with self.assertRaises(SimulatedCrash):
            await h.unlink(link_id)

        h2, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h2)
        self.assertTrue(h2.config.get(IDENTITY_MIGRATION_KEY))
        ok, message = await h2.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        self.assertEqual((await _usage(h2, "qq:a"))["daily_used"], 2)
        self.assertEqual((await _usage(h2, "qq:b"))["daily_used"], 2)
        self.assertNotIn(f"rate_limit:link:{link_id}", h2.kv.values)

    async def test_recovery_interrupted_again_then_completes(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        _flag(h.config, "crash_after_save", True)
        with self.assertRaises(SimulatedCrash):
            await h.link(["a", "b"])

        h2, _, _ = _reopen(self.tmp, daily_max=20)
        _flag(h2.config, "save_calls", 0)
        _flag(h2.config, "crash_before_save_number", 1)
        with self.assertRaises(SimulatedCrash):
            await h2.config_center_api.ensure_identity_recovered()

        h3, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h3)
        ok, message = await h3.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        link_id = h3.config["identity_links"][0]["id"]
        self.assertEqual((await _usage(h3, f"link:{link_id}"))["daily_used"], 1)
        self.assertFalse(h3.config.get(IDENTITY_MIGRATION_KEY))

    async def test_multiple_links_merge_and_branch_usage_is_not_double_counted(self):
        h, _, _ = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        self.assertTrue((await h.link(["a", "b"])).get("saved"))
        ab_id = h.config["identity_links"][0]["id"]
        await _seed(h, "qq:c")
        await _seed(h, "qq:d")
        self.assertTrue((await h.link(["c", "d"])).get("saved"))
        # 关联期间的新请求写到 canonical#own，不污染共同历史。
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await h.rate_limiter.check_and_record(f"link:{ab_id}", h)

        saved = await h.link(["a", "b", "c", "d"])
        self.assertTrue(saved.get("saved"), saved)
        merged_id = h.config["identity_links"][0]["id"]
        self.assertEqual(
            (await _usage(h, f"link:{merged_id}"))["daily_used"], 5
        )
        # 重连自身不得重复共同历史。
        saved = await h.link(["a", "b", "c", "d"])
        self.assertTrue(saved.get("saved"), saved)
        merged_id = h.config["identity_links"][0]["id"]
        self.assertEqual(
            (await _usage(h, f"link:{merged_id}"))["daily_used"], 5
        )

    async def test_window_and_daily_limits_survive_link_unlink_merge(self):
        h, _, _ = _open(self.tmp, daily_max=100)
        h.rate_limiter = RateLimiter(10, 5, 100)
        await _bind_authorize(h)
        with patch(
            "astrbot_plugin_onepercent_generator.rate_limiter._get_current_ts",
            return_value=1000.0,
        ):
            await _seed(h, "qq:a", 2)
            await _seed(h, "qq:b")
            self.assertTrue((await h.link(["a", "b"])).get("saved"))
            link_id = h.config["identity_links"][0]["id"]
            linked = await _usage(h, f"link:{link_id}")
            self.assertEqual(linked["window_used"], 3)
            self.assertEqual(linked["daily_used"], 3)

            self.assertTrue((await h.unlink(link_id)).get("saved"))
            split = await _usage(h, "qq:a")
            self.assertEqual(split["window_used"], 3)
            for _ in range(2):
                allowed = await h.rate_limiter.check_and_record("qq:a", h)
                self.assertTrue(allowed["allowed"], allowed)
            rejected = await h.rate_limiter.check_and_record("qq:a", h)
            self.assertFalse(rejected["allowed"])
            self.assertIn("请求过于频繁", rejected["message"])
            self.assertEqual((await _usage(h, "qq:a"))["window_used"], 5)

            # 分支新增的 2 次与共同历史 3 次相加；另一侧同源历史不重复。
            self.assertTrue((await h.link(["a", "b"])).get("saved"))
            merged_id = h.config["identity_links"][0]["id"]
            merged = await _usage(h, f"link:{merged_id}")
        self.assertEqual(merged["window_used"], 5)
        self.assertEqual(merged["daily_used"], 5)

    async def test_config_rejection_rolls_back_link_and_keeps_journal_absent(self):
        h, _, _ = _open(
            self.tmp, daily_max=20, config_cls=RejectNextConfig
        )
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        _flag(h.config, "reject_next_save", True)
        result = await h.link(["a", "b"])
        self.assertEqual(result.get("status"), "error", result)
        self.assertEqual(h.config["identity_links"], [])
        self.assertFalse(h.config.get(IDENTITY_MIGRATION_KEY))
        self.assertIn("rate_limit:qq:a", h.kv.values)
        self.assertFalse([
            key for key in h.kv.values if key.startswith("rate_limit:link:")
        ])

    async def test_kv_delete_failure_rolls_back_merge(self):
        h, _, _ = _open(
            self.tmp,
            daily_max=20,
            fail_delete=lambda key: key.startswith("rate_limit:qq:"),
        )
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        result = await h.link(["a", "b"])
        self.assertEqual(result.get("status"), "error", result)
        self.assertEqual(h.config["identity_links"], [])
        self.assertFalse(h.config.get(IDENTITY_MIGRATION_KEY))
        self.assertIn("rate_limit:qq:a", h.kv.values)
        self.assertIn("rate_limit:qq:b", h.kv.values)
        self.assertFalse([
            key for key in h.kv.values if key.startswith("rate_limit:link:")
        ])

    async def test_merge_rollback_failure_keeps_journal_and_recovers_after_restart(self):
        h, _, _ = _open(self.tmp, daily_max=1)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        original_delete, original_put = h.kv.delete_kv_data, h.kv.put_kv_data
        deletes = []

        async def fail_second_delete(key):
            if key.startswith("rate_limit:qq:"):
                deletes.append(key)
                if len(deletes) >= 2:
                    raise RuntimeError("delete unavailable")
            await original_delete(key)

        async def fail_restore(key, value):
            if key in deletes and key not in h.kv.values:
                raise RuntimeError("restore unavailable")
            await original_put(key, value)

        h.kv.delete_kv_data, h.kv.put_kv_data = fail_second_delete, fail_restore
        result = await h.link(["a", "b"])
        self.assertEqual(result.get("status"), "error", result)
        self.assertTrue(h.config.get(IDENTITY_MIGRATION_KEY))
        lost_uid = deletes[0].rsplit(":", 1)[-1]
        blocked = await h.authorize(lost_uid)
        self.assertEqual(blocked["kind"], "message", blocked)
        self.assertIn("尚未恢复", blocked["text"])
        self.assertTrue(h.config.get(IDENTITY_MIGRATION_KEY))

        restarted, _, _ = _reopen(self.tmp, daily_max=1)
        await _bind_authorize(restarted)
        denied = await restarted.authorize(lost_uid)
        self.assertEqual(denied["kind"], "message", denied)
        self.assertIn("今日", denied["text"])
        self.assertFalse(restarted.config.get(IDENTITY_MIGRATION_KEY))
        link_id = restarted.config["identity_links"][0]["id"]
        self.assertEqual((await _usage(restarted, f"link:{link_id}"))["daily_used"], 2)
        again, _, _ = _reopen(self.tmp, daily_max=1)
        await _bind_authorize(again)
        self.assertEqual((await _usage(again, f"link:{link_id}"))["daily_used"], 2)

    async def test_split_rollback_failure_keeps_journal_and_recovers_after_restart(self):
        h, _, _ = _open(self.tmp, daily_max=1)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        self.assertTrue((await h.link(["a", "b"])).get("saved"))
        link_id = h.config["identity_links"][0]["id"]
        original_put = h.kv.put_kv_data
        failures = {"split": False}

        async def fail_split_and_restore(key, value):
            if key == "rate_limit:qq:b":
                failures["split"] = True
                raise RuntimeError("target write unavailable")
            if failures["split"] and key == f"rate_limit:link:{link_id}":
                raise RuntimeError("source restore unavailable")
            await original_put(key, value)

        h.kv.put_kv_data = fail_split_and_restore
        result = await h.unlink(link_id)
        self.assertEqual(result.get("status"), "error", result)
        self.assertTrue(h.config.get(IDENTITY_MIGRATION_KEY))
        blocked = await h.authorize("a")
        self.assertEqual(blocked["kind"], "message", blocked)
        self.assertIn("尚未恢复", blocked["text"])

        restarted, _, _ = _reopen(self.tmp, daily_max=1)
        await _bind_authorize(restarted)
        for uid in ("a", "b"):
            denied = await restarted.authorize(uid)
            self.assertEqual(denied["kind"], "message", denied)
            self.assertIn("今日", denied["text"])
            self.assertEqual((await _usage(restarted, f"qq:{uid}"))["daily_used"], 1)
        self.assertFalse(restarted.config.get(IDENTITY_MIGRATION_KEY))
        self.assertEqual(restarted.config["identity_links"], [])

    async def test_recovery_save_failure_blocks_then_concurrent_request_recovers(self):
        h, _, _ = _open(self.tmp, daily_max=1)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        _flag(h.config, "crash_after_save", True)
        with self.assertRaises(SimulatedCrash):
            await h.link(["a", "b"])

        h2, _, _ = _reopen(self.tmp, daily_max=1, config_cls=RejectNextConfig)
        await _bind_authorize(h2)
        _flag(h2.config, "reject_next_save", True)
        results = await asyncio.gather(
            h2.authorize("a"), h2.authorize("a")
        )
        messages = [item for item in results if item["kind"] == "message"]
        self.assertEqual(len(messages), 2, results)
        self.assertTrue(
            any("尚未恢复" in item["text"] for item in messages), messages
        )
        self.assertTrue(any("今日" in item["text"] for item in messages), messages)
        self.assertFalse(h2.config.get(IDENTITY_MIGRATION_KEY))
        link_id = h2.config["identity_links"][0]["id"]
        self.assertEqual((await _usage(h2, f"link:{link_id}"))["daily_used"], 1)

    async def test_real_process_exit_after_unlink_config_commit(self):
        h, cfg_path, kv_path = _open(self.tmp, daily_max=20)
        await _bind_authorize(h)
        await _seed(h, "qq:a")
        await _seed(h, "qq:b")
        saved = await h.link(["a", "b"])
        self.assertTrue(saved.get("saved"), saved)
        link_id = h.config["identity_links"][0]["id"]

        worker = Path(self.tmp) / "crash_worker.py"
        worker.write_text(WORKER, encoding="utf-8")
        pkg_dir = Path(self.tmp) / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "astrbot_plugin_onepercent_generator").symlink_to(REPO)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(pkg_dir) + os.pathsep + env.get("PYTHONPATH", "")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            str(cfg_path),
            str(kv_path),
            str(REPO / "_conf_schema.json"),
            link_id,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.assertEqual(await proc.wait(), 17)

        h2, _, _ = _reopen(self.tmp, daily_max=20)
        await _bind_authorize(h2)
        self.assertTrue(h2.config.get(IDENTITY_MIGRATION_KEY))
        ok, message = await h2.config_center_api.ensure_identity_recovered()
        self.assertTrue(ok, message)
        self.assertEqual((await _usage(h2, "qq:a"))["daily_used"], 2)
        self.assertEqual((await _usage(h2, "qq:b"))["daily_used"], 2)
        self.assertNotIn(f"rate_limit:link:{link_id}", h2.kv.values)
        self.assertFalse(h2.config.get(IDENTITY_MIGRATION_KEY))
