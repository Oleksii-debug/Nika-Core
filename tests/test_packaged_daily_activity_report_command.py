from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from nika_core.activity_report import DailyActivityReportService
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    packaged_daily_activity_report_command,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult
from scripts.nika_windows import _daily_activity_report_result

ROOT = Path(__file__).resolve().parents[1]


class _OrdinaryHandler:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        self.calls.append(payload)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary-task",
            focus_id="tasks-heading",
        )


class _FailingReportService:
    def build_utc_day(self, day: date):
        del day
        raise RuntimeError("C:\\private\\secret.db provider-token-must-not-escape")


def _task_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
    assert row is not None
    return int(row["count"])


@pytest.mark.parametrize(
    "command",
    (
        "daily activity report",
        "Show daily activity report.",
        "Nika daily activity report",
        "щоденний звіт активності",
        "Покажи щоденний звіт активності!",
        "звіт діяльності за сьогодні",
        "Покажи звіт діяльності за сьогодні:",
    ),
)
def test_daily_activity_report_recognizer_accepts_only_explicit_aliases(command: str) -> None:
    assert packaged_daily_activity_report_command(command) is True


@pytest.mark.parametrize(
    "command",
    (
        "report",
        "покажи звіт",
        "Підготуй звіт про цей текст",
        "Create report application",
        "daily report for another workspace",
    ),
)
def test_daily_activity_report_recognizer_does_not_capture_generic_report_text(
    command: str,
) -> None:
    assert packaged_daily_activity_report_command(command) is False


def test_packaged_daily_report_is_read_only_and_bypasses_ordinary_task(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "daily report.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    ordinary = _OrdinaryHandler()
    service = DailyActivityReportService(store)
    fixed_day = date(2026, 10, 5)
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
        activity_report_handler=lambda: _daily_activity_report_result(
            service,
            day_provider=lambda: fixed_day,
        ),
    )
    command = "Покажи щоденний звіт активності"

    assert _task_count(store) == 0
    result = router.create({"command": command})

    assert result.status == "completed"
    assert result.focus_id == "logs-heading"
    assert "Звіт діяльності Nika: 2026-10-05T00:00:00+00:00" in result.message
    assert "Переходи завдань:" in result.message
    assert "Обмеження доказовості:" in result.message
    assert ordinary.calls == []
    assert router.active_project_id is None
    assert _task_count(store) == 0
    with pytest.raises(KeyError):
        repository.get(product_project_identity(command))


def test_daily_report_preserves_current_product_project_selection(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "selected project report.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    ordinary = _OrdinaryHandler()
    service = DailyActivityReportService(store)
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
        activity_report_handler=lambda: _daily_activity_report_result(
            service,
            day_provider=lambda: date(2026, 10, 5),
        ),
    )
    product_command = "Створи застосунок для доступного читання звітів"
    project_id = product_project_identity(product_command)
    created = router.create({"command": product_command})

    assert created.status == "completed"
    assert router.active_project_id == project_id
    before = repository.get(project_id)

    report = router.create({"command": "Покажи щоденний звіт активності"})
    after = repository.get(project_id)

    assert report.status == "completed"
    assert report.focus_id == "logs-heading"
    assert router.active_project_id == project_id
    assert after == before
    assert ordinary.calls == []
    assert _task_count(store) == 0


def test_explicit_daily_report_fails_closed_when_composition_does_not_bind_handler(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "missing handler.db")
    store.initialize()
    ordinary = _OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
    )

    with pytest.raises(PackagedProductJourneyError, match="звіт активності недоступний"):
        router.create({"command": "щоденний звіт активності"})

    assert ordinary.calls == []
    assert _task_count(store) == 0


def test_generic_report_command_remains_an_ordinary_agent_task(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ordinary report.db")
    store.initialize()
    ordinary = _OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
        activity_report_handler=lambda: pytest.fail(
            "generic report text must not invoke the daily report handler"
        ),
    )
    payload = {"command": "Підготуй звіт про цей текст"}

    result = router.create(payload)

    assert result.message == "ordinary-task"
    assert ordinary.calls == [payload]
    assert router.active_project_id is None


def test_packaged_report_failure_is_bounded_and_does_not_expose_exception_detail() -> None:
    result = _daily_activity_report_result(
        _FailingReportService(),  # type: ignore[arg-type]
        day_provider=lambda: date(2026, 10, 5),
    )

    assert result.status == "failed"
    assert result.focus_id == "logs-heading"
    assert result.message == "Не вдалося сформувати щоденний звіт активності."
    assert "secret" not in result.message.casefold()
    assert "token" not in result.message.casefold()
    assert "private" not in result.message.casefold()


def test_packaged_report_rejects_datetime_subclass_as_day_provider() -> None:
    result = _daily_activity_report_result(
        _FailingReportService(),  # type: ignore[arg-type]
        day_provider=lambda: datetime(2026, 10, 5, tzinfo=UTC),  # type: ignore[return-value]
    )

    assert result.status == "failed"
    assert result.message == "Не вдалося сформувати щоденний звіт активності."


def test_windows_composition_wires_canonical_activity_report_service() -> None:
    source = (ROOT / "scripts" / "nika_windows.py").read_text(encoding="utf-8")

    assert "DailyActivityReportService(store)" in source
    assert "activity_report_handler=lambda: _daily_activity_report_result(" in source
    assert "day_provider=activity_report_day" in source
