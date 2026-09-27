from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.process import ProcessResult
from nika_core.media.yt_dlp import YtDlpAdapter

_CREDENTIAL_QUERY_ALIASES = (
    "api-key",
    "client_secret",
    "client-secret",
    "subscription-key",
    "subscription_key",
    "x-api-key",
)
_CANARY = "slot3-media-query-secret-canary"


class FakeRunner:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.argv: list[tuple[str, ...]] = []

    def run(
        self,
        argv,
        *,
        cwd: Path,
        timeout_seconds: float,
        env=None,
    ) -> ProcessResult:
        del cwd, timeout_seconds, env
        normalized = tuple(argv)
        self.argv.append(normalized)
        output = self.outputs.pop(0)
        return ProcessResult(normalized, 0, output, b"", 0.01)


@pytest.mark.parametrize("query_key", _CREDENTIAL_QUERY_ALIASES)
def test_credential_query_aliases_fail_before_yt_dlp_process(
    tmp_path: Path,
    query_key: str,
) -> None:
    runner = FakeRunner([])
    source_url = f"https://media.example/watch?{query_key}={_CANARY}"

    with pytest.raises(MediaError) as caught:
        YtDlpAdapter(runner).discover(source_url, cwd=tmp_path)

    assert caught.value.code == MediaErrorCode.AUTH_REQUIRED
    assert _CANARY not in str(caught.value)
    assert runner.argv == []


@pytest.mark.parametrize("query_key", _CREDENTIAL_QUERY_ALIASES)
def test_upstream_credential_query_aliases_never_persist(
    tmp_path: Path,
    query_key: str,
) -> None:
    secret_url = f"https://media.example/watch?{query_key}={_CANARY}"
    payload = {
        "id": "credential-alias-fixture",
        "title": "Credential alias fixture",
        "duration": 1,
        "webpage_url": secret_url,
    }
    runner = FakeRunner([json.dumps(payload).encode()])

    result = YtDlpAdapter(runner).discover(
        "https://media.example/watch?id=public-fixture",
        cwd=tmp_path,
    )

    assert _CANARY not in result.source.locator
    assert _CANARY not in json.dumps(result.sanitized_metadata)
    assert len(runner.argv) == 1


def test_benign_subscription_query_remains_supported(tmp_path: Path) -> None:
    source_url = "https://media.example/watch?subscription=public-catalog"
    payload = {
        "id": "benign-query-fixture",
        "title": "Benign query fixture",
        "duration": 1,
        "webpage_url": source_url,
    }
    runner = FakeRunner([json.dumps(payload).encode()])

    result = YtDlpAdapter(runner).discover(source_url, cwd=tmp_path)

    assert result.source.locator == source_url
    assert len(runner.argv) == 1
