from __future__ import annotations

import os
import re
import subprocess

_SHA40 = re.compile(r"[0-9a-fA-F]{40}")


def main() -> None:
    expected = os.environ.get("NIKA_CANDIDATE_SHA", "")
    if _SHA40.fullmatch(expected) is None:
        raise SystemExit("NIKA_CANDIDATE_SHA must be an exact 40-character Git SHA")

    actual = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        text=True,
        encoding="utf-8",
    ).strip()
    if actual.casefold() != expected.casefold():
        raise SystemExit(f"checkout SHA mismatch: expected {expected}, got {actual}")

    # A matching HEAD alone does not prove the bytes being tested or packaged.
    # Fail closed on tracked edits, staged changes and untracked, non-ignored
    # files; ignored build/cache directories remain outside the source tree.
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise SystemExit("could not verify checkout worktree integrity") from None
    if status:
        # Do not log filenames: they may expose private local paths or secrets.
        raise SystemExit("checkout worktree is not clean at the expected SHA")

    # Git status deliberately trusts assume-unchanged/skip-worktree index bits.
    # A modified source file behind either bit must not earn SHA-bound evidence.
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-v"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise SystemExit("could not verify tracked checkout files") from None
    if any(line and line[0] != "H" for line in tracked.splitlines()):
        raise SystemExit("checkout contains unverified tracked file flags")

    print(f"Verified exact clean checkout SHA: {actual}")


if __name__ == "__main__":
    main()
