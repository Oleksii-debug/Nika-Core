from __future__ import annotations

import argparse
from pathlib import Path

from nika_core.packaging.release import build_release_archive


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify the exact complete Windows release ZIP."
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--product-version", required=True)
    args = parser.parse_args()
    artifact = build_release_archive(
        args.bundle,
        args.artifact,
        source_sha=args.source_sha,
        expected_product_version=args.product_version,
    )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
