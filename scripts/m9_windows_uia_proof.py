from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from nika_core.workspaces.semantic_adapters import PywinautoUIAAdapter

APP = r'''
import tkinter as tk
root = tk.Tk()
root.title("Nika M9 UIA Proof")
tk.Label(root, text="Accessibility repair").pack()
tk.Entry(root, name="problem").pack()
tk.Button(root, text="Repair now").pack()
root.mainloop()
'''


async def inspect_until_ready(process_id: int) -> None:
    adapter = PywinautoUIAAdapter(process_id=process_id)
    last_error: Exception | None = None
    for _ in range(30):
        try:
            evidence = await adapter.inspect_windows("Nika M9 UIA Proof")
            if any("Repair now" in item for item in evidence.accessible_controls):
                assert evidence.method.value == "uia"
                assert evidence.confidence == 1.0
                return
        except Exception as exc:  # noqa: BLE001 - bounded proof retry across GUI startup
            last_error = exc
        await asyncio.sleep(0.5)
    raise AssertionError(f"UIA proof did not expose the expected button: {last_error!r}")


def main() -> None:
    process = subprocess.Popen([sys.executable, "-c", APP])
    try:
        time.sleep(0.5)
        asyncio.run(inspect_until_ready(process.pid))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
