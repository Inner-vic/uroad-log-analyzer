#!/usr/bin/env python3
"""Durable, access-scoped context for Uroad report follow-ups.

This module never infers a target from conversational memory.  A follow-up is
accepted only when its job id and immutable Feishu context pass deterministic
checks.  Analysis code may use the returned local paths, but must not inspect
other run directories.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from task_registry import RegistryError, locked_registry, utc_now
from ccconnect_request_context import RequestContextError, followup_identity_from_context

RETENTION_DAYS_ENV = "UROAD_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 14


def retention_days() -> int:
    raw = os.environ.get(RETENTION_DAYS_ENV, str(DEFAULT_RETENTION_DAYS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RegistryError(f"{RETENTION_DAYS_ENV} 必须是正整数") from exc
    if value < 1:
        raise RegistryError(f"{RETENTION_DAYS_ENV} 必须大于 0")
    return value


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=retention_days())).astimezone().isoformat(timespec="seconds")


def _find_task(payload: dict, job_id: str) -> dict:
    for task in payload.get("tasks", []):
        if task.get("runId") == job_id:
            return task
    raise RegistryError(f"未找到任务: {job_id}")


def _text(value: object) -> str:
    return str(value or "").strip()


def _same(value: object, expected: object) -> bool:
    return bool(_text(value) and _text(expected) and _text(value) == _text(expected))


def _authorize(task: dict, *, requester_id: str, chat_id: str, thread_id: str) -> None:
    context = task.get("context") or {}
    owner = _text(context.get("requesterId"))
    stored_chat = _text(context.get("chatId"))
    stored_thread = _text(context.get("threadId"))
    scope = _text(context.get("accessScope")) or "owner"
    if not owner or not requester_id:
        raise RegistryError("任务缺少可验证的提问者身份；请在原报告线程中重新发起，或由管理员补齐消息元数据")
    if stored_chat and not _same(chat_id, stored_chat):
        raise RegistryError("追问不属于创建该任务的飞书会话")
    if _same(requester_id, owner):
        return
    if scope == "thread" and stored_thread and _same(thread_id, stored_thread):
        return
    raise RegistryError("无权读取其他任务的数据；请回复原报告线程或使用本人创建的任务")


def register_data_context(job_id: str, run_dir: Path) -> dict:
    run_dir = run_dir.resolve(strict=True)
    data = {
        "version": 1,
        "createdAt": utc_now(),
        "retentionUntil": _deadline(),
        "runDir": str(run_dir),
        "paths": {
            "original": str(run_dir / "original"),
            "raw": str(run_dir / "original"),
            "input": str(run_dir / "original"),
            "analysis": str(run_dir / "analysis" / "analysis.json"),
            "canSignals": str(run_dir / "analysis" / "can-signals.json"),
            "report": str(run_dir / "report"),
        },
    }
    with locked_registry() as payload:
        task = _find_task(payload, job_id)
        task["dataContext"] = data
        entries = task.setdefault("followups", [])
        request = task.get("request") or {}
        context = task.get("context") or {}
        for index, question in enumerate(request.get("attachedQuestions") or [], 1):
            text = _text(question)
            if not text:
                continue
            origin_message_id = _text(context.get("messageId"))
            message_id = f"{origin_message_id}:attached:{index}" if origin_message_id else f"attached:{index}"
            if any(_same(item.get("messageId"), message_id) for item in entries):
                continue
            active = any(item.get("status") == "running" for item in entries)
            entries.append({
                "questionId": f"q-{uuid4().hex[:12]}",
                "messageId": message_id,
                "originMessageId": origin_message_id or None,
                "question": text,
                "requesterId": _text(context.get("requesterId")),
                "chatId": _text(context.get("chatId")),
                "threadId": _text(context.get("threadId")),
                "dataVersion": data["version"],
                "createdAt": utc_now(),
                "status": "queued" if active else "running",
                "source": "initial_message",
            })
    context_path = run_dir / "followup-context.json"
    context_path.write_text(json.dumps({"jobId": job_id, **data}, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def enqueue_followup(job_id: str, *, question: str, requester_id: str, chat_id: str, thread_id: str, message_id: str) -> dict:
    question = question.strip()
    if not question:
        raise RegistryError("追问不能为空")
    with locked_registry() as payload:
        task = _find_task(payload, job_id)
        _authorize(task, requester_id=requester_id, chat_id=chat_id, thread_id=thread_id)
        data = task.get("dataContext") or {}
        deadline = _text(data.get("retentionUntil"))
        run_dir = Path(_text(data.get("runDir"))) if _text(data.get("runDir")) else None
        if not deadline or not run_dir or not run_dir.is_dir():
            raise RegistryError("该任务的数据上下文已不可用；请明确确认后重新拉取数据")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(deadline).astimezone(timezone.utc):
            raise RegistryError("该任务的数据保留期已结束；请明确确认后重新拉取数据")
        for item in task.setdefault("followups", []):
            if message_id and _same(item.get("messageId"), message_id):
                return dict(item)
        active = any(item.get("status") == "running" for item in task["followups"])
        entry = {
            "questionId": f"q-{uuid4().hex[:12]}",
            "messageId": message_id or None,
            "question": question,
            "requesterId": requester_id,
            "chatId": chat_id,
            "threadId": thread_id,
            "dataVersion": data.get("version", 1),
            "createdAt": utc_now(),
            "status": "queued" if active else "running",
        }
        task["followups"].append(entry)
        return {**entry, "jobId": job_id, "dataContext": data}


def resolve_followup_task(*, requester_id: str, chat_id: str, thread_id: str, message_id: str, question: str) -> str:
    """Resolve a follow-up only from its verified owner/chat/thread scope."""
    explicit = re.search(r"#?(run-[A-Za-z0-9-]+)", question or "", re.I)
    with locked_registry() as payload:
        if explicit:
            task = _find_task(payload, explicit.group(1))
            _authorize(task, requester_id=requester_id, chat_id=chat_id, thread_id=thread_id)
            return str(task["runId"])
        candidates: list[dict] = []
        for task in payload.get("tasks", []):
            if (task.get("execution") or {}).get("status", task.get("status")) != "completed":
                continue
            context = task.get("context") or {}
            if not _same(context.get("chatId"), chat_id):
                continue
            if not _same(context.get("requesterId"), requester_id):
                continue
            data = task.get("dataContext") or {}
            if not data.get("runDir"):
                continue
            # A reply thread is a strong match.  In one-to-one chats without
            # an outbound message id, owner+chat is allowed only when unique.
            if thread_id and _same(context.get("threadId"), thread_id):
                candidates.append(task)
            elif not thread_id:
                candidates.append(task)
        if len(candidates) == 1:
            return str(candidates[0]["runId"])
        if not candidates:
            raise RegistryError("未找到可追问的已完成日志任务；请回复报告所在会话，或携带 #job_id")
        raise RegistryError("当前聊天存在多个可追问任务；请回复目标报告所在会话，或携带 #job_id")


def claim_runnable_followup(job_id: str, runner_id: str) -> dict | None:
    """Atomically claim one task-local running follow-up for a worker."""
    with locked_registry() as payload:
        task = _find_task(payload, job_id)
        data = task.get("dataContext") or {}
        for entry in task.setdefault("followups", []):
            if entry.get("status") == "running" and not entry.get("runnerId"):
                entry["runnerId"] = runner_id
                entry["startedAt"] = entry.get("startedAt") or utc_now()
                return {"entry": dict(entry), "dataContext": dict(data)}
    return None
def retry_followup(job_id: str, question_id: str) -> dict:
    """Requeue a failed task-local question without touching its report data."""
    with locked_registry() as payload:
        task = _find_task(payload, job_id)
        entries = task.setdefault("followups", [])
        entry = next((item for item in entries if item.get("questionId") == question_id), None)
        if entry is None:
            raise RegistryError(f"未找到追问: {question_id}")
        prior_status = entry.get("status")
        prior_summary = _text(entry.get("summary"))
        if prior_status != "failed" and not (prior_status == "completed" and ("本地只读通道异常" in prior_summary or "原始日志未能成功读取" in prior_summary)):
            raise RegistryError("只有失败的追问可以重试")
        active = any(item is not entry and item.get("status") == "running" for item in entries)
        entry["status"] = "queued" if active else "running"
        entry["retryCount"] = int(entry.get("retryCount", 0)) + 1
        entry["retriedAt"] = utc_now()
        entry.pop("runnerId", None)
        entry.pop("completedAt", None)
        entry.pop("summary", None)
        return dict(entry)

def complete_followup(job_id: str, question_id: str, *, summary: str, status: str = "completed") -> dict:
    if status not in {"completed", "failed"}:
        raise RegistryError("追问状态必须为 completed 或 failed")
    with locked_registry() as payload:
        task = _find_task(payload, job_id)
        entries = task.setdefault("followups", [])
        for entry in entries:
            if entry.get("questionId") == question_id:
                entry["status"] = status
                entry["completedAt"] = utc_now()
                entry["summary"] = summary[:2000]
                entry.pop("runnerId", None)
                if status == "completed":
                    for queued in entries:
                        if queued.get("status") == "queued":
                            queued["status"] = "running"
                            queued["startedAt"] = utc_now()
                            break
                return dict(entry)
    raise RegistryError(f"未找到追问: {question_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("register-data")
    create.add_argument("--job-id", required=True)
    create.add_argument("--run-dir", type=Path, required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--job-id", required=True)
    enqueue.add_argument("--question", required=True)
    enqueue.add_argument("--requester-id", default="")
    enqueue.add_argument("--chat-id", default="")
    enqueue.add_argument("--thread-id", default="")
    enqueue.add_argument("--ccconnect-context", help="transport-generated [cc-connect request_context_v=1 ...] header")
    enqueue.add_argument("--message-id", default="")
    finish = commands.add_parser("complete")
    retry = commands.add_parser("retry")
    retry.add_argument("--job-id", required=True)
    retry.add_argument("--question-id", required=True)
    finish.add_argument("--job-id", required=True)
    finish.add_argument("--question-id", required=True)
    finish.add_argument("--summary", required=True)
    finish.add_argument("--status", choices=("completed", "failed"), default="completed")
    args = parser.parse_args()
    try:
        if args.command == "enqueue" and args.ccconnect_context:
            identity = followup_identity_from_context(args.ccconnect_context)
            for field, expected in identity.items():
                supplied = getattr(args, field)
                if supplied and supplied != expected:
                    raise RegistryError(f"手工 {field} 不能覆盖 CC Connect 请求上下文")
                setattr(args, field, expected)
        if args.command == "enqueue" and not all((args.requester_id, args.chat_id, args.message_id)):
            raise RegistryError("追问缺少可验证的请求上下文；请传入 --ccconnect-context 或完整的身份字段")
        if args.command == "register-data":
            result = register_data_context(args.job_id, args.run_dir)
        elif args.command == "enqueue":
            result = enqueue_followup(args.job_id, question=args.question, requester_id=args.requester_id, chat_id=args.chat_id, thread_id=args.thread_id, message_id=args.message_id)
        elif args.command == "retry":
            result = retry_followup(args.job_id, args.question_id)
        else:
            result = complete_followup(args.job_id, args.question_id, summary=args.summary, status=args.status)
    except (OSError, RegistryError, RequestContextError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=__import__('sys').stderr)
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())