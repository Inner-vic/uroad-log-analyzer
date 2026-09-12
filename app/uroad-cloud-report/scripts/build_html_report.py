#!/usr/bin/env python3
"""Build a self-contained, offline, interactive uroad HTML report."""
from __future__ import annotations

import argparse
import html
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from gear_labels import gear_display
from render_performance_charts import downsample_extrema, duration_points, finite, parse_epoch
from reason_labels import normalize_reason_code, reason_label

MAX_POINTS = 1200
MAX_FRAMES = 100
MAX_CATEGORIES = 40
SERIES = (
    ("preCostSeries", "前处理耗时", "ms", "#2563eb"),
    ("inferCostSeries", "推理链路耗时", "ms", "#f59e0b"),
    ("postCostSeries", "后处理耗时", "ms", "#16a34a"),
    ("e2eCostSeries", "端到端耗时", "ms", "#dc2626"),
)
CAN_SERIES = (
    ("vehicleSpeed", "车速 VehSpd_BCh1", "km/h", "#2563eb", False),
    ("gear", "挡位 ActuGearSta", "挡位", "#7c3aed", True),
    ("steeringAngle", "方向盘转角 EpsStrgAng", "deg", "#dc2626", False),
)


def _safe_json(value: object) -> str:
    """Serialize for a script element without allowing markup termination."""
    def normalize(item: object) -> object:
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item
    return (json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _number(value: object) -> float | None:
    number = finite(value)
    return number if number is not None else None


def _dict_list(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: object, limit: int = 80) -> list[str]:
    return [str(item)[:500] for item in value[:limit] if isinstance(item, (str, int, float, bool))] if isinstance(value, list) else []


def _duration(series: list[dict]) -> tuple[list[dict], str, dict]:
    points, mode = duration_points(series)
    def convert(items: list[dict]) -> list[dict]:
        result = []
        for point in items:
            x = point.get("epoch") if mode == "time" else _number(point.get("x"))
            if x is not None:
                result.append({"x": x, "y": point["value"], "label": point.get("x")})
        return result
    full = convert(points)
    result = []
    for point in downsample_extrema(points, MAX_POINTS):
        x = point.get("epoch") if mode == "time" else _number(point.get("x"))
        if x is not None:
            result.append({"x": x, "y": point["value"], "label": point.get("x")})
    return result, mode, _stats(full)


def _fps(series: list[dict]) -> tuple[list[dict], str, dict]:
    sequence = []
    for index, point in enumerate(series):
        value = _number(point.get("value"))
        x = _number(point.get("sequence"))
        if value is not None:
            x = x if x is not None else index + 1
            sequence.append({"x": x, "value": value, "label": x, "index": index})
    sampled = downsample_extrema(sequence, MAX_POINTS)
    full = [{"x": p["x"], "y": p["value"], "label": p.get("label")} for p in sequence]
    return [{"x": p["x"], "y": p["value"], "label": p.get("label")} for p in sampled], "sequence", _stats(full)


def _fps_points(series: list[dict]) -> tuple[list[dict], str, dict]:
    valid = [point for point in series if _number(point.get("value")) is not None]
    timed = []
    for point in valid:
        value, epoch = _number(point.get("value")), parse_epoch(point.get("ts"))
        if value is not None and epoch is not None:
            timed.append({"x": epoch, "value": value, "label": point.get("ts")})
    if timed and len(timed) == len(valid):
        timed.sort(key=lambda item: item["x"])
        for index, point in enumerate(timed):
            point["index"] = index
        full = [{"x": p["x"], "y": p["value"], "label": p.get("label")} for p in timed]
        sampled = [{"x": p["x"], "y": p["value"], "label": p.get("label")} for p in downsample_extrema(timed, MAX_POINTS)]
        return sampled, "time", _stats(full)
    points, mode, stats = _fps(valid)
    return points, "sequence_partial_time" if timed else mode, stats


def _can(signal: dict, gear: bool = False) -> tuple[list[dict], dict, int]:
    clean = []
    for point in _dict_list(signal.get("series")):
        x, value = _number(point.get("epochMs")), _number(point.get("value"))
        if x is None or value is None:
            continue
        item = {"x": x, "value": value, "index": len(clean)}
        if gear:
            item["semantic"] = gear_display(point.get("raw", value), point.get("label"), signal.get("confidence"))
        clean.append(item)
    clean.sort(key=lambda item: item["x"])
    for index, point in enumerate(clean):
        point["index"] = index
    if gear and len(clean) > MAX_POINTS:
        transitions = [clean[0]] + [clean[i] for i in range(1, len(clean)) if clean[i]["value"] != clean[i - 1]["value"]]
        if transitions[-1]["index"] != clean[-1]["index"]:
            transitions.append(clean[-1])
        if len(transitions) > MAX_POINTS:
            sampled = [transitions[i * (len(transitions) - 1) // (MAX_POINTS - 1)] for i in range(MAX_POINTS)]
        else:
            sampled = transitions
    else:
        sampled = downsample_extrema(clean, MAX_POINTS)
    display = [{"x": p["x"], "y": p["value"], **({"semantic": p["semantic"]} if "semantic" in p else {})} for p in sampled]
    full = [{"x": p["x"], "y": p["value"]} for p in clean]
    return display, _stats(full), len(clean)


def _stats(points: list[dict]) -> dict:
    values = [point["y"] for point in points]
    return {"count": len(values), "avg": statistics.mean(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def _domain(points: list[dict]) -> list[float] | None:
    if not points:
        return None
    low, high = min(point["x"] for point in points), max(point["x"] for point in points)
    if low == high:
        padding = max(1, abs(low) * .01)
        return [low - padding, high + padding]
    return [low, high]


def _reasons(items: list[dict]) -> tuple[list[dict], dict]:
    clean = []
    for item in items:
        value = _number(item.get("count"))
        if value is not None and value > 0:
            reason = item.get("reason")
            code = normalize_reason_code(reason) if isinstance(reason, (str, int, float, bool)) else "不可用"
            clean.append({"code": code, "label": reason_label(code), "value": value})
    ordered = sorted(clean, key=lambda item: (-item["value"], item["code"]))
    for item in ordered:
        item.pop("code", None)
    return ordered[:MAX_CATEGORIES], {"totalCategories": len(ordered), "displayCategories": min(len(ordered), MAX_CATEGORIES),
                                      "totalCount": sum(item["value"] for item in ordered), "truncated": len(ordered) > MAX_CATEGORIES}


def _allow(value: object, keys: tuple[str, ...]) -> dict:
    source = value if isinstance(value, dict) else {}
    result = {}
    for key in keys:
        item = source.get(key)
        result[key] = item[:1000] if isinstance(item, str) else item if isinstance(item, (int, float, bool)) or item is None else None
    return result


def _reason(value: object) -> object | None:
    if isinstance(value, str):
        value = value.strip()
        return value[:1000] if value else None
    if isinstance(value, bool) or value is None:
        return None
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _event_location(value: object) -> tuple[str, int] | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    chain, line = value.rsplit(":", 1)
    try:
        number = int(line)
    except ValueError:
        return None
    return (chain, number) if chain and number >= 0 else None


def _parsed_time(value: object) -> tuple[str, float] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if "T" in text or (len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-"):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return "epoch", parsed.timestamp() * 1000
            return "naive", (parsed.toordinal() * 86400 + parsed.hour * 3600 + parsed.minute * 60 + parsed.second + parsed.microsecond / 1e6) * 1000
        hours, minutes, seconds = text.split(":")
        hour, minute, second = int(hours), int(minutes), float(seconds)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second < 60):
            return None
        return "clock", (hour * 3600 + minute * 60 + second) * 1000
    except (ValueError, TypeError, OverflowError):
        return None


def _time_distance(left: tuple[str, float] | None, right: tuple[str, float] | None) -> float:
    if left is None or right is None or left[0] != right[0]:
        return math.inf
    distance = abs(left[1] - right[1])
    return min(distance, 86400000 - distance) if left[0] == "clock" else distance


def _correlate_timeline(items: list[dict], timeline: list[dict]) -> tuple[dict[int, list[dict]], dict]:
    """Link timeline events using provenance only; never use displayed frame numbers."""
    frame_info = []
    exact = {}
    for index, frame in enumerate(items):
        event_ids = {value for value in frame.get("events", []) if isinstance(value, str)} if isinstance(frame.get("events"), list) else set()
        chains = {location[0] for value in event_ids if (location := _event_location(value))}
        evidence = sorted({value for value in frame.get("evidence", []) if isinstance(value, int) and not isinstance(value, bool)}) if isinstance(frame.get("evidence"), list) else []
        for event_id in event_ids:
            exact.setdefault(event_id, []).append(index)
        frame_time = next((_parsed_time(frame.get(key)) for key in ("preTimingTimestamp", "postTimingTimestamp", "ts") if _parsed_time(frame.get(key)) is not None), None)
        frame_info.append((chains, evidence, frame_time))
    links, unmatched, ambiguous, duplicates = {}, 0, 0, 0
    seen = set()
    for order, event in enumerate(timeline):
        event_id = event.get("eventId")
        identity = (event_id, event.get("ts"), event.get("title"), event.get("description"))
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        location = _event_location(event_id)
        if not location:
            unmatched += 1
            continue
        candidates = []
        match = "精确事件"
        for index in exact.get(event_id, []):
            chains, evidence, frame_time = frame_info[index]
            distance = min((abs(location[1] - value) for value in evidence), default=math.inf) if len(chains) == 1 else math.inf
            time_distance = _time_distance(_parsed_time(event.get("ts")), frame_time)
            candidates.append((distance, time_distance, index))
        if not candidates:
            match = "邻近日志"
            for index, (chains, evidence, frame_time) in enumerate(frame_info):
                if len(chains) != 1 or location[0] not in chains or not evidence:
                    continue
                distance = min(abs(location[1] - value) for value in evidence)
                if distance <= 100:
                    time_distance = _time_distance(_parsed_time(event.get("ts")), frame_time)
                    candidates.append((distance, time_distance, index))
        if not candidates:
            unmatched += 1
            continue
        candidates.sort(key=lambda value: (value[0], value[1]))
        if len(candidates) > 1:
            ambiguous += 1
        distance, _, index = candidates[0]
        reference = {"time": str(event.get("ts") or "时间未知")[:100],
                     "title": str(event.get("title") or event.get("description") or "未命名事件")[:500],
                     "matchLabel": match}
        if match == "邻近日志":
            reference["lineDistance"] = distance
        links.setdefault(index, []).append(reference)
    return links, {"total": len(timeline), "retained": len(timeline) - duplicates,
                   "linked": sum(len(value) for value in links.values()),
                   "unmatched": unmatched, "ambiguous": ambiguous, "duplicates": duplicates}


def _sample_positions(positions: list[int], capacity: int) -> list[int]:
    if len(positions) <= capacity:
        return positions
    if capacity <= 1:
        return positions[:capacity]
    return [positions[i * (len(positions) - 1) // (capacity - 1)] for i in range(capacity)]


def _key_frames(items: list[dict], timeline_links: dict[int, list[dict]] | None = None) -> tuple[list[dict], dict]:
    timeline_links = timeline_links or {}
    keys = ("frame", "status", "humanSummary", "speedKph", "gear", "preTotalMs", "inferMs",
            "postTotalMs", "e2eMs", "skipReason", "errorReason", "timelineReferences")
    key_items = []
    for index, item in enumerate(items):
        status = str(item.get("status") or "").lower()
        skip_reason, error_reason = _reason(item.get("skipReason")), _reason(item.get("errorReason"))
        diagnostic = status in {"skipped", "error"} or skip_reason is not None or error_reason is not None
        references = timeline_links.get(index, [])
        if diagnostic or references:
            category = (("reasons", skip_reason, error_reason) if skip_reason is not None or error_reason is not None else ("status", status)) if diagnostic else None
            normalized = dict(item, skipReason=skip_reason, errorReason=error_reason,
                              timelineReferences=references)
            key_items.append((index, category, normalized))
    selected = key_items
    total_categories = len({category for _, category, _ in key_items if category is not None})
    display_categories = total_categories
    if len(key_items) > MAX_FRAMES:
        linked_positions = [position for position, (index, _, _) in enumerate(key_items) if index in timeline_links]
        if len(linked_positions) >= MAX_FRAMES:
            chosen = set(_sample_positions(linked_positions, MAX_FRAMES))
            display_categories = len({key_items[position][1] for position in chosen if key_items[position][1] is not None})
            selected = [key_items[i] for i in sorted(chosen)]
            frames = [_safe_frame(item, keys) for _, _, item in selected]
            return frames, {"totalFrames": len(items), "keyFrames": len(key_items), "displayFrames": len(frames),
                            "omittedFrames": len(key_items) - len(frames), "sampled": True,
                            "totalCategories": total_categories, "displayCategories": display_categories,
                            "omittedCategories": total_categories - display_categories,
                            "timelineLinkedFrames": len(timeline_links), "displayTimelineLinkedFrames": len(frames),
                            "omittedTimelineLinkedFrames": len(linked_positions) - len(frames),
                            "timelineReferences": sum(len(value) for value in timeline_links.values()),
                            "displayTimelineReferences": sum(len(frame["timelineReferences"]) for frame in frames),
                            "omittedTimelineReferences": sum(len(value) for value in timeline_links.values()) - sum(len(frame["timelineReferences"]) for frame in frames)}
        chosen = set(linked_positions)
        capacity = MAX_FRAMES - len(chosen)
        covered = {key_items[position][1] for position in chosen if key_items[position][1] is not None}
        category_first = {}
        for position, (_, category, _) in enumerate(key_items):
            if position not in chosen and category is not None and category not in covered:
                category_first.setdefault(category, position)
        representatives = list(category_first.values())
        chosen.update(_sample_positions(representatives, capacity))
        remaining_capacity = MAX_FRAMES - len(chosen)
        remaining = [position for position in range(len(key_items)) if position not in chosen and key_items[position][1] is not None]
        chosen.update(_sample_positions(remaining, remaining_capacity))
        selected = [key_items[i] for i in sorted(chosen)[:MAX_FRAMES]]
    display_categories = len({category for _, category, _ in selected if category is not None})
    frames = [_safe_frame(item, keys) for _, _, item in selected]
    return frames, {"totalFrames": len(items), "keyFrames": len(key_items), "displayFrames": len(frames),
                    "omittedFrames": len(key_items) - len(frames), "sampled": len(key_items) > len(frames),
                    "totalCategories": total_categories, "displayCategories": display_categories,
                    "omittedCategories": total_categories - display_categories,
                    "timelineLinkedFrames": len(timeline_links),
                    "displayTimelineLinkedFrames": sum(1 for index, _, _ in selected if index in timeline_links),
                    "omittedTimelineLinkedFrames": len(timeline_links) - sum(1 for index, _, _ in selected if index in timeline_links),
                    "timelineReferences": sum(len(value) for value in timeline_links.values()),
                    "displayTimelineReferences": sum(len(frame["timelineReferences"]) for frame in frames),
                    "omittedTimelineReferences": sum(len(value) for value in timeline_links.values()) - sum(len(frame["timelineReferences"]) for frame in frames)}


def _safe_frame(item: dict, keys: tuple[str, ...]) -> dict:
    frame = _allow(item, tuple(key for key in keys if key != "timelineReferences"))
    references = []
    for reference in item.get("timelineReferences", []):
        if not isinstance(reference, dict):
            continue
        safe = _allow(reference, ("time", "title"))
        references.append(safe)
    frame["timelineReferences"] = references
    for key in ("skipReason", "errorReason"):
        if frame.get(key) is not None:
            frame[key] = reason_label(frame[key])
    return frame


def build_payload(data: dict) -> dict:
    charts = data.get("charts") if isinstance(data.get("charts"), dict) else {}
    series_meta = charts.get("seriesMeta") if isinstance(charts.get("seriesMeta"), dict) else {}
    durations, duration_modes = {}, set()
    for key, label, unit, color in SERIES:
        points, mode, stats = _duration(_dict_list(charts.get(key)))
        if points:
            duration_modes.add(mode)
        meta = series_meta.get(key) if isinstance(series_meta.get(key), dict) else {}
        durations[key] = {"title": label, "unit": unit, "color": color, "points": points, "domain": _domain(points), "xMode": mode,
                          "syncGroup": f"duration-{key}", "coverage": (meta.get("coverage") or "observed") if points else "unavailable",
                          "source": (meta.get("source") or "未说明") if points else "不可用", "stats": stats, "displayPointCount": len(points)}
    fps, fps_mode, fps_stats = _fps_points(_dict_list(charts.get("fpsSeries")))
    can_data = charts.get("canSignals") if isinstance(charts.get("canSignals"), dict) else {}
    signals = can_data.get("signals") if isinstance(can_data.get("signals"), dict) else {}
    can = {}
    for key, label, unit, color, stepped in CAN_SERIES:
        signal = signals.get(key) if isinstance(signals.get(key), dict) else {}
        points, stats, observed_count = _can(signal, key == "gear")
        raw_count = signal.get("rawPointCount") if isinstance(signal.get("rawPointCount"), (int, float)) else None
        can[key] = {"title": label, "unit": unit, "color": color, "stepped": stepped, "points": points,
                    "confidence": str(signal.get("confidence") or "unknown")[:100], "channels": _string_list(signal.get("channels"), 20),
                    "rawPointCount": raw_count, "displayPointCount": len(points),
                    "observedPointCount": observed_count, "stats": stats,
                    "downsampled": observed_count > len(points), "transitionOmission": bool(stepped and observed_count > len(points)),
                    "source": "analysis.json.charts.canSignals" if points else "不可用",
                    "coverage": ("observed" if signal.get("confidence") == "confirmed" else "partial") if points else "unavailable"}
    skip, skip_meta = _reasons(_dict_list(charts.get("skipReasonStats")))
    error, error_meta = _reasons(_dict_list(charts.get("errorReasonStats")))
    reasons = {"skip": skip, "error": error}
    summary = _allow(data.get("summary"), ("runningStatus", "startupStatus", "totalFrames", "normalFrames", "skippedFrames", "errorFrames", "avgE2EMs", "avgE2ECoverage", "latestFps", "mainIssue"))
    source_in = data.get("source") if isinstance(data.get("source"), dict) else {}
    query = _allow(source_in.get("query"), ("vin", "startTime", "endTime", "cloudEnv", "providerEnvironment"))
    files = source_in.get("files")
    source = {"fileCount": len(files) if isinstance(files, list) else None, "query": query}
    timeline_all = _dict_list(data.get("timeline"))
    timeline_links, correlation_meta = _correlate_timeline(frames_all := _dict_list(data.get("frames")), timeline_all)
    timeline = [_allow(item, ("ts", "title", "description")) for item in timeline_all]
    issues = [_allow(item, ("fact", "issueCode", "count", "impact")) for item in _dict_list(data.get("issues"))]
    frames, frame_meta = _key_frames(frames_all, timeline_links)
    return {
        "summary": summary, "source": source, "timeline": timeline, "timelineMeta": {"total": len(timeline_all), "display": len(timeline), "truncated": False}, "correlationMeta": correlation_meta, "issues": issues, "frames": frames, "frameMeta": frame_meta,
        "durations": durations, "durationMixedModes": len(duration_modes) > 1,
        "fps": {"title": "FPS 趋势", "unit": "Hz", "color": "#0891b2", "points": fps,
                "coverage": ("partial_timestamps_sequence_fallback" if fps_mode == "sequence_partial_time" else "observed") if fps else "unavailable",
                "source": "analysis.json.charts.fpsSeries" if fps else "不可用", "stats": fps_stats, "displayPointCount": len(fps)}, "fpsMode": fps_mode,
        "reasons": reasons, "reasonMeta": {"skip": skip_meta, "error": error_meta}, "can": can,
        "canWarnings": _string_list(can_data.get("warnings")),
        "displayLimit": MAX_POINTS,
    }



_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "references" / "report-template.html"


def _text(value: object, fallback: str = "不可用") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _metric(value: object, suffix: str = "") -> str:
    number = _number(value)
    return f"{number:.2f}{suffix}" if number is not None else "不可用"


def _display_environment(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "prod":
        return "生产环境"
    if normalized in {"test", "testtwo"}:
        return "测试环境"
    return "未知"


def _display_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "warning": "警告", "success": "成功", "failed": "失败", "failure": "失败",
        "running": "运行中", "healthy": "正常", "error": "错误",
        "not_started": "未启动", "unknown": "未知",
    }.get(normalized, _text(value, "未知"))


def _status_class(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "healthy", "running"}:
        return "status-success"
    if normalized in {"failed", "failure", "error"}:
        return "status-failed"
    if normalized == "warning":
        return "status-warning"
    return "plain"


def _header(payload: dict) -> str:
    summary = payload.get("summary", {})
    source = payload.get("source", {})
    query = source.get("query", {})
    esc = lambda value: html.escape(_text(value))
    start_raw = _text(query.get("startTime"))
    end_raw = _text(query.get("endTime"))
    start, end = html.escape(start_raw), html.escape(end_raw)
    environment = html.escape(_display_environment(query.get("providerEnvironment")))
    report_title = html.escape(f"uroad云端日志解析报告 - {query.get('vin') or '未知VIN'} - {start_raw} ～ {end_raw}")
    payload["pageTitle"] = report_title
    avg_label = "超时平均耗时" if summary.get("avgE2ECoverage") == "overrun_only" else "平均端到端耗时"
    avg_title = ' title="仅统计超时样本的平均端到端耗时"' if summary.get("avgE2ECoverage") == "overrun_only" else ""
    meta = (
        f'<div class="metric metric-card metric-basic"><span>VIN</span><strong>{esc(query.get("vin"))}</strong></div>'
        f'<div class="metric metric-card metric-basic metric-query"><span>查询时间</span><strong><time class="query-time-part">{start}</time><span>～</span><time class="query-time-part">{end}</time></strong></div>'
        f'<div class="metric metric-card metric-basic"><span>文件数量</span><strong>{esc(source.get("fileCount"))} 个</strong></div>'
        f'<div class="metric metric-card metric-basic"><span>接口环境</span><strong class="metric-value plain">{environment}</strong></div>'
    )
    cards = [
        ("运行状态", _display_status(summary.get("runningStatus")), _status_class(summary.get("runningStatus")), ""),
        ("启动状态", _display_status(summary.get("startupStatus")), _status_class(summary.get("startupStatus")), ""),
        ("总帧数", _text(summary.get("totalFrames")), "plain", ""),
        ("正常帧数", _text(summary.get("normalFrames")), "plain", ""),
        ("跳帧数", _text(summary.get("skippedFrames")), "plain", ""),
        ("异常帧数", _text(summary.get("errorFrames")), "plain", ""),
        (avg_label, _metric(summary.get("avgE2EMs"), " ms"), "plain", avg_title),
        ("最新 FPS", _metric(summary.get("latestFps"), " Hz"), "plain", ""),
    ]
    metrics = "".join(
        f'<div class="metric metric-card metric-overview"{title}><span>{html.escape(label)}</span><strong class="metric-value {cls}">{html.escape(value)}</strong></div>'
        for label, value, cls, title in cards
    )
    return f'<header><h1>uroad云端日志解析报告</h1><div class="summary-groups"><div class="report-meta-grid">{meta}</div><div class="report-metrics-grid">{metrics}</div></div></header>'


def _frame_section(payload: dict) -> str:
    meta = payload.get("frameMeta", {})
    omitted = int(meta.get("omittedFrames") or 0)
    suffix = f"，省略 {omitted} 个" if omitted else ""
    status_labels = {"normal": "正常", "incomplete": "不完整", "skipped": "跳帧", "error": "异常"}
    keys = ("frame", "status", "humanSummary", "speedKph", "gear", "preTotalMs", "inferMs", "postTotalMs", "e2eMs", "skipReason", "errorReason")
    rows = []
    for frame in payload.get("frames", []):
        cells = []
        for key in keys:
            value = frame.get(key)
            if key == "status":
                value = status_labels.get(str(value), value)
            cells.append(f"<td>{html.escape(_text(value))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<section class="section"><h2>关键帧摘录</h2>'
        f'<p class="muted">共 {html.escape(_text(meta.get("totalFrames")))} 帧，摘录 {html.escape(_text(meta.get("displayFrames")))} 个与关键事件或诊断相关的帧记录{suffix}。表格仅呈现日志解析得到的帧事实字段。</p>'
        '<div class="frame-scroll"><table><thead><tr><th>frame</th><th>状态</th><th>说明</th><th>车速</th><th>挡位原始值</th><th>前处理 ms</th><th>推理 ms</th><th>后处理 ms</th><th>端到端 ms</th><th>跳帧原因</th><th>异常原因</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div></section>'
    )


def render(data: dict) -> str:
    payload = build_payload(data)
    header_html = _header(payload)
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__UROAD_PAGE_TITLE__": payload.get("pageTitle", html.escape("uroad云端日志解析报告")),
        "__UROAD_HEADER__": header_html,
        "__UROAD_MAIN_ISSUE__": html.escape(_text(payload.get("summary", {}).get("mainIssue"))),
        "__UROAD_FRAME_SECTION__": _frame_section(payload),
        "__UROAD_REPORT_DATA__": _safe_json(payload),
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise RuntimeError(f"report template marker invalid: {marker}")
        template = template.replace(marker, value)
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    temporary = output.with_suffix(output.suffix + ".tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    try:
        data = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
        temporary.write_text(render(data), encoding="utf-8")
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise
    print(json.dumps({"ok": True, "htmlReport": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
