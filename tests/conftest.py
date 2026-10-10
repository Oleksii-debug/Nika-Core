from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.product_factory_orchestration import OwnershipLease


class _AllowLegacyReviewAuthority:
    """Trusted test double for suites that predate PF4 authority composition.

    The fixture is deliberately scoped to legacy integration modules. PF4's dedicated
    authority tests use the real production composition and are never patched here.
    """

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
