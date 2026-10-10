from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts import c3_browser_agent_factory_acceptance as c3


def test_product_factory_program_host_materializes_and_accepts_generated_product(
    tmp_path: Path,
) -> None:
    factory, sources = c3._run_factory_program(tmp_path, "a" * 40)

    assert factory["factory_program_host_used"] is True
    assert factory["restart_exact"] is True
    assert factory["all_components_accepted"] is True
    assert factory["component_states"] == {
        "commerce-fixture": "accepted",
        "package": "accepted",
        "semantic-browser-agent": "accepted",
    }
    assert factory["component_attempts"] == {
        "commerce-fixture": 1,
        "package": 1,
        "semantic-browser-agent": 1,
    }
    assert factory["worker_dispatches"] == [
        "commerce-fixture",
        "semantic-browser-agent",
        "package",
    ]
    assert len(factory["generated_files"]) == 6
    assert len(factory["generated_sha256"]) == 6
    assert set(factory["generated_files"]) == set(sources)


def test_generated_agent_contract_is_semantic_only_and_reconcile_before_retry(
    tmp_path: Path,
) -> None:
    _factory, sources = c3._run_factory_program(tmp_path, "b" * 40)
    agent = sources["generated/c3_browser_agent/agent.py"]

    assert "ControlLocator" in agent
    assert "reconcile-read-only-before-bounded-idempotent-retry" in agent
    assert "USES_CSS_XPATH = False" in agent
    assert "USES_POSITIONAL_TARGETING = False" in agent
    assert "USES_COORDINATES = False" in agent
    forbidden = ("xpath=", ".first", ".last", ".nth(", "mouse.", "get_by_test_id")
    assert all(token not in agent for token in forbidden)


def test_generated_product_package_contains_source_and_canonical_manifest(tmp_path: Path) -> None:
    factory, sources = c3._run_factory_program(tmp_path / "factory", "c" * 40)
    output = tmp_path / "output"
    output.mkdir()
    package = c3.browser_proof._package(
        output,
        "c" * 40,
        factory,
        {"semantic_only": True, "simulated_order_count": 1},
    )
    package = c3._augment_package_with_generated_product(
        package,
        output_root=output,
        source_sha="c" * 40,
        generated_sources=sources,
    )

    bundle = Path(package["bundle"])
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_sha"] == "c" * 40
    assert package["generated_source_files"] == 6
    assert package["manifest_files"] == 9
    assert (
        bundle / "generated-product" / "generated" / "c3_browser_agent" / "agent.py"
    ).is_file()
    with zipfile.ZipFile(package["zip"]) as archive:
        names = set(archive.namelist())
    assert (
        "c3-browser-agent-product/generated-product/generated/c3_browser_agent/agent.py"
        in names
    )
    assert "c3-browser-agent-product/release-manifest.json" in names
