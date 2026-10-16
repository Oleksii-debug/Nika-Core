from __future__ import annotations

import re
import unicodedata

PACKAGED_PF11_EVIDENCE_KEYS = frozenset(
    {
        "route",
        "project_id",
        "spec_version",
        "state",
        "command_center_state_proven",
        "current_command_proven",
        "current_command_focus_proven",
        "bridge_state_project_id",
        "bridge_state_spec_version",
        "bridge_state_status_count",
        "bridge_state_decision_count",
        "restart_selection_integrity_proven",
        "bounded_projection_proven",
        "human_tested",
        "nvda_verified",
        "production_release_ready",
    }
)

_REQUIRED_TRUE_FIELDS = (
    "command_center_state_proven",
    "current_command_proven",
    "current_command_focus_proven",
    "restart_selection_integrity_proven",
    "bounded_projection_proven",
)
_REQUIRED_FALSE_FIELDS = (
    "human_tested",
    "nvda_verified",
    "production_release_ready",
)
_PRODUCT_PROJECT_ID_RE = re.compile(r"^product-[0-9a-f]{64}$")


class PackagedPF11EvidenceError(RuntimeError):
    """Raised when packaged PF11 evidence crosses the release boundary invalidly."""


def _require_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value or value != value.strip():
        raise PackagedPF11EvidenceError(
            f"packaged PF11 proof returned invalid {field}"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PackagedPF11EvidenceError(
            f"packaged PF11 proof returned invalid {field}"
        ) from exc
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in value
    ):
        raise PackagedPF11EvidenceError(
            f"packaged PF11 proof returned invalid {field}"
        )
    return value


def _require_exact_int(
    payload: dict[str, object],
    field: str,
    expected: int,
) -> int:
    value = payload.get(field)
    if type(value) is not int or value != expected:
        raise PackagedPF11EvidenceError(
            f"packaged PF11 proof returned invalid {field}"
        )
    return value


def _require_nonnegative_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise PackagedPF11EvidenceError(
            f"packaged PF11 proof returned invalid {field}"
        )
    return value


def require_packaged_pf11_evidence(payload: object) -> dict[str, object]:
    """Admit the exact raw evidence emitted by nika_windows --pf11-proof.

    This is a semantic boundary only. Callers remain responsible for safely reading
    bytes from their stage-specific filesystem/process boundary before invoking it.
    """
    if type(payload) is not dict:
        raise PackagedPF11EvidenceError(
            "packaged PF11 proof evidence must be a JSON object"
        )
    keys = frozenset(payload)
    if keys != PACKAGED_PF11_EVIDENCE_KEYS:
        missing = sorted(PACKAGED_PF11_EVIDENCE_KEYS - keys)
        unexpected = sorted(keys - PACKAGED_PF11_EVIDENCE_KEYS)
        raise PackagedPF11EvidenceError(
            "packaged PF11 proof evidence schema mismatch: "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )

    route = _require_text(payload, "route")
    project_id = _require_text(payload, "project_id")
    state = _require_text(payload, "state")
    _require_exact_int(payload, "spec_version", 1)
    bridge_project_id = _require_text(payload, "bridge_state_project_id")
    _require_exact_int(payload, "bridge_state_spec_version", 1)
    _require_nonnegative_int(payload, "bridge_state_status_count")
    _require_nonnegative_int(payload, "bridge_state_decision_count")

    if route != "product_project":
        raise PackagedPF11EvidenceError(
            "packaged PF11 ProductProject proof returned invalid route evidence"
        )
    if state != "active":
        raise PackagedPF11EvidenceError(
            "packaged PF11 proof returned invalid state"
        )
    if _PRODUCT_PROJECT_ID_RE.fullmatch(project_id) is None:
        raise PackagedPF11EvidenceError(
            "packaged PF11 proof returned a non-canonical ProductProject id"
        )
    if bridge_project_id != project_id:
        raise PackagedPF11EvidenceError(
            "packaged PF11 proof returned inconsistent ProductProject identity"
        )

    for field in _REQUIRED_TRUE_FIELDS:
        if payload.get(field) is not True:
            raise PackagedPF11EvidenceError(
                f"packaged PF11 proof must set {field}=true"
            )
    for field in _REQUIRED_FALSE_FIELDS:
        if payload.get(field) is not False:
            raise PackagedPF11EvidenceError(
                f"packaged PF11 proof may not set {field}=true"
            )
    return payload
