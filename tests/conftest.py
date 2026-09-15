from __future__ import annotations

from typing import Any

import pytest

from nika_core.model_gateway.gateway import ModelGateway

_LEGACY_CLOUD_AUTH_FILES = frozenset(
    {
        "test_intelligence_modes.py",
        "test_intelligence_provenance.py",
        "test_m4_model_tools.py",
        "test_model_gateway_api_route.py",
    }
)


class _AllowLegacyCloudEffectAuthorizer:
    """Explicit authority stub for legacy tests whose subject is downstream of authorization."""

    def authorize_cloud_effect(self, *, request: object, provider: object) -> None:
        del request, provider


_ALLOW_LEGACY_CLOUD_EFFECT = _AllowLegacyCloudEffectAuthorizer()


@pytest.fixture(autouse=True)
def _legacy_cloud_effect_authority(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep legacy CLOUD transport/route tests focused on their original contract.

    #704 made CLOUD execution fail closed unless a current effect authorizer is
    supplied. The dedicated cloud-authority suite owns that security contract.
    These four older modules test routing, transport, provenance, cancellation,
    and provider error normalization downstream of authorization, so they get an
    explicit synthetic authorizer instead of relying on the old implicit allow.
    """

    if request.node.path.name not in _LEGACY_CLOUD_AUTH_FILES:
        return

    original_init = ModelGateway.__init__

    def init_with_legacy_test_authority(
        self: ModelGateway,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("cloud_effect_authorizer", _ALLOW_LEGACY_CLOUD_EFFECT)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ModelGateway, "__init__", init_with_legacy_test_authority)
