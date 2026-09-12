#!/usr/bin/env python3
"""Lightweight task registry and serialization helpers for uroad report runs."""
from __future__ import annotations

import json
import os
import time
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

REQUEST_METADATA_ENV = "UROAD_REQUEST_METADATA"
TRIGGER_WORKER_ENV = "UROAD_TRIGGER_WORKER"

DEFAULT_WORKSPACE = Path("/workspace")
RUNS_DIRECTORY = Path("outputs") / "uroad-report-runs"
REGISTRY_FILENAME = "task-registry.json"
LOCK_FILENAME = ".task-registry.lock"
RUN_LOCK_FILENAME = ".pipeline-active.lock"
WORKER_LOCK_FILENAME = ".task-worker.lock"
NO_DATA_DEBOUNCE_SECONDS = 120
DELIVERY_MAX_ATTEMPTS = 5
DELIVERY_RETRY_SECONDS = 15 * 60
NOTIFICATION_MAX_ATTEMPTS = 5
NOTIFICATION_RETRY_SECONDS = 15 * 60
SKILL_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = SKILL_ROOT / "VERSION"
SKILL_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.is_file() else "unknown"
CODE_VERSION = hashlib.sha256(VERSION_FILE.read_bytes()).hexdigest() if VERSION_FILE.is_file() else "unknown"
EXECUTION_STATUSES = {"queued", "running", "completed", "no_data", "partial_data", "system_error", "cancelled"}
DELIVERY_STATUSES = {"pending", "uploading", "sent", "delivery_failed"}
LEGACY_STATUS_MAP = {"failed": "system_error", "interrupted": "system_error", "claimed": "running"}
ALLOWED_TRANSITIONS = {
    # queued -> terminal is retained for crash-safe/test/failure bookkeeping;
    # normal execution still claims queued before pipeline work starts.
    "queued": {"running", "completed", "no_data", "partial_data", "system_error", "cancelled"},
    "running": {"completed", "no_data", "partial_data", "system_error", "cancelled"},
    "completed": set(), "no_data": set(), "partial_data": set(), "system_error": set(), "cancelled": set(),
}


class RegistryError(RuntimeError):
    """Task registry failure."""


class DuplicateTaskError(RuntimeError):
    """Raised when an equivalent task is active, reusable, or in debounce."""

    def __init__(self, task: dict, *, reusable: bool):
        self.task = task
        self.reusable = reusable
        super().__init__(task.get("runId") or "duplicate-task")


def _clean_mapping(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if item is not None}


def runtime_version() -> dict:
    return {"skillVersion": SKILL_VERSION, "codeVersion": CODE_VERSION, "skillPath": str(SKILL_ROOT)}


class BusyTaskError(RuntimeError):
    """Raised when another heavy pipeline currently holds the execution lock."""

    def __init__(self, active: dict):
        self.active = active
        super().__init__(active.get("runId") or "busy")


@dataclass(frozen=True)
class RequestIdentity:
    vin: str
    start: str
    end: str
    cloud_env: str

    def key(self) -> str:
        return f"{self.vin}|{self.start}|{self.end}|{self.cloud_env}"


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def workspace_root() -> Path:
    value = os.environ.get("OPENCLAW_WORKSPACE", "").strip()
    base = Path(value) if value else DEFAULT_WORKSPACE
    return base.resolve(strict=True)


def runs_root() -> Path:
    return (workspace_root() / RUNS_DIRECTORY).resolve(strict=True)


def registry_path() -> Path:
    return runs_root() / REGISTRY_FILENAME


def registry_lock_path() -> Path:
    return runs_root() / LOCK_FILENAME


def run_lock_path() -> Path:
    return runs_root() / RUN_LOCK_FILENAME


def worker_lock_path() -> Path:
    return runs_root() / WORKER_LOCK_FILENAME


def _ensure_registry_file(path: Path) -> None:
    if not path.exists():
        payload = {"schemaVersion": "1.1", **runtime_version(), "tasks": []}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def locked_registry(timeout_seconds: float = 10.0) -> Iterator[dict]:
    path = registry_path()
    lock = registry_lock_path()
    _ensure_registry_file(path)
    started = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() - started >= timeout_seconds:
                raise RegistryError("任务注册表锁等待超时")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise RegistryError("任务注册表格式无效")
        yield payload
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def request_identity(vin: str, start: str, end: str, cloud_env: str) -> RequestIdentity:
    return RequestIdentity(vin=vin.upper(), start=start, end=end, cloud_env=cloud_env)


