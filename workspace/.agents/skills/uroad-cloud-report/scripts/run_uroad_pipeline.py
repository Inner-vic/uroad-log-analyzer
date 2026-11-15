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
import sysconfig
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from task_registry import (BusyTaskError, DuplicateTaskError, RegistryError, existing_task_for_execution,
                           load_request_metadata, mark_dequeued, mark_finished, register_or_reuse,
                           request_identity, runtime_version)
from scheduler import acquire_heavy_slot
from request_guard import reject_non_uroad_request
from managed_subprocess import StageProcessError, StageTimeout, run_bounded

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CANLOG_UROAD_DOWNLOAD = SCRIPTS / "cheyun_uroad_adapter.py"
CAN_SIGNAL_STREAM = SCRIPTS / "stream_can_signals.py"
ANALYSIS = SCRIPTS / "parse_uroad.py"
HTML_REPORT = SCRIPTS / "build_html_report.py"
WORKSPACE_ENV = "CC_CONNECT_WORKSPACE"
DEFAULT_WORKSPACE = Path(os.environ.get(WORKSPACE_ENV, "").strip()).expanduser() if os.environ.get(WORKSPACE_ENV, "").strip() else SKILL_ROOT.parents[2]
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
STAGE_TIMEOUT_SECONDS = 900
MAX_SUBPROCESS_TIMINGS = 256
RUN_STATE_LOCK = threading.RLock()
STAGE_SCRIPTS = {"cheyun_uroad_adapter.py": "uroad_download", "parse_uroad.py": "uroad_parse",
                 "stream_can_signals.py": "can_stream", "parquet_can_signals.py": "parquet_can",
                 "build_html_report.py": "report_build", "validate_report_artifact.py": "report_validate"}


def save(path: Path, value: dict) -> None:
    with RUN_STATE_LOCK:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)


save.state_lock = RUN_STATE_LOCK


def stage_for_command(command: list[str]) -> str:
    if not isinstance(command, list) or len(command) < 2:
        raise RuntimeError("Uroad 子步骤调用格式无效。")
    stage = STAGE_SCRIPTS.get(Path(command[1]).name)
    if stage is None:
        raise RuntimeError("Uroad 子步骤未登记，已停止执行。")
    return stage


