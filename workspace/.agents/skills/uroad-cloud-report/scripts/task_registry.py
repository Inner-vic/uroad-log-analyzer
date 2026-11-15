#!/usr/bin/env python3
"""Lightweight task registry and serialization helpers for uroad report runs."""
from __future__ import annotations

import json
import errno
import os
import time
import hashlib
import copy
import re
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

REQUEST_METADATA_ENV = "UROAD_REQUEST_METADATA"
TRIGGER_WORKER_ENV = "UROAD_TRIGGER_WORKER"

SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ENV = "CC_CONNECT_WORKSPACE"
DEFAULT_WORKSPACE = Path(os.environ.get(WORKSPACE_ENV, "").strip()).expanduser() if os.environ.get(WORKSPACE_ENV, "").strip() else SKILL_ROOT.parents[2]
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
    value = os.environ.get(WORKSPACE_ENV, "").strip()
    base = Path(value) if value else DEFAULT_WORKSPACE
    return base.resolve(strict=True)


def runs_root() -> Path:
    workspace = workspace_root()
    root = workspace / RUNS_DIRECTORY
    if root.exists() and (root.is_symlink() or bool(getattr(root, "is_junction", lambda: False)())):
        raise RegistryError("任务输出目录不能是符号链接或目录联接")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise RegistryError("任务输出目录不可用")
    resolved = root.resolve(strict=True)
    if workspace not in resolved.parents:
        raise RegistryError("任务输出目录超出工作区")
    return resolved


def registry_path() -> Path:
    return runs_root() / REGISTRY_FILENAME


def registry_lock_path() -> Path:
    return runs_root() / LOCK_FILENAME


def run_lock_path() -> Path:
    return runs_root() / RUN_LOCK_FILENAME


def worker_lock_path() -> Path:
    return runs_root() / WORKER_LOCK_FILENAME


def _write_registry(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".write-" + uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_registry_file(path: Path) -> None:
    if not path.exists():
        _write_registry(path, {"schemaVersion": "1.1", **runtime_version(), "tasks": []})


@contextmanager
def locked_registry(timeout_seconds: float = 10.0) -> Iterator[dict]:
    path = registry_path()
    lock = registry_lock_path()
    if lock.is_symlink() or getattr(lock,'is_junction',lambda:False)():
        raise RegistryError('任务注册表锁路径无效')
    stream=lock.open('a+b')
    acquired=False
    try:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise RegistryError('任务注册表锁文件无效')
        if info.st_size==0:
            stream.write(b'0');stream.flush()
        began=time.monotonic()
        while True:
            stream.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}:
                    raise RegistryError('任务注册表锁不可用') from exc
                if time.monotonic()-began>=timeout_seconds:
                    raise RegistryError('任务注册表锁等待超时') from None
                time.sleep(.05)
        _ensure_registry_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise RegistryError("任务注册表格式无效")
        yield payload
        _write_registry(path, payload)
    finally:
        if acquired:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()
        # Persistent inode: never unlink it while another waiter may have it
        # open. The OS automatically releases ownership when a process dies.


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
    result = {
        "requester": requester,
        "source": source,
        "routing": routing,
        "raw": payload,
    }
    if "taskRecord" in payload:
        if not isinstance(payload["taskRecord"], dict):
            raise RegistryError("已有任务元数据格式无效")
        result["taskRecord"] = copy.deepcopy(payload["taskRecord"])
    return result


def execution_options_for_task(task: dict) -> dict:
    if not isinstance(task, dict) or not isinstance(task.get("request"), dict):
        raise RegistryError("任务执行请求无效")
    request = task["request"]
    if "executionOptions" not in request:
        return {"eeaPlatform": "EEA3.0", "pageSize": 100, "maxFiles": None}
    options = request["executionOptions"]
    if (not isinstance(options, dict) or set(options) != {"eeaPlatform", "pageSize", "maxFiles"}
            or options["eeaPlatform"] != "EEA3.0" or type(options["pageSize"]) is not int or options["pageSize"] <= 0
            or (options["maxFiles"] is not None and (type(options["maxFiles"]) is not int or options["maxFiles"] <= 0))):
        raise RegistryError("任务执行选项无效")
    return copy.deepcopy(options)


