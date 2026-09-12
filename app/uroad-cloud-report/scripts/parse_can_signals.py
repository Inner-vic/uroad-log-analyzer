#!/usr/bin/env python3
"""Parse configured vehicle signals from a CANalyzer ASC file."""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
ASC_DATE = re.compile(r"^date\s+(?P<value>.+)$", re.IGNORECASE)
DATE_FORMATS = (
    "%a %b %d %H:%M:%S:%f %p %Y",
    "%a %b %d %H:%M:%S.%f %p %Y",
)


def parse_header_time(line: str) -> datetime | None:
    match = ASC_DATE.match(line.strip())
    if not match:
        return None
    text = re.sub(r"\s+", " ", match.group("value").strip())
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).replace(tzinfo=CST)
        except ValueError:
            pass
    raise ValueError(f"无法解析 ASC date header: {line.strip()}")


def parse_message(line: str) -> tuple[float, int, str, bytes] | None:
    parts = line.split()
    if not parts or not parts[0][0:1].isdigit():
        return None
    try:
        relative = float(parts[0])
        if len(parts) > 10 and parts[1].upper() == "CANFD":
            channel = int(parts[2])
            can_id = parts[3].lower().removesuffix("x")
            payload = bytes.fromhex("".join(parts[10:]))
        else:
            channel = int(parts[1])
            can_id = parts[2].lower().removesuffix("x")
            payload = bytes.fromhex("".join(parts[6:]))
        return relative, channel, can_id, payload
    except (IndexError, ValueError):
        return None


def extract_raw(payload: bytes, definition: dict) -> int:
    if "startByte" in definition:
        start = int(definition["startByte"])
        end = start + int(definition["lengthBytes"])
        if end > len(payload):
            raise ValueError("payload shorter than configured byte field")
        return int.from_bytes(payload[start:end], definition["byteOrder"], signed=False)
    start = int(definition["startBit"])
    length = int(definition["lengthBits"])
    if definition["byteOrder"] != "little":
        raise ValueError("bit fields currently support little byte order only")
    return (int.from_bytes(payload, "little") >> start) & ((1 << length) - 1)


def decode(payload: bytes, definition: dict) -> tuple[int, float | int, str | None]:
    raw = extract_raw(payload, definition)
    physical = raw * definition.get("factor", 1) + definition.get("offset", 0)
    if isinstance(physical, float):
        physical = round(physical, 6)
    label = (definition.get("enum") or {}).get(str(raw))
    return raw, physical, label


def deduplicate(points: list[dict], tolerance_ms: float = 1.0) -> list[dict]:
    unique: list[dict] = []
    for point in sorted(points, key=lambda item: (item["epochMs"], item["channel"])):
        if unique:
            previous = unique[-1]
            if point["payload"] == previous["payload"] and point["epochMs"] - previous["epochMs"] <= tolerance_ms:
                previous["mirrorChannels"] = sorted(set(previous["mirrorChannels"] + [point["channel"]]))
                continue
        point["mirrorChannels"] = [point["channel"]]
        unique.append(point)
    return unique


def compress_steps(points: list[dict]) -> list[dict]:
    if len(points) <= 2:
        return points
    output = [points[0]]
    for index in range(1, len(points) - 1):
        if points[index]["raw"] != points[index - 1]["raw"] or points[index]["raw"] != points[index + 1]["raw"]:
            output.append(points[index])
    output.append(points[-1])
    return output