def stage_caller(run_dir: Path, run: dict, save_fn=save):
    """Keep finite public timings and private argv diagnostics for every call."""
    state_lock = getattr(save_fn, "state_lock", RUN_STATE_LOCK)
    run_path = run_dir / "run.json"

    def call(command: list[str]):
        stage = stage_for_command(command)
        started = time.monotonic()
        with state_lock:
            sequence = int(run.get("subprocessCalls", 0)) + 1
            run["subprocessCalls"] = sequence
            timing = {"sequence": sequence, "stage": stage, "status": "running",
                      "timeoutSeconds": STAGE_TIMEOUT_SECONDS,
                      "startedAt": datetime.now(timezone.utc).isoformat()}
            timings = run.setdefault("subprocessTimings", [])
            if len(timings) >= MAX_SUBPROCESS_TIMINGS:
                oldest_finished = next((index for index, value in enumerate(timings) if value.get("status") != "running"), None)
                if oldest_finished is None:
                    raise RuntimeError("Uroad 并行子步骤数量超过记录上限。")
                timings.pop(oldest_finished)
                run["subprocessTimingsDropped"] = int(run.get("subprocessTimingsDropped", 0)) + 1
            timings.append(timing)
            save_fn(run_path, run)
        environment = os.environ.copy()
        environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        diagnostic = run_dir / "diagnostics" / f"subprocess-{sequence:04d}-{stage}.json"
        try:
            completed = run_bounded(command, env=environment, timeout=STAGE_TIMEOUT_SECONDS,
                                    stage=stage, diagnostics_path=diagnostic)
            if completed.returncode:
                reason = "can_non_zstd" if stage == "can_stream" and "非 zstd 文件" in (completed.stderr or "") else "process_failed"
                child_error_type = None
                for child_text in (completed.stderr, completed.stdout):
                    try:
                        child_payload = json.loads(child_text or "")
                    except (ValueError, TypeError):
                        continue
                    if isinstance(child_payload, dict) and child_payload.get("errorType") in {
                            "MemoryError", "ArrowMemoryError", "ValueError", "RuntimeError", "OSError",
                            "ArrowInvalid", "ArrowNotImplementedError", "OperationalError", "AssertionError",
                            "IncompleteRead", "ConnectionResetError", "TimeoutError", "CloudNetworkError", "CloudServerError"}:
                        child_error_type = child_payload["errorType"]
                        break
                if child_error_type in {"MemoryError", "ArrowMemoryError"}:
                    reason = "memory_exhausted"
                elif stage == "parquet_can" and child_error_type in {"IncompleteRead", "ConnectionResetError", "TimeoutError", "CloudNetworkError", "CloudServerError"}:
                    reason = "download_interrupted"
                failure = StageProcessError(stage=stage, returncode=completed.returncode,
                                            elapsed=time.monotonic()-started, reason=reason)
                failure.child_error_type = child_error_type
                raise failure
            with state_lock:
                timing.update(status="completed", returncode=0)
            output = (completed.stdout or "").strip()
            try:
                return json.loads(output) if output else None
            except json.JSONDecodeError:
                return None
        except StageTimeout:
            with state_lock:
                timing["status"] = "timeout"
            raise
        except BaseException as exc:
            with state_lock:
                timing.update(status="failed", errorType=type(exc).__name__)
                if isinstance(exc, StageProcessError):
                    timing["returncode"] = exc.returncode
                    if getattr(exc, "child_error_type", None):
                        timing["childErrorType"] = exc.child_error_type
            raise
        finally:
            with state_lock:
                timing.update(endedAt=datetime.now(timezone.utc).isoformat(),
                              elapsedSeconds=round(time.monotonic()-started, 3),
                              diagnostic=diagnostic.relative_to(run_dir).as_posix())
                save_fn(run_path, run)

    return call


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
    configured_workspace = os.environ.get(WORKSPACE_ENV, "").strip()
    workspace = Path(configured_workspace) if configured_workspace else DEFAULT_WORKSPACE
    try:
        workspace = workspace.resolve(strict=True)
    except OSError as exc:
        hint = WORKSPACE_ENV if configured_workspace else str(DEFAULT_WORKSPACE)
        raise ValueError(f"CC Connect 工作区不可用，请检查 {hint}: {workspace}") from exc
    if not workspace.is_dir():
        hint = WORKSPACE_ENV if configured_workspace else str(DEFAULT_WORKSPACE)
        raise ValueError(f"CC Connect 工作区不是目录，请检查 {hint}: {workspace}")
    if workspace == SKILL_ROOT or SKILL_ROOT in workspace.parents:
        raise ValueError(f"CC Connect 工作区不能位于 Skill 源码目录内: {workspace}")
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


# The business entrypoint may repair only these explicitly locked packages.
# Requirements outside this list are intentionally rejected rather than inferred
# from a user request or installed through an unpinned dependency resolution.
MANAGED_DEPENDENCIES = (
    ("zstandard", "zstandard", "0.25.0"),
    ("pyarrow", "pyarrow", "25.0.1"),
    ("openpyxl", "openpyxl", "3.1.5"),
    ("et-xmlfile", "et_xmlfile", "2.0.0"),
    ("tzdata", "tzdata", "2026.4"),
)
REQUIREMENTS_FILE = SKILL_ROOT / "requirements.txt"
DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 300
DEPENDENCY_LOCK_TIMEOUT_SECONDS = 180


