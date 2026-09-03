from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILES = (
    ROOT / "state" / "PROJECT_STATUS.md",
    ROOT / "state" / "PARALLEL_EXECUTION_BOARD.md",
)

LEGACY_STATIC_AUTHORITY = (
    "## Canonical main",
    "Current main at this reconciliation point:",
    "Scheduled Product Factory dependency order",
    "Current real PF5 code/evidence PR",
    "df84a72d6705aa78cb0c69df9e47a367098b74bb",
)


def test_coordination_state_documents_are_explicitly_non_authoritative_snapshots() -> None:
    for path in STATE_FILES:
        text = path.read_text(encoding="utf-8")
        assert "LIVE_GITHUB_PRECEDENCE=true" in text
        assert "NON_AUTHORITATIVE_SNAPSHOT=true" in text
        assert "Do not use this snapshot to decide current ownership" in text
        for stale_pattern in LEGACY_STATIC_AUTHORITY:
            assert stale_pattern not in text
