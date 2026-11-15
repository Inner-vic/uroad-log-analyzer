#!/usr/bin/env python3
"""Bounded-memory Parquet CAN evidence and a verified, read-only disk contract.

The public JSON is the existing ASC-parser contract.  The SQLite sidecar only
changes how its full evidence series are produced and consumed locally.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from parquet_to_asc import (
    _CHANNEL_NAMES, _DATA_NAMES, _ID_NAMES, _TIME_NAMES, _epoch_seconds,
    _find, _number, _payload,
)
from parse_can_signals import CST, decode, parse_header_time, public_point

METADATA_SCHEMA = "uroad-parquet-index-v1"
_METADATA_LIMIT = 1024 * 1024


class ParquetStreamError(RuntimeError):
    """A safe, nonempty business diagnostic; the private cause retains detail."""

    def __init__(self, stage: str, cause: Exception):
        self.stage = stage
        self.exception_type = type(cause).__name__
        message = "Parquet CAN 处理内存不足。" if isinstance(cause, MemoryError) else "Parquet CAN 处理失败。"
        super().__init__(f"{message} 阶段：{stage}；异常类型：{self.exception_type}。")


def failure_diagnostic(exc: Exception, stage: str | None = None) -> dict:
    """Never rely on str(MemoryError()), or expose rows, payloads, or paths."""
    if isinstance(exc, ParquetStreamError):
        return {"error": str(exc), "exceptionType": exc.exception_type, "stage": exc.stage}
    safe = ParquetStreamError(stage or getattr(exc, "stage", "parquet_can"), exc)
    return {"error": str(safe), "exceptionType": safe.exception_type, "stage": safe.stage}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object_hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _paths(output, index_path, metadata_path) -> tuple[Path, Path, Path]:
    paths = tuple(Path(item).resolve() for item in (output, index_path, metadata_path))
    if len(set(paths)) != 3:
        raise ValueError("Parquet CAN 的三个制品路径必须不同")
    return paths


def _path_receipt(output: Path, index_path: Path, metadata_path: Path) -> dict:
    return {"output": str(output), "index": str(index_path), "metadata": str(metadata_path)}


def _binding(value: dict) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError("Parquet CAN 缺少任务绑定")
    # Canonical JSON roundtrip also rejects unserializable/non-finite values.
    return json.loads(_json(value))


def _environment(binding: dict) -> dict:
    value = binding.get("environment")
    if isinstance(value, dict):
        environment = value.get("environment")
        provider = value.get("providerEnvironment")
    else:
        environment = value
        provider = binding.get("providerEnvironment", environment)
    # Provider names belong to the host's environment_config mapping (for
    # example Test currently maps to testtwo), not to our public env enum.
    if environment not in ("prod", "test") or not isinstance(provider, str) or not provider.strip():
        raise ValueError("Parquet CAN 任务绑定缺少有效环境")
    return {"environment": environment, "providerEnvironment": provider}


def _report(callback, stage: str, **counts) -> None:
    if callback is not None:
        try:
            callback({"stage": stage, **counts})
        except Exception:
            # Progress is best effort and cannot replace the actual result.
            pass


def _normalise_row(row: dict, row_number: int) -> dict:
    timestamp = _find(row, _TIME_NAMES)
    can_id = _find(row, _ID_NAMES)
    data = _find(row, _DATA_NAMES)
    if timestamp is None or can_id is None or data is None:
        raise ValueError(f"row {row_number} is missing timestamp, can id, or payload")
    epoch = _epoch_seconds(timestamp)
    if not math.isfinite(epoch):
        raise ValueError("Parquet timestamp must be finite")
    return {"epoch": epoch, "channel": int(_number(_find(row, _CHANNEL_NAMES) or 1)),
            "can_id": int(_number(can_id)), "data": _payload(data)}


@dataclass(frozen=True)
class BatchView:
    """A native-validated batch, retaining offsets in the unfiltered input."""

    total_rows: int
    min_epoch: float | None
    selected: list[tuple[int, dict]]


def _typed_columns(schema, arrow):
    """Only schemas whose non-target rows normalize without Python inspection."""
    types = getattr(arrow, "types", None)
    if types is None or not hasattr(schema, "field"):
        return None
    groups = (_TIME_NAMES, _ID_NAMES, _CHANNEL_NAMES, _DATA_NAMES)
    selected = []
    for aliases in groups:
        names = [name for name in schema.names if str(name).lower() in aliases]
        if len(names) != 1:
            return None
        selected.append(names[0])
    timestamp, identifier, channel, payload = (schema.field(name).type for name in selected)
    if (not types.is_timestamp(timestamp) or timestamp.unit not in ("s", "ms", "us")
            or not types.is_integer(identifier)
            or identifier.bit_width > 32 or not types.is_integer(channel)
            or not (types.is_binary(payload) or types.is_large_binary(payload) or types.is_fixed_size_binary(payload))):
        return None
    # Nanosecond values can fail the old Python datetime conversion only in
    # interior rows (including non-target rows). Preserve that error path.
    return tuple(selected)


def _typed_batch_view(batch, names, target_ids, arrow):
    from pyarrow import compute as pc

    timestamp_name, id_name, _channel_name, data_name = names
    column = lambda name: batch.column(batch.schema.get_field_index(name))
    timestamps, identifiers, payloads = (column(name) for name in (timestamp_name, id_name, data_name))
    # Null channel remains valid under the established ``channel or 1`` rule.
    # Required nulls must take the old full-row path, including non-target rows.
    if timestamps.null_count or identifiers.null_count or payloads.null_count:
        return None
    if batch.num_rows == 0:
        return BatchView(0, None, [])
    try:
        min_epoch = _epoch_seconds(pc.min(timestamps).as_py())
        max_epoch = _epoch_seconds(pc.max(timestamps).as_py())
        # Arrow timestamps can exceed Python's calendar range. Checking both
        # extremes prevents filtering out a non-target row that used to fail.
        if not math.isfinite(min_epoch) or not math.isfinite(max_epoch):
            return None
    except (OverflowError, ValueError, TypeError, OSError):
        return None
    width = identifiers.type.bit_width
    signed = arrow.types.is_signed_integer(identifiers.type)
    low, high = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)
    values = arrow.array([value for value in target_ids if low <= value <= high], type=identifiers.type)
    mask = pc.is_in(identifiers, value_set=values)
    offsets = pc.indices_nonzero(mask).to_pylist()
    rows = batch.filter(mask).to_pylist()
    return BatchView(batch.num_rows, min_epoch, list(zip(offsets, rows)))


def _iter_batches(path: Path, batch_size: int, *, target_ids=None):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("需要受控 pyarrow 运行时才能读取 Parquet") from exc
    source = pq.ParquetFile(path)
    try:
        # Keep all recognized aliases: an earlier alias can be null in a row.
        aliases = set(_TIME_NAMES + _ID_NAMES + _CHANNEL_NAMES + _DATA_NAMES)
        columns = [name for name in source.schema_arrow.names if str(name).lower() in aliases]
        typed = _typed_columns(source.schema_arrow, pa) if target_ids is not None else None
        for batch in source.iter_batches(batch_size=batch_size, columns=columns, use_threads=False):
            view = _typed_batch_view(batch, typed, target_ids, pa) if typed is not None else None
            yield view if view is not None else batch.to_pylist()
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-8192;
        CREATE TABLE raw_point(ordinal INTEGER PRIMARY KEY, signal TEXT NOT NULL,
            original_epoch REAL NOT NULL, channel INTEGER NOT NULL, payload BLOB NOT NULL);
        CREATE TABLE ordered_point(ordinal INTEGER PRIMARY KEY, signal TEXT NOT NULL,
            original_epoch REAL NOT NULL, epoch_ms REAL NOT NULL,
            channel INTEGER NOT NULL, payload BLOB NOT NULL);
        CREATE TABLE point(signal TEXT NOT NULL, position INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(signal, position)) WITHOUT ROWID;
        CREATE TABLE receipt(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
    """)


