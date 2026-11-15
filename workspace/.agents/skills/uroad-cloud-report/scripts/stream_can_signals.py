#!/usr/bin/env python3
"""Acquire EEA3 msg_xcu files and emit CAN signals without ZIP or ASC artifacts.

The EEA3 framing semantics are derived from the MIT-licensed
cheyun-canlog-download parser; see references/third-party-attribution.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cheyun_prod_client import (
    CAN_LOG_CLASS,
    EEA_PLATFORM,
    CloudNoDataError,
    download_to_file,
    environment_config,
    load_settings,
    ordered_files,
    query_files,
    safe_filename,
)
from parse_can_signals import compress_steps, decode, downsample_extrema, public_point

CST = timezone(timedelta(hours=8))
TARGET_IDS = {0x12A, 0x108, 0x106}
READ_CHUNK_BYTES = 256 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024


class SegmentIndex:
    """Disk-backed cross-file segment identity index."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE segment(digest BLOB PRIMARY KEY);
            CREATE TABLE point(seq INTEGER PRIMARY KEY AUTOINCREMENT, signal TEXT NOT NULL,
                               epoch_ms REAL NOT NULL, channel INTEGER NOT NULL, payload BLOB NOT NULL);
            CREATE INDEX point_order ON point(signal, epoch_ms, channel, seq);
        """)
        self.duplicates = 0

    def first_seen(self, frame: bytes) -> bool:
        digest = hashlib.sha256(frame[2:-1]).digest()
        cursor = self.db.execute("INSERT OR IGNORE INTO segment VALUES(?)", (digest,))
        if cursor.rowcount == 0:
            self.duplicates += 1
            return False
        return True

    def add_point(self, signal: str, epoch_ms: float, channel: int, payload: bytes) -> None:
        self.db.execute("INSERT INTO point(signal,epoch_ms,channel,payload) VALUES(?,?,?,?)",
                        (signal, epoch_ms, channel, payload))

    def iter_points(self, signal: str):
        for epoch_ms, channel, payload in self.db.execute(
                "SELECT epoch_ms,channel,payload FROM point WHERE signal=? ORDER BY epoch_ms,channel,seq", (signal,)):
            yield {"epochMs": epoch_ms, "channel": channel, "payload": bytes(payload).hex(), "bytes": bytes(payload)}

    def raw_count(self, signal: str) -> int:
        return self.db.execute("SELECT count(*) FROM point WHERE signal=?", (signal,)).fetchone()[0]

    def channels(self, signal: str) -> list[int]:
        return [row[0] for row in self.db.execute("SELECT DISTINCT channel FROM point WHERE signal=? ORDER BY channel", (signal,))]

    def close(self) -> None:
        self.db.close()


def _epochs(start: str | None, end: str | None) -> tuple[float | None, float | None]:
    start_epoch = datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp() if start else None
    end_epoch = (datetime.strptime(end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST) + timedelta(seconds=1)).timestamp() if end else None
    return start_epoch, end_epoch


def _consume_frame(frame: bytes, definitions_by_id: dict[int, dict], index: SegmentIndex,
                   start_epoch: float | None, end_epoch: float | None) -> None:
    if frame[:2] != b"\xaa\x55" or frame[-1] != 0x7B:
        raise ValueError("EEA3 frame has an invalid marker or trailer")
    pack_len = int.from_bytes(frame[2:4], "big")
    if pack_len < 6 or len(frame) != pack_len + 6:
        raise ValueError("EEA3 frame has an invalid length")
    if not index.first_seen(frame):
        return
    pack_time = int.from_bytes(frame[4:10], "big")
    messages = memoryview(frame)[10:-2]
    offset = 0
    while offset < len(messages):
        if len(messages) - offset < 8:
            raise ValueError("EEA3 frame contains a truncated message header")
        realtime = int.from_bytes(messages[offset:offset + 2], "big")
        type_channel = messages[offset + 2]
        can_id = int.from_bytes(messages[offset + 3:offset + 7], "big")
        dlc = messages[offset + 7]
        message_end = offset + 8 + dlc
        if message_end > len(messages):
            raise ValueError("EEA3 frame contains a truncated message payload")
        definition = definitions_by_id.get(can_id)
        if definition is not None:
            message_type, channel = (type_channel >> 6) & 0x03, type_channel & 0x3F
            payload = bytes(messages[offset + 8:message_end])
            if message_type not in {0, 1}:
                raise ValueError("target EEA3 message is not CAN/CANFD")
            if len(payload) != int(definition["payloadBytes"]):
                raise ValueError(f"target EEA3 payload length is invalid for {definition['canId']}")
            allowed = definition.get("ascChannels") or []
            epoch = (pack_time + realtime) / 10_000.0
            if (not allowed or channel in allowed) and (start_epoch is None or epoch >= start_epoch) and (end_epoch is None or epoch < end_epoch):
                epoch_ms = round(epoch * 1000, 3)
                key = definition["key"]
                index.add_point(key, epoch_ms, channel, payload)
        offset = message_end


def parse_eea3_stream(stream, definitions_path: Path, index: SegmentIndex, *,
                      start: str | None = None, end: str | None = None,
                      raw_by_signal=None, channels=None) -> tuple[SegmentIndex, None, int]:
    config = json.loads(definitions_path.read_text(encoding="utf-8"))
    definitions = config.get("signals") or []
    definitions_by_id = {int(item["canId"], 16): item for item in definitions if int(item["canId"], 16) in TARGET_IDS}
    if set(definitions_by_id) != TARGET_IDS:
        raise ValueError("CAN signal definitions do not contain the three required target IDs")
    if raw_by_signal is not None and raw_by_signal is not index:
        raise ValueError("target point retention must use the disk-backed SegmentIndex")
    start_epoch, end_epoch = _epochs(start, end)
    buffer = bytearray()
    frame_count = 0
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if chunk:
            buffer.extend(chunk)
        while True:
            if len(buffer) < 4:
                break
            if buffer[:2] != b"\xaa\x55":
                raise ValueError("source contains non-EEA3 data")
            pack_len = int.from_bytes(buffer[2:4], "big")
            frame_len = pack_len + 6
            if pack_len < 6 or frame_len > MAX_FRAME_BYTES:
                raise ValueError("EEA3 frame length is outside the bounded range")
            if len(buffer) < frame_len:
                break
            frame = bytes(buffer[:frame_len])
            del buffer[:frame_len]
            _consume_frame(frame, definitions_by_id, index, start_epoch, end_epoch)
            frame_count += 1
        if len(buffer) > MAX_FRAME_BYTES:
            raise ValueError("EEA3 rolling buffer exceeded its bound")
        if not chunk:
            break
    if buffer:
        raise ValueError("source ends with an incomplete EEA3 frame")
    if frame_count == 0:
        raise ValueError("source contains no EEA3 frames")
    index.db.commit()
    return index, None, frame_count


def build_contract(definitions_path: Path, raw_by_signal: SegmentIndex, channels=None,
                   *, max_points: int = 2000, evidence_mode: bool = True, duplicates: int = 0,
                   environment: str = "prod", provider_environment: str = "prod") -> dict:
    if max_points < 10:
        raise ValueError("max_points must be at least 10")
    config = json.loads(definitions_path.read_text(encoding="utf-8"))
    warnings, output_signals = [], {}
    for definition in config["signals"]:
        key = definition["key"]
        decoded, previous = [], None
        unique_count = 0
        first_ts = last_ts = None
        unknown = set()
        for point in raw_by_signal.iter_points(key):
            if previous is not None and point["payload"] == previous["payload"] and point["epochMs"] - previous["epochMs"] <= 1.0:
                continue
            previous = point
            raw, value, label = decode(point["bytes"], definition)
            decoded_point = {**point, "raw": raw, "value": value, "label": label,
                             "ts": datetime.fromtimestamp(point["epochMs"] / 1000, tz=CST).isoformat(timespec="milliseconds")}
            unique_count += 1
            first_ts = first_ts or decoded_point["ts"]
            last_ts = decoded_point["ts"]
            if definition.get("enum") and not label:
                unknown.add(raw)
            decoded.append(decoded_point)
            if not evidence_mode and len(decoded) >= max_points * 2:
                decoded = downsample_extrema(decoded, max_points)
        if not evidence_mode and len(decoded) > max_points:
            decoded = downsample_extrema(decoded, max_points)
        rendered = decoded if evidence_mode else compress_steps(decoded) if key == "gear" else downsample_extrema(decoded, max_points)
        if unique_count == 0:
            warnings.append(f"{definition['name']} 在所选 EEA3 数据和时间范围内无数据。")
        if definition.get("confidence") == "inferred":
            warnings.append(f"{definition['name']} 使用推断解码定义，需用权威 DBC/ARXML 复核。")
        elif definition.get("confidence") == "unverified_enum":
            warnings.append(f"{definition['name']} 当前仅展示数字原始编码；需求文档期望 D/R/P/N，缺少权威枚举映射。")
        output_signals[key] = {
            "name": definition["name"], "canId": definition["canId"], "logicalBus": config.get("logicalBus"),
            "unit": definition.get("unit"), "confidence": definition.get("confidence"), "validation": definition.get("validation"),
            "channels": raw_by_signal.channels(key), "configuredChannels": definition.get("ascChannels", []),
            "channelMapping": definition.get("channelMapping"), "rawPointCount": raw_by_signal.raw_count(key),
            "uniquePointCount": unique_count, "outputPointCount": len(rendered), "downsampled": len(rendered) < unique_count,
            "unknownRawValues": sorted(unknown),
            "coverage": {"start": first_ts, "end": last_ts},
            "series": [public_point(point) for point in rendered],
        }
    return {"schemaVersion": "1.1", "sourceAsc": "streaming-eea3", "headerTime": None,
            "signals": output_signals, "warnings": warnings, "deduplication": {"segments": duplicates},
            "environment": {"environment": environment, "providerEnvironment": provider_environment}}


def list_download_hosts(*, vin: str, start: str, end: str, page_size: int = 100,
                        eea_platform: str = EEA_PLATFORM, environment: str = "prod") -> dict:
    username = load_settings()["username"]
    result = query_files(vin=vin, start=start, end=end, log_class=CAN_LOG_CLASS,
                         username=username, page_size=page_size, eea_platform=eea_platform,
                         environment=environment)
    return {
        "environment": result.environment,
        "providerEnvironment": result.provider_environment,
        "total": result.total,
        "downloadHosts": result.download_hosts,
    }


def acquire(*, vin: str, start: str, end: str, output: Path, work_dir: Path,
            page_size: int = 100, max_files: int | None = None,
            definitions_path: Path | None = None, environment: str = "prod") -> dict:
    definitions_path = definitions_path or Path(__file__).resolve().parents[1] / "references" / "can-signals.json"
    output, work_dir = output.resolve(), work_dir.resolve()
    skill_root = Path(__file__).resolve().parents[1]
    if max_files is not None and max_files <= 0:
        raise ValueError("max-files must be greater than zero")
    if work_dir.exists():
        raise ValueError("CAN work directory must not already exist")
    if work_dir == Path(work_dir.anchor) or work_dir == skill_root or skill_root in work_dir.parents:
        raise ValueError("CAN work directory is unsafe")
    if output == work_dir or work_dir in output.parents:
        raise ValueError("CAN output must be outside the work directory")
    env_config = environment_config(environment)
    username = load_settings()["username"]
    result = query_files(vin=vin, start=start, end=end, log_class=CAN_LOG_CLASS,
                         username=username, page_size=page_size, eea_platform=EEA_PLATFORM,
                         environment=environment)
    selected = ordered_files(result.items)
    unexpected = [safe_filename(item) for item in selected if not safe_filename(item).lower().endswith(".zst")]
    if unexpected:
        raise RuntimeError(f"{environment} CAN 查询返回了非 zstd 文件（共 {len(unexpected)} 个）")
    if max_files is not None:
        selected = selected[:max_files]
    if not selected:
        raise CloudNoDataError(f"{environment} 环境没有 CAN 报文数据")
    work_dir.mkdir(parents=True, exist_ok=False)
    index = None
    try:
        index = SegmentIndex(work_dir / "segments.sqlite3")
        try:
            import zstandard as zstd
        except ImportError as exc:
            raise RuntimeError("zstandard 运行依赖不可用") from exc
        for number, item in enumerate(selected, 1):
            source = work_dir / safe_filename(item, number)
            url = item.get("downloadURL") or item.get("downloadUrl") or item.get("url")
            if not url:
                raise RuntimeError(f"{environment} CAN 文件记录缺少下载地址")
            try:
                download_to_file(str(url), source, environment=environment)
                with source.open("rb") as compressed, zstd.ZstdDecompressor().stream_reader(compressed) as stream:
                    parse_eea3_stream(stream, definitions_path, index, start=start, end=end,
                                      raw_by_signal=index)
            except Exception as exc:
                raise RuntimeError(f"CAN source {number} failed during streaming ingestion: {type(exc).__name__}: {exc}") from exc
            finally:
                source.unlink(missing_ok=True)
                source.with_name(source.name + ".part").unlink(missing_ok=True)
        result_contract = build_contract(definitions_path, index, duplicates=index.duplicates,
                                         environment=environment,
                                         provider_environment=env_config["providerEnvironment"])
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(result_contract, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return result_contract
    finally:
        if index is not None:
            index.close()
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--eea-platform", choices=(EEA_PLATFORM,), default=EEA_PLATFORM)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--output", required=False)
    parser.add_argument("--work-dir", required=False)
    parser.add_argument("--definitions", default=str(Path(__file__).resolve().parents[1] / "references" / "can-signals.json"))
    parser.add_argument("--cloud-env", choices=("prod", "test"), default="prod")
    parser.add_argument("--list-download-hosts", action="store_true")
    args = parser.parse_args()
    try:
        if args.list_download_hosts:
            payload = list_download_hosts(vin=args.vin, start=args.start, end=args.end,
                                          page_size=args.page_size, eea_platform=args.eea_platform,
                                          environment=args.cloud_env)
            print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
            return 0
        if not args.output or not args.work_dir:
            raise ValueError("完整下载模式需要 output 和 work-dir")
        output = Path(args.output).resolve()
        result = acquire(vin=args.vin, start=args.start, end=args.end, output=output,
                         work_dir=Path(args.work_dir).resolve(), page_size=args.page_size, max_files=args.max_files,
                         definitions_path=Path(args.definitions).resolve(), environment=args.cloud_env)
        print(json.dumps({"ok": True, "output": str(Path(args.output).resolve()),
                          "signals": {key: value["outputPointCount"] for key, value in result["signals"].items()},
                          "environment": result.get("environment")}, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
