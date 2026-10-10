from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.toolsmith.contracts import ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import ProcessExecutionError, run_typed_process


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("timeout_seconds", 10**1000),
        ("max_output_bytes", 10**1000),
        ("max_changed_files", 10**1000),
    ),
)
def test_tampered_budget_fails_before_containment_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid: int,
) -> None:
    budget = ResourceBudget(1, 1024, 1)
    object.__setattr__(budget, field, invalid)
    reached: list[str] = []

    def forbidden_pinned_runtime(*_args: object, **_kwargs: object) -> object:
        reached.append("executable")
        raise AssertionError("executable resolution must not run for invalid budget")

    def forbidden_workspace(*_args: object, **_kwargs: object) -> object:
        reached.append("workspace")
        raise AssertionError("workspace resolution must not run for invalid budget")

    monkeypatch.setattr(
        "nika_core.toolsmith.execution._pinned_runtime_argv",
        forbidden_pinned_runtime,
    )
    monkeypatch.setattr(
        "nika_core.toolsmith.execution._validate_process_workspace_root",
        forbidden_workspace,
    )

    with pytest.raises(ProcessExecutionError, match="resource budget"):
        run_typed_process(
            (),
            process_policy=ProcessPolicy(("python",)),
            resource_budget=budget,
            cwd=tmp_path,
            environment={},
        )

    assert reached == []
