from __future__ import annotations

import subprocess
import sys


def test_foundry_adapter_import_does_not_require_psutil() -> None:
    code = r'''
import builtins

real_import = builtins.__import__


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "psutil" or name.startswith("psutil."):
        raise ModuleNotFoundError("psutil intentionally unavailable in embedded-ai import proof")
    return real_import(name, globals, locals, fromlist, level)


builtins.__import__ = guarded_import
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
assert FoundryLocalProvider.__name__ == "FoundryLocalProvider"
'''
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
