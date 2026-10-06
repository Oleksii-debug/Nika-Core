from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M5_PROOF = ROOT / "scripts" / "m5_uia_proof.ps1"
WRAPPER = ROOT / "scripts" / "v01_autostart_uia_proof.ps1"
M11 = ROOT / ".github" / "workflows" / "m11-windows-release.yml"
M12 = ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml"


def test_packaged_factory_operator_is_exercised_by_physical_m5_uia_proof() -> None:
    source = M5_PROOF.read_text(encoding="utf-8")
    product_command = (
        "$productCommand = 'Створи застосунок для контрольованої UIA перевірки'"
    )
    status_command = (
        "Set-BoundControlValue $commandControl "
        "'покажи поточний статус Product Factory'"
    )

    assert product_command in source
    assert "Wait-BoundTextEvidence 'Оператор Product Factory'" in source
    assert "Wait-BoundTextEvidence $productId" in source
    for evidence in (
        "'unassigned'",
        "'active'",
        "'none'",
        "'not_started'",
        "'inspect_project'",
    ):
        assert evidence in source
    assert status_command in source
    assert (
        "поточна версія ProductProject ще не має підготовленого execution authority."
        in source
    )
    assert "SELECT COUNT(*) FROM tasks" in source
    assert "operator/status proof unexpectedly created a task" in source
    assert source.index(product_command) < source.index(
        "$sourceRootControl = Wait-DescendantName"
    )


def test_release_gates_execute_factory_operator_proof_on_packaged_exe() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "$proof = Join-Path $PSScriptRoot 'm5_uia_proof.ps1'" in wrapper
    assert wrapper.count("-VerifySourceSetup") >= 2

    m11 = M11.read_text(encoding="utf-8")
    m12 = M12.read_text(encoding="utf-8")
    wrapper_name = "v01_autostart_uia_proof.ps1"
    assert wrapper_name in m11
    assert wrapper_name in m12
    assert m11.count('      - "scripts/m5_uia_proof.ps1"') == 2

    source_paths = (
        "src/nika_core/product_command/**",
        "src/nika_core/product_project.py",
        "src/nika_core/product_decisions.py",
        "src/nika_core/product_factory_packaged_journey.py",
        "src/nika_core/product_factory_packaged_planning.py",
        "src/nika_core/product_factory_packaged_status.py",
    )
    for source_path in source_paths:
        assert m11.count(f'      - "{source_path}"') == 2

    regression_paths = (
        "tests/test_product_factory_packaged_journey.py",
        "tests/test_packaged_product_factory_status.py",
        "tests/test_m5_product_project_semantic_status.py",
        "tests/test_m5_product_factory_operator_uia.py",
    )
    regressions = m11.split("- name: Run M11 M12 source regressions", 1)[1]
    regressions = regressions.split("- name: Build standalone", 1)[0]
    for regression_path in regression_paths:
        assert m11.count(f'      - "{regression_path}"') == 2
        assert f"          {regression_path}" in regressions
