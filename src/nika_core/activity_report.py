from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot

_MAX_SIGNED_64 = (1 << 63) - 1
_MAX_GROUPED_ACTIVITY_ITEMS = 20
_MAX_GROUP_LABEL_UTF8_BYTES = 4096
_GROUPED_QUERY_LIMIT = _MAX_GROUPED_ACTIVITY_ITEMS + 1


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
    memory_update_events: int
    resource_snapshot: ResourceSnapshot | None
    limitations: tuple[str, ...]

    def render_text(self) -> str:
        lines = [
            f"Звіт діяльності Nika: {self.window_start.isoformat()} — {self.window_end.isoformat()}",
            f"Переходи завдань: {_format_counts(self.task_transitions)}",
            f"Події аудиту: {_format_counts(self.audit_events)}",
            f"Переходи експериментів: {_format_counts(self.experiment_transitions)}",
            f"Додано дослідницьких документів: {self.research_documents_added}",
            f"Події оновлення пам'яті: {self.memory_update_events}",
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
        if type(day) is not date:
            raise TypeError("day must be a built-in date")
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return self.build_window(start=start, end=start + timedelta(days=1))

    def build_window(self, *, start: datetime, end: datetime) -> DailyActivityReport:
        start_utc = _require_aware_utc(start, field="start")
        end_utc = _require_aware_utc(end, field="end")
        if end_utc <= start_utc:
            raise ValueError("end must be later than start")

        start_iso = start_utc.isoformat()
        end_iso = end_utc.isoformat()
        if not self._store.path.is_file():
            raise FileNotFoundError(f"Nika database does not exist: {self._store.path}")
        with self._store.connection() as conn:
            conn.execute("PRAGMA query_only = ON")
            # sqlite3 SELECT statements do not start a durable multi-statement transaction.
            # Pin every projection below to one snapshot so concurrent durable writes cannot
            # produce a report assembled from different database states.
            conn.execute("BEGIN")
            _validate_group_label_storage(
                conn.execute(
                    "SELECT typeof(new_state) AS storage_type, "
                    "length(CAST(new_state AS BLOB)) AS byte_count "
                    "FROM task_events WHERE created_at >= ? AND created_at < ? "
                    "AND (typeof(new_state) <> 'text' "
                    "OR length(CAST(new_state AS BLOB)) > ?) LIMIT 1",
                    (start_iso, end_iso, _MAX_GROUP_LABEL_UTF8_BYTES),
                ).fetchone()
            )
            _validate_group_label_storage(
                conn.execute(
                    "SELECT typeof(event_type) AS storage_type, "
                    "length(CAST(event_type AS BLOB)) AS byte_count "
                    "FROM audit_events WHERE created_at >= ? AND created_at < ? "
                    "AND (typeof(event_type) <> 'text' "
                    "OR length(CAST(event_type AS BLOB)) > ?) LIMIT 1",
                    (start_iso, end_iso, _MAX_GROUP_LABEL_UTF8_BYTES),
                ).fetchone()
            )
            _validate_group_label_storage(
                conn.execute(
                    "SELECT typeof(new_status) AS storage_type, "
                    "length(CAST(new_status AS BLOB)) AS byte_count "
                    "FROM experiment_events WHERE created_at >= ? AND created_at < ? "
                    "AND (typeof(new_status) <> 'text' "
                    "OR length(CAST(new_status AS BLOB)) > ?) LIMIT 1",
                    (start_iso, end_iso, _MAX_GROUP_LABEL_UTF8_BYTES),
                ).fetchone()
            )
            task_transitions, task_transitions_truncated = _grouped_counts(
                conn.execute(
                    "SELECT new_state AS value, COUNT(*) AS count "
                    "FROM task_events WHERE created_at >= ? AND created_at < ? "
                    "GROUP BY new_state ORDER BY new_state LIMIT ?",
                    (start_iso, end_iso, _GROUPED_QUERY_LIMIT),
                ).fetchall()
            )
            audit_events, audit_events_truncated = _grouped_counts(
                conn.execute(
                    "SELECT event_type AS value, COUNT(*) AS count "
                    "FROM audit_events WHERE created_at >= ? AND created_at < ? "
                    "GROUP BY event_type ORDER BY event_type LIMIT ?",
                    (start_iso, end_iso, _GROUPED_QUERY_LIMIT),
                ).fetchall()
            )
            experiment_transitions, experiment_transitions_truncated = _grouped_counts(
                conn.execute(
                    "SELECT new_status AS value, COUNT(*) AS count "
                    "FROM experiment_events WHERE created_at >= ? AND created_at < ? "
                    "GROUP BY new_status ORDER BY new_status LIMIT ?",
                    (start_iso, end_iso, _GROUPED_QUERY_LIMIT),
                ).fetchall()
            )
            research_documents_added = _scalar_count(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM corpus_documents "
                    "WHERE created_at >= ? AND created_at < ?",
                    (start_iso, end_iso),
                ).fetchone()
            )
            memory_update_events = _scalar_count(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM audit_events "
                    "WHERE event_type = ? AND created_at >= ? AND created_at < ?",
                    ("memory.upserted", start_iso, end_iso),
                ).fetchone()
            )

        snapshot = None
        if self._resource_observer is not None:
            snapshot = _observe_resource_snapshot(self._resource_observer)

        limitations = [
            "Включено лише канонічні durable-записи, що існують у локальній БД.",
            (
                "Вміст task payload, audit payload, пам'яті та дослідницьких документів "
                "навмисно не виводиться."
            ),
            (
                "Історичні CPU/RAM, API token usage і вартість не вигадуються; ресурсний "
                "знімок, якщо є, відображає лише момент побудови звіту."
            ),
            (
                "Семантичне твердження про те, що саме Nika вивчила, потребує окремого "
                "перевіреного learning evidence і тут не синтезується."
            ),
        ]
        truncated_sections = tuple(
            label
            for label, truncated in (
                ("переходи завдань", task_transitions_truncated),
                ("події аудиту", audit_events_truncated),
                ("переходи експериментів", experiment_transitions_truncated),
            )
            if truncated
        )
        if truncated_sections:
            limitations.append(
                "Для доступного читання в кожному групованому розділі показано не більше "
                f"{_MAX_GROUPED_ACTIVITY_ITEMS} категорій у стабільному порядку; "
                "додаткові категорії існують, але не деталізовані: "
                + ", ".join(truncated_sections)
                + "."
            )

        return DailyActivityReport(
            window_start=start_utc,
            window_end=end_utc,
            task_transitions=task_transitions,
            audit_events=audit_events,
            experiment_transitions=experiment_transitions,
            research_documents_added=research_documents_added,
            memory_update_events=memory_update_events,
            resource_snapshot=snapshot,
            limitations=tuple(limitations),
        )


