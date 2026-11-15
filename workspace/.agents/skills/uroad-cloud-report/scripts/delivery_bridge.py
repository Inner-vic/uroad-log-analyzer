#!/usr/bin/env python3
"""Deliver a validated Uroad HTML report back to its originating CC Connect session."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
import subprocess
import sys
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_registry import delivery_status, mark_delivery, pending_deliveries
from validate_report_artifact import validate_attachment_path

SEND_TIMEOUT_SECONDS = 90
NATIVE_ACK = "Message sent successfully."
NATIVE_RECEIPT_SCHEMA = "uroad-native-delivery-receipt-v1"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_native_receipt(receipt: Any) -> bool:
    return bool(isinstance(receipt, dict)
        and receipt.get("schema") == NATIVE_RECEIPT_SCHEMA
        and receipt.get("acknowledgement") == NATIVE_ACK
        and type(receipt.get("exitCode")) is int and receipt["exitCode"] == 0
        and isinstance(receipt.get("runId"), str) and receipt["runId"]
        and isinstance(receipt.get("sessionKey"), str) and receipt["sessionKey"].startswith("feishu:oc_")
        and _valid_source_binding(receipt.get("source"))
        and receipt["sessionKey"] == _session_for_source(receipt["source"])
        and receipt.get("payloadKind") in {"report", "text"}
        and isinstance(receipt.get("payloadSha256"), str) and len(receipt["payloadSha256"]) == 64
        and all(character in "0123456789abcdef" for character in receipt["payloadSha256"])
        and type(receipt.get("sizeBytes")) is int and receipt["sizeBytes"] > 0)


def _valid_source_binding(source: Any) -> bool:
    return bool(isinstance(source, dict) and set(source) == {"chatId", "messageId", "requesterId", "threadId"}
                and all(isinstance(source.get(key), str) and source[key].startswith(prefix)
                        for key, prefix in (("chatId", "oc_"), ("messageId", "om_"), ("requesterId", "ou_")))
                and isinstance(source.get("threadId"), str)
                and (not source["threadId"] or source["threadId"].startswith("om_")))


def _source_binding(payload: dict) -> dict:
    task = payload.get("task") or {}
    if not isinstance(task, dict):
        raise ValueError("任务来源绑定无效")
    parts = [task.get(name) or {} for name in ("context", "source", "requester", "routing")]
    if not isinstance(task, dict) or not all(isinstance(part, dict) for part in parts):
        raise ValueError("任务来源绑定无效")
    context, source, requester, routing = parts
    binding = {}
    for key, values in {
        "chatId": (context.get("chatId"), source.get("chatId")),
        "messageId": (context.get("messageId"), source.get("messageId")),
        "requesterId": (context.get("requesterId"), requester.get("id")),
        "threadId": (context.get("threadId"), routing.get("threadId")),
    }.items():
        present = [value for value in values if value is not None and value != ""]
        if any(not isinstance(value, str) or value != value.strip() for value in present) or len(set(present)) > 1:
            raise ValueError("任务来源身份冲突")
        binding[key] = present[0] if present else ""
    if not _valid_source_binding(binding):
        raise ValueError("任务缺少完整可验证的来源，拒绝发送")
    return binding


def _session_for_source(source: dict) -> str:
    suffix = f"root:{source['threadId']}" if source["threadId"] else source["requesterId"]
    return f"feishu:{source['chatId']}:{suffix}"


def _native_receipt(completed, run_id: str, session: str, source: dict, *, report: Path | None = None, text: str | None = None) -> dict:
    # This is the native CLI's existing synchronous /send acknowledgement.
    # It does not expose a Feishu message_id; do not manufacture one.
    if completed.returncode != 0 or NATIVE_ACK not in [line.strip() for line in (completed.stdout or "").splitlines()]:
        raise ValueError("原生发送未返回可验证的成功确认")
    content = text.encode("utf-8") if report is None else None
    receipt = {"schema": NATIVE_RECEIPT_SCHEMA, "runId": run_id, "sessionKey": session, "source": source,
               "payloadKind": "report" if report is not None else "text",
               "payloadSha256": _hash_file(report) if report is not None else hashlib.sha256(content).hexdigest(),
               "sizeBytes": report.stat().st_size if report is not None else len(content),
               "exitCode": 0, "acknowledgement": NATIVE_ACK,
               "confirmedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if report is not None:
        receipt["report"] = str(report)
    return receipt


@contextmanager
def _delivery_lock(run_path: Path):
    path = run_path.parent / ".standard-report-delivery.lock"
    if path.is_symlink():
        raise ValueError("附件交付锁路径无效")
    stream = path.open("a+b")
    locked = False
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
        yield locked
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _save_receipt(path: Path, receipt: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def delivery_succeeded(result: Any) -> bool:
    """Accept only an explicit successful outbound result."""
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return False
    status = str(result.get("status", "")).lower()
    if status in {"failed", "error", "rejected"}:
        return False
    receipt = result.get("receipt")
    return bool(_platform_message_id(result)
                or (_valid_native_receipt(receipt) and receipt["payloadKind"] == "report"))


def _platform_message_id(result: dict) -> str | None:
    for key in ("messageId", "message_id"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def record_delivery_result(run_id: str, result: Any) -> dict:
    """Persist a CC Connect outbound result and return a small audit record."""
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    receipt = result.get("receipt") if isinstance(result, dict) else None
    if delivery_succeeded(result) and (not _valid_native_receipt(receipt) or receipt.get("runId") == run_id):
        message_id = _platform_message_id(result)
        mark_delivery(run_id, "sent", delivered_at=now, error=None, messageId=message_id,
                      receipt=receipt, messageSentAt=now, uploadCompletedAt=now)
        return {"runId": run_id, "status": "sent", "messageId": message_id}
    error = _text(result.get("error") if isinstance(result, dict) else result) or "outbound delivery returned no success evidence"
    mark_delivery(run_id, "delivery_failed", error=error, failureStage=(result.get("failureStage") if isinstance(result, dict) else None))
    return {"runId": run_id, "status": "failed", "error": error}


def retry_candidates() -> list[dict]:
    """Return existing completed reports only; never enqueue analysis."""
    return pending_deliveries()


def validate_before_send(run_json: str | Path, attachment: str | Path) -> dict:
    """Mandatory adapter hook; callers must use returned report path verbatim."""
    return validate_attachment_path(Path(run_json), Path(attachment))


def _load_run(run_json: str | Path) -> tuple[Path, dict]:
    path = Path(run_json).resolve(strict=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("run.json 内容无效")
    return path, payload


def _session_key(payload: dict) -> str:
    source = _source_binding(payload)
    chat_id, requester_id, thread_id = source["chatId"], source["requesterId"], source["threadId"]
    if not chat_id.startswith("oc_"):
        raise ValueError("任务缺少可验证的来源群聊，拒绝发送附件")
    if thread_id:
        return f"feishu:{chat_id}:root:{thread_id}"
    if not requester_id.startswith("ou_"):
        raise ValueError("任务缺少可验证的请求人，拒绝发送附件")
    return f"feishu:{chat_id}:{requester_id}"


def _native_binary() -> Path:
    configured = os.environ.get("UROAD_CC_CONNECT_BIN", "").strip()
    if configured:
        binary = Path(configured).expanduser().resolve(strict=True)
    else:
        # scripts/ -> uroad-cloud-report/ -> skills/ -> .agents/ -> workspace/
        workspace = Path(os.environ.get("CC_CONNECT_WORKSPACE", Path(__file__).resolve().parents[4])).resolve(strict=True)
        binary = (workspace / "build" / "cc-connect-context.exe").resolve(strict=True)
    if binary.name.lower() != "cc-connect-context.exe" or not binary.is_file():
        raise RuntimeError("CC Connect 原生附件发送程序不可用")
    return binary


def _project_args(project: str | None = None) -> list[str]:
    """Let CC Connect select its sole runtime project unless explicitly pinned.

    Newer CC Connect builds append an internal suffix to the configured display
    name, so forwarding the TOML name can target a project that does not exist.
    The native CLI documents project selection as optional when one project is
    active; deployments with more than one project can pin the runtime name via
    UROAD_CC_CONNECT_PROJECT.
    """
    explicit = str(project or os.environ.get("UROAD_CC_CONNECT_PROJECT", "")).strip()
    return ["--project", explicit] if explicit else []


def send_native_text(run_json: str | Path, message: str, *, project: str | None = None) -> dict:
    """Send a normal chat reply to the same verified session as a report."""
    text = str(message or "").strip()
    if not text:
        raise ValueError("飞书回复不能为空")
    if len(text) > 6000:
        text = text[:6000].rstrip() + "\n\n（回复过长，已截断）"
    run_path, payload = _load_run(run_json)
    session = _session_key(payload)
    command = [str(_native_binary()), "send", *_project_args(project), "--session", session, "--message", text]
    completed = subprocess.run(command, check=True, timeout=SEND_TIMEOUT_SECONDS, capture_output=True, text=True, encoding="utf-8", errors="replace")
    receipt = _native_receipt(completed, str(payload.get("runId") or ""), session, _source_binding(payload), text=text)
    return {"ok": True, "runId": payload.get("runId"), "receipt": receipt}

def send_native_report(run_json: str | Path, *, project: str | None = None) -> dict:
    """Send exactly the validated report to its originating Feishu session."""
    try:
        run_path, payload = _load_run(run_json)
        run_id = str(payload.get("runId") or "").strip()
        if not run_id:
            raise ValueError("run.json 缺少 runId")
        with _delivery_lock(run_path) as acquired:
            if not acquired:
                return {"runId": run_id, "status": "in_progress"}
            try:
                # Re-read and validate while holding the same OS lock that covers
                # sending and registering the result, including failure updates.
                _, payload = _load_run(run_path)
                if str(payload.get("runId") or "").strip() != run_id:
                    raise ValueError("任务身份在交付期间发生变化")
                validated = validate_attachment_path(run_path, Path((payload.get("artifacts") or {}).get("report", "")))
                source = _source_binding(payload)
                session = _session_key(payload)
                report = Path(validated["report"])
                if delivery_status(run_id) == "sent":
                    return {"runId": run_id, "status": "already_sent"}
                receipt_path = run_path.parent / "standard-report-delivery-receipt.json"
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if (not _valid_native_receipt(receipt) or receipt.get("runId") != run_id
                            or receipt.get("source") != source
                            or receipt.get("sessionKey") != session or receipt.get("payloadKind") != "report"
                            or receipt.get("report") != str(report) or receipt.get("payloadSha256") != _hash_file(report)
                            or receipt.get("sizeBytes") != report.stat().st_size):
                        raise ValueError("原生附件送达凭据与当前任务不一致，拒绝重复发送")
                else:
                    command = [str(_native_binary()), "send", *_project_args(project), "--session", session, "--file", validated["report"]]
                    completed = subprocess.run(command, check=True, timeout=SEND_TIMEOUT_SECONDS, capture_output=True, text=True, encoding="utf-8", errors="replace")
                    receipt = _native_receipt(completed, run_id, session, source, report=report)
                    _save_receipt(receipt_path, receipt)
                audit = record_delivery_result(run_id, {"ok": True, "receipt": receipt})
                return {**audit, "filename": validated["filename"], "sizeBytes": validated["sizeBytes"]}
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                return _record_failure(run_id, exc)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # No task mutation without the per-run delivery lock.
        return {"status": "failed", "error": str(exc) or type(exc).__name__}


def _record_failure(run_id: str, exc: Exception) -> dict:
    detail = (exc.stderr or exc.stdout or "").strip() if isinstance(exc, subprocess.CalledProcessError) else ""
    error = detail or str(exc) or type(exc).__name__
    return record_delivery_result(run_id, {"ok": False, "error": error, "failureStage": "native_attachment_send"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    send = subparsers.add_parser("send", help="send a validated report through the configured CC Connect bot")
    send.add_argument("--run-json", required=True, type=Path)
    send.add_argument("--project")
    subparsers.add_parser("pending", help="list completed reports that still need delivery")
    args = parser.parse_args()
    if args.command == "pending":
        print(json.dumps(retry_candidates(), ensure_ascii=False, indent=2))
        return 0
    result = send_native_report(args.run_json, project=args.project)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in {"sent", "already_sent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
