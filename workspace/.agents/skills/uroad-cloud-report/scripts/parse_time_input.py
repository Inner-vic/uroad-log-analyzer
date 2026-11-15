#!/usr/bin/env python3
"""Parse normalized Uroad time expressions into complete Beijing times."""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo


def _shared_input_module(module_name: str):
    """Load only the allowlisted sibling source, never a generic module cache."""
    if module_name not in ("natural_input", "semantic_input"):
        raise ValueError("自然语言输入规范化组件名称异常，未创建任务")
    source = (Path(__file__).resolve().parents[2] / "chassis-business-routing" / "scripts" / (module_name + ".py")).resolve()
    name = "_ccconnect_chassis_" + module_name
    existing = sys.modules.get(name)
    if existing is not None:
        existing_source = getattr(existing, "__file__", None)
        if existing_source is None or Path(existing_source).resolve() != source:
            raise ValueError("自然语言输入规范化组件路径异常，未创建任务")
        return existing
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ValueError("自然语言输入规范化组件不可用，未创建任务")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module

BEIJING = ZoneInfo("Asia/Shanghai")
_DATE = r"20\d{2}-\d{2}-\d{2}"
# Do not silently truncate HH:MM:SS or fractional seconds to HH:MM.
_TIME = r"(?<![\d:])(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?![\d:]|\.\d)"
_CLOCK = r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:]|\.\d)"
_OFFSET = r"(?:Z|[+-]\d{2}:?\d{2})"
_DURATION = r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>小时|分钟|秒|分|时)"

@dataclass(frozen=True)
class ParsedTimeRange:
    start: str
    end: str
    original: str
    received_at: str
    notice: str | None
    normalization: dict | None = None


def _duration_seconds(amount: str, unit: str) -> int:
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("持续时长格式不正确") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("持续时长必须大于 0")
    multiplier = 3600 if unit in ("小时", "时") else 60 if unit in ("分钟", "分") else 1
    seconds = value * multiplier
    if seconds != seconds.to_integral_value():
        raise ValueError("持续时长必须能换算为整秒，请补充明确的秒数")
    if seconds < 1:
        raise ValueError("持续时长不能小于 1 秒")
    return int(seconds)


def _clock(text: str) -> tuple[int, int, int]:
    match = re.search(_TIME, text)
    if not match:
        raise ValueError("未识别时刻，请使用 HH:MM 或 HH:MM:SS")
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    second = int(match.group("second") or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("时间范围不合理，请检查时、分、秒")
    return hour, minute, second


def _base_datetime(text: str, received: datetime) -> tuple[datetime, bool]:
    # Natural/relative date resolution belongs exclusively to natural_input.
    # Legacy clock+duration forms keep their documented receipt-day default.
    explicit = re.search(_DATE, text)
    day = datetime.strptime(explicit.group(0), "%Y-%m-%d").date() if explicit else received.date()
    h, m, s = _clock(text)
    return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m, second=s, tzinfo=BEIJING), explicit is None


def _change_notices(changes: list) -> list[str]:
    notices = []
    for change in changes:
        if isinstance(change, dict):
            source, target = change.get("original", ""), change.get("normalized", "")
            basis = change.get("basis", "")
            detail = f"“{source}”规范为“{target}”"
            notices.append(detail + (f"（{basis}）" if basis else ""))
        elif change:
            notices.append(str(change))
    return notices


def _result(start: datetime, end: datetime, original: str, received: datetime, notices: list[str], normalization: dict) -> ParsedTimeRange:
    if start.microsecond or end.microsecond:
        raise ValueError("起止时间必须精确到整秒")
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间；如跨日，请明确结束日期")
    return ParsedTimeRange(
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
        original,
        received.isoformat(),
        "；".join(notices) + "。" if notices else None,
        normalization,
    )


