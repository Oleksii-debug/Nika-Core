from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from nika_core.config import AppConfig
from nika_core.diagnostics.health import HealthCheck, HealthReport, HealthService, HealthStatus
from nika_core.resources.contracts import ResourceObserverPort


def _resource_observer() -> ResourceObserverPort | None:
    try:
        from nika_core.resources import PsutilResourceObserver
    except Exception:
        # psutil is optional for the base install; import diagnostics are never echoed publicly.
        return None
    return PsutilResourceObserver()


def _configuration_failure_report() -> HealthReport:
    return HealthReport(
        generated_at=datetime.now(UTC),
        checks=(
            HealthCheck(
                check_id="configuration",
                status=HealthStatus.FAIL,
                summary="Typed application configuration could not be loaded safely.",
            ),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Nika Core health report")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stable machine-readable JSON instead of plain text",
    )
    args = parser.parse_args(argv)

    try:
        config = AppConfig.from_environment()
    except Exception:
        # Pydantic diagnostics can include raw environment values, so expose a stable message only.
        report = _configuration_failure_report()
    else:
        report = HealthService(config, resource_observer=_resource_observer()).run()

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(report.render_text())
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
