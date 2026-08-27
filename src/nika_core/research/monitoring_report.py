from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from nika_core.research.models import RefreshDisposition, ResearchEvidence, SourceKind
from nika_core.research.scheduled_profiles import ResearchProfileDelta

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|cookie|password|api[_-]?key|access[_-]?token|refresh[_-]?token|token)"
    r"\s*[:=]\s*[^\s,;]+"
)


def _required_line(value: str, field_name: str, *, max_length: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return normalized


def _optional_code(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_line(value, field_name, max_length=120)


def _timestamp(value: str, field_name: str) -> str:
    normalized = _required_line(value, field_name, max_length=80)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return normalized


def _safe_label(value: str) -> str:
    normalized = " ".join(value.split())
    normalized = normalized.replace("<", "‹").replace(">", "›")
    normalized = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", normalized)
    if len(normalized) > 180:
        return normalized[:177].rstrip() + "..."
    return normalized


@dataclass(frozen=True, slots=True)
class MonitoringSourceCheck:
    """UI-safe projection of one source outcome inside one monitoring cycle.

    Deliberately excludes raw response bodies, request headers, cookies, credentials,
    request/final URLs and free-form network error messages. Those remain in their
    canonical subsystem stores and are not copied into the user-facing report.
    """

    source_id: str
    source_kind: SourceKind
    disposition: RefreshDisposition
    attempts: int
    error_code: str | None = None
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required_line(self.source_id, "source_id"))
        if not isinstance(self.source_kind, SourceKind):
            raise TypeError("source_kind must be a SourceKind")
        if not isinstance(self.disposition, RefreshDisposition):
            raise TypeError("disposition must be a RefreshDisposition")
        if (
            not isinstance(self.attempts, int)
            or isinstance(self.attempts, bool)
            or self.attempts < 0
        ):
            raise ValueError("attempts must be a non-negative integer")
        object.__setattr__(self, "error_code", _optional_code(self.error_code, "error_code"))
        if self.snapshot_id is not None:
            object.__setattr__(
                self,
                "snapshot_id",
                _required_line(self.snapshot_id, "snapshot_id", max_length=160),
            )

    @property
    def retries(self) -> int:
        return max(self.attempts - 1, 0)


@dataclass(frozen=True, slots=True)
class MonitoringChange:
    """Compact normalized change reference; never carries page/document body text."""

    kind: str
    document_id: str
    title: str
    evidence: tuple[ResearchEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _required_line(self.kind, "kind", max_length=40))
        object.__setattr__(
            self,
            "document_id",
            _required_line(self.document_id, "document_id", max_length=160),
        )
        object.__setattr__(self, "title", _safe_label(_required_line(self.title, "title")))
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, ResearchEvidence) for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of ResearchEvidence")


@dataclass(frozen=True, slots=True)
class MonitoringCheck:
    """One canonical monitoring cycle as a read-only reporting projection."""

    check_id: str
    checked_at: str
    sources: tuple[MonitoringSourceCheck, ...]
    changes: tuple[MonitoringChange, ...]
    condition_matched: bool
    result_set_id: str | None = None
    previous_result_set_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _required_line(self.check_id, "check_id"))
        object.__setattr__(self, "checked_at", _timestamp(self.checked_at, "checked_at"))
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("sources must contain at least one MonitoringSourceCheck")
        if not all(isinstance(item, MonitoringSourceCheck) for item in self.sources):
            raise TypeError("sources must contain MonitoringSourceCheck values")
        if not isinstance(self.changes, tuple) or not all(
            isinstance(item, MonitoringChange) for item in self.changes
        ):
            raise TypeError("changes must be a tuple of MonitoringChange")
        if not isinstance(self.condition_matched, bool):
            raise TypeError("condition_matched must be a bool")
        if self.result_set_id is not None:
            object.__setattr__(
                self,
                "result_set_id",
                _required_line(self.result_set_id, "result_set_id", max_length=160),
            )
        if self.previous_result_set_id is not None:
            object.__setattr__(
                self,
                "previous_result_set_id",
                _required_line(
                    self.previous_result_set_id,
                    "previous_result_set_id",
                    max_length=160,
                ),
            )


