#!/usr/bin/env python3
"""Import local AVM/uroad logs and generate manifest.json.

Cloud acquisition is intentionally handled by the CANLog-derived
cheyun_uroad_adapter.py. This script is the local-import half of the
uroad-cloud-report Skill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED_SUFFIXES = {".log", ".txt", ".zst", ".hzst", ".zip", ".gz"}


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


def safe_name(path: Path) -> str:
    return path.name.replace("\\", "_").replace("/", "_")


def collect_inputs(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(
            item for item in source.rglob("*")
            if item.is_file() and (item.suffix.lower() in SUPPORTED_SUFFIXES or not item.suffix)
        )
    raise FileNotFoundError(f"输入不存在: {source}")


def safe_extract_zip(archive: Path, destination: Path) -> list[str]:
    destination = destination.resolve()
    extracted: list[str] = []
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            if member.is_dir():
                continue
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"ZIP 包含不安全路径: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            extracted.append(str(target.relative_to(destination)).replace(os.sep, "/"))
    return extracted


def decompress_zst(source: Path, destination: Path) -> None:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("zstandard 运行依赖不可用；请重新运行主入口以自动修复，若仍失败请联系部署维护人员") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as compressed, destination.open("wb") as output:
        zstd.ZstdDecompressor().copy_stream(compressed, output)


def import_source(source: Path, run_dir: Path) -> tuple[list[dict], list[str]]:
    raw_dir = run_dir / "raw"
    input_dir = run_dir / "input"
    records: list[dict] = []
    extracted: list[str] = []
    for item in collect_inputs(source):
        target = raw_dir / safe_name(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.resolve() != target.resolve():
            shutil.copy2(item, target)
        records.append({
            "name": target.name,
            "path": str(target.relative_to(run_dir)).replace(os.sep, "/"),
            "sizeBytes": target.stat().st_size,
            "sha256": sha256(target),
            "status": "available",
            "source": "local",
        })
        suffix = target.suffix.lower()
        if suffix == ".zip":
            extracted.extend(safe_extract_zip(target, input_dir))
        elif suffix in {".zst", ".hzst"}:
            destination = input_dir / target.name.rsplit(".", 1)[0]
            decompress_zst(target, destination)
            extracted.append(str(destination.relative_to(input_dir)).replace(os.sep, "/"))
        else:
            destination = input_dir / target.name
            shutil.copy2(target, destination)
            extracted.append(destination.name)
    return records, extracted


def build_manifest(args, run_dir: Path, records: list[dict], extracted: list[str]) -> Path:
    manifest = {
        "schemaVersion": "1.0",
        "runId": args.run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "sourceType": "local",
        "query": {"vin": args.vin, "startTime": args.start, "endTime": args.end},
        "files": records,
        "coverage": {"requestedStart": args.start, "requestedEnd": args.end, "actualStart": None, "actualEnd": None, "missingSegments": []},
        "extracted": {
            "directory": "input",
            "fileCount": len(extracted),
            "files": extracted,
            "candidateFiles": extracted,
            "uroadMatched": any("uroad" in item.lower() for item in extracted),
            "avmMatched": any("avm" in item.lower() for item in extracted),
        },
        "provenance": {
            "adapter": "LocalImportAdapter",
            "adapterVersion": "0.7.0",
            "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        },
    }
    output = run_dir / "manifest.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local", help="固定使用 local 子命令")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--vin")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    if args.local != "local":
        parser.error("本入口只支持 local；云端请使用 cheyun_uroad_adapter.py")
    try:
        run_dir = safe_output_dir(Path(args.output_dir))
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        (run_dir / "input").mkdir(parents=True, exist_ok=True)
        records, extracted = import_source(Path(args.input).resolve(), run_dir)
        manifest = build_manifest(args, run_dir, records, extracted)
        print(json.dumps({"ok": True, "manifest": str(manifest)}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
