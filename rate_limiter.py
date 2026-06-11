"""
频率限制模块
基于 QQ 号的全局频率限制（不区分群聊/私聊）
支持滑动窗口和每日上限，管理员不受限制
"""

import time
from datetime import datetime


# KV 存储键前缀
RATE_LIMIT_PREFIX = "rate_limit:"


def _get_today_str() -> str:
    """获取今日日期字符串 YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


def _get_current_ts() -> float:
    """获取当前时间戳"""
    return time.time()


class RateLimiter:
    """频率限制器"""

    def __init__(self, window_minutes: int = 10, max_requests: int = 3, daily_max: int = 20):
        """
        Args:
            window_minutes: 滑动窗口时间（分钟）
            max_requests: 窗口内最大请求次数
            daily_max: 每日最大请求次数
        """
        self.window_seconds = window_minutes * 60
        self.max_requests = max_requests
        self.daily_max = daily_max
        self.window_minutes = window_minutes

    async def check_and_record(self, qq_id: str, kv_storage) -> dict:
        """
        检查频率限制并记录请求

        Args:
            qq_id: QQ 号
            kv_storage: AstrBot KV 存储对象

        Returns:
            {
                "allowed": bool,
                "message": str,  # 不允许时的提示信息
            }
        """
        key = f"{RATE_LIMIT_PREFIX}{qq_id}"
        now = _get_current_ts()
        today = _get_today_str()

        # 读取现有记录
        record = await kv_storage.get_kv_data(key, None)
        if record is None:
            record = {
                "window_start": now,
                "window_count": 0,
                "daily_count": 0,
                "daily_date": today,
            }
        else:
            # 兼容：record 可能是 str 或 dict
            if isinstance(record, str):
                import json
                record = json.loads(record)

        # 重置每日计数（如果跨天）
        if record.get("daily_date") != today:
            record["daily_count"] = 0
            record["daily_date"] = today

        # 检查每日上限
        if record["daily_count"] >= self.daily_max:
            return {
                "allowed": False,
                "message": (
                    f"⏳ 今日请求次数已用完（每日最多 {self.daily_max} 次）"
                ),
            }

        # 滑动窗口检查
        window_start = record.get("window_start", now)
        window_count = record.get("window_count", 0)

        if now - window_start >= self.window_seconds:
            # 窗口已过期，重置
            record["window_start"] = now
            record["window_count"] = 0
            window_count = 0

        if window_count >= self.max_requests:
            remaining_seconds = int(self.window_seconds - (now - window_start))
            remaining_minutes = remaining_seconds // 60
            return {
                "allowed": False,
                "message": (
                    f"⏳ 请求过于频繁，请稍后再试"
                    f"（每 {self.window_minutes} 分钟最多 {self.max_requests} 次，"
                    f"今日已用 {record['daily_count']}/{self.daily_max} 次）"
                ),
            }

        # 允许请求，更新记录
        record["window_count"] = window_count + 1
        record["daily_count"] = record["daily_count"] + 1

        # 保存记录
        import json
        await kv_storage.put_kv_data(key, json.dumps(record))

        return {"allowed": True, "message": ""}
