"""频率限制：按连接器作用域身份计数，支持身份关联合并。"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RATE_LIMIT_PREFIX = "rate_limit:"


class KVRecoveryIncomplete(RuntimeError):
    """KV 迁移和回滚均失败；调用方必须保留持久恢复日志。"""


def _zone(name: str):
    name = str(name or "").strip()
    if not name or name.lower() in {"local", "system"}:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone().tzinfo


def _get_today_str(timezone_name: str = "") -> str:
    return datetime.now(_zone(timezone_name)).strftime("%Y-%m-%d")


def _get_current_ts() -> float:
    return time.time()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class RateLimiter:
    def __init__(
        self,
        window_minutes: int = 10,
        max_requests: int = 3,
        daily_max: int = 20,
        timezone_name: str = "",
    ):
        self.window_minutes = max(0, int(window_minutes))
        self.window_seconds = self.window_minutes * 60
        self.max_requests = max(1, int(max_requests))
        self.daily_max = max(0, int(daily_max))
        self.timezone_name = str(timezone_name or "")
        self._lock = asyncio.Lock()

    async def _read(self, key, kv_storage, now=None):
        record = await kv_storage.get_kv_data(key, None)
        if now is None:
            now = _get_current_ts()
        if record is None:
            return {
                "window_start": now,
                "window_count": 0,
                "daily_count": 0,
                "daily_date": _get_today_str(self.timezone_name),
            }
        if isinstance(record, str):
            try:
                record = json.loads(record)
            except Exception:
                record = {}
        if not isinstance(record, dict):
            record = {}
        record.setdefault("window_start", now)
        record.setdefault("window_count", 0)
        record.setdefault("daily_count", 0)
        record.setdefault("daily_date", _get_today_str(self.timezone_name))
        record.setdefault("lineage", "")
        return record

    async def check_and_record(self, identity_key: str, kv_storage) -> dict:
        key = f"{RATE_LIMIT_PREFIX}{identity_key}"
        async with self._lock:
            now = _get_current_ts()
            today = _get_today_str(self.timezone_name)
            record = await self._read(key, kv_storage, now)
            self._ensure_parts(record, identity_key, now)
            self._recompute_totals(record, now, today)
            if self.daily_max > 0 and record["daily_count"] >= self.daily_max:
                return {
                    "allowed": False,
                    "message": f"⏳ 今日请求次数已用完（每日最多 {self.daily_max} 次）",
                }
            if self.window_minutes > 0 and record["window_count"] >= self.max_requests:
                remaining = max(
                    0,
                    int(
                        self.window_seconds
                        - (now - float(record.get("window_start", now)))
                    ),
                )
                return {
                    "allowed": False,
                    "message": (
                        "⏳ 请求过于频繁，请稍后再试"
                        f"（每 {self.window_minutes} 分钟最多 "
                        f"{self.max_requests} 次，今日已用 "
                        f"{record['daily_count']}/{self.daily_max} 次）"
                    ),
                }
            if record.get("parts_frozen"):
                # 继承来的 part 属于共同历史或更早的独立计量段，不能再被
                # 本次请求修改；新请求写入本记录专属的 part。
                part_id = f"{identity_key}#own"
            else:
                part_id = identity_key
            part = record["parts"].get(part_id)
            if not isinstance(part, dict):
                part = {
                    "window_start": now,
                    "window_count": 0,
                    "daily_count": 0,
                    "daily_date": today,
                }
            else:
                part = self._normalise_part(part, now)
            if part["daily_date"] != today:
                part["daily_count"] = 0
                part["daily_date"] = today
            if self.window_minutes > 0 and now - part["window_start"] >= self.window_seconds:
                part["window_start"] = now
                part["window_count"] = 0
            part["window_count"] += 1
            part["daily_count"] += 1
            record["parts"][part_id] = part
            self._recompute_totals(record, now, today)
            await kv_storage.put_kv_data(key, json.dumps(record))
            return {"allowed": True, "message": ""}

    async def get_usage_status(self, identity_key: str, kv_storage) -> dict:
        key = f"{RATE_LIMIT_PREFIX}{identity_key}"
        async with self._lock:
            now = _get_current_ts()
            today = _get_today_str(self.timezone_name)
            record = await self._read(key, kv_storage, now)
            self._ensure_parts(record, identity_key, now)
            self._recompute_totals(record, now, today)
            daily_used = int(record.get("daily_count", 0))
            window_used = (
                daily_used if self.window_minutes <= 0
                else int(record.get("window_count", 0))
            )
        return {
            "window_used": window_used,
            "window_max": self.max_requests if self.window_minutes > 0 else 0,
            "window_minutes": self.window_minutes,
            "daily_used": daily_used,
            "daily_max": self.daily_max,
        }

    @staticmethod
    async def _delete(kv_storage, key: str) -> None:
        deleter = getattr(kv_storage, "delete_kv_data", None)
        if deleter is not None:
            await deleter(key)

    async def _snapshot(self, kv_storage, keys: list[str]) -> dict:
        return {
            key: await kv_storage.get_kv_data(key, None)
            for key in dict.fromkeys(keys)
        }

    async def _restore(self, kv_storage, snapshot: dict) -> None:
        for key, value in snapshot.items():
            if value is None:
                await self._delete(kv_storage, key)
            else:
                await kv_storage.put_kv_data(
                    key, value if isinstance(value, str) else json.dumps(value)
                )

    def _active_window_count(self, record: dict, now: float) -> int:
        count = max(0, _to_int(record.get("window_count", 0)))
        if self.window_minutes <= 0:
            return count
        start = _to_float(record.get("window_start", now), now)
        if now - start >= self.window_seconds:
            return 0
        return count

    @staticmethod
    def _normalise_part(part: dict, now: float) -> dict:
        return {
            "window_start": _to_float(part.get("window_start"), now),
            "window_count": max(0, _to_int(part.get("window_count", 0))),
            "daily_count": max(0, _to_int(part.get("daily_count", 0))),
            "daily_date": str(part.get("daily_date") or ""),
        }

    @staticmethod
    def _drop_legacy_base(record: dict) -> None:
        for field in (
            "lineage_base_start",
            "lineage_base_window",
            "lineage_base_daily",
            "lineage_base_date",
        ):
            record.pop(field, None)

    def _ensure_parts(self, record: dict, identity: str, now: float) -> dict:
        """把旧记录转换为“历史组成”字典，供合并时按来源去重。

        每个 part 代表一段彼此不重叠的实际请求。split_usage() 会把同一份
        part 复制给多个身份，因此再次关联时同一个 part 只计一次；解除后
        请求写入各自身份的 part，再次关联时会相加。
        """
        raw = record.get("parts")
        if isinstance(raw, dict) and raw:
            parts = {}
            for part_id, item in raw.items():
                if isinstance(item, dict):
                    parts[str(part_id)] = self._normalise_part(item, now)
            if parts:
                record["parts"] = parts
                self._drop_legacy_base(record)
                return parts

        parts = {}
        identity = str(identity)
        lineage = str(record.get("lineage") or "")
        current_start = _to_float(record.get("window_start"), now)
        current_window = max(0, _to_int(record.get("window_count", 0)))
        current_daily = max(0, _to_int(record.get("daily_count", 0)))
        current_date = str(record.get("daily_date") or "")
        shared_record = "lineage_base_window" in record or bool(lineage)
        if "lineage_base_window" in record:
            # dee94dd 的旧记录带 lineage_base_*；先还原成共享基线 + 本身份
            # 新增两部分，避免升级后第一次合并把历史重复或漏掉。
            base_start = _to_float(record.get("lineage_base_start"), 0.0)
            base_window = max(0, _to_int(record.get("lineage_base_window", 0)))
            base_daily = max(0, _to_int(record.get("lineage_base_daily", 0)))
            base_date = str(record.get("lineage_base_date") or "")
            base_id = f"base:{lineage or identity}"
            if base_window or base_daily:
                parts[base_id] = {
                    "window_start": base_start,
                    "window_count": base_window,
                    "daily_count": base_daily,
                    "daily_date": base_date,
                }
            if abs(current_start - base_start) < 1e-6:
                extra_window = max(0, current_window - base_window)
            else:
                # 拆分后窗口已经重置，当前窗口全部是解除关联后的新请求。
                extra_window = current_window
            extra_daily = (
                current_daily - base_daily
                if current_date == base_date else current_daily
            )
            extra_daily = max(0, extra_daily)
            if extra_window or extra_daily:
                parts[identity] = {
                    "window_start": current_start,
                    "window_count": extra_window,
                    "daily_count": extra_daily,
                    "daily_date": current_date,
                }
        elif current_window or current_daily:
            # 旧版 split 只写 lineage、不写 parts；同一 lineage 的副本代表
            # 同一份共同历史，必须共用一个 part 才能去重。该格式无法区分
            # 解除后各自新增的请求，升级时按共同历史保守取最大值。
            part_id = f"base:{lineage}" if lineage else identity
            parts[part_id] = {
                "window_start": current_start,
                "window_count": current_window,
                "daily_count": current_daily,
                "daily_date": current_date,
            }
        record["parts"] = parts
        if shared_record:
            record["parts_frozen"] = True
        self._drop_legacy_base(record)
        return parts

    def _recompute_totals(self, record: dict, now: float, today: str) -> None:
        parts = record.get("parts")
        if not isinstance(parts, dict):
            return
        window_count = 0
        daily_count = 0
        starts = []
        for part in parts.values():
            if not isinstance(part, dict):
                continue
            active = self._active_window_count(part, now)
            if active:
                window_count += active
                starts.append(_to_float(part.get("window_start"), now))
            if str(part.get("daily_date") or "") == today:
                daily_count += max(0, _to_int(part.get("daily_count", 0)))
        record["window_start"] = max(starts, default=now)
        record["window_count"] = window_count
        record["daily_count"] = daily_count
        record["daily_date"] = today

    def _union_parts(self, parts_list: list[dict], now: float, today: str) -> dict:
        merged = {}
        for parts in parts_list:
            if not isinstance(parts, dict):
                continue
            for part_id, part in parts.items():
                current = self._normalise_part(part, now)
                existing = merged.get(part_id)
                if existing is None:
                    existing = current
                    existing["daily_count"] = (
                        current["daily_count"]
                        if current["daily_date"] == today else 0
                    )
                    existing["daily_date"] = today
                    merged[part_id] = existing
                    continue
                current_active = self._active_window_count(current, now)
                existing_active = self._active_window_count(existing, now)
                if current_active > existing_active:
                    chosen = current
                    active = current_active
                elif current_active < existing_active:
                    chosen = existing
                    active = existing_active
                elif current["window_start"] >= existing["window_start"]:
                    chosen = current
                    active = current_active
                else:
                    chosen = existing
                    active = existing_active
                chosen = dict(chosen)
                chosen["window_count"] = active
                chosen["daily_count"] = max(
                    existing["daily_count"] if existing["daily_date"] == today else 0,
                    current["daily_count"] if current["daily_date"] == today else 0,
                )
                chosen["daily_date"] = today
                merged[part_id] = chosen
        return merged

    async def merge_usage(
        self, old_keys: list[str], canonical_key: str, kv_storage
    ):
        """关联身份时合并计数。

        每个持久记录保存若干互不重叠的 part。解除关联后的副本共享同一批
        part，再次关联时按 part id 去重；解除后各身份新增的请求使用各自
        identity 的 part，再次关联时会相加。写入或删除中途失败时恢复全部
        旧记录，避免计数半迁移。
        """
        async with self._lock:
            now = _get_current_ts()
            today = _get_today_str(self.timezone_name)
            old_unique = []
            seen = set()
            for identity in old_keys:
                key = f"{RATE_LIMIT_PREFIX}{identity}"
                if key in seen:
                    continue
                seen.add(key)
                old_unique.append(key)
            canonical_key_full = f"{RATE_LIMIT_PREFIX}{canonical_key}"
            if canonical_key_full not in seen:
                old_unique.append(canonical_key_full)
            snapshot = await self._snapshot(kv_storage, old_unique)
            parts_list = []
            for key in old_unique:
                record = await self._read(key, kv_storage, now)
                identity = key[len(RATE_LIMIT_PREFIX):]
                self._ensure_parts(record, identity, now)
                parts_list.append(record.get("parts", {}))
            merged_parts = self._union_parts(parts_list, now, today)
            merged = {
                "window_start": now,
                "window_count": 0,
                "daily_count": 0,
                "daily_date": today,
                "lineage": canonical_key,
                "parts": merged_parts,
                "parts_frozen": True,
            }
            self._recompute_totals(merged, now, today)
            try:
                await kv_storage.put_kv_data(
                    canonical_key_full, json.dumps(merged)
                )
                for key in old_unique:
                    if key != canonical_key_full:
                        await self._delete(kv_storage, key)
            except Exception:
                try:
                    await self._restore(kv_storage, snapshot)
                except Exception as restore_error:
                    raise KVRecoveryIncomplete("限流计数回滚未完成") from restore_error
                raise

    async def split_usage(
        self, canonical_key: str, target_keys: list[str], kv_storage
    ):
        """解除关联时把同一份历史计数复制给每个身份并删除关联记录。

        每个目标记录带相同的 parts；再次关联时同一 part 只取一次，不会因
        解除/重连重复放大额度，也不会把历史额度清零。写入中途失败时恢复
        原记录。
        """
        async with self._lock:
            now = _get_current_ts()
            today = _get_today_str(self.timezone_name)
            source_key = f"{RATE_LIMIT_PREFIX}{canonical_key}"
            targets = [
                f"{RATE_LIMIT_PREFIX}{target}" for target in target_keys
            ]
            snapshot = await self._snapshot(
                kv_storage, [source_key, *targets]
            )
            record = await self._read(source_key, kv_storage, now)
            self._ensure_parts(record, canonical_key, now)
            self._recompute_totals(record, now, today)
            if not record.get("window_count") and not record.get("daily_count"):
                await self._delete(kv_storage, source_key)
                return
            copied = copy.deepcopy(record)
            copied["lineage"] = canonical_key
            copied["parts_frozen"] = True
            self._drop_legacy_base(copied)
            try:
                for target in targets:
                    await kv_storage.put_kv_data(
                        target, json.dumps(copied)
                    )
                await self._delete(kv_storage, source_key)
            except Exception:
                try:
                    await self._restore(kv_storage, snapshot)
                except Exception as restore_error:
                    raise KVRecoveryIncomplete("限流计数回滚未完成") from restore_error
                raise
