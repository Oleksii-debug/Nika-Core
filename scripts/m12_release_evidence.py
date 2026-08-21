from __future__ import annotations

import argparse
from pathlib import Path

from nika_core.packaging.release import verify_distributable_evidence


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify that M12 evidence binds the exact final distributable ZIP."
    )
    result.add_argument("--artifact", type=Path, required=True)
    result.add_argument("--evidence", type=Path, required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--artifact-reference", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    findings = verify_distributable_evidence(
        args.artifact,
        args.evidence,
        source_sha=args.source_sha,
        artifact_reference=args.artifact_reference,
    )
    if findings:
        raise SystemExit("M12 distributable evidence verification failed: " + ", ".join(findings))
    print("M12 distributable evidence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
