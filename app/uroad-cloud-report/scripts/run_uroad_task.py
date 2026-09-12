#!/usr/bin/env python3
"""Queued wrapper around the uroad pipeline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from task_registry import REQUEST_METADATA_ENV, TRIGGER_WORKER_ENV, load_request_metadata, should_trigger_worker
from parse_time_input import parse_time_range, BEIJING

PIPELINE = SCRIPT_ROOT / "run_uroad_pipeline.py"
TASK_WORKER = SCRIPT_ROOT / "task_worker.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--raw-time-expression")
    parser.add_argument("--received-at")
    parser.add_argument("--cloud-env", choices=("auto", "prod", "test"), default="auto")
    parser.add_argument("--eea-platform", default="EEA3.0")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--keep-on-success", action="store_true")
    parser.add_argument("--request-metadata", help="JSON string for requester/source metadata")
    parser.add_argument("--defer-to-worker", action="store_true", help="Register only and let worker consume queued task")
    parser.add_argument("--existing-run-id")
    args = parser.parse_args()

    metadata = {}
    if args.request_metadata:
        metadata = json.loads(args.request_metadata)
    os.environ[REQUEST_METADATA_ENV] = json.dumps(metadata, ensure_ascii=False)
    load_request_metadata()
    if not args.start or not args.end:
        expression = args.raw_time_expression or (metadata.get("rawMessage") if isinstance(metadata, dict) else None)
        if not expression:
            raise SystemExit("缺少完整起止时间，或未提供 raw-time-expression")
        received = datetime.fromisoformat(args.received_at).astimezone(BEIJING) if args.received_at else None
        parsed = parse_time_range(expression, received)
        args.start, args.end = parsed.start, parsed.end
        metadata["rawTimeExpression"] = parsed.original
        metadata["normalizedStart"] = parsed.start
        metadata["normalizedEnd"] = parsed.end
        metadata["timezone"] = "Asia/Shanghai"
        metadata["timeNotice"] = parsed.notice
        os.environ[REQUEST_METADATA_ENV] = json.dumps(metadata, ensure_ascii=False)
    print(json.dumps({"skillPath": str(SCRIPT_ROOT.parent), "rawTimeExpression": metadata.get("rawTimeExpression"), "normalizedStart": args.start, "normalizedEnd": args.end, "timezone": metadata.get("timezone", "Asia/Shanghai")}, ensure_ascii=False), file=sys.stderr)

    command = [
        sys.executable,
        str(PIPELINE),
        "--vin", args.vin,
        "--start", args.start,
        "--end", args.end,
        "--cloud-env", args.cloud_env,
        "--eea-platform", args.eea_platform,
        "--page-size", str(args.page_size),
    ]
    if args.max_files is not None:
        command.extend(["--max-files", str(args.max_files)])
    if args.keep_on_success:
        command.append("--keep-on-success")
    if args.existing_run_id:
        command.extend(["--existing-run-id", args.existing_run_id])

    completed = subprocess.run(command, text=True)
    should_spawn = should_trigger_worker() and (args.defer_to_worker or completed.returncode in (0, 2, 3))
    if should_spawn:
        worker_env = os.environ.copy()
        worker_env[TRIGGER_WORKER_ENV] = "0"
        subprocess.Popen([sys.executable, str(TASK_WORKER), "--loop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=worker_env)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
