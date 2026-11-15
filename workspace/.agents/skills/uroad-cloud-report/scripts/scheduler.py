#!/usr/bin/env python3
"""Bounded local scheduler for heavy Uroad jobs.

Slots are filesystem leases rather than one global lock.  This allows a small,
explicitly configured number of concurrent downloads/parses while retaining a
hard upper bound across all Codex/CC Connect sessions.
"""
from __future__ import annotations

import os
import json
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from task_registry import BusyTaskError, mark_running, runs_root

MAX_HEAVY_JOBS_ENV = "UROAD_MAX_HEAVY_JOBS"
MIN_FREE_GB_ENV = "UROAD_MIN_FREE_GB"
DEFAULT_MAX_HEAVY_JOBS = 2
DEFAULT_MIN_FREE_GB = 20
SLOT_PREFIX = ".heavy-slot-"
SLOT_SUFFIX = ".lock"
RETAINED_LEASE_SCHEMA = "uroad-retained-recovery-lease-v1"
HEAVY_LEASE_SCHEMA = "uroad-heavy-lease-v1"


def _lease_value(path: Path) -> tuple[str, dict | None]:
    """Read bounded legacy or explicitly owned recovery leases, never links."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 4096):
        raise RuntimeError("运行槽位文件无效")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise RuntimeError("运行槽位文件不可验证") from exc
    if not value.startswith("{"):
        return value or "unknown", None
    try:
        payload = json.loads(value)
    except ValueError as exc:
        raise RuntimeError("恢复运行槽位文件无效") from exc
    if (not isinstance(payload, dict) or set(payload) != {"schema", "runId", "pid", "token"}
            or payload.get("schema") not in {RETAINED_LEASE_SCHEMA, HEAVY_LEASE_SCHEMA}
            or type(payload.get("runId")) is not str or not payload["runId"]
            or type(payload.get("pid")) is not int or payload["pid"] <= 0
            or type(payload.get("token")) is not str or re.fullmatch(r"[0-9a-f]{32}", payload["token"]) is None):
        raise RuntimeError("恢复运行槽位文件无效")
    return payload["runId"], payload


def retained_recovery_lease(run_id: str) -> dict:
    """Require exactly one lease for this run, owned by this recovery process."""
    root = runs_root()
    paths = list(root.glob(f"{SLOT_PREFIX}*{SLOT_SUFFIX}"))
    if len(paths) > 1024:
        raise RuntimeError("运行槽位文件数量无效")
    matches = []
    for path in paths:
        if re.fullmatch(r"\.heavy-slot-[0-9]+\.lock", path.name) is None:
            raise RuntimeError("运行槽位文件无效")
        try:
            value, record = _lease_value(path)
        except FileNotFoundError:
            continue
        if value == run_id:
            matches.append(record)
    if (len(matches) != 1 or matches[0] is None or matches[0]["pid"] != os.getpid()
            or matches[0]['schema'] != RETAINED_LEASE_SCHEMA):
        raise RuntimeError("原任务必须由当前恢复进程独占一个运行槽位")
    return matches[0]


def _positive_int(value: str | None, default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是正整数") from exc
    if parsed < 1:
        raise RuntimeError(f"{name} 必须大于 0")
    return parsed


def max_heavy_jobs() -> int:
    return _positive_int(os.environ.get(MAX_HEAVY_JOBS_ENV), DEFAULT_MAX_HEAVY_JOBS, MAX_HEAVY_JOBS_ENV)


def min_free_bytes() -> int:
    gigabytes = _positive_int(os.environ.get(MIN_FREE_GB_ENV), DEFAULT_MIN_FREE_GB, MIN_FREE_GB_ENV)
    return gigabytes * 1024 * 1024 * 1024


def _slot_path(root: Path, index: int) -> Path:
    return root / f"{SLOT_PREFIX}{index}{SLOT_SUFFIX}"


def active_slot_run_ids() -> list[str]:
    try:
        root = runs_root()
    except OSError:
        return []
    values = []
    for index in range(max_heavy_jobs()):
        path = _slot_path(root, index)
        if not path.is_file() or path.is_symlink():
            continue
        try:
            value, _ = _lease_value(path)
        except (OSError, ValueError, RuntimeError):
            value = "unknown"
        values.append(value or "unknown")
    return values


def available_heavy_slots() -> int:
    return max(0, max_heavy_jobs() - len(active_slot_run_ids()))


def interrupted_recovery_lease(run_id: str, expected: dict) -> tuple[Path, dict]:
    if (not isinstance(expected, dict) or set(expected) != {"slotName", "schema", "runId", "pid", "token"}
            or type(expected.get("slotName")) is not str
            or re.fullmatch(r"\.heavy-slot-[0-9]+\.lock", expected["slotName"]) is None
            or expected.get("schema") != RETAINED_LEASE_SCHEMA or expected.get("runId") != run_id
            or type(expected.get("pid")) is not int or not 0 < expected["pid"] <= 0xFFFFFFFF
            or type(expected.get("token")) is not str or re.fullmatch(r"[0-9a-f]{32}", expected["token"]) is None):
        raise RuntimeError("原任务遗留槽位证据无效")
    path = runs_root() / expected["slotName"]
    expected_payload = {key: value for key, value in expected.items() if key != "slotName"}
    found = []
    paths = list(runs_root().glob(f"{SLOT_PREFIX}*{SLOT_SUFFIX}"))
    if len(paths) > 1024:
        raise RuntimeError("运行槽位数量无效")
    for candidate in paths:
        value, payload = _lease_value(candidate)
        if value == run_id:
            found.append((candidate, payload))
    if found != [(path, expected_payload)] or expected_payload.get("pid") == os.getpid():
        raise RuntimeError("原任务遗留槽位已变化或不唯一")
    return path, expected_payload


def replace_recovery_lease(path: Path, expected: dict, replacement: dict) -> None:
    _, current = _lease_value(path)
    if current != expected:
        raise RuntimeError("恢复运行槽位所有权已变化")
    temporary = path.with_name(path.name + ".replace-" + uuid.uuid4().hex)
    encoded = json.dumps(replacement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        if _lease_value(path)[1] != expected:
            raise RuntimeError("恢复运行槽位所有权已变化")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def acquire_interrupted_recovery_slot(run_id: str, *, expected_run_sha256: str, context_binding: dict,
                                      expected_attempt_id: str, expected_lease: dict) -> Iterator[dict]:
    from task_registry import claim_interrupted_recovery
    if shutil.disk_usage(runs_root()).free < min_free_bytes():
        raise RuntimeError("可用磁盘空间不足，无法接管原任务")
    replacement = {"schema": RETAINED_LEASE_SCHEMA, "runId": run_id, "pid": os.getpid(), "token": uuid.uuid4().hex}
    path = None
    identity = None
    try:
        task = claim_interrupted_recovery(run_id, expected_run_sha256=expected_run_sha256,
            context_binding=context_binding, expected_attempt_id=expected_attempt_id,
            expected_lease=expected_lease, replacement_lease=replacement)
        path = runs_root() / expected_lease["slotName"]
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        yield task
    finally:
        if path is not None:
            try:
                info = path.lstat()
                if identity == (info.st_dev, info.st_ino) and _lease_value(path)[1] == replacement:
                    path.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def acquire_heavy_slot(run_id: str) -> Iterator[dict]:
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if free < min_free_bytes():
        required = min_free_bytes() // (1024 * 1024 * 1024)
        available = free // (1024 * 1024 * 1024)
        raise RuntimeError(f"可用磁盘空间不足：剩余 {available} GB，任务要求至少保留 {required} GB")

    lease = None
    identity = None
    encoded = json.dumps({"schema": HEAVY_LEASE_SCHEMA, "runId": run_id,
                          "pid": os.getpid(), "token": uuid.uuid4().hex}, sort_keys=True).encode("utf-8")
    try:
        for index in range(max_heavy_jobs()):
            candidate = _slot_path(root, index)
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            lease = candidate
            try:
                info = os.fstat(fd); identity = (info.st_dev, info.st_ino)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            except BaseException:
                if lease.exists() and (lease.stat().st_dev, lease.stat().st_ino) == identity:
                    lease.unlink()
                raise
            break
        if lease is None:
            raise BusyTaskError({"active": [{"runId": value, "status": "running"} for value in active_slot_run_ids()],
                                 "maxHeavyJobs": max_heavy_jobs()})
        yield mark_running(run_id, require_unstarted=True)
    finally:
        if lease is not None:
            try:
                info = lease.lstat()
                if (identity == (info.st_dev, info.st_ino) and not lease.is_symlink()
                        and not getattr(info, 'st_file_attributes', 0) & 0x400 and lease.read_bytes() == encoded):
                    lease.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def acquire_retained_recovery_slot(run_id: str, *, expected_run_sha256: str, context_binding: dict) -> Iterator[dict]:
    """Reserve capacity, then atomically reopen only one verified retained run.

    Recovery has no queued intermediate state: ordinary workers cannot claim
    the retained task while its original files are being reused.
    """
    from task_registry import claim_retained_recovery
    root = runs_root()
    if shutil.disk_usage(root).free < min_free_bytes():
        raise RuntimeError("可用磁盘空间不足，无法恢复原任务")
    payload = {"schema": RETAINED_LEASE_SCHEMA, "runId": run_id,
               "pid": os.getpid(), "token": uuid.uuid4().hex}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    lease = None
    lease_identity = None
    lease_complete = False
    try:
        for index in range(max_heavy_jobs()):
            candidate = _slot_path(root, index)
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            lease = candidate
            try:
                info = os.fstat(fd)
                lease_identity = (info.st_dev, info.st_ino)
                offset = 0
                while offset < len(encoded):
                    written = os.write(fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("恢复运行槽位写入失败")
                    offset += written
                lease_complete = True
            finally:
                os.close(fd)
            break
        if lease is None:
            raise BusyTaskError({"maxHeavyJobs": max_heavy_jobs()})
        yield claim_retained_recovery(run_id, expected_run_sha256, context_binding)
    finally:
        if lease is not None:
            try:
                info = lease.lstat()
                if (lease_identity == (info.st_dev, info.st_ino)
                        and not lease.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400
                        and (not lease_complete or lease.read_bytes() == encoded)):
                    lease.unlink()
            except FileNotFoundError:
                pass