def _require_aware_utc(value: datetime, *, field: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{field} must be a built-in datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _observe_resource_snapshot(
    observer: ResourceObserverPort,
) -> ResourceSnapshot | None:
    try:
        observed = observer.snapshot()
    except Exception:  # noqa: BLE001
        return None
    return _validated_resource_snapshot(observed)


def _validated_resource_snapshot(value: object) -> ResourceSnapshot | None:
    if type(value) is not ResourceSnapshot:
        return None

    cpu_percent = value.cpu_percent
    memory_percent = value.memory_percent
    available_memory_bytes = value.available_memory_bytes
    logical_cpu_count = value.logical_cpu_count
    total_memory_bytes = value.total_memory_bytes
    process_rss_bytes = value.process_rss_bytes
    battery_percent = value.battery_percent
    power_plugged = value.power_plugged

    if not _valid_percentage(cpu_percent):
        return None
    if not _valid_percentage(memory_percent):
        return None
    if not _valid_nonnegative_int(available_memory_bytes):
        return None
    if not _valid_optional_nonnegative_int(logical_cpu_count):
        return None
    if not _valid_optional_nonnegative_int(total_memory_bytes):
        return None
    if not _valid_optional_nonnegative_int(process_rss_bytes):
        return None
    if battery_percent is not None and not _valid_percentage(battery_percent):
        return None
    if power_plugged is not None and type(power_plugged) is not bool:
        return None

    return ResourceSnapshot(
        cpu_percent=float(cpu_percent),
        memory_percent=float(memory_percent),
        available_memory_bytes=available_memory_bytes,
        logical_cpu_count=logical_cpu_count,
        total_memory_bytes=total_memory_bytes,
        process_rss_bytes=process_rss_bytes,
        battery_percent=None if battery_percent is None else float(battery_percent),
        power_plugged=power_plugged,
    )


def _valid_percentage(value: object) -> bool:
    if type(value) is not int and type(value) is not float:
        return False
    try:
        number = float(value)
    except OverflowError:
        return False
    return math.isfinite(number) and 0.0 <= number <= 100.0


def _valid_nonnegative_int(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_SIGNED_64


def _valid_optional_nonnegative_int(value: object) -> bool:
    return value is None or _valid_nonnegative_int(value)


def _validate_group_label_storage(row: object | None) -> None:
    if row is None:
        return
    storage_type = row["storage_type"]  # type: ignore[index]
    byte_count = row["byte_count"]  # type: ignore[index]
    if storage_type != "text":
        raise ValueError("grouped activity label must use SQLite TEXT storage")
    if (
        type(byte_count) is not int
        or byte_count < 0
        or byte_count > _MAX_GROUP_LABEL_UTF8_BYTES
    ):
        raise ValueError("grouped activity label exceeds safe UTF-8 storage bound")


def _grouped_counts(
    rows: list[object],
) -> tuple[tuple[ActivityCount, ...], bool]:
    truncated = len(rows) > _MAX_GROUPED_ACTIVITY_ITEMS
    selected = rows[:_MAX_GROUPED_ACTIVITY_ITEMS]
    counts: list[ActivityCount] = []
    for row in selected:
        value = row["value"]  # type: ignore[index]
        count = row["count"]  # type: ignore[index]
        if type(value) is not str:
            raise ValueError("grouped activity label must use SQLite TEXT storage")
        if type(count) is not int or not 1 <= count <= _MAX_SIGNED_64:
            raise ValueError("grouped activity count must be a positive SQLite integer")
        counts.append(ActivityCount(value=value, count=count))
    return tuple(counts), truncated


def _scalar_count(row: object | None) -> int:
    if row is None:
        return 0
    return int(row["count"])  # type: ignore[index]


def _format_counts(items: tuple[ActivityCount, ...]) -> str:
    if not items:
        return "немає зафіксованих подій"
    return ", ".join(f"{_safe_label(item.value)}={item.count}" for item in items)


def _safe_label(value: str) -> str:
    control_safe = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in value
    )
    collapsed = " ".join(control_safe.split())
    if not collapsed:
        return "[порожня назва]"
    if len(collapsed) <= 120:
        return collapsed
    return collapsed[:117] + "..."


__all__ = ["ActivityCount", "DailyActivityReport", "DailyActivityReportService"]
