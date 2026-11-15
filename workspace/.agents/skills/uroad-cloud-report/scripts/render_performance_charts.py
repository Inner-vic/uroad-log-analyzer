#!/usr/bin/env python3
"""Render uroad duration, FPS, and reason-distribution SVG charts."""
from __future__ import annotations

import argparse
import html
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reason_labels import normalize_reason_code, reason_label

WIDTH = 1200
LEFT = 92
RIGHT = 38
PLOT_WIDTH = WIDTH - LEFT - RIGHT
CST = timezone(timedelta(hours=8))
SERIES = (
    ("preCostSeries", "前处理耗时", "#2563eb"),
    ("inferCostSeries", "推理链路耗时", "#f59e0b"),
    ("postCostSeries", "后处理耗时", "#16a34a"),
    ("e2eCostSeries", "端到端耗时", "#dc2626"),
)


def finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clean_series(series: list[dict], x_key: str) -> list[dict]:
    clean = []
    for point in series:
        value = finite(point.get("value"))
        x = point.get(x_key)
        if value is None or x is None:
            continue
        clean.append({"x": x, "value": value, "index": len(clean)})
    return clean


def duration_points(series: list[dict]) -> tuple[list[dict], str]:
    timed = []
    for point in series:
        value = finite(point.get("value"))
        epoch = parse_epoch(point.get("ts"))
        if value is None or epoch is None:
            continue
        timed.append({"x": point.get("ts"), "epoch": epoch, "value": value})
    if timed:
        timed.sort(key=lambda item: item["epoch"])
        for index, point in enumerate(timed):
            point["index"] = index
        return timed, "time"
    sequence = clean_series(series, "sequence")
    return sequence, "sequence"


