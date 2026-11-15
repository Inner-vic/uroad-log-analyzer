#!/usr/bin/env python3
"""Convert CAN records stored in Parquet into CANalyzer ASCII (ASC).

The converter is intentionally schema-tolerant: it accepts common names for
timestamp, CAN id, channel and payload columns.  Parquet decoding uses the
pipeline-managed pyarrow runtime directly, avoiding an unnecessary pandas
dependency.  No network/install step is performed by this script.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
_TIME_NAMES = ("timestamp", "ts", "time", "datetime", "date", "epoch_ms", "epoch", "time_ms")
_ID_NAMES = ("can_id", "canid", "arbitration_id", "message_id", "msg_id", "id", "identifier")
_CHANNEL_NAMES = ("channel", "channel_id", "bus", "can_channel")
_DATA_NAMES = ("data", "payload", "bytes", "raw_data", "frame_data")


def _find(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in lowered and lowered[name] is not None:
            return lowered[name]
    return None


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r"[-+]?0[xX][0-9a-fA-F]+", text):
        return float(int(text, 0))
    if re.fullmatch(r"[0-9a-fA-F]+", text) and re.search(r"[a-fA-F]", text):
        return float(int(text, 16))
    return float(text)


def _epoch_seconds(value: Any, *, epoch_unit: str = "auto") -> float:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            value = _number(text)
    number = _number(value)
    if epoch_unit == "us" or (epoch_unit == "auto" and abs(number) > 1e14):
        return number / 1_000_000.0
    if epoch_unit == "ms" or (epoch_unit == "auto" and abs(number) > 1e11):
        return number / 1000.0
    return number


def _payload(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (list, tuple)):
        return bytes(int(x) & 0xff for x in value)
    text = str(value).strip()
    if text.startswith("0x"):
        text = text[2:]
    text = re.sub(r"[\s,:_-]+", "", text)
    if not text:
        return b""
    if len(text) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", text):
        raise ValueError(f"invalid payload: {value!r}")
    return bytes.fromhex(text)


def normalize_rows(rows: list[dict[str, Any]], *, epoch_unit: str = "auto") -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, 1):
        timestamp = _find(row, _TIME_NAMES)
        can_id = _find(row, _ID_NAMES)
        data = _find(row, _DATA_NAMES)
        if timestamp is None or can_id is None or data is None:
            raise ValueError(f"row {index} is missing timestamp, can id, or payload")
        channel = int(_number(_find(row, _CHANNEL_NAMES) or 1))
        result.append({"epoch": _epoch_seconds(timestamp, epoch_unit=epoch_unit),
                       "channel": channel, "can_id": int(_number(can_id)),
                       "data": _payload(data)})
    return sorted(result, key=lambda item: item["epoch"])


def rows_to_asc(rows: list[dict[str, Any]], *, header: datetime | None = None) -> str:
    if not rows:
        raise ValueError("Parquet contains no CAN rows")
    first = header or datetime.fromtimestamp(rows[0]["epoch"], tz=CST)
    relative_base = first.timestamp()
    lines = ["date " + first.strftime("%a %b %d %H:%M:%S:%f %p %Y"),
             "base hex  timestamps absolute", "internal events logged", "// Converted from Parquet"]
    for row in rows:
        relative = row["epoch"] - relative_base
        can_id = f"{row['can_id']:X}"
        payload = row["data"]
        lines.append(f"{relative:.6f} {row['channel']} {can_id} Rx d {len(payload)} " + " ".join(f"{b:02X}" for b in payload))
    return "\n".join(lines) + "\n"


def read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(path)
    except ImportError as exc:
        raise RuntimeError("需要受控 pyarrow 运行时才能读取 Parquet；当前环境未安装或未激活") from exc
    except Exception as exc:
        raise RuntimeError(f"读取 Parquet 失败: {exc}") from exc
    return table.to_pylist()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", required=True)
    parser.add_argument("--asc", required=True)
    parser.add_argument("--epoch-unit", choices=("auto", "s", "ms", "us"), default="auto")
    args = parser.parse_args()
    try:
        rows = normalize_rows(read_parquet(Path(args.parquet)), epoch_unit=args.epoch_unit)
        output = Path(args.asc).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rows_to_asc(rows), encoding="utf-8")
        print(json.dumps({"ok": True, "input": str(Path(args.parquet).resolve()), "output": str(output), "rows": len(rows)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
