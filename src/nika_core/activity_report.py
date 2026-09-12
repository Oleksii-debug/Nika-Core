from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot


@dataclass(frozen=True, slots=True)
class ActivityCount:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class DailyActivityReport:
    """Read-only daily projection over canonical durable runtime evidence."""

    window_start: datetime
    window_end: datetime
    task_transitions: tuple[ActivityCount, ...]
    audit_events: tuple[ActivityCount, ...]
    experiment_transitions: tuple[ActivityCount, ...]
    research_documents_added: int
    memory_records_updated: int
    resource_snapshot: ResourceSnapshot | None
    limitations: tuple[str, ...]

    def render_text(self) -> str:
        lines = [
            f"Звіт діяльності Nika: {self.window_start.isoformat()} — {self.window_end.isoformat()}",
            f"Переходи завдань: {_format_counts(self.task_transitions)}",
            f"Події аудиту: {_format_counts(self.audit_events)}",
            f"Переходи експериментів: {_format_counts(self.experiment_transitions)}",
            f"Додано дослідницьких документів: {self.research_documents_added}",
            f"Оновлено записів пам'яті: {self.memory_records_updated}",
        ]
        if self.resource_snapshot is None:
            lines.append("Ресурси: поточний знімок не надано.")
        else:
            snapshot = self.resource_snapshot
            resource_text = (
                f"CPU {snapshot.cpu_percent:.1f}%; RAM {snapshot.memory_percent:.1f}%; "
                f"доступна RAM {snapshot.available_memory_bytes} байт"
            )
            if snapshot.battery_percent is not None:
                resource_text += f"; батарея {snapshot.battery_percent:.1f}%"
            if snapshot.power_plugged is not None:
                resource_text += "; живлення від мережі: " + (
                    "так" if snapshot.power_plugged else "ні"
                )
            lines.append(f"Ресурси на момент звіту: {resource_text}.")
        lines.append("Обмеження доказовості:")
        lines.extend(f"- {item}" for item in self.limitations)
        return "\n".join(lines)


class DailyActivityReportService:
    """Build truthful reports without creating another journal, queue, or database."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        resource_observer: ResourceObserverPort | None = None,
    ) -> None:
        self._store = store
        self._resource_observer = resource_observer

    def build_utc_day(self, day: date) -> DailyActivityReport:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return self.build_window(start=start, end=start + timedelta(days=1))

    def build_window(self, *, start: datetime, end: datetime) -> DailyActivityReport:
        start_utc = _require_aware_utc(start, field="start")
        end_utc = _require_aware_utc(end, field="end")
        if end_utc <= start_utc:
            raise ValueError("end must be later than start")

        start_iso = start_utc.isoformat()
        end_iso = end_utc.isoformat()
        with self._store.connection() as conn:
            task_transitions = _grouped_counts(
                conn.execute(
                    "SELECT new_state AS value, COUNT(*) AS count "
                    "FROM task_events WHERE created_at >= ? AND created_at < ? "
                    "GROUP BY new_state ORDER BY new_state",
                    (start_iso, end_iso),
                ).fetchall()
            )
            audit_events = _grouped_counts(
                conn.execute(
                    "SELECT event_type AS value, COUNT(*) AS count "
                    "FROM audit_events WHERE created_at >= ? AND created_at < ? "
                    "GROUP BY event_type ORDER BY event_type",
                    (start_iso, end_iso),
                ).fetchall()
            )
            experiment_transitions = _grouped_counts(
                conn.execute(
                    "SELECT new_status AS value, COUNT(*) AS count "
                    "FROM experiment_events WHERE created_at >= ? AND created_at < ? "
                    "GROUP BY new_status ORDER BY new_status",
                    (start_iso, end_iso),
                ).fetchall()
            )
            research_documents_added = _scalar_count(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM corpus_documents "
                    "WHERE created_at >= ? AND created_at < ?",
                    (start_iso, end_iso),
                ).fetchone()
            )
            memory_records_updated = _scalar_count(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM memory_records "
                    "WHERE updated_at >= ? AND updated_at < ?",
                    (start_iso, end_iso),
                ).fetchone()
            )

        snapshot = None
        if self._resource_observer is not None:
            snapshot = self._resource_observer.snapshot()

        return DailyActivityReport(
            window_start=start_utc,
            window_end=end_utc,
            task_transitions=task_transitions,
            audit_events=audit_events,
            experiment_transitions=experiment_transitions,
            research_documents_added=research_documents_added,
            memory_records_updated=memory_records_updated,
            resource_snapshot=snapshot,
            limitations=(
                "Включено лише канонічні durable-записи, що існують у локальній БД.",
                "Вміст task payload, audit payload, пам'яті та дослідницьких документів "
                "навмисно не виводиться.",
                "Історичні CPU/RAM, API token usage і вартість не вигадуються; ресурсний "
                "знімок, якщо є, відображає лише момент побудови звіту.",
                "Семантичне твердження про те, що саме Nika вивчила, потребує окремого "
                "перевіреного learning evidence і тут не синтезується.",
            ),
        )


def _require_aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _grouped_counts(rows: list[object]) -> tuple[ActivityCount, ...]:
    return tuple(
        ActivityCount(value=str(row["value"]), count=int(row["count"]))  # type: ignore[index]
        for row in rows
    )


def _scalar_count(row: object | None) -> int:
    if row is None:
        return 0
    return int(row["count"])  # type: ignore[index]


def _format_counts(items: tuple[ActivityCount, ...]) -> str:
    if not items:
        return "немає зафіксованих подій"
    return ", ".join(f"{_safe_label(item.value)}={item.count}" for item in items)


def _safe_label(value: str) -> str:
    collapsed = " ".join(value.split())
    if not collapsed:
        return "[порожня назва]"
    if len(collapsed) <= 120:
        return collapsed
    return collapsed[:117] + "..."


__all__ = ["ActivityCount", "DailyActivityReport", "DailyActivityReportService"]
