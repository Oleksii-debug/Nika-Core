from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.process import ProcessResult
from nika_core.media.yt_dlp import YtDlpAdapter

_CANARY = "nika_slot3_media_query_secret_canary"
_CREDENTIAL_QUERY_ALIASES = (
    "api-key",
    "client_secret",
    "client-secret",
    "subscription-key",
    "subscription_key",
    "x-api-key",
)


class _FakeRunner:
    def __init__(self, *, metadata_url: str | None = None) -> None:
        self.metadata_url = metadata_url
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        del cwd, timeout_seconds
        normalized = tuple(argv)
        self.calls.append(normalized)
        webpage_url = self.metadata_url or normalized[-1]
        payload = {
            "id": "fixture-video",
            "title": "Credential query fixture",
            "duration": 1.0,
            "webpage_url": webpage_url,
        }
        return ProcessResult(
            argv=normalized,
            returncode=0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
            elapsed_seconds=0.01,
        )


@pytest.mark.parametrize("query_key", _CREDENTIAL_QUERY_ALIASES)
def test_credential_query_alias_is_rejected_before_media_subprocess(
    tmp_path: Path,
    query_key: str,
) -> None:
    runner = _FakeRunner()
    adapter = YtDlpAdapter(runner)  # type: ignore[arg-type]
    source_url = f"https://media.example/watch?{query_key}={_CANARY}"

    with pytest.raises(MediaError) as caught:
        adapter.discover(source_url, cwd=tmp_path)

    assert caught.value.code is MediaErrorCode.AUTH_REQUIRED
    assert _CANARY not in str(caught.value)
    assert _CANARY not in repr(caught.value)
    assert runner.calls == []


@pytest.mark.parametrize("query_key", _CREDENTIAL_QUERY_ALIASES)
def test_upstream_credential_query_alias_never_enters_persistence_safe_metadata(
    tmp_path: Path,
    query_key: str,
) -> None:
    credential_url = f"https://media.example/watch?{query_key}={_CANARY}"
    runner = _FakeRunner(metadata_url=credential_url)
    adapter = YtDlpAdapter(runner)  # type: ignore[arg-type]

    discovery = adapter.discover(
        "https://media.example/watch?id=public-fixture",
        cwd=tmp_path,
    )

    assert _CANARY not in discovery.source.locator
    serialized_metadata = json.dumps(discovery.sanitized_metadata, sort_keys=True)
    assert _CANARY not in serialized_metadata


def test_benign_subscription_query_remains_supported(tmp_path: Path) -> None:
    source_url = "https://media.example/watch?subscription=public-catalog"
    runner = _FakeRunner()
    adapter = YtDlpAdapter(runner)  # type: ignore[arg-type]

    discovery = adapter.discover(source_url, cwd=tmp_path)

    assert discovery.source.locator == source_url
    assert len(runner.calls) == 1
