"""Only safe classified child errors may reach task status and user messages."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import run_uroad_pipeline as pipeline
from managed_subprocess import StageProcessError


class CanFailureDiagnosticsTests(unittest.TestCase):
    def check_failure(self, child):
        with tempfile.TemporaryDirectory() as tmp:
            run = {'status':'running'}
            command = [sys.executable, str(SCRIPTS/'parquet_can_signals.py')]
            caller = pipeline.stage_caller(Path(tmp), run)
            with patch.object(pipeline, 'run_bounded', return_value=subprocess.CompletedProcess(command,1,'',json.dumps(child))):
                with self.assertRaises(StageProcessError) as caught: caller(command)
            return caught.exception, run

    def test_empty_memory_error_is_classified_without_leaking_child_detail(self):
        error, run = self.check_failure({'ok':False,'error':'','errorType':'MemoryError'})
        self.assertEqual(error.reason,'memory_exhausted')
        self.assertIn('内存不足',str(error))
        self.assertEqual(run['subprocessTimings'][0]['childErrorType'],'MemoryError')

    def test_unknown_child_error_and_private_text_are_not_public(self):
        error, run = self.check_failure({'ok':False,'error':'https://private.invalid/?token=secret','errorType':'private-identifier-secret'})
        self.assertNotIn('private',str(error))
        self.assertNotIn('secret',str(error))
        self.assertNotIn('childErrorType',run['subprocessTimings'][0])

    def test_network_interruption_keeps_specific_safe_classification(self):
        error,run=self.check_failure({'ok':False,'error':'private partial bytes','errorType':'IncompleteRead'})
        self.assertEqual(error.reason,'download_interrupted')
        self.assertIn('下载连接中断',str(error))
        self.assertNotIn('private',str(error))
        self.assertEqual(run['subprocessTimings'][0]['childErrorType'],'IncompleteRead')


if __name__=='__main__':unittest.main()
