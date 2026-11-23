"""统一配置中心认证 Web API。"""

from __future__ import annotations

import copy
import logging
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config_center import ConfigCenter, _safe_list, parse_umo, session_entry_error
from .migration import (
    SEARCH_MATERIAL_PREFS_VERSION,
    SEARCH_MODE_PREFS_VERSION,
)
from .model_config_api import _norm_base
from .model_service import (
    MODEL_SOURCE_ASTRBOT,
    ModelService,
    fetch_manual_models,
)
from .search_service import (
    MODES,
    SEARCH_GROUPS,
    SearchService,
    resolve_search_groups,
    search_enabled,
)
from .ai_client import AIClientError
from .rate_limiter import KVRecoveryIncomplete

logger = logging.getLogger("astrbot")
PLUGIN_NAME = "astrbot_plugin_onepercent_generator"
SECTIONS = ("model", "search", "rate", "sessions", "admins", "identity", "advanced")
IDENTITY_MIGRATION_KEY = "identity_migration"
IDENTITY_MIGRATION_VERSION = 1


def _error(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _search_group_order(value) -> list[str]:
    """把保存的优先级列表规范成三组全序。

    旧列表里的 auto 展开为三组、off 丢弃；其余顺序保持不变，缺失的组
    按固定顺序补在末尾，保证界面始终有且只有三行。
    """
    raw = value if isinstance(value, (list, tuple)) else []
    order: list[str] = []
    for item in raw:
        key = str(item).strip().lower()
        if key == "auto":
            for group in SEARCH_GROUPS:
                if group not in order:
                    order.append(group)
        elif key in SEARCH_GROUPS and key not in order:
            order.append(key)
    for group in SEARCH_GROUPS:
        if group not in order:
            order.append(group)
    return order


def _search_group_enabled(value, strict: bool = True) -> list[str]:
    """解析勾选的组；auto 展开为三组，off/未知项按策略处理。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    elif strict:
        raise ValueError("搜索分组启用列表无效")
    else:
        raw = []
    enabled: set[str] = set()
    for item in raw:
        key = str(item).strip().lower()
        if key == "auto":
            enabled.update(SEARCH_GROUPS)
        elif key in SEARCH_GROUPS:
            enabled.add(key)
        elif key == "off":
            continue
        elif strict:
            raise ValueError("搜索分组无效")
    return [group for group in SEARCH_GROUPS if group in enabled]


def _mode_prefs(search_cfg: dict) -> tuple[str, list[str], list[str]]:
    """返回展示用 mode、三组顺序与已启用组。

    显式列表优先；旧配置尚无列表时按 mode/降级设置换算。
    """
    raw_order = search_cfg.get("mode_order")
    has_list = isinstance(raw_order, list) and bool(raw_order)
    if has_list:
        order = _search_group_order(raw_order)
        enabled = _search_group_enabled(
            search_cfg.get("mode_enabled"), strict=False
        )
    else:
        groups = resolve_search_groups(
            enabled=search_cfg.get("enabled", True),
            mode=search_cfg.get("mode"),
            fallback_enabled=search_cfg.get("fallback_enabled", False),
        )
        order = list(SEARCH_GROUPS)
        enabled = [group for group in order if group in groups]
    effective = next((group for group in order if group in enabled), "off")
    return effective, order, enabled


def _int_value(data: dict, key: str, minimum: int, maximum: int) -> int:
    raw = data.get(key)
    if isinstance(raw, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{key} 必须在 {minimum}-{maximum} 之间")
    return value


def _int_with_default(
    data: dict, old: dict, key: str, default: int, minimum: int, maximum: int
) -> int:
    """读取整数：页面显式值 > 旧配置 > 新默认值。"""
    raw = data.get(key, old.get(key, default))
    return _int_value({key: raw}, key, minimum, maximum)


class ConfigCenterAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self._save_lock = False
        self._pending_merge = None
        self._pending_split = None
        self._recovery_error = ""
        # 单调递增的配置修订号：用于拦截两个页面基于同一快照依次保存时的静默覆盖。
        self._revision = 0

    def _event(self):
        umo = getattr(self.plugin, "last_umo", "")
        return SimpleNamespace(unified_msg_origin=umo) if umo else None

    async def state(self):
        center = self.plugin.config_center
        search_cfg = self.plugin.config.get("search_config", {}) or {}
        search_mode, search_mode_order, search_mode_enabled = _mode_prefs(search_cfg)
        candidates = await self.plugin.search_service.candidate_status(
            self._event()
        )
        events = await self.plugin.stats_store.events()
        identities = center.known_identities(events)
        # 配置非法时（例如 sessions 是数字）也不能让状态接口崩溃：管理员的
        # 诊断页面正是修复入口，必须始终可读。
        raw_sessions = center.settings.get("sessions")
        safe_sessions = _safe_list(raw_sessions)
        connectors = sorted({
            *[
                parse_umo(item.get("umo", ""))[0]
                for item in safe_sessions
                if isinstance(item, dict)
            ],
            *[item.get("connector", "") for item in center.identity_links],
            *[item.get("connector", "") for item in center.admin_users],
            *[item.get("connector", "") for item in identities],
        } - {""})
        rate_cfg = self.plugin.config.get("rate_limit", {}) or {}
        return {
            "revision": self._revision,
            "model": self.plugin.model_service.public_state(),
            "search": {
                "enabled": search_enabled(search_cfg.get("enabled", True)),
                "mode": search_mode,
                "mode_order": search_mode_order,
                "mode_enabled": search_mode_enabled,
                "result_count": int(search_cfg.get("result_count", 3) or 3),
                "timeout_ms": int(search_cfg.get("timeout_ms", 8000) or 8000),
                "per_backend_timeout_ms": int(search_cfg.get("per_backend_timeout_ms", 0) or 0),
                "summary_input_max_chars": int(search_cfg.get("summary_input_max_chars", 6000) or 6000),
                "summarize_enabled": bool(search_cfg.get("summarize_enabled", False)),
                "summarize_max_chars": int(search_cfg.get("summarize_max_chars", 150) or 150),
                "material_max_chars": int(search_cfg.get("material_max_chars", 3000) or 3000),
                "material_max_items": int(search_cfg.get("material_max_items", 6) or 6),
                "material_snippet_chars": int(search_cfg.get("material_snippet_chars", 400) or 400),
                "api_provider": search_cfg.get("api_provider", "tavily"),
                "api_base_url": search_cfg.get("api_base_url", "https://api.tavily.com"),
                "api_key_configured": bool(search_cfg.get("api_key")),
                "api_key_masked": "••••••••" if search_cfg.get("api_key") else "",
                "api_key_usable": bool(search_cfg.get("api_key"))
                and bool(str(search_cfg.get("api_key_origin") or "").strip())
                and _norm_base(str(search_cfg.get("api_key_origin") or ""))
                == _norm_base(str(search_cfg.get("api_base_url") or "")),
                "candidates": candidates,
                "modes": list(SEARCH_GROUPS),
                "last_result": self.plugin.search_service.last_result or {},
            },
            "rate": {
                "window_minutes": int(
                    rate_cfg.get("window_minutes")
                    if rate_cfg.get("window_minutes") is not None else 10
                ),
                "max_requests_per_window": int(
                    rate_cfg.get("max_requests_per_window")
                    if rate_cfg.get("max_requests_per_window") is not None else 3
                ),
                "daily_max": int(
                    rate_cfg.get("daily_max")
                    if rate_cfg.get("daily_max") is not None else 20
                ),
                "reset_timezone": rate_cfg.get("reset_timezone", ""),
                "timezone_label": self._timezone_label(rate_cfg.get("reset_timezone", "")),
                "admin_exception": True,
            },
            "session": {
                "allow_private": bool(center.settings.get("allow_private", True)),
                "allow_group": bool(center.settings.get("allow_group", True)),
                "sessions": [
                    {**item, "label": self._session_label(item.get("umo", ""))}
                    for item in safe_sessions
                    if isinstance(item, dict) and not session_entry_error(item)
                ],
                "invalid_sessions": list(
                    getattr(center, "invalid_sessions", []) or []
                ),
                "sessions_raw_type": type(raw_sessions).__name__,
                "admin_users": center.admin_users,
                "legacy_admin_present": center.legacy_admin_present(),
                "legacy_admin_pending": center.legacy_admin_pending,
                "legacy_umo_pending": center.legacy_umo_pending,
                "legacy_restrictions_pending": bool(
                    self.plugin.config.get("legacy_restrictions_pending", False)
                ),
                "migration_required": center.migration_required(),
                "config_error": center.config_error,
                "identity_links": center.identity_links,
                "known_identities": identities,
                "connectors": connectors,
            },
            "advanced": {
                "preset_games": list(self.plugin.config.get("preset_games", [])),
                "show_generation_stats": bool(
                    self.plugin.config.get("show_generation_stats", True)
                ),
            },
            "stats_started_at": self.plugin.stats_store._data.get("started_at"),
        }

    @staticmethod
    def _session_label(umo: str) -> str:
        connector, chat_type = parse_umo(umo)
        tail = str(umo or "").split(":")[-1][-10:]
        return f"{connector} · {chat_type} · {tail}"

    @staticmethod
    def _timezone_label(name: str) -> str:
        if not str(name or "").strip():
            import datetime as _dt
            return _dt.datetime.now().astimezone().tzname() or "本地时区"
        return str(name)

    async def stats(self):
        from astrbot.api.web import request
        period = request.query.get("period", "7d")
        raw = request.query.get("chats", "")
        chats = None
        if raw == "__none__":
            chats = []
        elif raw:
            chats = str(raw).split(",")
        return await self.plugin.stats_store.snapshot(period, chats)

    async def save(self):
        from astrbot.api.web import request
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        section = str(payload.get("section") or "")
        data = payload.get("data")
        if section not in SECTIONS or not isinstance(data, dict):
            return _error("未知配置分区或数据格式错误")
        base_revision = payload.get("base_revision")
        if base_revision is not None:
            try:
                base_revision = int(base_revision)
            except (TypeError, ValueError):
                return _error("base_revision 格式错误")
        if self._save_lock:
            return _error("配置正在保存，请稍后重试")
        # 身份映射切换、计数迁移与请求的身份解析/限流检查必须共享同一把锁。
        # 只锁 merge_usage() 不够：_apply_runtime() 会让新映射先对请求可见。
        async with self.plugin.identity_rate_lock:
            if self._save_lock:
                return _error("配置正在保存，请稍后重试")
            if base_revision is not None and base_revision != self._revision:
                # 另一页面已基于更新的状态写入；拒绝这次过期保存，避免旧草稿静默覆盖。
                return {
                    "saved": False,
                    "conflict": True,
                    "revision": self._revision,
                    "message": "配置已被其他页面更新，已阻止本次保存以避免覆盖，请刷新后再保存（当前草稿已保留）。",
                }
            recovered, message = await self.ensure_identity_recovered()
            if not recovered:
                return _error(message)
            self._save_lock = True
            try:
                result = await self._save_locked(section, data)
            finally:
                self._save_lock = False
        if not result.get("saved"):
            return result
        self._revision += 1
        result["revision"] = self._revision
        try:
            result["state"] = await self.state()
        except Exception:
            logger.exception("[小作文生成器] 保存成功但读取配置状态失败")
            result["state"] = None
        return result

    async def _save_locked(self, section: str, data: dict):
        old_config = copy.deepcopy(dict(self.plugin.config))
        self._pending_merge = None
        self._pending_split = None
        journal = None
        counts_done = False
        try:
            updates = await self._build_updates(section, data)
            journal = self._build_identity_journal(updates)
            if journal is not None:
                # 新身份映射与迁移日志必须一次性落盘，不能出现“映射
                # 已生效但没有任何恢复记录”的中间态。KV 迁移随后执行。
                updates[IDENTITY_MIGRATION_KEY] = journal
            await self._persist(updates)
            if journal is None:
                await self._apply_runtime(section, data)
            else:
                await self._run_pending()
                counts_done = True
                await self._apply_runtime(section, data)
                await self._clear_identity_journal()
        except KVRecoveryIncomplete:
            # KV 快照未能完整恢复；不能移除已持久化的迁移日志。
            # 重放会根据当前 KV 状态完成迁移，完成前请求保持安全关闭。
            logger.exception("[小作文生成器] 限流计数回滚未完成")
            return await self._identity_post_commit_error()
        except ValueError as exc:
            if counts_done:
                return await self._identity_post_commit_error()
            rolled_back = await self._rollback_config(
                old_config, keep_journal=journal is not None
            )
            if not rolled_back:
                return _error(
                    "保存失败且回滚未完成；身份迁移将在恢复后重放，"
                    "恢复完成前普通请求会被拒绝"
                )
            return _error(str(exc))
        except AIClientError as exc:
            if counts_done:
                return await self._identity_post_commit_error()
            rolled_back = await self._rollback_config(
                old_config, keep_journal=journal is not None
            )
            if not rolled_back:
                return _error(
                    "保存失败且回滚未完成；身份迁移将在恢复后重放，"
                    "恢复完成前普通请求会被拒绝"
                )
            return _error(exc.user_message.removeprefix("❌ "))
        except Exception:
            logger.exception("[小作文生成器] 保存配置中心失败")
            if counts_done:
                return await self._identity_post_commit_error()
            rolled_back = await self._rollback_config(
                old_config, keep_journal=journal is not None
            )
            if not rolled_back:
                return _error(
                    "保存失败且回滚未完成；身份迁移将在恢复后重放，"
                    "恢复完成前普通请求会被拒绝"
                )
            return _error("保存失败，请查看 AstrBot 日志")
        finally:
            self._pending_merge = None
            self._pending_split = None
        return {"saved": True, "section": section}

    async def _identity_post_commit_error(self):
        """计数已提交后的失败：不能回滚映射，否则 KV 与授权会错位。

        保留持久化日志，下一次请求/保存前由 ensure_identity_recovered()
        幂等重放并清理；恢复完成前普通请求会被拒绝。
        """
        self._recovery_error = "身份计数迁移尚未完成"
        try:
            await self._apply_runtime("identity", {})
        except Exception:
            logger.exception("[小作文生成器] 身份迁移后重建运行时失败")
            try:
                self.plugin.config_center = ConfigCenter(self.plugin)
            except Exception:
                logger.exception("[小作文生成器] 身份迁移后重建配置中心失败")
        return _error(
            "⚠️ 身份变更已保存，但计数迁移日志尚未清理；"
            "恢复完成前普通请求会被暂时拒绝，请稍后重试或查看 AstrBot 日志"
        )

    def _build_identity_journal(self, updates: dict) -> dict | None:
        """把本次身份映射变更记录为可重放的 write-ahead journal。"""
        if self._pending_merge:
            old_keys, canonical = self._pending_merge
            return {
                "version": IDENTITY_MIGRATION_VERSION,
                "kind": "merge",
                "old_keys": list(dict.fromkeys(str(key) for key in old_keys)),
                "canonical": str(canonical),
                "target_keys": [],
                "identity_links": copy.deepcopy(
                    updates["identity_links"]
                    if "identity_links" in updates
                    else self.plugin.config.get("identity_links", [])
                ),
                "created_at": time.time(),
            }
        if self._pending_split:
            canonical, targets = self._pending_split
            return {
                "version": IDENTITY_MIGRATION_VERSION,
                "kind": "split",
                "old_keys": [],
                "canonical": str(canonical),
                "target_keys": list(
                    dict.fromkeys(str(key) for key in targets)
                ),
                "identity_links": copy.deepcopy(
                    updates["identity_links"]
                    if "identity_links" in updates
                    else self.plugin.config.get("identity_links", [])
                ),
                "created_at": time.time(),
            }
        return None

    def _identity_journal(self) -> dict:
        raw = self.plugin.config.get(IDENTITY_MIGRATION_KEY)
        return raw if isinstance(raw, dict) else {}

    def _journal_error(self, journal: dict) -> str:
        if not journal:
            return ""
        if journal.get("version") != IDENTITY_MIGRATION_VERSION:
            return "身份迁移日志版本无效"
        kind = str(journal.get("kind") or "")
        if kind not in {"merge", "split"}:
            return "身份迁移日志类型无效"
        canonical = str(journal.get("canonical") or "")
        if not canonical:
            return "身份迁移日志目标身份为空"
        if kind == "merge":
            old_keys = journal.get("old_keys")
            if not isinstance(old_keys, list) or not old_keys:
                return "身份迁移日志旧身份为空"
            if not all(isinstance(key, str) and key for key in old_keys):
                return "身份迁移日志旧身份无效"
        else:
            targets = journal.get("target_keys")
            if not isinstance(targets, list) or not targets:
                return "身份迁移日志拆分目标为空"
            if not all(isinstance(key, str) and key for key in targets):
                return "身份迁移日志拆分目标无效"
        links = journal.get("identity_links")
        if not isinstance(links, list):
            return "身份迁移日志映射无效"
        for item in links:
            if not isinstance(item, dict):
                return "身份迁移日志映射条目无效"
            if not str(item.get("connector") or "").strip():
                return "身份迁移日志映射连接器为空"
            uids = item.get("uids")
            if not isinstance(uids, list) or not all(
                isinstance(uid, str) and uid for uid in uids
            ):
                return "身份迁移日志映射 UID 无效"
        return ""

    async def ensure_identity_recovered(self) -> tuple[bool, str]:
        """重放未完成的身份计数迁移；调用方必须持有 identity_rate_lock。"""
        journal = self._identity_journal()
        if not journal:
            self._recovery_error = ""
            return True, ""
        error = self._journal_error(journal)
        if error:
            self._recovery_error = error
            logger.error("[小作文生成器] %s", error)
            return False, (
                "⚠️ 身份关联计数迁移日志无效，已暂停普通请求；"
                "请管理员检查插件配置中的 identity_migration"
            )
        try:
            await self._replay_identity_journal(journal)
        except Exception:
            self._recovery_error = "身份计数迁移恢复失败"
            logger.exception("[小作文生成器] 身份计数迁移恢复失败")
            return False, (
                "⚠️ 身份关联计数尚未恢复完成，已暂停本次普通请求；"
                "请稍后重试或查看 AstrBot 日志"
            )
        self._recovery_error = ""
        return True, ""

    async def _replay_identity_journal(self, journal: dict) -> None:
        """幂等重放 merge/split，并一次性提交映射、清除日志。"""
        kind = str(journal.get("kind") or "")
        canonical = str(journal.get("canonical") or "")
        if kind == "merge":
            await self.plugin.rate_limiter.merge_usage(
                [str(key) for key in journal.get("old_keys") or []],
                canonical,
                self.plugin,
            )
        elif kind == "split":
            await self.plugin.rate_limiter.split_usage(
                canonical,
                [str(key) for key in journal.get("target_keys") or []],
                self.plugin,
            )
        else:
            raise ValueError("未知身份迁移类型")
        self.plugin.config["identity_links"] = copy.deepcopy(
            journal.get("identity_links") or []
        )
        # 先清内存再落盘；落盘失败则恢复内存日志，保证同一进程内还能重试。
        self.plugin.config[IDENTITY_MIGRATION_KEY] = {}
        try:
            await self._save_config()
        except Exception:
            self.plugin.config[IDENTITY_MIGRATION_KEY] = journal
            raise
        self.plugin.config_center = ConfigCenter(self.plugin)

    async def _run_pending(self) -> None:
        if self._pending_merge:
            old_keys, canonical = self._pending_merge
            await self.plugin.rate_limiter.merge_usage(
                old_keys, canonical, self.plugin
            )
        if self._pending_split:
            canonical, targets = self._pending_split
            await self.plugin.rate_limiter.split_usage(
                canonical, targets, self.plugin
            )

    async def _save_config(self) -> None:
        saver = getattr(self.plugin.config, "save_config_async", None)
        if saver is not None:
            saved = await saver()
            if saved is False:
                raise RuntimeError("配置持久化未提交")
            return
        sync = getattr(self.plugin.config, "save_config", None)
        if sync is not None:
            sync()

    async def _clear_identity_journal(self) -> None:
        journal = copy.deepcopy(self._identity_journal())
        if not journal:
            return
        self.plugin.config[IDENTITY_MIGRATION_KEY] = {}
        try:
            await self._save_config()
        except Exception:
            # 计数和映射已提交；保留日志只会让下次启动幂等重放，
            # 但绝不能静默假装清理成功，否则当前进程无法再重试。
            self.plugin.config[IDENTITY_MIGRATION_KEY] = journal
            logger.exception("[小作文生成器] 清除身份迁移日志失败")
            raise

    async def _rollback_config(
        self, old_config: dict, keep_journal: bool = False
    ) -> bool:
        """保存/计数迁移失败时恢复配置、运行时中心，保持三者一致。

        回滚落盘失败时返回 False。身份保存的调用方会保留 write-ahead
        journal，让重启/下一次请求继续恢复，而不是让旧映射与已迁移 KV
        长期并存。
        """
        if not isinstance(old_config, dict):
            return False
        current_config = copy.deepcopy(dict(self.plugin.config))
        self.plugin.config.clear()
        self.plugin.config.update(old_config)
        try:
            await self._save_config()
        except Exception:
            logger.exception("[小作文生成器] 回滚配置持久化失败")
            if keep_journal and current_config.get(IDENTITY_MIGRATION_KEY):
                # 磁盘可能仍是“新映射 + 日志”。恢复同样的内存状态，
                # 让请求前的 ensure_identity_recovered() 完成或回滚。
                self.plugin.config.clear()
                self.plugin.config.update(current_config)
            self.plugin.config_center = ConfigCenter(self.plugin)
            return False
        self.plugin.config_center = ConfigCenter(self.plugin)
        return True

    async def _build_updates(self, section: str, data: dict) -> dict:
        if section == "model":
            return {"ai_config": self.plugin.model_config_api._merge_saved(data)}
        if section == "search":
            return {"search_config": self._search_config(data)}
        if section == "rate":
            return {"rate_limit": self._rate_config(data)}
        if section == "sessions":
            return self._session_config(data)
        if section == "admins":
            admins = self._admin_users(data)
            active_uids = {item["uid"] for item in admins}
            if data.get("clear_legacy_admin_pending") is True:
                # 用户明确不再需要这些旧 UID 的权限：直接丢弃待迁移条目，
                # 不要求先添加一个管理员再删除。
                pending = []
            else:
                pending = [
                    item for item in self.plugin.config.get("legacy_admin_pending", [])
                    if isinstance(item, dict)
                    and str(item.get("uid") or "") not in active_uids
                ]
            return {
                "admin_users": admins,
                "admin_qqs": [],
                "legacy_admin_pending": pending,
            }
        if section == "advanced":
            games = data.get("preset_games", [])
            if not isinstance(games, list):
                raise ValueError("预设游戏必须是列表")
            return {
                "preset_games": [str(x).strip() for x in games if str(x).strip()],
                "show_generation_stats": bool(data.get("show_generation_stats", True)),
            }
        if section == "identity":
            return await self._identity_update(data)
        raise ValueError("未知配置分区")

    def _search_config(self, data: dict) -> dict:
        old = self.plugin.config.get("search_config", {}) or {}
        enabled = search_enabled(data.get("enabled", old.get("enabled", True)))
        explicit_list = ("mode_order" in data) or ("mode_enabled" in data)
        if explicit_list:
            # 配置中心提交的列表是唯一事实来源：保留顺序与勾选，不按
            # 单值 mode 反向重排，避免重启或旧字段把列表洗回去。
            order = _search_group_order(
                data.get("mode_order", old.get("mode_order"))
            )
            enabled_groups = _search_group_enabled(
                data.get("mode_enabled", old.get("mode_enabled"))
            )
            mode_enabled = [key for key in order if key in enabled_groups]
        elif isinstance(old.get("mode_order"), list) and old.get("mode_order"):
            # 未提交列表（例如只保存结果数等普通参数）时绝不能丢原列表，
            # 也不能按单值 mode 重新生成，否则顺序与勾选会被洗掉。
            order = _search_group_order(old.get("mode_order"))
            enabled_groups = _search_group_enabled(
                old.get("mode_enabled"), strict=False
            )
            mode_enabled = [key for key in order if key in enabled_groups]
        else:
            # 旧配置尚无列表：按可见 mode 生成一次三组策略。
            requested_mode = str(
                data.get("mode", old.get("mode", "auto"))
            ).strip().lower()
            if requested_mode not in MODES:
                raise ValueError("搜索模式无效")
            groups = resolve_search_groups(
                enabled=enabled, mode=requested_mode,
            )
            order = list(SEARCH_GROUPS)
            mode_enabled = [key for key in order if key in groups]
        mode = mode_enabled[0] if mode_enabled else "off"
        cfg = dict(old)
        cfg.update({
            "enabled": enabled,
            "mode": mode,
            "mode_order": order,
            "mode_enabled": mode_enabled,
            "mode_prefs_version": SEARCH_MODE_PREFS_VERSION,
            # 独立降级开关已移除：具体顺序与勾选完全由列表表达。
            "fallback_enabled": False,
            "result_count": _int_value(data, "result_count", 1, 10),
            "timeout_ms": _int_value(data, "timeout_ms", 1000, 60000),
            "per_backend_timeout_ms": _int_value(data, "per_backend_timeout_ms", 0, 60000),
            "summary_input_max_chars": _int_value(data, "summary_input_max_chars", 200, 50000),
            "summarize_enabled": bool(data.get("summarize_enabled", old.get("summarize_enabled", False))),
            "summarize_max_chars": _int_value(data, "summarize_max_chars", 20, 1000),
            "material_max_chars": _int_with_default(
                data, old, "material_max_chars", 3000, 1000, 6000
            ),
            "material_max_items": _int_with_default(
                data, old, "material_max_items", 6, 1, 10
            ),
            "material_snippet_chars": _int_with_default(
                data, old, "material_snippet_chars", 400, 120, 600
            ),
            "material_prefs_version": SEARCH_MATERIAL_PREFS_VERSION,
            "api_provider": str(data.get("api_provider", old.get("api_provider", "tavily"))).lower(),
            "api_base_url": str(data.get("api_base_url", old.get("api_base_url", "https://api.tavily.com"))).strip(),
        })
        if cfg["api_provider"] not in {"tavily"}:
            raise ValueError("目前只支持 Tavily 搜索 API")
        if not cfg["api_base_url"].startswith(("http://", "https://")):
            raise ValueError("搜索 API 地址无效")
        raw_key = data.get("api_key")
        old_key = str(old.get("api_key") or "")
        # 只能信任显式记录的原绑定地址。旧版 api_base_url 不是绑定来源：
        # 原生配置页可以单独改它，若把它当成绑定地址，保存时就会把旧 Key
        # 静默改绑到当前地址。
        old_origin = str(old.get("api_key_origin") or "").strip()
        same_origin = bool(old_origin) and _norm_base(
            old_origin
        ) == _norm_base(cfg["api_base_url"])
        if data.get("clear_api_key"):
            cfg["api_key"] = ""
            cfg["api_key_origin"] = ""
        elif isinstance(raw_key, str) and raw_key.strip() and not raw_key.startswith("•"):
            cfg["api_key"] = raw_key.strip()
            cfg["api_key_origin"] = cfg["api_base_url"]
        elif old_key and same_origin:
            cfg["api_key"] = old_key
            cfg["api_key_origin"] = cfg["api_base_url"]
        elif old_key and not old_origin:
            # 已有 Key 却没有绑定地址：无法确认它属于哪个服务地址，不能
            # 把当前地址当成来源静默补记。要求重新输入或明确清除。
            raise ValueError(
                "原搜索 API Key 缺少绑定地址：请重新输入 API Key，"
                "或勾选“清除搜索 API Key”后再保存"
            )
        elif old_key:
            # 地址已变更但没有提供新 Key，也没有明确清除：不能静默保存一个
            # “看起来已配置、实际不可用”的旧 Key。强制用户重新输入或明确
            # 清除，避免把旧凭据留在配置里造成误解和后续误复用。
            raise ValueError(
                "搜索服务地址已变更：请重新输入 API Key，或勾选“清除搜索 API Key”后再保存"
            )
        else:
            cfg["api_key"] = ""
            cfg["api_key_origin"] = ""
        return cfg

    def _rate_config(self, data: dict) -> dict:
        window = _int_value(data, "window_minutes", 0, 1440)
        max_requests = _int_value(data, "max_requests_per_window", 1, 1000)
        daily = _int_value(data, "daily_max", 0, 1000)
        tz = str(data.get("reset_timezone") or "").strip()
        if tz and tz.lower() not in {"local", "system"}:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("每日重置时区无效") from exc
        return {
            "window_minutes": window,
            "max_requests_per_window": max_requests,
            "daily_max": daily,
            "reset_timezone": tz,
        }

    def _session_config(self, data: dict) -> dict:
        old = self.plugin.config.get("session_settings", {})
        if not isinstance(old, dict):
            old = {}
        if "sessions" not in data:
            # 普通开关保存（私聊/群聊总开关）只改放行策略，必须原样保留
            # 会话列表：包括 null（尚未配置）、非数组和含无效条目的原值。
            # 否则前端草稿里的安全占位值会把“未配置/配置错误”洗成
            # 明确空列表，下一次保存就静默放行普通会话。
            # 只有显式提交 sessions 键（添加/移除/勾选/清空按钮）才允许
            # 改变会话列表本身。
            return {
                "session_settings": {
                    "allow_private": bool(
                        data.get("allow_private", old.get("allow_private", True))
                    ),
                    "allow_group": bool(
                        data.get("allow_group", old.get("allow_group", True))
                    ),
                    "sessions": copy.deepcopy(old.get("sessions")),
                },
            }
        sessions = data.get("sessions")
        if sessions is None:
            raise ValueError(
                "会话列表尚未配置：请添加 UMO 会话，或明确保存空列表（不限制）"
            )
        if not isinstance(sessions, list):
            raise ValueError("会话列表类型错误（应为数组），请修正后重试")
        normalized = []
        seen = set()
        for item in sessions:
            reason = session_entry_error(item)
            if reason == "entry_not_object":
                raise ValueError("会话条目格式错误（应为对象）")
            if reason == "umo_missing":
                raise ValueError("会话 UMO 缺失")
            if reason == "umo_malformed":
                raise ValueError(
                    "会话 UMO 无效（需要 platform:MessageType:session，完整会话 ID 不能为空）"
                )
            if reason == "umo_unknown_type":
                raise ValueError("UMO 必须包含平台和会话类型")
            umo = str(item.get("umo") or "").strip()
            if umo in seen:
                continue
            seen.add(umo)
            connector, chat_type = parse_umo(umo)
            normalized.append({
                "umo": umo,
                "connector": connector,
                "type": chat_type,
                "enabled": bool(item.get("enabled", True)),
            })
        legacy_pending = bool(
            self.plugin.config.get("legacy_restrictions_pending", False)
        ) or bool(self.plugin.config.get("legacy_umo_pending"))
        if legacy_pending and not normalized:
            raise ValueError(
                "检测到无法自动映射的旧 QQ 限制：请添加明确的 UMO 会话后再保存"
            )
        return {
            "session_settings": {
                "allow_private": bool(data.get("allow_private", True)),
                "allow_group": bool(data.get("allow_group", True)),
                "sessions": normalized,
            },
            "whitelist_umos": [item["umo"] for item in normalized if item["enabled"]],
            # 用户显式保存会话策略即视为完成旧限制/旧 UMO 迁移。
            "legacy_restrictions_pending": False,
            "legacy_umo_pending": [],
            "whitelist_groups": [],
            "whitelist_privates": [],
            "whitelist_mode": "",
        }

    def _admin_users(self, data: dict) -> list[dict]:
        items = data.get("admin_users", [])
        if not isinstance(items, list):
            raise ValueError("管理员列表必须是数组")
        result = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("管理员格式错误")
            connector = str(item.get("connector") or "").strip()
            uid = str(item.get("uid") or "").strip()
            if not connector:
                raise ValueError("管理员必须指定连接器；旧管理员请在会话页绑定连接器")
            if not uid or ":" in uid:
                raise ValueError("管理员 UID 无效，不能填写 UMO")
            key = (connector, uid)
            if key in seen:
                continue
            seen.add(key)
            result.append({"connector": connector, "uid": uid, "legacy": False})
        return result

    async def _identity_update(self, data: dict) -> dict:
        action = str(data.get("action") or "")
        center = self.plugin.config_center
        if action == "link":
            connector = str(data.get("connector") or "")
            requested = set(str(uid) for uid in (data.get("uids") or []))
            old_link_ids = [
                item.get("id") for item in center.identity_links
                if item.get("connector") == connector
                and requested & set(item.get("uids") or [])
            ]
            link = center.link_identities(connector, list(requested))
            old_keys = [
                f"{link['connector']}:{uid}" for uid in link["uids"]
            ] + [f"link:{link_id}" for link_id in old_link_ids]
            # 计数迁移放到配置保存成功之后，避免保存失败时计数已改变。
            self._pending_merge = (old_keys, f"link:{link['id']}")
            return {"identity_links": center.identity_links}
        if action == "unlink":
            link_id = str(data.get("id") or "")
            link = next(
                (item for item in center.identity_links
                 if item.get("id") == link_id),
                None,
            )
            if link:
                self._pending_split = (
                    f"link:{link_id}",
                    [
                        f"{link.get('connector')}:{uid}"
                        for uid in link.get("uids", [])
                    ],
                )
            center.unlink_identity(link_id)
            return {"identity_links": center.identity_links}
        raise ValueError("未知身份操作")

    async def _persist(self, updates: dict) -> None:
        old = copy.deepcopy(dict(self.plugin.config))
        self.plugin.config.update(updates)
        saved = True
        try:
            saver = getattr(self.plugin.config, "save_config_async", None)
            if saver is not None:
                saved = await saver()
            else:
                sync = getattr(self.plugin.config, "save_config", None)
                if sync is not None:
                    sync()
        except Exception:
            # 身份映射可能与日志一起已经落盘，失败结果不再确定。保留
            # 尝试写入的内存状态（含 journal），让回滚或请求前恢复决定
            # 最终状态；直接恢复 old 会丢失“磁盘可能已提交”的证据。
            if IDENTITY_MIGRATION_KEY not in self.plugin.config:
                self.plugin.config.clear()
                self.plugin.config.update(old)
            raise
        if saved is False:
            # 明确的“未提交”语义：磁盘仍是 old，内存也必须回退。
            self.plugin.config.clear()
            self.plugin.config.update(old)
            raise RuntimeError("配置持久化未提交")

    async def _apply_runtime(self, section: str, data: dict) -> None:
        if section == "model":
            await self.plugin.model_service.reload_async()
            if self.plugin.ai_client is not self.plugin.model_service:
                self.plugin.ai_client = self.plugin.model_service
        elif section == "search":
            await self.plugin.reload_search_service()
        elif section == "rate":
            self.plugin.reload_rate_limiter()
        elif section in ("sessions", "admins", "identity"):
            self.plugin.config_center = ConfigCenter(self.plugin)
        elif section == "advanced":
            self.plugin.preset_games = list(
                self.plugin.config.get("preset_games", [])
            )
            self.plugin.show_generation_stats = bool(
                self.plugin.config.get("show_generation_stats", True)
            )

    async def model_models(self):
        from astrbot.api.web import request
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        if str(payload.get("source") or "") == MODEL_SOURCE_ASTRBOT:
            return _error("AstrBot 预设模型由 AstrBot 管理，无需远程模型列表")
        try:
            cfg = self.plugin.model_config_api._merge_draft(payload)
            models = await fetch_manual_models(
                cfg.get("base_url", ""),
                cfg.get("api_key", ""),
                api_format=cfg.get("api_format", "chat_completions"),
            )
            return {"models": models, "context_length": None}
        except AIClientError as exc:
            return _error(exc.user_message.removeprefix("❌ "))
        except ValueError as exc:
            return _error(str(exc))

    @staticmethod
    def _model_test_tokens(result: dict) -> tuple[dict | None, bool]:
        usage = result.get("token_usage") if isinstance(result, dict) else None
        if not isinstance(usage, dict) or result.get("token_available") is not True:
            return None, False
        try:
            return ({
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }, True)
        except (TypeError, ValueError):
            return None, False

    async def model_test(self):
        from astrbot.api.web import request
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        try:
            cfg = self.plugin.model_config_api._merge_draft(payload)
        except (ValueError, AIClientError) as exc:
            return _error(getattr(exc, "user_message", str(exc)).removeprefix("❌ "))
        service = ModelService(self.plugin.context, {"ai_config": cfg})
        started = time.monotonic()
        try:
            result = await service.test_connection(
                umo=str(payload.get("umo") or "")
            )
        except Exception as exc:
            return _error(
                getattr(exc, "user_message", str(exc)).removeprefix("❌ ")
            )
        finally:
            await service.close()
        # 配置页测试只使用明确传入的会话；不回退到“最后一次聊天”，
        # 避免在用户不知情时测试另一个会话的默认模型。
        test_umo = str(payload.get("umo") or "")
        token_usage, token_available = self._model_test_tokens(result)
        try:
            await self.plugin.stats_store.record({
                "kind": "model",
                "status": "success" if result.get("success") else "failed",
                "model_kind": "test",
                "chat": self.plugin._chat_meta(test_umo) if test_umo else None,
                "model_source": cfg.get("source", "manual"),
                "model": result.get("model", ""),
                "token_usage": token_usage,
                "token_available": token_available,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_category": "",
            })
        except Exception:
            logger.exception("[小作文生成器] 记录主动测试失败")
        if not result.get("success"):
            return _error(result.get("message") or "模型测试失败")
        return result

    async def search_test(self):
        from astrbot.api.web import request
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return _error("请求格式错误")
        game = str(payload.get("game") or "原神").strip()
        if not game:
            return _error("游戏名不能为空")
        # event 只用于调用需要会话上下文的后端；网页主动测试没有聊天
        # 归属，不能借用 last_umo 冒充“最近一次聊天发起的搜索”。
        event = self._event()
        result = await self.plugin.search_service.test_search(game, event=event)
        # 主动测试与正常生成共用同一套后端尝试记录口径：只有真正发起的
        # 后端尝试进入“实际搜索”，第二个及之后的尝试才计入回退。
        await self.plugin._record_search(
            result,
            "",
            request_id=f"search-test-{time.time_ns()}",
        )
        return result
