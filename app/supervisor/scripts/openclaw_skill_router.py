"""Deterministically route one inbound Feishu message to at most one workflow."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

RouteName = Literal[
    "asgard-dispatch",
    "ad-p99-comparison",
    "ad-p99-continuation",
    "uroad-log-analysis",
    "no-match",
]

SHEETS_URL = re.compile(
    r"https://[^\s<>\"']+\.feishu\.cn/sheets/[A-Za-z0-9]+(?:\?[^\s<>\"']*)?",
    re.IGNORECASE,
)
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
TIMESTAMP = re.compile(r"\b20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\b")
COMPARISON_KEY = re.compile(r"#([0-9a-f]{12,64})\b", re.IGNORECASE)
P99_INTENT = re.compile(r"(?:\bAD\s*P99\b|P99\s*(?:时延|延迟)|(?:时延|延迟)\s*对比)", re.IGNORECASE)
COMPARE_INTENT = re.compile(r"(?:对比|比较|baseline|替换后|刷包)", re.IGNORECASE)
UROAD_INTENT = re.compile(r"(?:\buroad\b|云端日志)", re.IGNORECASE)
UROAD_LOG = re.compile(r"(?:日志|\blog\b|分析|排查)", re.IGNORECASE)
UPSTREAM_COMMAND = re.compile(r"\bfsdlog\b", re.IGNORECASE)
DISPATCH_INTENT = re.compile(r"(?:AD\s*P99\s*)?时延分析", re.IGNORECASE)
ASGARD_COMMAND = re.compile(
    r"\bfsdlog\s+"
    r"(?P<vin>[A-HJ-NPR-Z0-9]{17})\s*,\s*"
    r"(?P<start>20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*,\s*"
    r"(?P<end>20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*,\s*"
    r"(?P<platform>[A-Za-z0-9][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RouteDecision:
    route: RouteName
    reason: str
    sheet_urls: tuple[str, ...] = ()
    comparison_key: str | None = None


def _unique_sheet_urls(text: str) -> tuple[str, ...]:
    """Return final Sheets links in input order."""
    found: list[str] = []
    for match in SHEETS_URL.finditer(text):
        url = match.group(0).rstrip("，,。.;；:：)）]】")
        if url not in found:
            found.append(url)
    return tuple(found)


def parse_asgard_dispatch(text: str) -> tuple[str, str] | None:
    """Return two validated fsdlog commands only for the explicit dispatch syntax.

    The user may place “时延分析” before or after the two commands.  Any other
    free text is allowed, but exactly two complete commands, one VIN, and two
    non-overlapping time windows are required.  The returned commands preserve
    their original order while normalising surrounding whitespace.
    """
    if not isinstance(text, str) or not DISPATCH_INTENT.search(text):
        return None
    matches = list(ASGARD_COMMAND.finditer(text))
    if len(matches) != 2:
        return None
    commands = tuple(" ".join(match.group(0).split()) for match in matches)
    if commands[0] == commands[1]:
        return None
    values = [match.groupdict() for match in matches]
    if values[0]["vin"].upper() != values[1]["vin"].upper():
        return None
    ranges = [
        (
            datetime.strptime(value["start"], "%Y-%m-%d %H:%M:%S"),
            datetime.strptime(value["end"], "%Y-%m-%d %H:%M:%S"),
        )
        for value in values
    ]
    if any(start >= end for start, end in ranges):
        return None
    if max(ranges[0][0], ranges[1][0]) < min(ranges[0][1], ranges[1][1]):
        return None
    return commands


def route_message(
    text: str,
    *,
    bot_mentioned: bool,
    is_card_reply: bool = False,
    has_pending_ad_p99_dispatch: bool = False,
) -> RouteDecision:
    """Classify one current event without reading conversation history."""
    if not isinstance(text, str) or not text.strip():
        return RouteDecision("no-match", "消息为空。")
    if not bot_mentioned:
        return RouteDecision("no-match", "未 @ OpenClaw 机器人。")

    urls = _unique_sheet_urls(text)
    if parse_asgard_dispatch(text) is not None:
        return RouteDecision(
            "asgard-dispatch",
            "明确的双 fsdlog 时延分析派发请求。",
        )

    key_match = COMPARISON_KEY.search(text)
    has_p99_role = bool(re.search(r"(?:baseline|基线|替换后|刷包后)", text, re.IGNORECASE))
    if key_match and has_p99_role and (len(urls) == 1 or is_card_reply):
        return RouteDecision(
            "ad-p99-continuation",
            "带 comparison_key 的 AD P99 补充结果。",
            urls,
            key_match.group(1).lower(),
        )

    # A raw fsdlog is for Asgard only; it must not trigger either local Skill.
    if UPSTREAM_COMMAND.search(text):
        return RouteDecision("no-match", "fsdlog 仅交由阿斯加德会话处理。", urls)

    has_p99_intent = bool(P99_INTENT.search(text))
    has_compare_intent = bool(COMPARE_INTENT.search(text))
    has_baseline_label = bool(re.search(r"(?:baseline|基线)", text, re.IGNORECASE))
    has_replacement_label = bool(re.search(r"(?:替换后|刷包后|replacement)", text, re.IGNORECASE))
    if len(urls) == 2 and (
        has_p99_intent
        or has_compare_intent
        or (
            has_pending_ad_p99_dispatch
            and has_baseline_label
            and has_replacement_label
        )
    ):
        return RouteDecision(
            "ad-p99-comparison",
            (
                "与待处理阿斯加德派发任务匹配的双 Sheets URL。"
                if has_pending_ad_p99_dispatch and not (has_p99_intent or has_compare_intent)
                else "明确的双 Sheets URL AD P99 对比请求。"
            ),
            urls,
        )

    timestamps = TIMESTAMP.findall(text)
    is_uroad_request = (
        not urls
        and bool(UROAD_INTENT.search(text))
        and bool(UROAD_LOG.search(text))
        and not has_p99_intent
        and len(VIN.findall(text)) == 1
        and len(timestamps) == 2
    )
    if is_uroad_request:
        return RouteDecision(
            "uroad-log-analysis",
            "明确的单时段 uroad 日志分析请求。",
        )

    if urls:
        return RouteDecision(
            "no-match",
            "Sheets URL 未满足 AD P99 的双链接或带 comparison_key 补充条件。",
            urls,
        )
    return RouteDecision("no-match", "未满足任一 Skill 的完整触发条件。")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw 双 Skill 入站消息路由器")
    parser.add_argument("--text", required=True)
    parser.add_argument("--bot-mentioned", action="store_true")
    parser.add_argument("--card-reply", action="store_true")
    parser.add_argument("--pending-ad-p99", action="store_true")
    args = parser.parse_args()
    decision = route_message(
        args.text,
        bot_mentioned=args.bot_mentioned,
        is_card_reply=args.card_reply,
        has_pending_ad_p99_dispatch=args.pending_ad_p99,
    )
    print(json.dumps(asdict(decision), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
