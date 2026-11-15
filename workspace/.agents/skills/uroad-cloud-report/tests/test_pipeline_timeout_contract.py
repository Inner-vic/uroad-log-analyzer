"""Synthetic-only timeout, lifecycle and artifact admission regressions."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import managed_subprocess as managed
import run_uroad_pipeline as pipeline
import validate_report_artifact as validator

VIN = "DEM0VEH1CLEX00001"
START, END = "2026-11-02 17:29:00", "2026-11-02 18:04:00"
RUN_ID = "run-20261103-120000-000001-abcdef01"
HTML = '<!doctype html><html><style></style><script>const data=JSON.parse("")</script><p id="report-data"></p></html>'


def process_has_ended(pid):
    if os.name == "nt":
        api = managed._api()
        api.OpenProcess.argtypes = [managed.wintypes.DWORD, managed.wintypes.BOOL, managed.wintypes.DWORD]
        api.OpenProcess.restype = managed.wintypes.HANDLE
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            return True
        try:
            return api.WaitForSingleObject(handle, 0) == 0
        finally:
            api.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split()[2] == "Z":
        return True
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


class TimeoutContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run_dir = self.root / "outputs/uroad-report-runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        self.report = self.run_dir / "report" / pipeline.build_report_filename(VIN, START, END)

    def test_real_timeout_reclaims_child_and_grandchild_and_hides_argv(self):
        child_pid = self.root / "child.pid"
        secret = "synthetic-private-command-marker"
        child = "import time; time.sleep(30)"
        code = ("import subprocess,sys,pathlib,time;"
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid));"
                f"print({secret!r},flush=True);time.sleep(30)")
        before = time.monotonic()
        diagnostic = self.root / "diagnostics/timeout.json"
        with self.assertRaises(managed.StageTimeout) as caught:
            managed.run_bounded([sys.executable, "-B", "-c", code], env=os.environ.copy(),
                                timeout=1.0, stage="uroad_parse", diagnostics_path=diagnostic)
        self.assertLess(time.monotonic()-before, 6)
        self.assertTrue(child_pid.is_file(), "synthetic parent did not start its grandchild")
        pid = int(child_pid.read_text())
        deadline = time.monotonic()+2
        while not process_has_ended(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(process_has_ended(pid), "isolated grandchild survived timeout")
        error = caught.exception
        self.assertEqual(error.stage, "uroad_parse")
        self.assertTrue(error.retryable)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(str(self.root), str(error))
        evidence = json.loads(diagnostic.read_text(encoding="utf-8"))
        self.assertEqual(evidence["status"], "timeout")
        self.assertIn(secret, str(evidence["command"]))

    def test_success_still_cleans_up_descendants_with_closed_pipes(self):
        child_pid = self.root / "survivor.pid"
        child = "import time;time.sleep(30)"
        code = ("import subprocess,sys,pathlib;"
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid));print('done')")
        result = managed.run_bounded([sys.executable, "-B", "-c", code], env=os.environ.copy(),
                                     timeout=3, stage="uroad_parse")
        self.assertEqual(result.returncode, 0)
        deadline = time.monotonic()+2
        pid = int(child_pid.read_text())
        while not process_has_ended(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(process_has_ended(pid))

    def test_fixed_900s_per_stage_and_safe_failure(self):
        run = {"status": "running"}
        caller = pipeline.stage_caller(self.run_dir, run)
        command = [sys.executable, str(SCRIPTS/"parse_uroad.py"), "--manifest", "private-manifest"]
        with patch.object(pipeline, "run_bounded", return_value=subprocess.CompletedProcess(command, 1, "", "private-stderr-url")) as bounded:
            with self.assertRaises(managed.StageProcessError) as caught:
                caller(command)
        self.assertEqual(bounded.call_args.kwargs["timeout"], 900)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(run["subprocessTimings"][0]["stage"], "uroad_parse")
        self.assertEqual(run["subprocessTimings"][0]["status"], "failed")
        self.assertNotIn("command", run["subprocessTimings"][0])

    def test_parallel_calls_share_atomic_save_and_distinct_timings(self):
        run = {"status": "running"}
        caller = pipeline.stage_caller(self.run_dir, run)
        def fake(command, **_kwargs):
            time.sleep(.01)
            return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")
        commands = [[sys.executable, str(SCRIPTS/name)] for name in ("parse_uroad.py", "stream_can_signals.py")]
        with patch.object(pipeline, "run_bounded", side_effect=fake), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(caller, commands))
        self.assertTrue(all(result["ok"] for result in results))
        saved = json.loads((self.run_dir/"run.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["subprocessTimings"]), 2)
        self.assertEqual({item["sequence"] for item in saved["subprocessTimings"]}, {1, 2})
        self.assertTrue(all(item["status"] == "completed" and item["elapsedSeconds"] >= 0 for item in saved["subprocessTimings"]))

    def test_known_can_non_zstd_fallback_is_preserved(self):
        caller = pipeline.stage_caller(self.run_dir, {})
        command = [sys.executable, str(SCRIPTS/"stream_can_signals.py")]
        with patch.object(pipeline, "run_bounded", return_value=subprocess.CompletedProcess(command, 1, "", '{"error":"CAN 查询返回了非 zstd 文件"}')):
            with self.assertRaisesRegex(managed.StageProcessError, "非 zstd 文件"):
                caller(command)

    def test_existing_failed_evidence_is_never_overwritten_on_retry(self):
        prior = self.run_dir/"run.json"
        prior.write_text('{"status":"system_error","original":true}', encoding="utf-8")
        evidence = self.run_dir/"merge-state"
        evidence.mkdir()
        marker = evidence/"progress.txt"
        marker.write_text("preserve me")
        with self.assertRaisesRegex(RuntimeError, "不能覆盖重试"):
            pipeline.allocate_run_directory(self.root, existing_run_id=RUN_ID)
        self.assertEqual(marker.read_text(), "preserve me")
        self.assertTrue(json.loads(prior.read_text())["original"])

    def test_unexecuted_queued_existing_directory_remains_usable(self):
        (self.run_dir/"run.json").write_text('{"status":"queued","error":null}')
        self.assertEqual(pipeline.allocate_run_directory(self.root, existing_run_id=RUN_ID), self.run_dir)

    def report_payload(self, status="validating_report"):
        self.report.parent.mkdir()
        self.report.write_text(HTML, encoding="utf-8")
        payload = {"runId": RUN_ID, "status": status, "input": {"vin": VIN, "start": START, "end": END},
                   "steps": {"report": "completed"}, "reportValidation": {"status": "pending"},
                   "artifacts": {"report": str(self.report)},
                   "task": {"runId": RUN_ID, "request": {"vin": VIN, "start": START, "end": END},
                            "context": {"requesterId": "ou_owner", "chatId": "oc_chat", "messageId": "om_source"}}}
        pipeline.save(self.run_dir/"run.json", payload)
        return payload

    def test_internal_validation_does_not_relax_default_delivery_gate(self):
        self.report_payload()
        path = self.run_dir/"run.json"
        with patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": str(self.root)}):
            with self.assertRaises(ValueError):
                validator.validate(path)
            with self.assertRaises(ValueError):
                validator.validate_attachment_path(path, self.report)
            self.assertTrue(validator.validate(path, allow_validating=True)["ok"])

    def test_internal_validation_requires_pending_complete_report_and_source(self):
        original = self.report_payload()
        mutations = (lambda p: p["steps"].update(report="pending"),
                     lambda p: p["reportValidation"].update(status="passed"),
                     lambda p: p["task"]["context"].pop("messageId"),
                     lambda p: p["task"].update(runId="foreign"),
                     lambda p: p.update(status="running"))
        with patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": str(self.root)}):
            for mutate in mutations:
                payload = copy.deepcopy(original)
                mutate(payload)
                pipeline.save(self.run_dir/"run.json", payload)
                with self.assertRaises(ValueError):
                    validator.validate(self.run_dir/"run.json", allow_validating=True)

    def test_contextless_offline_report_validates_but_cannot_bypass_delivery_identity(self):
        payload = self.report_payload()
        payload["task"]["context"] = {}
        pipeline.save(self.run_dir/"run.json", payload)
        with patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": str(self.root)}):
            self.assertTrue(validator.validate(self.run_dir/"run.json", allow_validating=True)["ok"])
            with self.assertRaises(ValueError):
                validator.validate_attachment_path(self.run_dir/"run.json", self.report)
        import delivery_bridge
        with self.assertRaisesRegex(ValueError, "任务缺少完整可验证的来源，拒绝发送"):
            delivery_bridge._session_key(payload)

    def test_partial_identity_conflicts_and_request_input_mismatch_are_rejected(self):
        original = self.report_payload()
        mutations = (lambda p: p["task"]["context"].pop("requesterId"),
                     lambda p: p["task"].update(source={"chatId": "oc_foreign"}),
                     lambda p: p["task"].update(requester={"id": "ou_foreign"}),
                     lambda p: p["task"]["request"].update(start="2026-11-02 17:30:00"),
                     lambda p: p["task"].pop("request"))
        with patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": str(self.root)}):
            for mutate in mutations:
                payload = copy.deepcopy(original)
                mutate(payload)
                pipeline.save(self.run_dir/"run.json", payload)
                with self.assertRaises(ValueError):
                    validator.validate(self.run_dir/"run.json", allow_validating=True)

    def run_mock_pipeline(self, *, validate_failure=False, delivery_failure=False,
                          registration_failure=False, registry_completion_failure=False):
        """Exercise main with fake acquisition and child APIs; no business registry."""
        import cheyun_prod_client as cloud
        import delivery_bridge
        import followup_context
        import followup_agent
        import process_time_chunks
        import retention_cleanup
        task = {"runId": RUN_ID, "status": "queued", "request": {"vin": VIN, "start": START, "end": END},
                "context": {"requesterId": "ou_owner", "chatId": "oc_chat", "messageId": "om_source"}}
        events = []
        @contextmanager
        def slot(_run_id):
            yield dict(task, status="running")
        def processing(_args, run_dir, run, save, call, scripts):
            (run_dir/"slices/0001/input").mkdir(parents=True)
            (run_dir/"slices/0001/input/evidence.log").write_text("synthetic evidence")
            (run_dir/"merge-state").mkdir()
            (run_dir/"analysis").mkdir()
            for name in ("analysis.json", "can-signals.json"):
                (run_dir/"analysis"/name).write_text("{}")
            (run_dir/"manifest.json").write_text("{}")
        def child(command, **kwargs):
            stage = kwargs["stage"]
            if stage == "report_build":
                self.report.parent.mkdir(exist_ok=True)
                self.report.write_text(HTML, encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "{}", "")
            if stage == "report_validate":
                current = json.loads((self.run_dir/"run.json").read_text(encoding="utf-8"))
                self.assertEqual(current["status"], "validating_report")
                self.assertIn("--allow-validating", command)
                events.append("validate")
                if validate_failure:
                    raise managed.StageTimeout(stage=stage, timeout=900, elapsed=900)
                return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "report": str(self.report)}), "")
            raise AssertionError("unexpected synthetic stage")
        def finished(_run_id, status, **_kwargs):
            events.append(status)
            if status == "completed" and registry_completion_failure:
                raise RuntimeError("synthetic registry completion failure")
        argv = [str(SCRIPTS/"run_uroad_pipeline.py"), "--vin", VIN, "--start", START, "--end", END,
                "--cloud-env", "prod", "--existing-run-id", RUN_ID]
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(pipeline, "validate_workspace", return_value=self.root))
            stack.enter_context(patch.object(pipeline, "bootstrap_dependencies"))
            stack.enter_context(patch.object(pipeline, "load_request_metadata", return_value={"taskRecord": task}))
            stack.enter_context(patch.object(pipeline, "existing_task_for_execution", return_value=task))
            stack.enter_context(patch.object(pipeline, "acquire_heavy_slot", side_effect=slot))
            stack.enter_context(patch.object(pipeline, "mark_finished", side_effect=finished))
            stack.enter_context(patch.object(pipeline, "run_bounded", side_effect=child))
            stack.enter_context(patch.object(cloud, "load_settings", return_value={"username": "synthetic"}))
            stack.enter_context(patch.object(cloud, "query_files", return_value=type("Listing", (), {"items": [{}], "total": 1, "download_hosts": []})()))
            stack.enter_context(patch.object(cloud, "provider_environment", return_value="prod"))
            stack.enter_context(patch.object(retention_cleanup, "apply_retention", return_value={"ok": True}))
            stack.enter_context(patch.object(process_time_chunks, "process", side_effect=processing))
            stack.enter_context(patch.object(followup_context, "register_data_context",
                                            side_effect=RuntimeError("synthetic private registration fault") if registration_failure else None,
                                            return_value={"version": 1, "retentionUntil": "future"}))
            self.followup_worker = stack.enter_context(patch.object(followup_agent, "start_pending_followups", return_value={"started": False}))
            self.delivery_call = stack.enter_context(patch.object(delivery_bridge, "send_native_report", return_value={"status": "failed" if delivery_failure else "sent"}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            result = pipeline.main()
        return result, events, json.loads((self.run_dir/"run.json").read_text(encoding="utf-8"))

    def test_main_validates_before_terminal_registry_completion(self):
        code, events, saved = self.run_mock_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(events, ["validate", "completed"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["reportValidation"]["status"], "passed")

    def test_validation_timeout_preserves_evidence_without_false_completion(self):
        code, events, saved = self.run_mock_pipeline(validate_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["validate", "system_error"])
        self.assertEqual(saved["error"]["type"], "StageTimeout")
        self.assertEqual(saved["error"]["timeoutSeconds"], 900)
        self.assertEqual(saved["error"]["stage"], "report_validate")
        self.assertTrue((self.run_dir/"slices/0001/input/evidence.log").is_file())
        self.assertTrue((self.run_dir/"merge-state").is_dir())
        self.assertFalse(self.report.exists())
        self.assertEqual(saved["cleanup"]["removed"], [])

    def test_delivery_failure_keeps_validated_report_and_completed_analysis(self):
        code, events, saved = self.run_mock_pipeline(delivery_failure=True)
        self.assertEqual(code, 0)
        self.assertEqual(events, ["validate", "completed"])
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(self.report.is_file())

    def test_followup_registration_failure_does_not_block_standard_report(self):
        code, events, saved = self.run_mock_pipeline(registration_failure=True)
        self.assertEqual(code, 0)
        self.assertEqual(events, ["validate", "completed"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["followup"]["status"], "registration_failed")
        self.assertNotIn("synthetic private", saved["followup"]["error"])
        self.assertTrue(self.report.is_file())
        self.delivery_call.assert_called_once()
        self.followup_worker.assert_not_called()

    def test_registry_completion_failure_retains_valid_report_but_cannot_deliver(self):
        code, events, saved = self.run_mock_pipeline(registry_completion_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["validate", "completed", "system_error"])
        self.assertEqual(saved["status"], "system_error")
        self.assertEqual(saved["reportValidation"]["status"], "passed")
        self.assertTrue(self.report.is_file())
        self.delivery_call.assert_not_called()
        self.followup_worker.assert_not_called()
        with patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": str(self.root)}):
            with self.assertRaises(ValueError):
                validator.validate_attachment_path(self.run_dir/"run.json", self.report)


if __name__ == "__main__":
    unittest.main()
