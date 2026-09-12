#!/usr/bin/env python3
"""Thin uroad wrapper around the CheYun downloader."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def locate_downloader() -> Path:
    return (Path(__file__).resolve().with_name("cheyun_api_adapter.py")).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--eea-platform", choices=("EEA3.0",), default="EEA3.0")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--cloud-env", choices=("prod", "test"), default="prod")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        downloader = locate_downloader()
        command = [
            sys.executable,
            str(downloader),
            "--vin", args.vin,
            "--start", args.start,
            "--end", args.end,
            "--eea-platform", args.eea_platform,
            "--page-size", str(args.page_size),
            "--output-dir", args.output_dir,
            "--cloud-env", args.cloud_env,
        ]
        if args.max_files is not None:
            command.extend(["--max-files", str(args.max_files)])
        if args.list_only:
            command.append("--list-only")
        if args.run_id:
            command.extend(["--run-id", args.run_id])
        return subprocess.run(command, check=False).returncode
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"uroad 下载适配器失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
