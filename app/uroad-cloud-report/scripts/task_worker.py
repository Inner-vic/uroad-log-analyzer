#!/usr/bin/env python3
"""Consume queued uroad tasks serially."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from task_registry import REQUEST_METADATA_ENV, claim_next_queued_task, mark_dequeued, recover_stale_tasks, run_lock_path, worker_lock_path

RUN_TASK = SCRIPT_ROOT / "run_uroad_task.py"


def acquire_worker_lock() -> int:
    return os.open(worker_lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)


def release_worker_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            worker_lock_path().unlink()
        except FileNotFoundError:
            pass


def run_once() -> int:
    if run_lock_path().exists():
        return 0
    task = claim_next_queued_task()
    if not task:
        return 0
    request = task.get("request") or {}
    metadata = {
        "requester": task.get("requester") or {},
        "source": task.get("source") or {},
        "routing": {**(task.get("routing") or {}), "triggerType": (task.get("routing") or {}).get("triggerType") or "queued"},
        "taskRecord": task,
    }
    command = [
        sys.executable,
        str(RUN_TASK),
        "--vin", str(request.get("vin", "")),
        "--start", str(request.get("start", "")),
        "--end", str(request.get("end", "")),
        "--cloud-env", str(request.get("cloudEnv", "auto")),
        "--existing-run-id", str(task.get("runId", "")),
        "--request-metadata", json.dumps(metadata, ensure_ascii=False),
    ]
    try:
        completed = subprocess.run(command, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # The task was claimed before launching the child.  If the child
        # cannot even be started, do not leave the task stuck in claimed.
        mark_dequeued(str(task.get("runId", "")))
        print(json.dumps({"ok": False, "requeued": True, "runId": task.get("runId"), "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    if completed.returncode == 3:
        mark_dequeued(str(task.get("runId", "")))
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Keep polling the queue")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()

    try:
        fd = acquire_worker_lock()
    except FileExistsError:
        print(json.dumps({"ok": False, "error": "task worker already running"}, ensure_ascii=False), file=sys.stderr)
        return 2

    try:
        recovered = recover_stale_tasks()
        if recovered:
            print(json.dumps({"ok": True, "recovered": [task.get("runId") for task in recovered]}, ensure_ascii=False))
        if not args.loop:
            return run_once()
        idle_polls = 0
        while True:
            code = run_once()
            if code == 0:
                idle_polls += 1
                if idle_polls >= 2:
                    return 0
            else:
                idle_polls = 0
            if code not in (0, 2, 3):
                return code
            time.sleep(max(args.poll_seconds, 0.5))
    finally:
        release_worker_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
