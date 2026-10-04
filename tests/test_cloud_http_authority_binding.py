from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)


class _UnusedCredentialResolver:
    def resolve(self, credential_ref: str) -> str:
        raise AssertionError("host validation must not resolve credentials")


def _config(base_url: str) -> ApiModelRouteConfig:
    return ApiModelRouteConfig(
        provider_id="cloud-test",
        base_url=base_url,
        default_model="model-a",
        credential_ref="env:SYNTHETIC_TEST_CREDENTIAL",
    )


@pytest.mark.parametrize("separator", ("\u3002", "\uff0e", "\uff61"))
def test_ambiguous_unicode_hostname_cannot_retarget_authorized_cloud_effect(
    separator: str,
) -> None:
    url = f"https://approved.example{separator}unapproved.example/v1"
    assert urlsplit(url).hostname != httpx.URL(url).host

    # Previously the capability declared the urlsplit host, while the actual
    # HTTPX request would send credentials to the IDNA-normalized host.
    with pytest.raises(ValueError, match="host differs from HTTP transport"):
        _config(url)


@pytest.mark.parametrize(
    ("base_url", "expected_authority"),
    (
        ("https://approved.example/v1", "approved.example"),
        ("https://APPROVED.EXAMPLE.:443/v1", "approved.example"),
        ("https://bücher.example/v1", "bücher.example"),
        ("https://xn--bcher-kva.example/v1", "xn--bcher-kva.example"),
        ("https://faß.de/v1", "faß.de"),
        ("https://xn--fa-hia.de/v1", "xn--fa-hia.de"),
        ("https://ς.gr/v1", "ς.gr"),
        ("https://xn--3xa.gr/v1", "xn--3xa.gr"),
        ("https://[::1]:443/v1", "::1"),
    ),
)
def test_valid_host_spellings_preserve_exact_outbound_authority(
    base_url: str, expected_authority: str,
) -> None:
    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(base_url),
        credential_resolver=_UnusedCredentialResolver(),
    )
    declared = provider.capabilities.effect_network_host
    assert declared == expected_authority
    assert declared is not None
    transport_url = httpx.URL(base_url)
    assert declared in (
        transport_url.host.rstrip("."),
        transport_url.raw_host.decode("ascii").rstrip("."),
    )


@pytest.mark.parametrize(
    "base_url",
    ("https://approved.example:99999/v1", "https://approved.example:0/v1"),
)
def test_invalid_httpx_port_is_rejected_at_route_configuration(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="invalid HTTP authority"):
        _config(base_url)
