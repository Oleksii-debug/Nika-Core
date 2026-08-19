from __future__ import annotations

import os
import re
import subprocess

_SHA40 = re.compile(r"[0-9a-fA-F]{40}")


def main() -> None:
    expected = os.environ.get("NIKA_CANDIDATE_SHA", "").strip()
    if _SHA40.fullmatch(expected) is None:
        raise SystemExit("NIKA_CANDIDATE_SHA must be an exact 40-character Git SHA")

    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
    ).strip()
    if actual.casefold() != expected.casefold():
        raise SystemExit(f"checkout SHA mismatch: expected {expected}, got {actual}")

    print(f"Verified exact checkout SHA: {actual}")


if __name__ == "__main__":
    main()
