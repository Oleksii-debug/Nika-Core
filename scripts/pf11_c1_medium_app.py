from __future__ import annotations

import argparse
from pathlib import Path

from nika_core.product_factory_c1_acceptance import (
    C1MediumAppAcceptanceRunner,
    write_c1_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--windows-package", action="store_true")
    args = parser.parse_args()

    evidence = C1MediumAppAcceptanceRunner(
        root=args.root,
        source_sha=args.source_sha,
    ).run(build_windows_package=args.windows_package)
    target = write_c1_evidence(args.evidence, evidence)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
