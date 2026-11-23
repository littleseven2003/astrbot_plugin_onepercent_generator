"""
插件本地统计存储。

只记录请求/搜索/模型 Token 等可聚合元数据，不记录文章正文、消息内容或密钥。
文件使用原子替换写入；重启后保留。Token 缺失使用 null + token_available=False，
不能当作 0。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .search_service import MODES

PERIODS = {
    "5h": (5 * 3600, 5),
    "1d": (24 * 3600, 6),
    "7d": (7 * 24 * 3600, 7),
    "30d": (30 * 24 * 3600, 6),
    "all": (None, 6),
}


def _default_path() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import (
            get_astrbot_plugin_data_path,
        )
        base = Path(get_astrbot_plugin_data_path())
    except Exception:
        base = Path(__file__).resolve().parent / "data"
    return base / "astrbot_plugin_onepercent_generator" / "config_center_stats.json"


class StatsStore:
    def __init__(self, path=None, clock=None):
        self.path = Path(path) if path else _default_path()
        self.clock = clock or time.time
        self._lock = asyncio.Lock()
        self._data = {
            "version": 1,
            "started_at": self.clock(),
            "events": [],
        }
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        events = raw.get("events")
        started = raw.get("started_at")
        if isinstance(events, list):
            self._data["events"] = [
                item for item in events if isinstance(item, dict)
            ]
        if isinstance(started, (int, float)):
            self._data["started_at"] = float(started)
        if self._data["events"]:
            stamps = [
                float(item["ts"]) for item in self._data["events"]
                if isinstance(item.get("ts"), (int, float))
            ]
            if stamps:
                self._data["started_at"] = min(self._data["started_at"], min(stamps))

    async def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    async def record(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        safe = {k: v for k, v in event.items() if k != "content"}
        safe.setdefault("ts", self.clock())
        safe.setdefault("id", uuid.uuid4().hex)
        async with self._lock:
            if not self._data["events"]:
                self._data["started_at"] = float(safe["ts"])
            self._data["events"].append(safe)
            await self._write()

    async def events(self) -> list[dict]:
        async with self._lock:
            return [dict(item) for item in self._data["events"]]

    def _chats(self, events: list[dict]) -> list[dict]:
        seen = {}
        for event in events:
            chat = event.get("chat") if isinstance(event, dict) else None
            if not isinstance(chat, dict):
                continue
            key = str(chat.get("key") or "")
            if not key or key in seen:
                continue
            seen[key] = {
                "key": key,
                "connector": str(chat.get("connector") or "未知连接器"),
                "type": str(chat.get("type") or "unknown"),
                "label": str(chat.get("label") or key),
                "umo": str(chat.get("umo") or ""),
            }
        return sorted(seen.values(), key=lambda x: (x["connector"], x["type"], x["label"]))

    @staticmethod
    def _bucket_labels(start: float, end: float, count: int, period: str):
        span = max(1.0, end - start)
        labels = []
        for i in range(count):
            point = start + span * (i + 1) / count
            dt = datetime.fromtimestamp(point).astimezone()
            if period in ("5h", "1d"):
                labels.append(dt.strftime("%H:%M"))
            else:
                labels.append(dt.strftime("%m-%d"))
        return labels

    async def snapshot(self, period: str = "7d", chats=None) -> dict:
        if period not in PERIODS:
            period = "7d"
        selected = None
        if chats is not None:
            selected = set(str(x) for x in chats if str(x))
        async with self._lock:
            events = list(self._data["events"])
            started_at = float(self._data["started_at"])
        all_chats = self._chats(events)
        if selected is not None:
            events = [
                e for e in events
                if isinstance(e.get("chat"), dict)
                and str(e["chat"].get("key")) in selected
            ]
        now = float(self.clock())
        span, buckets = PERIODS[period]
        start = started_at if period == "all" or span is None else max(started_at, now - span)
        if start > now:
            start = now
        if period == "all" and not events:
            start = started_at
        in_range = [
            e for e in events
            if isinstance(e.get("ts"), (int, float)) and start <= float(e["ts"]) <= now
        ]
        rows = [
            {"requests": 0, "searches": 0, "tokens": 0,
             "tokens_missing": False}
            for _ in range(buckets)
        ]
        tokens_available_total = 0
        tokens_missing_total = False
        model_events = 0
        by_kind = {"generate": 0, "summarize": 0, "test": 0}
        requests = 0
        searches = 0
        searches_by_mode = {}
        failures = 0
        fallback_attempts = 0
        # 旧版本把整次请求的 degraded 复制到每个 attempt，且没有 fallback。
        # 有 request_id 时可以按请求分组，用“attempt 数 - 1”还原真实回退；
        # 没有 request_id 的旧记录无法可靠分组，只标记不完整，绝不猜测。
        legacy_fallback_groups = {}
        fallback_unknown_events = 0
        # 旧记录按 request_id 分组时必须看完整历史，而不能只看当前范围：
        # 首次尝试可能落在范围起点之前，范围内的每次尝试就都是回退。
        # degraded 仍只在至少一次尝试明确为 True 时参与推断。
        for event in events:
            if event.get("kind") != "search":
                continue
            if event.get("attempted") is not True:
                continue
            if event.get("fallback") is not None:
                continue
            request_id = str(event.get("request_id") or "").strip()
            if not request_id:
                continue
            ts = event.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            group = legacy_fallback_groups.setdefault(
                request_id,
                {"in_range": 0, "before_start": 0, "degraded": False},
            )
            if ts < start:
                group["before_start"] += 1
            elif ts <= now:
                group["in_range"] += 1
            if event.get("degraded") is True:
                group["degraded"] = True
        for event in in_range:
            kind = event.get("kind")
            if kind == "request":
                requests += 1
            elif kind == "search":
                if event.get("attempted") is True:
                    searches += 1
                    search_mode = str(event.get("search_mode") or "").strip()
                    mode_key = search_mode if search_mode in MODES else "unknown"
                    searches_by_mode[mode_key] = searches_by_mode.get(mode_key, 0) + 1
                    fallback = event.get("fallback")
                    if fallback is True:
                        fallback_attempts += 1
                    elif fallback is None:
                        request_id = str(event.get("request_id") or "").strip()
                        if not request_id:
                            # 旧记录若明确 degraded=False，可以确定没有回退；
                            # 只有 True/缺失才无法可靠判断。
                            if event.get("degraded") is not False:
                                fallback_unknown_events += 1
                        # 有 request_id 的旧记录已按完整历史统一分组，
                        # 这里不再重复累计。
            elif kind == "model":
                model_events += 1
                if event.get("status") == "failed":
                    failures += 1
                tokens = event.get("token_usage")
                model_kind = str(event.get("model_kind") or "generate")
                if model_kind not in by_kind:
                    model_kind = "generate"
                if isinstance(tokens, dict) and event.get("token_available") is True:
                    total = tokens.get("total_tokens")
                    if isinstance(total, int) and total >= 0:
                        tokens_available_total += total
                        by_kind[model_kind] += total
                    else:
                        tokens_missing_total = True
                else:
                    tokens_missing_total = True
            else:
                continue
            rel = (float(event["ts"]) - start) / max(1.0, now - start)
            idx = min(buckets - 1, max(0, int(rel * buckets)))
            if kind == "request":
                rows[idx]["requests"] += 1
            elif kind == "search":
                if event.get("attempted") is True:
                    rows[idx]["searches"] += 1
            elif kind == "model":
                tokens = event.get("token_usage")
                if isinstance(tokens, dict) and event.get("token_available") is True:
                    total = tokens.get("total_tokens")
                    if isinstance(total, int) and total >= 0:
                        rows[idx]["tokens"] += total
                    else:
                        rows[idx]["tokens_missing"] = True
                else:
                    rows[idx]["tokens_missing"] = True
        # 旧记录只有在同一 request_id 下能看清尝试先后时才可还原；degraded
        # 为整次请求标记。范围起点前已有尝试时，范围内的每次尝试都是回退；
        # 否则范围内的第 2 次起才是回退。若只看到一次尝试且前面没有记录，
        # 说明分组不完整（可能被保留策略截断），不能静默当成零回退。
        fallback_incomplete = fallback_unknown_events > 0
        for group in legacy_fallback_groups.values():
            if not group["degraded"] or group["in_range"] == 0:
                continue
            if group["before_start"] > 0:
                fallback_attempts += group["in_range"]
            elif group["in_range"] > 1:
                fallback_attempts += group["in_range"] - 1
            else:
                fallback_incomplete = True
        labels = self._bucket_labels(start, now, buckets, period)
        for row, label in zip(rows, labels):
            row["label"] = label
        token_status = (
            "unavailable" if model_events == 0
            else "unavailable" if tokens_available_total == 0 and tokens_missing_total
            else "incomplete" if tokens_missing_total
            else "complete"
        )
        last_records = [
            {
                "kind": e.get("kind"),
                "ts": e.get("ts"),
                "status": e.get("status", ""),
                "chat": e.get("chat", {}),
                "model_kind": e.get("model_kind", ""),
                "backend": e.get("backend", ""),
                "search_mode": e.get("search_mode", ""),
                "duration_ms": e.get("duration_ms", 0),
            }
            for e in in_range[-20:]
        ]
        return {
            "period": period,
            "started_at": started_at,
            "range_start": start,
            "range_end": now,
            "buckets": rows,
            "chats": all_chats,
            "selected_chats": sorted(selected) if selected is not None else None,
            "matched_events": len(in_range),
            "totals": {
                "requests": requests,
                "searches": searches,
                "searches_by_mode": searches_by_mode,
                "tokens": tokens_available_total,
                "token_status": token_status,
                "tokens_by_kind": by_kind,
                "failures": failures,
                "fallback_attempts": fallback_attempts,
                "fallback_incomplete": fallback_incomplete,
                "fallback_unknown_events": fallback_unknown_events,
            },
            "recent": list(reversed(last_records)),
            "retained_events": len(self._data["events"]),
        }
