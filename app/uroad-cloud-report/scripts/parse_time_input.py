#!/usr/bin/env python3
"""Parse compact Chinese uroad time expressions into complete Beijing times."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
_TIME = r"(?P<hour>\d{1,2})[:：点](?P<minute>\d{1,2})(?:(?:分)?(?P<second>\d{1,2})秒?)?"
_DURATION = r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>秒|分钟?|分|小时?|时)"

@dataclass(frozen=True)
class ParsedTimeRange:
    start: str
    end: str
    original: str
    received_at: str
    notice: str | None


def _duration_seconds(amount: str, unit: str) -> int:
    value = float(amount)
    if value <= 0:
        raise ValueError("持续时长必须大于 0")
    multiplier = 3600 if unit.startswith(("时", "小时")) else 60 if unit.startswith(("分", "分钟")) else 1
    seconds = int(value * multiplier)
    if seconds <= 0:
        raise ValueError("持续时长不能小于 1 秒")
    return seconds


def _clock(text: str) -> tuple[int, int, int]:
    match = re.search(_TIME, text)
    if not match:
        raise ValueError("未识别时刻，请使用 HH:MM 或 HH:MM:SS")
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    second = int(match.group("second") or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("时间范围不合理，请检查时、分、秒")
    return hour, minute, second


def _base_datetime(text: str, received: datetime) -> datetime:
    # Explicit date wins. Relative dates are resolved against message receipt time.
    day = received.date()
    if "昨天" in text:
        day -= timedelta(days=1)
    elif "前天" in text:
        day -= timedelta(days=2)
    explicit = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", text)
    if explicit:
        day = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
    h, m, s = _clock(text)
    return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m, second=s, tzinfo=BEIJING)


def parse_time_range(text: str, received_at: datetime | None = None) -> ParsedTimeRange:
    """Parse full dates or compact start+duration / before-after windows.

    received_at is the original message receipt time in Beijing time. The
    returned strings intentionally omit timezone to preserve existing API input.
    """
    received = (received_at or datetime.now(BEIJING)).astimezone(BEIJING)
    original = text.strip()
    # Full explicit range remains supported.
    full = re.search(r"(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*(?:到|至|~|～|-|—)\s*(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)", original)
    if full:
        def normalize(value: str) -> str:
            value = re.sub(r"[年月/]", "-", value).replace("年", "-").replace("月", "-").replace("日", "")
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S" if value.count(":") == 2 else "%Y-%m-%d %H:%M")
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        start, end = normalize(full.group(1)), normalize(full.group(2))
        if start >= end:
            raise ValueError("结束时间必须晚于开始时间")
        return ParsedTimeRange(start, end, original, received.isoformat(timespec="seconds"), None)

    base = _base_datetime(original, received)
    duration = re.search(_DURATION, original)
    before_after = re.search(r"(?:前后各|前后)\s*" + _DURATION, original)
    if before_after:
        seconds = _duration_seconds(before_after.group("amount"), before_after.group("unit"))
        start, end = base - timedelta(seconds=seconds), base + timedelta(seconds=seconds)
        note = f"未提供完整日期，按原消息收到时的北京时间（{received.strftime('%Y-%m-%d')}）解析；已按前后各 {before_after.group('amount')}{before_after.group('unit')} 查询。"
    elif duration and re.search(r"(?:开始|起始|起点).{0,8}(?:往后|之后|后查)|(?:往后|之后|后查)", original):
        seconds = _duration_seconds(duration.group("amount"), duration.group("unit"))
        start, end = base, base + timedelta(seconds=seconds)
        note = f"未提供完整日期，按原消息收到时的北京时间（{received.strftime('%Y-%m-%d')}）解析。"
    else:
        raise ValueError("请提供完整起止时间，或说明起始时刻+持续时长/事件时刻前后各多少时间")
    return ParsedTimeRange(start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), original, received.isoformat(timespec="seconds"), note)
