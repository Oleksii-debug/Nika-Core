from __future__ import annotations

from pathlib import Path

from scripts.verify_packaged_notices import module_notice_findings


def test_frozen_runtime_inventory_is_independent_of_the_notice_producer() -> None:
    modules = {"httpx._client", "markdown_it.main", "_pytest._version", "wheel", "os", "nika_core"}
    titles = {"httpx 0.28.1", "markdown-it-py 4.3.0", "pytest 8.4.2", "wheel 0.48.0"}
    assert module_notice_findings(modules, titles) == ()
    assert module_notice_findings(modules, titles - {"wheel 0.48.0"}) == (
        "missing-packaged-distribution-notice:wheel",
    )
    assert module_notice_findings(modules | {"unreviewed_new_package"}, titles) == (
        "unmapped-packaged-module:unreviewed_new_package",
    )
    # The previously demonstrated ambient Click must not silently enter a build.
    assert module_notice_findings({"click"}, set()) == (
        "missing-packaged-distribution-notice:click",
    )


def test_exact_package_jobs_prove_module_notice_coverage() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    command = "python scripts/verify_packaged_notices.py --bundle"
    for name in ("m11-windows-release.yml", "m12-prehuman-release-gate.yml"):
        source = (workflows / name).read_text(encoding="utf-8")
        assert f"run: {command} dist/NikaCore" in source
    source = (workflows / "m12-prehuman-release-gate.yml").read_text(encoding="utf-8")
    assert f"{command} $extractRoot" in source