def parse_time_range(text: str, received_at: datetime | None = None, *, interpretation: dict | None = None) -> ParsedTimeRange:
    """Normalize natural dates/times, then apply Uroad range semantics.

    A timezone-free receipt timestamp means Beijing time, not the host zone.
    Clock-only start+duration/before-after expressions retain their receipt-day
    default; ordinary two-clock ranges require a date rather than guessing one.
    """
    original = text
    if interpretation is None:
        normalized = _shared_input_module("natural_input").normalize_query_input(text, received_at=received_at)
    else:
        normalized = _shared_input_module("semantic_input").apply_local_range_interpretation(text, interpretation, received_at=received_at)
    if normalized.get("needs_confirmation"):
        raise ValueError(normalized.get("message") or "时间信息不明确，请补充日期和时刻")
    value = normalized["text"]
    received = datetime.fromisoformat(normalized["received_at"])
    received = received.replace(tzinfo=BEIJING) if received.tzinfo is None else received.astimezone(BEIJING)
    notices = _change_notices(normalized.get("changes", []))
    audit = {"received_at": normalized["received_at"], "changes": normalized.get("changes", []), "method": "validated_interpretation" if interpretation is not None else "normalization"}
    if interpretation is not None:
        audit["interpretation"] = interpretation
    clocks = list(re.finditer(_CLOCK, value))
    full = re.search(
        rf"(?P<start>{_DATE}(?:T|\s+){_CLOCK}(?:{_OFFSET})?)"
        r"\s*(?:到|至|~|～|-|—|,|，)\s*"
        rf"(?P<end>(?:{_DATE}(?:T|\s+))?{_CLOCK}(?:{_OFFSET})?)(?![A-Za-z0-9:+-]|\.\d)",
        value,
    )
    if full:
        # A timezone offset is not a third clock. Only look for extra clocks
        # outside the two complete endpoints selected by the range grammar.
        if re.search(_CLOCK, value[:full.start()] + value[full.end():]):
            raise ValueError("识别到多个时刻，请明确本次查询的唯一开始和结束时间")
        start_text, end_text = full.group("start"), full.group("end")
        start_date = start_text[:10]
        if not re.match(_DATE, end_text):
            end_text = start_date + " " + end_text
            notices.append(f"结束时刻未重复日期，沿用开始日期 {start_date}；未自动推断跨日")
        def parse_complete(timestamp: str, endpoint: str) -> datetime:
            source = timestamp[:10] + "T" + timestamp[10:].lstrip(" T")
            offset = re.search(r"[+-](?P<hour>\d{2}):?(?P<minute>\d{2})$", source)
            if offset and (int(offset["hour"]) > 23 or int(offset["minute"]) > 59):
                raise ValueError("时区偏移无效，请确认小时和分钟")
            parsed = datetime.fromisoformat(source.replace("Z", "+00:00"))
            if parsed.microsecond:
                raise ValueError("起止时间必须精确到整秒")
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=BEIJING)
            beijing = parsed.astimezone(BEIJING)
            canonical = beijing.strftime("%Y-%m-%d %H:%M:%S")
            basis = f"{endpoint}时刻保留用户明确时区偏移 {parsed.utcoffset()}，换算为北京时间"
            audit["changes"].append({"kind": "timezone", "original": timestamp, "normalized": canonical, "basis": basis})
            notices.append(f"{endpoint}时刻“{timestamp}”按明确时区偏移换算为北京时间 {canonical}")
            return beijing
        start = parse_complete(start_text, "开始")
        end = parse_complete(end_text, "结束")
        return _result(start, end, original, received, notices, audit)

    if len(clocks) != 1:
        raise ValueError("请提供完整起止日期和时间，或说明一个起始时刻+持续时长/事件时刻前后各多少时间")
    if len(re.findall(_DATE, value)) > 1:
        raise ValueError("识别到多个日期，请明确本次查询使用的日期")
    base, receipt_day_default = _base_datetime(value, received)
    before_after_matches = list(re.finditer(r"(?:前后各|前后)\s*" + _DURATION, value))
    # Capture the duration after its operator, not an earlier date or clock.
    forward_matches = list(re.finditer(r"(?:往后(?:查)?|之后(?:查)?|后查|持续)\s*" + _DURATION, value))
    if len(before_after_matches) > 1 or len(forward_matches) > 1:
        raise ValueError("识别到多个持续范围，请明确本次要查询的唯一范围")
    before_after = before_after_matches[0] if before_after_matches else None
    forward = forward_matches[0] if forward_matches else None
    if before_after and forward:
        raise ValueError("同时提供了前后范围和持续范围，请明确本次要查询哪一种")
    duration_match = before_after or forward
    if duration_match and re.match(r"\s*\d+(?:\.\d+)?\s*(?:小时|分钟|秒|分|时)", value[duration_match.end():]):
        raise ValueError("请将持续时长换算为单一单位，例如 90 分钟，避免遗漏复合时长")
    if receipt_day_default:
        notices.append(f"未提供日期，沿用原有时长查询约定，按原消息收到时的北京时间当天 {received.strftime('%Y-%m-%d')} 解析")
    if before_after:
        seconds = _duration_seconds(before_after.group("amount"), before_after.group("unit"))
        try:
            start, end = base - timedelta(seconds=seconds), base + timedelta(seconds=seconds)
        except OverflowError as exc:
            raise ValueError("时间范围过大，请缩小持续时长") from exc
        notices.append(f"已按事件时刻前后各 {before_after.group('amount')}{before_after.group('unit')} 查询")
    elif forward:
        seconds = _duration_seconds(forward.group("amount"), forward.group("unit"))
        try:
            start, end = base, base + timedelta(seconds=seconds)
        except OverflowError as exc:
            raise ValueError("时间范围过大，请缩小持续时长") from exc
        notices.append(f"已从起始时刻向后查询 {forward.group('amount')}{forward.group('unit')}")
    else:
        raise ValueError("请提供完整起止时间，或说明起始时刻+持续时长/事件时刻前后各多少时间")
    return _result(start, end, original, received, notices, audit)
