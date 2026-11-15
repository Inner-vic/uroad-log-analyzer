#!/usr/bin/env python3
"""Resume one retained CAN acquisition without creating or re-querying a job.

The default command only validates local evidence. Execution is explicit and
claims the original failed task directly under the bounded scheduler lease.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
from uuid import uuid4

import run_uroad_pipeline as pipeline
import task_registry as registry
from ccconnect_request_context import metadata_from_context_header
from process_time_chunks import (EvidenceStore, _full_can_available,
                                 _parser_progress, _parquet_progress,
                                 cleanup_completed_chunk, retain_original_chunk)

SCRIPTS = Path(__file__).resolve().parent
RECOVERY_SCHEMA = "uroad-retained-can-recovery-v1"
MAX_RECOVERY_ATTEMPTS = 16
IDENTITY_KEYS = ("requesterId", "chatId", "messageId", "threadId")


class RecoveryAdmissionError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise RecoveryAdmissionError(message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ordinary(path: Path, root: Path, *, directory=False):
    """Reject links at every existing component, including Windows junctions."""
    _require(path.is_absolute() and path.is_relative_to(root), "恢复材料超出原任务目录。")
    for item in (path, *path.parents):
        _require(not item.is_symlink() and not bool(getattr(item, "is_junction", lambda: False)()),
                 "恢复材料包含链接，已停止。")
        if item == root:
            break
    _require(path.is_dir() if directory else path.is_file(), "恢复材料缺失，不能确认原任务进度。")
    _require(path.resolve(strict=True).is_relative_to(root.resolve(strict=True)), "恢复材料超出原任务目录。")
    return path


def _json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        raise RecoveryAdmissionError("恢复材料无法验证，已停止。") from None
    _require(isinstance(value, dict), "恢复材料格式无效。")
    return value


def _identity(task, binding):
    _require(isinstance(task, dict), "原任务来源记录缺失。")
    context, source = task.get("context"), task.get("source")
    requester, routing = task.get("requester"), task.get("routing")
    _require(all(isinstance(item, dict) for item in (context, source, requester, routing)), "原任务来源记录不完整。")
    for key in IDENTITY_KEYS:
        _require(key in context and (context[key] or None) == binding[key], "请求上下文与原任务身份不一致。")
    _require(source.get("chatId") == binding["chatId"] and source.get("messageId") == binding["messageId"]
             and requester.get("id") == binding["requesterId"]
             and (routing.get("threadId") or None) == binding["threadId"], "原任务来源别名与上下文冲突。")


def _no_report(run, task, run_dir):
    for name in ("report", "analysis"):
        directory = run_dir / name
        if directory.exists() or directory.is_symlink():
            _ordinary(directory, run_dir, directory=True)
    for record in (run, task):
        _require(not any((record.get(key) or {}).get("report") for key in ("artifact", "artifacts", "diagnosticArtifacts")),
                 "原任务已有报告登记，不能重新分析。")
        delivery = record.get("delivery") or {}
        _require(isinstance(delivery, dict) and delivery.get("status") in (None, "pending")
                 and not delivery.get("deliveredAt") and not delivery.get("messageId"), "原任务已有交付记录，不能重新分析。")
    _require((run.get("reportValidation") or {}).get("status") != "passed"
             and (run.get("steps") or {}).get("report") != "completed", "原任务报告已通过校验，不能重新分析。")
    report_dir = run_dir / "report"
    _require(not report_dir.exists() or not any(report_dir.iterdir()),
             "原任务已有报告文件，不能覆盖。")
    for path in (run_dir / "manifest.json", run_dir / "analysis" / "analysis.json", run_dir / "analysis" / "can-signals.json"):
        _require(not path.exists() and not path.is_symlink(), "原任务已有最终分析材料，不能覆盖。")


def _diagnostic_sequence(run, run_dir):
    sequence = run.get("subprocessCalls")
    _require(type(sequence) is int and sequence >= 0, "原任务子步骤序号无效，不能安全续写诊断。")
    diagnostics = run_dir / "diagnostics"
    if diagnostics.exists():
        _ordinary(diagnostics, run_dir, directory=True)
        for diagnostic in diagnostics.glob("subprocess-*.json"):
            match = re.fullmatch(r"subprocess-(\d+)-[a-z_]+\.json", diagnostic.name)
            _require(match is not None and int(match[1]) <= sequence and not diagnostic.is_symlink(),
                     "原任务诊断序号冲突，不能覆盖已有记录。")


def _retention_paths(chunk, run_dir):
    """Check both resolved endpoints before the standard directory moves."""
    for parent in (run_dir / "original", run_dir / "original" / "0001"):
        if parent.exists() or parent.is_symlink():
            _ordinary(parent, run_dir, directory=True)
    for name in ("raw", "input"):
        source = chunk / name
        if source.exists() or source.is_symlink():
            _ordinary(source, run_dir, directory=True)
            destination = run_dir / "original" / "0001" / name
            _require(destination.resolve().is_relative_to(run_dir.resolve())
                     and not destination.exists() and not destination.is_symlink(), "原始材料保留目标不可安全使用。")


@dataclass
class RetainedPlan:
    run_path: Path
    workspace: Path
    run: dict
    task: dict
    binding: dict
    run_bytes: bytes
    evidence: dict
    analysis: dict
    query: dict
    environment: str
    provider_environment: str
    page_size: int
    max_files: int | None
    recover_interrupted: bool = False
    expected_attempt_id: str | None = None
    expected_lease: dict | None = None
    completed_can: dict | None = None

    def public(self):
        return {"ok": True, "status": "ready", "execute": False,
                "runId": self.run["runId"], "reuseUroad": True,
                "resumeStage": "merge" if self.recover_interrupted else "parquet_can", "newTask": False,
                "recoverInterrupted": self.recover_interrupted, "reuseCompletedCAN": self.recover_interrupted}


def preflight(run_json: str | Path, context_header: str, *, recover_interrupted=False) -> RetainedPlan:
    """Pure local admission: no directory creation, bootstrap, claims or query."""
    metadata = metadata_from_context_header(context_header)
    binding = {key: metadata["raw"][key] for key in IDENTITY_KEYS}
    workspace = pipeline.validate_workspace()
    runs = workspace / pipeline.RUNS_DIRECTORY
    path = Path(run_json).absolute()
    _require(path.name == "run.json" and path.parent.parent == runs, "请指定固定输出目录中的精确 run.json。")
    _ordinary(path, workspace)
    run_bytes = path.read_bytes()
    try:
        run = json.loads(run_bytes)
    except ValueError:
        raise RecoveryAdmissionError("原任务状态无法验证。") from None
    _require(isinstance(run, dict) and run.get("runId") == path.parent.name, "原任务标识与目录不一致。")
    from retention_cleanup import retention_seconds, _age_basis
    _require(time.time()-_age_basis(path.parent, path, run) < retention_seconds(), "原任务已超过数据保留期限，不能恢复。")
    reg_path = _ordinary(runs / registry.REGISTRY_FILENAME, workspace)
    tasks = _json(reg_path).get("tasks")
    _require(isinstance(tasks, list), "任务注册表格式无效。")
    matches = [item for item in tasks if isinstance(item, dict) and item.get("runId") == run["runId"]]
    _require(len(matches) == 1, "原任务没有唯一注册记录。")
    task = matches[0]
    _identity(task, binding)
    _identity(run.get("task"), binding)
    expected_status = "running" if recover_interrupted else "system_error"
    for record in (task, run):
        _require(record.get("status") == expected_status, "原任务状态与明确指定的恢复模式不一致。")
    _require((task.get("execution") or {}).get("status") == expected_status
             and (run.get("error") is None if recover_interrupted else (run.get("error") or {}).get("retryable") is True),
             "原任务尚未满足安全恢复条件。")
    request, inputs = task.get("request"), run.get("input")
    _require(isinstance(request, dict) and isinstance(inputs, dict)
             and run.get("request") == request and run["task"].get("request") == request,
             "原任务请求记录不一致。")
    for key in ("vin", "start", "end"):
        _require(isinstance(inputs.get(key), str) and inputs[key] == request.get(key), "原任务请求与输入不一致。")
    _require(request.get("cloudEnv") == inputs.get("cloud_env")
             and request.get("cloudEnv") in {"auto", "prod", "test"}, "原任务环境请求不一致。")
    args = argparse.Namespace(**{key: inputs.get(key) for key in ("vin", "start", "end", "page_size", "max_files")})
    _require(type(args.page_size) is int and (args.max_files is None or type(args.max_files) is int), "原任务分页参数无效。")
    pipeline.validate_inputs(args)
    _require(inputs.get("eea_platform") == "EEA3.0", "原任务平台不受支持。")
    chunks = run.get("chunks") or {}
    active = chunks.get("active") or {}
    lanes = active.get("lanes") or {}
    _require(chunks.get("total") == 1 and chunks.get("completed") == 0 and not chunks.get("items")
             and active.get("index") == 1 and active.get("start") == inputs["start"] and active.get("end") == inputs["end"]
             and active.get("status") == ("running" if recover_interrupted else "failed") and active.get("branchesDrained") is True
             and ((active.get("source") == "can" and active.get("stage") == "parquet_can")
                  or (recover_interrupted and active.get("source") == "uroad+can" and active.get("stage") == "merge"))
             and (lanes.get("uroad") or {}).get("status") == "completed"
             and (lanes.get("can") or {}).get("status") == ("completed" if recover_interrupted else "failed")
             and (lanes.get("can") or {}).get("stage") == "parquet_can"
             and (recover_interrupted or (run.get("error") or {}).get("stage") == "parquet_can"), "原任务不是已排空的受控 CAN Parquet 恢复状态。")
    _no_report(run, task, path.parent)
    _diagnostic_sequence(run, path.parent)
    recovery = run.get("recovery")
    expected_attempt_id, expected_lease = None, None
    if recovery is not None:
        attempts = recovery.get("attempts") if isinstance(recovery, dict) else None
        _require(isinstance(recovery, dict) and recovery.get("schema") == RECOVERY_SCHEMA
                 and isinstance(attempts, list) and len(attempts) < MAX_RECOVERY_ATTEMPTS
                 and all(isinstance(item, dict) for item in attempts)
                 and all(item.get("status") in {"failed", "interrupted"} for item in (attempts[:-1] if recover_interrupted else attempts)),
                 "已有恢复尝试尚未安全结束，需核对后处理。")
        if recover_interrupted:
            _require(bool(attempts) and attempts[-1].get("status") == "running"
                     and isinstance(attempts[-1].get("attemptId"), str)
                     and re.fullmatch(r"[0-9a-f]{32}", attempts[-1]["attemptId"]) is not None,
                     "原任务没有唯一待接管的恢复尝试。")
            expected_attempt_id = attempts[-1]["attemptId"]
    if recover_interrupted:
        _require(expected_attempt_id is not None, "原任务缺少明确的中断恢复尝试。")
        import scheduler
        lease_paths = list(runs.glob(".heavy-slot-*.lock"))
        _require(len(lease_paths) <= 1024, "原任务槽位记录数量无效。")
        matching_leases = []
        for lease_path in lease_paths:
            value, lease = scheduler._lease_value(lease_path)
            if value == run["runId"]:
                _require(isinstance(lease, dict), "原任务遗留槽位缺少受控所有权证据。")
                matching_leases.append({"slotName": lease_path.name, **lease})
        _require(len(matching_leases) == 1, "原任务没有唯一遗留恢复槽位。")
        expected_lease = matching_leases[0]
        registry.validate_interrupted_recovery(run["runId"], expected_run_sha256=hashlib.sha256(run_bytes).hexdigest(),
            context_binding=binding, expected_attempt_id=expected_attempt_id, expected_lease=expected_lease)
    selected = (run.get("environmentSelection") or {}).get("selected") or {}
    env, provider = selected.get("environment"), selected.get("providerEnvironment")
    _require(env in {"prod", "test"} and isinstance(provider, str) and provider
             and active.get("environment") == env
             and inputs.get("resolved_cloud_env") == env and inputs.get("resolved_provider_environment") == provider
             and (inputs["cloud_env"] == "auto" or inputs["cloud_env"] == env), "原任务实际来源环境不一致。")
    chunk = path.parent / "slices" / "0001"
    manifest_path = _ordinary(chunk / "manifest.json", path.parent)
    manifest = _json(manifest_path)
    query = {"cloudEnv": env, "providerEnvironment": provider, "vin": inputs["vin"],
             "startTime": inputs["start"], "endTime": inputs["end"], "logClass": "log_fsdA_service"}
    source_query = manifest.get("query")
    _require(manifest.get("runId") == run["runId"] and isinstance(source_query, dict)
             and all(source_query.get(key) == value for key, value in query.items())
             and source_query.get("eeaPlatform") == inputs["eea_platform"] and source_query.get("platformType") == "user-car"
             and manifest.get("environment") == {"environment": env, "providerEnvironment": provider}
             and (manifest.get("extracted") or {}).get("uroadMatched") is True, "Uroad 来源清单与原任务不一致。")
    analysis_path = _ordinary(chunk / "analysis" / "analysis.json", path.parent)
    analysis = _json(analysis_path)
    source = analysis.get("source") or {}
    manifest_sha = _sha(manifest_path)
    _require(analysis.get("runId") == run["runId"] and source.get("manifestSha256") == manifest_sha
             and source.get("query") == source_query and isinstance(source.get("manifestPath"), str)
             and Path(source["manifestPath"]).resolve() == manifest_path.resolve(), "已完成 Uroad 解析与来源清单无法绑定。")
    progress_path = _ordinary(chunk / "analysis" / "parse-progress.json", path.parent)
    progress = _parser_progress(chunk, path.parent)
    _require(progress.get("available") is True and progress["value"].get("stage") == "completed", "Uroad 解析尚无可信完成进度。")
    work = _ordinary(chunk / "can-parquet", path.parent, directory=True)
    snapshot_path = _ordinary(work / "sources.json", path.parent)
    import parquet_can_signals as parquet
    can_binding = {"runId": run["runId"], "vin": inputs["vin"], "start": inputs["start"], "end": inputs["end"],
                   "environment": env, "providerEnvironment": provider}
    options = {"pageSize": args.page_size, "maxFiles": args.max_files,
               "definitions_sha256": parquet._sha(parquet.DEFINITIONS), "logClass": parquet.CAN_LOG_CLASS}
    # Existing=True makes missing sources a hard error; this path cannot query.
    parquet._snapshot(args, work, can_binding, options, None, True)
    can_progress_path = _ordinary(work / "progress.json", path.parent)
    can_progress = parquet.validate_progress(_json(can_progress_path))
    evidence = {"manifest": (manifest_path, manifest_sha), "analysis": (analysis_path, _sha(analysis_path)),
                "parseProgress": (progress_path, _sha(progress_path)), "canSnapshot": (snapshot_path, _sha(snapshot_path)),
                "canProgress": (can_progress_path, _sha(can_progress_path))}
    completed_can = None
    if recover_interrupted:
        _require(can_progress["stage"] == "completed", "CAN 缓存尚无可信完成进度。")
        from parquet_can_stream import load_indexed_contract
        can_path = _ordinary(chunk / "analysis" / "can-signals.json", path.parent)
        index_path = _ordinary(work / "points.sqlite3", path.parent)
        metadata_path = _ordinary(work / "contract-metadata.json", path.parent)
        completed_can = load_indexed_contract(can_path, index_path, metadata_path, can_binding, parquet.DEFINITIONS)
        _require(_full_can_available(completed_can) and completed_can.get("environment") == {
            "environment": env, "providerEnvironment": provider}, "已完成 CAN 缓存与原任务不一致。")
        evidence.update({"canOutput": (can_path, _sha(can_path)), "canIndex": (index_path, _sha(index_path)),
                         "canMetadata": (metadata_path, _sha(metadata_path))})
    _require(path.read_bytes() == run_bytes, "前检期间任务状态已变化，请重新核对。")
    return RetainedPlan(path, workspace, run, task, binding, run_bytes, evidence, analysis, query,
                        env, provider, args.page_size, args.max_files, recover_interrupted,
                        expected_attempt_id, expected_lease, completed_can)


def _exclusive_bytes(path, value):
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_json(path, value):
    """Publish a new artifact atomically, refusing even an intervening file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".recovery-" + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _archive(plan, attempt_id):
    run_dir = plan.run_path.parent
    for directory in (run_dir / "diagnostics", run_dir / "diagnostics" / "recovery"):
        if directory.exists():
            _ordinary(directory, run_dir, directory=True)
        else:
            directory.mkdir()
    archive = run_dir / "diagnostics" / "recovery" / attempt_id
    archive.mkdir()
    _exclusive_bytes(archive / "run.json", plan.run_bytes)
    for key, name in (("parseProgress", "parse-progress.json"), ("canProgress", "can-progress.json")):
        path, expected = plan.evidence[key]
        value = path.read_bytes()
        _require(hashlib.sha256(value).hexdigest() == expected, "归档期间任务进度已变化，已停止。")
        _exclusive_bytes(archive / name, value)
    audit = {"schema": RECOVERY_SCHEMA, "attemptId": attempt_id, "runId": plan.run["runId"],
             "expectedRunSha256": hashlib.sha256(plan.run_bytes).hexdigest(),
             "source": {key + "Sha256": digest for key, (_, digest) in plan.evidence.items()}}
    _exclusive_bytes(archive / "archive.json", json.dumps(audit, ensure_ascii=False, indent=2).encode("utf-8"))
    return archive, audit


