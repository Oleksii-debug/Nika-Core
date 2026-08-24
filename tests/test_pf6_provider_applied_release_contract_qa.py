from __future__ import annotations

from dataclasses import fields
from typing import get_args, get_type_hints

from nika_core.product_factory_deployment import ProviderDeploymentResult, ReleaseRef


def _contains_release_ref(annotation: object) -> bool:
    if annotation is ReleaseRef:
        return True
    return any(_contains_release_ref(arg) for arg in get_args(annotation))


def test_provider_deployment_result_carries_exact_applied_release_ref() -> None:
    """Provider apply evidence must prove the complete exact release identity.

    PF6 authority is project + version + source SHA + artifact digest.  A deploy
    result that reports only SHA/digest/evidence cannot independently prove that
    the provider applied the requested version/project release rather than a
    same-SHA substitution.  The provider result contract therefore needs at
    least one field carrying ReleaseRef (optionally nullable while uncertain).
    """

    hints = get_type_hints(ProviderDeploymentResult)
    exact_release_fields = tuple(
        field.name
        for field in fields(ProviderDeploymentResult)
        if _contains_release_ref(hints.get(field.name))
    )

    assert exact_release_fields, (
        "ProviderDeploymentResult must expose provider-reported exact ReleaseRef "
        "authority; source SHA/artifact digest alone cannot authorize PF6 release identity"
    )
