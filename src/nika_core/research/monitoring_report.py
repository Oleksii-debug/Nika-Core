from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from nika_core.research.models import RefreshDisposition, ResearchEvidence, SourceKind
from nika_core.research.scheduled_profiles import ResearchProfileDelta

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\\b(password|passwd|client[_-]?secret|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token)\\s*[:=]\\s*(?:\"[^\"\\r\\n]*\"|'[^'\\r\\n]*'|[^\\s,;]+)"
)
_HEADER_SECRET = re.compile(
    r"(?i)\\b(authorization|proxy-authorization|cookie|set-cookie)\\s*[:=]\\s*[^\\r\\n]*"
)
_SAFE_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\\Z")
_SAFE_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+~-]{0,159}\\Z")
_SENSITIVE_REFERENCE_RE = re.compile(
    r"(?i)^(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|"
    r"client[_-]?secret|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token):"
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
    normalized = _required_line(value, field_name, max_length=120)
    if _SAFE_CODE_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a bounded safe code")
    return normalized


def _safe_reference(
    value: str,
    field_name: str,
    *,
    max_length: int = 160,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or len(value) > max_length or any(character.isspace() for character in value):
        raise ValueError(f"{field_name} must be a bounded safe reference")
    if (
        _SAFE_REFERENCE_RE.fullmatch(value) is None
        or "://" in value
        or _SENSITIVE_REFERENCE_RE.search(value) is not None
    ):
        raise ValueError(f"{field_name} must be a bounded safe reference")
    return value


def _timestamp(value: str, field_name: str) -> str:
    normalized = _required_line(value, field_name, max_length=80)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return normalized


def _safe_label(value: str) -> str:
    normalized = " ".join(value.split())
    normalized = normalized.replace("<", "‹").replace(">", "›")
    normalized = _HEADER_SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", normalized)
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
        object.__setattr__(self, "source_id", _safe_reference(self.source_id, "source_id"))
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
                _safe_reference(self.snapshot_id, "snapshot_id"),
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
            _safe_reference(self.document_id, "document_id"),
        )
        object.__setattr__(self, "title", _safe_label(_required_line(self.title, "title")))
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, ResearchEvidence) for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of ResearchEvidence")
        for evidence in self.evidence:
            _safe_reference(evidence.source_id, "evidence.source_id")
            _timestamp(evidence.observed_at, "evidence.observed_at")


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
    next_scheduled_check: str | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _safe_reference(self.check_id, "check_id"))
        object.__setattr__(self, "checked_at", _timestamp(self.checked_at, "checked_at"))
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("sources must contain at least one MonitoringSourceCheck")
        if not all(isinstance(item, MonitoringSourceCheck) for item in self.sources):
            raise TypeError("sources must contain MonitoringSourceCheck values")
        source_keys = tuple((item.source_kind.value, item.source_id) for item in self.sources)
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("sources must not contain duplicate stable identities")
        object.__setattr__(
            self,
            "sources",
            tuple(sorted(self.sources, key=lambda item: (item.source_kind.value, item.source_id))),
        )
        if not isinstance(self.changes, tuple) or not all(
            isinstance(item, MonitoringChange) for item in self.changes
        ):
            raise TypeError("changes must be a tuple of MonitoringChange")
        allowed_source_keys = set(source_keys)
        for change in self.changes:
            for evidence in change.evidence:
                if (evidence.source_kind.value, evidence.source_id) not in allowed_source_keys:
                    raise ValueError(
                        "change evidence source identity is not part of this monitoring check"
                    )
        if not isinstance(self.condition_matched, bool):
            raise TypeError("condition_matched must be a bool")
        if self.result_set_id is not None:
            object.__setattr__(
                self,
                "result_set_id",
                _safe_reference(self.result_set_id, "result_set_id"),
            )
        if self.previous_result_set_id is not None:
            object.__setattr__(
                self,
                "previous_result_set_id",
                _safe_reference(
                    self.previous_result_set_id,
                    "previous_result_set_id",
                ),
            )
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
        if self.terminal_reason is not None and self.next_scheduled_check is not None:
            raise ValueError("terminal monitoring check cannot have a next scheduled check")
        if self.condition_matched and self.next_scheduled_check is not None:
            raise ValueError("matched condition cannot have a future scheduled check")

    @property
    def safe_evidence_summary(self) -> str:
        provenance_count = sum(len(change.evidence) for change in self.changes)
        return (
            f"{len(self.changes)} normalized change reference(s); "
            f"{provenance_count} provenance reference(s); "
            f"{len(self.sources)} source outcome(s)"
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
        object.__setattr__(self, "monitor_id", _safe_reference(self.monitor_id, "monitor_id"))
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
                _safe_reference(self.state_reference, "state_reference"),
            )
        if self.terminal_reason is not None and self.next_scheduled_check is not None:
            raise ValueError("terminal monitoring report cannot have a next scheduled check")
        if self.checks:
            latest = self.checks[-1]
            if (
                self.next_scheduled_check is not None
                and self.next_scheduled_check != latest.next_scheduled_check
            ):
                raise ValueError("report next scheduled check contradicts latest check snapshot")
            if self.terminal_reason is not None and self.terminal_reason != latest.terminal_reason:
                raise ValueError("report terminal reason contradicts latest check snapshot")
            if latest.condition_matched and self.next_scheduled_check is not None:
                raise ValueError("matched condition cannot have a future scheduled check")
        previous: datetime | None = None
        for check in self.checks:
            current = datetime.fromisoformat(check.checked_at)
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
    latest = report.checks[-1] if report.checks else None
    if latest is not None:
        lines.append(
            "Condition now: " + ("matched" if latest.condition_matched else "not matched")
        )
    else:
        lines.append("Condition now: not checked yet")
    effective_next = (
        latest.next_scheduled_check if latest is not None else report.next_scheduled_check
    )
    effective_terminal = latest.terminal_reason if latest is not None else report.terminal_reason
    lines.append(f"Next scheduled check: {effective_next or 'none'}")
    lines.append(f"Terminal reason: {effective_terminal or 'none'}")
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
                f"Next scheduled check: {check.next_scheduled_check or 'none'}",
                f"Terminal reason: {check.terminal_reason or 'none'}",
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

        lines.append(f"Evidence summary: {check.safe_evidence_summary}")

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
