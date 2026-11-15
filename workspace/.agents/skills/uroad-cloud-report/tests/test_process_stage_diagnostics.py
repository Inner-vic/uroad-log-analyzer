"""Synthetic single-window stage, failure-retention and owned-future checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
from threading import Event, RLock, get_ident
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import process_time_chunks as processing


class SyntheticTimeout(TimeoutError):
    def __init__(self, stage):
        self.stage = stage
        self.timeout_seconds = 0.1
        self.elapsed_seconds = 0.1
        self.retryable = True
        super().__init__('当前阶段超过受控时间限制。')


class ProcessStageDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = {'runId': 'run-synthetic', 'status': 'running'}
        self.args = SimpleNamespace(vin='DEM0VEH1CLEX00001', start='2026-11-03 10:00:00',
            end='2026-11-03 10:35:00', resolved_cloud_env='test', resolved_provider_environment='testtwo',
            cloud_env='auto', eea_platform='EEA3.0', page_size=100, max_files=None)
        self.scripts = {'python': 'synthetic-python', 'uroad': 'cheyun_uroad_adapter.py',
            'analysis': 'parse_uroad.py', 'can_stream': 'stream_can_signals.py', 'parquet_can': 'parquet_can_signals.py'}
        self.chunk = self.root/'slices/0001'
        self.calls = []
        self.snapshots = []
        self.progress = {'schema': 'uroad-parse-progress-v1', 'stage': 'correlating_inference',
            'files_read': 1, 'lines_read': 10, 'cycles_processed': 2, 'inference_reports': 3,
            'matches': 1, 'elapsed_seconds': 0.1}
        self.can = {'signals': {key: {'series': [{'value': 1}]} for key in processing.REQUIRED_CAN_SIGNALS},
                    'environment': {'environment': 'test', 'providerEnvironment': 'testtwo'}}
        self.save_running = 0
        self.max_saves = 0
        def save(path, value):
            # The real parent shares this RLock with call() timing writes. A
            # deliberate pause would expose overlapping JSON serialization.
            self.save_running += 1
            self.max_saves = max(self.max_saves, self.save_running)
            try:
                sleep(0.001)
                self.snapshots.append(deepcopy(value))
                processing.atomic_json(Path(path), value)
            finally:
                self.save_running -= 1
        save.state_lock = RLock()
        self.save = save

    def command_value(self, command, flag):
        return command[command.index(flag)+1]

    def download(self, command):
        chunk = Path(self.command_value(command, '--output-dir'))
        (chunk/'raw').mkdir(parents=True)
        (chunk/'input').mkdir()
        (chunk/'analysis').mkdir()
        (chunk/'raw/archive.zst').write_bytes(b'synthetic archive')
        (chunk/'input/source.log').write_text('synthetic log\n', encoding='utf-8')
        query = {'vin': self.args.vin, 'startTime': self.args.start, 'endTime': self.args.end,
                 'cloudEnv': 'test', 'providerEnvironment': 'testtwo', 'logClass': 'log_fsdA_service'}
        processing.atomic_json(chunk/'manifest.json', {'query': query, 'extracted': {'uroadMatched': True}})

    def can_output(self, command):
        path = Path(self.command_value(command, '--output'))
        path.parent.mkdir(parents=True, exist_ok=True)
        processing.atomic_json(path, self.can)
        work = self.chunk/'can-work'
        work.mkdir(exist_ok=True)
        (work/'source.can').write_bytes(b'synthetic CAN')

    def parse_output(self, command):
        self.assertIn('--progress', command)
        directory = Path(self.command_value(command, '--output-dir'))
        processing.atomic_json(directory/'parse-progress.json', {**self.progress, 'stage': 'completed'})
        processing.atomic_json(directory/'analysis.json', {'source': {}, 'summary': {}})

    def run_process(self, call):
        processing.process(self.args, self.root, self.run, self.save, call, self.scripts)

    def test_parser_timeout_waits_for_can_and_preserves_complete_failure_material(self):
        can_started = Event(); parser_failed = Event(); release_can = Event()
        def call(command):
            self.calls.append(command)
            if command[1] == self.scripts['uroad']:
                return self.download(command)
            if command[1] == self.scripts['can_stream']:
                can_started.set()
                self.assertTrue(release_can.wait(3))
                return self.can_output(command)
            self.assertEqual(command[1], self.scripts['analysis'])
            self.assertTrue(can_started.wait(2))
            self.assertIn('--progress', command)
            processing.atomic_json(self.chunk/'analysis/parse-progress.json', self.progress)
            parser_failed.set()
            raise SyntheticTimeout('uroad_parse')
        with ThreadPoolExecutor(max_workers=1) as owner:
            future = owner.submit(self.run_process, call)
            try:
                self.assertTrue(parser_failed.wait(2))
                # Wait for the controller to persist its drain state while CAN
                # still owns its write paths. It must not report a terminal yet.
                for _ in range(200):
                    with self.save.state_lock:
                        if self.run['chunks']['active']['status'] == 'draining':
                            break
                    sleep(0.005)
                with self.save.state_lock:
                    self.assertEqual(self.run['chunks']['active']['status'], 'draining')
                    self.assertEqual(self.run['chunks']['active']['stage'], 'uroad_parse')
                    self.assertEqual(self.run['chunks']['active']['lanes']['can']['status'], 'running')
                self.assertFalse(future.done())
                self.assertTrue((self.chunk/'input/source.log').is_file())
            finally:
                release_can.set()
            with self.assertRaises(SyntheticTimeout) as error:
                future.result(timeout=3)
        self.assertEqual(error.exception.lane_stage, 'uroad_parse')
        state = self.run['chunks']['active']
        self.assertEqual((state['status'], state['source'], state['stage']), ('failed', 'uroad', 'uroad_parse'))
        self.assertTrue(state['branchesDrained'])
        self.assertEqual(state['lanes']['can']['status'], 'completed')
        self.assertEqual([item['stage'] for item in state['lanes']['uroad']['stages']], ['uroad_download', 'uroad_parse'])
        for item in state['lanes']['uroad']['stages']:
            self.assertIn('startedAt', item); self.assertIn('endedAt', item)
            self.assertGreaterEqual(item['elapsedSeconds'], 0)
        self.assertEqual(state['lanes']['uroad']['parserProgress']['value'], self.progress)
        self.assertEqual(self.max_saves, 1)
        for name in ('raw/archive.zst', 'input/source.log', 'manifest.json', 'analysis/parse-progress.json',
                     'analysis/can-signals.json', 'can-work/source.can'):
            self.assertTrue((self.chunk/name).is_file(), name)
        self.assertTrue((self.root/'merge-state/evidence.sqlite3').is_file())
        self.assertEqual(self.run['failureRetention']['paths'], ['slices', 'merge-state'])
        self.assertFalse((self.root/'analysis/analysis.json').exists())
        self.assertFalse((self.root/'report').exists())
        self.assertEqual({command[1] for command in self.calls},
                         {self.scripts[key] for key in ('uroad', 'analysis', 'can_stream')})

    def test_can_fallback_has_separate_precise_stage_and_success_cleanup_is_unchanged(self):
        def call(command):
            self.calls.append(command)
            if command[1] == self.scripts['uroad']:
                return self.download(command)
            if command[1] == self.scripts['analysis']:
                return self.parse_output(command)
            if command[1] == self.scripts['can_stream']:
                raise RuntimeError('非 zstd 文件')
            return self.can_output(command)
        # Full metric merging is covered by parser/report regressions; this
        # fixture isolates successful retention and actual lane coordination.
        with patch.object(processing.EvidenceStore, 'add_slice'), \
                patch.object(processing, 'load_indexed_contract', return_value=self.can) as indexed, \
                patch.object(processing.EvidenceStore, 'finalize_can', return_value=self.can), \
                patch.object(processing.EvidenceStore, 'finalize_uroad', return_value={'summary': {}}):
            self.run_process(call)
        fallback = next(command for command in self.calls if command[1] == self.scripts['parquet_can'])
        self.assertEqual(self.command_value(fallback, '--work-dir'), str(self.chunk/'can-parquet'))
        self.assertEqual(self.command_value(fallback, '--run-id'), self.run['runId'])
        self.assertEqual(self.command_value(fallback, '--page-size'), str(self.args.page_size))
        self.assertEqual(indexed.call_args.args[3], {'runId':self.run['runId'], 'vin':self.args.vin,
            'start':self.args.start, 'end':self.args.end, 'environment':'test', 'providerEnvironment':'testtwo'})
        state = self.run['chunks']['items'][0]
        self.assertEqual([item['stage'] for item in state['lanes']['can']['stages']], ['can_stream', 'parquet_can'])
        self.assertEqual([item['status'] for item in state['lanes']['can']['stages']], ['failed', 'completed'])
        self.assertEqual(state['lanes']['can']['status'], 'completed')
        self.assertEqual(self.max_saves, 1)
        self.assertNotIn('failureRetention', self.run)
        self.assertTrue((self.root/'slices/0001/analysis/can-signals.json').is_file())
        self.assertFalse((self.root/'slices/0001/analysis/analysis.json').exists())
        self.assertIn('slices/0001/analysis/can-signals.json',state['retainedCANEvidence'])
        self.assertFalse((self.root/'merge-state').exists())
        self.assertTrue((self.root/'original/0001/raw/archive.zst').is_file())
        self.assertTrue((self.root/'original/0001/input/source.log').is_file())
        self.assertTrue((self.root/'analysis/analysis.json').is_file())
        self.assertEqual(self.run['chunks']['completed'], 1)

    def test_manifest_environment_conflict_fails_before_parser_and_retains_sources(self):
        def call(command):
            self.calls.append(command)
            if command[1] == self.scripts['uroad']:
                self.download(command)
                path = self.chunk/'manifest.json'
                value = json.loads(path.read_text(encoding='utf-8'))
                value['query']['providerEnvironment'] = 'prod'
                processing.atomic_json(path, value)
            else:
                self.can_output(command)
        with self.assertRaisesRegex(RuntimeError, 'manifest') as error:
            self.run_process(call)
        self.assertEqual(error.exception.lane_stage, 'uroad_download')
        self.assertNotIn(self.scripts['analysis'], [command[1] for command in self.calls])
        self.assertTrue((self.chunk/'manifest.json').is_file())
        self.assertTrue((self.root/'merge-state/evidence.sqlite3').is_file())

    def test_invalid_progress_is_optional_evidence_and_never_copies_unknown_fields(self):
        path = self.chunk/'analysis/parse-progress.json'
        path.parent.mkdir(parents=True)
        for value in ({**self.progress, 'raw_log': 'must never be surfaced'},
                      {**self.progress, 'lines_read': True}, {**self.progress, 'elapsed_seconds': float('nan')},
                      {**self.progress, 'elapsed_seconds': 10**400}):
            processing.atomic_json(path, value)
            self.assertEqual(processing._parser_progress(self.chunk, self.root),
                             {'path': 'slices/0001/analysis/parse-progress.json', 'available': False})

    def test_second_submission_failure_drains_first_lane_before_terminal(self):
        lane_started=Event();submission_failed=Event();release_lane=Event()
        real_executor=ThreadPoolExecutor
        test=self
        class FailsSecondSubmission:
            def __init__(self,**kwargs):
                self.executor=real_executor(**kwargs)
                self.calls=0
            def submit(self,function,*args,**kwargs):
                self.calls+=1
                if self.calls==2:
                    test.assertTrue(lane_started.wait(2))
                    submission_failed.set()
                    raise RuntimeError('synthetic second submission failed')
                return self.executor.submit(function,*args,**kwargs)
            def shutdown(self,**kwargs):return self.executor.shutdown(**kwargs)
        def call(command):
            self.calls.append(command)
            if command[1]==self.scripts['uroad']:
                self.download(command)
                lane_started.set()
                self.assertTrue(release_lane.wait(3))
                return
            self.assertEqual(command[1],self.scripts['analysis'])
            return self.parse_output(command)
        with patch.object(processing,'ThreadPoolExecutor',FailsSecondSubmission),real_executor(max_workers=1) as owner:
            future=owner.submit(self.run_process,call)
            try:
                self.assertTrue(submission_failed.wait(2))
                for _ in range(200):
                    with self.save.state_lock:
                        if self.run['chunks']['active']['status']=='draining':break
                    sleep(.005)
                with self.save.state_lock:
                    state=self.run['chunks']['active']
                    self.assertEqual(state['status'],'draining')
                    self.assertEqual(state['stage'],'parallel_start')
                    self.assertFalse(state.get('branchesDrained',False))
                self.assertFalse(future.done())
            finally:release_lane.set()
            with self.assertRaisesRegex(RuntimeError,'second submission'):
                future.result(timeout=3)
        state=self.run['chunks']['active']
        self.assertEqual(state['status'],'failed')
        self.assertTrue(state['branchesDrained'])
        self.assertEqual(state['lanes']['uroad']['status'],'completed')
        self.assertEqual(state['lanes']['can']['status'],'pending')
        self.assertEqual(self.max_saves,1)
        self.assertTrue((self.chunk/'analysis/analysis.json').is_file())
        self.assertTrue((self.chunk/'raw/archive.zst').is_file())
        self.assertFalse((self.root/'analysis/analysis.json').exists())

    def test_huge_progress_elapsed_does_not_replace_original_parser_timeout(self):
        def call(command):
            if command[1]==self.scripts['uroad']:return self.download(command)
            if command[1]==self.scripts['can_stream']:return self.can_output(command)
            processing.atomic_json(self.chunk/'analysis/parse-progress.json',
                                   {**self.progress,'elapsed_seconds':10**400})
            raise SyntheticTimeout('uroad_parse')
        with self.assertRaises(SyntheticTimeout) as caught:self.run_process(call)
        self.assertEqual(caught.exception.stage,'uroad_parse')
        state=self.run['chunks']['active']
        self.assertEqual(state['lanes']['uroad']['parserProgress'],
                         {'path':'slices/0001/analysis/parse-progress.json','available':False})
        self.assertTrue(state['branchesDrained'])


if __name__ == '__main__':
    unittest.main()
