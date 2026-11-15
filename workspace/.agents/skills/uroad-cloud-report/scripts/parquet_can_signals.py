#!/usr/bin/env python3
"""Task-bound Parquet CAN acquisition with frozen sources and bounded decoding."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
from http.client import IncompleteRead
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import traceback
import uuid
from cheyun_prod_client import CAN_LOG_CLASS, CloudNetworkError, CloudServerError, download_to_file, environment_config, load_settings, query_files, safe_filename

DEFINITIONS = Path(__file__).resolve().parents[1] / "references/can-signals.json"
SNAPSHOT_SCHEMA = "uroad-parquet-source-snapshot-v1"
RECEIPT_SCHEMA = "uroad-parquet-download-v1"
PROGRESS_SCHEMA = "uroad-parquet-progress-v1"
COUNTERS = ("files_total", "files_downloaded", "files_decoded", "rows_seen", "points_selected", "unique_points")
STAGES = {"listing", "downloading", "decoding", "writing_result", "completed", "failed"}
DOWNLOAD_ATTEMPTS = 3


class CacheBindingError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("."+path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ordinary(path):
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        raise CacheBindingError("任务缓存包含链接，拒绝读取。")
    if not path.is_file():
        raise CacheBindingError("任务缓存文件缺失，不能确认来源。")


def _read(path):
    _ordinary(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CacheBindingError("任务缓存凭据不可验证。") from None
    if not isinstance(value, dict):
        raise CacheBindingError("任务缓存凭据格式无效。")
    return value


def _paths(args):
    work, output = Path(args.work_dir).absolute(), Path(args.output).absolute()
    if not re.fullmatch(r"run-[A-Za-z0-9-]+", args.run_id):
        raise CacheBindingError("CAN Parquet 缺少有效任务标识。")
    task_root = next((parent for parent in work.parents if parent.name == args.run_id), None)
    if task_root is None or task_root not in output.parents or output == work or work in output.parents:
        raise CacheBindingError("CAN Parquet 缓存和输出必须属于同一任务，且输出位于缓存之外。")
    for candidate in (work, output.parent):
        for path in (candidate, *candidate.parents):
            if path.exists() and (path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())):
                raise CacheBindingError("CAN Parquet 任务路径不能是链接。")
            if path == task_root:
                break
    if task_root.resolve() not in work.resolve().parents or task_root.resolve() not in output.resolve().parents:
        raise CacheBindingError("CAN Parquet 路径超出当前任务。")
    return work.resolve(), output.resolve()


def validate_progress(value):
    required = {"schema", "stage", "elapsed_seconds", *COUNTERS}
    if (not isinstance(value, dict) or set(value) not in (required, required|{"errorType"})
            or value.get("schema") != PROGRESS_SCHEMA or type(value.get("stage")) is not str or value["stage"] not in STAGES
            or any(type(value.get(key)) is not int or value[key] < 0 for key in COUNTERS)
            or type(value.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(value["elapsed_seconds"]) or value["elapsed_seconds"] < 0
            or ("errorType" in value and (not isinstance(value["errorType"], str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value["errorType"]) is None))):
        raise ValueError("Invalid finite Parquet progress")
    return value


class Progress:
    def __init__(self, work):
        self.path, self.started = work/"progress.json", time.monotonic()
        self.last_write = 0.0
        self.value = {"schema": PROGRESS_SCHEMA, "stage": "listing", **dict.fromkeys(COUNTERS, 0), "elapsed_seconds": 0.0}

    def update(self, stage=None, **counts):
        previous_stage = self.value["stage"]
        if stage is not None:
            self.value["stage"] = stage
        if set(counts)-set(COUNTERS)-{"errorType"}:
            raise ValueError("Unknown Parquet progress count")
        self.value.update(counts)
        self.value["elapsed_seconds"] = round(time.monotonic()-self.started, 6)
        validate_progress(self.value)
        now = time.monotonic()
        if previous_stage != self.value["stage"] or self.value["stage"] in {"completed", "failed"} or now-self.last_write >= 2:
            _atomic(self.path, self.value)
            self.last_write = now

    def decode(self, value):
        if not isinstance(value, dict):
            raise ValueError("Invalid decoder progress")
        if value.get("stage") == "failed":
            return
        translated = {target: value[source] for source, target in
                      (("filesRead", "files_decoded"), ("rowsSeen", "rows_seen"),
                       ("pointsSelected", "points_selected"), ("uniquePoints", "unique_points")) if source in value}
        self.update("writing_result" if value.get("stage") == "writing_contract" else "decoding", **translated)


def _backend():
    return importlib.import_module("parquet_can_stream")


def _validate_items(items):
    if not isinstance(items, list) or not items:
        raise CacheBindingError("当前冻结来源没有 CAN Parquet 文件。")
    for item in items:
        if (not isinstance(item, dict)
                or not isinstance(item.get("fileName") or item.get("name"), str)
                or not (item.get("fileName") or item.get("name")).strip()
                or not isinstance(item.get("downloadURL") or item.get("downloadUrl") or item.get("url"), str)
                or not (item.get("downloadURL") or item.get("downloadUrl") or item.get("url")).strip()
                or not safe_filename(item).lower().endswith((".parquet", ".pq"))):
            raise CacheBindingError("CAN Parquet 来源缺少名称、下载地址或格式不符。")


def _snapshot(args, work, binding, options, progress, existing):
    path = work/"sources.json"
    if path.exists():
        snapshot = _read(path)
        proof = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
        if (snapshot.get("schema") != SNAPSHOT_SCHEMA or snapshot.get("binding") != binding
                or snapshot.get("options") != options or snapshot.get("snapshot_sha256") != _digest(proof)):
            raise CacheBindingError("CAN Parquet 来源快照与任务、请求、环境或配置不一致。")
        _validate_items(snapshot.get("items"))
        return snapshot
    if existing:
        raise CacheBindingError("已有任务缓存缺少冻结来源，禁止重新查询或猜测归属。")
    progress.update("listing")
    query = query_files(vin=args.vin, start=args.start, end=args.end, log_class=CAN_LOG_CLASS,
                        username=load_settings()["username"], environment=args.cloud_env, page_size=args.page_size)
    if query.environment != binding["environment"] or query.provider_environment != binding["providerEnvironment"]:
        raise CacheBindingError("CAN Parquet 查询环境与当前任务不一致。")
    items = query.items if args.max_files is None else query.items[:args.max_files]
    _validate_items(items)
    snapshot = {"schema": SNAPSHOT_SCHEMA, "binding": binding, "options": options,
                "items": items, "queryTotal": query.total}
    snapshot["snapshot_sha256"] = _digest(snapshot)
    _atomic(path, snapshot)
    return snapshot


def _download_with_retry(url, source, *, environment):
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            download_to_file(url, source, environment=environment)
            return attempt
        except (IncompleteRead, ConnectionError, TimeoutError, CloudNetworkError, CloudServerError):
            # Retry the same frozen source only. Permissions, binding failures
            # and local disk errors do not enter this network retry path.
            if source.exists() or source.with_name(source.name + ".part").exists():
                raise CacheBindingError("中断下载留下未确认文件，已停止自动重试。") from None
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(min(attempt, 2))


def _downloads(snapshot, work, progress):
    paths = []
    items = snapshot["items"]
    progress.update("downloading", files_total=len(items))
    for number, item in enumerate(items, 1):
        basename = re.sub(r"[^A-Za-z0-9._-]", "_", safe_filename(item, number))[-120:]
        directory = work/"downloads"/f"item-{number:04d}"
        for parent in (directory, directory.parent):
            if parent.exists() and (parent.is_symlink() or bool(getattr(parent, "is_junction", lambda: False)())):
                raise CacheBindingError("下载缓存目录不能是链接。")
        if work.resolve() not in directory.resolve().parents:
            raise CacheBindingError("下载缓存目录超出当前任务。")
        directory.mkdir(parents=True, exist_ok=True)
        source, receipt_path = directory/(f"source-{number:04d}-"+basename), directory/"receipt.json"
        source_binding = {"schema": RECEIPT_SCHEMA, "snapshot_sha256": snapshot["snapshot_sha256"],
                          "ordinal": number, "record_sha256": _digest(item), "path": source.relative_to(work).as_posix()}
        if receipt_path.exists():
            receipt = _read(receipt_path)
            _ordinary(source)
            if (any(receipt.get(key) != value for key, value in source_binding.items())
                    or type(receipt.get("bytes")) is not int or receipt["bytes"] <= 0
                    or receipt["bytes"] != source.stat().st_size or receipt.get("sha256") != _sha(source)):
                raise CacheBindingError("CAN Parquet 下载缓存校验不一致，拒绝复用。")
        else:
            if source.exists() or source.with_name(source.name+".part").exists():
                raise CacheBindingError("CAN Parquet 缓存没有下载回执，不能确认来源。")
            url = item.get("downloadURL") or item.get("downloadUrl") or item.get("url")
            if not isinstance(url, str) or not url:
                raise CacheBindingError("冻结来源缺少下载地址，禁止重新查询。")
            attempts = _download_with_retry(url, source, environment=snapshot["binding"]["environment"])
            _ordinary(source)
            size = source.stat().st_size
            if size <= 0:
                raise CacheBindingError("CAN Parquet 下载为空。")
            _atomic(receipt_path, {**source_binding, "bytes": size, "sha256": _sha(source), "downloadAttempts": attempts})
        paths.append(source)
        progress.update("downloading", files_downloaded=number)
    return paths


def _archive_incomplete(work):
    candidates = [path for path in work.iterdir() if path.name in {"points.sqlite3", "points.sqlite3-wal", "points.sqlite3-shm"}
                  or path.name.startswith("points.sqlite3.building-")]
    if not candidates:
        return
    archive = work/("failed-decode-"+uuid.uuid4().hex)
    archive.mkdir()
    for source in candidates:
        destination = archive/source.name
        if source.resolve().parent != work.resolve() or work.resolve() not in destination.resolve().parents:
            raise CacheBindingError("失败解码缓存超出当前任务，拒绝移动。")
        _ordinary(source)
        source.replace(destination)


def acquire(args):
    if not isinstance(args.vin, str) or re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", args.vin) is None:
        raise CacheBindingError("CAN Parquet VIN 格式无效。")
    try:
        start, end = (datetime.strptime(value, "%Y-%m-%d %H:%M:%S") for value in (args.start, args.end))
    except (TypeError, ValueError):
        raise CacheBindingError("CAN Parquet 请求时间格式无效。") from None
    if start >= end:
        raise CacheBindingError("CAN Parquet 请求时段无效。")
    if type(args.page_size) is not int or args.page_size <= 0 or (args.max_files is not None and (type(args.max_files) is not int or args.max_files <= 0)):
        raise CacheBindingError("CAN Parquet 分页和文件上限参数无效。")
    work, output = _paths(args)
    existing = work.exists() and any(work.iterdir())
    work.mkdir(parents=True, exist_ok=True)
    progress, stage, cache_authorized = Progress(work), "listing", False
    try:
        config = environment_config(args.cloud_env)
        binding = {"runId": args.run_id, "vin": args.vin, "start": args.start, "end": args.end,
                   "environment": args.cloud_env, "providerEnvironment": config["providerEnvironment"]}
        options = {"pageSize": args.page_size, "maxFiles": args.max_files,
                   "definitions_sha256": _sha(DEFINITIONS), "logClass": CAN_LOG_CLASS}
        snapshot = _snapshot(args, work, binding, options, progress, existing)
        cache_authorized = True
        stage = "downloading"
        paths = _downloads(snapshot, work, progress)
        backend = _backend()
        index, metadata = work/"points.sqlite3", work/"contract-metadata.json"
        stage = "decoding"
        if metadata.exists():
            contract = backend.load_indexed_contract(output, index, metadata, binding, DEFINITIONS)
            rows = (contract.get("sourceParquet") or {}).get("rows", 0)
            if type(rows) is int and rows >= 0:
                progress.update("decoding", rows_seen=rows, files_decoded=len(paths))
        else:
            if output.exists():
                raise CacheBindingError("已有 CAN 输出缺少成功回执，拒绝覆盖。")
            _archive_incomplete(work)
            progress.update("decoding")
            summary = backend.build_contract(paths, DEFINITIONS, args.start, args.end, output, index, metadata,
                                             binding, progress=progress.decode, batch_size=4096)
            rows = summary.get("rows", 0)
            if type(rows) is int and rows >= 0:
                progress.update("writing_result", rows_seen=rows, files_decoded=len(paths))
        progress.update("completed")
        return {"ok": True, "files": len(paths), "rows": progress.value["rows_seen"]}
    except Exception as exc:
        if getattr(exc, "stage", None) == "writing_contract" or progress.value["stage"] == "writing_result":
            stage = "writing_result"
        error_type = getattr(exc, "exception_type", type(exc).__name__)
        error = ("CAN Parquet 解析内存不足，已保留任务缓存；请核查后恢复。" if isinstance(exc, MemoryError) or error_type == "ArrowMemoryError"
                 else "CAN Parquet 缓存或来源绑定不一致，已停止恢复。" if isinstance(exc, CacheBindingError)
                 else f"CAN Parquet {stage} 失败（{error_type}），已保留任务缓存；下载地址过期时不会自动重新查询。")
        if not existing or cache_authorized:
            try:
                _atomic(work/"diagnostic.json", {"schema": "uroad-parquet-diagnostic-v1", "stage": stage,
                                               "errorType": error_type, "traceback": traceback.format_exc()[-20000:]})
                progress.update("failed", errorType=error_type)
            except Exception:
                pass
        failure = RuntimeError(error)
        failure.error_type, failure.stage = error_type, stage
        raise failure from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("vin", "start", "end", "output", "work-dir", "run-id"):
        parser.add_argument("--"+field, required=True)
    parser.add_argument("--cloud-env", choices=("prod", "test"), default="prod")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-files", type=int)
    args = parser.parse_args()
    try:
        print(json.dumps(acquire(args), ensure_ascii=False))
        return 0
    except Exception as exc:
        error_type = getattr(exc, "error_type", type(exc).__name__)
        message = (str(exc).strip() if hasattr(exc, "error_type") or isinstance(exc, CacheBindingError)
                   else f"CAN Parquet 处理失败（{error_type}）。")
        message = message or f"CAN Parquet 处理失败（{error_type}）。"
        if isinstance(exc, MemoryError):
            message = "CAN Parquet 解析内存不足，已停止处理。"
        print(json.dumps({"ok": False, "error": message, "errorType": error_type,
                          "stage": getattr(exc, "stage", "listing")}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
