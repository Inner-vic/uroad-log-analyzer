#!/usr/bin/env python3
"""Apply bounded retention to completed and failed uroad report runs."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

DEFAULT_WORKSPACE = Path("/workspace")
RUNS_DIRECTORY = Path("outputs") / "uroad-report-runs"
RUN_NAME = re.compile(r"^run-\d{8}-\d{6}-\d{6}-[0-9a-f]{8}$")
DIAGNOSTIC_TTL_SECONDS = 24 * 60 * 60
SUCCESS_TTL_SECONDS = 7 * 24 * 60 * 60
FAILED_TTL_SECONDS = 3 * 24 * 60 * 60
MAX_SUCCESS_RUNS = 20
RETRY_WINDOW_SECONDS = 75 * 60
MAX_RETRY_ATTEMPTS = 5
DIAGNOSTIC_PATHS = ("manifest.json", "analysis/analysis.json", "analysis/can-signals.json")


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".retention.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _safe_run_dirs(runs_root: Path) -> list[tuple[Path, Path, dict]]:
    result = []
    if not runs_root.exists():
        return result
    resolved_root = runs_root.resolve(strict=True)
    if runs_root.is_symlink() or not runs_root.is_dir():
        return result
    for run_dir in runs_root.iterdir():
        if not RUN_NAME.fullmatch(run_dir.name) or run_dir.is_symlink() or not run_dir.is_dir():
            continue
        try:
            resolved_run = run_dir.resolve(strict=True)
            if resolved_run.parent != resolved_root:
                continue
            run_json = run_dir / "run.json"
            if run_json.is_symlink() or not run_json.is_file():
                continue
            payload = json.loads(run_json.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            result.append((run_dir, run_json, payload))
        except (OSError, json.JSONDecodeError):
            continue
    return result


def _age_basis(run_dir: Path, run_json: Path, status: str) -> float:
    if status == "completed":
        report = next((path for path in (run_dir / "report").glob("*.html") if path.is_file() and not path.is_symlink()), None) if (run_dir / "report").is_dir() else None
        if report is not None:
            return report.stat().st_mtime
    return run_json.stat().st_mtime


def apply_retention(workspace: Path, now: float | None = None) -> dict:
    workspace = workspace.resolve(strict=True)
    runs_root = workspace / RUNS_DIRECTORY
    now = time.time() if now is None else now
    records = _safe_run_dirs(runs_root)
    completed = []
    for run_dir, run_json, payload in records:
        status = str(payload.get("status") or "")
        try:
            basis = _age_basis(run_dir, run_json, status)
        except OSError:
            continue
        if status == "completed":
            completed.append((basis, run_dir, run_json, payload))

    completed.sort(key=lambda item: item[0], reverse=True)
    beyond_limit = {item[1] for item in completed[MAX_SUCCESS_RUNS:]}
    removed_runs, removed_diagnostics, skipped, errors = [], [], [], []

    for run_dir, run_json, payload in records:
        status = str(payload.get("status") or "")
        try:
            age = max(0.0, now - _age_basis(run_dir, run_json, status))
            delivery = payload.get("delivery") or {}
            notification = payload.get("notification") or {}
            # Only live execution is unconditionally protected. Delivery/notification
            # protection is bounded so a missing consumer cannot leak runs forever.
            protected = status in {"queued", "running", "claimed"}
            for state, record, failed_state in (("delivery", delivery, "delivery_failed"), ("notification", notification, "notification_failed")):
                if record.get("status") in ({"pending", "uploading"} if state == "delivery" else {"pending"}):
                    created = record.get("createdAt") or payload.get("createdAt")
                    attempts = int(record.get("attemptCount", record.get("attempts", 0)) or 0)
                    try:
                        retry_age = now - __import__('datetime').datetime.fromisoformat(str(created)).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        retry_age = RETRY_WINDOW_SECONDS + 1
                    if attempts >= MAX_RETRY_ATTEMPTS or retry_age >= RETRY_WINDOW_SECONDS:
                        record["status"] = failed_state
                        record["lastError"] = record.get("lastError") or "重试期限已过"
                        _atomic_json(run_json, payload)
                    else:
                        protected = True
            remove_run = (not protected and ((status == "completed" and (age >= SUCCESS_TTL_SECONDS or run_dir in beyond_limit))
                          or (status in {"failed", "interrupted", "system_error", "cancelled"} and age >= FAILED_TTL_SECONDS)))
            if remove_run:
                resolved_root = runs_root.resolve(strict=True)
                if run_dir.resolve(strict=True).parent != resolved_root or run_dir.is_symlink():
                    skipped.append(run_dir.name)
                    continue
                shutil.rmtree(run_dir)
                removed_runs.append(run_dir.name)
                continue
            if status != "completed" or age < DIAGNOSTIC_TTL_SECONDS:
                skipped.append(run_dir.name)
                continue
            removed = []
            resolved_run = run_dir.resolve(strict=True)
            for relative in DIAGNOSTIC_PATHS:
                target = run_dir / relative
                if target.is_symlink() or not target.is_file():
                    continue
                if resolved_run not in target.resolve(strict=True).parents:
                    continue
                target.unlink()
                removed.append(relative)
            analysis_dir = run_dir / "analysis"
            if analysis_dir.is_dir() and not analysis_dir.is_symlink() and not any(analysis_dir.iterdir()):
                analysis_dir.rmdir()
            if removed:
                artifacts = payload.get("artifacts")
                if isinstance(artifacts, dict):
                    for key in ("manifest", "analysis", "canSignals"):
                        artifacts.pop(key, None)
                payload["retention"] = {"diagnosticsRemoved": removed, "policy": "diagnostics-24h; success-7d-or-latest-20; failed-3d"}
                _atomic_json(run_json, payload)
                removed_diagnostics.append({"run": run_dir.name, "files": removed})
        except OSError as exc:
            errors.append({"run": run_dir.name, "error": str(exc)})
    return {"ok": not errors, "removedRuns": removed_runs, "removedDiagnostics": removed_diagnostics,
            "skipped": skipped, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args()
    workspace = args.workspace or Path(os.environ.get("OPENCLAW_WORKSPACE", "").strip() or DEFAULT_WORKSPACE)
    try:
        result = apply_retention(workspace)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