def downsample_extrema(points: list[dict], max_points: int = 1400) -> list[dict]:
    if len(points) <= max_points:
        return points
    bucket_count = max(1, (max_points - 2) // 2)
    selected = [points[0]]
    interior = points[1:-1]
    for bucket in range(bucket_count):
        start = bucket * len(interior) // bucket_count
        end = (bucket + 1) * len(interior) // bucket_count
        values = interior[start:end]
        if not values:
            continue
        low = min(values, key=lambda item: item["value"])
        high = max(values, key=lambda item: item["value"])
        for point in sorted({low["index"]: low, high["index"]: high}.values(), key=lambda item: item["index"]):
            selected.append(point)
    selected.append(points[-1])
    return selected


def extent(values: list[float], floor_zero: bool = True) -> tuple[float, float]:
    low, high = min(values), max(values)
    if floor_zero and low >= 0:
        low = 0.0
    if low == high:
        padding = max(1.0, abs(high) * 0.1)
        return low if floor_zero and low == 0 else low - padding, high + padding
    padding = (high - low) * 0.08
    return max(0.0, low - padding) if floor_zero else low - padding, high + padding


def number(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def svg_start(height: int) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">',
        '<rect width="100%" height="100%" rx="12" fill="#ffffff"/>',
        '<style>text{font-family:"Microsoft YaHei",Arial,sans-serif}.axis{stroke:#94a3b8;stroke-width:1}.grid{stroke:#e2e8f0;stroke-width:1}.label{fill:#334155;font-size:13px}.title{fill:#0f172a;font-size:18px;font-weight:600}.note{fill:#64748b;font-size:12px}.empty{fill:#94a3b8;font-size:15px}</style>',
    ]


def duration_svg(charts: dict) -> str:
    panel_height = 220
    height = len(SERIES) * panel_height + 30
    out = svg_start(height)
    meta = charts.get("seriesMeta", {})
    for panel, (key, title, color) in enumerate(SERIES):
        top = 42 + panel * panel_height
        bottom = top + 145
        points, x_mode = duration_points(charts.get(key, []))
        series_meta = meta.get(key, {})
        display_title = series_meta.get("label") or title
        out.append(f'<text class="title" x="{LEFT}" y="{top-14}">{html.escape(display_title)}</text>')
        out.append(f'<line class="axis" x1="{LEFT}" y1="{bottom}" x2="{WIDTH-RIGHT}" y2="{bottom}"/>')
        out.append(f'<line class="axis" x1="{LEFT}" y1="{top}" x2="{LEFT}" y2="{bottom}"/>')
        if not points:
            out.append(f'<text class="empty" x="{LEFT+24}" y="{top+72}">该耗时在当前日志中不可用</text>')
            continue
        sampled = downsample_extrema(points)
        values = [point["value"] for point in points]
        low, high = extent(values)
        for tick in range(5):
            y = top + (bottom - top) * tick / 4
            value = high - (high - low) * tick / 4
            out.append(f'<line class="grid" x1="{LEFT}" y1="{y:.2f}" x2="{WIDTH-RIGHT}" y2="{y:.2f}"/>')
            out.append(f'<text class="label" x="{LEFT-10}" y="{y+4:.2f}" text-anchor="end">{number(value)}</text>')
        if x_mode == "time":
            start, end = points[0]["epoch"], points[-1]["epoch"]
            span = end - start
            for tick in range(5):
                x = LEFT + PLOT_WIDTH * tick / 4
                epoch = start + span * tick / 4
                fmt = "%H:%M:%S.%f" if span < 5000 else "%H:%M:%S"
                label = datetime.fromtimestamp(epoch / 1000, tz=CST).strftime(fmt)
                if "%f" in fmt:
                    label = label[:-3]
                out.append(f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{bottom}"/>')
                out.append(f'<text class="label" x="{x:.2f}" y="{bottom+22}" text-anchor="middle">{label}</text>')
        else:
            for tick in range(5):
                x = LEFT + PLOT_WIDTH * tick / 4
                point_index = round((len(points) - 1) * tick / 4)
                label = points[point_index]["x"]
                out.append(f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{bottom}"/>')
                out.append(f'<text class="label" x="{x:.2f}" y="{bottom+22}" text-anchor="middle">{html.escape(str(label))}</text>')
        coordinates = []
        for point in sampled:
            if x_mode == "time":
                x = LEFT + (point["epoch"] - start) / max(1.0, end - start) * PLOT_WIDTH
            else:
                x = LEFT + point["index"] / max(1, len(points) - 1) * PLOT_WIDTH
            y = top + (high - point["value"]) / max(1e-9, high - low) * (bottom - top)
            coordinates.append(f"{x:.2f},{y:.2f}")
        out.append(f'<polyline points="{" ".join(coordinates)}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        coverage = series_meta.get("coverage")
        coverage_note = " · 仅超时样本" if coverage == "overrun_only" else ""
        stats = f'{len(points)} 点 · 平均 {number(statistics.mean(values))} ms · 最低 {number(min(values))} ms · 最高 {number(max(values))} ms{coverage_note}'
        out.append(f'<text class="note" x="{WIDTH-RIGHT}" y="{top-14}" text-anchor="end">{stats}</text>')
        axis_label = "中国时间" if x_mode == "time" else "处理序号"
        out.append(f'<text class="note" x="{WIDTH-RIGHT}" y="{bottom+42}" text-anchor="end">X 轴：{axis_label}</text>')
        out.append(f'<text class="label" x="25" y="{(top+bottom)/2:.2f}" transform="rotate(-90 25 {(top+bottom)/2:.2f})" text-anchor="middle">ms</text>')
    out.append("</svg>")
    return "\n".join(out)


def parse_epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CST)
        return parsed.timestamp() * 1000
    except (TypeError, ValueError):
        return None


def fps_svg(charts: dict) -> str:
    height, top, bottom = 300, 58, 218
    raw = clean_series(charts.get("fpsSeries", []), "ts")
    points = []
    for point in raw:
        epoch = parse_epoch(point["x"])
        if epoch is not None:
            points.append({**point, "epoch": epoch, "index": len(points)})
    out = svg_start(height)
    out.append(f'<text class="title" x="{LEFT}" y="30">FPS 趋势</text>')
    out.append(f'<line class="axis" x1="{LEFT}" y1="{bottom}" x2="{WIDTH-RIGHT}" y2="{bottom}"/>')
    out.append(f'<line class="axis" x1="{LEFT}" y1="{top}" x2="{LEFT}" y2="{bottom}"/>')
    if not points:
        out.append(f'<text class="empty" x="{LEFT+24}" y="135">当前日志中未识别到标准 FPS 数据</text>')
        out.append("</svg>")
        return "\n".join(out)
    values = [point["value"] for point in points]
    low, high = extent(values)
    start, end = points[0]["epoch"], points[-1]["epoch"]
    for tick in range(5):
        y = top + (bottom - top) * tick / 4
        value = high - (high - low) * tick / 4
        out.append(f'<line class="grid" x1="{LEFT}" y1="{y:.2f}" x2="{WIDTH-RIGHT}" y2="{y:.2f}"/>')
        out.append(f'<text class="label" x="{LEFT-10}" y="{y+4:.2f}" text-anchor="end">{number(value)}</text>')
        x = LEFT + PLOT_WIDTH * tick / 4
        epoch = start + (end - start) * tick / 4
        label = datetime.fromtimestamp(epoch / 1000, tz=CST).strftime("%H:%M:%S")
        out.append(f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{bottom}"/>')
        out.append(f'<text class="label" x="{x:.2f}" y="{bottom+22}" text-anchor="middle">{label}</text>')
    coordinates = []
    for point in downsample_extrema(points):
        x = LEFT + (point["epoch"] - start) / max(1.0, end - start) * PLOT_WIDTH
        y = top + (high - point["value"]) / max(1e-9, high - low) * (bottom - top)
        coordinates.append(f"{x:.2f},{y:.2f}")
    out.append(f'<polyline points="{" ".join(coordinates)}" fill="none" stroke="#7c3aed" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    stats = f'{len(points)} 点 · 最新 {number(values[-1])} Hz · 平均 {number(statistics.mean(values))} Hz · 最低 {number(min(values))} Hz · 最高 {number(max(values))} Hz'
    out.append(f'<text class="note" x="{WIDTH-RIGHT}" y="30" text-anchor="end">{stats}</text>')
    out.append(f'<text class="label" x="25" y="{(top+bottom)/2:.2f}" transform="rotate(-90 25 {(top+bottom)/2:.2f})" text-anchor="middle">Hz</text>')
    out.append("</svg>")
    return "\n".join(out)


def reason_svg(charts: dict) -> str:
    groups = (("skipReasonStats", "跳帧原因分布", "#2563eb"), ("errorReasonStats", "异常类型分布", "#dc2626"))
    rows = [max(1, min(8, len(charts.get(key, [])))) for key, _, _ in groups]
    panel_heights = [85 + count * 40 for count in rows]
    height = sum(panel_heights) + 30
    out = svg_start(height)
    top = 44
    for (key, title, color), panel_height in zip(groups, panel_heights):
        items = []
        for item in charts.get(key, []):
            count = finite(item.get("count"))
            if count is not None and count > 0:
                reason = item.get("reason")
                code = normalize_reason_code(reason) if isinstance(reason, (str, int, float, bool)) else "未分类"
                items.append({"reason": code, "count": count})
        items.sort(key=lambda item: (-item["count"], item["reason"]))
        items = items[:8]
        out.append(f'<text class="title" x="{LEFT}" y="{top-14}">{title}</text>')
        if not items:
            out.append(f'<text class="empty" x="{LEFT+24}" y="{top+38}">未识别到相关统计</text>')
            top += panel_height
            continue
        total = sum(item["count"] for item in items)
        maximum = max(item["count"] for item in items)
        for index, item in enumerate(items):
            y = top + index * 40
            label = reason_label(item["reason"])
            bar_x = 330
            bar_width = (WIDTH - RIGHT - bar_x - 110) * item["count"] / maximum
            out.append(f'<text class="label" x="{bar_x-14}" y="{y+19}" text-anchor="end">{html.escape(label)}</text>')
            out.append(f'<rect x="{bar_x}" y="{y+4}" width="{bar_width:.2f}" height="22" rx="4" fill="{color}" opacity="0.86"/>')
            out.append(f'<text class="label" x="{bar_x+bar_width+10:.2f}" y="{y+20}">{item["count"]} 次（{item["count"]/total*100:.1f}%）</text>')
        top += panel_height
    out.append("</svg>")
    return "\n".join(out)


def render_files(data: dict, output_dir: Path) -> dict[str, str]:
    charts = data.get("charts", data)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "duration": output_dir / "performance-duration.svg",
        "fps": output_dir / "fps-trend.svg",
        "reasons": output_dir / "reason-distribution.svg",
    }
    outputs["duration"].write_text(duration_svg(charts), encoding="utf-8")
    outputs["fps"].write_text(fps_svg(charts), encoding="utf-8")
    outputs["reasons"].write_text(reason_svg(charts), encoding="utf-8")
    return {key: str(path) for key, path in outputs.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    outputs = render_files(data, Path(args.output_dir).resolve())
    print(json.dumps({"ok": True, "charts": outputs}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
