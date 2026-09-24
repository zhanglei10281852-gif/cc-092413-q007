"""带时区偏移的观测时间解析。

所有时间值在存储与计算时统一为 UTC，原始 UTC 偏移量单独保留，
响应中同时给出 UTC 时间与上报时携带的偏移量。
"""
from __future__ import annotations

from datetime import datetime, timezone

_OFFSET_ALIAS = {"Z": "+00:00", "z": "+00:00"}


def parse_aware(value: str) -> datetime:
    """解析必须携带显式时区偏移的时间字符串，结果规范化为 UTC。"""
    return parse_aware_with_offset(value)[0]


def parse_aware_with_offset(value: str) -> tuple[datetime, int]:
    """解析带偏移时间，返回 (UTC 时间, 原始偏移分钟数)。"""
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + _OFFSET_ALIAS[normalized[-1]]
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须携带显式时区偏移，例如 2026-09-24T20:00:00+08:00")
    offset_minutes = int(parsed.utcoffset().total_seconds() // 60)
    return parsed.astimezone(timezone.utc), offset_minutes


def offset_minutes(value: str) -> int:
    return parse_aware_with_offset(value)[1]


def format_offset(offset_minutes: int) -> str:
    """分钟数 -> ISO 8601 偏移字符串（+08:00 / -05:30 / +00:00）。"""
    sign = "+" if offset_minutes >= 0 else "-"
    total = abs(offset_minutes)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"