@dataclass(frozen=True, slots=True)
class MonitoringReport:
    """Backend result contract for an accessible chronological monitoring report.

    `next_scheduled_check` and `terminal_reason` are snapshots supplied by the
    canonical monitoring controller. This type does not schedule, cancel or persist
    monitoring state and therefore cannot become a second runtime authority.
    """

    monitor_id: str
    checks: tuple[MonitoringCheck, ...]
    next_scheduled_check: str | None = None
    terminal_reason: str | None = None
    state_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "monitor_id", _required_line(self.monitor_id, "monitor_id"))
        if not isinstance(self.checks, tuple) or not all(
            isinstance(item, MonitoringCheck) for item in self.checks
        ):
            raise TypeError("checks must be a tuple of MonitoringCheck")
        if self.next_scheduled_check is not None:
            object.__setattr__(
                self,
                "next_scheduled_check",
                _timestamp(self.next_scheduled_check, "next_scheduled_check"),
            )
        object.__setattr__(
            self,
            "terminal_reason",
            _optional_code(self.terminal_reason, "terminal_reason"),
        )
        if self.state_reference is not None:
            object.__setattr__(
                self,
                "state_reference",
                _required_line(self.state_reference, "state_reference", max_length=160),
            )
        if self.terminal_reason is not None and self.next_scheduled_check is not None:
            raise ValueError("terminal monitoring report cannot have a next scheduled check")
        if (
            self.checks
            and self.checks[-1].condition_matched
            and self.next_scheduled_check is not None
        ):
            raise ValueError("matched condition cannot have a future scheduled check")
        previous: datetime | None = None
        for check in self.checks:
            current = datetime.fromisoformat(check.checked_at.replace("Z", "+00:00"))
            if previous is not None and current < previous:
                raise ValueError("checks must be ordered chronologically")
            previous = current


def changes_from_profile_delta(delta: ResearchProfileDelta | None) -> tuple[MonitoringChange, ...]:
    """Adapt the existing recurring Research delta without copying body/snippet content."""
    if delta is None:
        return ()
    if not isinstance(delta, ResearchProfileDelta):
        raise TypeError("delta must be a ResearchProfileDelta or None")
    return tuple(
        MonitoringChange(
            kind=delta_item.kind.value,
            document_id=delta_item.item.document_id,
            title=delta_item.item.title,
            evidence=delta_item.item.evidence,
        )
        for delta_item in delta.items
    )


def render_monitoring_report_text(report: MonitoringReport, *, max_checks: int = 20) -> str:
    """Render compact keyboard/screen-reader friendly text for existing UIResult.message."""
    if not isinstance(report, MonitoringReport):
        raise TypeError("report must be a MonitoringReport")
    if not isinstance(max_checks, int) or isinstance(max_checks, bool) or max_checks < 1:
        raise ValueError("max_checks must be a positive integer")

    checks = report.checks[-max_checks:]
    omitted = len(report.checks) - len(checks)
    lines = [
        "Monitoring report",
        f"Monitor: {report.monitor_id}",
        f"Checks recorded: {len(report.checks)}",
    ]
    if report.checks:
        lines.append(
            "Condition now: "
            + ("matched" if report.checks[-1].condition_matched else "not matched")
        )
    else:
        lines.append("Condition now: not checked yet")
    lines.append(f"Next scheduled check: {report.next_scheduled_check or 'none'}")
    lines.append(f"Terminal reason: {report.terminal_reason or 'none'}")
    if report.state_reference is not None:
        lines.append(f"State reference: {report.state_reference}")
    if omitted:
        lines.append(f"History: showing latest {len(checks)}; {omitted} earlier checks omitted")

    for position, check in enumerate(checks, start=len(report.checks) - len(checks) + 1):
        lines.extend(
            [
                "",
                f"Check {position}",
                f"Check reference: {check.check_id}",
                f"Check time: {check.checked_at}",
                "Condition: " + ("matched" if check.condition_matched else "not matched"),
                "Sources:",
            ]
        )
        for source in check.sources:
            source_line = (
                f"- {source.source_id} [{source.source_kind.value}]: "
                f"{_disposition_text(source.disposition)}; retries={source.retries}"
            )
            if source.error_code is not None:
                source_line += f"; error={source.error_code}"
            else:
                source_line += "; error=none"
            if source.snapshot_id is not None:
                source_line += f"; snapshot={source.snapshot_id}"
            lines.append(source_line)

        if check.changes:
            lines.append("What changed:")
            for change in check.changes:
                lines.append(f"- {change.kind}: {change.title} (document {change.document_id})")
                for evidence in change.evidence:
                    freshness = (
                        evidence.freshness.value if evidence.freshness is not None else "n/a"
                    )
                    lines.append(
                        "  Provenance: "
                        f"{evidence.source_kind.value} source={evidence.source_id}; "
                        f"observed={evidence.observed_at}; freshness={freshness}"
                    )
        else:
            lines.append("What changed: no normalized result change recorded")

        if check.previous_result_set_id is not None:
            lines.append(f"Previous result set: {check.previous_result_set_id}")
        if check.result_set_id is not None:
            lines.append(f"Result set: {check.result_set_id}")

    return "\n".join(lines).rstrip() + "\n"


def _disposition_text(disposition: RefreshDisposition) -> str:
    labels = {
        RefreshDisposition.CHANGED: "content changed",
        RefreshDisposition.NOT_MODIFIED: "not modified",
        RefreshDisposition.UNCHANGED: "no material change",
        RefreshDisposition.DYNAMIC_REQUIRED: "dynamic inspection required",
        RefreshDisposition.REMOVED: "source removed",
        RefreshDisposition.BLOCKED: "source blocked",
        RefreshDisposition.UNSUPPORTED: "source unsupported",
        RefreshDisposition.FAILED: "check failed",
    }
    return labels[disposition]
