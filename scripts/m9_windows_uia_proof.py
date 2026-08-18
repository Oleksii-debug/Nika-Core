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
    last_summary = ""
    for _ in range(30):
        try:
            evidence = await adapter.inspect_windows("Nika M9 UIA Proof")
            last_summary = evidence.summary
            named_controls = [
                item for item in evidence.accessible_controls if ":" in item and item.split(":", 1)[1]
            ]
            if evidence.accessible_controls and named_controls:
                assert evidence.method.value == "uia"
                assert evidence.confidence == 1.0
                assert evidence.target == "Nika M9 UIA Proof"
                print(evidence.summary)
                return
        except Exception as exc:  # noqa: BLE001 - bounded proof retry across GUI startup
            last_error = exc
        await asyncio.sleep(0.5)
    raise AssertionError(
        "UIA proof did not expose process-scoped named semantic controls: "
        f"last_error={last_error!r}; last_summary={last_summary!r}"
    )


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
