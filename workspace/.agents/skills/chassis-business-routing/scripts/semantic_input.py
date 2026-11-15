"""Validate an AI time interpretation against quoted facts, without model or I/O.

The conversation agent supplies a proposal, not execution authority. Only quoted
time spans are rewritten; VINs, role labels, URLs and transport identity remain
in the original text and must still pass the existing business parser.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import FunctionType

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_chassis_business_temporal_normalizer", HERE / "natural_input.py")
_normalizer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_normalizer)
normalize_input = _normalizer.normalize_input


def apply_local_range_interpretation(text, proposal, received_at=None):
    """Use business range semantics with the unchanged source/quote checks."""
    validate = FunctionType(apply_interpretation.__code__,
        dict(apply_interpretation.__globals__, normalize_input=_normalizer.normalize_query_input),
        apply_interpretation.__name__, apply_interpretation.__defaults__,
        apply_interpretation.__closure__)
    return validate(text, proposal, received_at)

BEIJING = timezone(timedelta(hours=8))
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?"
_TOKENS = re.compile(
    rf"(?P<stamp>\d{{4}}-\d{{2}}-\d{{2}}[ T]{_CLOCK}(?:Z|[+-]\d{{2}}:?\d{{2}})?)"
    rf"|(?P<date>\d{{4}}-\d{{2}}-\d{{2}})|(?P<clock>{_CLOCK})"
)
_PROTECTED = re.compile(
    r"https?://|\[cc-connect|走查测试|时延分析|日志分析|uroad|"
    r"baseline|base|PCM\s*更新|replacement|"
    r"基线|刷包前|刷包后|替换前|替换后|更新后|替换小包|小包后|"
    r"(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])", re.I
)
_CLOCK_TOKEN = re.compile(r"(?<![A-Za-z0-9_:])\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?![\d:])")


def has_unbound_clocks(text, time_ranges):
    """Do not silently discard a third endpoint or truncate fractional seconds."""
    spans = [match.span() for match in time_ranges]
    urls = [match.span() for match in re.finditer(r"https?://[^\s<>\"'，；。！？]+", text, re.I)]
    return any(not any(left <= match.start() and match.end() <= right for left, right in spans + urls)
               for match in _CLOCK_TOKEN.finditer(text))


def _instant(value, *, integral=True):
    if not isinstance(value, str):
        raise ValueError("候选时间必须是 ISO 时间字符串")
    offset = re.search(r"[+-](\d{2}):?(\d{2})$", value)
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
        # fromisoformat normalizes +07:60 to +08:00; candidates must not
        # silently repair a malformed offset, even if its instant matches.
        raise ValueError("候选时区偏移无效")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if integral and parsed.microsecond:
        raise ValueError("候选时间必须精确到整秒")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _quoted_instants(text):
    """Require actual date evidence; an unqualified clock never means today."""
    day = None
    values = []
    for match in _TOKENS.finditer(text):
        if match["stamp"]:
            instant = _instant(match["stamp"])
            day = instant.date()
            values.append(instant)
        elif match["date"]:
            day = datetime.strptime(match["date"], "%Y-%m-%d").date()
        elif match["clock"]:
            if day is None:
                raise ValueError("引用时段缺少可确定的日期")
            values.append(_instant(day.isoformat() + "T" + match["clock"]))
    if len(values) != 2:
        raise ValueError("每条引用必须包含日期依据及恰好两个明确时刻")
    return values


def apply_interpretation(text, proposal, received_at=None):
    """Accept only evidence-bound start/end candidates from the current message.

    Proposal schema: source_sha256, received_at, windows[{quote,start,end}].
    No arbitrary canonical text, role reassignment or context fields are allowed.
    Returns the same envelope as normalize_input, and never creates a task.
    """
    if isinstance(received_at, str):
        received_at = datetime.fromisoformat(received_at)
    result = normalize_input(text, received_at)
    if result["needs_confirmation"]:
        return result
    try:
        allowed = {"schema_version", "source_sha256", "received_at", "windows"}
        if not isinstance(proposal, dict) or set(proposal) - allowed:
            raise ValueError("AI 候选结构无效或包含非时间字段")
        if proposal.get("schema_version", 1) != 1:
            raise ValueError("AI 候选版本不支持")
        if proposal.get("source_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ValueError("AI 候选与当前原文不一致")
        if _instant(proposal.get("received_at"), integral=False) != _instant(result["received_at"], integral=False):
            raise ValueError("AI 候选的接收时间锚点不一致")
        windows = proposal.get("windows")
        if not isinstance(windows, list) or not 1 <= len(windows) <= 2:
            raise ValueError("AI 候选需要一段或两段时间")
        spans, changes = [], []
        previous_end = -1
        anchor = datetime.fromisoformat(result["received_at"])
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"quote", "start", "end"}:
                raise ValueError("每段 AI 候选只接受 quote、start、end")
            quote = window["quote"]
            if not isinstance(quote, str) or not quote.strip() or len(quote) > 1024:
                raise ValueError("时间引用无效")
            if text.count(quote) != 1 or _PROTECTED.search(quote):
                raise ValueError("时间引用必须唯一且不包含 VIN、链接、业务或角色字段")
            index = text.index(quote)
            if index < previous_end:
                raise ValueError("时间引用必须不重叠并保持原文顺序")
            previous_end = index + len(quote)
            # A quote is not a separate message. Keep its preceding source
            # context, so “21号” cannot overwrite a same-sentence explicit
            # 2025年9月 with the receipt year's/month's 2026年10月.
            normalized_prefix = normalize_input(text[:index], anchor)
            normalized_through_quote = normalize_input(text[:previous_end], anchor)
            if normalized_through_quote["needs_confirmation"]:
                raise ValueError(normalized_through_quote["message"])
            prefix_text = normalized_prefix["text"]
            context_text = normalized_through_quote["text"]
            if not context_text.startswith(prefix_text):
                raise ValueError("引用边界切断了原文时间字段")
            facts = _quoted_instants(context_text[len(prefix_text):])
            start, end = _instant(window["start"]), _instant(window["end"])
            if facts != [start, end]:
                raise ValueError("AI 候选时间与引用中的时间事实不一致")
            if end <= start:
                raise ValueError("结束时间必须晚于开始时间，跨日须有日期依据")
            canonical = start.strftime("%Y-%m-%d %H:%M:%S") + " - " + end.strftime("%Y-%m-%d %H:%M:%S")
            spans.append((index, previous_end, canonical))
            changes.append({"kind": "ai_time_span", "original": quote,
                            "normalized": canonical,
                            "basis": "AI 提取候选；程序核对原文引用、日期依据、时刻与输入顺序"})
        rewritten = text
        for start, end, replacement in reversed(spans):
            rewritten = rewritten[:start] + replacement + rewritten[end:]
        # Normalize any unrelated time tokens only after the replacements.
        accepted = normalize_input(rewritten, anchor)
        accepted["changes"] = result["changes"] + changes + accepted["changes"]
        accepted["interpretation"] = proposal
        return accepted
    except (ValueError, TypeError, KeyError, OverflowError):
        # Keep invalid source or proposal content out of a user-facing error.
        result.update(needs_confirmation=True, missing=["时间解释确认"],
                      message="无法核实这段自然语言对应的准确日期和起止时间，请补充或确认；未创建任务。")
        return result
