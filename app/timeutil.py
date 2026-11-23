"""UTC 时间工具：统一以秒精度、ISO-8601（Z 结尾）存储与输出。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc

#: 采样开始前要求计划参数已经持续生效的时长。
STABILIZATION = timedelta(minutes=5)


def to_utc(value: datetime) -> datetime:
    """校验并转换到 UTC，截断到秒。

    不接受无时区（naive）时间，避免把本地时间误当成 UTC。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区，例如 2026-11-23T09:00:00Z")
    return value.astimezone(UTC).replace(microsecond=0)


def parse_utc(text: str) -> datetime:
    """解析数据库中保存的 UTC 时间字符串。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
