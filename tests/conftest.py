from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nika_core.model_gateway.gateway import ModelGateway
from nika_core.product_factory_orchestration import OwnershipLease

_LEGACY_CLOUD_AUTH_FILES = frozenset(
    {
        "test_intelligence_modes.py",
        "test_intelligence_provenance.py",
        "test_m4_model_tools.py",
        "test_model_gateway_api_route.py",
        "test_multi_agent_model_gateway_runtime.py",
        "test_v01_model_runtime_binding.py",
        "test_v01_packaged_model_runtime_composition.py",
        "test_v01_packaged_model_selection_modes.py",
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
    These legacy modules test routing, transport, provenance, cancellation,
    runtime composition, and provider error normalization downstream of
    authorization, so they get an explicit synthetic authorizer instead of
    relying on the old implicit allow.
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

class _AllowLegacyReviewAuthority:
    """Explicit trusted test double for legacy Product Factory suites."""

    def verify(self, subject, evidence_refs):
        return bool(subject.fingerprint and evidence_refs)


@pytest.fixture(autouse=True)
def _legacy_product_factory_authority_compat(request, monkeypatch):
    module = request.module
    module_file = Path(module.__file__).name
    if module_file not in {
        "test_product_factory_program_host.py",
        "test_product_factory_scale_recovery.py",
    }:
        return

    original_binding = module.ProductProjectCoordinatorBinding

    def trusted_binding(*args, **kwargs):
        binding = original_binding(*args, **kwargs)
        binding._review_authority = _AllowLegacyReviewAuthority()
        return binding

    monkeypatch.setattr(module, "ProductProjectCoordinatorBinding", trusted_binding)

    if module_file == "test_product_factory_program_host.py":
        original_envelope = module._envelope

        def trusted_envelope(*args, **kwargs):
            return replace(
                original_envelope(*args, **kwargs),
                producer_actor_id="legacy-program-worker",
            )

        monkeypatch.setattr(module, "_envelope", trusted_envelope)

        original_context = module.CodingWorkerDispatchContext

        def trusted_context(*args, **kwargs):
            if "ownership_lease" not in kwargs:
                workspace_lease = kwargs.get("lease")
                if workspace_lease is None:
                    raise AssertionError("legacy adapter context requires workspace lease")
                component_id = Path(workspace_lease.workspace_root).name
                kwargs["ownership_lease"] = OwnershipLease(
                    lease_id=f"owner:{workspace_lease.lease_id}",
                    worker_id="legacy-program-worker",
                    component_ids=(component_id,),
                    allowed_paths=(f"src/{component_id}",),
                )
            return original_context(*args, **kwargs)

        monkeypatch.setattr(module, "CodingWorkerDispatchContext", trusted_context)
    else:
        original_envelope = module._successful_envelope

        def trusted_envelope(*args, **kwargs):
            return replace(
                original_envelope(*args, **kwargs),
                producer_actor_id="legacy-scale-worker",
            )

        monkeypatch.setattr(module, "_successful_envelope", trusted_envelope)

