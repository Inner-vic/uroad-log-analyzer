"""Bounded task-local subprocess trees; no commands or stderr in public errors.

Windows uses a gated launcher assigned to a kill-on-close Job Object before it
may spawn the command. POSIX uses a new process group. No executable-name or
unrelated-process killing is used. Raw argv/output belong only in diagnostics.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

STAGES = {"uroad_download": "Uroad 日志下载", "uroad_parse": "Uroad 日志解析",
          "can_stream": "CAN 信号获取与解析", "parquet_can": "CAN Parquet 解析",
          "report_build": "报告生成", "report_validate": "报告校验"}
DIAGNOSTIC_TAIL_CHARS = 20_000
TREE_DRAIN_SECONDS = 10


class StageTimeout(RuntimeError):
    retryable = True

    def __init__(self, *, stage: str, timeout: float, elapsed: float):
        if stage not in STAGES:
            raise ValueError("Unknown bounded stage")
        self.stage = stage
        self.timeout_seconds = timeout
        self.elapsed_seconds = round(elapsed, 3)
        super().__init__(f"{STAGES[stage]}超过单步骤 {timeout:g} 秒时限，已停止本步骤进程树；可核查进度后重试。")


class StageProcessError(RuntimeError):
    retryable = True

    def __init__(self, *, stage: str, returncode: int | None = None, elapsed: float = 0,
                 reason: str = "process_failed"):
        if stage not in STAGES:
            raise ValueError("Unknown bounded stage")
        self.stage, self.returncode = stage, returncode
        self.elapsed_seconds, self.reason = round(elapsed, 3), reason
        message = ("CAN 查询返回了非 zstd 文件，需按受控备用格式解析。" if reason == "can_non_zstd"
                   else f"{STAGES[stage]}内存不足，已保留本任务输入和诊断。" if reason == "memory_exhausted"
                   else "CAN Parquet 下载连接中断，已保留此前完成的下载和校验记录。" if reason == "download_interrupted"
                   else f"{STAGES[stage]}执行失败，已保留本任务诊断；可核查后重试。")
        super().__init__(message)


def _api():
    if os.name != "nt":
        raise RuntimeError("Windows Job Object requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = [
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("CreateEventW", [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("OpenEventW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetEvent", [wintypes.HANDLE], wintypes.BOOL),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ]
    for name, arguments, result in signatures:
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Job:
    def __enter__(self):
        self.api, self.handle = _api(), None
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Cannot create private Uroad Job")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise OSError("Cannot enforce Uroad Job limits")
        return self

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise OSError("Cannot assign gated Uroad process to Job")

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise OSError("Cannot terminate private Uroad Job")

    def __exit__(self, *_):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class _Event:
    def __enter__(self):
        self.api = _api()
        self.name = "Local\\uroad-stage-" + uuid.uuid4().hex
        self.handle = self.api.CreateEventW(None, True, False, self.name)
        if not self.handle:
            raise OSError("Cannot create Uroad release event")
        return self

    def signal(self):
        if not self.api.SetEvent(self.handle):
            raise OSError("Cannot release isolated Uroad process")

    def __exit__(self, *_):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _drain(process):
    try:
        return process.communicate(timeout=TREE_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        # No unbounded communicate while a descendant could retain pipes.
        process.kill()
        try:
            return process.communicate(timeout=TREE_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            raise OSError("Isolated process pipes did not close") from None


def _run_windows(argv, *, env, timeout, cwd):
    with _Job() as job, _Event() as event:
        launcher = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                    "--release-event", event.name, "--", *argv]
        process = subprocess.Popen(launcher, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        assigned = False
        try:
            job.assign(process)
            assigned = True
            event.signal()
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            job.terminate()
            stdout, stderr = _drain(process)
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            if assigned:
                job.terminate()
            else:
                # No command was released, so the launcher cannot have children.
                process.terminate()
            _drain(process)
            raise


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_posix(argv, *, env, timeout, cwd):
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        start_new_session=True, close_fds=True)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            stdout, stderr = _drain(process)
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            _kill_group(process)
            _drain(process)
            raise
    finally:
        # Also remove surviving descendants after a successful leader exits.
        _kill_group(process)


def _write_diagnostics(path: Path | None, payload: dict):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_bounded(argv, *, env, timeout, stage, diagnostics_path=None, cwd=None):
    if (stage not in STAGES or not isinstance(argv, (tuple, list)) or not argv
            or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Invalid bounded stage invocation")
    argv = list(argv)
    path = Path(diagnostics_path) if diagnostics_path is not None else None
    started = time.monotonic()
    diagnostic = {"schema": "uroad-subprocess-diagnostic-v1", "stage": stage, "command": argv,
                  "timeoutSeconds": timeout, "startedAt": datetime.now(timezone.utc).isoformat(),
                  "status": "starting"}
    try:
        _write_diagnostics(path, diagnostic)
        run = _run_windows if os.name == "nt" else _run_posix
        result = run(argv, env=env, timeout=timeout, cwd=cwd)
        diagnostic.update(status="completed" if result.returncode == 0 else "failed", returncode=result.returncode,
                          stdoutTail=(result.stdout or "")[-DIAGNOSTIC_TAIL_CHARS:],
                          stderrTail=(result.stderr or "")[-DIAGNOSTIC_TAIL_CHARS:])
        return result
    except subprocess.TimeoutExpired as exc:
        diagnostic.update(status="timeout", stdoutTail=str(exc.stdout or "")[-DIAGNOSTIC_TAIL_CHARS:],
                          stderrTail=str(exc.stderr or "")[-DIAGNOSTIC_TAIL_CHARS:])
        raise StageTimeout(stage=stage, timeout=timeout, elapsed=time.monotonic()-started) from None
    except (OSError, subprocess.SubprocessError):
        diagnostic["status"] = "launch_or_cleanup_failed"
        raise StageProcessError(stage=stage, elapsed=time.monotonic()-started) from None
    finally:
        diagnostic.update(endedAt=datetime.now(timezone.utc).isoformat(), elapsedSeconds=round(time.monotonic()-started, 3))
        try:
            _write_diagnostics(path, diagnostic)
        except OSError:
            # Preserve a safe timeout/failure instead of leaking raw OS paths.
            pass


def _launcher(event_name, argv):
    api = _api()
    handle = api.OpenEventW(0x00100000, False, event_name)  # SYNCHRONIZE
    if not handle:
        return 125
    try:
        if api.WaitForSingleObject(handle, 60_000) != 0:
            return 125
    finally:
        api.CloseHandle(handle)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                               creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    return process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-event", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not args.release_event.startswith("Local\\uroad-stage-"):
        return 125
    return _launcher(args.release_event, command)


if __name__ == "__main__":
    raise SystemExit(main())
