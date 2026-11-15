#!/usr/bin/env python3
"""Query CheYun CAN Parquet files online, download them, and merge into ASC."""
from __future__ import annotations
import argparse, json, shutil, tempfile
from pathlib import Path
from parquet_to_asc import normalize_rows, read_parquet, rows_to_asc
from cheyun_prod_client import CAN_LOG_CLASS, download_to_file, load_settings, query_files, safe_filename


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vin', required=True); p.add_argument('--start', required=True); p.add_argument('--end', required=True)
    p.add_argument('--asc', required=True); p.add_argument('--cloud-env', choices=('prod','test'), default='prod')
    p.add_argument('--epoch-unit', choices=('auto','s','ms','us'), default='auto')
    args = p.parse_args()
    try:
        result = query_files(vin=args.vin, start=args.start, end=args.end, log_class=CAN_LOG_CLASS,
                             username=load_settings()['username'], environment=args.cloud_env)
        if not result.items: raise RuntimeError(f'{args.cloud_env} 环境没有 CAN Parquet 数据')
        with tempfile.TemporaryDirectory(prefix='uroad-parquet-') as temp:
            root = Path(temp); all_rows = []
            for i, item in enumerate(result.items, 1):
                name = safe_filename(item, i)
                if not name.lower().endswith(('.parquet', '.pq')):
                    raise RuntimeError(f'在线 CAN 文件不是 Parquet: {name}')
                url = item.get('downloadURL') or item.get('downloadUrl') or item.get('url')
                path = root / name
                download_to_file(str(url), path, environment=args.cloud_env)
                all_rows.extend(normalize_rows(read_parquet(path), epoch_unit=args.epoch_unit))
            all_rows.sort(key=lambda x: x['epoch'])
            output = Path(args.asc).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rows_to_asc(all_rows), encoding='utf-8')
        print(json.dumps({'ok': True, 'vin': args.vin, 'environment': args.cloud_env,
                          'files': len(result.items), 'rows': len(all_rows), 'output': str(output)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1

if __name__ == '__main__': raise SystemExit(main())