def execute(plan: RetainedPlan):
    """Explicitly resume only the original task's frozen CAN source snapshot."""
    pipeline.bootstrap_dependencies(plan.workspace)
    attempt_id = uuid4().hex
    archive, audit = _archive(plan, attempt_id)
    from scheduler import acquire_retained_recovery_slot, acquire_interrupted_recovery_slot
    acquire = acquire_interrupted_recovery_slot if plan.recover_interrupted else acquire_retained_recovery_slot
    options = {"expected_run_sha256": audit["expectedRunSha256"], "context_binding": plan.binding}
    if plan.recover_interrupted:
        options.update(expected_attempt_id=plan.expected_attempt_id, expected_lease=plan.expected_lease)
    with acquire(plan.run["runId"], **options) as active_task:
        run, run_dir = plan.run, plan.run_path.parent
        attempt = {**audit, "archive": archive.relative_to(run_dir).as_posix(), "startedAt": _now(),
                   "status": "running", "reuseUroad": True, "reuseCompletedCAN": plan.recover_interrupted}
        execution_completed, delivery = False, None
        stage = "merge" if plan.recover_interrupted else "parquet_can"
        report_path = run_dir / "report" / pipeline.build_report_filename(run["input"]["vin"], run["input"]["start"], run["input"]["end"])
        try:
            # The scheduler atomically checked run SHA under its registry lock.
            # Re-check immutable evidence before the first task mutation.
            for path, digest in plan.evidence.values():
                _ordinary(path, run_dir)
                _require(_sha(path) == digest, "领取期间任务来源材料已变化，已停止。")
            _no_report(run, active_task, run_dir)
            _diagnostic_sequence(run, run_dir)
            run.update(status="running", task=active_task, error=None)
            run.pop("completedAt", None)
            if plan.recover_interrupted:
                previous = run["recovery"]["attempts"][-1]
                _require(previous["attemptId"] == plan.expected_attempt_id and previous["status"] == "running",
                         "原任务恢复尝试已变化，不能接管。")
                previous.update(status="interrupted", endedAt=_now(), interruptedByAttempt=attempt_id)
            run.setdefault("recovery", {"schema": RECOVERY_SCHEMA, "attempts": []})["attempts"].append(attempt)
            pipeline.save(plan.run_path, run)
            call = pipeline.stage_caller(run_dir, run, pipeline.save)
            chunk, inputs = run_dir / "slices" / "0001", run["input"]
            state = run["chunks"]["active"]
            lane = state["lanes"]["can"]
            can_path = chunk / "analysis" / "can-signals.json"
            if not plan.recover_interrupted:
                state.update(status="running", source="can", stage="parquet_can")
                lane.update(status="running", stage="parquet_can")
                phase = {"stage": "parquet_can", "status": "running", "startedAt": _now(), "recoveryAttempt": attempt_id}
                phase_started = time.monotonic()
                lane.setdefault("stages", []).append(phase)
                pipeline.save(plan.run_path, run)
                command = [sys.executable, str(SCRIPTS / "parquet_can_signals.py"), "--vin", inputs["vin"],
                           "--start", inputs["start"], "--end", inputs["end"], "--output", str(can_path),
                           "--cloud-env", plan.environment, "--work-dir", str(chunk / "can-parquet"),
                           "--run-id", run["runId"], "--page-size", str(plan.page_size)]
                if plan.max_files is not None:
                    command.extend(["--max-files", str(plan.max_files)])
                try:
                    call(command)
                finally:
                    phase.update(endedAt=_now(), elapsedSeconds=round(time.monotonic()-phase_started, 6))
                    lane["parquetProgress"] = _parquet_progress(chunk, run_dir)
                    pipeline.save(plan.run_path, run)
            from parquet_can_stream import load_indexed_contract
            can = load_indexed_contract(can_path, chunk / "can-parquet" / "points.sqlite3",
                                        chunk / "can-parquet" / "contract-metadata.json",
                                        {"runId": run["runId"], "vin": inputs["vin"], "start": inputs["start"], "end": inputs["end"],
                                         "environment": plan.environment, "providerEnvironment": plan.provider_environment},
                                        SCRIPTS.parent / "references" / "can-signals.json")
            _require(_full_can_available(can) and can.get("environment") == {
                "environment": plan.environment, "providerEnvironment": plan.provider_environment}, "恢复的 CAN 信号或来源环境不完整。")
            if not plan.recover_interrupted:
                phase.update(status="completed", endedAt=_now())
                lane.update(status="completed", indexedEvidence=True, recoveredAt=_now(), recoveryElapsedSeconds=phase["elapsedSeconds"])
            else:
                lane.update(status="completed", indexedEvidence=True, reusedCompletedEvidence=True)
            pipeline.save(plan.run_path, run)
            stage = "merge"
            run["stage"] = stage
            state.update(stage=stage, source="uroad+can", status="running")
            pipeline.save(plan.run_path, run)
            registry.heartbeat(run["runId"], stage)
            merge_root = run_dir / "recovery" / attempt_id / "merge-state"
            for directory in (merge_root.parent.parent, merge_root.parent):
                if directory.exists():
                    _ordinary(directory, run_dir, directory=True)
                else:
                    directory.mkdir()
            merge_root.mkdir()
            store = EvidenceStore(merge_root / "evidence.sqlite3")
            try:
                store.add_slice(1, plan.analysis, can)
                merged_can = store.finalize_can()
                merged = store.finalize_uroad(merged_can, plan.query, run["runId"])
            finally:
                store.close()
            final_manifest = {"schemaVersion": "1.1", "runId": run["runId"], "query": plan.query,
                              "environment": {"environment": plan.environment, "providerEnvironment": plan.provider_environment}, "chunks": 1}
            for target, value in ((run_dir / "analysis" / "can-signals.json", merged_can),
                                  (run_dir / "analysis" / "analysis.json", merged), (run_dir / "manifest.json", final_manifest)):
                _publish_json(target, value)
            _retention_paths(chunk, run_dir)
            state["retainedOriginal"] = retain_original_chunk(chunk, run_dir, 1)
            state["retainedCANEvidence"] = [(chunk / "can-parquet").relative_to(run_dir).as_posix(), can_path.relative_to(run_dir).as_posix()]
            state.update(status="completed", branchesDrained=True, drainedAt=_now())
            run["chunks"].update(completed=1, active=None, items=[deepcopy(state)])
            cleanup_completed_chunk(chunk, indexed_can=True)
            run["steps"].update(download="completed", canlog="completed", canSignals="completed", analysis="completed")
            run["diagnosticArtifacts"] = {"manifest": str(run_dir / "manifest.json"), "canSignals": str(run_dir / "analysis" / "can-signals.json"),
                                          "analysis": str(run_dir / "analysis" / "analysis.json")}
            pipeline.save(plan.run_path, run)
            stage = "report_build"
            run["stage"] = stage
            pipeline.save(plan.run_path, run)
            registry.heartbeat(run["runId"], stage)
            call([sys.executable, str(pipeline.HTML_REPORT), "--analysis", str(run_dir / "analysis" / "analysis.json"), "--output", str(report_path)])
            _require(report_path.is_file() and report_path.stat().st_size > 0, "恢复任务尚未生成有效报告。")
            pipeline.validate_html_report(report_path)
            run["steps"]["report"] = "completed"
            run["artifacts"] = {**run.pop("diagnosticArtifacts"), "report": str(report_path)}
            run["cleanup"] = {"stage": "retained_for_followup", "removed": [], "completed": True, "errors": []}
            run.update(status="validating_report", reportValidation={"status": "pending"})
            pipeline.save(plan.run_path, run)
            stage = "report_validate"
            run["stage"] = stage
            pipeline.save(plan.run_path, run)
            registry.heartbeat(run["runId"], stage)
            validated = call([sys.executable, str(SCRIPTS / "validate_report_artifact.py"), "--run-json", str(plan.run_path), "--allow-validating"])
            _require(isinstance(validated, dict) and validated.get("ok") is True, "恢复报告完成前校验未通过。")
            run.update(reportValidation={"status": "passed", "validatedAt": _now()}, status="completed", completedAt=_now())
            attempt.update(status="completed", endedAt=_now())
            pipeline.save(plan.run_path, run)
            registry.mark_finished(run["runId"], "completed", report=str(report_path))
            execution_completed = True
            followup_registered = False
            try:
                from followup_context import register_data_context
                context = register_data_context(run["runId"], run_dir)
                run["followup"] = {"jobId": run["runId"], "status": "registered", "dataVersion": context["version"], "retentionUntil": context["retentionUntil"]}
                followup_registered = True
            except Exception as exc:
                run["followup"] = {"jobId": run["runId"], "status": "registration_failed", "errorType": type(exc).__name__,
                                   "error": "追问数据上下文登记未完成；标准报告已生成。"}
            pipeline.save(plan.run_path, run)
            from delivery_bridge import send_native_report
            delivery = send_native_report(plan.run_path)
            followups = {"started": False, "reason": "report_delivery_failed" if delivery.get("status") not in {"sent", "already_sent"} else "followup_registration_failed"}
            if followup_registered and delivery.get("status") in {"sent", "already_sent"}:
                from followup_agent import start_pending_followups
                followups = start_pending_followups(run["runId"])
            return {"ok": True, "status": "completed", "runId": run["runId"], "reuseUroad": True,
                    "delivery": delivery, "followupDispatch": followups}
        except (Exception, KeyboardInterrupt) as exc:
            if execution_completed:
                if not isinstance(delivery, dict) or delivery.get("status") not in {"sent", "already_sent"}:
                    registry.mark_delivery(run["runId"], "delivery_failed", error="报告已生成，但附件交付尚未确认。")
                    delivery = {"status": "delivery_failed", "error": "报告已生成，但附件交付尚未确认。"}
                run["postCompletionError"] = {"type": type(exc).__name__, "message": "报告已生成，后续交付或追问处理未完成。"}
                pipeline.save(plan.run_path, run)
                return {"ok": True, "status": "completed", "runId": run["runId"], "reuseUroad": True, "delivery": delivery}
            # Never expose commands, download URLs or raw child exceptions.
            message = "同任务缓存恢复未完成，已保留现有输入、进度与诊断。"
            error = {"message": message, "type": type(exc).__name__, "stage": getattr(exc, "stage", stage),
                     "source": "can" if stage == "parquet_can" else "recovery", "retryable": bool(getattr(exc, "retryable", False))}
            attempt.update(status="failed", endedAt=_now(), errorType=type(exc).__name__, stage=error["stage"])
            run.update(status="system_error", error=error)
            active = (run.get("chunks") or {}).get("active")
            if active:
                active.update(status="failed", branchesDrained=True)
                if stage == "parquet_can":
                    active["lanes"]["can"]["status"] = "failed"
                    if active["lanes"]["can"].get("stages"):
                        active["lanes"]["can"]["stages"][-1].update(status="failed", endedAt=_now(), errorType=type(exc).__name__)
            if (run.get("reportValidation") or {}).get("status") != "passed":
                pipeline.cleanup_report_output(run_dir, report_path.name)
            retained = [name for name in ("slices", "merge-state", "analysis", "original", "diagnostics", "recovery", "report") if (run_dir / name).exists()]
            run["failureRetention"] = {"policy": "task_retention", "paths": retained, "reason": "recovery_failed"}
            run["cleanup"] = {"stage": "retained_for_diagnostics", "removed": [], "retained": retained, "completed": True, "errors": []}
            pipeline.save(plan.run_path, run)
            try:
                _exclusive_bytes(archive / "execution-error.json", json.dumps({"errorType": type(exc).__name__, "stage": error["stage"],
                                 "traceback": traceback.format_exc()}, ensure_ascii=False).encode("utf-8"))
            finally:
                registry.mark_finished(run["runId"], "system_error", error=message)
            return {"ok": False, "status": "system_error", "runId": run["runId"], "error": error}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--ccconnect-context", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover-interrupted", action="store_true", help="explicitly take over a verified interrupted retained attempt")
    args = parser.parse_args(argv)
    try:
        plan = preflight(args.run_json, args.ccconnect_context, recover_interrupted=args.recover_interrupted)
        result = execute(plan) if args.execute else plan.public()
    except (Exception, KeyboardInterrupt) as exc:
        result = {"ok": False, "status": "recovery_not_started", "error": "原任务恢复前检查或领取未通过，未重新获取或解析日志。", "errorType": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False), file=sys.stdout if result["ok"] else sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
