#!/usr/bin/env python3
"""Owned AVM/uroad downloader with fixed prod/testtwo environment mapping."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cheyun_prod_client import (
    EEA_PLATFORM,
    UROAD_LOG_CLASS,
    QueryResult,
    download_to_file,
    environment_config,
    load_settings,
    ordered_files,
    query_files,
    safe_filename,
)

DEFAULT_LOG_CLASS = UROAD_LOG_CLASS
DEFAULT_PLATFORM = "user-car"
FILE_PREFIX = "AVM_Service-"
FILE_SUFFIX = ".zst"


def read_username() -> str:
    return load_settings()["username"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    skill_root = Path(__file__).resolve().parents[1]
    if resolved == skill_root or skill_root in resolved.parents:
        raise ValueError(f"禁止将运行产物写入 Skill 源码目录: {resolved}")
    return resolved


def filter_uroad_files(items: list[dict]) -> list[dict]:
    return [
        item for item in items
        if str(item.get("fileName", "")).startswith(FILE_PREFIX)
        and str(item.get("fileName", "")).lower().endswith(FILE_SUFFIX)
    ]


def query_files_for_env(args, token: str, env: str) -> QueryResult:
    return query_files(
        vin=args.vin,
        start=args.start,
        end=args.end,
        log_class=args.log_class,
        username=token,
        page_size=args.page_size,
        eea_platform=args.eea_platform,
        environment=env,
    )


def decompress_zst(source: Path, destination: Path) -> None:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("zstandard 运行依赖不可用；请重新运行主入口以自动修复，若仍失败请联系部署维护人员") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.part")
    try:
        with source.open("rb") as compressed, temporary.open("xb") as output:
            zstd.ZstdDecompressor().copy_stream(compressed, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_manifest(args, run_dir: Path, records: list[dict], extracted: list[str], total: int,
                   selected_count: int, excluded: list[dict], selected_for_download: int,
                   attempts: list[dict], resolved_environment: str, provider_environment: str) -> Path:
    manifest = {
        "schemaVersion": "1.1",
        "runId": args.run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "sourceType": "canlog_api",
        "query": {
            "vin": args.vin,
            "startTime": args.start,
            "endTime": args.end,
            "cloudEnv": resolved_environment,
            "providerEnvironment": provider_environment,
            "platformType": args.platform_type,
            "logClass": args.log_class,
            "eeaPlatform": args.eea_platform,
        },
        "environment": {
            "environment": resolved_environment,
            "providerEnvironment": provider_environment,
        },
        "files": records,
        "coverage": {"requestedStart": args.start, "requestedEnd": args.end, "actualStart": None, "actualEnd": None, "missingSegments": []},
        "extracted": {"directory": "input", "fileCount": len(extracted), "files": extracted, "candidateFiles": extracted, "uroadMatched": bool(extracted), "avmMatched": any("avm" in name.lower() for name in extracted)},
        "provenance": {
            "adapter": "StandaloneCanlogApiAdapter",
            "adapterVersion": "0.3.0",
            "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "queryTotal": total,
            "selectedRecords": selected_count,
            "selectedForDownload": selected_for_download,
            "excludedRecords": len(excluded),
            "excludedFileNames": [item.get("fileName") for item in excluded],
            "listOnly": args.list_only,
            "platformAttempts": attempts,
        },
    }
    output = run_dir / "manifest.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return output


def main() -> int:
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
    args = parser.parse_args()
    try:
        args.log_class = DEFAULT_LOG_CLASS
        if args.page_size <= 0 or (args.max_files is not None and args.max_files < 0):
            raise ValueError("page-size/max-files 参数无效")
        token = read_username()
        run_dir = safe_output_dir(Path(args.output_dir))
        raw_dir, input_dir = run_dir / "raw", run_dir / "input"
        raw_dir.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(parents=True, exist_ok=True)
        args.platform_type = DEFAULT_PLATFORM
        result = query_files_for_env(args, token, args.cloud_env)
        config = environment_config(args.cloud_env)
        attempts = [{
            "platform": DEFAULT_PLATFORM,
            "env": args.cloud_env,
            "providerEnvironment": config["providerEnvironment"],
            "total": result.total,
            "status": "selected" if filter_uroad_files(result.items) else "no_data",
            "downloadHosts": result.download_hosts,
        }]
        selected = ordered_files(filter_uroad_files(result.items))
        excluded = [item for item in result.items if item not in selected]
        if not selected:
            manifest = build_manifest(args, run_dir, [], [], result.total, 0, excluded, 0, attempts,
                                      args.cloud_env, config["providerEnvironment"])
            print(json.dumps({"ok": True, "manifest": str(manifest), "noData": True, "downloadHosts": result.download_hosts}, ensure_ascii=False, indent=2))
            return 0
        selected_for_download = selected if args.max_files is None else selected[:args.max_files]
        if args.list_only:
            manifest = build_manifest(args, run_dir, [], [], result.total, len(selected), excluded, 0, attempts,
                                      args.cloud_env, config["providerEnvironment"])
        else:
            records = []
            for number, item in enumerate(ordered_files(selected_for_download), 1):
                filename = safe_filename(item, number)
                url = item.get("downloadURL") or item.get("downloadUrl") or item.get("url")
                if not filename or not url:
                    raise RuntimeError(f"日志记录缺少文件名或下载地址: {filename}")
                archive = raw_dir / filename
                try:
                    download_to_file(str(url), archive, environment=args.cloud_env)
                    digest, size = sha256(archive), archive.stat().st_size
                    decompressed = input_dir / filename[:-4]
                    decompress_zst(archive, decompressed)
                    records.append({
                        "name": filename,
                        "inputPath": decompressed.relative_to(run_dir).as_posix(),
                        "sizeBytes": size,
                        "sha256": digest,
                        "status": "ingested",
                        "source": f"owned_{args.cloud_env}_client",
                        "fileKey": item.get("fileKey"),
                        "collectTime": item.get("collectTime"),
                        "receiveTime": item.get("receiveTime"),
                        "fileSize": item.get("fileSize"),
                        "logType": item.get("logType"),
                    })
                finally:
                    archive.unlink(missing_ok=True)
                    archive.with_name(archive.name + ".part").unlink(missing_ok=True)
            extracted = [path.relative_to(input_dir).as_posix() for path in sorted(input_dir.rglob("*")) if path.is_file()]
            manifest = build_manifest(args, run_dir, records, extracted, result.total, len(selected), excluded,
                                      len(selected_for_download), attempts, args.cloud_env, config["providerEnvironment"])
        print(json.dumps({"ok": True, "manifest": str(manifest), "downloadHosts": result.download_hosts}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