def downsample_extrema(points: list[dict], max_points: int) -> list[dict]:
    if len(points) <= max_points:
        return points
    bucket_count = max(1, max_points // 2)
    start = points[0]["epochMs"]
    span = max(1, points[-1]["epochMs"] - start)
    buckets: dict[int, list[dict]] = defaultdict(list)
    for point in points:
        bucket = min(bucket_count - 1, int((point["epochMs"] - start) * bucket_count / span))
        buckets[bucket].append(point)
    selected = [points[0], points[-1]]
    for bucket_points in buckets.values():
        selected.append(min(bucket_points, key=lambda point: point["value"]))
        selected.append(max(bucket_points, key=lambda point: point["value"]))
    return sorted({point["epochMs"]: point for point in selected}.values(), key=lambda point: point["epochMs"])


def public_point(point: dict) -> dict:
    output = {
        "ts": point["ts"],
        "epochMs": point["epochMs"],
        "value": point["value"],
        "raw": point["raw"],
    }
    if point.get("label"):
        output["label"] = point["label"]
    return output


def parse_asc(asc_path: Path, definitions_path: Path, start_time: str | None = None, end_time: str | None = None, max_points: int = 2000, evidence_mode: bool = False) -> dict:
    config = json.loads(definitions_path.read_text(encoding="utf-8"))
    definitions = config["signals"]
    definitions_by_id = {definition["canId"].lower().removeprefix("0x"): definition for definition in definitions}
    header_time: datetime | None = None
    raw_by_signal: dict[str, list[dict]] = defaultdict(list)
    channels_by_signal: dict[str, set[int]] = defaultdict(set)
    start_epoch = datetime.fromisoformat(start_time).replace(tzinfo=CST).timestamp() if start_time else None
    end_exclusive = (datetime.fromisoformat(end_time).replace(tzinfo=CST) + timedelta(seconds=1)).timestamp() if end_time else None

    with asc_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if header_time is None:
                header_time = parse_header_time(line) or header_time
            parsed = parse_message(line)
            if not parsed:
                continue
            if header_time is None:
                raise ValueError("ASC message appears before a valid date header")
            relative, channel, can_id, payload = parsed
            definition = definitions_by_id.get(can_id)
            if not definition or len(payload) != int(definition["payloadBytes"]):
                continue
            allowed_channels = definition.get("ascChannels")
            if allowed_channels and channel not in allowed_channels:
                continue
            absolute_epoch = header_time.timestamp() + relative
            if start_epoch is not None and absolute_epoch < start_epoch:
                continue
            if end_exclusive is not None and absolute_epoch >= end_exclusive:
                continue
            epoch_ms = round(absolute_epoch * 1000, 3)
            channels_by_signal[definition["key"]].add(channel)
            raw_by_signal[definition["key"]].append({
                "epochMs": epoch_ms,
                "channel": channel,
                "payload": payload.hex(),
                "bytes": payload,
            })

    warnings: list[str] = []
    output_signals: dict[str, dict] = {}
    for definition in definitions:
        key = definition["key"]
        deduplicated = deduplicate(raw_by_signal[key])
        decoded: list[dict] = []
        for point in deduplicated:
            raw, value, label = decode(point["bytes"], definition)
            decoded.append({**point, "raw": raw, "value": value, "label": label, "ts": datetime.fromtimestamp(point["epochMs"] / 1000, tz=CST).isoformat(timespec="milliseconds")})
        raw_point_count = len(raw_by_signal[key])
        unique_point_count = len(decoded)
        if evidence_mode:
            rendered = decoded
        elif key == "gear":
            rendered = compress_steps(decoded)
        else:
            rendered = downsample_extrema(decoded, max_points)
        unknown_values = sorted({point["raw"] for point in decoded if definition.get("enum") and not point.get("label")})
        if not decoded:
            warnings.append(f"{definition['name']} 在所选 ASC 和时间范围内无数据。")
        if definition.get("confidence") == "inferred":
            warnings.append(f"{definition['name']} 使用推断解码定义，需用权威 DBC/ARXML 复核。")
        elif definition.get("confidence") == "unverified_enum":
            warnings.append(f"{definition['name']} 当前仅展示数字原始编码；需求文档期望 D/R/P/N，缺少权威枚举映射。")
        output_signals[key] = {
            "name": definition["name"],
            "canId": definition["canId"],
            "logicalBus": config.get("logicalBus"),
            "unit": definition.get("unit"),
            "confidence": definition.get("confidence"),
            "validation": definition.get("validation"),
            "channels": sorted(channels_by_signal[key]),
            "configuredChannels": definition.get("ascChannels", []),
            "channelMapping": definition.get("channelMapping"),
            "rawPointCount": raw_point_count,
            "uniquePointCount": unique_point_count,
            "outputPointCount": len(rendered),
            "downsampled": len(rendered) < unique_point_count,
            "unknownRawValues": unknown_values,
            "coverage": {
                "start": decoded[0]["ts"] if decoded else None,
                "end": decoded[-1]["ts"] if decoded else None,
            },
            "series": [public_point(point) for point in rendered],
        }
    return {
        "schemaVersion": "1.0",
        "sourceAsc": str(asc_path),
        "headerTime": header_time.isoformat() if header_time else None,
        "signals": output_signals,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asc", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--definitions", default=str(Path(__file__).resolve().parents[1] / "references" / "can-signals.json"))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--max-points", type=int, default=2000)
    parser.add_argument("--evidence-mode", action="store_true", help="Emit all slice-unique points for disk evidence ingestion")
    args = parser.parse_args()
    try:
        if args.max_points < 10:
            raise ValueError("max-points 必须至少为 10")
        result = parse_asc(Path(args.asc).resolve(), Path(args.definitions).resolve(), args.start, args.end, args.max_points, args.evidence_mode)
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output)
        print(json.dumps({"ok": True, "output": str(output), "signals": {key: value["outputPointCount"] for key, value in result["signals"].items()}}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
