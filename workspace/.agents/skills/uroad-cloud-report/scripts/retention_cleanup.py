#!/usr/bin/env python3
"""Apply bounded 14-day retention to complete Uroad job directories."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_WORKSPACE = Path(os.environ.get("CC_CONNECT_WORKSPACE", "").strip()).expanduser() if os.environ.get("CC_CONNECT_WORKSPACE", "").strip() else Path(__file__).resolve().parents[4]
RUNS_DIRECTORY = Path("outputs") / "uroad-report-runs"
RUN_NAME = re.compile(r"^run-\d{8}-\d{6}-\d{6}-[0-9a-f]{8}$")
RETENTION_DAYS_ENV = "UROAD_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 14


def retention_seconds() -> int:
    raw = os.environ.get(RETENTION_DAYS_ENV, str(DEFAULT_RETENTION_DAYS)).strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError(f"{RETENTION_DAYS_ENV} 必须是正整数") from exc
    if days < 1:
        raise ValueError(f"{RETENTION_DAYS_ENV} 必须大于 0")
    return days * 24 * 60 * 60


def _safe_run_dirs(runs_root: Path) -> list[tuple[Path, Path, dict]]:
    records = []
    if not runs_root.exists() or runs_root.is_symlink() or not runs_root.is_dir():
        return records
    resolved_root = runs_root.resolve(strict=True)
    for run_dir in runs_root.iterdir():
        if not RUN_NAME.fullmatch(run_dir.name) or run_dir.is_symlink() or not run_dir.is_dir():
            continue
        try:
            if run_dir.resolve(strict=True).parent != resolved_root:
                continue
            run_json = run_dir / "run.json"
            if run_json.is_symlink() or not run_json.is_file():
                continue
            payload = json.loads(run_json.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                records.append((run_dir, run_json, payload))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _age_basis(run_dir: Path, run_json: Path, payload: dict) -> float:
    completed_at = payload.get("completedAt")
    if isinstance(completed_at, str):
        try:
            return datetime.fromisoformat(completed_at).timestamp()
        except ValueError:
            pass
    return run_json.stat().st_mtime


def apply_retention(workspace: Path, now: float | None = None) -> dict:
    workspace = workspace.resolve(strict=True)
    runs_root = workspace / RUNS_DIRECTORY
    now = time.time() if now is None else now
    ttl = retention_seconds()
    removed, kept, skipped, errors = [], [], [], []
    for run_dir, run_json, payload in _safe_run_dirs(runs_root):
        status = str(payload.get("status") or "")
        if status in {"queued", "claimed", "running"}:
            kept.append(run_dir.name)
            continue
        try:
            age = max(0.0, now - _age_basis(run_dir, run_json, payload))
            if age < ttl:
                kept.append(run_dir.name)
                continue
            if run_dir.resolve(strict=True).parent != runs_root.resolve(strict=True) or run_dir.is_symlink():
                skipped.append(run_dir.name)
                continue
            shutil.rmtree(run_dir)
            removed.append(run_dir.name)
        except OSError as exc:
            errors.append({"run": run_dir.name, "error": str(exc)})
    return {
        "ok": not errors,
        "retentionDays": ttl // 86400,
        "removedRuns": removed,
        "keptRuns": kept,
        "skipped": skipped,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args()
    workspace = args.workspace or Path(os.environ.get("CC_CONNECT_WORKSPACE", "").strip() or DEFAULT_WORKSPACE)
    try:
        result = apply_retention(workspace)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())