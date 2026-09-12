import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
from validate_report_artifact import validate_attachment_path


def test_internal_files_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_WORKSPACE', str(tmp_path))
    vin = 'DEM0VEH1CLE000001'
    start, end = '2026-09-04 11:20:00', '2026-09-04 11:20:59'
    run = tmp_path / 'outputs' / 'uroad-report-runs' / 'run-20260904-112000-000000-abcdef12'
    report = run / 'report' / f'uroad-report_{vin}_20260904-112000_20260904-112059.html'
    report.parent.mkdir(parents=True)
    report.write_text('<!doctype html><html><style></style><script>const data=JSON.parse("{}");</script></html>', encoding='utf-8')
    payload = {'status':'completed','input':{'vin':vin,'start':start,'end':end},'artifacts':{'report':str(report)}}
    run_json = run / 'run.json'
    run_json.write_text(json.dumps(payload), encoding='utf-8')
    assert validate_attachment_path(run_json, report)['ok']
    for internal in ('run.json', 'manifest.json', 'analysis.json', 'can-signals.json'):
        candidate = run / internal
        candidate.write_text('{}', encoding='utf-8')
        try:
            validate_attachment_path(run_json, candidate)
        except ValueError:
            pass
        else:
            raise AssertionError(internal)