def _managed_requirement_specs() -> dict[str, str]:
    expected = {name: f"{name}=={version}" for name, _, version in MANAGED_DEPENDENCIES}
    try:
        lines = [line.strip() for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        raise RuntimeError("Uroad 固定依赖清单不可读；已停止自动修复") from exc
    actual = {line.split("==", 1)[0].strip().lower(): line for line in lines if line and not line.startswith("#")}
    if actual != expected:
        raise RuntimeError("Uroad 固定依赖清单与受控白名单不一致；已停止自动修复")
    return expected


def _missing_managed_dependencies() -> list[str]:
    expected = [(package, module, version) for package, module, version in MANAGED_DEPENDENCIES]
    probe = (
        "import importlib,importlib.metadata,json; "
        f"expected={expected!r}; missing=[]; "
        "exec(\"for package,module,version in expected:\\n"
        "    try:\\n"
        "        importlib.import_module(module)\\n"
        "        actual=importlib.metadata.version(package)\\n"
        "        if actual != version: missing.append(package)\\n"
        "    except Exception: missing.append(package)\"); "
        "print(json.dumps(sorted(set(missing))))"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe], check=True,
            timeout=DEPENDENCY_VALIDATION_TIMEOUT_SECONDS, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        missing = json.loads((completed.stdout or "").strip())
        if not isinstance(missing, list) or any(item not in {package for package, _, _ in MANAGED_DEPENDENCIES} for item in missing):
            raise ValueError("invalid dependency probe result")
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return [package for package, _, _ in MANAGED_DEPENDENCIES]
    if "zstandard" not in missing and not _zstandard_available():
        missing.append("zstandard")
    return sorted(set(missing))


def _isolated_site_packages() -> Path:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    probe = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe], check=True,
            timeout=DEPENDENCY_VALIDATION_TIMEOUT_SECONDS, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        site_packages = Path((completed.stdout or "").strip()).resolve(strict=True)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法定位 Uroad 受控 Python 运行目录；已停止自动修复") from exc
    if not site_packages.is_dir():
        raise RuntimeError("Uroad 受控 Python 运行目录不可用；已停止自动修复")
    return site_packages


def _acquire_dependency_lock(site_packages: Path) -> tuple[Path, int]:
    lock = site_packages / ".uroad-dependency-bootstrap.lock"
    deadline = time.monotonic() + DEPENDENCY_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            return lock, descriptor
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("等待 Uroad 依赖自动修复超时；请稍后重试")
            time.sleep(1)
        except OSError as exc:
            raise RuntimeError("无法获取 Uroad 依赖自动修复锁；已停止自动修复") from exc


