from __future__ import annotations

import hashlib
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.packaging import notices


def _distribution(root: Path, name: str = "proxy_tools", version: str = "0.1.0"):
    metadata = Message()
    metadata["Name"] = name
    metadata["License"] = "MIT"
    return SimpleNamespace(
        version=version, metadata=metadata, files=(), locate_file=lambda item: root / item,
    )


def test_metadata_only_license_is_not_redistribution_notice(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="No license text"):
        notices._distribution_section("httpx", _distribution(tmp_path, "httpx", "0.28.1"))


def test_proxy_license_retains_exact_upstream_text_and_conflicting_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Small source fixture tests the identity contract; the real Windows build
    # separately checks the installed module against the pinned upstream digest.
    source = tmp_path / "proxy_tools" / "__init__.py"
    source.parent.mkdir()
    source.write_bytes(b"reviewed source\n")
    monkeypatch.setattr(
        notices, "_PROXY_TOOLS_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    dist = _distribution(tmp_path)
    title, body = notices._distribution_section("proxy-tools", dist)
    assert title == "proxy_tools 0.1.0"
    assert "Declared license: MIT" in body
    assert "The BSD License (BSD)" in body
    assert "Copyright (c) 2013 Armin Ronacher" in body
    assert "Copyright (c) 2014 Jonathan Tushman" in body
    assert "db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6/LICENSE.txt" in body
    assert hashlib.sha256(notices._PROXY_TOOLS_LICENSE.encode()).hexdigest() == (
        "a428fb8a2e762af3eb0a6edbbb88e9b42ccfee80fd9b423958bcacf9b9abbfe4"
    )
    source.write_bytes(b"changed source\n")
    with pytest.raises(RuntimeError, match="Unreviewed proxy-tools"):
        notices._distribution_section("proxy-tools", dist)
    source.write_bytes(b"reviewed source\n")
    with pytest.raises(RuntimeError, match="Unreviewed proxy-tools"):
        notices._distribution_section("proxy-tools", _distribution(tmp_path, version="0.2.0"))
    assert notices._distribution_section("proxy-tools", dist) == (title, body)


def test_missing_proxy_source_cannot_use_pinned_notice(tmp_path: Path) -> None:
    (tmp_path / "proxy_tools").mkdir()
    with pytest.raises(RuntimeError, match="Unreviewed proxy-tools"):
        notices._distribution_section("proxy-tools", _distribution(tmp_path))