def _write_contract(connection: sqlite3.Connection, contract: dict, path: Path) -> None:
    """Stream arrays instead of constructing a signal-sized Python list."""
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("{")
        first = True
        for key, value in contract.items():
            if not first:
                stream.write(",")
            first = False
            stream.write(json.dumps(key) + ":")
            if key != "signals":
                stream.write(_json(value))
                continue
            stream.write("{")
            for signal_index, (signal, definition) in enumerate(value.items()):
                if signal_index:
                    stream.write(",")
                stream.write(json.dumps(signal) + ":{")
                for field_index, (field, field_value) in enumerate(definition.items()):
                    if field_index:
                        stream.write(",")
                    stream.write(json.dumps(field) + ":" + _json(field_value))
                if definition:
                    stream.write(",")
                stream.write('"series":[')
                for point_index, (encoded,) in enumerate(connection.execute(
                        "SELECT payload FROM point WHERE signal=? ORDER BY position", (signal,))):
                    if point_index:
                        stream.write(",")
                    stream.write(encoded)
                stream.write("]}")
            stream.write("}")
        stream.write("}\n")


def build_contract(paths, definitions_path, start, end, output, index_path,
                   metadata_path, binding, progress=None, batch_size=4096) -> dict:
    """Produce full unique CAN evidence with RAM bounded by a Parquet batch.

    A metadata receipt is published last.  Failed private ``*.building-*``
    files remain beside the index for the task's existing retention policy.
    """
    stage = "preparing"
    connection = None
    try:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        output, index_path, metadata_path = _paths(output, index_path, metadata_path)
        if index_path.exists() or metadata_path.exists():
            raise ValueError("已有 Parquet CAN 索引或收据，需先验证复用或归档")
        binding = _binding(binding)
        environment = _environment(binding)
        definitions_path = Path(definitions_path).resolve()
        definitions_hash = _sha256(definitions_path)
        config = json.loads(definitions_path.read_text(encoding="utf-8"))
        definitions = config["signals"]
        definitions_by_id = {definition["canId"].lower().removeprefix("0x"): definition for definition in definitions}
        target_ids = set()
        for identifier in definitions_by_id:
            try:
                number = int(identifier, 16)
            except ValueError:
                continue
            if f"{number:x}" == identifier:
                target_ids.add(number)
        selected_paths = [Path(path).resolve() for path in paths]
        if any(path in (output, index_path, metadata_path, definitions_path) for path in selected_paths):
            raise ValueError("Parquet CAN 输入与制品路径冲突")
        start_epoch = datetime.fromisoformat(start).replace(tzinfo=CST).timestamp() if start else None
        end_exclusive = (datetime.fromisoformat(end).replace(tzinfo=CST) + timedelta(seconds=1)).timestamp() if end else None
        for path in (output, index_path, metadata_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        suffix = f".building-{uuid4().hex}"
        building_index = index_path.with_name(index_path.name + suffix)
        building_output = output.with_name(output.name + suffix)
        building_metadata = metadata_path.with_name(metadata_path.name + suffix)
        connection = sqlite3.connect(building_index)
        _schema(connection)
        rows_seen = files_read = points_selected = unique_total = 0
        min_epoch = None
        stage = "reading_parquet"
        for path in selected_paths:
            for batch in _iter_batches(path, batch_size, target_ids=target_ids):
                row_base = rows_seen
                if isinstance(batch, BatchView):
                    rows_seen += batch.total_rows
                    if batch.min_epoch is not None:
                        min_epoch = batch.min_epoch if min_epoch is None else min(min_epoch, batch.min_epoch)
                    selected_rows = batch.selected
                else:
                    # Existing list-batch callers and schema fallbacks keep the
                    # exact previous per-row normalization and error behavior.
                    rows_seen += len(batch)
                    selected_rows = enumerate(batch)
                with connection:
                    for local_ordinal, row in selected_rows:
                        ordinal = row_base + local_ordinal + 1
                        normalised = _normalise_row(row, ordinal)
                        epoch = normalised["epoch"]
                        min_epoch = epoch if min_epoch is None else min(min_epoch, epoch)
                        definition = definitions_by_id.get(f"{normalised['can_id']:x}")
                        if not definition or len(normalised["data"]) != int(definition["payloadBytes"]):
                            continue
                        allowed = definition.get("ascChannels")
                        if allowed and normalised["channel"] not in allowed:
                            continue
                        connection.execute("INSERT INTO raw_point VALUES(?,?,?,?,?)", (
                            ordinal, definition["key"], epoch, normalised["channel"], normalised["data"]))
                        points_selected += 1
                _report(progress, stage, filesRead=files_read, rowsSeen=rows_seen, pointsSelected=points_selected)
            files_read += 1
            _report(progress, stage, filesRead=files_read, rowsSeen=rows_seen, pointsSelected=points_selected)
        if min_epoch is None:
            raise ValueError("Parquet contains no CAN rows")
        # Reproduce the old global ASC header and six-decimal relative text.
        # Even '-0.000000' was skipped by parse_message, so preserve that rule.
        header = datetime.fromtimestamp(min_epoch, tz=CST)
        header = parse_header_time("date " + header.strftime("%a %b %d %H:%M:%S:%f %p %Y"))
        relative_base = header.timestamp()
        stage = "normalizing_timestamps"
        read_cursor = connection.execute("SELECT ordinal,signal,original_epoch,channel,payload FROM raw_point ORDER BY ordinal")
        while True:
            batch = read_cursor.fetchmany(batch_size)
            if not batch:
                break
            with connection:
                for ordinal, signal, epoch, channel, payload in batch:
                    relative_text = f"{epoch - relative_base:.6f}"
                    if not relative_text[0:1].isdigit():
                        continue
                    absolute = relative_base + float(relative_text)
                    if start_epoch is not None and absolute < start_epoch:
                        continue
                    if end_exclusive is not None and absolute >= end_exclusive:
                        continue
                    connection.execute("INSERT INTO ordered_point VALUES(?,?,?,?,?,?)", (
                        ordinal, signal, epoch, round(absolute * 1000, 3), channel, payload))
            _report(progress, stage, filesRead=files_read, rowsSeen=rows_seen, pointsSelected=points_selected)
        read_cursor.close()
        connection.execute("CREATE INDEX point_order ON ordered_point(signal,epoch_ms,channel,original_epoch,ordinal)")
        connection.commit()
        stage = "deduplicating"
        warnings, signals, signal_counts = [], {}, {}
        for definition in definitions:
            signal = definition["key"]
            previous_epoch = previous_payload = None
            unique_count = 0
            first_ts = last_ts = None
            unknown = set()
            raw_count = connection.execute("SELECT count(*) FROM ordered_point WHERE signal=?", (signal,)).fetchone()[0]
            channels = [row[0] for row in connection.execute(
                "SELECT DISTINCT channel FROM ordered_point WHERE signal=? ORDER BY channel", (signal,))]
            cursor = connection.execute("SELECT epoch_ms,payload FROM ordered_point WHERE signal=? ORDER BY epoch_ms,channel,original_epoch,ordinal", (signal,))
            for epoch_ms, payload in cursor:
                if previous_payload is not None and payload == previous_payload and epoch_ms - previous_epoch <= 1.0:
                    continue
                previous_epoch, previous_payload = epoch_ms, payload
                raw, value, label = decode(payload, definition)
                ts = datetime.fromtimestamp(epoch_ms / 1000, tz=CST).isoformat(timespec="milliseconds")
                point = public_point({"ts": ts, "epochMs": epoch_ms, "raw": raw, "value": value, "label": label})
                connection.execute("INSERT INTO point VALUES(?,?,?)", (signal, unique_count, _json(point)))
                unique_count += 1
                unique_total += 1
                first_ts = first_ts or ts
                last_ts = ts
                if definition.get("enum") and not label:
                    unknown.add(raw)
                if unique_count % batch_size == 0:
                    connection.commit()
                    _report(progress, stage, filesRead=files_read, rowsSeen=rows_seen, uniquePoints=unique_total)
            cursor.close()
            if not unique_count:
                warnings.append(f"{definition['name']} 在所选 ASC 和时间范围内无数据。")
            if definition.get("confidence") == "inferred":
                warnings.append(f"{definition['name']} 使用推断解码定义，需用权威 DBC/ARXML 复核。")
            elif definition.get("confidence") == "unverified_enum":
                warnings.append(f"{definition['name']} 当前仅展示数字原始编码；需求文档期望 D/R/P/N，缺少权威枚举映射。")
            signals[signal] = {
                "name": definition["name"], "canId": definition["canId"], "logicalBus": config.get("logicalBus"),
                "unit": definition.get("unit"), "confidence": definition.get("confidence"),
                "validation": definition.get("validation"), "channels": channels,
                "configuredChannels": definition.get("ascChannels", []), "channelMapping": definition.get("channelMapping"),
                "rawPointCount": raw_count, "uniquePointCount": unique_count, "outputPointCount": unique_count,
                "downsampled": False, "unknownRawValues": sorted(unknown),
                "coverage": {"start": first_ts, "end": last_ts},
            }
            signal_counts[signal] = unique_count
            connection.commit()
        contract = {"schemaVersion": "1.0", "sourceAsc": str(index_path.parent / "combined.asc"),
                    "headerTime": header.isoformat(), "signals": signals, "warnings": warnings,
                    "environment": environment, "sourceParquet": {"files": len(selected_paths), "rows": rows_seen}}
        stage = "writing_contract"
        _report(progress, stage, filesRead=files_read, rowsSeen=rows_seen, uniquePoints=unique_total)
        _write_contract(connection, contract, building_output)
        output_hash = _sha256(building_output)
        receipt = {"schema": METADATA_SCHEMA, "paths": _path_receipt(output, index_path, metadata_path),
                   "binding": binding, "definitionsSha256": definitions_hash, "outputSha256": output_hash,
                   "contractSha256": _object_hash(contract), "signalCounts": signal_counts,
                   "rowsSeen": rows_seen, "files": len(selected_paths), "minEpoch": min_epoch}
        connection.execute("INSERT INTO receipt VALUES(1,?)", (_json(receipt),))
        connection.executescript("DROP TABLE raw_point; DROP TABLE ordered_point;")
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        connection = None
        metadata = {**receipt, "contract": contract, "indexSha256": _sha256(building_index)}
        building_metadata.write_text(_json(metadata) + "\n", encoding="utf-8")
        # The receipt is the commit marker.  An interrupted publish has no valid
        # new receipt and must not be interpreted as completed evidence.
        building_output.replace(output)
        building_index.replace(index_path)
        building_metadata.replace(metadata_path)
        _report(progress, "completed", filesRead=files_read, rowsSeen=rows_seen, uniquePoints=unique_total)
        return {"ok": True, "output": str(output), "index": str(index_path), "metadata": str(metadata_path),
                "files": len(selected_paths), "rows": rows_seen, "rowsSeen": rows_seen,
                "signals": signal_counts, "outputSha256": output_hash, "indexSha256": metadata["indexSha256"]}
    except Exception as exc:
        _report(progress, "failed", exceptionType=type(exc).__name__)
        if isinstance(exc, MemoryError) or type(exc).__name__ == "ArrowMemoryError":
            # The wrapper classifies resource exhaustion using its real type.
            exc.stage = stage
            raise
        if isinstance(exc, ParquetStreamError):
            raise
        raise ParquetStreamError(stage, exc) from exc
    finally:
        if connection is not None:
            connection.close()


class SQLiteSeries:
    """A read-only, repeatable series; each iterator owns its connection."""

    __slots__ = ("_path", "_signal", "_count")

    def __init__(self, path: Path, signal: str, count: int):
        self._path, self._signal, self._count = path, signal, count

    def __len__(self) -> int:
        return self._count

    def __iter__(self):
        connection = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True)
        try:
            for (encoded,) in connection.execute("SELECT payload FROM point WHERE signal=? ORDER BY position", (self._signal,)):
                yield json.loads(encoded)
        finally:
            connection.close()


