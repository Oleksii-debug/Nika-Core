from __future__ import annotations

import argparse

from nika_core.diagnostics import CheckStatus, collect_diagnostics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Nika Core configuration and database diagnostics."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON object instead of accessible text.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return a non-zero exit code for warnings as well as failures.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = collect_diagnostics()
    print(report.to_json() if args.json else report.to_text())
    if report.status is CheckStatus.FAIL:
        return 2
    if args.strict and report.status is CheckStatus.WARN:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
