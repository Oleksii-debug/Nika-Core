"""Native keyboard-accessible fatal startup message for windowless Windows builds."""

from __future__ import annotations

import ctypes
import os
import sys


def show_recovery_error(message: str) -> None:
    if os.name != "nt":
        print(message, file=sys.stderr)
        return
    from ctypes import wintypes

    dialog = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
    dialog.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    dialog.restype = ctypes.c_int
    dialog(None, message, "Nika Core — відновлення даних", 0x00000010 | 0x00010000)