def load_indexed_contract(output, index_path, metadata_path, binding, definitions_path) -> dict:
    """Verify the completed exact-path receipt before exposing lazy series."""
    output, index_path, metadata_path = _paths(output, index_path, metadata_path)
    binding = _binding(binding)
    if metadata_path.stat().st_size > _METADATA_LIMIT:
        raise ValueError("Parquet CAN 收据超出允许大小")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("schema") != METADATA_SCHEMA:
        raise ValueError("Parquet CAN 收据格式不正确")
    if metadata.get("paths") != _path_receipt(output, index_path, metadata_path):
        raise ValueError("Parquet CAN 制品路径与收据不匹配")
    if _json(metadata.get("binding")) != _json(binding):
        raise ValueError("Parquet CAN 任务绑定与收据不匹配")
    if metadata.get("definitionsSha256") != _sha256(Path(definitions_path).resolve()):
        raise ValueError("Parquet CAN 信号定义与收据不匹配")
    if metadata.get("outputSha256") != _sha256(output) or metadata.get("indexSha256") != _sha256(index_path):
        raise ValueError("Parquet CAN 制品校验失败")
    contract = metadata.get("contract")
    if not isinstance(contract, dict) or _object_hash(contract) != metadata.get("contractSha256"):
        raise ValueError("Parquet CAN 合同元数据校验失败")
    connection = sqlite3.connect(index_path.as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT payload FROM receipt WHERE id=1").fetchone()
        expected_receipt = {key: value for key, value in metadata.items() if key not in ("contract", "indexSha256")}
        if not row or json.loads(row[0]) != expected_receipt:
            raise ValueError("Parquet CAN 索引与收据不匹配")
        counts = metadata.get("signalCounts")
        signals = contract.get("signals")
        if not isinstance(counts, dict) or not isinstance(signals, dict) or set(counts) != set(signals):
            raise ValueError("Parquet CAN 信号计数不正确")
        actual = dict(connection.execute("SELECT signal,count(*) FROM point GROUP BY signal"))
        if set(actual) - set(counts):
            raise ValueError("Parquet CAN 索引包含未绑定信号")
        for signal, count in counts.items():
            definition = signals[signal]
            if type(count) is not int or count < 0 or actual.get(signal, 0) != count:
                raise ValueError("Parquet CAN 索引点数校验失败")
            if not isinstance(definition, dict) or "series" in definition or definition.get("outputPointCount") != count or definition.get("uniquePointCount") != count or definition.get("downsampled") is not False:
                raise ValueError("Parquet CAN 完整证据计数不一致")
            definition["series"] = SQLiteSeries(index_path, signal, count)
    finally:
        connection.close()
    return contract
