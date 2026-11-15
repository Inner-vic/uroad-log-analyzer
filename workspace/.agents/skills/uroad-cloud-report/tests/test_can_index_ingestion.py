"""The host consumes complete CAN evidence as an iterator, without JSON loading."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from process_time_chunks import EvidenceStore, cleanup_completed_chunk
import parquet_can_stream as parquet


class LazySeries:
    def __init__(self, count):self.count=count;self.read=0
    def __len__(self):return self.count
    def __iter__(self):
        for n in range(self.count):
            self.read+=1
            yield {'epochMs':100000+n*10,'raw':n,'value':n,'ts':f'2026-11-03T10:00:{n%60:02d}+08:00'}


class CanIndexIngestionTests(unittest.TestCase):
    def test_host_retains_every_lazy_point_and_environment_in_disk_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=EvidenceStore(root/'evidence.sqlite3')
            try:
                series={key:LazySeries(100) for key in ('vehicleSpeed','gear','steeringAngle')}
                can={'signals':{key:{'name':key,'series':points,'rawPointCount':100,'uniquePointCount':100} for key,points in series.items()},
                     'environment':{'environment':'test','providerEnvironment':'testtwo'}}
                with self.assertRaises(TypeError):json.dumps(can)
                store.add_slice(1,{'charts':{}},can)
                for key,points in series.items():
                    self.assertEqual(points.read,100)
                    self.assertEqual(store.row_count('can:'+key),100)
                merged=store.finalize_can()
                self.assertEqual(merged['environment'],can['environment'])
                self.assertEqual({k:v['uniquePointCount'] for k,v in merged['signals'].items()},dict.fromkeys(series,100))
                foreign={**can,'environment':{'environment':'prod','providerEnvironment':'prod'}}
                with self.assertRaisesRegex(RuntimeError,'environment mismatch'):store.add_slice(2,{'charts':{}},foreign)
            finally:store.close()

    def test_completed_task_keeps_receipt_paths_and_full_can_artifacts_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);chunk=root/'slices/0001';cache=chunk/'can-parquet';cache.mkdir(parents=True)
            source=cache/'0001-source.parquet';source.write_bytes(b'synthetic parquet')
            output=chunk/'analysis/can-signals.json';index=cache/'points.sqlite3';metadata=cache/'contract-metadata.json'
            binding={'runId':'run-synthetic','vin':'DEM0VEH1CLEX00001','start':'2026-11-03 10:00:00','end':'2026-11-03 10:00:01','environment':'test','providerEnvironment':'testtwo'}
            definitions=Path(__file__).resolve().parents[1]/'references/can-signals.json'
            rows=[{'timestamp':1793671200.25,'can_id':0x12A,'channel':2,'data':bytes(32)}]
            with patch.object(parquet,'_iter_batches',return_value=iter([rows])):
                parquet.build_contract([source],definitions,binding['start'],binding['end'],output,index,metadata,binding)
            (output.parent/'analysis.json').write_text('redundant Uroad output')
            cleanup_completed_chunk(chunk,indexed_can=True)
            contract=parquet.load_indexed_contract(output,index,metadata,binding,definitions)
            self.assertTrue(source.is_file())
            self.assertTrue(metadata.is_file())
            self.assertFalse((output.parent/'analysis.json').exists())
            self.assertEqual(sum(sum(1 for _ in s['series']) for s in contract['signals'].values()),1)


if __name__=='__main__':unittest.main()
