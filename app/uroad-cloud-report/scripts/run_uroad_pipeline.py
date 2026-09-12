#!/usr/bin/env python3
"""Run uroad acquisition, analysis, CAN-signal parsing, and reporting."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from task_registry import BusyTaskError, DuplicateTaskError, acquire_run_slot, load_request_metadata, mark_dequeued, mark_finished, register_or_reuse, runtime_version

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CANLOG_UROAD_DOWNLOAD = SCRIPTS / "cheyun_uroad_adapter.py"
CAN_SIGNAL_STREAM = SCRIPTS / "stream_can_signals.py"
ANALYSIS = SCRIPTS / "parse_uroad.py"
HTML_REPORT = SCRIPTS / "build_html_report.py"
DEFAULT_WORKSPACE = Path("/workspace")
RUNS_DIRECTORY = Path("outputs") / "uroad-report-runs"
DEPENDENCY_VALIDATION_TIMEOUT_SECONDS = 30
VENDOR_ROOT = SKILL_ROOT / "vendor"
VENDOR_MANIFEST = VENDOR_ROOT / "manifest.json"
VENDOR_MANIFEST_SHA256 = "b7aabc4eb67da5e3238127181d5c818484542c40e36140ccc7ad56e37f8473a4"
ZSTANDARD_VERSION = "0.25.0"
VENDOR_SOURCE = "https://pypi.org/project/zstandard/0.25.0/"
VENDOR_SUPPORTED = "CPython 3.9-3.14, Linux x86-64, glibc (manylinux_2_17)"
VENDOR_TAGS = ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def cleanup_intermediate(run_dir: Path) -> tuple[list[str], list[str]]:
    removed, errors = [], []
    for name in ("raw", "input", "canlog", "slices", "merge-state"):
        target = run_dir / name
        if target.exists():
            try:
                shutil.rmtree(target)
                removed.append(name)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
    return removed, errors


def build_report_filename(vin: str, start: str, end: str) -> str:
    start_stamp = datetime.strptime(start, TIME_FORMAT).strftime("%Y%m%d-%H%M%S")
    end_stamp = datetime.strptime(end, TIME_FORMAT).strftime("%Y%m%d-%H%M%S")
    return f"uroad-report_{vin.upper()}_{start_stamp}_{end_stamp}.html"


def cleanup_report_output(run_dir: Path, report_filename: str) -> None:
    report_dir = run_dir / "report"
    report_path = report_dir / report_filename
    for target in (report_path, report_path.with_suffix(report_path.suffix + ".tmp")):
        target.unlink(missing_ok=True)
    if report_dir.is_dir() and not any(report_dir.iterdir()):
        report_dir.rmdir()


def validate_html_report(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    lowered = content.lower()
    required = ("<!doctype html", "<html", "</html>", "<style", "</style>", "<script", "</script>", 'id="report-data"', "const data=json.parse")
    missing = [marker for marker in required if marker not in lowered]
    if missing:
        raise RuntimeError("HTML 报告结构不完整: " + ", ".join(missing))


def validate_inputs(args: argparse.Namespace) -> None:
    vin = args.vin.strip()
    if not VIN_PATTERN.fullmatch(vin):
        raise ValueError("请提供有效的 17 位 VIN（仅使用字母和数字，不含 I、O、Q）")
    try:
        start = datetime.strptime(args.start, TIME_FORMAT)
        end = datetime.strptime(args.end, TIME_FORMAT)
    except ValueError as exc:
        raise ValueError("请将开始和结束时间填写为 YYYY-MM-DD HH:MM:SS 格式") from exc
    if start >= end:
        raise ValueError("请确认开始时间早于结束时间")
    if args.page_size <= 0:
        raise ValueError("请将 page-size 设置为大于 0 的整数")
    if args.max_files is not None and args.max_files <= 0:
        raise ValueError("请将 max-files 设置为大于 0 的整数，或不提供该选项")
    args.vin = vin.upper()


def validate_workspace() -> Path:
    configured_workspace = os.environ.get("OPENCLAW_WORKSPACE", "").strip()
    workspace = Path(configured_workspace) if configured_workspace else DEFAULT_WORKSPACE
    try:
        workspace = workspace.resolve(strict=True)
    except OSError as exc:
        hint = "OPENCLAW_WORKSPACE" if configured_workspace else str(DEFAULT_WORKSPACE)
        raise ValueError(f"OpenClaw 工作区不可用，请检查 {hint}: {workspace}") from exc
    if not workspace.is_dir():
        hint = "OPENCLAW_WORKSPACE" if configured_workspace else str(DEFAULT_WORKSPACE)
        raise ValueError(f"OpenClaw 工作区不是目录，请检查 {hint}: {workspace}")
    if workspace == SKILL_ROOT or SKILL_ROOT in workspace.parents:
        raise ValueError(f"OpenClaw 工作区不能位于 Skill 源码目录内: {workspace}")
    return workspace


def _activate_python_path(directory: Path) -> None:
    value = str(directory)
    if value not in sys.path:
        sys.path.insert(0, value)
    current = os.environ.get("PYTHONPATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if value not in entries:
        os.environ["PYTHONPATH"] = os.pathsep.join([value, *entries])


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _probe_zstandard(directory: Path | None = None) -> bool:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    expected = ""
    if directory is not None:
        expected = str(directory.resolve(strict=True))
    probe = (
        "import json,pathlib,sys; "
        f"expected={expected!r}; "
        "sys.path.insert(0,expected) if expected else None; "
        "import zstandard,zstandard.backend_c as backend; "
        "v=str(getattr(zstandard,'__version__','')); "
        f"ok=(v == {ZSTANDARD_VERSION!r}); "
        "origin=str(pathlib.Path(zstandard.__file__).resolve()); "
        "backend_origin=str(pathlib.Path(backend.__file__).resolve()); "
        "inside=(not expected) or (pathlib.Path(origin).is_relative_to(pathlib.Path(expected)) and "
        "pathlib.Path(backend_origin).is_relative_to(pathlib.Path(expected))); "
        "data=b'uroad-offline-zstandard-runtime-check'*17; "
        "roundtrip=zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(data)) == data; "
        "print(json.dumps({'ok':bool(ok and inside and roundtrip),'version':v,'origin':origin,'backend_origin':backend_origin}))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe], check=True, timeout=DEPENDENCY_VALIDATION_TIMEOUT_SECONDS,
            env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        payload = json.loads((completed.stdout or "").strip())
        return payload.get("ok") is True
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _zstandard_available(directory: Path | None = None) -> bool:
    try:
        return _probe_zstandard(directory)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _glibc_identity() -> tuple[str, str]:
    libc_name, libc_version = platform.libc_ver()
    if libc_name.lower() == "glibc" and libc_version:
        return "glibc", libc_version
    try:
        configured = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        configured = ""
    match = re.fullmatch(r"glibc\s+([0-9]+(?:\.[0-9]+)+)", configured.strip(), re.IGNORECASE)
    if match:
        return "glibc", match.group(1)
    try:
        function = ctypes.CDLL(None).gnu_get_libc_version
        function.restype = ctypes.c_char_p
        value = function()
        if value:
            return "glibc", value.decode("ascii")
    except (AttributeError, OSError, TypeError, UnicodeDecodeError):
        pass
    return (libc_name or "unknown"), (libc_version or "unknown")


def _runtime_identity() -> str:
    libc_name, libc_version = _glibc_identity()
    return (
        f"implementation={sys.implementation.name}, python={platform.python_version()}, "
        f"system={platform.system()}, machine={platform.machine()}, libc={libc_name or 'unknown'}-{libc_version or 'unknown'}"
    )


def _selected_vendor_tag() -> str:
    version = sys.version_info[:2]
    libc_name, libc_version = _glibc_identity()
    libc_numbers = tuple(int(value) for value in re.findall(r"\d+", libc_version)[:2])
    supported = "CPython 3.9-3.14 / Linux x86-64 / glibc (manylinux_2_17)"
    if (
        sys.implementation.name != "cpython"
        or version < (3, 9) or version > (3, 14)
        or platform.system() != "Linux"
        or platform.machine().lower() not in {"x86_64", "amd64"}
        or libc_name.lower() != "glibc"
        or len(libc_numbers) < 2 or libc_numbers < (2, 17)
    ):
        raise RuntimeError(f"当前运行环境不受支持：{_runtime_identity()}；支持范围：{supported}")
    return f"cp{version[0]}{version[1]}"


def _load_verified_manifest() -> dict:
    if not VENDOR_MANIFEST.is_file() or _sha256(VENDOR_MANIFEST) != VENDOR_MANIFEST_SHA256:
        raise RuntimeError("离线 zstandard 清单缺失或完整性校验失败；已停止运行")
    try:
        manifest = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("离线 zstandard 清单不可读；已停止运行") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("离线 zstandard 清单格式无效；已停止运行")
    required_root = {"schema_version", "package", "version", "source", "license", "supported", "runtimes"}
    license_record = manifest.get("license")
    if (
        set(manifest) != required_root
        or manifest.get("schema_version") != 1
        or manifest.get("package") != "zstandard"
        or manifest.get("version") != ZSTANDARD_VERSION
        or manifest.get("source") != VENDOR_SOURCE
        or manifest.get("supported") != VENDOR_SUPPORTED
        or not isinstance(license_record, dict) or set(license_record) != {"path", "sha256"}
        or license_record.get("path") != "LICENSE.zstandard.txt"
        or not re.fullmatch(r"[0-9a-f]{64}", str(license_record.get("sha256", "")))
        or not isinstance(manifest.get("runtimes"), list)
    ):
        raise RuntimeError("离线 zstandard 来源或许可证完整性校验失败；已停止运行")
    license_path = VENDOR_ROOT / license_record["path"]
    if not license_path.is_file() or _sha256(license_path) != license_record["sha256"]:
        raise RuntimeError("离线 zstandard 来源或许可证完整性校验失败；已停止运行")
    wheel_hash = re.compile(r"^[0-9a-f]{64}$")
    seen_tags = set()
    for record in manifest.get("runtimes", []):
        if not isinstance(record, dict) or set(record) != {"python_tag", "abi_tag", "platform", "runtime", "wheel", "wheel_sha256", "files"}:
            raise RuntimeError("离线 zstandard 运行时清单格式无效；已停止运行")
        tag = str(record.get("python_tag", ""))
        wheel = str(record.get("wheel", ""))
        wheel_pattern = rf"^zstandard-0\.25\.0-{re.escape(tag)}-{re.escape(tag)}-manylinux2014_x86_64\.manylinux_2_17_x86_64(?:\.manylinux_2_28_x86_64)?\.whl$"
        files = record.get("files")
        file_paths = set()
        if (
            tag not in VENDOR_TAGS or tag in seen_tags
            or record.get("abi_tag") != tag
            or record.get("platform") != "linux_x86_64_glibc_manylinux_2_17"
            or record.get("runtime") != f"runtimes/{tag}"
            or not re.fullmatch(wheel_pattern, wheel)
            or not wheel_hash.fullmatch(str(record.get("wheel_sha256", "")))
            or not isinstance(files, list) or not files
        ):
            raise RuntimeError("离线 zstandard wheel 来源元数据校验失败；已停止运行")
        seen_tags.add(tag)
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
                raise RuntimeError("离线 zstandard 文件清单格式无效；已停止运行")
            value = item.get("path")
            pure = PurePosixPath(value) if isinstance(value, str) else PurePosixPath("/")
            if (
                not isinstance(value, str) or not value or pure.is_absolute()
                or any(part in {"", ".", ".."} for part in pure.parts)
                or value in file_paths or not wheel_hash.fullmatch(str(item.get("sha256", "")))
                or not isinstance(item.get("size"), int) or item["size"] < 0
            ):
                raise RuntimeError("离线 zstandard 文件清单路径或哈希无效；已停止运行")
            file_paths.add(value)
    if seen_tags != set(VENDOR_TAGS):
        raise RuntimeError("离线 zstandard 支持矩阵不完整；已停止运行")
    return manifest


def _verify_vendor_runtime(record: dict) -> Path:
    runtime = (VENDOR_ROOT / str(record.get("runtime", ""))).resolve(strict=True)
    vendor = VENDOR_ROOT.resolve(strict=True)
    if vendor not in runtime.parents or _is_link_or_junction(runtime):
        raise RuntimeError("离线 zstandard 运行目录来源异常；已停止运行")
    expected = {item["path"]: item for item in record.get("files", [])}
    actual = {}
    for path in runtime.rglob("*"):
        if _is_link_or_junction(path):
            raise RuntimeError(f"离线 zstandard 包含链接文件：{path.name}；已停止运行")
        if path.is_file():
            actual[path.relative_to(runtime).as_posix()] = path
    if set(actual) != set(expected):
        raise RuntimeError("离线 zstandard 文件集合完整性校验失败；已停止运行")
    for relative, path in actual.items():
        item = expected[relative]
        if path.stat().st_size != item.get("size") or _sha256(path) != item.get("sha256"):
            raise RuntimeError(f"离线 zstandard 文件完整性校验失败：{relative}；已停止运行")
    if not _probe_zstandard(runtime):
        raise RuntimeError("离线 zstandard 模块来源、版本或压缩往返校验失败；已停止运行")
    return runtime


def bootstrap_dependencies(workspace: Path) -> Path | None:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise RuntimeError(f"OpenClaw 工作区不是目录: {workspace}")
    tag = _selected_vendor_tag()
    manifest = _load_verified_manifest()
    if _zstandard_available():
        return None
    matches = [item for item in manifest.get("runtimes", []) if item.get("python_tag") == tag and item.get("abi_tag") == tag]
    if len(matches) != 1:
        raise RuntimeError(f"未找到匹配的离线 zstandard 运行时：{_runtime_identity()}")
    runtime = _verify_vendor_runtime(matches[0])
    _activate_python_path(runtime)
    return runtime


def allocate_run_directory(workspace: Path | None = None, existing_run_id: str | None = None) -> Path:
    workspace = workspace or validate_workspace()
    outputs_root = workspace / RUNS_DIRECTORY.parent
    runs_root = workspace / RUNS_DIRECTORY
    try:
        outputs_is_junction = getattr(outputs_root, "is_junction", lambda: False)
        if outputs_root.exists() and (outputs_root.is_symlink() or outputs_is_junction()):
            raise ValueError(f"报告输出目录不能是符号链接或目录联接: {outputs_root}")
        outputs_root.mkdir(exist_ok=True)
        if not outputs_root.is_dir():
            raise ValueError(f"报告输出目录不是有效目录: {outputs_root}")
        resolved_outputs_root = outputs_root.resolve(strict=True)
        if resolved_outputs_root.parent != workspace:
            raise ValueError(f"报告输出目录解析后超出 OpenClaw 工作区: {outputs_root}")
        is_junction = getattr(runs_root, "is_junction", lambda: False)
        if runs_root.exists() and (runs_root.is_symlink() or is_junction()):
            raise ValueError(f"报告运行目录不能是符号链接或目录联接: {runs_root}")
        runs_root.mkdir(parents=False, exist_ok=True)
        if not runs_root.is_dir():
            raise ValueError(f"报告运行目录不是有效目录: {runs_root}")
        resolved_runs_root = runs_root.resolve(strict=True)
        expected_runs_root = (workspace / RUNS_DIRECTORY).resolve(strict=True)
        if resolved_runs_root != expected_runs_root or workspace not in resolved_runs_root.parents:
            raise ValueError(f"报告运行目录解析后超出 OpenClaw 工作区: {runs_root}")
        if existing_run_id:
            run_dir = runs_root / existing_run_id
            run_dir.mkdir(exist_ok=True)
            return run_dir
        for _ in range(10):
            run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}"
            run_dir = runs_root / run_id
            try:
                run_dir.mkdir()
                return run_dir
            except FileExistsError:
                continue
    except OSError as exc:
        raise ValueError(f"无法在 OpenClaw 工作区创建报告目录，请检查目录权限: {workspace}") from exc
    raise RuntimeError("无法分配唯一报告目录，请稍后重试")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", required=True, help="车辆 VIN")
    parser.add_argument("--start", required=True, help="开始时间，格式 YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", required=True, help="结束时间，格式 YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--eea-platform", choices=("EEA3.0",), default="EEA3.0")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--cloud-env", choices=("auto", "prod", "test"), default="auto")
    parser.add_argument("--keep-on-success", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--existing-run-id", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        validate_inputs(args)
        from cheyun_prod_client import CloudNoDataError, CAN_LOG_CLASS, UROAD_LOG_CLASS, load_settings, provider_environment, query_files
        settings = load_settings()
        workspace = validate_workspace()
        bootstrap_dependencies(workspace)
        try:
            from retention_cleanup import apply_retention
            retention = apply_retention(workspace)
        except (OSError, RuntimeError, ValueError) as cleanup_exc:
            retention = {"ok": False, "removedRuns": [], "removedDiagnostics": [], "skipped": [], "errors": [{"run": "housekeeping", "error": str(cleanup_exc)}]}
        run_dir = None
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    metadata = load_request_metadata()
    run_id = args.existing_run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}"
    if not args.existing_run_id:
        try:
            task_record = register_or_reuse(run_id, args.vin, args.start, args.end, args.cloud_env, metadata=metadata)
        except DuplicateTaskError as exc:
            payload = {"ok": False, "duplicate": True, "reusable": exc.reusable, "existing": exc.task, "error": "已存在相同请求的任务，请优先复用现有结果"}
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
    else:
        task_record = metadata.get("taskRecord") or {"runId": args.existing_run_id, "status": "queued"}
    try:
        run_dir = allocate_run_directory(workspace, existing_run_id=run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        if not args.existing_run_id:
            try:
                mark_finished(run_id, "system_error", error=str(exc))
            except Exception:
                pass
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    run = {
        "schemaVersion": "1.2",
        "runId": args.existing_run_id or run_id,
        "status": "queued",
        "input": vars(args),
        "steps": {"download": "pending", "canlog": "pending", "canSignals": "pending", "analysis": "pending", "report": "pending"},
        "artifacts": {},
        "error": None,
        "housekeeping": retention,
        **runtime_version(),
        "request": (task_record.get("request") if isinstance(task_record, dict) else {}),
    }
    run_path = run_dir / "run.json"
    save(run_path, run)

    try:
        run["task"] = task_record
        save(run_path, run)

        def requeue_busy_task(active: dict) -> int:
            mark_dequeued(run_id)
            run["status"] = "queued"
            run["task"] = metadata.get("taskRecord") or {"runId": run_id, "status": "queued"}
            run["queue"] = {"reason": "busy", "active": active}
            save(run_path, run)
            print(json.dumps({"ok": False, "queued": True, "busy": True, "active": active, "run": str(run_path), "error": "已有任务在运行，当前任务保持排队"}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 3

        try:
            with acquire_run_slot(run_id) as active_task:
                run["status"] = "running"
                run["task"] = active_task
                save(run_path, run)

                def preflight_environment(env: str) -> dict:
                    uroad = query_files(vin=args.vin, start=args.start, end=args.end, log_class=UROAD_LOG_CLASS, username=settings["username"], page_size=args.page_size, eea_platform=args.eea_platform, environment=env)
                    if not uroad.items:
                        raise CloudNoDataError(f"{env} 环境没有 uroad 数据")
                    can = query_files(vin=args.vin, start=args.start, end=args.end, log_class=CAN_LOG_CLASS, username=settings["username"], page_size=args.page_size, eea_platform=args.eea_platform, environment=env)
                    if not can.items:
                        raise CloudNoDataError(f"{env} 环境没有 CAN 报文数据")
                    return {
                        "environment": env,
                        "providerEnvironment": provider_environment(env),
                        "uroadTotal": uroad.total,
                        "canTotal": can.total,
                        "uroadDownloadHosts": uroad.download_hosts,
                        "canDownloadHosts": can.download_hosts,
                    }

                if args.cloud_env == "auto":
                    try:
                        selected = preflight_environment("prod")
                    except CloudNoDataError:
                        selected = preflight_environment("test")
                    args.resolved_cloud_env = selected["environment"]
                    args.resolved_provider_environment = selected["providerEnvironment"]
                    run["environmentSelection"] = {"mode": "auto", "selected": selected}
                else:
                    selected = preflight_environment(args.cloud_env)
                    args.resolved_cloud_env = selected["environment"]
                    args.resolved_provider_environment = selected["providerEnvironment"]
                    run["environmentSelection"] = {"mode": args.cloud_env, "selected": selected}
                save(run_path, run)

                def call(command: list[str]):
                    environment = os.environ.copy()
                    environment["PYTHONDONTWRITEBYTECODE"] = "1"
                    try:
                        completed = subprocess.run(command, check=True, text=True, encoding="utf-8", env=environment, timeout=900, capture_output=True)
                        output = (completed.stdout or "").strip()
                        if output:
                            try:
                                return json.loads(output)
                            except json.JSONDecodeError:
                                return None
                        return None
                    except subprocess.CalledProcessError as exc:
                        detail = (getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or "").strip()
                        if detail:
                            try:
                                payload = json.loads(detail)
                                detail = payload.get("error") or detail
                            except (json.JSONDecodeError, AttributeError):
                                pass
                        error = RuntimeError(detail or f"子进程失败: {Path(command[1]).name} (exit {exc.returncode})")
                        error.retryable = True
                        raise error from exc

                from process_time_chunks import process
                process(args, run_dir, run, save, call, {"python": sys.executable, "uroad": str(CANLOG_UROAD_DOWNLOAD), "can_stream": str(CAN_SIGNAL_STREAM), "analysis": str(ANALYSIS)})
                run["steps"].update(download="completed", canlog="completed", canSignals="completed", analysis="completed")
                run["diagnosticArtifacts"] = {"manifest": str(run_dir / "manifest.json"), "canSignals": str(run_dir / "analysis" / "can-signals.json"), "analysis": str(run_dir / "analysis" / "analysis.json")}
                run["steps"]["analysis"] = "completed"
                save(run_path, run)

                html_report_path = run_dir / "report" / build_report_filename(args.vin, args.start, args.end)
                call([sys.executable, str(HTML_REPORT), "--analysis", str(run_dir / "analysis" / "analysis.json"), "--output", str(html_report_path)])
                if not html_report_path.is_file() or html_report_path.stat().st_size == 0:
                    raise RuntimeError("HTML 报告未生成或为空")
                with html_report_path.open("rb") as report_file:
                    if not report_file.read(1):
                        raise RuntimeError("HTML 报告不可读或为空")
                validate_html_report(html_report_path)
                run["steps"]["report"] = "completed"
                removed, cleanup_errors = cleanup_intermediate(run_dir)
                run["cleanup"] = {"stage": "report_ready", "removed": removed, "completed": not cleanup_errors, "errors": cleanup_errors}
                if cleanup_errors:
                    raise RuntimeError("中间文件清理失败: " + "; ".join(cleanup_errors))
                run["artifacts"] = {**run.pop("diagnosticArtifacts"), "report": str(html_report_path)}
                run["status"] = "completed"
                run["completedAt"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
                save(run_path, run)
                mark_finished(run_id, "completed", report=str(html_report_path))

                validated = call([sys.executable, str(SCRIPTS / "validate_report_artifact.py"), "--run-json", str(run_path)])
                if not isinstance(validated, dict) or validated.get("ok") is not True:
                    raise RuntimeError("报告完成后校验结果无效")

                print(json.dumps({"ok": True, "run": str(run_path), "artifacts": run["artifacts"], "task": run.get("task"), "delivery": {"reportValidation": validated, "attachment": validated["report"], "attachmentType": "text/html"}}, ensure_ascii=False, indent=2))
                return 0
        except BusyTaskError as exc:
            if args.existing_run_id:
                return requeue_busy_task(exc.active)
            raise
    except (Exception, KeyboardInterrupt) as exc:
        report_cleanup_error = None
        try:
            cleanup_report_output(run_dir, build_report_filename(args.vin, args.start, args.end))
        except OSError as cleanup_exc:
            report_cleanup_error = str(cleanup_exc)
        # No data is a temporary, user-actionable outcome. Cloud/API failures
        # must remain distinguishable from an empty successful response.
        from cheyun_prod_client import CloudNoDataError, CloudQueryError
        if isinstance(exc, CloudNoDataError):
            run["status"] = "no_data"
            run["userMessage"] = "截至当前暂未查询到该 VIN 和时间范围的数据，数据可能仍在上传或同步。本结果不是永久结论，稍后再次发起相同请求时系统会重新查询。"
        elif isinstance(exc, CloudQueryError):
            run["status"] = "system_error"
        elif isinstance(exc, KeyboardInterrupt):
            run["status"] = "system_error"
        else:
            run["status"] = "system_error"
        active = run.get("chunks", {}).get("active") or {}
        if active:
            active["status"] = "failed"
        run["error"] = {"message": str(exc) or type(exc).__name__, "type": type(exc).__name__, "retryable": bool(getattr(exc, "retryable", False) or isinstance(exc, subprocess.TimeoutExpired)), "slice": active.get("index"), "source": active.get("source"), "stage": active.get("stage")}
        save(run_path, run)
        removed, cleanup_errors = cleanup_intermediate(run_dir)
        cleanup_errors = list(getattr(exc, "cleanup_errors", [])) + cleanup_errors
        if report_cleanup_error:
            cleanup_errors.append("report: " + report_cleanup_error)
        run["cleanup"] = {"stage": "failed", "removed": removed, "completed": not cleanup_errors, "errors": cleanup_errors}
        if run.get("chunks"):
            run["chunks"]["active"] = None
        save(run_path, run)
        mark_finished(run_id, run["status"], error=run["error"]["message"])
        result = {"ok": False, "run": str(run_path), "task": run.get("task"), "status": run["status"], "error": run["error"]}
        if run.get("userMessage"):
            result["message"] = run["userMessage"]
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2 if run["status"] == "no_data" else 1


if __name__ == "__main__":
    raise SystemExit(main())
