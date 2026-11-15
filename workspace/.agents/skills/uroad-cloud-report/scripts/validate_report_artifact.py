#!/usr/bin/env python3
"""Validate the completed HTML artifact before a CC Connect delivery adapter sends it."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from run_uroad_pipeline import DEFAULT_WORKSPACE, RUNS_DIRECTORY, WORKSPACE_ENV, build_report_filename


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def validate(run_json: Path, *, allow_validating: bool = False) -> dict:
    if type(allow_validating) is not bool:
        raise ValueError("报告校验模式无效")
    workspace_value = os.environ.get(WORKSPACE_ENV, "").strip()
    workspace_base = Path(workspace_value) if workspace_value else DEFAULT_WORKSPACE
    workspace = workspace_base.resolve(strict=True)
    run_path = run_json.resolve(strict=True)
    if not _inside(run_path, workspace) or run_path.name != "run.json":
        raise ValueError("run.json 不在当前 CC Connect 工作区内")

    payload = json.loads(run_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("status") not in ({"completed", "validating_report"} if allow_validating else {"completed"}):
        raise ValueError("本次运行尚未成功完成")
    request = payload.get("input")
    artifacts = payload.get("artifacts")
    if not isinstance(request, dict) or not isinstance(artifacts, dict):
        raise ValueError("run.json 缺少输入或附件信息")
    if payload.get("status") == "validating_report":
        steps = payload.get("steps")
        if (payload.get("reportValidation") != {"status": "pending"}
                or not isinstance(steps, dict) or steps.get("report") != "completed"):
            raise ValueError("报告尚未进入受控的完成前校验阶段")
        task = payload.get("task") or {}
        if not isinstance(task, dict):
            raise ValueError("报告校验缺少完整任务来源")
        context = task.get("context", {})
        source = task.get("source", {})
        requester = task.get("requester", {})
        if not all(isinstance(item, dict) for item in (task, context, source, requester)):
            raise ValueError("报告校验缺少完整任务来源")
        task_request = task.get("request")
        if (task.get("runId") != payload.get("runId") or not isinstance(task_request, dict)
                or any(not isinstance(request.get(key), str) or not request[key]
                       or task_request.get(key) != request[key] for key in ("vin", "start", "end"))):
            raise ValueError("报告校验的任务请求与输入不一致")
        identity = []
        for candidates in ((context.get("chatId"), source.get("chatId")),
                           (context.get("messageId"), source.get("messageId")),
                           (context.get("requesterId"), requester.get("id"))):
            present = [value for value in candidates if value is not None and value != ""]
            if (any(not isinstance(value, str) or not value.strip() for value in present)
                    or len(set(present)) > 1):
                raise ValueError("报告校验的任务来源身份冲突或无效")
            identity.append(bool(present))
        # Contextless offline reports are an explicit supported workflow. Any
        # partly supplied or conflicting identity remains an error; delivery's
        # default completed gate and verified-session requirement are unchanged.
        if any(identity) and not all(identity):
            raise ValueError("报告校验缺少完整任务来源")

    expected_name = build_report_filename(
        str(request.get("vin", "")), str(request.get("start", "")), str(request.get("end", ""))
    )
    report_value = artifacts.get("report")
    if not isinstance(report_value, str) or not report_value.strip():
        raise ValueError("run.json 缺少 HTML 报告路径")
    report = Path(report_value).resolve(strict=True)
    runs_root = (workspace / RUNS_DIRECTORY).resolve(strict=True)
    if not _inside(run_path, runs_root):
        raise ValueError("run.json 不在固定输出目录内")
    expected_parent = (run_path.parent / "report").resolve(strict=True)
    if report.parent != expected_parent or not _inside(report, workspace):
        raise ValueError("HTML 报告路径超出本次运行目录")
    if report.name != expected_name or report.suffix.lower() != ".html" or not report.is_file() or report.is_symlink():
        raise ValueError("HTML 报告文件名或文件类型与本次请求不匹配")
    size = report.stat().st_size
    if size <= 0:
        raise ValueError("HTML 报告为空")
    with report.open("rb") as stream:
        head = stream.read(4096).lower()
    if b"<!doctype html" not in head and b"<html" not in head:
        raise ValueError("报告文件缺少 HTML 文档特征")

    completed_at = payload.get("completedAt")
    if completed_at is not None and not isinstance(completed_at, str):
        raise ValueError("run.json 完成时间字段格式无效")

    return {
        "ok": True,
        "runId": str(payload.get("runId", "")),
        "status": str(payload.get("status", "")),
        "completedAt": completed_at,
        "report": str(report),
        "filename": report.name,
        "sizeBytes": size,
        "request": {
            "vin": str(request.get("vin", "")),
            "start": str(request.get("start", "")),
            "end": str(request.get("end", "")),
        },
    }


def validate_attachment_path(run_json: Path, attachment: Path | str) -> dict:
    """Final send-time gate: the exact file being sent must be the validated HTML report."""
    result = validate(run_json)
    candidate = Path(str(attachment)).resolve(strict=True)
    expected = Path(result["report"]).resolve(strict=True)
    if candidate != expected:
        raise ValueError("待发送附件不是本次运行已校验的 HTML 报告")
    if candidate.suffix.lower() != ".html" or candidate.is_symlink() or candidate.stat().st_size <= 0:
        raise ValueError("待发送附件不是有效的 HTML 文件")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--allow-validating", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = validate(args.run_json, allow_validating=args.allow_validating)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
