#!/usr/bin/env python3
"""Run queued Uroad jobs with bounded parallelism.

Several short-lived workers may coexist.  The filesystem scheduler slots are
the authority for heavy-job capacity; registry claims provide atomic fairness.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scheduler import available_heavy_slots
from task_registry import claim_next_queued_task, execution_options_for_task, mark_claim_start_failed, recover_stale_tasks

RUN_TASK = SCRIPT_ROOT / "run_uroad_task.py"


def task_metadata(task: dict) -> dict:
    context = task.get("context") or {}
    return {
        "requester": {"id": context.get("requesterId")},
        "source": {"chatId": context.get("chatId"), "messageId": context.get("messageId")},
        "routing": {"threadId": context.get("threadId"), "route": "uroad-cloud-report"},
        "rawMessage": context.get("rawMessage"),
        "receivedAt": context.get("receivedAt"),
        "timezone": context.get("timezone") or "Asia/Shanghai",
        "taskRecord": deepcopy(task),
    }


def run_one(task: dict) -> int:
    request = task.get("request") or {}
    options = execution_options_for_task(task)
    command = [
        sys.executable, str(RUN_TASK),
        "--vin", str(request.get("vin", "")),
        "--start", str(request.get("start", "")),
        "--end", str(request.get("end", "")),
        "--cloud-env", str(request.get("cloudEnv", "auto")),
        "--eea-platform", options["eeaPlatform"], "--page-size", str(options["pageSize"]),
        "--existing-run-id", str(task.get("runId", "")),
        "--request-metadata", json.dumps(task_metadata(task), ensure_ascii=False),
    ]
    if options["maxFiles"] is not None:
        command.extend(["--max-files", str(options["maxFiles"])])
    for question in request.get("attachedQuestions") or []:
        command.extend(["--question", str(question)])
    environment = os.environ.copy()
    environment["UROAD_TRIGGER_WORKER"] = "0"
    completed = subprocess.run(command, text=True, encoding="utf-8", env=environment)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="poll until idle timeout")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--idle-seconds", type=float, default=180.0)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.idle_seconds <= 0:
        raise SystemExit("poll-seconds 和 idle-seconds 必须大于 0")

    recover_stale_tasks(max_age_seconds=30)
    last_health = time.monotonic()
    idle_started = time.monotonic()
    while True:
        if time.monotonic() - last_health >= 30:
            recover_stale_tasks(max_age_seconds=30)
            last_health = time.monotonic()
        if available_heavy_slots() < 1:
            time.sleep(args.poll_seconds)
        else:
            task = claim_next_queued_task()
            if task:
                idle_started = time.monotonic()
                try:
                    result = run_one(task)
                except (OSError, RuntimeError, ValueError):
                    result = 1
                if result != 0:
                    mark_claim_start_failed(task['runId'])
                continue
            if not args.loop:
                return 0
            time.sleep(args.poll_seconds)
        if time.monotonic() - idle_started >= args.idle_seconds:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