def existing_task_for_execution(run_id: str, identity: RequestIdentity, metadata: dict, *, execution_options: dict | None = None) -> dict:
    """Read an exact queued claim; caller-supplied task copies are evidence only."""
    requester, source, routing = (metadata.get(key) or {} for key in ("requester", "source", "routing"))
    binding = _retained_binding({"requesterId": requester.get("id"), "chatId": source.get("chatId"),
                                "messageId": source.get("messageId"), "threadId": routing.get("threadId")})
    with locked_registry() as payload:
        matches = [task for task in payload["tasks"] if task.get("runId") == run_id]
        if len(matches) != 1:
            raise RegistryError("已有任务没有唯一注册记录")
        task = matches[0]
        _retained_check_aliases(task, binding)
        request = task.get("request")
        if (not isinstance(request, dict) or request.get("key") != identity.key()
                or any(request.get(key) != value for key, value in
                       (("vin", identity.vin), ("start", identity.start), ("end", identity.end), ("cloudEnv", identity.cloud_env)))
                or task.get("status") not in {"queued", "claimed"}
                or (task.get("execution") or {}).get("status") != task.get("status")):
            raise RegistryError("已有任务请求或领取状态不一致")
        supplied = metadata.get("taskRecord")
        if supplied is not None:
            _retained_check_aliases(supplied, binding)
            if supplied.get("runId") != run_id or supplied.get("request") != request:
                raise RegistryError("已有任务元数据与注册记录冲突")
        options = execution_options_for_task(task)
        if execution_options is not None and execution_options != options:
            raise RegistryError("已有任务执行选项与原请求不一致")
        return copy.deepcopy(task)


def _matching_source_message(tasks: list[dict], metadata: dict) -> dict | None:
    """Deduplicate transport retries, never legitimate repeated analyses."""
    message_id = _clean_mapping(metadata.get("source")).get("messageId") or (metadata.get("raw") or {}).get("messageId")
    if not message_id:
        return None
    for task in reversed(tasks):
        context = task.get("context") or {}
        source = task.get("source") or {}
        if str(context.get("messageId") or source.get("messageId") or "") == str(message_id):
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


