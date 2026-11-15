"""Independent inventory from the inspected packaged HTTP transport, not producer output."""

from __future__ import annotations

import re
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.packaging import notices

# Confirmed in the PYZ of the M11 final ZIP, SHA256
# bcf482d8a1df35baa00c46078250b9e7d0e006f6498e6382b3911b2b9233be1c.
# This set deliberately does not derive from RUNTIME_DISTRIBUTIONS: doing so
# would reproduce the original mutually agreeing producer/verifier blind spot.
PACKAGED_HTTP_DISTRIBUTIONS = {"httpx", "httpcore", "anyio", "h11", "idna", "certifi"}


def test_actual_packaged_http_transport_has_release_notice_coverage() -> None:
    assert PACKAGED_HTTP_DISTRIBUTIONS <= set(notices.RUNTIME_DISTRIBUTIONS)


@pytest.mark.parametrize("omitted", sorted(PACKAGED_HTTP_DISTRIBUTIONS))
def test_missing_bundled_http_notice_fails_and_regeneration_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, omitted: str,
) -> None:
    monkeypatch.setattr(notices, "_python_license", lambda: "Python license")

    def distribution(name: str) -> SimpleNamespace:
        metadata = Message()
        metadata["Name"] = name
        metadata["License-Expression"] = "MPL-2.0" if name == "certifi" else "MIT"
        return SimpleNamespace(version="1.0", metadata=metadata, files=())

    monkeypatch.setattr(notices.metadata, "distribution", distribution)
    path = notices.build_third_party_notices(tmp_path)
    original = path.read_text(encoding="utf-8")
    assert notices.verify_third_party_notices(tmp_path) == ()
    assert "https://pypi.org/project/certifi/1.0/#files" in original
    altered, count = re.subn(
        rf"(?ms)^===== {re.escape(omitted)} 1\.0 =====\n.*?(?=^===== |\Z)",
        "", original,
    )
    assert count == 1
    path.write_text(altered, encoding="utf-8")
    assert f"notices:{omitted}" in notices.verify_third_party_notices(tmp_path)
    notices.build_third_party_notices(tmp_path)
    assert notices.verify_third_party_notices(tmp_path) == ()


def test_certifi_source_reference_binds_and_escapes_the_resolved_version() -> None:
    metadata = Message()
    metadata["Name"] = "certifi"
    metadata["License-Expression"] = "MPL-2.0"
    installed = SimpleNamespace(version="2026.10.23?redirect=other", metadata=metadata, files=())
    _title, body = notices._distribution_section("certifi", installed)
    assert "certifi/2026.10.23%3Fredirect%3Dother/#files" in body
