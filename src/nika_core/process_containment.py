from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
from typing import Self


class ProcessContainmentError(RuntimeError):
    """Raised when the OS process-tree containment boundary cannot be established."""


class WindowsJob:
    """Kill-on-close Windows Job Object used to contain one spawned process tree."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._handle is not None

    def assign(self, process_handle: int) -> None:
        if os.name != "nt":
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        create_job.restype = ctypes.c_void_p
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        set_information.restype = ctypes.c_int
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        assign_process.restype = ctypes.c_int

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = create_job(None, None)
        if not job:
            raise ProcessContainmentError(
                f"CreateJobObjectW failed with Win32 error {ctypes.get_last_error()}"
            )
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not set_information(job, 9, ctypes.byref(information), ctypes.sizeof(information)):
            kernel32.CloseHandle(job)
            raise ProcessContainmentError(
                f"SetInformationJobObject failed with Win32 error {ctypes.get_last_error()}"
            )
        if not assign_process(job, ctypes.c_void_p(process_handle)):
            kernel32.CloseHandle(job)
            raise ProcessContainmentError(
                f"AssignProcessToJobObject failed with Win32 error {ctypes.get_last_error()}"
            )
        with self._lock:
            self._handle = int(job)

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle is None or os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def process_group_popen_options() -> tuple[int, bool]:
    """Return the canonical process-group/session flags for a contained child process."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), False
    return 0, True


def terminate_process_tree(process: subprocess.Popen[bytes], job: WindowsJob) -> None:
    """Terminate the contained process tree without raising cleanup-only failures."""
    if os.name == "nt" and job.active:
        job.close()
        return
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        return
