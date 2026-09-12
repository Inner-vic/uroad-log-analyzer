#!/usr/bin/env python3
"""Parse mixed logs into a structured uroad analysis result."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACTUAL_TS = re.compile(r"\+0800 (?P<date>\d{4}-\d\d-\d\d)-(?P<h>\d\d)_(?P<m>\d\d)_(?P<s>\d\d)_(?P<ms>\d{3})")
GLOG_TS = re.compile(r"^[IWE](?P<md>\d{4}) (?P<ts>\d\d:\d\d:\d\d\.\d{6})")
LEVEL_RE = re.compile(r"(?:^|\s)(?P<level>[IWE])(?:\s|$)")
PRE_TIMING = re.compile(r"UroadPreNode::run\(\) timing\(ms\) frame=(?P<frame>\d+).*?TOTAL=(?P<total>[\d.]+)")
POST_TIMING = re.compile(r"UroadPostNode::run\(\) timing\(ms\) frame=(?P<frame>\d+).*?TOTAL=(?P<total>[\d.]+)")
PRE_HEAD = re.compile(r"UroadPreNode::run\(\).*?frame=(?P<frame>\d+).*?speed=(?P<speed>[\d.]+)kph.*?gear=(?P<gear>[^ |]+)")
POST_HEAD = re.compile(r"UroadPostNode run frame=(?P<frame>\d+)")
FPS_RE = re.compile(r"FPSlast10frames:\s*(?P<fps>[\d.]+)\s*Hz")
SKIP_FRAME = re.compile(r"skip frame=(?P<frame>\d+)")
SKIP_REASON = re.compile(r"skip_reason=(?P<reason>[\w-]+)")
POINTS_RE = re.compile(r"trajectory too few pts=(?P<points>\d+).*?skip frame=(?P<frame>\d+)")
GRAPH_OVERRUN = re.compile(
    r"graph:\s*(?P<graph>[^,]+),\s*context idx:\s*(?P<ctx>\d+),.*?"
    r"startTime:\s*(?P<start>\d+),\s*endTime:\s*(?P<end>\d+),\s*"
    r"running time:\s*(?P<ms>\d+) ms, exceed max exec time:\s*(?P<limit>\d+) ms"
)
GRAPH_EXEC = re.compile(r"GraphName:\s*(?P<graph>\S+) .*?execTime\s+(?P<ms>\d+)")
TRAJECTORY_GEN = re.compile(r"TrajectoryGeneration(?:Impl|FromSnapshot):\s*(?P<ms>[\d.]+)\s*ms")
EDSG_TASK = re.compile(
    r"CPUNode:\s+(?P<action>active|deactive)\s+ctx\s+(?P<ctx>\d+),\s*"
    r"taskID\s+(?P<task_id>\d+),\s*taskName\s+(?P<task>[^,\s]+),\s*"
    r"frameId\s+(?P<frame_id>\d+)(?:,\s*maxDurationUs\s+(?P<duration_us>\d+))?"
)
INFERENCE_REPORT = re.compile(
    r"\[(?P<node>UnifiedInferenceNode_[^\]]+)\]\s+runWithContext\s+ctx=(?P<ctx>\d+)\s+"
    r"infer=(?P<ms>\d+)\s+ms\s+(?P<us>\d+)\s+us"
)
FRAME_DROP = re.compile(r"FrameControl:\s+graph='(?P<graph>[^']+)'.*?DROP context")
ISSUE_RULES = (
    ("EMPTY_POINT_CLOUD", "ConvertRawToPointCloud returned empty cloud", "当前雷达点云为空，相关 uroad 处理可能被跳过。", "empty_point_cloud", "warning"),
    ("EXPECTED_INPUT_COUNT", "Expected 17 inputs, got", "当前帧后处理失败：收到的输入数量不正确。", "expected_input_count", "error"),
    ("PASS_THROUGH_DECODE_FAILED", "Failed to extract pass-through data", "当前帧后处理失败：透传数据解析失败。", "pass_through_decode_failed", "error"),
    ("INFERENCE_OUTPUT_FAILED", "Failed to extract inference output", "当前帧后处理失败：推理输出解析失败。", "inference_output_failed", "error"),
    ("BEV_SPLATTING_FAILED", "Failed to run BEV splatting", "当前帧后处理失败：BEV 重建失败。", "bev_splatting_failed", "error"),
    ("LIDAR_TIMESTAMP_FAILED", "lidar_timestamp decode failed", "当前帧处理失败：雷达时间戳解析失败。", "lidar_timestamp_failed", "error"),
    ("PASSTHROUGH_COUNT_FAILED", "N_passthrough decode failed", "当前帧处理失败：透传点数解析失败。", "passthrough_count_failed", "error"),
    ("TRAJECTORY_META_FAILED", "traj_meta decode failed", "当前帧处理失败：轨迹元数据解析失败。", "trajectory_meta_failed", "error"),
    ("INFERENCE_FIELD_FAILED", "means3D decode failed", "当前帧处理失败：推理输出关键字段解析失败。", "inference_field_failed", "error"),
    ("EMPTY_BACK_TRAJECTORY", "RunSplattingBEV: N_back=0", "当前帧处理失败：后段轨迹为空。", "empty_back_trajectory", "error"),
    ("EMPTY_INFERENCE_DATA", "RunSplattingBEV: inference data empty", "当前帧处理失败：推理结果为空。", "empty_inference_data", "error"),
    ("BEV_RECONSTRUCTION_FAILED", "BEVSplatter failed:", "当前帧处理失败：BEV 重建模块执行异常。", "bev_reconstruction_failed", "error"),
    ("TRAJECTORY_THREAD_JITTER", "trajectory_thread severe jitter", "后处理轨迹线程抖动严重，可能影响结果稳定性。", "trajectory_thread_jitter", "warning"),
    ("TRAJECTORY_THREAD_OVERRUN", "trajectory_thread overrun", "后处理轨迹线程执行超时，存在跳拍风险。", "trajectory_thread_overrun", "warning"),
    ("POST_INPUT_QUEUE_FULL", "post_input queue full", "后处理输入发布队列已满，部分帧可能被丢弃。", "post_input_queue_full", "warning"),
    ("WHEEL_TRA_QUEUE_FULL", "wheel_tra queue full", "轮迹发布队列已满，部分消息可能被丢弃。", "wheel_tra_queue_full", "warning"),
    ("ODOM_DATA_MISSING", "No odom data yet, skipping publish", "当前尚未收到里程计数据，部分发布被跳过。", "odom_data_missing", "warning"),
    ("SUS_PREVIEW_PUBLISH_FAILED", "Failed to publish SusPreviewInfo", "悬架预览信息发布失败。", "sus_preview_publish_failed", "warning"),
    ("BACK_TRAJECTORY_MISMATCH", "Back trajectory size mismatch", "后段轨迹长度异常，结果可能不完整。", "back_trajectory_mismatch", "warning"),
    ("FRONT_TRAJECTORY_MISMATCH", "Front trajectory size mismatch", "前段轨迹长度异常，结果可能不完整。", "front_trajectory_mismatch", "warning"),
    ("VISUAL_HEADS_INCOMPLETE", "inference_visual_heads_ size=", "视觉头数据不完整，重建结果可能受影响。", "visual_heads_incomplete", "warning"),
    ("PC_TRAJECTORY_HEIGHT_LOW", "pc_traj_back_z too small", "点云轨迹高度信息不足，已跳过相关处理。", "pc_trajectory_height_low", "warning"),
)
LIDAR_VALID = re.compile(r"sensorName:\s*(?P<sensor>[A-Za-z0-9_]+_lidar).*?isValid:\s*(?P<valid>[01])")

def parse_timestamp(line: str) -> str | None:
    m = ACTUAL_TS.search(line)
    if m:
        return f"{m.group('date')}T{m.group('h')}:{m.group('m')}:{m.group('s')}.{m.group('ms')}+08:00"
    m = GLOG_TS.search(line)
    if m:
        return f"2026-{m.group('md')[:2]}-{m.group('md')[2:]}T{m.group('ts')}+08:00"
    return None


def display_timestamp(value: str | None) -> str | None:
    """Format timestamps for Chinese readers while preserving ISO internally."""
    if not value:
        return None
    match = re.search(r"T(\d{2}:\d{2}:\d{2}\.\d{3})", value)
    return match.group(1) if match else value


def timestamp_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp() * 1000
    except ValueError:
        return None


def epoch_us_iso(value: int) -> str:
    china_tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc).astimezone(china_tz).isoformat(timespec="milliseconds")


def duration_ms(start: str | None, end: str | None) -> float | None:
    start_ms = timestamp_ms(start)
    end_ms = timestamp_ms(end)
    if start_ms is None or end_ms is None or end_ms < start_ms:
        return None
    return round(end_ms - start_ms, 3)


def metric(values: list[float]) -> dict:
    return {
        "count": len(values), "sum": sum(values),
        "avg": statistics.mean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def level(line: str) -> str:
    m = LEVEL_RE.search(line)
    return {"I": "info", "W": "warning", "E": "error"}.get(m.group("level"), "info") if m else "info"

def stable_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def event(source: str, line_no: int, line: str, category: str, summary: str, frame=None, fields=None, reason=None):
    return {
        "eventId": f"{source}:{line_no}",
        "timestamp": parse_timestamp(line),
        "category": category,
        "level": level(line),
        "frame": frame,
        "reasonCode": reason,
        "fields": fields or {},
        "summary": summary,
        "evidence": {"sourceFile": source, "line": line_no, "rawExcerpt": line.strip()[:1000]},
    }

class CompactEvents(list):
    """Keep key events while aggregating high-frequency facts."""
    AGGREGATED = {
        "node_schedule", "graph_exec", "context_drop", "trajectory_data_timeout",
        "uroad_graph_overrun", "trajectory_generation", "lidar_invalid_data",
    }

    def __init__(self):
        super().__init__()
        self.stats: dict[str, dict] = {}

    def append(self, item):  # noqa: ANN001
        key = item.get("reasonCode") or item.get("category")
        if key not in self.AGGREGATED:
            super().append(item)
            return
        timestamp = item.get("timestamp")
        stat = self.stats.setdefault(key, {"count": 0, "firstTimestamp": timestamp, "lastTimestamp": timestamp, "samples": [], "eventIds": []})
        stat["count"] += 1
        stat["eventIds"].append(item.get("eventId"))
        if timestamp:
            stat["firstTimestamp"] = stat["firstTimestamp"] or timestamp
            stat["lastTimestamp"] = timestamp
        if len(stat["samples"]) < 3:
            stat["samples"].append({"eventId": item.get("eventId"), "timestamp": timestamp, "summary": item.get("summary"), "evidence": item.get("evidence")})
            super().append(item)

def select_key_events(events: list[dict]) -> list[dict]:
    """Keep requirement-defined timeline events and collapse repetitive records."""
    selected = []
    seen = set()
    categories = {"startup", "shutdown", "frame_start", "pre_timing", "post_start", "post_timing", "fps"}
    for item in events:
        category = item.get("category")
        source = item.get("evidence", {}).get("sourceFile")
        reason = item.get("reasonCode") or item.get("summary")
        if category in {"startup", "shutdown"}:
            key = (category, item.get("summary"))
        elif category in {"frame_start", "pre_timing", "post_start", "post_timing"}:
            key = (category, source)
        elif category == "fps":
            key = (category, source)
        elif category == "skip":
            key = (category, reason)
        elif item.get("level") == "error":
            key = ("error", reason)
        elif item.get("level") == "warning":
            key = ("warning", reason)
        else:
            continue
        if key not in seen:
            selected.append(item)
            seen.add(key)
    return selected


def candidate_paths(manifest: dict) -> list[Path]:
    base = Path(manifest["_manifestPath"]).parent
    extracted = manifest.get("extracted", {})
    names = extracted.get("files", [])
    paths = [base / "input" / name for name in names]
    if not paths:
        paths = [base / record["path"] for record in manifest.get("files", [])]
    return [p for p in paths if p.is_file()]

def parse_file(path: Path):
    events = CompactEvents()
    important_events = []
    pre_totals = []
    post_totals = []
    pre_series = []
    post_series = []
    fps_values = []
    trajectory_generation_ms = []
    graph_exec_ms = []
    graph_overrun_ms = []
    graph_overrun_stats = Counter()
    edsg_task_stats = Counter()
    edsg_frames = defaultdict(lambda: {"inferenceNodes": {}})
    inference_reports = []
    graph_runs = []
    frame_drop_stats = Counter()
    lidar_valid_stats = Counter()
    skip_reasons = Counter()
    issue_counts = Counter()
    frames = defaultdict(lambda: {"frame": None, "status": "incomplete", "events": [], "evidence": []})
    active_frame_key = None
    active_post_frame_key = None
    frame_sequence = 0
    startup_seen = False
    shutdown_seen = False
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            line_count = line_no
            is_uroad = "[uroad]" in line or any(k in line for k in (
                "chassis_uroad_graph", "UroadPreNode", "UroadPostNode", "UnifiedInferenceNode", "PostprocessManager",
                "[PointCloudProcessor]", "[ImageProcessor]", "DdsListener:", "[SusPreviewInfo]",
                "ExtractInferenceOutput", "RunSplattingBEV", "BEVSplatter", "TrajectoryGeneration",
                "GraphName:", "graph:", "FrameControl:", "_lidar",
            ))
            if not is_uroad:
                continue
            source = path.name
            if any(k in line for k in ("UroadPreNode construct", "UroadPostNode construct", "PostprocessManager: Initialized successfully")):
                startup_seen = True
                event_item = event(source, line_no, line, "startup", "uroad 运行组件已初始化。")
                events.append(event_item)
                important_events.append(event_item)
                continue
            if any(k in line for k in ("Stop lidar", "UroadPreNode destruct", "UroadPostNode destruct", "PostprocessManager: Destructed")):
                shutdown_seen = True
                event_item = event(source, line_no, line, "shutdown", "uroad 运行组件正在退出。")
                events.append(event_item)
                important_events.append(event_item)
                continue
            m = EDSG_TASK.search(line)
            if m and m.group("task").startswith(("UroadPreNode_", "UroadPostNode_", "UnifiedInferenceNode_")):
                task = m.group("task")
                action = m.group("action")
                ctx = int(m.group("ctx"))
                frame_id = int(m.group("frame_id"))
                timestamp = parse_timestamp(line)
                frame = edsg_frames[frame_id]
                frame["edsgFrameId"] = frame_id
                if task.startswith("UroadPreNode_"):
                    kind = "pre"
                    frame["ctx"] = ctx
                    frame[f"pre{action.title()}Timestamp"] = timestamp
                    if m.group("duration_us"):
                        frame["preDurationMs"] = int(m.group("duration_us")) / 1000
                elif task.startswith("UroadPostNode_"):
                    kind = "post"
                    frame["ctx"] = ctx
                    frame[f"post{action.title()}Timestamp"] = timestamp
                    if m.group("duration_us"):
                        frame["postDurationMs"] = int(m.group("duration_us")) / 1000
                else:
                    kind = "inference"
                    node = frame["inferenceNodes"].setdefault(task, {"node": task, "ctx": ctx})
                    node[f"{action}Timestamp"] = timestamp
                    if m.group("duration_us"):
                        node["durationMs"] = int(m.group("duration_us")) / 1000
                edsg_task_stats[f"{kind}_{action}"] += 1
                fields = {"node": kind, "action": action, "task": task, "ctx": ctx, "frameId": frame_id}
                if m.group("duration_us"):
                    fields["durationUs"] = int(m.group("duration_us"))
                events.append(event(source, line_no, line, "node_schedule", f"{kind} 节点被 EDSG {action} 调度。", fields=fields, reason=f"{kind}_node_{action}"))
                continue
            m = INFERENCE_REPORT.search(line)
            if m:
                inference_reports.append({
                    "node": m.group("node"),
                    "ctx": int(m.group("ctx")),
                    "value": int(m.group("ms")) + int(m.group("us")) / 1000,
                    "ts": parse_timestamp(line),
                    "sourceFile": source,
                    "line": line_no,
                })
                continue
            m = FRAME_DROP.search(line)
            if m:
                frame_drop_stats[m.group("graph")] += 1
                events.append(event(source, line_no, line, "context_drop", f"调度图 {m.group('graph')} 丢弃了一个处理 context。", fields={"graph": m.group("graph")}, reason="context_drop"))
                continue
            m = LIDAR_VALID.search(line)
            if m:
                sensor = m.group("sensor")
                valid = m.group("valid")
                lidar_valid_stats[f"{sensor}_isValid_{valid}"] += 1
                if valid == "0":
                    events.append(event(source, line_no, line, "warning", f"{sensor} 数据有效性校验失败。", fields={"sensor": sensor, "isValid": 0}, reason="lidar_invalid_data"))
                continue
            matched_issue = False
            for issue_code, pattern, summary, reason_code, severity in ISSUE_RULES:
                if pattern in line:
                    issue_counts[issue_code] += 1
                    events.append(event(source, line_no, line, "error" if severity == "error" else "warning", summary, reason=reason_code))
                    matched_issue = True
                    break
            if matched_issue:
                continue
            m = TRAJECTORY_GEN.search(line)
            if m:
                value = float(m.group("ms"))
                trajectory_generation_ms.append(value)
                events.append(event(source, line_no, line, "performance", f"轨迹生成耗时 {value:.2f} ms。", fields={"trajectoryGenerationMs": value}, reason="trajectory_generation"))
                continue
            m = GRAPH_EXEC.search(line)
            if m:
                value = float(m.group("ms"))
                graph_exec_ms.append(value)
                events.append(event(source, line_no, line, "performance", f"调度图 {m.group('graph')} 执行耗时 {value:.0f} ms。", fields={"graph": m.group("graph"), "execMs": value}, reason="graph_exec"))
                continue
            m = PRE_HEAD.search(line)
            if m:
                frame = int(m.group("frame"))
                raw_gear = m.group("gear")
                if frame == 0 and active_frame_key is not None:
                    frame_sequence += 1
                active_frame_key = (frame, frame_sequence)
                frame_key = active_frame_key
                frames[frame_key].update({"frame": frame, "sequence": frame_sequence, "speedKph": float(m.group("speed")), "gear": raw_gear})
                frames[frame_key]["events"].append(f"{source}:{line_no}")
                frames[frame_key]["evidence"].append(line_no)
                event_item = event(source, line_no, line, "frame_start", f"第 {frame} 帧开始处理，当前车速 {float(m.group('speed')):.1f} kph，挡位 {raw_gear}。", frame, {"speedKph": float(m.group("speed")), "gear": raw_gear, "sequence": frame_key[1]})
                events.append(event_item)
                important_events.append(event_item)
            m = PRE_TIMING.search(line)
            if m:
                frame, total = int(m.group("frame")), float(m.group("total"))
                frame_key = active_frame_key if active_frame_key is not None else (frame, frame_sequence)
                pre_totals.append(total)
                pre_series.append({"ts": parse_timestamp(line), "value": total, "applicationFrame": frame, "source": "UroadPreNode timing TOTAL", "confidence": "high", "sourceFile": source, "line": line_no})
                frames[frame_key].update({"frame": frame, "sequence": frame_key[1], "preTotalMs": total, "preTimingTimestamp": parse_timestamp(line)})
                frames[frame_key]["events"].append(f"{source}:{line_no}")
                frames[frame_key]["evidence"].append(line_no)
                event_item = event(source, line_no, line, "pre_timing", f"第 {frame} 帧前处理完成，总耗时 {total:.2f} ms。", frame, {"totalMs": total})
                events.append(event_item)
                important_events.append(event_item)
                continue
            m = POST_TIMING.search(line)
            if m:
                frame, total = int(m.group("frame")), float(m.group("total"))
                post_key = ("post", frame)
                post_totals.append(total)
                post_series.append({"ts": parse_timestamp(line), "value": total, "applicationFrame": frame, "source": "UroadPostNode timing TOTAL", "confidence": "high", "sourceFile": source, "line": line_no})
                if active_post_frame_key is not None:
                    frames[active_post_frame_key].update({"postTotalMs": total, "postTimingTimestamp": parse_timestamp(line)})
                    frames[active_post_frame_key]["events"].append(f"{source}:{line_no}")
                    frames[active_post_frame_key]["evidence"].append(line_no)
                event_item = event(source, line_no, line, "post_timing", f"PostNode 应用帧 {frame} 后处理完成，总耗时 {total:.2f} ms。", frame, {"totalMs": total, "frameNamespace": "post"})
                events.append(event_item)
                important_events.append(event_item)
                continue
            m = POST_HEAD.search(line)
            if m:
                frame = int(m.group("frame"))
                matching_pre_key = next((key for key, value in reversed(list(frames.items())) if value.get("frame") == frame and value.get("frameNamespace") != "post"), None)
                frame_key = matching_pre_key or ("post", frame)
                if matching_pre_key is None:
                    frames[frame_key].update({"frameNamespace": "post"})
                frames[frame_key].update({"frame": frame, "sequence": frames[frame_key].get("sequence"), "postStartSeen": True, "postStartTimestamp": parse_timestamp(line)})
                frames[frame_key]["events"].append(f"{source}:{line_no}")
                frames[frame_key]["evidence"].append(line_no)
                active_post_frame_key = frame_key
                event_item = event(source, line_no, line, "post_start", f"应用帧 {frame} 进入后处理阶段。", frame)
                events.append(event_item)
                important_events.append(event_item)
                continue
            m = FPS_RE.search(line)
            if m:
                value = float(m.group("fps")); fps_values.append(value)
                event_item = event(source, line_no, line, "fps", f"最近 10 帧平均帧率为 {value:.2f} Hz。", fields={"fps": value})
                events.append(event_item)
                important_events.append(event_item)
                continue
            m = POINTS_RE.search(line)
            if m:
                frame, points = int(m.group("frame")), int(m.group("points"))
                frame_key = active_frame_key if active_frame_key is not None else (frame, frame_sequence)
                skip_reasons["trajectory_too_few_points"] += 1; issue_counts["TRAJECTORY_TOO_FEW_POINTS"] += 1
                frames[frame_key].update({"frame": frame, "sequence": frame_key[1], "status": "skipped", "skipReason": "trajectory_too_few_points"})
                event_item = event(source, line_no, line, "skip", f"第 {frame} 帧被跳过：轨迹点数量不足。", frame, {"points": points, "sequence": frame_key[1]}, "trajectory_too_few_points")
                events.append(event_item)
                important_events.append(event_item)
                continue
            m = SKIP_REASON.search(line)
            if m:
                reason = m.group("reason"); skip_reasons[reason] += 1
                frame_m = SKIP_FRAME.search(line); frame = int(frame_m.group("frame")) if frame_m else None
                texts = {"gear_gate": "当前不是有效前进挡，uroad 暂停处理。", "speed_gate": "当前车速不满足 uroad 运行条件。"}
                summary = f"第 {frame} 帧被跳过：{texts.get(reason, f'触发 {reason} 条件。')}" if frame is not None else f"uroad 触发 {reason} 条件。"
                if frame is not None:
                    frame_key = active_frame_key if active_frame_key is not None else (frame, frame_sequence)
                    frames[frame_key].update({"frame": frame, "sequence": frame_key[1], "status": "skipped", "skipReason": reason})
                event_item = event(source, line_no, line, "skip", summary, frame, reason=reason)
                events.append(event_item)
                important_events.append(event_item)
                continue
            if "超速抑制中" in line:
                issue_counts["SPEED_GATE_SUPPRESSION"] += 1
                frame_m = SKIP_FRAME.search(line)
                frame = int(frame_m.group("frame")) if frame_m else None
                if frame is not None:
                    frame_key = active_frame_key if active_frame_key is not None else (frame, frame_sequence)
                    frames[frame_key].update({"frame": frame, "sequence": frame_key[1], "status": "skipped", "skipReason": "speed_gate"})
                summary = f"第 {frame} 帧已跳过：当前车速超出 uroad 工作范围。" if frame is not None else "当前车速超出 uroad 工作范围，处理被抑制。"
                event_item = event(source, line_no, line, "skip", summary, frame, reason="speed_gate")
                events.append(event_item)
                important_events.append(event_item)
                skip_reasons["speed_gate"] += 1
                continue
            if "Camera image is empty" in line:
                issue_counts["CAMERA_IMAGE_EMPTY"] += 1
                event_item = event(source, line_no, line, "skip", "本帧被跳过：相机图像为空。", reason="camera_image_empty")
                events.append(event_item)
                important_events.append(event_item)
                continue
            if "Trajectory data timeout" in line:
                issue_counts["TRAJECTORY_DATA_TIMEOUT"] += 1
                events.append(event(source, line_no, line, "warning", "轨迹数据获取超时，当前发布可能被跳过。", reason="trajectory_data_timeout"))
                continue
            if "temporal gap exceeded" in line:
                issue_counts["TEMPORAL_GAP"] += 1
                events.append(event(source, line_no, line, "warning", "检测到较大的时间间隔，uroad 进入冷启动。", fields={"raw": line.strip()}, reason="temporal_gap"))
                continue
            if "TrajectoryProcess" in line and "SKIP" in line:
                issue_counts["TRAJECTORY_PROCESS_SKIP"] += 1
                events.append(event(source, line_no, line, "warning", "轨迹处理跳过了部分车轮结果。", reason="trajectory_process_skip"))
                continue
            if "Input BumpPoint is nullptr" in line:
                issue_counts["BUMPOINT_NULL"] += 1
                events.append(event(source, line_no, line, "warning", "地图融合输入缺少 BumpPoint，相关处理可能不完整。", reason="bumppoint_null"))
                continue
            m = GRAPH_OVERRUN.search(line)
            if m:
                ms, limit = int(m.group("ms")), int(m.group("limit"))
                graph = m.group("graph").strip()
                start_us = int(m.group("start"))
                end_us = int(m.group("end"))
                graph_overrun_ms.append(float(ms))
                graph_overrun_stats[graph] += 1
                graph_runs.append({
                    "graph": graph,
                    "ctx": int(m.group("ctx")),
                    "startTimeUs": start_us,
                    "endTimeUs": end_us,
                    "ts": epoch_us_iso(start_us),
                    "value": float(ms),
                    "limitMs": limit,
                    "sourceFile": source,
                    "line": line_no,
                })
                issue_counts["UROAD_GRAPH_OVERRUN"] += 1
                events.append(event(source, line_no, line, "performance", f"调度图 {graph} 执行超时：实际耗时 {ms} ms，超过 {limit} ms 上限。", fields={"graph": graph, "contextIdx": int(m.group("ctx")), "startTimeUs": start_us, "endTimeUs": end_us, "runningMs": ms, "limitMs": limit}, reason="uroad_graph_overrun"))
    return events, pre_totals, post_totals, pre_series, post_series, fps_values, skip_reasons, issue_counts, frames, line_count, startup_seen, shutdown_seen, trajectory_generation_ms, graph_exec_ms, graph_overrun_ms, graph_overrun_stats, edsg_task_stats, edsg_frames, inference_reports, graph_runs, frame_drop_stats, lidar_valid_stats

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--can-signals", help="parse_can_signals.py 生成的 JSON")
    args = parser.parse_args()
    can_signals = None
    if args.can_signals:
        can_signals_path = Path(args.can_signals).resolve()
        can_signals = json.loads(can_signals_path.read_text(encoding="utf-8"))
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["_manifestPath"] = str(manifest_path)
    output = Path(args.output_dir).resolve(); output.mkdir(parents=True, exist_ok=True)
    all_events=[]; event_stats={}; pre=[]; post=[]; pre_points=[]; post_points=[]; fps=[]; trajectory_generation=[]; graph_exec=[]; graph_overrun=[]; graph_overruns=Counter(); edsg_tasks=Counter(); edsg_frame_data={}; inference_reports=[]; graph_runs=[]; frame_drops=Counter(); lidar_valid=Counter(); skip=Counter(); issues=Counter(); frame_data={}; total_lines=0; files=[]
    startup_seen = False
    shutdown_seen = False
    frame_counter = 0
    for path in candidate_paths(manifest):
        files.append(path.name)
        result = parse_file(path)
        events, p, q, pre_file_points, post_file_points, f, s, i, frames, lines, startup, shutdown, trajectory_ms, exec_ms, overrun_ms, overrun_stats, task_stats, edsg_frames, infer_reports, file_graph_runs, drop_stats, lidar_stats = result
        startup_seen = startup_seen or startup
        shutdown_seen = shutdown_seen or shutdown
        for stat_key, stat_value in events.stats.items():
            aggregate = event_stats.setdefault(stat_key, {"count": 0, "firstTimestamp": stat_value.get("firstTimestamp"), "lastTimestamp": stat_value.get("lastTimestamp"), "samples": []})
            aggregate["count"] += stat_value.get("count", 0)
            aggregate["firstTimestamp"] = aggregate["firstTimestamp"] or stat_value.get("firstTimestamp")
            aggregate["lastTimestamp"] = stat_value.get("lastTimestamp") or aggregate["lastTimestamp"]
            aggregate["samples"] = (aggregate["samples"] + stat_value.get("samples", []))[:3]
        all_events.extend(events); pre.extend(p); post.extend(q); pre_points.extend(pre_file_points); post_points.extend(post_file_points); fps.extend(f); trajectory_generation.extend(trajectory_ms); graph_exec.extend(exec_ms); graph_overrun.extend(overrun_ms); graph_overruns.update(overrun_stats); edsg_tasks.update(task_stats); inference_reports.extend(infer_reports); graph_runs.extend(file_graph_runs); frame_drops.update(drop_stats); lidar_valid.update(lidar_stats); skip.update(s); issues.update(i); total_lines += lines
        for edsg_frame_id, edsg_value in edsg_frames.items():
            edsg_frame_data[(path.name, edsg_frame_id)] = edsg_value
        for key, value in frames.items():
            if isinstance(key, tuple) and key and key[0] == "post":
                composite_key = ("post", path.name, key[1])
                if composite_key not in frame_data:
                    frame_data[composite_key] = value
                else:
                    frame_data[composite_key].update({k: v for k, v in value.items() if v is not None})
                continue
            original_frame = key[0] if isinstance(key, tuple) else key
            local_sequence = key[1] if isinstance(key, tuple) else 0
            composite_key = (original_frame, local_sequence + frame_counter)
            if composite_key not in frame_data:
                frame_data[composite_key] = value
            else:
                frame_data[composite_key].update({k: v for k, v in value.items() if v is not None})
        numeric_sequences = [key[1] for key in frames if isinstance(key, tuple) and isinstance(key[0], int)]
        frame_counter += max(numeric_sequences, default=-1) + 1

    for points in (pre_points, post_points):
        points.sort(key=lambda item: (item.get("ts") or "", item.get("sourceFile") or "", item.get("line") or 0))
        for sequence, point in enumerate(points, 1):
            point["sequence"] = sequence

    inference_series = []
    edsg_cycles = []
    inference_node_values = defaultdict(list)
    matched_reports = 0
    for (source_file, edsg_frame_id), data in sorted(edsg_frame_data.items(), key=lambda item: (item[1].get("preActiveTimestamp") or "", item[0])):
        pre_end = data.get("preDeactiveTimestamp")
        post_start = data.get("postActiveTimestamp")
        infer_ms = duration_ms(pre_end, post_start)
        cycle = {
            "sourceFile": source_file,
            "edsgFrameId": edsg_frame_id,
            "ctx": data.get("ctx"),
            "preActiveTimestamp": data.get("preActiveTimestamp"),
            "preDeactiveTimestamp": pre_end,
            "postActiveTimestamp": post_start,
            "postDeactiveTimestamp": data.get("postDeactiveTimestamp"),
            "inferenceNodes": list(data.get("inferenceNodes", {}).values()),
        }
        if infer_ms is not None:
            cycle["inferChainMs"] = infer_ms
            inference_series.append({
                "ts": pre_end,
                "value": infer_ms,
                "edsgFrameId": edsg_frame_id,
                "ctx": data.get("ctx"),
                "source": "EDSG PreNode deactive → PostNode active",
                "confidence": "high",
            })
        window_start = timestamp_ms(pre_end)
        window_end = timestamp_ms(post_start)
        for report_item in inference_reports:
            report_ts = timestamp_ms(report_item.get("ts"))
            if report_item.get("sourceFile") != source_file or report_item.get("ctx") != data.get("ctx"):
                continue
            if window_start is None or window_end is None or report_ts is None or not window_start <= report_ts <= window_end:
                continue
            node = data.get("inferenceNodes", {}).get(report_item["node"])
            if node is None:
                continue
            node["reportedInferMs"] = report_item["value"]
            node["reportedTimestamp"] = report_item["ts"]
            inference_node_values[report_item["node"]].append(report_item["value"])
            matched_reports += 1
        edsg_cycles.append(cycle)
    inference_series.sort(key=lambda item: (item.get("ts") or "", item.get("edsgFrameId") or 0))
    for sequence, point in enumerate(inference_series, 1):
        point["sequence"] = sequence

    chassis_runs = sorted((item for item in graph_runs if item.get("graph") == "chassis_uroad_graph"), key=lambda item: item["startTimeUs"])
    e2e_series = []
    for sequence, item in enumerate(chassis_runs, 1):
        e2e_series.append({
            "ts": item["ts"],
            "sequence": sequence,
            "value": item["value"],
            "ctx": item["ctx"],
            "source": "chassis_uroad_graph running time",
            "confidence": "high",
            "coverage": "overrun_only",
            "sourceFile": item["sourceFile"],
            "line": item["line"],
        })
    graph_e2e_values = [item["value"] for item in e2e_series]

    frame_list = []
    sorted_frame_data = sorted(frame_data.items(), key=lambda item: (item[1].get("preTimingTimestamp") or item[1].get("postTimingTimestamp") or "", str(item[0])))
    for cycle_index, (frame_key, data) in enumerate(sorted_frame_data, 1):
        has_pre = data.get("preTotalMs") is not None; has_post = data.get("postTotalMs") is not None
        if has_pre or has_post:
            data["displaySequence"] = cycle_index
        if has_pre and has_post:
            data["status"] = "normal" if data.get("status") != "skipped" else data["status"]
        if data.get("preTimingTimestamp") and data.get("postStartTimestamp"):
            try:
                start = datetime.fromisoformat(data["preTimingTimestamp"])
                end = datetime.fromisoformat(data["postStartTimestamp"])
                data["inferMs"] = round((end - start).total_seconds() * 1000, 3)
                data["inferSource"] = "application_frame_marker"
                data["inferConfidence"] = "high"
                if has_pre and has_post:
                    data["e2eMs"] = round(data["preTotalMs"] + data["inferMs"] + data["postTotalMs"], 3)
                    data["e2eSource"] = "application_stage_sum"
                    data["e2eConfidence"] = "high"
            except ValueError:
                data["timingStatus"] = "unavailable"
        else:
            data["timingStatus"] = "unavailable"
        if data.get("status") == "skipped":
            reason_text = {
                "speed_gate": "当前车速超出 uroad 工作范围",
                "perception_gate": "当前感知状态不满足运行条件",
                "apa_gate": "当前处于泊车模式",
                "gear_gate": "当前不是 D 挡",
                "camera_image_empty": "相机图像为空",
                "trajectory_too_few_points": "轨迹点数量不足",
                "image_processing_failed": "图像处理失败",
                "point_cloud_too_few_points": "点云有效点数不足",
            }.get(data.get("skipReason"), data.get("skipReason") or "触发跳帧条件")
            data["humanSummary"] = f"第 {cycle_index} 个处理记录（{data.get('frameNamespace', 'pre')} 应用帧 {data.get('frame')}）已跳过：{reason_text}。"
        elif has_pre and has_post and data.get("inferMs") is not None and data.get("e2eMs") is not None:
            data["humanSummary"] = (
                f"第 {cycle_index} 个处理记录（应用帧 {data.get('frame')}）处理完成，前处理 {data['preTotalMs']:.2f} ms，"
                f"推理 {data['inferMs']:.2f} ms，后处理 {data['postTotalMs']:.2f} ms，端到端 {data['e2eMs']:.2f} ms。"
            )
        else:
            available = []
            if has_pre:
                available.append(f"前处理 {data['preTotalMs']:.2f} ms")
            if has_post:
                available.append(f"后处理 {data['postTotalMs']:.2f} ms")
            detail = f"，已识别{'、'.join(available)}" if available else ""
            namespace = "PostNode" if data.get("frameNamespace") == "post" else "PreNode"
            data["humanSummary"] = f"第 {cycle_index} 个处理记录（{namespace} 应用帧 {data.get('frame')}）记录不完整{detail}。"
        frame_list.append(data)
    issues_out=[]
    if frame_drops:
        issues["EDSG_CONTEXT_DROP"] = sum(frame_drops.values())
    issue_text={
      "EMPTY_POINT_CLOUD":"当前雷达点云为空，相关 uroad 处理可能被跳过。",
      "EXPECTED_INPUT_COUNT":"当前帧后处理失败：收到的输入数量不正确。",
      "UROAD_GRAPH_OVERRUN":"uroad 调度图执行时间超过配置上限，可能导致处理延迟或跳拍。",
      "BUMPOINT_NULL":"地图融合输入缺少 BumpPoint，相关处理结果可能不完整。",
      "EDSG_CONTEXT_DROP":"EDSG 调度图丢弃处理 context，可能造成对应图跳拍；需按 graph 分布确认是否影响 uroad 主链路。",
      "TRAJECTORY_TOO_FEW_POINTS":"多次出现轨迹点数量不足，导致前处理跳帧。",
      "CAMERA_IMAGE_EMPTY":"出现相机图像为空，导致本帧跳过。",
      "TRAJECTORY_DATA_TIMEOUT":"出现轨迹数据超时，发布可能被跳过。",
      "TEMPORAL_GAP":"出现较大时间间隔并触发冷启动。",
      "TRAJECTORY_PROCESS_SKIP":"轨迹处理跳过部分车轮结果。",
      "PC_TRAJECTORY_HEIGHT_LOW":"点云轨迹高度信息不足，已跳过相关处理。",
    }
    reason_codes = {
      "UROAD_GRAPH_OVERRUN": {"uroad_graph_overrun"},
      "TRAJECTORY_TOO_FEW_POINTS": {"trajectory_too_few_points"},
      "CAMERA_IMAGE_EMPTY": {"camera_image_empty"},
      "TRAJECTORY_DATA_TIMEOUT": {"trajectory_data_timeout"},
      "TEMPORAL_GAP": {"temporal_gap"},
      "TRAJECTORY_PROCESS_SKIP": {"trajectory_process_skip"},
      "SPEED_GATE_SUPPRESSION": {"speed_gate"},
      "BUMPOINT_NULL": {"bumppoint_null"},
      "EDSG_CONTEXT_DROP": {"context_drop"},
    }
    for code, count in issues.items():
        refs = [e["eventId"] for e in all_events if e.get("reasonCode") in reason_codes.get(code, set())]
        issue_item = {"issueCode": code, "severity": "high" if code == "UROAD_GRAPH_OVERRUN" else "warning", "confidence": "high", "count": count, "fact": issue_text.get(code, code), "impact": None, "impactConfidence": "unavailable", "evidenceRefs": refs, "needsManualReview": True}
        if code == "EDSG_CONTEXT_DROP":
            issue_item["breakdown"] = dict(frame_drops.most_common())
        if code == "UROAD_GRAPH_OVERRUN":
            issue_item["breakdown"] = dict(graph_overruns.most_common())
        issues_out.append(issue_item)

    normal_frames = sum(1 for item in frame_list if item.get("status") == "normal")
    skipped_frames = sum(1 for item in frame_list if item.get("status") == "skipped")
    error_frames = sum(1 for item in frame_list if item.get("status") == "error")
    application_e2e_values = [item["e2eMs"] for item in frame_list if item.get("e2eMs") is not None]
    avg_e2e = statistics.mean(graph_e2e_values) if graph_e2e_values else statistics.mean(application_e2e_values) if application_e2e_values else None
    avg_e2e_coverage = "overrun_only" if graph_e2e_values else "all_correlated_cycles" if application_e2e_values else "unavailable"
    error_reason_stats = Counter({"uroad_graph_overrun": len(graph_overrun)})
    for code, count in issues.items():
        if code == "UROAD_GRAPH_OVERRUN":
            continue
        if code == "EDSG_CONTEXT_DROP":
            error_reason_stats["edsg_context_drop"] = count
        elif code in reason_codes:
            reason = next(iter(reason_codes[code]), code.lower())
            error_reason_stats[reason] += count
    has_runtime_evidence = bool(frame_list or pre or post or fps or all_events)
    if not startup_seen and not has_runtime_evidence:
        running_status, startup_status = "not_started", "unknown"
    elif error_frames:
        running_status, startup_status = "error", "success" if startup_seen else "unknown"
    elif issues or skipped_frames or not startup_seen:
        running_status, startup_status = "warning", "success" if startup_seen else "unknown"
    else:
        running_status, startup_status = "healthy", "success"
    main_issue = max(issues_out, key=lambda item: (item.get("severity") == "high", item.get("count", 0))).get("fact") if issues_out else None
    summary = {
        "title": "uroad 运行分析结果",
        "description": f"uroad 已识别 {len(frame_list)} 个应用处理记录，其中 {len(pre)} 条前处理耗时、{len(post)} 条后处理耗时；{skipped_frames} 个记录关联为跳过、{error_frames} 个记录关联为异常。",
        "runningStatus": running_status,
        "startupStatus": startup_status,
        "totalFrames": len(frame_list),
        "normalFrames": normal_frames,
        "skippedFrames": skipped_frames,
        "errorFrames": error_frames,
        "latestFps": fps[-1] if fps else None,
        "avgE2EMs": avg_e2e,
        "avgE2ECoverage": avg_e2e_coverage,
        "skipEventCount": sum(skip.values()),
        "associatedSkippedCycles": skipped_frames,
        "mainIssue": main_issue,
    }
    timeline = [{"ts": display_timestamp(item.get("timestamp")), "level": item.get("level"), "category": item.get("category"), "title": item.get("summary"), "description": item.get("summary"), "eventId": item.get("eventId"), "raw": item.get("evidence", {}).get("rawExcerpt")} for item in select_key_events(all_events)]
    fallback_inference = [
        {"ts": item.get("preTimingTimestamp"), "sequence": index, "value": item["inferMs"], "applicationFrame": item.get("frame"), "source": "application Pre timing → Post start", "confidence": "high"}
        for index, item in enumerate((item for item in frame_list if item.get("inferMs") is not None), 1)
    ]
    fallback_e2e = [
        {"ts": item.get("preTimingTimestamp"), "sequence": index, "value": item["e2eMs"], "applicationFrame": item.get("frame"), "source": "application stage sum", "confidence": "high", "coverage": "all_correlated_cycles"}
        for index, item in enumerate((item for item in frame_list if item.get("e2eMs") is not None), 1)
    ]
    final_inference_series = inference_series or fallback_inference
    final_e2e_series = e2e_series or fallback_e2e
    series_meta = {
        "preCostSeries": {"label": "前处理耗时", "definition": "UroadPreNode timing TOTAL", "source": "application_timing", "confidence": "high", "coverage": "observed_samples"},
        "inferCostSeries": {"label": "推理链路耗时", "definition": "同一 EDSG frameId 的 PreNode deactive 到 PostNode active；包含调度等待，并行推理头不求和。" if inference_series else "应用层 Pre timing 到 Post start。", "source": "edsg_boundaries" if inference_series else "application_markers", "confidence": "high", "coverage": "matched_edsg_cycles" if inference_series else "matched_application_cycles"},
        "postCostSeries": {"label": "后处理耗时", "definition": "UroadPostNode timing TOTAL", "source": "application_timing", "confidence": "high", "coverage": "observed_samples"},
        "e2eCostSeries": {"label": "端到端耗时", "definition": "chassis_uroad_graph running time；当前格式仅在超过阈值时输出。" if e2e_series else "应用层前处理、推理、后处理之和。", "source": "chassis_uroad_graph" if e2e_series else "application_stage_sum", "confidence": "high", "coverage": "overrun_only" if e2e_series else "matched_application_cycles"},
    }
    charts = {
        "preCostSeries": pre_points,
        "inferCostSeries": final_inference_series,
        "postCostSeries": post_points,
        "e2eCostSeries": final_e2e_series,
        "seriesMeta": series_meta,
        "fpsSeries": [{"ts": item.get("timestamp"), "value": item.get("fields", {}).get("fps")} for item in all_events if item.get("category") == "fps"],
        "skipReasonStats": [{"reason": key, "count": value} for key, value in skip.items()],
        "errorReasonStats": [{"reason": key, "count": value} for key, value in error_reason_stats.items()],
    }
    if can_signals:
        charts["canSignals"] = can_signals
    report = {
      "schemaVersion":"1.0", "runId":manifest.get("runId"),
      "source":{"manifestPath":str(manifest_path),"manifestSha256":stable_hash(manifest_path),"parserVersion":"0.4.0","ruleVersion":"uroad-rules-0.4","files":files,"query":manifest.get("query", {})},
      "completeness":{"startup":"detected" if startup_seen else "unknown","shutdown":"detected" if shutdown_seen else "unknown","frameCorrelation":"edsg_frame_id" if inference_series else "partial","inferenceTiming":"available" if final_inference_series else "unavailable","e2eTiming":"partial" if e2e_series else "available" if fallback_e2e else "unavailable","canSignals":"available" if can_signals and all(signal.get("series") for signal in can_signals.get("signals", {}).values()) else "partial" if can_signals else "unavailable","warnings":(["端到端数据来自 chassis_uroad_graph 超时日志，仅覆盖超过阈值的样本，不能代表全部周期。"] if e2e_series else []) + ([] if final_inference_series else ["当前输入缺少可可靠关联的推理边界，未计算推理耗时。"]) + (can_signals.get("warnings", []) if can_signals else ["当前未提供 CAN 信号数据，车辆信号时间曲线不可用。"])},
      "summary":summary,
      "timeline":timeline,
      "charts":charts,
      "frames":frame_list,
      "summaryFacts":[f"识别到 {len(frame_list)} 个带 frame 的事件记录。",f"识别到 {len(all_events)} 条 uroad/关联事件。"],
      "eventStats":event_stats,
      "events":all_events,
      "edsgCycles": edsg_cycles,
      "inferenceNodes": {node: metric(values) for node, values in sorted(inference_node_values.items())},
      "metrics":{"totalLinesScanned":total_lines,"eventCount":len(all_events),"frameCount":len(frame_list),"edsgTaskStats":dict(edsg_tasks),"edsgCycleCount":len(edsg_cycles),"edsgInferenceReportMatches":matched_reports,"frameDropStats":dict(frame_drops),"frameDropCount":sum(frame_drops.values()),"lidarValidityStats":dict(lidar_valid),"preTimingCount":len(pre),"postTimingCount":len(post),"inferenceChainMs":metric([item["value"] for item in final_inference_series]),"e2eMs":{**metric([item["value"] for item in final_e2e_series]),"coverage":avg_e2e_coverage},"skipEventCount":sum(skip.values()),"associatedSkippedCycles":skipped_frames,"trajectoryGenerationMs":metric(trajectory_generation),"graphExecMs":metric(graph_exec),"graphOverrunCount":len(graph_overrun),"graphOverrunStats":dict(graph_overruns),"chassisGraphOverrunCount":graph_overruns.get("chassis_uroad_graph",0),"preTotalMs":metric(pre),"postTotalMs":metric(post),"fps":{"count":len(fps),"latest":fps[-1] if fps else None,"min":min(fps) if fps else None,"max":max(fps) if fps else None},"skipReasonStats":dict(skip),"errorReasonStats":dict(error_reason_stats)},
      "issues":issues_out,
      "evidence":{"sourceFiles":files,"lineBased":True}
    }
    out_path=output/"analysis.json"; temp=out_path.with_suffix(".json.tmp"); temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); temp.replace(out_path)
    print(json.dumps({"ok":True,"analysis":str(out_path),"events":len(all_events),"frames":len(frame_list),"issues":len(issues_out)},ensure_ascii=False,indent=2))
    return 0

if __name__ == "__main__": raise SystemExit(main())
