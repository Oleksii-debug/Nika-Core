from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.product_factory_packaged_journey import product_project_identity
from nika_core.product_command.routing import route_command
from scripts import nika_windows

COMMAND = "Створи застосунок для керування витратами малого бізнесу"


@pytest.mark.parametrize(
    "corruption",
    (
        "unavailable",
        "non_mapping",
        "missing_selection_key",
        "non_mapping_selection",
        "different_project",
        "different_goal",
        "boolean_version",
        "bad_status_count",
    ),
)
def test_pf11_rejects_invalid_pre_command_state_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    decision = route_command(COMMAND)
    assert decision.normalized_goal is not None
    expected_id = product_project_identity(decision.normalized_goal)
    selected: dict[str, object] = {
        "project_id": expected_id,
        "spec_version": 1,
        "status_count": 0,
        "decision_count": 0,
        "goal": decision.normalized_goal,
    }
    response: object = {"ok": True, "state": {"product_project": selected}}
    if corruption == "unavailable":
        response = {"ok": False, "state": {"product_project": selected}}
    elif corruption == "non_mapping":
        response = None
    elif corruption == "missing_selection_key":
        response = {"ok": True, "state": {}}
    elif corruption == "non_mapping_selection":
        response = {"ok": True, "state": {"product_project": []}}
    elif corruption == "different_project":
        selected["project_id"] = "different-project"
    elif corruption == "different_goal":
        selected["goal"] = "unrelated goal"
    elif corruption == "boolean_version":
        selected["spec_version"] = True
    elif corruption == "bad_status_count":
        selected["status_count"] = True

    class InvalidBridge:
        def get_state(self) -> object:
            return response

        def dispatch(self, _request: object) -> None:
            pytest.fail("PF11 must not dispatch a command after failed recovery")

    monkeypatch.setattr(
        nika_windows, "build_windows_bridge", lambda _config: (InvalidBridge(), object())
    )
    config = AppConfig(database_path=tmp_path / "pre-command-proof.db")
    with pytest.raises((RuntimeError, TypeError), match="PF11"):
        nika_windows._run_pf11_proof(config, command=COMMAND, output_path=None)
