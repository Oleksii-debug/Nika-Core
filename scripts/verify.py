from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence


def run_step(label: str, command: Sequence[str]) -> None:
    print(f"\n== {label} ==", flush=True)
    print("$ " + " ".join(command), flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    python = sys.executable
    steps: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Dependency consistency", (python, "-m", "pip", "check")),
        ("Ruff", ("ruff", "check", "src", "tests", "scripts")),
        ("Compile", (python, "-m", "compileall", "-q", "src", "tests", "scripts")),
        ("Tests", (python, "-m", "pytest")),
    )
    for label, command in steps:
        run_step(label, command)
    if "NIKA_CANDIDATE_SHA" in os.environ:
        run_step(
            "Post-verification source identity",
            (python, "scripts/qa_assert_checkout_identity.py"),
        )
    print("\nAll verification steps passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
