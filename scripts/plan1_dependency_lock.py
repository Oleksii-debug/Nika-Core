"""Maintain reviewed pip exports of uv's resolved dependency graph.

uv is a development-only resolver. CI installs the checked-in hashed exports
with pip; it never resolves dependencies or installs uv to regenerate evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

PROFILES = {
    "core": ("dev", "agent", "planning"),
    "m12-ubuntu": ("dev", "agent", "planning", "browser"),
    "m12-windows": ("dev", "agent", "planning", "windows-interaction"),
    "packaging": ("gui", "qa", "dev"),
}
INPUTS = ("pyproject.toml", "docs/PLAN1_DEPENDENCY_AUTHORITY.json")
OUTPUTS = ("uv.lock", "requirements/build.txt", *(f"requirements/{p}.txt" for p in PROFILES))
MANIFEST = "requirements/manifest.json"
RESOLVER = "uv 0.13.6"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_lock(root: Path) -> None:
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("resolver") != RESOLVER
        or manifest.get("profiles") != {k: list(v) for k, v in PROFILES.items()}
        or set(manifest.get("sha256", {})) != {*INPUTS, *OUTPUTS}
    ):
        raise ValueError("Dependency lock inventory differs from the reviewed profiles")
    for name in (*INPUTS, *OUTPUTS):
        if digest(root / name) != manifest["sha256"][name]:
            raise ValueError(f"Stale or modified dependency lock input: {name}")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    if lock["requires-python"].replace(" ", "") != project["project"]["requires-python"]:
        raise ValueError("Dependency lock Python range differs from the project")


def refresh_lock(root: Path) -> None:
    version = subprocess.run(
        ["uv", "--version"], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip().split(" (")[0]
    if version != RESOLVER:
        raise ValueError(f"Use the reviewed resolver {RESOLVER}, found {version}")

    def run(arguments: list[str], *, source: str | None = None) -> None:
        subprocess.run(
            ["uv", *arguments], cwd=root, input=source, text=True, check=True, timeout=180,
            stdout=subprocess.DEVNULL,
        )

    (root / "requirements").mkdir(exist_ok=True)
    run(["lock", "--default-index", "https://pypi.org/simple"])
    for name, extras in PROFILES.items():
        options = [value for extra in extras for value in ("--extra", extra)]
        run([
            "export", "--locked", "--no-emit-project", "--no-header", *options,
            "-o", f"requirements/{name}.txt",
        ])
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    run([
        "pip", "compile", "-", "--universal", "--python-version", "3.12",
        "--default-index", "https://pypi.org/simple", "--generate-hashes", "--no-header",
        "-o", "requirements/build.txt",
    ], source="\n".join(project["build-system"]["requires"]) + "\n")
    manifest = {
        "schema_version": 1,
        "resolver": RESOLVER,
        "profiles": {k: list(v) for k, v in PROFILES.items()},
        "sha256": {name: digest(root / name) for name in (*INPUTS, *OUTPUTS)},
    }
    (root / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    verify_lock(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        if args.refresh:
            refresh_lock(args.root)
        else:
            verify_lock(args.root)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Dependency lock verification failed: {exc}", file=sys.stderr)
        return 1
    print("Dependency lock inputs and pip profiles verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