def load_request_metadata() -> dict:
    raw = os.environ.get(REQUEST_METADATA_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"请求元数据 JSON 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError("请求元数据必须是对象")
    requester = _clean_mapping(payload.get("requester"))
    source = _clean_mapping(payload.get("source"))
    routing = _clean_mapping(payload.get("routing"))
    return {
        "requester": requester,
        "source": source,
        "routing": routing,
        "raw": payload,
    }


def _matching_task(tasks: list[dict], identity: RequestIdentity) -> dict | None:
    for task in reversed(tasks):
        request = task.get("request") or {}
        if request.get("key") == identity.key():
            return task
    return None


def queue_position(tasks: list[dict], *, before_run_id: str | None = None) -> int:
    waiting = [task for task in tasks if task.get("status") == "queued"]
    if before_run_id is None:
        return len(waiting)
    for index, task in enumerate(waiting):
        if task.get("runId") == before_run_id:
            return index
    return len(waiting)


def refresh_queue_positions(tasks: list[dict]) -> None:
    waiting_index = 0
    for task in tasks:
        status = task.get("status")
        if status == "queued":
            task["queuePosition"] = waiting_index
            waiting_index += 1
        elif status in {"claimed", "running"}:
            task["queuePosition"] = 0
        else:
            task["queuePosition"] = None


def _task_report_exists(task: dict) -> bool:
    report = (task.get("artifact") or {}).get("report") or (task.get("artifacts") or {}).get("report")
    return bool(report and Path(str(report)).is_file())


def _new_task_compat_fields(task: dict) -> None:
    """Generate legacy fields once; new code reads the canonical fields."""
    request, context, execution, artifact = task["request"], task["context"], task["execution"], task["artifact"]
    task["requester"] = {"id": context.get("requesterId")} if context.get("requesterId") else {}
    task["source"] = {"chatId": context.get("chatId"), "messageId": context.get("messageId")}
    task["routing"] = {"threadId": context.get("threadId"), "triggerType": "new"}
    task["artifacts"] = dict(artifact)
    task["status"] = execution["status"]


def _age_seconds(timestamp: object) -> float | None:
    try:
        return max(0.0, datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(str(timestamp)).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def register_or_reuse(run_id: str, vin: str, start: str, end: str, cloud_env: str, metadata: dict | None = None) -> dict:
    identity = request_identity(vin, start, end, cloud_env)
    created_at = utc_now()
    metadata = metadata or {}
    with locked_registry() as payload:
        tasks = payload["tasks"]
        existing = _matching_task(tasks, identity)
        status = (existing or {}).get("status")
        if existing and status in {"queued", "claimed", "running"}:
            raise DuplicateTaskError(existing, reusable=False)
        if existing and status == "completed" and _task_report_exists(existing):
            raise DuplicateTaskError(existing, reusable=True)
        if existing and status == "no_data":
            age = _age_seconds(existing.get("completedAt") or existing.get("updatedAt"))
            if age is not None and age < NO_DATA_DEBOUNCE_SECONDS:
                raise DuplicateTaskError(existing, reusable=False)
        task = {
            "runId": run_id,
            "taskId": run_id,
            "status": "queued",
            "createdAt": created_at,
            "queuedAt": created_at,
            "startedAt": None,
            "completedAt": None,
            "request": {
                "key": identity.key(),
                "vin": identity.vin,
                "start": identity.start,
                "end": identity.end,
                "cloudEnv": identity.cloud_env,
            },
            **runtime_version(),
            "version": runtime_version(),
            "context": {},
            "artifact": {},
            "delivery": {"status": "pending"},
            "execution": {"status": "queued"},
            "notification": {"status": "pending"},
        }
        request_raw = metadata.get("raw") or {}
        task["request"].update({
            "rawTimeExpression": request_raw.get("rawTimeExpression"),
            "normalizedStart": start,
            "normalizedEnd": end,
        })
        task["context"] = {
            key: request_raw.get(key) or created_at if key == "receivedAt" else request_raw.get(key)
            for key in ("requesterId", "chatId", "messageId", "threadId", "rawMessage", "receivedAt", "timezone")
        }
        task["context"]["timezone"] = task["context"].get("timezone") or "Asia/Shanghai"
        task["context"]["requesterId"] = task["context"].get("requesterId") or (metadata.get("requester") or {}).get("id")
        task["context"]["chatId"] = task["context"].get("chatId") or (metadata.get("source") or {}).get("chatId")
        task["context"]["messageId"] = task["context"].get("messageId") or (metadata.get("source") or {}).get("messageId")
        task["context"]["threadId"] = task["context"].get("threadId") or (metadata.get("routing") or {}).get("threadId")
        _new_task_compat_fields(task)
        tasks.append(task)
        refresh_queue_positions(tasks)
        return task


@contextmanager
def acquire_run_slot(run_id: str) -> Iterator[dict]:
    lock = run_lock_path()
    started = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() - started >= 1.0:
                active = current_active_task()
                raise BusyTaskError(active or {"runId": "unknown", "status": "running"})
            time.sleep(0.1)
    try:
        os.write(fd, run_id.encode("utf-8"))
        os.close(fd)
        task = mark_running(run_id)
        yield task
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def current_active_task() -> dict | None:
    with locked_registry() as payload:
        tasks = payload["tasks"]
        for task in reversed(tasks):
            if task.get("status") in {"claimed", "running"}:
                return task
    return None


def next_queued_task() -> dict | None:
    with locked_registry() as payload:
        tasks = payload["tasks"]
        refresh_queue_positions(tasks)
        for task in tasks:
            if task.get("status") == "queued":
                return dict(task)
    return None


def claim_next_queued_task() -> dict | None:
    with locked_registry() as payload:
        tasks = payload["tasks"]
        refresh_queue_positions(tasks)
        for task in tasks:
            if task.get("status") == "queued":
                task["status"] = "claimed"
                task["startedAt"] = utc_now()
                task.setdefault("execution", {})["status"] = "claimed"
                refresh_queue_positions(tasks)
                return dict(task)
    return None


def recover_stale_tasks(max_age_seconds: int = 600) -> list[dict]:
    """Requeue old claimed/running records when no pipeline lock exists.

    This is intentionally conservative: an active pipeline lock prevents
    recovery, and only records with a parseable startedAt older than the
    threshold are touched.
    """
    if run_lock_path().exists():
        return []
    now = datetime.now(timezone.utc).timestamp()
    recovered: list[dict] = []
    with locked_registry() as payload:
        tasks = payload["tasks"]
        for task in tasks:
            if task.get("status") not in {"claimed", "running"}:
                continue
            started = task.get("startedAt")
            try:
                age = now - datetime.fromisoformat(str(started)).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            if age < max_age_seconds:
                continue
            task["status"] = "queued"
            task["startedAt"] = None
            task.setdefault("queue", {})["reason"] = "stale-recovery"
            recovered.append(dict(task))
        if recovered:
            refresh_queue_positions(tasks)
    return recovered


def mark_running(run_id: str) -> dict:
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                current = LEGACY_STATUS_MAP.get(task.get("status"), task.get("status"))
                if "running" not in ALLOWED_TRANSITIONS.get(current, set()) and current != "running":
                    raise RegistryError(f"非法状态迁移: {current} -> running")
                task.setdefault("execution", {})["status"] = "running"
                task["status"] = "running"  # legacy mirror
                task["startedAt"] = task.get("startedAt") or utc_now()
                task["workerPid"] = os.getpid()
                task["heartbeatAt"] = utc_now()
                task["lockOwner"] = f"pid:{os.getpid()}"
                refresh_queue_positions(payload["tasks"])
                return dict(task)
    raise RegistryError(f"未找到任务记录: {run_id}")


def mark_dequeued(run_id: str) -> dict:
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                task.setdefault("execution", {})["status"] = "queued"
                task["status"] = "queued"  # legacy mirror
                task["startedAt"] = None
                refresh_queue_positions(payload["tasks"])
                return dict(task)
    raise RegistryError(f"未找到任务记录: {run_id}")


def mark_finished(run_id: str, status: str, *, report: str | None = None, error: str | None = None) -> None:
    if status not in EXECUTION_STATUSES or status == "queued":
        raise RegistryError(f"无效执行状态: {status}")
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                current = LEGACY_STATUS_MAP.get(task.get("status"), task.get("status"))
                if status != current and status not in ALLOWED_TRANSITIONS.get(current, set()):
                    raise RegistryError(f"非法状态迁移: {current} -> {status}")
                task.setdefault("execution", {})["status"] = status
                task["status"] = status  # legacy mirror
                task["completedAt"] = utc_now()
                task["execution"]["completedAt"] = task["completedAt"]
                task["heartbeatAt"] = utc_now()
                task.setdefault("notification", {})["status"] = "pending"
                task["notification"]["queuedAt"] = task["completedAt"]
                if report:
                    task.setdefault("artifact", {})["report"] = report
                    task["artifacts"] = dict(task["artifact"])  # legacy mirror
                if error:
                    task["error"] = error
                refresh_queue_positions(payload["tasks"])
                return
    raise RegistryError(f"未找到任务记录: {run_id}")


def heartbeat(run_id: str, stage: str | None = None) -> dict:
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                task["heartbeatAt"] = utc_now()
                if stage:
                    task["stage"] = stage
                return dict(task)
    raise RegistryError(f"未找到任务记录: {run_id}")


def request_cancel(run_id: str, requester_id: str | None = None, allowed_requesters: set[str] | None = None) -> dict:
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") != run_id:
                continue
            owner = str((task.get("request") or {}).get("requesterId") or "")
            if allowed_requesters is not None and requester_id not in allowed_requesters and requester_id != owner:
                raise RegistryError("无权取消此任务")
            status = LEGACY_STATUS_MAP.get(task.get("status"), task.get("status"))
            if status == "queued":
                task["status"] = "cancelled"
                task.setdefault("execution", {})["status"] = "cancelled"
                task["completedAt"] = utc_now()
                task.setdefault("notification", {})["status"] = "pending"
            elif status == "running":
                task["cancelRequestedAt"] = utc_now()
            else:
                return dict(task)
            return dict(task)
    raise RegistryError(f"未找到任务记录: {run_id}")


def mark_delivery(run_id: str, status: str, *, delivered_at: str | None = None, error: str | None = None, **fields: object) -> None:
    if status == "failed":
        status = "delivery_failed"
    if status not in DELIVERY_STATUSES:
        raise RegistryError(f"无效交付状态: {status}")
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                delivery = task.setdefault("delivery", {})
                delivery["status"] = status
                delivery["attemptCount"] = int(delivery.get("attemptCount", 0)) + 1
                delivery["lastAttemptAt"] = utc_now()
                delivery["attempts"] = delivery["attemptCount"]  # legacy alias
                for key, value in fields.items():
                    if value is not None:
                        delivery[key] = value
                if status == "sent":
                    delivery["deliveredAt"] = delivered_at or utc_now()
                    delivery.pop("error", None)
                elif status == "delivery_failed":
                    delivery["error"] = error or "附件发送失败"
                    delivery["lastError"] = delivery["error"]
                    task["deliveryStatus"] = "delivery_failed"
                return
    raise RegistryError(f"未找到任务记录: {run_id}")


def mark_notification(run_id: str, status: str, *, error: str | None = None, **fields: object) -> None:
    if status not in {"pending", "sent", "notification_failed"}:
        raise RegistryError(f"无效通知状态: {status}")
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                notification = task.setdefault("notification", {})
                notification["status"] = status
                notification["attemptCount"] = int(notification.get("attemptCount", 0)) + (0 if status == "pending" else 1)
                if status != "pending":
                    notification["lastAttemptAt"] = utc_now()
                for key, value in fields.items():
                    if value is not None:
                        notification[key] = value
                if error:
                    notification["error"] = error
                    notification["lastError"] = error
                elif status == "sent":
                    notification.pop("error", None)
                return
    raise RegistryError(f"未找到任务记录: {run_id}")


def pending_deliveries() -> list[dict]:
    """Return completed executions whose report still needs delivery."""
    with locked_registry() as payload:
        result = []
        for task in payload["tasks"]:
            execution = task.get("execution") or {}
            delivery = task.get("delivery") or {}
            if execution.get("status", task.get("status")) != "completed":
                continue
            if delivery.get("status", "pending") not in {"pending", "failed"}:
                continue
            report = (task.get("artifacts") or {}).get("report")
            if report:
                result.append(dict(task))
        return result


def should_trigger_worker() -> bool:
    return os.environ.get(TRIGGER_WORKER_ENV, "1").strip().lower() not in {"0", "false", "no"}