def _install_managed_dependencies(site_packages: Path, missing: list[str]) -> None:
    specifications = _managed_requirement_specs()
    if not missing:
        return
    command = [
        sys.executable, "-I", "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input", "--no-deps", "--only-binary=:all:",
        "--target", str(site_packages),
        *(specifications[name] for name in missing),
    ]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            command, check=True, timeout=DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
            env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        detail = ((exc.stderr or exc.stdout or "").strip())[-800:]
        raise RuntimeError(f"Uroad 受控依赖自动修复失败：{detail or 'pip 未返回可用错误信息'}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Uroad 受控依赖自动修复失败；请检查本机网络、Python 写入权限和包源可用性") from exc


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
    """Verify and, if necessary, repair the fixed Uroad runtime dependency set."""
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise RuntimeError(f"CC Connect 工作区不是目录: {workspace}")
    missing = _missing_managed_dependencies()
    if not missing:
        return None
    site_packages = _isolated_site_packages()
    lock, descriptor = _acquire_dependency_lock(site_packages)
    try:
        # Another concurrent heavy task may have finished repair while this one
        # waited for the lock, so always re-check before invoking pip.
        missing = _missing_managed_dependencies()
        if missing:
            _install_managed_dependencies(site_packages, missing)
        remaining = _missing_managed_dependencies()
        if remaining:
            raise RuntimeError("Uroad 受控依赖自动修复后仍不可用：" + ", ".join(remaining))
    finally:
        try:
            os.close(descriptor)
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    return None


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
            raise ValueError(f"报告输出目录解析后超出 CC Connect 工作区: {outputs_root}")
        is_junction = getattr(runs_root, "is_junction", lambda: False)
        if runs_root.exists() and (runs_root.is_symlink() or is_junction()):
            raise ValueError(f"报告运行目录不能是符号链接或目录联接: {runs_root}")
        runs_root.mkdir(parents=False, exist_ok=True)
        if not runs_root.is_dir():
            raise ValueError(f"报告运行目录不是有效目录: {runs_root}")
        resolved_runs_root = runs_root.resolve(strict=True)
        expected_runs_root = (workspace / RUNS_DIRECTORY).resolve(strict=True)
        if resolved_runs_root != expected_runs_root or workspace not in resolved_runs_root.parents:
            raise ValueError(f"报告运行目录解析后超出 CC Connect 工作区: {runs_root}")
        if existing_run_id:
            run_dir = runs_root / existing_run_id
            if (Path(existing_run_id).name != existing_run_id or existing_run_id in {".", ".."}
                    or run_dir.resolve().parent != resolved_runs_root):
                raise ValueError("已有任务标识无效，拒绝覆盖运行目录。")
            prior_run = run_dir / "run.json"
            evidence_names = ("raw", "input", "canlog", "slices", "merge-state", "analysis", "original", "diagnostics", "report")
            if any((run_dir / name).exists() for name in evidence_names):
                raise RuntimeError("该任务已有保留的执行证据，不能覆盖重试；需使用新的恢复尝试，原任务证据保持保留。")
            if prior_run.exists():
                try:
                    previous = json.loads(prior_run.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise RuntimeError("已有任务状态不可验证，拒绝覆盖重试。") from None
                if not isinstance(previous, dict) or previous.get("status") != "queued" or previous.get("error"):
                    raise RuntimeError("已有任务已执行，不能覆盖重试；需使用新的恢复尝试。")
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
        raise ValueError(f"无法在 CC Connect 工作区创建报告目录，请检查目录权限: {workspace}") from exc
    raise RuntimeError("无法分配唯一报告目录，请稍后重试")


def initial_run(args, task_record, retention):
    return {"schemaVersion": "1.3", "runId": task_record["runId"], "status": "queued",
            "input": vars(args), "steps": {key: "pending" for key in ("download", "canlog", "canSignals", "analysis", "report")},
            "artifacts": {}, "error": None, "housekeeping": retention, **runtime_version(),
            "request": task_record["request"], "task": task_record}


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
    parser.add_argument("--enqueue-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--question", action="append", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.enqueue_only and args.existing_run_id:
        parser.error("领取后的任务不能再次仅入队")

    try:
        validate_inputs(args)
        from cheyun_prod_client import CloudNoDataError, CAN_LOG_CLASS, UROAD_LOG_CLASS, load_settings, provider_environment, query_files
        settings = load_settings()
        workspace = validate_workspace()
        bootstrap_dependencies(workspace)
        # Daily retention is owned by the independent scheduled maintenance
        # task. Never recursively clean other jobs on a business request path.
        retention = {"ok": True, "mode": "scheduled_only"}
        run_dir = None
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    metadata = load_request_metadata()
    reject_non_uroad_request(metadata)
    run_id = args.existing_run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}"
    if args.enqueue_only:
        try:
            run_dir = allocate_run_directory(workspace, existing_run_id=run_id)
            run_path = run_dir / "run.json"
            task_record = register_or_reuse(run_id, args.vin, args.start, args.end, args.cloud_env,
                metadata=metadata, attached_questions=args.question,
                execution_options={"eeaPlatform": args.eea_platform, "pageSize": args.page_size, "maxFiles": args.max_files},
                prepare_callback=lambda task: save(run_path, initial_run(args, task, retention)))
        except DuplicateTaskError as exc:
            if run_dir is not None and run_dir.is_dir() and not any(run_dir.iterdir()):
                run_dir.rmdir()
            print(json.dumps({"ok": False, "duplicate": True, "reusable": exc.reusable, "existing": exc.task,
                              "error": "相同来源消息已有任务，未重复创建。"}, ensure_ascii=False), file=sys.stderr)
            return 2
        except (OSError, RuntimeError, ValueError):
            print(json.dumps({"ok": False, "error": "任务入队持久化失败，未启动日志分析。"}, ensure_ascii=False), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "queued": True, "runId": run_id,
                          "run": str(run_path), "record_path": str(run_path)}, ensure_ascii=False))
        return 0
    if not args.existing_run_id:
        try:
            task_record = register_or_reuse(
                run_id, args.vin, args.start, args.end, args.cloud_env,
                metadata=metadata, attached_questions=args.question,
                execution_options={"eeaPlatform": args.eea_platform, "pageSize": args.page_size, "maxFiles": args.max_files},
            )
        except DuplicateTaskError as exc:
            payload = {"ok": False, "duplicate": True, "reusable": exc.reusable, "existing": exc.task, "error": "已存在相同请求的任务，请优先复用现有结果"}
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
    else:
        try:
            task_record = existing_task_for_execution(args.existing_run_id,
                request_identity(args.vin, args.start, args.end, args.cloud_env), metadata,
                execution_options={"eeaPlatform": args.eea_platform, "pageSize": args.page_size, "maxFiles": args.max_files})
        except RegistryError:
            print(json.dumps({"ok": False, "error": "已有任务请求、来源或领取状态不一致，未启动分析。"}, ensure_ascii=False), file=sys.stderr)
            return 1
    try:
        run_dir = allocate_run_directory(workspace, existing_run_id=run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            # retry-existing may already have requeued its registry record.
            # Restore its failed terminal state without rewriting old run.json.
            mark_finished(run_id, "system_error", error=str(exc))
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    run = initial_run(args, task_record, retention)
    run_path = run_dir / "run.json"
    save(run_path, run)
    execution_completed = False
    delivery = None

    try:
        run["task"] = task_record
        save(run_path, run)
        if args.enqueue_only:
            print(json.dumps({"ok": True, "queued": True, "runId": run_id,
                              "run": str(run_path), "record_path": str(run_path)}, ensure_ascii=False))
            return 0

        def requeue_busy_task(active: dict) -> int:
            mark_dequeued(run_id)
            run["status"] = "queued"
            run["task"] = {**task_record, "status": "queued",
                           "execution": {**task_record.get("execution", {}), "status": "queued"}}
            run["queue"] = {"reason": "busy", "active": active}
            save(run_path, run)
            # Queueing behind another run is an accepted asynchronous outcome,
            # not an execution failure.  Returning non-zero caused the Feishu
            # adapter to show `Exec failed` even though the worker later
            # completed the report successfully.
            print(json.dumps({"ok": True, "queued": True, "busy": True, "active": active, "run": str(run_path), "message": "已有任务在运行，当前任务已排队"}, ensure_ascii=False, indent=2))
            return 0

        try:
            with acquire_heavy_slot(run_id) as active_task:
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

                call = stage_caller(run_dir, run, save)

                from process_time_chunks import process
                process(args, run_dir, run, save, call, {"python": sys.executable, "uroad": str(CANLOG_UROAD_DOWNLOAD), "can_stream": str(CAN_SIGNAL_STREAM), "parquet_can": str(SCRIPTS / "parquet_can_signals.py"), "analysis": str(ANALYSIS)})
                run["steps"].update(download="completed", canlog="completed", canSignals="completed", analysis="completed")
                run["diagnosticArtifacts"] = {"manifest": str(run_dir / "manifest.json"), "canSignals": str(run_dir / "analysis" / "can-signals.json"), "analysis": str(run_dir / "analysis" / "analysis.json")}
                run["steps"]["analysis"] = "completed"
                save(run_path, run)

                html_report_path = run_dir / "report" / build_report_filename(args.vin, args.start, args.end)
                run['stage']='report_build'
                save(run_path,run)
                call([sys.executable, str(HTML_REPORT), "--analysis", str(run_dir / "analysis" / "analysis.json"), "--output", str(html_report_path)])
                if not html_report_path.is_file() or html_report_path.stat().st_size == 0:
                    raise RuntimeError("HTML 报告未生成或为空")
                with html_report_path.open("rb") as report_file:
                    if not report_file.read(1):
                        raise RuntimeError("HTML 报告不可读或为空")
                validate_html_report(html_report_path)
                run["steps"]["report"] = "completed"
                # Raw downloads and parsed artifacts remain task-local for the
                # configured follow-up window.  Retention cleanup removes the
                # complete run atomically after that window expires.
                run["cleanup"] = {"stage": "retained_for_followup", "removed": [], "completed": True, "errors": []}
                run["artifacts"] = {**run.pop("diagnosticArtifacts"), "report": str(html_report_path)}
                run["status"] = "validating_report"
                run["reportValidation"] = {"status": "pending"}
                run['stage']='report_validate'
                save(run_path, run)
                validated = call([sys.executable, str(SCRIPTS / "validate_report_artifact.py"),
                                  "--run-json", str(run_path), "--allow-validating"])
                if not isinstance(validated, dict) or validated.get("ok") is not True:
                    raise RuntimeError("报告完成前校验结果无效")
                run["reportValidation"] = {"status": "passed", "validatedAt": datetime.now(timezone.utc).isoformat()}
                run["status"] = "completed"
                run["completedAt"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
                save(run_path, run)
                mark_finished(run_id, "completed", report=str(html_report_path))
                execution_completed = True

                followup_registered = False
                try:
                    from followup_context import register_data_context
                    followup_data = register_data_context(run_id, run_dir)
                    run["followup"] = {"jobId": run_id, "status": "registered",
                                       "dataVersion": followup_data["version"], "retentionUntil": followup_data["retentionUntil"]}
                    followup_registered = True
                except Exception as registration_error:
                    # Follow-up availability must not discard or withhold a
                    # standard report that has already passed artifact checks.
                    run["followup"] = {"jobId": run_id, "status": "registration_failed",
                                       "errorType": type(registration_error).__name__,
                                       "error": "追问数据上下文登记未完成；标准报告已生成。"}
                save(run_path, run)

                from delivery_bridge import send_native_report
                run['stage']='delivery'
                save(run_path,run)
                delivery = send_native_report(run_path)
                if delivery.get('status') in {'sent','already_sent'}:
                    run['stage']='completed'
                    save(run_path,run)
                from followup_agent import start_pending_followups
                followup_dispatch = (
                    start_pending_followups(run_id)
                    if followup_registered and delivery.get("status") in {"sent", "already_sent"}
                    else {"started": False, "reason": "report_delivery_failed" if delivery.get("status") not in {"sent", "already_sent"} else "followup_registration_failed"}
                )
                print(json.dumps({"ok": True, "run": str(run_path), "artifacts": run["artifacts"], "task": run.get("task"), "followup": run["followup"], "followupDispatch": followup_dispatch, "delivery": {"reportValidation": validated, "attachment": validated["report"], "attachmentType": "text/html", "result": delivery}}, ensure_ascii=False, indent=2))
                return 0
        except BusyTaskError as exc:
            if args.existing_run_id:
                return requeue_busy_task(exc.active)
            raise
    except (Exception, KeyboardInterrupt) as exc:
        if execution_completed:
            # Publishing/follow-up faults must never reopen successful analysis,
            # delete its validated report, or attempt completed -> system_error.
            if not isinstance(delivery, dict) or delivery.get("status") not in {"sent", "already_sent"}:
                from task_registry import mark_delivery
                mark_delivery(run_id, "delivery_failed", error="报告已生成，但附件交付尚未确认。")
                delivery = {"status": "failed", "error": "报告已生成，但附件交付尚未确认。"}
            run["postCompletionError"] = {"type": type(exc).__name__, "message": "报告已生成，后续交付或追问处理未完成。"}
            save(run_path, run)
            print(json.dumps({"ok": True, "run": str(run_path), "status": "completed",
                              "artifacts": run["artifacts"], "delivery": delivery}, ensure_ascii=False, indent=2))
            return 0
        report_cleanup_error = None
        try:
            if (run.get("reportValidation") or {}).get("status") != "passed":
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
        run["error"] = {"message": str(exc) or type(exc).__name__, "type": type(exc).__name__, "retryable": bool(getattr(exc, "retryable", False) or isinstance(exc, subprocess.TimeoutExpired)), "slice": active.get("index"), "source": active.get("source"), "stage": getattr(exc, "stage", None) or active.get("stage")}
        if isinstance(exc, StageTimeout):
            run["error"].update(timeoutSeconds=exc.timeout_seconds, elapsedSeconds=exc.elapsed_seconds)
        if isinstance(exc, StageProcessError):
            run["error"].update(reason=exc.reason, elapsedSeconds=exc.elapsed_seconds)
            if getattr(exc, "child_error_type", None):
                run["error"]["childErrorType"] = exc.child_error_type
        save(run_path, run)
        # Keep partial acquisition / parsing evidence task-local. The normal
        # retention job removes the whole failed task after its configured TTL.
        retained = [name for name in ("raw", "input", "canlog", "slices", "merge-state", "analysis", "original", "diagnostics", "report")
                    if (run_dir / name).exists()]
        cleanup_errors = list(getattr(exc, "cleanup_errors", []))
        if report_cleanup_error:
            cleanup_errors.append("report: " + report_cleanup_error)
        run["failureRetention"] = {"policy": "task_retention", "paths": retained, "reason": "processing_failed"}
        run["cleanup"] = {"stage": "retained_for_diagnostics", "removed": [], "retained": retained,
                          "completed": not cleanup_errors, "errors": cleanup_errors}
        save(run_path, run)
        mark_finished(run_id, run["status"], error=run["error"]["message"])
        result = {"ok": False, "run": str(run_path), "task": run.get("task"), "status": run["status"], "error": run["error"]}
        if run.get("userMessage"):
            result["message"] = run["userMessage"]
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2 if run["status"] == "no_data" else 1


if __name__ == "__main__":
    raise SystemExit(main())
