"""Synthetic retained recovery: source admission, cache reuse and delivery gates."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import resume_uroad_task as resume
import run_uroad_pipeline as pipeline
import task_registry as registry
import scheduler
import parquet_can_signals as wrapper
import parquet_can_stream as parquet
import build_html_report
import validate_report_artifact as validator
import followup_context
import followup_agent
import delivery_bridge

VIN = "DEM0VEH1CLEX00001"
START, END = "2026-11-03 10:00:00", "2026-11-03 10:35:00"
RUN_ID = "run-20261103-110153-634967-aabbccdd"
HEADER = '[cc-connect request_context_v=1 sender_id=ou_synthetic sender_name="测试" platform=feishu chat_id=oc_synthetic message_id=om_synthetic thread_id=]'
BINDING = {"requesterId": "ou_synthetic", "chatId": "oc_synthetic", "messageId": "om_synthetic", "threadId": None}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def file_state(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class RetainedCanResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": str(self.root), "UROAD_MAX_HEAVY_JOBS": "2", "UROAD_MIN_FREE_GB": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.run_dir = self.root / "outputs" / "uroad-report-runs" / RUN_ID
        self.run_path = self.run_dir / "run.json"
        self.reg_path = self.run_dir.parent / registry.REGISTRY_FILENAME
        self.chunk = self.run_dir / "slices" / "0001"
        self.work = self.chunk / "can-parquet"
        self.work.mkdir(parents=True)
        self.query = {"vin": VIN, "startTime": START, "endTime": END, "cloudEnv": "test", "providerEnvironment": "testtwo",
                      "logClass": "log_fsdA_service", "platformType": "user-car", "eeaPlatform": "EEA3.0"}
        self.manifest = {"runId": RUN_ID, "query": self.query, "environment": {"environment": "test", "providerEnvironment": "testtwo"},
                         "extracted": {"uroadMatched": True}, "files": []}
        write_json(self.chunk / "manifest.json", self.manifest)
        self.analysis = {"runId": RUN_ID, "source": {"manifestPath": str(self.chunk / "manifest.json"),
                         "manifestSha256": resume._sha(self.chunk / "manifest.json"), "query": self.query, "files": []},
                         "charts": {}, "metrics": {}, "summary": {"runningStatus": "healthy", "startupStatus": "success"},
                         "frames": [], "events": [], "timeline": [], "edsgCycles": [], "issues": []}
        write_json(self.chunk / "analysis" / "analysis.json", self.analysis)
        progress = {"schema": "uroad-parse-progress-v1", "stage": "completed", "files_read": 1, "lines_read": 20,
                    "cycles_processed": 0, "inference_reports": 0, "matches": 0, "elapsed_seconds": 0.1}
        write_json(self.chunk / "analysis" / "parse-progress.json", progress)
        write_json(self.work / "progress.json", {"schema": wrapper.PROGRESS_SCHEMA, "stage": "failed",
                   **dict.fromkeys(wrapper.COUNTERS, 0), "files_total": 111, "files_downloaded": 89,
                   "elapsed_seconds": 0.2, "errorType": "IncompleteRead"})
        request = {"vin": VIN, "start": START, "end": END, "cloudEnv": "auto", "attachedQuestions": [],
                   "key": f"{VIN}|{START}|{END}|auto"}
        self.task = {"runId": RUN_ID, "taskId": RUN_ID, "status": "system_error", "execution": {"status": "system_error"},
                     "context": dict(BINDING), "source": {"chatId": BINDING["chatId"], "messageId": BINDING["messageId"]},
                     "requester": {"id": BINDING["requesterId"]}, "routing": {"threadId": None}, "request": request,
                     "artifact": {}, "artifacts": {}, "delivery": {"status": "pending"}}
        self.run = {"schemaVersion": "1.3", "runId": RUN_ID, "status": "system_error", "task": deepcopy(self.task),
                    "request": request, "input": {"vin": VIN, "start": START, "end": END, "cloud_env": "auto", "eea_platform": "EEA3.0",
                    "page_size": 37, "max_files": None, "resolved_cloud_env": "test", "resolved_provider_environment": "testtwo"},
                    "steps": {"download": "pending", "canlog": "pending", "canSignals": "pending", "analysis": "pending", "report": "pending"},
                    "artifacts": {}, "error": {"retryable": True, "stage": "parquet_can", "source": "can", "message": "synthetic interrupted"},
                    "environmentSelection": {"mode": "auto", "selected": {"environment": "test", "providerEnvironment": "testtwo"}},
                    "subprocessCalls": 5, "chunks": {"total": 1, "completed": 0, "items": [], "active": {
                    "index": 1, "start": START, "end": END, "status": "failed", "source": "can", "stage": "parquet_can", "branchesDrained": True,
                    "environment": "test", "lanes": {"uroad": {"status": "completed", "stage": "uroad_parse", "stages": []},
                    "can": {"status": "failed", "stage": "parquet_can", "stages": []}}}}}
        self.save_state()
        (self.run_dir / "diagnostics").mkdir()
        (self.run_dir / "diagnostics" / "subprocess-0005-parquet_can.json").write_bytes(b"old-private-diagnostic")
        self.items = [{"name": "same.parquet", "url": f"https://synthetic.invalid/{number}"} for number in range(1, 112)]
        self.snapshot = {"schema": wrapper.SNAPSHOT_SCHEMA, "binding": {"runId": RUN_ID, "vin": VIN, "start": START, "end": END,
                         "environment": "test", "providerEnvironment": "testtwo"},
                         "options": {"pageSize": 37, "maxFiles": None, "definitions_sha256": wrapper._sha(wrapper.DEFINITIONS), "logClass": wrapper.CAN_LOG_CLASS},
                         "items": self.items, "queryTotal": 111}
        self.snapshot["snapshot_sha256"] = wrapper._digest(self.snapshot)
        write_json(self.work / "sources.json", self.snapshot)
        self.cached_bytes = {}
        for number, item in enumerate(self.items[:89], 1):
            directory = self.work / "downloads" / f"item-{number:04d}"
            source = directory / f"source-{number:04d}-same.parquet"
            directory.mkdir(parents=True)
            value = f"synthetic frozen parquet {number}".encode()
            source.write_bytes(value)
            write_json(directory / "receipt.json", {"schema": wrapper.RECEIPT_SCHEMA, "snapshot_sha256": self.snapshot["snapshot_sha256"],
                       "ordinal": number, "record_sha256": wrapper._digest(item), "path": source.relative_to(self.work).as_posix(),
                       "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()})
            self.cached_bytes[source] = value
        for name in ("raw", "input"):
            target = self.chunk / name
            target.mkdir()
            (target / "synthetic.log").write_bytes(b"synthetic retained input")
        self.commands, self.downloads = [], []

    def save_state(self):
        write_json(self.run_path, self.run)
        write_json(self.reg_path, {"schemaVersion": "1.1", "tasks": [self.task]})

    def read_run(self):
        return json.loads(self.run_path.read_text(encoding="utf-8"))

    def test_default_cli_is_read_only_and_does_not_bootstrap_or_query(self):
        before = file_state(self.root)
        with patch.object(pipeline, "bootstrap_dependencies", side_effect=AssertionError("no bootstrap")), \
             patch.object(wrapper, "query_files", side_effect=AssertionError("no query")), \
             patch.object(resume, "execute", side_effect=AssertionError("no execute")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(resume.main(["--run-json", str(self.run_path), "--ccconnect-context", HEADER]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        self.assertEqual(file_state(self.root), before)

    def test_transport_and_source_identity_conflicts_are_rejected_without_writes(self):
        for field in BINDING:
            with self.subTest(field=field):
                original = deepcopy(self.run)
                self.run["task"]["context"][field] = "foreign"
                self.save_state()
                before = file_state(self.root)
                with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
                self.assertEqual(file_state(self.root), before)
                self.run = original
        self.save_state()
        self.task["source"]["chatId"] = "foreign"
        self.save_state()
        with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)

    def test_request_branch_state_and_diagnostic_conflicts_are_refused(self):
        mutations = [lambda r: r["input"].update(vin="DEM0VEH1CLEX00002"),
                     lambda r: r["chunks"]["active"].update(branchesDrained=False),
                     lambda r: r["chunks"]["active"]["lanes"]["uroad"].update(status="failed"),
                     lambda r: r["error"].update(retryable=False),
                     lambda r: r["delivery"].update(status="sent") if "delivery" in r else r.update(delivery={"status": "sent"}),
                     lambda r: r.update(subprocessCalls=4)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                original = deepcopy(self.run)
                mutate(self.run); self.save_state()
                with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
                self.run = original
        self.save_state()

    def test_source_sha_query_and_parser_completion_are_mandatory(self):
        analysis_path = self.chunk / "analysis" / "analysis.json"
        for source in ({**self.analysis["source"], "manifestSha256": "0"*64},
                       {**self.analysis["source"], "query": {**self.query, "cloudEnv": "prod"}},
                       {**self.analysis["source"], "manifestPath": str(self.run_dir / "other.json")}):
            write_json(analysis_path, {**self.analysis, "source": source})
            with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
        write_json(analysis_path, self.analysis)
        progress_path = self.chunk / "analysis" / "parse-progress.json"
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        write_json(progress_path, {**progress, "stage": "failed"})
        with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)

    def test_frozen_snapshot_is_required_and_cannot_be_requeried_or_changed(self):
        snapshot_path = self.work / "sources.json"
        snapshot_path.unlink()
        with patch.object(wrapper, "query_files", side_effect=AssertionError("must never query")):
            with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
        altered = deepcopy(self.snapshot)
        altered["options"]["pageSize"] = 100
        altered.pop("snapshot_sha256")
        altered["snapshot_sha256"] = wrapper._digest(altered)
        write_json(snapshot_path, altered)
        with patch.object(wrapper, "query_files", side_effect=AssertionError("must never query")):
            with self.assertRaises(wrapper.CacheBindingError): resume.preflight(self.run_path, HEADER)

    def test_existing_final_artifact_is_not_overwritten(self):
        target = self.run_dir / "analysis" / "analysis.json"
        target.parent.mkdir()
        target.write_bytes(b"existing successful evidence")
        with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
        self.assertEqual(target.read_bytes(), b"existing successful evidence")

    def bounded(self, command, *, env, timeout, stage, diagnostics_path):
        self.commands.append(command)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(timeout, 900)
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        with diagnostics_path.open("xb") as stream: stream.write(b"synthetic new diagnostic")
        if Path(command[1]).name == "parquet_can_signals.py":
            args = dict(zip(command[2::2], command[3::2]))
            value = wrapper.acquire(SimpleNamespace(vin=args["--vin"], start=args["--start"], end=args["--end"],
                    output=args["--output"], cloud_env=args["--cloud-env"], work_dir=args["--work-dir"],
                    run_id=args["--run-id"], page_size=int(args["--page-size"]), max_files=int(args["--max-files"]) if "--max-files" in args else None))
        elif Path(command[1]).name == "build_html_report.py":
            args = dict(zip(command[2::2], command[3::2]))
            report = Path(args["--output"])
            report.parent.mkdir()
            report.write_text(build_html_report.render(json.loads(Path(args["--analysis"]).read_text(encoding="utf-8"))), encoding="utf-8")
            value = {"ok": True}
        elif Path(command[1]).name == "validate_report_artifact.py":
            self.assertIn("--allow-validating", command)
            self.assertEqual(self.read_run()["status"], "validating_report")
            value = validator.validate(Path(command[command.index("--run-json")+1]), allow_validating=True)
        else:
            raise AssertionError("recovery must not download or parse Uroad or re-run can_stream")
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    def download(self, url, destination, *, environment):
        number = int(url.rsplit("/", 1)[1])
        self.assertGreater(number, 89)
        self.assertEqual(environment, "test")
        self.downloads.append(number)
        destination.write_bytes(f"synthetic parquet {number}".encode())
        return destination.stat().st_size

    def batches(self, path, batch_size, *, target_ids=None):
        self.assertEqual(batch_size, 4096)
        number = int(path.parent.name.split("-")[1])
        epoch = datetime.strptime(START, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).timestamp() + number/1000
        yield [{"timestamp": epoch, "can_id": can_id, "channel": channel, "data": bytes(size)}
               for can_id, channel, size in ((0x12A, 2, 32), (0x108, 1, 32), (0x106, 2, 16))]

    def send(self, run_path):
        result = validator.validate(run_path)
        self.assertEqual(result["runId"], RUN_ID)
        self.assertEqual(self.read_run()["task"]["context"], BINDING)
        self.assertEqual(json.loads(self.reg_path.read_text(encoding="utf-8"))["tasks"][0]["status"], "completed")
        registry.mark_delivery(RUN_ID, "sent")
        return {"status": "sent"}

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.object(pipeline, "bootstrap_dependencies", return_value=None))
        stack.enter_context(patch.object(pipeline, "run_bounded", side_effect=self.bounded))
        stack.enter_context(patch.object(wrapper, "query_files", side_effect=AssertionError("no requery")))
        stack.enter_context(patch.object(wrapper, "download_to_file", side_effect=self.download))
        stack.enter_context(patch.object(parquet, "_iter_batches", side_effect=self.batches))
        stack.enter_context(patch.object(scheduler, "min_free_bytes", return_value=0))
        stack.enter_context(patch.object(delivery_bridge, "send_native_report", side_effect=self.send))
        stack.enter_context(patch.object(followup_agent, "start_pending_followups", return_value={"started": False, "reason": "no_pending"}))
        return stack

    def test_real_cache_resume_reuses_89_of_111_and_only_executes_can_report_validation(self):
        plan = resume.preflight(self.run_path, HEADER)
        old_run, old_progress = self.run_path.read_bytes(), (self.work / "progress.json").read_bytes()
        with self.patches(): result = resume.execute(plan)
        self.assertTrue(result["ok"])
        self.assertEqual(result["delivery"]["status"], "sent")
        self.assertEqual(self.downloads, list(range(90, 112)))
        self.assertEqual([Path(command[1]).name for command in self.commands], ["parquet_can_signals.py", "build_html_report.py", "validate_report_artifact.py"])
        self.assertEqual((self.run_dir / "diagnostics" / "subprocess-0005-parquet_can.json").read_bytes(), b"old-private-diagnostic")
        self.assertEqual(self.read_run()["subprocessCalls"], 8)
        for path, value in self.cached_bytes.items(): self.assertEqual(path.read_bytes(), value)
        archives = list((self.run_dir / "diagnostics" / "recovery").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "run.json").read_bytes(), old_run)
        self.assertEqual((archives[0] / "can-progress.json").read_bytes(), old_progress)
        self.assertEqual(self.read_run()["recovery"]["attempts"][0]["status"], "completed")
        self.assertFalse((self.chunk / "analysis" / "analysis.json").exists())
        self.assertTrue((self.work / "contract-metadata.json").is_file())
        self.assertTrue((self.run_dir / "original" / "0001" / "raw" / "synthetic.log").is_file())
        self.assertFalse(list(self.run_dir.parent.glob(".heavy-slot-*.lock")))
        with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)

    def test_run_sha_change_after_preflight_refuses_claim_without_business_call(self):
        plan = resume.preflight(self.run_path, HEADER)
        self.run_path.write_bytes(self.run_path.read_bytes()+b"\n")
        changed_run, registry_bytes = self.run_path.read_bytes(), self.reg_path.read_bytes()
        with self.patches():
            with self.assertRaises(registry.RegistryError): resume.execute(plan)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.run_path.read_bytes(), changed_run)
        self.assertEqual(self.reg_path.read_bytes(), registry_bytes)

    def test_can_failure_is_safe_and_keeps_completed_uroad_and_download_receipts(self):
        plan = resume.preflight(self.run_path, HEADER)
        old_analysis = (self.chunk / "analysis" / "analysis.json").read_bytes()
        with self.patches(), patch.object(pipeline, "run_bounded", side_effect=pipeline.StageProcessError(stage="parquet_can", reason="download_interrupted")), \
             patch.object(delivery_bridge, "send_native_report") as send:
            result = resume.execute(plan)
        self.assertFalse(result["ok"])
        send.assert_not_called()
        run = self.read_run()
        self.assertEqual(run["status"], "system_error")
        self.assertEqual(run["chunks"]["active"]["lanes"]["uroad"]["status"], "completed")
        self.assertTrue(run["chunks"]["active"]["branchesDrained"])
        self.assertEqual((self.chunk / "analysis" / "analysis.json").read_bytes(), old_analysis)
        self.assertTrue((self.work / "downloads" / "item-0089" / "receipt.json").is_file())
        self.assertNotIn("synthetic.invalid", json.dumps(result))
        self.assertEqual(resume.preflight(self.run_path, HEADER).public()["status"], "ready")

    def test_followup_registration_failure_still_completes_and_sends_valid_report(self):
        with self.patches(), patch.object(followup_context, "register_data_context", side_effect=RuntimeError("private marker")), \
             patch.object(followup_agent, "start_pending_followups") as followup:
            result = resume.execute(resume.preflight(self.run_path, HEADER))
        self.assertTrue(result["ok"])
        self.assertEqual(result["delivery"]["status"], "sent")
        self.assertEqual(self.read_run()["followup"]["status"], "registration_failed")
        followup.assert_not_called()
        self.assertTrue(Path(self.read_run()["artifacts"]["report"]).is_file())

    def test_invalid_report_is_deleted_and_never_sent(self):
        bounded = self.bounded
        def invalid(command, **kwargs):
            result = bounded(command, **kwargs)
            if Path(command[1]).name == "build_html_report.py":
                Path(command[command.index("--output")+1]).write_text("incomplete report")
            return result
        with self.patches(), patch.object(pipeline, "run_bounded", side_effect=invalid), patch.object(delivery_bridge, "send_native_report") as send:
            result = resume.execute(resume.preflight(self.run_path, HEADER))
        self.assertFalse(result["ok"])
        send.assert_not_called()
        self.assertFalse(list((self.run_dir / "report").glob("*.html")))
        self.assertTrue((self.run_dir / "analysis" / "analysis.json").is_file())
        self.assertEqual(self.read_run()["status"], "system_error")

    def test_registry_completion_failure_keeps_valid_report_without_delivery(self):
        original = registry.mark_finished
        def finish(run_id, status, **kwargs):
            if status == "completed": raise registry.RegistryError("synthetic registry failure")
            return original(run_id, status, **kwargs)
        with self.patches(), patch.object(registry, "mark_finished", side_effect=finish), patch.object(delivery_bridge, "send_native_report") as send:
            result = resume.execute(resume.preflight(self.run_path, HEADER))
        self.assertFalse(result["ok"])
        send.assert_not_called()
        run = self.read_run()
        self.assertEqual(run["status"], "system_error")
        self.assertEqual(run["reportValidation"]["status"], "passed")
        self.assertTrue(Path(run["artifacts"]["report"]).is_file())
        with self.assertRaises(ValueError): validator.validate(self.run_path)

    def test_post_completion_delivery_error_does_not_reopen_analysis(self):
        with self.patches(), patch.object(delivery_bridge, "send_native_report", side_effect=RuntimeError("private transport marker")):
            result = resume.execute(resume.preflight(self.run_path, HEADER))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.read_run()["status"], "completed")
        self.assertEqual(json.loads(self.reg_path.read_text(encoding="utf-8"))["tasks"][0]["delivery"]["status"], "delivery_failed")
        self.assertTrue(Path(self.read_run()["artifacts"]["report"]).is_file())

    def interrupted(self, *, merge=False):
        """Complete synthetic CAN then model only the original host's death."""
        with patch.object(wrapper, "query_files", side_effect=AssertionError("no requery")), \
             patch.object(wrapper, "download_to_file", side_effect=self.download), \
             patch.object(parquet, "_iter_batches", side_effect=self.batches):
            wrapper.acquire(SimpleNamespace(vin=VIN, start=START, end=END, output=str(self.chunk / "analysis/can-signals.json"),
                cloud_env="test", work_dir=str(self.work), run_id=RUN_ID, page_size=37, max_files=None))
        self.downloads.clear()
        old_attempt = "a" * 32
        self.task.update(status="running", workerPid=4242424)
        self.task["execution"]["status"] = "running"
        self.run.update(status="running", error=None, task=deepcopy(self.task),
                        recovery={"schema": resume.RECOVERY_SCHEMA, "attempts": [{"attemptId": old_attempt, "status": "running"}]})
        active = self.run["chunks"]["active"]
        active.update(status="running", source="uroad+can" if merge else "can", stage="merge" if merge else "parquet_can")
        active["lanes"]["can"].update(status="completed", indexedEvidence=True)
        self.save_state()
        lease = {"schema": scheduler.RETAINED_LEASE_SCHEMA, "runId": RUN_ID, "pid": 4242424, "token": "b"*32}
        write_json(self.run_dir.parent / ".heavy-slot-0.lock", lease)
        old_merge = self.run_dir / "recovery" / old_attempt / "merge-state"
        old_merge.mkdir(parents=True)
        (old_merge / "private-partial.bin").write_bytes(b"old merge evidence")
        return old_attempt, old_merge

    def orphan_process_guards(self):
        stack = ExitStack()
        stack.enter_context(patch.object(registry, "_retained_pid_alive", return_value=False))
        stack.enter_context(patch.object(registry, "_retained_workers_exited", return_value=None))
        return stack

    def test_interrupted_mode_is_explicit_and_preflight_is_read_only(self):
        self.interrupted()
        before = file_state(self.root)
        with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
        with self.orphan_process_guards():
            plan = resume.preflight(self.run_path, HEADER, recover_interrupted=True)
        self.assertEqual(plan.public()["resumeStage"], "merge")
        self.assertTrue(plan.public()["reuseCompletedCAN"])
        self.assertEqual(file_state(self.root), before)

    def test_interrupted_merge_reuses_completed_can_without_download_or_decode(self):
        old_attempt, old_merge = self.interrupted(merge=True)
        can_paths = [self.chunk / "analysis/can-signals.json", self.work / "points.sqlite3", self.work / "contract-metadata.json"]
        original_can = {path: path.read_bytes() for path in can_paths}
        old_run = self.run_path.read_bytes()
        with self.patches(), self.orphan_process_guards(), \
             patch.object(wrapper, "acquire", side_effect=AssertionError("completed CAN must not reacquire")), \
             patch.object(registry, "heartbeat", wraps=registry.heartbeat) as heartbeat:
            result = resume.execute(resume.preflight(self.run_path, HEADER, recover_interrupted=True))
        self.assertTrue(result["ok"])
        self.assertEqual(self.downloads, [])
        self.assertEqual([Path(command[1]).name for command in self.commands], ["build_html_report.py", "validate_report_artifact.py"])
        for path, value in original_can.items(): self.assertEqual(path.read_bytes(), value)
        self.assertEqual((old_merge / "private-partial.bin").read_bytes(), b"old merge evidence")
        attempts = self.read_run()["recovery"]["attempts"]
        self.assertEqual([(item["attemptId"], item["status"]) for item in attempts[:1]], [(old_attempt, "interrupted")])
        self.assertEqual(attempts[1]["status"], "completed")
        self.assertNotEqual(attempts[1]["attemptId"], old_attempt)
        archive = self.run_dir / attempts[1]["archive"]
        self.assertEqual((archive / "run.json").read_bytes(), old_run)
        self.assertEqual([call.args[1] for call in heartbeat.call_args_list], ["merge", "report_build", "report_validate"])
        self.assertFalse(list(self.run_dir.parent.glob(".heavy-slot-*.lock")))

    def test_interrupted_cache_signature_or_live_process_prevents_takeover(self):
        self.interrupted()
        with self.orphan_process_guards(), patch.object(registry, "_retained_pid_alive", return_value=True):
            with self.assertRaises(registry.RegistryError): resume.preflight(self.run_path, HEADER, recover_interrupted=True)
        with self.orphan_process_guards(), patch.object(registry, "_retained_workers_exited", side_effect=registry.RegistryError("owned child alive")):
            with self.assertRaises(registry.RegistryError): resume.preflight(self.run_path, HEADER, recover_interrupted=True)
        metadata = self.work / "contract-metadata.json"
        metadata.write_bytes(metadata.read_bytes()+b"corrupt")
        before = file_state(self.root)
        with self.orphan_process_guards():
            with self.assertRaises(Exception): resume.preflight(self.run_path, HEADER, recover_interrupted=True)
        self.assertEqual(file_state(self.root), before)

    def test_expired_failed_or_interrupted_task_is_refused_without_writes(self):
        from retention_cleanup import retention_seconds
        import time
        self.run["completedAt"] = datetime.fromtimestamp(time.time()-retention_seconds()-60, tz=timezone.utc).isoformat()
        self.save_state()
        before = file_state(self.root)
        with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER)
        self.assertEqual(file_state(self.root), before)
        self.run.pop("completedAt")
        self.interrupted()
        old = time.time()-retention_seconds()-60
        os.utime(self.run_path, (old, old))
        before = file_state(self.root)
        with self.orphan_process_guards():
            with self.assertRaises(resume.RecoveryAdmissionError): resume.preflight(self.run_path, HEADER, recover_interrupted=True)
        self.assertEqual(file_state(self.root), before)


if __name__ == "__main__": unittest.main()
