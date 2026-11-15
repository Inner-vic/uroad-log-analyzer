"""Synthetic-only equivalence and operation-count proofs for Uroad correlation."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'parse_uroad.py'
spec=importlib.util.spec_from_file_location('uroad_candidate_parser_index_tests',SCRIPT)
parser=importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


class LegacyReportIndex:
    """Frozen pre-optimization scan, including conversion before identity checks."""
    def __init__(self,reports):self.reports=reports

    def matches(self,source,context,nodes,start,end):
        result=[]
        for item in self.reports:
            stamp=parser.timestamp_ms(item.get('ts'))
            if item.get('sourceFile')!=source or item.get('ctx')!=context:
                continue
            if start is None or end is None or stamp is None or not start<=stamp<=end:
                continue
            if nodes.get(item['node']) is None:
                continue
            result.append(item)
        return result


class ParserIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def report(self,stamp,node='UnifiedInferenceNode_A',source='a.log',ctx=1,value=1):
        return {'ts':stamp,'node':node,'sourceFile':source,'ctx':ctx,'value':value,'line':value}

    def ts(self,millisecond):return f'2026-11-03T10:00:00.{millisecond:03d}+08:00'

    def test_closed_endpoints_original_report_order_and_exact_identities(self):
        reports=[self.report(self.ts(30),value=1),self.report(self.ts(10),value=2),
                 self.report(self.ts(20),source='other.log',value=3),
                 self.report(self.ts(20),ctx=2,value=4),
                 self.report(self.ts(20),node='unknown',value=5),
                 self.report(None,value=6),self.report('invalid',value=7),
                 self.report(self.ts(20),node='UnifiedInferenceNode_B',value=8),
                 self.report(self.ts(20),value=9)]
        index=parser._InferenceReportIndex(reports)
        nodes={'UnifiedInferenceNode_A':{},'UnifiedInferenceNode_B':{}}
        result=index.matches('a.log',1,nodes,parser.timestamp_ms(self.ts(10)),parser.timestamp_ms(self.ts(30)))
        self.assertEqual([item['value'] for item in result],[1,2,8,9])
        self.assertEqual(result,LegacyReportIndex(reports).matches('a.log',1,nodes,parser.timestamp_ms(self.ts(10)),parser.timestamp_ms(self.ts(30))))
        self.assertEqual(index.matches('a.log',1,nodes,None,1),[])
        self.assertEqual(index.matches('a.log',1,nodes,2,1),[])

    def test_overlapping_cycles_do_not_consume_or_deduplicate_reports(self):
        reports=[self.report(self.ts(20),value=1),self.report(self.ts(20),value=2)]
        original=deepcopy(reports)
        index=parser._InferenceReportIndex(reports)
        nodes={'UnifiedInferenceNode_A':{}}
        bounds=[(10,30),(15,25)]
        results=[index.matches('a.log',1,nodes,parser.timestamp_ms(self.ts(a)),parser.timestamp_ms(self.ts(b))) for a,b in bounds]
        self.assertEqual([len(items) for items in results],[2,2])
        self.assertEqual([items[-1]['value'] for items in results],[2,2])
        self.assertEqual(reports,original)

    def test_randomized_lookup_equals_frozen_scan_for_multiple_sources_contexts_nodes(self):
        rng=random.Random(1103)
        reports=[self.report(self.ts(rng.randrange(100)),source=rng.choice(('a.log','b.log')),
                    ctx=rng.randrange(3),node=rng.choice(('A','B','unknown')),value=ordinal)
                 for ordinal in range(400)]
        rng.shuffle(reports)
        index=parser._InferenceReportIndex(reports);old=LegacyReportIndex(reports)
        for _ in range(100):
            source=rng.choice(('a.log','b.log'));context=rng.randrange(3)
            low=rng.randrange(90);high=rng.randrange(low,100)
            args=(source,context,{'A':{},'B':{}},parser.timestamp_ms(self.ts(low)),parser.timestamp_ms(self.ts(high)))
            self.assertEqual(index.matches(*args),old.matches(*args))

    def fixture(self):
        directory=self.root/'input';directory.mkdir()
        def line(ms,text):return f'I1103 10:00:00.{ms:03d}000 {text}\n'
        lines=[]
        for frame,low,high in ((10,10,30),(11,15,35)):
            for ms,action,task in ((low-1,'active','UroadPreNode_0'),(low,'deactive','UroadPreNode_0'),
                                   (low+1,'active','UnifiedInferenceNode_A'),(high,'active','UroadPostNode_0'),
                                   (high+1,'deactive','UroadPostNode_0')):
                lines.append(line(ms,f'CPUNode: {action} ctx 3, taskID 1, taskName {task}, frameId {frame}, maxDurationUs 1000'))
        # Deliberately out of timestamp order: the final original report wins,
        # even if its timestamp is earlier. Boundary 10 is inclusive for frame10.
        lines.extend([line(25,'[UnifiedInferenceNode_A] runWithContext ctx=3 infer=7 ms 50 us'),
                      line(10,'[UnifiedInferenceNode_A] runWithContext ctx=3 infer=4 ms 75 us'),
                      line(20,'[UnifiedInferenceNode_UNKNOWN] runWithContext ctx=3 infer=99 ms 0 us'),
                      line(20,'[UnifiedInferenceNode_A] runWithContext ctx=9 infer=99 ms 0 us'),
                      line(0,'UroadPreNode construct'),
                      line(1,'UroadPreNode::run() frame=42 speed=12kph gear=D'),
                      line(2,'UroadPreNode::run() timing(ms) frame=42 TOTAL=1.5'),
                      line(3,'UroadPostNode run frame=42'),
                      line(4,'UroadPostNode::run() timing(ms) frame=42 TOTAL=2.5'),
                      line(5,'FPSlast10frames: 10.0 Hz'),
                      line(6,'[uroad] Trajectory data timeout')])
        (directory/'a.log').write_text(''.join(lines),encoding='utf-8')
        (directory/'b.log').write_text(''.join(line.replace('infer=7 ms','infer=17 ms') for line in lines),encoding='utf-8')
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps({'runId':'synthetic-only','extracted':{'files':['a.log','b.log']},'query':{}}),encoding='utf-8')
        return manifest

    def analyze(self,manifest,output,*,legacy=False,progress=False):
        output.mkdir(exist_ok=True)
        args=SimpleNamespace(manifest=str(manifest),output_dir=str(output),can_signals=None)
        observation=parser._ParseProgress(output,enabled=progress)
        with redirect_stdout(io.StringIO()):
            if legacy:
                with patch.object(parser,'_InferenceReportIndex',LegacyReportIndex):parser._analyze(args,observation)
            else:parser._analyze(args,observation)
        return json.loads((output/'analysis.json').read_text(encoding='utf-8'))

    def test_complete_synthetic_analysis_is_field_for_field_equal_to_legacy_join(self):
        manifest=self.fixture()
        old=self.analyze(manifest,self.root/'old',legacy=True)
        new=self.analyze(manifest,self.root/'new')
        self.assertEqual(new,old)
        self.assertEqual(new['metrics']['edsgCycleCount'],4)
        self.assertEqual(new['metrics']['edsgInferenceReportMatches'],6)
        self.assertEqual(new['inferenceNodes']['UnifiedInferenceNode_A']['count'],6)
        self.assertFalse((self.root/'new/parse-progress.json').exists())

    def test_timestamp_conversion_count_scales_with_reports_not_cycles_times_reports(self):
        reports=[self.report(self.ts(value%100),value=value) for value in range(256)]
        real=parser.timestamp_ms
        count=0
        def counted(value):
            nonlocal count
            count+=1
            return real(value)
        start,end=real(self.ts(10)),real(self.ts(20))
        args=('a.log',1,{'UnifiedInferenceNode_A':{}},start,end)
        with patch.object(parser,'timestamp_ms',side_effect=counted):
            index=parser._InferenceReportIndex(reports)
            for _ in range(64):index.matches(*args)
        self.assertEqual(count,len(reports))
        count=0
        with patch.object(parser,'timestamp_ms',side_effect=counted):
            old=LegacyReportIndex(reports)
            for _ in range(64):old.matches(*args)
        self.assertEqual(count,64*len(reports))

    def test_progress_is_finite_identity_free_and_does_not_change_analysis(self):
        manifest=self.fixture()
        plain=self.analyze(manifest,self.root/'plain')
        observed=self.analyze(manifest,self.root/'observed',progress=True)
        self.assertEqual(observed,plain)
        value=json.loads((self.root/'observed/parse-progress.json').read_text())
        self.assertIs(parser.validate_parse_progress(value),value)
        self.assertEqual(value['stage'],'completed')
        self.assertEqual(value['files_read'],2)
        self.assertEqual(value['cycles_processed'],4)
        self.assertEqual(value['matches'],6)
        text=json.dumps(value)
        for forbidden in ('a.log','b.log','synthetic-only',str(self.root),'rawExcerpt','VIN'):
            self.assertNotIn(forbidden,text)

    def test_progress_write_rate_is_low_and_io_failure_does_not_mask_analysis(self):
        output=self.root/'progress';output.mkdir()
        with patch.object(parser,'monotonic',return_value=10.0),patch.object(Path,'write_text',return_value=0) as write,patch.object(Path,'replace'):
            observation=parser._ParseProgress(output,enabled=True)
            for value in range(100):observation.update(lines_read=value)
            self.assertEqual(write.call_count,1)
            observation.update(stage='indexing_inference')
            self.assertEqual(write.call_count,2)
        observation=parser._ParseProgress(output,enabled=True)
        with patch.object(Path,'write_text',side_effect=OSError('private I/O detail')):
            observation.update(stage='failed')

    def test_progress_validator_rejects_extra_identity_fields_bool_counts_and_nonfinite_times(self):
        base={'schema':parser.PROGRESS_SCHEMA,'stage':'reading_inputs','elapsed_seconds':0.0,
              **dict.fromkeys(parser.PROGRESS_COUNTS,0)}
        for extra in ({'sourceFile':'secret'},{'lines_read':True},{'elapsed_seconds':float('inf')},{'stage':'arbitrary raw exception'}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):parser.validate_parse_progress({**base,**extra})

    def test_input_line_progress_counts_all_lines_without_retaining_raw_data(self):
        observed=[]
        with patch.object(Path,'open',return_value=io.StringIO('ordinary irrelevant line\n'*100001)):
            result=parser.parse_file(Path('synthetic.log'),on_progress=observed.append)
        self.assertEqual(observed,[100000])
        self.assertEqual(result[9],100001)

    def test_main_writes_finite_failed_stage_without_raw_exception(self):
        output=self.root/'failed';output.mkdir()
        with patch.object(sys,'argv',['parse_uroad','--manifest','unused','--output-dir',str(output),'--progress']),patch.object(parser,'_analyze',side_effect=RuntimeError('private raw exception')):
            with self.assertRaises(RuntimeError):parser.main()
        value=json.loads((output/'parse-progress.json').read_text())
        self.assertEqual(value['stage'],'failed')
        self.assertNotIn('private',json.dumps(value))


if __name__=='__main__':unittest.main()
