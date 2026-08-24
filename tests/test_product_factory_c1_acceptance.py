from __future__ import annotations

from pathlib import Path

from nika_core.product_factory_c1_acceptance import C1MediumAppAcceptanceRunner


def test_c1_medium_app_uses_real_product_factory_lifecycle(tmp_path: Path) -> None:
    evidence = C1MediumAppAcceptanceRunner(
        root=tmp_path / "C1 medium app",
        source_sha="a" * 40,
    ).run()

    attempts = dict(evidence.component_attempts)
    assert evidence.project_id == "product-c1-medium-expense-manager"
    assert evidence.spec_version == 2
    assert evidence.spec_history_versions == (1, 2)
    assert evidence.research_package_id == "c1-research-local-windows-v1"
    assert evidence.selected_option_id == "local-tk-sqlite"
    assert len(evidence.ownership_lease_ids) == 5
    assert evidence.independent_qa_role_ids
    assert attempts["01-storage"] == 2
    assert attempts["04-desktop-ui"] == 2
    assert evidence.worker_repair_component == "01-storage"
    assert evidence.rejected_qa_component == "04-desktop-ui"
    assert evidence.restart_recovery_proven is True
    assert evidence.all_components_accepted is True
    assert evidence.upgrade_safe_data_proven is True
    assert evidence.accessible_control_contract_proven is True
    assert len(evidence.generated_test_digest) == 64
    assert len(evidence.installer_sha256) == 64
    assert evidence.package_path is None
    assert evidence.package_sha256 is None
    assert evidence.installed_executable_proven is False
    assert evidence.packaged_restart_proven is False
    assert evidence.human_tested is False
    assert evidence.nvda_verified is False
    assert evidence.production_release_ready is False


def test_c1_generated_product_is_component_scoped_not_monolithic(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    C1MediumAppAcceptanceRunner(root=root, source_sha="b" * 40).run()
    product = root / "product"

    expected = (
        "src/c1_expense_manager/data/storage.py",
        "src/c1_expense_manager/config/settings.py",
        "src/c1_expense_manager/domain/service.py",
        "src/c1_expense_manager/desktop/ui.py",
        "src/c1_expense_manager/main.py",
        "installer/install.ps1",
        "tests/test_storage_upgrade.py",
        "tests/test_settings.py",
        "tests/test_domain.py",
        "tests/test_accessibility_contract.py",
        "tests/test_package_contract.py",
    )
    for relative in expected:
        assert (product / relative).is_file(), relative

    ui = (product / "src/c1_expense_manager/desktop/ui.py").read_text(encoding="utf-8")
    assert "<Alt-a>" in ui
    assert "<Alt-r>" in ui
    assert "pyautogui" not in ui

    installer = (product / "installer" / "install.ps1").read_text(encoding="utf-8")
    assert "Get-ChildItem -LiteralPath $bundle" in installer
    assert "Copy-Item -LiteralPath $_.FullName" in installer
    assert "Copy-Item -LiteralPath (Join-Path $BundlePath '*')" not in installer
    assert "Start-Process -Verb RunAs" not in installer
