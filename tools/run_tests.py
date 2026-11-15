"""Offline test groups with isolated module namespaces and synthetic task data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'workspace/.agents/skills/uroad-cloud-report'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('full','portable'), default='full')
    args = parser.parse_args()
    suites = ([('root',SKILL,'test_*.py'),('contracts',SKILL/'tests','test_*.py')]
              if args.suite == 'full' else [(name,SKILL,name) for name in
                  ('test_parse_time_input.py','test_natural_time_input.py','test_ccconnect_request_context.py','test_request_guard.py')])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PYTHONPATH=str(SKILL/'scripts'), CC_CONNECT_WORKSPACE=str(ROOT/'workspace'),
               UROAD_TRIGGER_WORKER='0')
    for name, directory, pattern in suites:
        result = subprocess.run([sys.executable,'-B','-X','utf8','-m','unittest','discover',
                                 '-s',str(directory),'-p',pattern],cwd=ROOT,env=env)
        if result.returncode:
            print(json.dumps({'status':'failed','group':name}))
            return result.returncode
    print(json.dumps({'status':'passed','suite':args.suite,'groups':len(suites)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
