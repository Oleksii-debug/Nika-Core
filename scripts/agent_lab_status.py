from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from nika_core.agent_lab_status import AgentLabStatusReader
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read bounded operational status from the Nika Core Agent Lab database."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of screen-reader-friendly text",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="maximum recent teams and experiments to include (1-200)",
    )
    args = parser.parse_args(argv)
    config = AppConfig.from_environment()
    try:
        snapshot = AgentLabStatusReader(
            SQLiteStore(config.database_path), limit=args.limit
        ).snapshot()
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as exc:
        print(f"Agent Lab status unavailable: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(snapshot.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(snapshot.accessible_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