def _attached_questions(value: object) -> list[str]:
    """Retain optional first-message questions without interpreting them."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        question = str(item or "").strip()
        if question and question not in result:
            result.append(question)
    return result


def register_or_reuse(
    run_id: str,
    vin: str,
    start: str,
    end: str,
    cloud_env: str,
    metadata: dict | None = None,
    attached_questions: list[str] | None = None,
    execution_options: dict | None = None,
    prepare_callback=None,
) -> dict:
    identity = request_identity(vin, start, end, cloud_env)
    created_at = utc_now()
    metadata = metadata or {}
    with locked_registry() as payload:
        tasks = payload["tasks"]
        existing = _matching_source_message(tasks, metadata)
        if existing:
            # The same Feishu event can be delivered more than once.  It must
            # not create a second job, while a later message with identical
            # VIN/time is still a legitimate new analysis request.
            raise DuplicateTaskError(existing, reusable=True)
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
                "attachedQuestions": _attached_questions(attached_questions),
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
        if execution_options is not None:
            task["request"]["executionOptions"] = copy.deepcopy(execution_options)
            execution_options_for_task(task)
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
        task["context"]["accessScope"] = "thread" if task["context"].get("chatId") and task["context"].get("threadId") else "owner"
        _new_task_compat_fields(task)
        # Persist the initial run before a queued record becomes claimable.
        # A callback failure aborts the registry transaction entirely.
        if prepare_callback is not None:
            prepare_callback(copy.deepcopy(task))
        tasks.append(task)
        refresh_queue_positions(tasks)
        return task



def next_queued_task() -> dict | None:
    with locked_registry() as payload:
        tasks = payload["tasks"]
        refresh_queue_positions(tasks)
        for task in tasks:
            if task.get("status") == "queued":
                return dict(task)
    return None


def claim_next_queued_task() -> dict | None:
    """Claim queued work fairly across requesters when metadata is available."""
    with locked_registry() as payload:
        tasks = payload["tasks"]
        refresh_queue_positions(tasks)
        queued = [task for task in tasks if task.get("status") == "queued"]
        if not queued:
            return None
        scheduler = payload.setdefault("scheduler", {})
        last_requester = str(scheduler.get("lastRequesterId") or "")
        selected = next((task for task in queued if str((task.get("context") or {}).get("requesterId") or "") != last_requester), queued[0])
        selected["status"] = "claimed"
        selected["startedAt"] = utc_now()
        selected["claimantPid"] = os.getpid()
        selected.setdefault("execution", {})["status"] = "claimed"
        scheduler["lastRequesterId"] = str((selected.get("context") or {}).get("requesterId") or "")
        refresh_queue_positions(tasks)
        return dict(selected)


def mark_claim_start_failed(run_id: str) -> bool:
    """The claiming worker may close its own child that never began execution."""
    previous=None
    changed=False
    try:
        with locked_registry() as payload:
            task = next((t for t in payload['tasks'] if t.get('runId') == run_id), None)
            if not task or task.get('status') != 'claimed' or task.get('claimantPid') != os.getpid():
                return False
            path = runs_root()/run_id/'run.json'
            message='后台启动检查失败，任务未进入日志分析；请排查后重新提交。'
            if any((path.parent/name).exists() for name in ('raw','input','slices','analysis','report','diagnostics','merge-state')):
                return False
            if path.is_symlink() or getattr(path.parent,'is_junction',lambda:False)():
                return False
            if path.is_file():
                previous=path.read_bytes();run=json.loads(previous)
                if (run.get('runId') != run_id or run.get('status') != 'queued'
                        or run.get('request') != task.get('request')):
                    return False
                run.update(status='system_error',error={'type':'WorkerStartFailed','message':message,'retryable':False},completedAt=utc_now())
                _write_registry(path,run);changed=True
            task.update(status='system_error',error=message,completedAt=utc_now())
            task['execution'].update(status='system_error',completedAt=task['completedAt'])
            task.setdefault('notification',{})['status']='pending'
            refresh_queue_positions(payload['tasks'])
        return True
    except BaseException:
        if changed:
            temporary=path.with_name(path.name+'.restore-'+uuid4().hex)
            temporary.write_bytes(previous);temporary.replace(path)
        raise


def recover_stale_tasks(max_age_seconds: int = 600) -> list[dict]:
    """Recover dead unstarted claims; retain interrupted execution for repair.

    Age alone never proves a task dead. Inspect exact ownership before taking
    the registry lock, then compare the original record again under that lock.
    """
    try:
        snapshots = json.loads(registry_path().read_text(encoding="utf-8"))["tasks"]
    except (OSError, ValueError, KeyError):
        return []
    recovered = []
    from scheduler import _lease_value, HEAVY_LEASE_SCHEMA
    for snapshot in snapshots[:10000]:
        if len(recovered) >= 50:
            break
        status = snapshot.get("status")
        if status not in {"claimed", "running"}:
            continue
        age = _age_seconds(snapshot.get("startedAt"))
        owner = snapshot.get("workerPid") if status == "running" else snapshot.get("claimantPid")
        if (age is None or age < max_age_seconds or type(owner) is not int or owner <= 0
                or _retained_pid_alive(owner)):
            continue
        run_id = snapshot.get("runId")
        if type(run_id) is not str or re.fullmatch(r"run-[A-Za-z0-9_-]{1,100}", run_id) is None:
            continue
        directory = runs_root() / run_id
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            continue
        try:
            leases = []
            for path in runs_root().glob('.heavy-slot-*.lock'):
                lease_run, value = _lease_value(path)
                if lease_run == run_id:
                    if (value is None or value["schema"] != HEAVY_LEASE_SCHEMA or value["pid"] != owner):
                        raise RegistryError("旧槽位需要显式恢复，不能自动接管")
                    leases.append((path, path.read_bytes()))
            if len(leases) > 1:
                continue
            path = directory / 'run.json'
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                continue
            previous = path.read_bytes() if path.is_file() else None
            run = json.loads(previous) if previous else None
            evidence = any((directory/name).exists() for name in ('raw','input','slices','analysis','report','merge-state','diagnostics','original','recovery'))
            fresh = status == 'claimed' and not evidence and not leases and (run is None or run.get('status') == 'queued')
            if not fresh:
                if (not isinstance(run, dict) or run.get('runId') != run_id or run.get('status') != 'running'
                        or run.get('request') != snapshot.get('request')
                        or (run.get('task') or {}).get('context') != snapshot.get('context')
                        or run.get('recovery') or (run.get('reportValidation') or {}).get('status') == 'passed'):
                    continue
                _retained_workers_exited(run_id)
            changed_run = False
            try:
                with locked_registry() as payload:
                    task = next((t for t in payload['tasks'] if t.get('runId') == run_id), None)
                    if task != snapshot or _retained_pid_alive(owner) or (path.read_bytes() if path.exists() else None) != previous:
                        continue
                    if any(p.read_bytes() != raw for p,raw in leases):
                        continue
                    if fresh:
                        task.update(status='queued',startedAt=None)
                        task['execution']['status']='queued'
                        task.pop('claimantPid',None)
                        task.setdefault('queue',{})['reason']='dead-unstarted-claim'
                    else:
                        diagnostic = directory/'diagnostics'
                        if diagnostic.is_symlink() or getattr(diagnostic,'is_junction',lambda:False)():
                            raise RegistryError('诊断目录不可验证')
                        diagnostic.mkdir(exist_ok=True)
                        archive = diagnostic/('interrupted-state-'+uuid4().hex+'.json')
                        with archive.open('xb') as stream:stream.write(previous);stream.flush();os.fsync(stream.fileno())
                        message='执行进程已中断，已有输入和诊断已保留，需要受控恢复。'
                        run.update(status='system_error',error={'type':'ExecutionInterrupted','message':message,'retryable':False,
                            'stage':run.get('stage') or (run.get('chunks',{}).get('active') or {}).get('stage')},completedAt=utc_now())
                        run['failureRetention']={'policy':'task_retention','reason':'execution_interrupted','archive':archive.relative_to(directory).as_posix()}
                        _write_registry(path,run);changed_run=True
                        task.update(status='system_error',error=message,completedAt=run['completedAt'])
                        task['execution'].update(status='system_error',completedAt=run['completedAt'])
                        task.setdefault('notification',{})['status']='pending'
                    refresh_queue_positions(payload['tasks'])
                recovered.append(copy.deepcopy(task))
            except BaseException:
                if changed_run:
                    temporary=path.with_name(path.name+'.restore-'+uuid4().hex)
                    temporary.write_bytes(previous);temporary.replace(path)
                raise
            for lease_path, raw in leases:
                if lease_path.is_file() and not lease_path.is_symlink() and lease_path.read_bytes()==raw:
                    lease_path.unlink()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            # Unknown ownership, active child, drift, or malformed state is
            # intentionally left for explicit maintenance, never blind replay.
            continue
    return recovered


def mark_running(run_id: str, *, require_unstarted: bool = False) -> dict:
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                if require_unstarted and task.get('status') not in {'queued','claimed'}:
                    raise RegistryError('任务已被执行进程领取，拒绝重复运行')
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


def requeue_retryable(run_id: str) -> dict:
    """Requeue one failed run only when its persisted failure is retryable.

    This keeps the original task identity and delivery routing intact.  It is
    deliberately separate from the normal state machine so terminal records
    cannot be reopened accidentally by workers or duplicate Feishu events.
    """
    run_path = runs_root() / run_id / "run.json"
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RegistryError(f"任务状态不可读，无法重试: {run_id}") from exc
    error = run.get("error") or {}
    if error.get("retryable") is not True:
        raise RegistryError(f"任务错误未标记为可重试: {run_id}")

    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") != run_id:
                continue
            current = LEGACY_STATUS_MAP.get(task.get("status"), task.get("status"))
            if current != "system_error":
                raise RegistryError(f"仅 system_error 任务可重试，当前状态: {current}")
            retry = task.setdefault("retry", {})
            retry["count"] = int(retry.get("count", 0)) + 1
            retry["requestedAt"] = utc_now()
            retry["lastError"] = task.get("error") or error.get("message")
            task["status"] = "queued"
            execution = task.setdefault("execution", {})
            execution["status"] = "queued"
            execution.pop("completedAt", None)
            task["completedAt"] = None
            task["startedAt"] = None
            task.pop("error", None)
            task.pop("workerPid", None)
            task.pop("lockOwner", None)
            task.pop("heartbeatAt", None)
            task.setdefault("queue", {})["reason"] = "retryable-error"
            task.setdefault("notification", {})["status"] = "pending"
            refresh_queue_positions(payload["tasks"])
            return dict(task)
    raise RegistryError(f"未找到任务记录: {run_id}")


def _retained_binding(value: object) -> dict:
    keys = {"requesterId", "chatId", "messageId", "threadId"}
    if not isinstance(value, dict) or set(value) != keys:
        raise RegistryError("恢复来源身份格式无效")
    result = {}
    for key in keys:
        item = value[key]
        if key == "threadId" and item is None:
            item = ""
        if (type(item) is not str or item != item.strip()
                or (not item and key != "threadId") or len(item) > 512
                or any(ord(character) < 32 for character in item)):
            raise RegistryError("恢复来源身份格式无效")
        result[key] = item
    return result


def _retained_read_run(run_id: str, expected_sha: str) -> dict:
    if (type(run_id) is not str or re.fullmatch(r"run-[0-9]{8}-[0-9]{6}-[0-9]{6}-[0-9a-f]{8}", run_id) is None
            or type(expected_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None):
        raise RegistryError("恢复任务标识或校验值无效")
    root = workspace_root() / RUNS_DIRECTORY
    path = root / run_id / "run.json"
    try:
        # Reject every workspace-relative reparse component before opening the
        # original bytes; resolving a junction first would lose that evidence.
        current = workspace_root()
        for component in path.relative_to(current).parts:
            current = current / component
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RegistryError("恢复任务路径不能包含链接")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024 * 1024:
            raise RegistryError("恢复任务状态文件无效")
        with path.open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RegistryError("恢复任务状态文件过大")
        after = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or hashlib.sha256(raw).hexdigest() != expected_sha):
            raise RegistryError("原任务状态已变化，拒绝恢复")
        run = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise RegistryError("原任务状态不可验证，拒绝恢复") from exc
    if not isinstance(run, dict) or run.get("runId") != run_id:
        raise RegistryError("恢复任务状态身份不一致")
    return run


def _retained_check_aliases(record: dict, binding: dict, *, require_context: bool = True) -> None:
    if not isinstance(record, dict):
        raise RegistryError("原任务来源身份不一致")
    context = record.get("context")
    if require_context or context is not None:
        if not isinstance(context, dict) or _retained_binding({key: context.get(key) for key in binding}) != binding:
            raise RegistryError("原任务来源身份不一致")
    aliases = (("requester", "id", "requesterId"), ("source", "chatId", "chatId"),
               ("source", "messageId", "messageId"), ("routing", "threadId", "threadId"))
    for section, field, canonical in aliases:
        value = record.get(section)
        if value is not None and not isinstance(value, dict):
            raise RegistryError("原任务来源别名无效")
        if isinstance(value, dict) and field in value:
            actual = value[field]
            if canonical == "threadId" and actual is None:
                actual = ""
            if actual != binding[canonical] or type(actual) is not str:
                raise RegistryError("原任务来源别名冲突")
    for section in ("request", "input", "source", "context"):
        value = record.get(section)
        if isinstance(value, dict):
            for key in binding:
                if key in value:
                    actual = value[key]
                    if key == "threadId" and actual is None:
                        actual = ""
                    if type(actual) is not str or actual != binding[key]:
                        raise RegistryError("原任务来源字段冲突")


def _retained_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            # Only ERROR_INVALID_PARAMETER proves the PID does not exist.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            return not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _retained_validate(run: dict, task: dict, binding: dict, run_id: str, *, interrupted: bool = False,
                       expected_attempt_id: str | None = None) -> None:
    if not isinstance(task, dict):
        raise RegistryError("原任务注册状态无效")
    if task.get("runId") != run_id or task.get("taskId", run_id) != run_id:
        raise RegistryError("原任务注册身份不一致")
    embedded = run.get("task")
    for record in (task, embedded):
        _retained_check_aliases(record, binding)
        for section in ("execution", "delivery", "reportValidation", "steps", "artifact", "artifacts", "retry", "notification"):
            if record.get(section) is not None and not isinstance(record[section], dict):
                raise RegistryError("原任务状态字段无效")
    _retained_check_aliases(run, binding, require_context=False)
    for section in ("chunks", "error", "environmentSelection", "delivery", "reportValidation", "steps", "artifact", "artifacts"):
        if run.get(section) is not None and not isinstance(run[section], dict):
            raise RegistryError("原任务状态字段无效")
    if embedded.get("runId") != run_id or embedded.get("taskId", run_id) != run_id:
        raise RegistryError("原任务快照身份不一致")
    request = task.get("request")
    if (not isinstance(request, dict) or request != embedded.get("request") or request != run.get("request")
            or not isinstance(run.get("input"), dict)):
        raise RegistryError("原任务请求快照不一致")
    source_input = run["input"]
    for field, input_field in (("vin", "vin"), ("start", "start"), ("end", "end"), ("cloudEnv", "cloud_env")):
        if type(request.get(field)) is not str or request[field] != source_input.get(input_field):
            raise RegistryError("原任务请求与输入不一致")
    if request["cloudEnv"] not in {"auto", "prod", "test"}:
        raise RegistryError("原任务请求环境无效")
    if request.get("key") != request_identity(request["vin"], request["start"], request["end"], request["cloudEnv"]).key():
        raise RegistryError("原任务请求标识不一致")
    for field, canonical in (("normalizedStart", "start"), ("normalizedEnd", "end")):
        if field in request and request[field] != request[canonical]:
            raise RegistryError("原任务规范时间不一致")
    # run.task is the pipeline's last running snapshot, not its terminal mirror.
    expected_status = "running" if interrupted else "system_error"
    if (run.get("status") != expected_status or task.get("status") != expected_status
            or (task.get("execution") or {}).get("status") != expected_status):
        raise RegistryError("仅原任务明确失败终态允许保留恢复")
    error = run.get("error")
    active = (run.get("chunks") or {}).get("active") or {}
    if not isinstance(active, dict) or not isinstance(active.get("lanes"), dict):
        raise RegistryError("原任务活动切片无效")
    lanes = active.get("lanes") or {}
    if not isinstance(lanes.get("uroad"), dict) or not isinstance(lanes.get("can"), dict):
        raise RegistryError("原任务分支状态无效")
    if ((not interrupted and (not isinstance(error, dict) or error.get("retryable") is not True))
            or (interrupted and error is not None)
            or active.get("status") != ("running" if interrupted else "failed")
            or (active.get("source"), active.get("stage")) not in
               ({("can", "parquet_can"), ("uroad+can", "merge")} if interrupted else {("can", "parquet_can")})
            or active.get("branchesDrained") is not True
            or (lanes.get("uroad") or {}).get("status") != "completed"
            or (lanes.get("can") or {}).get("status") != ("completed" if interrupted else "failed")
            or (lanes.get("can") or {}).get("stage") != "parquet_can"):
        raise RegistryError("原任务不满足保留 CAN 恢复条件")
    for field, expected in (("source", "can"), ("stage", "parquet_can"), ("slice", active.get("index"))):
        if not interrupted and field in error and error[field] != expected:
            raise RegistryError("原任务失败来源不一致")
    selected_record = (run.get("environmentSelection") or {}).get("selected")
    if not isinstance(selected_record, dict):
        raise RegistryError("原任务环境选择无效")
    selected = selected_record.get("environment")
    if type(selected) is not str or selected not in {"prod", "test"} or active.get("environment") != selected:
        raise RegistryError("原任务切片环境不一致")
    for field, expected in (("resolved_cloud_env", selected),
                            ("resolved_provider_environment", selected_record.get("providerEnvironment"))):
        if field in source_input and source_input[field] != expected:
            raise RegistryError("原任务已解析环境别名冲突")
    if ((request["cloudEnv"] != "auto" and request["cloudEnv"] != selected)
            or (run["environmentSelection"].get("mode") != request["cloudEnv"])):
        raise RegistryError("原任务环境策略不一致")
    for record in (run, task, embedded):
        if (record.get("delivery") or {}).get("status") in {"sent", "already_sent"} or record.get("deliveryStatus") == "sent":
            raise RegistryError("已交付任务不可恢复")
        if ((record.get("reportValidation") or {}).get("status") == "passed"
                or (record.get("steps") or {}).get("report") == "completed"
                or any((record.get(section) or {}).get("report") for section in ("artifact", "artifacts"))):
            raise RegistryError("已有报告任务不可恢复")
    pid = task.get("workerPid")
    if embedded.get("workerPid") is not None and embedded["workerPid"] != pid:
        raise RegistryError("原任务进程快照不一致")
    if pid is not None and (type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or _retained_pid_alive(pid)):
        raise RegistryError("原任务工作进程尚未确认退出")
    if interrupted:
        attempts = (run.get("recovery") or {}).get("attempts")
        if (type(pid) is not int or type(expected_attempt_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", expected_attempt_id) is None
                or not isinstance(attempts, list) or not 0 < len(attempts) < 16
                or any(not isinstance(item, dict) for item in attempts)
                or attempts[-1].get("attemptId") != expected_attempt_id or attempts[-1].get("status") != "running"
                or any(item.get("status") not in {"failed", "interrupted"} for item in attempts[:-1])):
            raise RegistryError("原任务没有唯一待接管的恢复尝试")
        root = runs_root() / run_id
        for target in (root / "manifest.json", root / "analysis" / "analysis.json", root / "analysis" / "can-signals.json"):
            if target.exists() or target.is_symlink():
                raise RegistryError("已有最终材料，禁止接管重算")
        report = root / "report"
        if report.is_symlink() or (report.exists() and (not report.is_dir() or any(report.iterdir()))):
            raise RegistryError("已有报告材料，禁止接管重算")


def _retained_checked_validate(run: dict, task: dict, binding: dict, run_id: str, **options) -> None:
    try:
        _retained_validate(run, task, binding, run_id, **options)
    except (AttributeError, TypeError, KeyError, ValueError, OverflowError) as exc:
        raise RegistryError("原任务保留状态结构无效") from exc


def _retained_workers_exited(run_id: str) -> None:
    """Inspect only exact-job owned Python command metadata, never log contents."""
    if os.name != "nt":
        raise RegistryError("中断接管仅支持受控 Windows 进程检查")
    script_names = "run_uroad_task.py|run_uroad_pipeline.py|resume_uroad_task.py|parquet_can_signals.py|parse_uroad.py|cheyun_uroad_adapter.py|stream_can_signals.py|build_html_report.py|validate_report_artifact.py"
    script_pattern = "(" + "|".join(re.escape(name) for name in script_names.split("|")) + ")"
    command = ("@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.ProcessId -ne "
               + str(os.getpid()) + " -and $_.CommandLine -like '*" + run_id + "*' -and $_.CommandLine -match '"
               + script_pattern + "' } | ForEach-Object { [int]$_.ProcessId }) | ConvertTo-Json -Compress")
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", timeout=15)
        if completed.returncode != 0 or len(completed.stdout) > 4096:
            raise RegistryError("原任务子进程状态不可验证")
        pids = json.loads(completed.stdout) if completed.stdout.strip() else []
        if type(pids) is int:
            pids = [pids]
        if not isinstance(pids, list) or any(type(pid) is not int or pid <= 0 for pid in pids) or pids:
            raise RegistryError("原任务仍有工作子进程，禁止接管")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RegistryError("原任务子进程状态不可验证") from exc


def validate_interrupted_recovery(run_id: str, *, expected_run_sha256: str, context_binding: dict,
                                   expected_attempt_id: str, expected_lease: dict) -> dict:
    """Local read-only preflight; the actual claim repeats this under its lock."""
    from scheduler import interrupted_recovery_lease
    binding = _retained_binding(context_binding)
    run = _retained_read_run(run_id, expected_run_sha256)
    _, lease = interrupted_recovery_lease(run_id, expected_lease)
    try:
        payload = json.loads((workspace_root() / RUNS_DIRECTORY / REGISTRY_FILENAME).read_text(encoding="utf-8"))
        matches = [item for item in payload["tasks"] if isinstance(item, dict) and item.get("runId") == run_id]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RegistryError("原任务注册状态不可验证") from exc
    if len(matches) != 1:
        raise RegistryError("原任务注册记录不唯一")
    task = matches[0]
    _retained_checked_validate(run, task, binding, run_id, interrupted=True, expected_attempt_id=expected_attempt_id)
    if task.get("workerPid") != lease["pid"]:
        raise RegistryError("原任务与遗留槽位进程不一致")
    _retained_workers_exited(run_id)
    return copy.deepcopy(task)


def claim_interrupted_recovery(run_id: str, *, expected_run_sha256: str, context_binding: dict,
                               expected_attempt_id: str, expected_lease: dict, replacement_lease: dict) -> dict:
    """CAS one dead retained lease and its exact running task without queueing."""
    from scheduler import interrupted_recovery_lease, replace_recovery_lease
    binding = _retained_binding(context_binding)
    if (not isinstance(replacement_lease, dict) or set(replacement_lease) != {"schema", "runId", "pid", "token"}
            or replacement_lease.get("schema") != "uroad-retained-recovery-lease-v1"
            or replacement_lease.get("runId") != run_id or replacement_lease.get("pid") != os.getpid()
            or type(replacement_lease.get("token")) is not str
            or re.fullmatch(r"[0-9a-f]{32}", replacement_lease["token"]) is None):
        raise RegistryError("接管运行槽位所有权无效")
    _retained_read_run(run_id, expected_run_sha256)
    path, old = interrupted_recovery_lease(run_id, expected_lease)
    changed = False
    try:
        with locked_registry() as payload:
            matches = [(index, item) for index, item in enumerate(payload["tasks"])
                       if isinstance(item, dict) and item.get("runId") == run_id]
            if len(matches) != 1:
                raise RegistryError("原任务注册记录不唯一")
            index, task = matches[0]
            run = _retained_read_run(run_id, expected_run_sha256)
            _retained_checked_validate(run, task, binding, run_id, interrupted=True, expected_attempt_id=expected_attempt_id)
            if task.get("workerPid") != old["pid"]:
                raise RegistryError("原任务与遗留槽位进程不一致")
            _retained_workers_exited(run_id)
            updated = copy.deepcopy(task)
            previous = updated.get("retry") or {}
            count = previous.get("count", 0)
            if type(count) is not int or not 0 <= count < 16:
                raise RegistryError("原任务恢复次数无效或已达上限")
            now = utc_now()
            updated["retry"] = {**previous, "mode": "interrupted-retained-can-recovery", "count": count + 1,
                "expectedRunSha256": expected_run_sha256, "previousAttemptId": expected_attempt_id,
                "interruptedWorkerPid": old["pid"], "startedAt": now}
            updated.update(status="running", startedAt=now, completedAt=None, workerPid=os.getpid(),
                           heartbeatAt=now, lockOwner=f"retained-pid:{os.getpid()}")
            updated.setdefault("execution", {})["status"] = "running"
            updated["execution"].pop("completedAt", None)
            _retained_read_run(run_id, expected_run_sha256)
            interrupted_recovery_lease(run_id, expected_lease)
            replace_recovery_lease(path, old, replacement_lease)
            changed = True
            payload["tasks"][index] = updated
            refresh_queue_positions(payload["tasks"])
        return copy.deepcopy(updated)
    except BaseException:
        if changed:
            replace_recovery_lease(path, replacement_lease, old)
        raise


def claim_retained_recovery(run_id: str, expected_run_sha256: str, context_binding: dict) -> dict:
    """Explicit retained retry protocol; never queue or rewrite original run.json."""
    binding = _retained_binding(context_binding)
    run = _retained_read_run(run_id, expected_run_sha256)
    from scheduler import retained_recovery_lease
    retained_recovery_lease(run_id)
    path = registry_path()
    try:
        initial = json.loads(path.read_text(encoding="utf-8"))
        tasks = initial["tasks"]
        if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
            raise RegistryError("原任务注册结构无效")
        matches = [task for task in tasks if task.get("runId") == run_id]
        if len(matches) != 1:
            raise RegistryError("原任务注册记录不唯一")
        _retained_checked_validate(run, matches[0], binding, run_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RegistryError("原任务注册状态不可验证") from exc
    with locked_registry() as payload:
        if any(not isinstance(task, dict) for task in payload["tasks"]):
            raise RegistryError("原任务注册结构无效")
        matches = [(index, task) for index, task in enumerate(payload["tasks"]) if task.get("runId") == run_id]
        if len(matches) != 1:
            raise RegistryError("原任务注册记录不唯一")
        index, task = matches[0]
        # Re-read exact original bytes and repeat every check under the registry
        # lock. Rejections do not mutate payload or persist a queued state.
        run = _retained_read_run(run_id, expected_run_sha256)
        lease = retained_recovery_lease(run_id)
        _retained_checked_validate(run, task, binding, run_id)
        updated = copy.deepcopy(task)
        previous = updated.get("retry") or {}
        count = previous.get("count", 0)
        if type(count) is not int or not 0 <= count < 16:
            raise RegistryError("原任务恢复次数无效或已达上限")
        now = utc_now()
        updated["retry"] = {**previous, "mode": "retained-can-recovery", "count": count + 1,
                            "expectedRunSha256": expected_run_sha256, "startedAt": now,
                            "lastError": copy.deepcopy(task.get("error") or run["error"])}
        updated.update(status="running", startedAt=now, completedAt=None,
                       workerPid=os.getpid(), heartbeatAt=now, lockOwner=f"retained-pid:{lease['pid']}")
        updated.setdefault("execution", {})["status"] = "running"
        updated["execution"].pop("completedAt", None)
        updated.pop("error", None)
        updated.setdefault("notification", {})["status"] = "pending"
        _retained_read_run(run_id, expected_run_sha256)
        if retained_recovery_lease(run_id) != lease:
            raise RegistryError("恢复运行槽位所有权已变化")
        payload["tasks"][index] = updated
        refresh_queue_positions(payload["tasks"])
        return copy.deepcopy(updated)


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
            owner = (task.get("context") or {}).get("requesterId")
            legacy_owner = (task.get("request") or {}).get("requesterId")
            if not owner or legacy_owner not in (None, "", owner):
                raise RegistryError("任务所有者记录不一致")
            if not requester_id or (requester_id != owner and requester_id not in (allowed_requesters or set())):
                raise RegistryError("无权取消此任务")
            status = LEGACY_STATUS_MAP.get(task.get("status"), task.get("status"))
            if status == "queued":
                task["status"] = "cancelled"
                task.setdefault("execution", {})["status"] = "cancelled"
                task["completedAt"] = utc_now()
                task.setdefault("notification", {})["status"] = "pending"
            elif status == "running":
                raise RegistryError("任务已在运行，当前入口不支持安全取消；任务尚未停止")
            else:
                return dict(task)
            return dict(task)
    raise RegistryError(f"未找到任务记录: {run_id}")


def delivery_status(run_id: str) -> str:
    """Return the persisted delivery status for one task."""
    with locked_registry() as payload:
        for task in payload["tasks"]:
            if task.get("runId") == run_id:
                return str((task.get("delivery") or {}).get("status", "pending"))
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
            if delivery.get("status", "pending") not in {"pending", "failed", "delivery_failed"}:
                continue
            report = (task.get("artifacts") or {}).get("report")
            if report:
                result.append(dict(task))
        return result


def should_trigger_worker() -> bool:
    return os.environ.get(TRIGGER_WORKER_ENV, "1").strip().lower() not in {"0", "false", "no"}
