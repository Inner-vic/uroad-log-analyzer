#!/usr/bin/env python3
"""Pure, conservative normalization of temporal text before business parsing.

This module does not select a business, infer a VIN, call a model, or perform I/O.
It turns only explicit temporal tokens into machine-readable dates and clocks.
Omitted date components use the receipt-time anchor, with an auditable basis;
ambiguous/invalid expressions are retained and require confirmation.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

BEIJING = timezone(timedelta(hours=8))
_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_HAN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_N = r"[0-9０-９零〇一二两三四五六七八九十]+"
_D = r"[0-9０-９]"
_YEAR = rf"(?:{_D}{{4}}|[零〇一二三四五六七八九]{{4}})"
_BOUNDARY = r"(?<![A-Za-z0-9０-９_/／.:：﹕．－-])"
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'，；。！？]+", re.I)
_ISO_AWARE = re.compile(
    r"(?<![A-Za-z0-9_])\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?"
    r"(?P<offset>[+-](?:\d{2}:\d{2}|\d{4}(?!-\d{1,2}-\d{1,2}(?:[T\s]|$)))|Z)(?!\d)"
)
_FULL_DATE = re.compile(
    _BOUNDARY + rf"(?P<year>{_YEAR})(?:年(?P<cm>{_N})月(?P<cd>{_N})(?![0-9０-９])[日号]?"
    rf"|(?P<sep>[-/.－／．])(?P<nm>{_D}{{1,2}})(?P=sep)(?P<nd>{_D}{{1,2}})(?![0-9０-９])[日号]?)"
)
_MONTH_DAY = re.compile(
    _BOUNDARY + rf"(?:(?P<cm>{_N})月(?P<cd>{_N})(?![0-9０-９])[日号]?"
    rf"|(?P<nm>{_D}{{1,2}})[/／－-](?P<nd>{_D}{{1,2}})(?![0-9０-９/／])[日号]?)"
)
_DAY_ONLY = re.compile(_BOUNDARY + rf"(?P<day>{_N})[日号](?![A-Za-z])")
_YEAR_MONTH = re.compile(rf"(?P<year>{_YEAR})年(?P<month>{_N})月")
_RELATIVE = re.compile(r"今天|今日|昨天|昨日|前天")
_PERIOD = r"凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚间|夜里|夜间"
_HAN_CLOCK = re.compile(
    rf"(?:(?P<period>{_PERIOD})\s*)?(?P<hour>{_N})\s*[点时]"
    rf"(?:(?P<special>半|一刻|三刻)|(?P<minute>{_N})\s*分?)?"
    rf"(?:(?P<second>{_N})\s*秒)?(?P<whole>整)?"
)
_COLON_CLOCK = re.compile(
    rf"(?<![A-Za-z0-9０-９_.．:+])(?:(?P<period>{_PERIOD})\s*)?"
    rf"(?P<hour>{_D}{{1,2}})\s*(?P<separator>[:：﹕．])\s*"
    rf"(?P<minute>{_D}{{1,2}})(?:\s*[:：﹕]\s*(?P<second>{_D}{{1,2}}))?"
    r"(?![0-9０-９:.])"
)
_VAGUE = re.compile(r"大概|大约|大致|差不多|左右(?!轮)|前后一点|某(?:天|日|时)|(?:早|上|下|晚)些时候")
_RANGE_LINK = re.compile(r"\s*(?:(?:开始|起|起始)\s*[,，]?\s*)?(?:一直\s*)?(?:到|至|[-~～—])\s*")
_UNSUPPORTED_DATE = re.compile(
    r"昨晚|昨夜|昨早|昨晨|前晚|前夜|今早|今晚|今夜|今晨|"
    r"大前天|明天|后天|去年|前年|明年|后年|上(?:个)?月|下(?:个)?月|"
    r"(?:上上|上|本|这|下下|下)周[一二三四五六日天]?|"
    r"(?:星期|礼拜|周)[一二三四五六日天]|月初|月底|周末"
)
_EXPLICIT_ZONE = re.compile(
    r"(?<![A-Za-z])(?:UTC|GMT)(?:\s*[+＋－-]\s*\d{1,2}(?::?\d{2})?)?(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:EST|EDT|PST|PDT|CST|CDT|JST|CET|CEST)(?![A-Za-z])|"
    r"(?:美东|美西|美国东部|美国西部|纽约|洛杉矶|伦敦|东京|日本|欧洲中部)(?:当地)?时间",
    re.I,
)
_ALTERNATIVE = re.compile(r"二选一|或者|或是|还是|或")
_STAMP = (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
          r"(?:Z|[+-](?:\d{2}:\d{2}|\d{4}))?")
_DATED_RANGE = re.compile(_STAMP + r"\s*(?:到|至|~|～|—|-|/|,|，)\s*" + _STAMP)
_COMPACT_LOCAL_RANGE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<date>20\d{2}-\d{2}-\d{2})[ \t]+"
    r"(?P<start>\d{2}:\d{2}(?::\d{2})?)-"
    r"(?P<end>\d{2}:\d{2}(?::\d{2})?)(?![A-Za-z0-9_:.+\-])")


def normalize_query_input(text: str, received_at: datetime | None = None) -> dict[str, Any]:
    """Normalize business ranges before scanning otherwise ambiguous offsets.

    Fully dated endpoint pairs and T timestamps keep their explicit zones.
    An unbound date + clock-clock query is a same-day local range; no rollover,
    identity, business trigger or missing date is inferred here.
    """
    protected = [m.span() for pattern in (_URL, _DATED_RANGE) for m in pattern.finditer(text)]
    changes = []
    def expand(match: re.Match[str]) -> str:
        if any(left < match.end() and match.start() < right for left, right in protected):
            return match.group(0)
        # A single zoned instant plus a duration is not a two-clock range.
        if re.match(r"\s*(?:持续|往后|之后|后查|前后)", text[match.end():]):
            return match.group(0)
        value = f"{match['date']} {match['start']} - {match['date']} {match['end']}"
        changes.append({"kind": "range_completion", "original": match.group(0),
                        "normalized": value,
                        "basis": "同一明确本地时段的结束时刻沿用开始日期；完整 ISO 双端点和 T 时区不改写"})
        return value
    expanded = _COMPACT_LOCAL_RANGE.sub(expand, text)
    result = normalize_input(expanded, received_at)
    result["changes"] = changes + result["changes"]
    return result


def _number(value: str) -> int:
    value = value.translate(_DIGITS)
    if value.isascii() and value.isdigit():
        return int(value)
    if all(ch in _HAN for ch in value):
        return int("".join(str(_HAN[ch]) for ch in value))
    if value.count("十") == 1:
        left, right = value.split("十")
        if (not left or left in _HAN) and (not right or right in _HAN):
            return (_HAN[left] if left else 1) * 10 + (_HAN[right] if right else 0)
    raise ValueError("不支持的中文数字")


def normalize_input(text: str, received_at: datetime | None = None) -> dict[str, Any]:
    """Return normalized text plus assumptions; never repair an invalid date.

    A naive receipt time means Beijing wall time. Missing years/months may use
    this anchor only when the resulting date is not in its future. A future
    yearless expression is not silently interpreted as last year.
    """
    original_text = text or ""
    anchor = received_at or datetime.now(BEIJING)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=BEIJING)
    anchor = anchor.astimezone(BEIJING)
    changes: list[dict[str, str]] = []
    missing: list[str] = []
    messages: list[str] = []
    edits: list[tuple[int, int, str]] = []
    temporal_spans: list[tuple[int, int, str]] = []
    protected = [(m.start(), m.end()) for m in _URL.finditer(original_text)]
    occupied = list(protected)

    # An unrecognized date is not the same as an omitted date. In particular,
    # legacy clock+duration callers must not interpret “上周三15:30” as today.
    for unresolved in _UNSUPPORTED_DATE.finditer(original_text):
        if any(unresolved.start() < end and unresolved.end() > start for start, end in protected):
            continue
        nearby = original_text[unresolved.end():unresolved.end() + 32]
        nearby = re.split(r"[。！？!?;；\n]", nearby, maxsplit=1)[0]
        if any(pattern.search(nearby) for pattern in (_HAN_CLOCK, _COLON_CLOCK, _MONTH_DAY, _DAY_ONLY)):
            return {"text": original_text, "received_at": anchor.isoformat(), "changes": [],
                    "needs_confirmation": True, "missing": ["完整日期确认"],
                    "message": f"检测到日期表达“{unresolved.group(0)}”，当前规则不能无歧义补齐，请明确完整年月日；不会按当天查询。"}

    def free(start: int, end: int) -> bool:
        return not any(start < right and end > left for left, right in occupied)

    def issue(field: str, message: str) -> None:
        if field not in missing:
            missing.append(field)
        if message not in messages:
            messages.append(message)

    def edit(start: int, end: int, value: str, kind: str, basis: str) -> None:
        source = original_text[start:end]
        if source != value:
            edits.append((start, end, value))
            changes.append({"kind": kind, "original": source, "normalized": value, "basis": basis})
        occupied.append((start, end))
        temporal_spans.append((start, end, kind))

    known_dates: list[tuple[int, date]] = []
    date_uses_receipt_anchor: dict[int, bool] = {}

    def add_date(match: re.Match[str], year: int, month: int, day: int, basis: str,
                 inferred: bool = False) -> None:
        try:
            value = date(year, month, day)
        except ValueError:
            occupied.append(match.span())
            issue("有效日期", f"日期“{match.group(0)}”无效，请确认年月日。")
            return
        if inferred and value > anchor.date():
            occupied.append(match.span())
            issue("省略日期的年月确认", f"“{match.group(0)}”按接收日期补齐为 {value.isoformat()}，晚于接收日期；请明确年份或完整日期。")
            return
        known_dates.append((match.start(), value))
        date_uses_receipt_anchor[match.start()] = inferred
        edit(*match.span(), value.isoformat(), "date", basis)

    # Downstream legacy parsers do not share an aware/naive policy. Convert an
    # explicit ISO offset to Beijing wall time before *any* range comparison;
    # never merely drop the offset or silently truncate fractional seconds.
    for match in _ISO_AWARE.finditer(original_text):
        if not free(*match.span()):
            continue
        # A space-separated same-day range such as 15:00-15:35 is also
        # lexically an offset timestamp. Negative offsets beyond 14 hours
        # cannot name a civil timezone; leave these valid endpoint clocks
        # for ordinary range parsing. Keep explicit T timestamps unchanged.
        if (match.group(0)[10] == " "
                and re.fullmatch(r"-(?:1[5-9]|2[0-3]):[0-5]\d", match["offset"])):
            continue
        try:
            # datetime.fromisoformat accepts overflowing offset minutes
            # (+08:99 becomes +09:39); validate the user's token ourselves.
            offset_token = match["offset"]
            if offset_token != "Z":
                offset_digits = offset_token[1:].replace(":", "")
                if int(offset_digits[:2]) > 23 or int(offset_digits[2:]) > 59:
                    issue("有效时区偏移", f"时区偏移“{offset_token}”无效，请确认；不会自动归一为其它偏移。")
                    occupied.append(match.span())
                    continue
            aware = datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))
            if re.search(r":\d{2}\.\d+", match.group(0)):
                issue("整秒时间确认", f"时刻“{match.group(0)}”包含分数秒，请确认精确到整秒的查询边界；不会自动截断。")
                occupied.append(match.span())
                continue
            local = aware.astimezone(BEIJING)
            offset = aware.utcoffset()
            assert offset is not None
            offset_minutes = int(offset.total_seconds()) // 60
            sign = "+" if offset_minutes >= 0 else "-"
            offset_name = f"{sign}{abs(offset_minutes) // 60:02d}:{abs(offset_minutes) % 60:02d}"
            known_dates.append((match.start(), local.date()))
            date_uses_receipt_anchor[match.start()] = False
            edit(*match.span(), local.strftime("%Y-%m-%d %H:%M:%S"), "timezone",
                 f"按用户明确时区偏移 {offset_name} 换算为北京时间（UTC+08:00），保持同一时刻")
        except ValueError:
            issue("有效日期时间", f"日期时间“{match.group(0)}”无效，请确认。")
            occupied.append(match.span())

    for match in _FULL_DATE.finditer(original_text):
        if not free(*match.span()):
            continue
        try:
            add_date(match, _number(match["year"]), _number(match["cm"] or match["nm"]),
                     _number(match["cd"] or match["nd"]), "用户明确提供完整年月日")
        except ValueError:
            occupied.append(match.span())
            issue("有效日期", f"日期“{match.group(0)}”无效，请确认。")

    for match in _RELATIVE.finditer(original_text):
        if not free(*match.span()):
            continue
        days = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2}[match.group(0)]
        value = anchor.date() - timedelta(days=days)
        known_dates.append((match.start(), value))
        date_uses_receipt_anchor[match.start()] = True
        edit(*match.span(), value.isoformat(), "date", f"以消息接收北京时间 {anchor.isoformat()} 为基准")

    for match in _MONTH_DAY.finditer(original_text):
        if not free(*match.span()):
            continue
        try:
            # A preceding explicit full date in the same sentence can supply
            # its year; never borrow dates from a previous message or task.
            sentence_start = max((original_text.rfind(ch, 0, match.start()) for ch in "\n。！？!?"), default=-1) + 1
            previous = [(pos, value) for pos, value in known_dates if sentence_start <= pos < match.start()]
            if previous:
                previous_pos, previous_date = max(previous, key=lambda item: item[0])
                month = _number(match["cm"] or match["nm"])
                if abs(month - previous_date.month) >= 10:
                    occupied.append(match.span())
                    issue("跨年日期确认", f"“{match.group(0)}”与同句日期可能跨年，请明确其年份。")
                    continue
                year = previous_date.year
                basis = f"沿用同句已明确日期的年份 {year}"
                inferred_from_anchor = date_uses_receipt_anchor[previous_pos]
            else:
                year = anchor.year
                basis = f"省略年份，按消息接收北京时间的年份 {year} 补齐"
                inferred_from_anchor = True
            add_date(match, year, _number(match["cm"] or match["nm"]),
                     _number(match["cd"] or match["nd"]), basis, inferred_from_anchor)
        except ValueError:
            occupied.append(match.span())
            issue("有效日期", f"日期“{match.group(0)}”无效，请确认。")

    for match in _DAY_ONLY.finditer(original_text):
        if not free(*match.span()):
            continue
        sentence_start = max((original_text.rfind(ch, 0, match.start()) for ch in "\n。！？!?"), default=-1) + 1
        previous = [(pos, value) for pos, value in known_dates if sentence_start <= pos < match.start()]
        # Also accept an explicit standalone 年月 such as “2026年9月，21号”.
        months = list(_YEAR_MONTH.finditer(original_text, sentence_start, match.start()))
        months = [m for m in months if not any(m.start() < right and m.end() > left for left, right in protected)]
        if months and (not previous or months[-1].start() >= max(previous)[0]):
            try:
                year, month = _number(months[-1]["year"]), _number(months[-1]["month"])
            except ValueError:
                issue("有效日期", "同句年月无效，请提供完整日期。")
                occupied.append(match.span())
                continue
            basis, inferred = f"沿用同句明确年月 {year:04d}-{month:02d}", False
        elif previous:
            previous_pos, latest = max(previous, key=lambda item: item[0])
            year, month = latest.year, latest.month
            basis = f"沿用同句已明确日期的年月 {year:04d}-{month:02d}"
            inferred = date_uses_receipt_anchor[previous_pos]
        else:
            year, month = anchor.year, anchor.month
            basis = f"省略年月，按消息接收北京时间的年月 {year:04d}-{month:02d} 补齐"
            inferred = True
        try:
            add_date(match, year, month, _number(match["day"]), basis, inferred)
        except ValueError:
            occupied.append(match.span())
            issue("有效日期", f"日期“{match.group(0)}”无效，请确认。")

    # Temporal vagueness is not permission to invent an exact instant. Avoid
    # treating non-temporal prose (e.g. “左右轮日志”) as a vague time.
    visible = list(original_text)
    for start, end in protected:
        visible[start:end] = " " * (end - start)
    visible_text = "".join(visible)
    temporal_seen = bool(known_dates or _HAN_CLOCK.search(visible_text) or _COLON_CLOCK.search(visible_text) or _ISO_AWARE.search(visible_text))
    if temporal_seen:
        for zone in _EXPLICIT_ZONE.finditer(visible_text):
            nearby = visible_text[max(0, zone.start() - 80):zone.end() + 80]
            if _HAN_CLOCK.search(nearby) or _COLON_CLOCK.search(nearby) or _ISO_AWARE.search(nearby):
                issue("查询时区确认", f"输入明确标注“{zone.group(0)}”，请提供换算后的北京时间；不会忽略时区直接查询。")
        for alternative in _ALTERNATIVE.finditer(visible_text):
            before = visible_text[max(0, alternative.start() - 40):alternative.start()]
            after = visible_text[alternative.end():alternative.end() + 40]
            left = sorted([m for pattern in (_HAN_CLOCK, _COLON_CLOCK, _ISO_AWARE) for m in pattern.finditer(before)], key=lambda m: m.end())
            right = sorted([m for pattern in (_HAN_CLOCK, _COLON_CLOCK, _ISO_AWARE) for m in pattern.finditer(after)], key=lambda m: m.start())
            close_left = bool(left and re.fullmatch(r"[\s，,]*", before[left[-1].end():]))
            close_right = bool(right and re.fullmatch(r"[\s，,]*", after[:right[0].start()]))
            if (left and right and (close_left or close_right)) or (alternative.group(0) == "二选一" and len(left) + len(right) >= 2):
                issue("唯一时间范围", "检测到多个备选时刻（或/二选一），请明确本次使用哪一组起止时间；不会自动选择。")
    if temporal_seen:
        for vague in _VAGUE.finditer(original_text):
            if any(vague.start() < right and vague.end() > left for left, right in protected):
                continue
            vicinity = visible_text[max(0, vague.start() - 20):vague.end() + 20]
            if any(pattern.search(vicinity) for pattern in (_HAN_CLOCK, _COLON_CLOCK, _RELATIVE, _FULL_DATE, _MONTH_DAY, _DAY_ONLY)):
                issue("精确时间", "时间包含“大概/左右”等模糊描述，请确认准确起止时间；不会自动将其定为精确时刻。")

    clock_matches = sorted([(m, True) for m in _HAN_CLOCK.finditer(original_text)] +
                           [(m, False) for m in _COLON_CLOCK.finditer(original_text)],
                           key=lambda item: (item[0].start(), -item[0].end()))
    previous_clock: tuple[int, str | None] | None = None
    for match, han_clock in clock_matches:
        if not free(*match.span()):
            continue
        if han_clock and not match["period"] and not any(match[key] for key in ("minute", "special", "second", "whole")):
            # “时” is also an existing duration unit. Never reinterpret an
            # explicit duration as the clock at which the request starts.
            prefix = original_text[max(0, match.start() - 16):match.start()]
            if match.group(0).rstrip().endswith("时") and re.search(
                r"(?:持续|往后(?:查|看)?|向后(?:查|看)?|前后各|长达|时长为?|共计)\s*$", prefix
            ):
                occupied.append(match.span())
                continue
        try:
            hour = _number(match["hour"])
            minute = _number(match["minute"]) if match["minute"] else 0
            second = _number(match["second"]) if match["second"] else 0
            if han_clock and match["special"]:
                minute = {"半": 30, "一刻": 15, "三刻": 45}[match["special"]]
            period = match["period"]
            basis = "用户明确提供24小时制时刻；统一数字、分隔符与秒数"
            if not period and previous_clock and previous_clock[1]:
                between = original_text[previous_clock[0]:match.start()]
                if _RANGE_LINK.fullmatch(between):
                    period = previous_clock[1]
                    basis = f"同一连续时间范围沿用前一时刻的“{period}”"
            if period:
                if not (0 <= hour <= 23):
                    raise ValueError("小时无效")
                if period in {"晚上", "晚间", "夜里", "夜间"}:
                    if 0 <= hour <= 6 or hour == 12:
                        issue("跨日时间确认", f"“{match.group(0)}”可能跨日，请明确对应日期及24小时制时刻。")
                        occupied.append(match.span())
                        continue
                    if 7 <= hour <= 11:
                        hour += 12
                    elif not 19 <= hour <= 23:
                        raise ValueError("时段与小时不一致")
                elif period == "傍晚":
                    if 5 <= hour <= 7:
                        hour += 12
                    elif not 17 <= hour <= 19:
                        raise ValueError("时段与小时不一致")
                elif period == "下午":
                    if 1 <= hour <= 6:
                        hour += 12
                    elif not 12 <= hour <= 18:
                        raise ValueError("时段与小时不一致")
                elif period == "中午":
                    if hour in {1, 2}:
                        hour += 12
                    elif hour not in {11, 12, 13, 14}:
                        raise ValueError("时段与小时不一致")
                elif period == "凌晨":
                    if hour > 6:
                        raise ValueError("时段与小时不一致")
                elif period in {"早上", "早晨"}:
                    if not 5 <= hour <= 11:
                        raise ValueError("时段与小时不一致")
                elif period == "上午":
                    if not 1 <= hour <= 12:
                        raise ValueError("时段与小时不一致")
                if basis.startswith("用户"):
                    basis = f"根据用户明确的“{period}”换算24小时制"
            elif han_clock and 1 <= hour <= 12:
                issue("上午或下午", f"“{match.group(0)}”未说明上午或下午，请用24小时制或补充时段。")
                occupied.append(match.span())
                previous_clock = (match.end(), None)
                continue
            if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
                raise ValueError("时分秒超出范围")
            edit(*match.span(), f"{hour:02d}:{minute:02d}:{second:02d}", "clock", basis)
            previous_clock = (match.end(), period)
        except ValueError:
            occupied.append(match.span())
            issue("有效时刻", f"时刻“{match.group(0)}”无效或时段不一致，请确认。")

    # Insert date/clock whitespace only at tokens recognized above, never with
    # a whole-text regex that could accidentally touch a URL or artifact name.
    clock_starts = {start for start, _end, kind in temporal_spans if kind == "clock"}
    joins = {end for _start, end, kind in temporal_spans if kind == "date" and end in clock_starts}
    for boundary in joins:
        edits.append((boundary, boundary, " "))
        changes.append({"kind": "separator", "original": "", "normalized": " ",
                        "basis": "在已识别且直接相邻的日期和时刻之间补空格"})
    normalized = original_text
    for start, end, replacement in sorted(edits, reverse=True):
        normalized = normalized[:start] + replacement + normalized[end:]
    if normalized != original_text and not edits:
        raise AssertionError("normalization must be auditable")
    return {
        "text": normalized,
        "received_at": anchor.isoformat(),
        "changes": changes,
        "needs_confirmation": bool(missing),
        "missing": missing,
        "message": "；".join(messages),
    }
