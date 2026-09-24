"""时间值统一按 UTC 存储；输入必须携带时区偏移。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

from app.core.errors import ValidationError

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def parse_instant(value: str, field: str = "时间") -> datetime:
    """解析带时区偏移的 ISO 8601 字符串并转换为 UTC；缺省偏移直接拒绝。"""
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field}不是合法的 ISO 8601 时间，例如 2026-09-24T12:00:00+08:00") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field}必须携带时区偏移，例如 2026-09-24T12:00:00+08:00")
    return parsed.astimezone(UTC)


def parse_sourced_instant(value: str, field: str = "时间") -> tuple[datetime, str]:
    """解析带偏移的时间，返回 (UTC 时间, 来源偏移文本，如 +08:00)。"""
    utc_value = parse_instant(value, field)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    delta = datetime.fromisoformat(text).utcoffset() or timedelta(0)
    total = abs(int(delta.total_seconds()))
    sign = "+" if delta.total_seconds() >= 0 else "-"
    return utc_value, f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def utc_iso(value: datetime, timespec: str = "seconds") -> str:
    """统一输出带显式 +00:00 偏移的 UTC 字符串。"""
    return value.astimezone(UTC).isoformat(timespec=timespec)


def utc_stamp(value: datetime) -> str:
    """系统时间列（生效/审批/as_of 锚点）使用微秒精度，避免同秒排序平局。"""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_utc_offset(value: str) -> timezone:
    match = _OFFSET_RE.match(value.strip())
    if not match:
        raise ValidationError("时区偏移格式应为 ±HH:MM，例如 +08:00")
    sign, hours_text, minutes_text = match.groups()
    hours, minutes = int(hours_text), int(minutes_text)
    if hours > 23 or minutes >= 60 or (hours == 23 and minutes > 0):
        raise ValidationError("时区偏移超出合法范围")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if sign == "+" else -delta)


def day_bounds(date_text: str, offset_text: str) -> tuple[datetime, datetime, timezone]:
    """按给定 UTC 偏移定义本地自然日，返回对应的 UTC 半开区间 [start, end)。"""
    try:
        day = date.fromisoformat(date_text)
    except ValueError as exc:
        raise ValidationError("日期格式应为 YYYY-MM-DD") from exc
    if len(date_text) != 10:
        raise ValidationError("日期格式应为 YYYY-MM-DD")
    zone = parse_utc_offset(offset_text)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    start_utc = start_local.astimezone(UTC)
    return start_utc, start_utc + timedelta(days=1), zone
