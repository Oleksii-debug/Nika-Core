"""Create a fresh stdlib venv for locked CI installs and package freezing."""

from __future__ import annotations

import os
import site
import subprocess
import sys
import venv
from pathlib import Path


def main() -> int:
    root = Path.cwd() / ".ci-venv"
    if sys.argv[1:] == ["--verify"]:
        if (
            sys.prefix == sys.base_prefix
            or Path(sys.prefix).resolve() != root.resolve()
            or site.ENABLE_USER_SITE
            or os.environ.get("PYTHONPATH")
        ):
            raise RuntimeError("CI interpreter is not isolated from runner packages")
        print("Verified isolated CI interpreter.")
        return 0
    if sys.argv[1:]:
        raise ValueError("Unsupported CI interpreter option")
    github_path = Path(os.environ["GITHUB_PATH"])
    github_env = Path(os.environ["GITHUB_ENV"])
    # Refuse existing state instead of deleting or trusting an unknown environment.
    root.mkdir(exist_ok=False)
    venv.EnvBuilder(with_pip=True, system_site_packages=False, symlinks=False).create(root)
    binaries = root / ("Scripts" if os.name == "nt" else "bin")
    with github_path.open("a", encoding="utf-8") as target:
        target.write(str(binaries) + "\n")
    with github_env.open("a", encoding="utf-8") as target:
        target.write(f"VIRTUAL_ENV={root}\n")
        target.write("PYTHONPATH=\n")
        target.write("PYTHONNOUSERSITE=1\n")
    print("Created isolated CI interpreter for reviewed hashed dependency profiles.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Could not prepare isolated CI interpreter: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
