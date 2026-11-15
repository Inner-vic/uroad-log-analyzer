#!/usr/bin/env python3
"""Create a Uroad job or return its bounded-scheduler queue state."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from task_registry import (
    REQUEST_METADATA_ENV,
    TRIGGER_WORKER_ENV,
    RegistryError,
    requeue_retryable,
    should_trigger_worker,
)
from parse_time_input import parse_time_range, BEIJING
from request_guard import reject_non_uroad_request
from ccconnect_request_context import RequestContextError, merge_transport_metadata, metadata_from_context_header

PIPELINE = SCRIPT_ROOT / "run_uroad_pipeline.py"
TASK_WORKER = SCRIPT_ROOT / "task_worker.py"


def _json_result(text: str) -> dict:
    try:
        value = json.loads(text.strip())
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _scheduled_worker():
    if os.name != 'nt':
        return None
    workspace=Path(os.environ.get('CC_CONNECT_WORKSPACE') or SCRIPT_ROOT.parents[3]).resolve()
    installation=workspace/'deployment/uroad-dispatcher.installation.json'
    if not installation.exists():
        if SCRIPT_ROOT.resolve().is_relative_to(workspace/'.agents/skills/uroad-cloud-report'):
            raise RuntimeError('独立调度尚未安装，任务仍保留在队列')
        return None
    config=json.loads(installation.read_text(encoding='utf-8-sig'))
    dispatcher=SCRIPT_ROOT/'dispatch_uroad_worker.pyw'
    if (config.get('schema') != 'uroad-dispatcher-installation-v1'
            or config.get('taskName') != 'CC Connect Uroad Dispatcher'
            or Path(config.get('workspace','')).resolve() != workspace
            or Path(config.get('python','')).resolve() != Path(sys.executable).resolve()
            or config.get('dispatcherSha256') != hashlib.sha256(dispatcher.read_bytes()).hexdigest()):
        raise RuntimeError('独立调度部署不一致，未启动执行')
    encoded=base64.b64encode(json.dumps(config).encode('utf-8')).decode('ascii')
    command=("$c=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+encoded+"'))|ConvertFrom-Json;"
        "$t=Get-ScheduledTask -TaskName 'CC Connect Uroad Dispatcher' -ErrorAction Stop;"
        "if($t.State -eq 'Disabled' -or @($t.Actions).Count -ne 1 -or $t.Actions[0].Execute -ne $c.actionExecutable "
        "-or $t.Actions[0].Arguments -ne $c.actionArguments){throw 'Dispatcher deployment mismatch'};"
        "Start-ScheduledTask -TaskName 'CC Connect Uroad Dispatcher' -ErrorAction Stop")
    completed=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
    if completed.returncode:
        raise RuntimeError('独立调度启动失败，任务仍保留在队列')
    return {'started':True,'method':'scheduled_task'}


def start_background_worker() -> dict:
    """The worker owns execution independently of the interactive request."""
    scheduled=_scheduled_worker()
    if scheduled is not None:
        return scheduled
    env = os.environ.copy()
    env[TRIGGER_WORKER_ENV] = "0"
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "env": env, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        options["start_new_session"] = True
    process = subprocess.Popen([sys.executable, str(TASK_WORKER), "--loop"], **options)
    return {'started':True,'method':'detached_process','pid':process.pid}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--raw-time-expression")
    parser.add_argument("--received-at")
    parser.add_argument("--interpretation", help="evidence-bound natural-time interpretation JSON")
    parser.add_argument("--cloud-env", choices=("auto", "prod", "test"), default="auto")
    parser.add_argument("--eea-platform", default="EEA3.0")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--keep-on-success", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--request-metadata", help="JSON object with requester/source/thread ids")
    parser.add_argument("--ccconnect-context", help="transport-generated [cc-connect request_context_v=1 ...] header")
    parser.add_argument("--question", action="append", default=[], help="optional first-message question; stored verbatim for post-report Codex analysis")
    parser.add_argument("--defer-to-worker", action="store_true", help="allow scheduler to start this job when capacity is free")
    parser.add_argument("--foreground", action="store_true", help="maintenance only: wait for a new task in this process")
    parser.add_argument("--existing-run-id", help=argparse.SUPPRESS)
    parser.add_argument("--retry-existing", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.foreground and args.defer_to_worker:
        raise SystemExit("foreground 不能与 defer-to-worker 同用")
    if args.defer_to_worker and args.existing_run_id:
        raise SystemExit("已有领取任务不能重新提交到后台队列")
    deferred = not args.existing_run_id and not args.foreground

    if args.retry_existing and not args.existing_run_id:
        raise SystemExit("retry-existing 必须与 existing-run-id 同用")
    if args.interpretation and (args.start or args.end):
        raise SystemExit("interpretation 不能与 start/end 同用，请提供原始时间表达式进行校验")
    try:
        interpretation = json.loads(args.interpretation) if args.interpretation else None
    except json.JSONDecodeError as exc:
        raise SystemExit("interpretation 必须是 JSON 对象") from exc
    if interpretation is not None and not isinstance(interpretation, dict):
        raise SystemExit("interpretation 必须是 JSON 对象")

    try:
        metadata = json.loads(args.request_metadata) if args.request_metadata else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"request-metadata 必须是 JSON 对象: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SystemExit("request-metadata 必须是 JSON 对象")
    if args.ccconnect_context:
        try:
            metadata = merge_transport_metadata(metadata_from_context_header(args.ccconnect_context), metadata)
        except RequestContextError as exc:
            raise SystemExit(str(exc)) from exc
    reject_non_uroad_request(metadata)
    if not args.start or not args.end:
        expression = args.raw_time_expression or metadata.get("rawMessage")
        if not expression:
            raise SystemExit("缺少完整起止时间，或未提供 raw-time-expression")
        # parse_time_range owns timezone interpretation: a timezone-free
        # receipt timestamp means Beijing time, not this host's local zone.
        received = datetime.fromisoformat(args.received_at) if args.received_at else None
        parsed = parse_time_range(expression, received, interpretation=interpretation)
        args.start, args.end = parsed.start, parsed.end
        metadata.update({"rawTimeExpression": parsed.original, "normalizedStart": args.start, "normalizedEnd": args.end, "timezone": "Asia/Shanghai"})
        metadata["inputNormalization"] = parsed.normalization
    if args.retry_existing:
        try:
            metadata["taskRecord"] = requeue_retryable(args.existing_run_id)
        except RegistryError as exc:
            raise SystemExit(str(exc)) from exc
    os.environ[REQUEST_METADATA_ENV] = json.dumps(metadata, ensure_ascii=False)

    command = [sys.executable, str(PIPELINE), "--vin", args.vin, "--start", args.start, "--end", args.end, "--cloud-env", args.cloud_env, "--eea-platform", args.eea_platform, "--page-size", str(args.page_size)]
    if args.max_files is not None:
        command.extend(["--max-files", str(args.max_files)])
    if args.existing_run_id:
        command.extend(["--existing-run-id", args.existing_run_id])
    if deferred:
        command.append("--enqueue-only")
    for question in args.question:
        command.extend(["--question", question])

    child_environment = os.environ.copy()
    # Windows pipes otherwise inherit the active ANSI code page while this
    # parent decodes them as UTF-8.  Force the pipeline and every Python child
    # it launches to emit UTF-8, and keep replacement decoding as a final
    # guard so a diagnostic can never be hidden by UnicodeDecodeError.
    child_environment["PYTHONIOENCODING"] = "utf-8"
    child_environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=child_environment,
    )
    result = _json_result(completed.stdout)
    queued = bool(result.get("queued"))
    if completed.returncode == 0 and should_trigger_worker() and queued:
        try:
            result["worker"] = start_background_worker()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            result["worker"] = {"started": False, "errorType": type(exc).__name__}
            result.update(ok=False, error="任务已保留在队列，但后台进程启动失败，需要恢复调度。")
            print(json.dumps(result, ensure_ascii=False))
            return 1
    if queued and result:
        result['message']='任务已排队，完成后会在原会话返回标准报告。'
        print(json.dumps(result, ensure_ascii=False))
    elif completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
