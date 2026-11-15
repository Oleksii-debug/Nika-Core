from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_packaged_notices
from scripts.verify_packaged_notices import module_notice_findings


def test_frozen_runtime_inventory_is_independent_of_the_notice_producer() -> None:
    modules = {"httpx._client", "markdown_it.main", "_pytest._version", "wheel", "os", "nika_core"}
    titles = {"httpx", "markdown-it-py", "pytest", "wheel"}
    assert module_notice_findings(modules, titles) == ()
    assert module_notice_findings(modules, titles - {"wheel"}) == (
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


@pytest.mark.parametrize("finding", ["notices:unreadable", "notices:httpx"])
def test_rejected_canonical_evidence_never_reopens_or_parses_unadmitted_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finding: str,
) -> None:
    monkeypatch.setattr(
        verify_packaged_notices, "verify_third_party_notices", lambda _: (finding,),
    )

    def deny_uncontrolled_read(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("Rejected evidence must not be reread")

    monkeypatch.setattr(Path, "read_text", deny_uncontrolled_read)
    assert verify_packaged_notices.verify_bundle(tmp_path) == (finding,)
