from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.packaging import notices
from scripts import m11_sbom_evidence


class _FakeMetadata(dict[str, str]):
    def get_all(self, key: str, default: list[str] | None = None) -> list[str]:
        del key
        return [] if default is None else default


class _FakeDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.version = version
        self.metadata = _FakeMetadata({"Name": name, "License-Expression": "MIT"})
        self.files: tuple[object, ...] = ()


def _fake_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {"httpcore": "1.0.9", "httpx": "0.28.1"}

    def distribution(name: str) -> _FakeDistribution:
        try:
            version = versions[name]
        except KeyError as exc:
            raise notices.metadata.PackageNotFoundError(name) from exc
        return _FakeDistribution(name, version)

    monkeypatch.setattr(notices, "_python_license", lambda: "Python license")
    monkeypatch.setattr(notices.metadata, "distribution", distribution)


def test_notices_use_exact_runtime_authority_and_reject_stale_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_runtime(monkeypatch)
    runtime_names = ("httpx", "httpcore")

    target = notices.build_third_party_notices(
        tmp_path,
        distribution_names=runtime_names,
    )
    text = target.read_text(encoding="utf-8")

    assert "===== httpx 0.28.1 =====" in text
    assert "===== httpcore 1.0.9 =====" in text
    assert notices.verify_third_party_notices(
        tmp_path,
        distribution_names=runtime_names,
    ) == ()

    target.write_text(
        text + "\n===== stale-package 9.9 =====\nDeclared license: MIT\n",
        encoding="utf-8",
    )
    assert "notices:unexpected-section:stale-package 9.9" in notices.verify_third_party_notices(
        tmp_path,
        distribution_names=runtime_names,
    )


def test_notices_fail_when_runtime_authority_component_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_runtime(monkeypatch)
    runtime_names = ("httpx", "httpcore")
    target = notices.build_third_party_notices(
        tmp_path,
        distribution_names=runtime_names,
    )
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text.replace(
            "\n===== httpx 0.28.1 =====\nDeclared license: MIT",
            "",
        ),
        encoding="utf-8",
    )

    findings = notices.verify_third_party_notices(
        tmp_path,
        distribution_names=runtime_names,
    )
    assert "notices:httpx" in findings


def test_sbom_script_extracts_notice_names_from_persisted_supply_components() -> None:
    supply_chain = {
        "components": [
            {"name": "httpcore", "version": "1.0.9"},
            {"name": "httpx", "version": "0.28.1"},
        ]
    }

    assert m11_sbom_evidence._runtime_distribution_names(supply_chain) == (
        "httpcore",
        "httpx",
    )

    script = (Path(__file__).resolve().parents[1] / "scripts/m11_sbom_evidence.py").read_text(
        encoding="utf-8"
    )
    assert "supply_chain = json.loads(supply_path.read_text" in script
    assert script.count("distribution_names=runtime_distributions") == 2
    assert "tuple(notice_findings)" in script
